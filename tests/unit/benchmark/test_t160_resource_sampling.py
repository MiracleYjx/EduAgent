"""TCR 19: measured worker identity, sampling coverage and v2 memory budgets."""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[3] / "benchmark/t160"
sys.path.insert(0, str(TOOLS))
sampling = importlib.import_module("resource_sampler")
runner = importlib.import_module("performance_runner")


def observed(start, *, app=300, pg=100, redis=10, error=None):
    values = (app, pg, redis)
    return {
        "perf_counter": start,
        "sample_completed_perf": start + 0.05,
        "sampled_at": "2026-10-03T00:00:00+00:00",
        "app_working_set_bytes": app,
        "postgres_container_process_rss_bytes": pg,
        "redis_container_process_rss_bytes": redis,
        "simultaneous_observed_sum_bytes": (
            sum(values) if all(v is not None for v in values) else None
        ),
        "errors": error or {},
    }


def observer(tmp_path, rows):
    result = sampling.ResourceSampler(
        pid=os.getpid(),
        pg_container="fixture-pg",
        redis_container="fixture-redis",
        output_path=tmp_path / "unused.jsonl",
    )
    result._rows = rows
    return result


def test_one_observation_does_not_prove_a_full_sampling_cycle(tmp_path):
    result = observer(tmp_path, [observed(10.1)]).summary(
        start_perf=10.0, end_perf=10.4
    )
    assert result["sample_count"] == 1
    assert result["peaks"]["app_working_set_bytes"] == 300
    assert result["complete_at_requested_sampling_resolution"] is False


def test_continuous_window_keeps_simultaneous_peak_instead_of_sum_of_peaks(tmp_path):
    result = observer(
        tmp_path, [observed(10.1), observed(11.2, app=100, pg=300)]
    ).summary(start_perf=10.0, end_perf=11.4)
    assert result["complete_at_requested_sampling_resolution"] is True
    assert result["peaks"]["app_working_set_bytes"] == 300
    assert result["peaks"]["postgres_container_process_rss_bytes"] == 300
    assert result["peaks"]["simultaneous_observed_sum_bytes"] == 410


def test_missing_component_cannot_pass_budget_or_be_filled_with_zero(tmp_path):
    result = observer(
        tmp_path,
        [
            observed(
                10.1, redis=None, error={"redis": {"code": "CONTAINER_PROBE_FAILED"}}
            ),
            observed(
                11.2, redis=None, error={"redis": {"code": "CONTAINER_PROBE_FAILED"}}
            ),
        ],
    ).summary(start_perf=10.0, end_perf=11.4)
    assert result["complete_at_requested_sampling_resolution"] is False
    assert result["peaks"]["redis_container_process_rss_bytes"] is None
    assert result["peaks"]["simultaneous_observed_sum_bytes"] is None
    assert sampling.evaluate_v2_memory_budget(result)["budget_pass"] is None


def test_v2_budget_checks_each_component_and_same_sample_total():
    result = {
        "complete_at_requested_sampling_resolution": True,
        "peaks": {
            "app_working_set_bytes": 4_000_000_001,
            "postgres_container_process_rss_bytes": 1_000_000_000,
            "redis_container_process_rss_bytes": 100_000_000,
            "simultaneous_observed_sum_bytes": 5_100_000_001,
        },
    }
    report = sampling.evaluate_v2_memory_budget(result)
    assert report["component_pass"]["app_working_set_bytes"] is False
    assert report["component_pass"]["simultaneous_observed_sum_bytes"] is True
    assert report["budget_pass"] is False
    assert report["limits_bytes"]["redis_container_process_rss_bytes"] == 512_000_000
    assert report["limits_mib"]["app_working_set_bytes"] == 4_000_000_000 / 1_048_576


@pytest.mark.skipif(os.name != "nt", reason="Actual Windows process identity required")
def test_actual_current_worker_identity_and_rejected_unrelated_root():
    pid = os.getpid()
    assert sampling.verified_worker_chain(pid, pid) == [pid]
    with pytest.raises(RuntimeError, match="exact launched"):
        sampling.verified_worker_chain(pid, 0)
    value, error = sampling.windows_working_set(pid)
    assert error is None and value > 0
    value, error = sampling.windows_working_set(0)
    assert value is None and error["code"] == "PROCESS_OPEN_FAILED"


