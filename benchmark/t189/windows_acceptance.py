"""Real frozen Windows protocol; private configs, owned consoles and 1Hz memory."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import httpx
import psutil
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from backend.app.core.config import get_settings
from benchmark.t160.resource_sampler import (
    ResourceSampler,
    evaluate_v2_memory_budget,
    verified_worker_chain,
)


def dump(path, data):
    Path(path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def normal_exit(parent):
    # CREATE_NEW_CONSOLE isolates Ctrl+C from shared services and other consoles.
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.FreeConsole()
    if not kernel.AttachConsole(parent.pid):
        raise OSError(ctypes.get_last_error(), "AttachConsole")
    kernel.SetConsoleCtrlHandler(None, True)
    if not kernel.GenerateConsoleCtrlEvent(0, 0):
        raise OSError(ctypes.get_last_error(), "GenerateConsoleCtrlEvent")
    try:
        parent.wait(timeout=15)
    finally:
        kernel.FreeConsole()
        # Do not propagate the observer's temporary ignore flag into the next EXE.
        kernel.SetConsoleCtrlHandler(None, False)
    return parent.returncode


def launch(exe, config, port, output, target, *, no_browser=False):
    output.mkdir(parents=True, exist_ok=False)
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    started = time.perf_counter()
    row = {
        "start_utc": datetime.now(UTC).isoformat(),
        "target_seconds": target,
        "browser_measurement": (
            "disabled by explicit user instruction"
            if no_browser
            else "real default OS browser request; render verified separately, no source/stub server"
        ),
        "config": config.name,
        "exe_sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
    }
    args = [
        str(exe),
        "--config",
        str(config),
        "--port",
        str(port),
        "--startup-timeout",
        "90",
    ]
    if no_browser:
        args.append("--no-browser")
    stream = (output / "launcher.log").open("w", encoding="utf-8")
    parent = subprocess.Popen(
        args,
        stdout=stream,
        stderr=stream,
        stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        startupinfo=si,
    )
    sampler = ResourceSampler(
        pid=parent.pid,
        pg_container="eduagent-postgres-1",
        redis_container="eduagent-redis-1",
        output_path=output / "resources.jsonl",
    )
    worker = None
    ready_end = None
    try:
        sampler.start()
        deadline = started + 110
        with httpx.Client(timeout=2, trust_env=False) as client:
            while time.perf_counter() < deadline:
                contents = (output / "launcher.log").read_text(
                    "utf-8", errors="replace"
                )
                match = re.search(r"PID=(\d+)", contents)
                if match:
                    worker = int(match[1])
                    row["worker_chain"] = verified_worker_chain(worker, parent.pid)
                    url = f"http://127.0.0.1:{port}"
                    statuses = [
                        client.get(url + path).status_code
                        for path in ("/ready", "/gradio/")
                    ]
                    if statuses == [200, 200]:
                        # Parent logs readiness immediately before calling webbrowser.open.
                        # Allow actual call to complete; do not use a pre-existing live service.
                        time.sleep(0.15)
                        ready_end = time.perf_counter()
                        row.update(
                            readiness=statuses,
                            url=url + "/gradio/",
                            elapsed_seconds=ready_end - started,
                            browser_request_failed="浏览器未打开"
                            in (output / "launcher.log").read_text(
                                "utf-8", errors="replace"
                            ),
                        )
                        break
                if parent.poll() is not None:
                    break
                time.sleep(0.05)
        row["startup_success"] = ready_end is not None
        row["timing_target_met"] = (
            ready_end is not None and ready_end - started < target
        )
        if ready_end:
            sampler.sample_now()
            row["resources"] = sampler.summary(
                start_perf=started, end_perf=time.perf_counter()
            )
            row["memory_budget"] = evaluate_v2_memory_budget(row["resources"])
            row["normal_exit_code"] = normal_exit(parent)
            row["owned_worker_exited"] = not psutil.pid_exists(worker)
        else:
            row["exit_code"] = parent.poll()
    except Exception as exc:  # noqa: BLE001 - retain every real acceptance failure
        row["failure_type"] = type(exc).__name__
    finally:
        sampler.stop()
        # Cleanup is confined to exact Popen identity and verified owned worker.
        if parent.poll() is None:
            parent.terminate()
            parent.wait(timeout=10)
        if worker and psutil.pid_exists(worker):
            proc = psutil.Process(worker)
            if (
                Path(proc.exe()).resolve() == exe.resolve()
                and proc.ppid() == parent.pid
            ):
                proc.terminate()
                proc.wait(timeout=10)
                row["forced_owned_cleanup"] = True
        row["parent_pid"] = parent.pid
        row["worker_pid"] = worker
        stream.close()
        dump(output / "run.json", row)
    return row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Disable repeated UI opening at user request",
    )
    args = parser.parse_args()
    exe = args.exe.resolve()
    output = args.output.resolve()
    private = args.private_root.resolve()
    if not output.is_relative_to(
        ROOT / "benchmark/results"
    ) or not private.is_relative_to(ROOT / ".cache"):
        raise ValueError("Use owned repository evidence/private directories")
    output.mkdir(parents=True, exist_ok=False)
    private.mkdir(parents=True, exist_ok=False)
    settings = get_settings()
    original = make_url(str(settings.database_url))
    admin = create_engine(
        original.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    configs = []
    databases = []
    runs = []
    try:
        for i in range(3):
            name = "eduagent_t189_" + uuid4().hex[:12]
            with admin.connect() as conn:
                conn.execute(text(f'CREATE DATABASE "{name}"'))
            databases.append(name)
            values = {
                "DATABASE_URL": original.set(database=name).render_as_string(
                    hide_password=False
                ),
                "REDIS_URL": str(settings.redis_url),
                "LLM_PROVIDER": settings.llm_provider,
                "DEEPSEEK_API_KEY": settings.deepseek_api_key.get_secret_value(),
                "DEEPSEEK_BASE_URL": str(settings.deepseek_base_url),
                "DEEPSEEK_MODEL": settings.deepseek_model,
                "EMBEDDING_PROVIDER": "bge",
                "EMBEDDING_MODEL": str(args.model_dir.resolve()),
                "RERANK_PROVIDER": "llm",
                "CONFIDENCE_THRESHOLD": "0.7",
                "JWT_SECRET_KEY": "t189-isolated-learning-project-32chars",
                "DEV_MODE": "True",
                "STORAGE_ROOT": str(private / f"storage-{i+1}"),
                "OCR_ENABLED": "False",
            }
            config = private / f"first-{i+1}.env"
            config.write_text(
                "\n".join(k + "=" + json.dumps(v) for k, v in values.items()),
                encoding="utf-8",
            )
            configs.append(config)
            row = launch(
                exe,
                config,
                19491 + i,
                output / f"first-{i+1}",
                30,
                no_browser=args.no_browser,
            )
            row.update(mode="first", repeat=i + 1)
            runs.append(row)
            print(
                "first",
                i + 1,
                row.get("elapsed_seconds"),
                row.get("normal_exit_code"),
                flush=True,
            )
        for i in range(5):
            row = launch(
                exe,
                configs[0],
                19491,
                output / f"subsequent-{i+1}",
                10,
                no_browser=args.no_browser,
            )
            row.update(mode="subsequent", repeat=i + 1)
            runs.append(row)
            print(
                "subsequent",
                i + 1,
                row.get("elapsed_seconds"),
                row.get("normal_exit_code"),
                flush=True,
            )
        dump(
            output / "summary.json",
            {
                "protocol": "v2-evaluation-1",
                "runs": runs,
                "timing_pass": all(r.get("timing_target_met") for r in runs),
                "model_calls": 0,
                "model_network": "actual configured DeepSeek TCP/TLS; credentials/inference not proven",
                "resource_scope": "owned frozen launcher plus actual child subtree working set; shared PostgreSQL container and Redis process RSS; excludes browser/OS; not unique physical pages",
                "annotation": "AI-assisted + developer review, independent teacher 0",
            },
        )
    finally:
        cleanup = []
        with admin.connect() as conn:
            for name in databases:
                active = conn.scalar(
                    text("SELECT count(*) FROM pg_stat_activity WHERE datname=:name"),
                    {"name": name},
                )
                item = {"database": name, "active_connections": active}
                if (
                    active == 0
                    and name != original.database
                    and re.fullmatch(r"eduagent_t189_[a-f0-9]{12}", name)
                ):
                    conn.execute(text(f'DROP DATABASE "{name}"'))
                    item["removed"] = True
                cleanup.append(item)
        dump(output / "cleanup.json", cleanup)
        admin.dispose()


if __name__ == "__main__":
    main()
