"""Zero-cloud real Windows venv-worker sampling proof, never a T160 workload result."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from resource_sampler import ResourceSampler, verified_worker_chain, windows_working_set


def write(path, value):
    candidate = path.with_name("." + path.name + ".tmp")
    with candidate.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    os.replace(candidate, path)


def await_file(path, process=None, seconds=30):
    deadline = time.perf_counter() + seconds
    while not path.exists():
        if time.perf_counter() >= deadline or (
            process is not None and process.poll() is not None
        ):
            raise RuntimeError(
                "Resource probe handshake deadline/worker exit: " + path.name
            )
        time.sleep(0.02)
    return json.loads(path.read_text(encoding="utf-8"))


def worker(directory):
    write(
        directory / "actual-worker.json",
        {"pid": os.getpid(), "parent_pid": os.getppid(), "executable": sys.executable},
    )
    ready = await_file(directory / "controller-ready.json")
    assert ready["verified_worker_pid"] == os.getpid()
    baseline_start = time.perf_counter()
    baseline_local, baseline_error = windows_working_set(os.getpid())
    time.sleep(4)
    baseline_end = time.perf_counter()
    buffer = bytearray(64 * 1_048_576)
    for offset in range(0, len(buffer), 4096):
        buffer[offset] = 173
    allocated_start = time.perf_counter()
    allocated_local, allocated_error = windows_working_set(os.getpid())
    time.sleep(6)
    allocated_end = time.perf_counter()
    write(
        directory / "worker-result.json",
        {
            "pid": os.getpid(),
            "allocated_bytes": len(buffer),
            "baseline_start_perf": baseline_start,
            "baseline_end_perf": baseline_end,
            "allocated_start_perf": allocated_start,
            "allocated_end_perf": allocated_end,
            "baseline_local_working_set_bytes": baseline_local,
            "allocated_local_working_set_bytes": allocated_local,
            "errors": [error for error in (baseline_error, allocated_error) if error],
            "cloud_requests": 0,
            "database_statements": 0,
        },
    )
    await_file(directory / "controller-stop.json")
    assert (
        buffer[0] == 173
    )  # Keep the actual touched allocation alive until sampling stops.


def execute(python, isolation_file, directory):
    assert os.name == "nt", "Actual Windows venv process proof required"
    assert python.is_file()
    isolation = json.loads(isolation_file.read_text(encoding="utf-8"))
    directory.mkdir(parents=True, exist_ok=False)
    command = [
        str(python),
        "-X",
        "utf8",
        str(Path(__file__).resolve()),
        "--worker",
        str(directory),
    ]
    process = None
    sampler = None
    identity = None
    chain = None
    receipt = {
        "kind": "zero_cloud_real_worker_resource_probe",
        "started_at": datetime.now(UTC).isoformat(),
        "cloud_requests": 0,
        "database_statements": 0,
        "docker_scope": "read-only /proc process RSS probes in named existing containers",
        "technical_probe_only": True,
        "formal_resource_budget_proven": False,
    }
    with (directory / "process.log").open("xb") as log:
        try:
            process = subprocess.Popen(
                command,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            identity = await_file(directory / "actual-worker.json", process)
            chain = verified_worker_chain(identity["pid"], process.pid)
            sampler = ResourceSampler(
                pid=identity["pid"],
                pg_container=isolation["pg_container"],
                redis_container=isolation["redis_container"],
                output_path=directory / "resources.jsonl",
            )
            sampler.start()
            write(
                directory / "controller-ready.json",
                {"verified_worker_pid": identity["pid"]},
            )
            phases = await_file(directory / "worker-result.json", process, seconds=25)
            sampler.sample_now()
            sampler.stop()
            baseline = sampler.summary(
                start_perf=phases["baseline_start_perf"],
                end_perf=phases["baseline_end_perf"],
            )
            allocated = sampler.summary(
                start_perf=phases["allocated_start_perf"],
                end_perf=phases["allocated_end_perf"],
            )
            write(directory / "controller-stop.json", {"stop": True})
            code = process.wait(timeout=10)
            rows = [
                json.loads(line)
                for line in (directory / "resources.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            delta = (
                allocated["peaks"]["app_working_set_bytes"]
                - baseline["peaks"]["app_working_set_bytes"]
                if allocated["peaks"]["app_working_set_bytes"] is not None
                and baseline["peaks"]["app_working_set_bytes"] is not None
                else None
            )
            passed = (
                code == 0
                and phases["pid"] == identity["pid"]
                and all(row["pid"] == identity["pid"] for row in rows)
                and baseline["complete_at_requested_sampling_resolution"]
                and allocated["complete_at_requested_sampling_resolution"]
                and delta is not None
                and delta >= 32 * 1_048_576
            )
            receipt.update(
                controller_popen_pid=process.pid,
                actual_worker=identity,
                verified_parent_chain=chain,
                phases=phases,
                baseline=baseline,
                allocated=allocated,
                observed_working_set_increase_bytes=delta,
                observed_working_set_increase_mib=(
                    delta / 1_048_576 if delta is not None else None
                ),
                expected_touched_allocation_bytes=64 * 1_048_576,
                sampled_root_pids=sorted({row["pid"] for row in rows}),
                exit_code=code,
                passed=passed,
                source_sha256={
                    name: hashlib.sha256(
                        (Path(__file__).parent / name).read_bytes()
                    ).hexdigest()
                    for name in ("resource_smoke.py", "resource_sampler.py")
                },
            )
        except Exception as exc:  # noqa: BLE001 - Preserve the actual probe failure.
            receipt.update(
                passed=False, error={"type": type(exc).__name__, "message": str(exc)}
            )
        finally:
            if sampler is not None:
                sampler.stop()
            if process is not None and process.poll() is None:
                assert process.args == command
                if identity is not None and chain is not None:
                    verified_worker_chain(identity["pid"], process.pid)
                    subprocess.run(
                        ["taskkill", "/PID", str(identity["pid"]), "/T", "/F"],
                        check=False,
                        capture_output=True,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=10)
            receipt["completed_at"] = datetime.now(UTC).isoformat()
            write(directory / "receipt.json", receipt)
    print(
        json.dumps(
            {
                "passed": receipt["passed"],
                "cloud_requests": 0,
                "database_statements": 0,
                "receipt": str(directory / "receipt.json"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if receipt["passed"] else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", type=Path)
    parser.add_argument("--isolation-file", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--worker", type=Path)
    args = parser.parse_args()
    if args.worker:
        worker(args.worker.resolve())
        return 0
    if not all((args.python, args.isolation_file, args.output_root)):
        parser.error("controller requires --python, --isolation-file and --output-root")
    return execute(
        args.python.resolve(), args.isolation_file.resolve(), args.output_root.resolve()
    )


if __name__ == "__main__":
    raise SystemExit(main())