def test_launcher_samples_handshaken_worker_and_waits_with_finite_deadline(
    tmp_path, monkeypatch
):
    directory = tmp_path / "job"
    waits = []
    sampled = []

    class Process:
        pid = 111
        args = None

        def poll(self):
            return 0

        def wait(self, *, timeout=None):
            waits.append(timeout)
            assert (
                timeout == 360
            ), "single import budget300s plus explicit process startup/cleanup allowance60s"
            return 0

    def popen(args, **kwargs):
        process = Process()
        process.args = args
        (directory / "actual-worker.json").write_text(
            json.dumps({"pid": 222, "parent_pid": 111}), encoding="utf-8"
        )
        return process

    class Observer:
        def __init__(self, *, pid, **kwargs):
            sampled.append(pid)

        def start(self):
            return self

        def stop(self):
            return None

        def summary(self, **kwargs):
            return {"complete_at_requested_sampling_resolution": False, "peaks": {}}

    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    monkeypatch.setattr(
        runner, "verified_worker_chain", lambda worker, parent: [worker, parent]
    )
    monkeypatch.setattr(runner, "ResourceSampler", Observer)
    result = runner.launch_job(
        {"directory": str(directory), "trials": [{"mode": "cold", "repetition": 1}]},
        {},
        {"pg_container": "fixture-pg", "redis_container": "fixture-redis"},
    )
    assert sampled == [222]
    assert waits == [360]
    assert result["pid"] == 222 and result["controller_popen_pid"] == 111


def test_application_working_set_tracks_only_root_and_actual_descendants(monkeypatch):
    monkeypatch.setattr(
        sampling,
        "windows_process_parents",
        lambda: {10: 9, 11: 10, 12: 11, 9: 8, 100: 9},
        raising=False,
    )
    calls = []

    def memory(pid):
        calls.append(pid)
        return {10: 100, 11: 50, 12: 25}[pid], None

    monkeypatch.setattr(sampling, "windows_working_set", memory)
    value, rows, error = sampling.windows_application_working_set(10)
    assert value == 175 and error is None
    assert [row["pid"] for row in rows] == [10, 11, 12]
    assert calls == [10, 11, 12]


def test_descendant_exiting_during_sample_keeps_application_total_unknown(monkeypatch):
    monkeypatch.setattr(
        sampling, "windows_process_parents", lambda: {10: 9, 11: 10}, raising=False
    )
    monkeypatch.setattr(
        sampling,
        "windows_working_set",
        lambda pid: (
            (100, None) if pid == 10 else (None, {"code": "PROCESS_OPEN_FAILED"})
        ),
    )
    value, rows, error = sampling.windows_application_working_set(10)
    assert value is None and error["code"] == "APPLICATION_PROCESS_SAMPLE_MISSING"
    assert rows[1]["working_set_bytes"] is None


def test_process_deadline_retains_timeout_receipt_without_inventing_business_terminal(
    tmp_path, monkeypatch
):
    directory = tmp_path / "job"

    class Process:
        pid = 111
        args = None
        terminated = False

        def poll(self):
            return 1 if self.terminated else None

        def wait(self, *, timeout=None):
            if not self.terminated:
                raise runner.subprocess.TimeoutExpired(self.args, timeout)
            return 1

        def terminate(self):
            self.terminated = True

    def popen(args, **kwargs):
        process = Process()
        process.args = args
        (directory / "actual-worker.json").write_text(
            json.dumps({"pid": 222, "parent_pid": 111}), encoding="utf-8"
        )
        return process

    class Observer:
        def __init__(self, **kwargs):
            pass

        def start(self):
            return self

        def stop(self):
            return None

        def summary(self, **kwargs):
            return {"complete_at_requested_sampling_resolution": False, "peaks": {}}

    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    monkeypatch.setattr(runner.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runner, "verified_worker_chain", lambda worker, parent: [worker, parent]
    )
    monkeypatch.setattr(runner, "ResourceSampler", Observer)
    result = runner.launch_job(
        {"directory": str(directory), "trials": [{"mode": "cold", "repetition": 1}]},
        {},
        {"pg_container": "fixture-pg", "redis_container": "fixture-redis"},
    )
    assert result["timed_out"] is True
    assert result["timeout_limit_seconds"] == 360
    assert result["exit_code"] is None
    assert json.loads((directory / "process.json").read_text())["timed_out"] is True
    assert not (directory / "runs").exists()


def test_interrupted_evidence_write_preserves_last_complete_record(
    tmp_path, monkeypatch
):
    target = tmp_path / "run.json"
    previous = {"status": "Extracting", "http_requests": [{"attempt": 1}]}
    runner.write_json(target, previous)
    original_write = Path.write_text

    def interrupted_write(path, data, *args, **kwargs):
        original_write(path, data[:8], *args, **kwargs)
        raise OSError("Injected interruption during evidence write")

    monkeypatch.setattr(Path, "write_text", interrupted_write)
    with pytest.raises(OSError, match="Injected interruption"):
        runner.write_json(
            target,
            {
                "status": "Pending Review",
                "http_requests": [{"attempt": 1}, {"attempt": 2}],
            },
        )
    assert json.loads(target.read_text(encoding="utf-8")) == previous
