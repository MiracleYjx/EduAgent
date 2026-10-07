"""Frozen T189 faults: observe no serving and cleanup of owned preload workers."""

from __future__ import annotations

import argparse
import json
import re
import socket
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import psutil
from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def actual_failure_step(messages: str) -> str | None:
    match = re.search(r"^启动失败：\[([^\]\r\n]+)\]", messages, re.MULTILINE)
    return match.group(1) if match else None


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def run(args):
    root = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    private = args.private_root.resolve()
    if not output.is_relative_to(
        root / "benchmark/results"
    ) or not private.is_relative_to(root / ".cache"):
        raise ValueError("Use fresh owned repository directories")
    output.mkdir(parents=True, exist_ok=False)
    private.mkdir(parents=True, exist_ok=False)
    values = dotenv_values(args.source_config)
    original = make_url(values["DATABASE_URL"])
    admin = create_engine(
        original.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    name = "eduagent_t189_" + uuid4().hex[:12]
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_engine(original.set(database=name))
    with engine.begin() as conn:
        conn.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
        )
        conn.execute(text("INSERT INTO alembic_version VALUES ('unknown_t189_fault')"))
    engine.dispose()
    cases = [
        ("missing_config", None, "外置配置"),
        ("invalid_config", {"JWT_SECRET_KEY": "short"}, "配置校验"),
        (
            "database_unreachable",
            {
                "DATABASE_URL": original.set(port=1).render_as_string(
                    hide_password=False
                )
            },
            "PostgreSQL / pgvector",
        ),
        ("redis_unreachable", {"REDIS_URL": "redis://127.0.0.1:1/0"}, "Redis"),
        ("model_network", {"DEEPSEEK_BASE_URL": "http://127.0.0.1:1/v1"}, "模型网络"),
        (
            "missing_local_model",
            {
                "EMBEDDING_PROVIDER": "local",
                "EMBEDDING_MODEL": str(private / "missing-model"),
            },
            "外置模型",
        ),
        (
            "missing_ocr_weights",
            {
                "OCR_ENABLED": "True",
                "OCR_PROVIDER": "rapidocr",
                "OCR_MODEL": "PP-OCRv5-mobile",
                "OCR_MODEL_DIR": str(private / "missing-ocr"),
            },
            "OCR",
        ),
        (
            "migration_unknown_revision",
            {
                "DATABASE_URL": original.set(database=name).render_as_string(
                    hide_password=False
                )
            },
            "数据库迁移",
        ),
    ]
    rows = []
    try:
        for label, changes, expected in cases:
            config = private / (label + ".env")
            if changes is not None:
                config.write_text(
                    "\n".join(
                        k + "=" + json.dumps(v)
                        for k, v in (values | changes).items()
                        if v is not None
                    ),
                    encoding="utf-8",
                )
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0
            owned = {}
            listening = False
            started = time.perf_counter()
            with (output / (label + ".log")).open("w", encoding="utf-8") as stream:
                process = subprocess.Popen(
                    [
                        str(args.exe.resolve()),
                        "--config",
                        str(config),
                        "--no-browser",
                        "--port",
                        str(args.port),
                    ],
                    stdout=stream,
                    stderr=stream,
                    stdin=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NEW_CONSOLE,
                    startupinfo=si,
                )
                try:
                    while process.poll() is None and time.perf_counter() - started < 60:
                        try:
                            for child in psutil.Process(process.pid).children(
                                recursive=True
                            ):
                                owned[child.pid] = child.create_time()
                        except psutil.NoSuchProcess:
                            pass
                        try:
                            with socket.create_connection(
                                ("127.0.0.1", args.port), timeout=0.05
                            ):
                                listening = True
                        except OSError:
                            pass
                        time.sleep(0.05)
                    exit_code = process.wait(timeout=1)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=10)
            messages = (output / (label + ".log")).read_text(encoding="utf-8")
            remaining = []
            for pid, identity in owned.items():
                try:
                    if psutil.Process(pid).create_time() == identity:
                        remaining.append(pid)
                except psutil.NoSuchProcess:
                    pass
            row = {
                "case": label,
                "exit_code": exit_code,
                "expected_step": expected,
                "observed_step": actual_failure_step(messages) == expected,
                "actual_failure_step": actual_failure_step(messages),
                "no_business_port_observed": not listening,
                "owned_preload_pids_observed": list(owned),
                "owned_preload_exited": not remaining,
                "remaining_owned_pids": remaining,
                "elapsed_seconds": time.perf_counter() - started,
            }
            row["passed"] = (
                exit_code == 1
                and row["observed_step"]
                and not listening
                and not remaining
                and "所属 PID=" not in messages
            )
            rows.append(row)
            print(label, row["passed"], flush=True)
    finally:
        with admin.connect() as conn:
            active = conn.scalar(
                text("SELECT count(*) FROM pg_stat_activity WHERE datname=:name"),
                {"name": name},
            )
            removed = active == 0 and name != original.database
            if removed:
                conn.execute(text(f'DROP DATABASE "{name}"'))
        admin.dispose()
        save(
            output / "summary.json",
            {
                "cases": rows,
                "protocol": "v2-evaluation-1",
                "service_gate": "Preloaded processes may exist but never serve before successful preflight",
                "owned_migration_database": name,
                "active_connections": active,
                "database_removed": removed,
            },
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--source-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=19494)
    run(parser.parse_args())
