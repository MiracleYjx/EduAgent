"""Frozen T142 application-cold/warm assembly protocol, real browser and services."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from sqlalchemy import func, select

from backend.app.core.database import get_session_factory
from backend.app.models import Exam, ExamQuestion, Question
from backend.app.schemas.exam_assembly import AssemblyRequest
from backend.app.services.exam_assembly_service import ExamAssemblyService
from backend.app.services.exam_service import ExamService
from benchmark.t160.resource_sampler import (
    ResourceSampler,
    container_rss,
    evaluate_v2_memory_budget,
    verified_worker_chain,
    windows_application_working_set,
)
from benchmark.t179.business_acceptance import check_request, facts
from benchmark.t179.fixtures import CORPUS, require_isolation


def dump(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class AssemblyResourceSampler(ResourceSampler):
    """Observe independent component scopes concurrently, at the fixed 1s interval."""

    def start(self):
        self._action_origin = time.perf_counter()
        return super().start()

    def _loop(self):
        next_tick = self._action_origin + self.interval_seconds
        while not self._stop.wait(max(0.0, next_tick - time.perf_counter())):
            self.sample_now()
            next_tick += self.interval_seconds
            if next_tick < time.perf_counter():
                next_tick = time.perf_counter() + self.interval_seconds

    def sample_now(self):
        with self._guard:
            if self._stream is None:
                raise RuntimeError("sampler is not running")
            start = time.perf_counter()
            with ThreadPoolExecutor(max_workers=3) as pool:
                app_future = pool.submit(windows_application_working_set, self.pid)
                pg_future = pool.submit(container_rss, self.pg_container, "postgres")
                redis_future = pool.submit(
                    container_rss, self.redis_container, "redis-server"
                )
                app, app_rows, app_error = app_future.result()
                pg, pg_rows, pg_error = pg_future.result()
                redis, redis_rows, redis_error = redis_future.result()
            row = {
                "sampled_at": datetime.now(UTC).isoformat(),
                "perf_counter": start,
                "sample_completed_perf": time.perf_counter(),
                "pid": self.pid,
                "application_root_pid": self.pid,
                "app_working_set_bytes": app,
                "application_processes": app_rows,
                "postgres_container_process_rss_bytes": pg,
                "postgres_processes": pg_rows,
                "redis_container_process_rss_bytes": redis,
                "redis_processes": redis_rows,
                "simultaneous_observed_sum_bytes": (
                    app + pg + redis
                    if all(v is not None for v in (app, pg, redis))
                    else None
                ),
                "errors": {
                    k: v
                    for k, v in [
                        ("app", app_error),
                        ("postgres", pg_error),
                        ("redis", redis_error),
                    ]
                    if v is not None
                },
                "observation_method": "three independent probes in parallel; common cycle start/completion, actual worker subtree; no browser/controller memory in app scope",
            }
            self._rows.append(row)
            self._stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._stream.flush()
            return row


def action_resources(sampler, browser, start_perf):
    """Use only complete cycles inside the browser action; short actions need no fake 1s span."""
    if start_perf is None or browser.get("elapsed_ms") is None:
        return {"complete_at_requested_sampling_resolution": False, "peaks": {}}
    end_perf = start_perf + browser["elapsed_ms"] / 1000
    result = sampler.summary(start_perf=start_perf, end_perf=end_perf)
    # A 1-second schedule can legitimately contain one observation in a subsecond
    # action. Completeness means no missing component and no unobserved interval
    # longer than the requested period (plus recorded probe duration).
    rows = []
    for line in sampler.output_path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue  # Concurrent final line can only be outside the closed window.
        if (
            row["perf_counter"] >= start_perf
            and row["sample_completed_perf"] <= end_perf
        ):
            rows.append(row)
    probe_duration = max(
        (r["sample_completed_perf"] - r["perf_counter"] for r in rows), default=0
    )
    tolerance = sampler.interval_seconds + 0.05
    complete = (
        bool(rows)
        and probe_duration <= sampler.interval_seconds
        and not any(r["errors"] for r in rows)
    )
    complete = complete and rows[0]["perf_counter"] - start_perf <= 0.05
    complete = complete and end_perf - rows[-1]["sample_completed_perf"] <= tolerance
    complete = complete and (result.get("max_observed_gap_seconds") or 0) <= tolerance
    result["complete_at_requested_sampling_resolution"] = complete
    result["coverage_rule"] = (
        "complete simultaneous component cycles inside actual action; first cycle starts within50ms; last boundary and cycle gaps <=1s +50ms scheduling tolerance; probes <=1s; one cycle allowed when action is shorter than schedule; no continuous-peak guarantee"
    )
    result["probe_duration_max_seconds"] = probe_duration
    result["sample_rows"] = rows
    result["action_boundary_perf"] = {"start": start_perf, "end": end_perf}
    return result


def database_state(course_id):
    with get_session_factory()() as session:
        return {
            "course_exams": session.scalar(
                select(func.count())
                .select_from(Exam)
                .where(Exam.course_id == course_id)
            ),
            "course_links": session.scalar(
                select(func.count())
                .select_from(ExamQuestion)
                .join(Exam)
                .where(Exam.course_id == course_id)
            ),
            "course_questions": session.scalar(
                select(func.count())
                .select_from(Question)
                .where(Question.course_id == course_id)
            ),
        }


def remove_owned_draft(exam_id, teacher_id):
    # Disposal is outside timing, after evidence capture, in the guarded isolated DB.
    require_isolation()
    with get_session_factory()() as session:
        service = ExamAssemblyService(session)
        exam = service._load_exam(exam_id, teacher_id)
        service.require_editable(exam)
        if not exam.title.startswith("T179 ASM-"):
            raise RuntimeError("Benchmark draft identity mismatch")
        session.delete(exam)
        session.commit()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--fixtures", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--isolation", type=Path, required=True)
    p.add_argument("--node", required=True)
    p.add_argument("--playwright", required=True)
    p.add_argument("--port", type=int, default=18762)
    p.add_argument("--pilot", action="store_true")
    args = p.parse_args()
    require_isolation()
    spec = json.loads(args.fixtures.read_text(encoding="utf-8"))
    isol = json.loads(args.isolation.read_text(encoding="utf-8"))
    if args.output.exists():
        raise ValueError("Fresh batch directory required; never overwrite failures")
    args.output.mkdir(parents=True)
    pool = json.loads((CORPUS / "question_pool.json").read_text(encoding="utf-8"))[
        "candidates"
    ]
    qids = [q["question_id"] for q in pool]
    cases = [
        c
        for c in json.loads(
            (CORPUS / "assembly_cases.json").read_text(encoding="utf-8")
        )
        if c["case_id"]
        in [f"ASM-{n}-{s}" for n in [1, 25, 100] for s in ["SAT", "UNSAT"]]
    ]
    if args.pilot:
        cases = [c for c in cases if c["case_id"].endswith("-SAT")]
    plan = []
    for case in cases:
        kinds = (
            [("pilot", [0])]
            if args.pilot
            else [
                ("cold", [1]),
                ("cold", [2]),
                ("cold", [3]),
                ("warm", [0, 1, 2, 3, 4, 5]),
            ]
        )
        for kind, indices in kinds:
            plan.append(
                {
                    "case": case,
                    "kind": kind,
                    "indices": indices,
                    "worker_run": f"{case['case_id']}-{kind}-{indices[0]}",
                }
            )
    for group in plan:
        group["action_run_ids"] = {
            str(
                index
            ): f"{group['case']['case_id']}-{group['kind']}-{index}-{uuid4().hex[:8]}"
            for index in group["indices"]
        }
    manifest = {
        "started_at": datetime.now(UTC).isoformat(),
        "source_files_sha256": {
            str(path.relative_to(ROOT))
            .replace("\\", "/"): hashlib.sha256(path.read_bytes())
            .hexdigest()
            for directory in [ROOT / "backend", ROOT / "benchmark/t179"]
            for path in directory.rglob("*")
            if path.is_file() and path.suffix in {".py", ".cjs"}
        },
        "environment": json.loads(
            (args.isolation.parent / "environment.json").read_text(encoding="utf-8")
        ),
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "sample_hashes": (
            spec["input_hashes"]
            if "input_hashes" in spec
            else spec.get("source_hashes")
        ),
        "planned": plan,
        "timed_planned": 3 if args.pilot else 48,
        "total_actions_planned": 3 if args.pilot else 54,
        "candidate_count": 300,
        "initial_database_state": database_state(spec["course_id"]),
        "concurrency": 1,
        "cloud_calls": 0,
        "annotation": "AI assisted + developer review; independent teacher=0",
        "cold": "new actual application worker per action; ready DB/Redis reused; OS disk cache not cleared",
        "warm": "one same-size preheat plus five fresh drafts in same worker",
        "timing": "actual browser click to full preview or genuine unsatisfied diagnostic including rendered actual images; no generated questions",
    }
    dump(args.output / "manifest.json", manifest)
    results = []
    for group in plan:
        case = group["case"]
        directory = args.output / group["worker_run"]
        directory.mkdir()
        worker_receipt = directory / "worker.json"
        log = (directory / "worker.log").open("wb")
        proc = subprocess.Popen(
            [
                sys.executable,
                "-u",
                str(Path(__file__).with_name("performance_app.py")),
                "--fixtures",
                str(args.fixtures),
                "--output",
                str(worker_receipt),
                "--port",
                str(args.port),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        sampler = None
        try:
            deadline = time.monotonic() + 90
            url = f"http://127.0.0.1:{args.port}"
            while True:
                if proc.poll() is not None:
                    raise RuntimeError("Worker exited before readiness")
                try:
                    with urllib.request.urlopen(url + "/health", timeout=2) as response:
                        assert response.status == 200
                    with urllib.request.urlopen(
                        url + "/acceptance/worker", timeout=2
                    ) as response:
                        worker = json.load(response)
                    break
                except (OSError, AssertionError):
                    if time.monotonic() > deadline:
                        raise RuntimeError("Worker readiness timed out")
                    time.sleep(0.3)
            chain = verified_worker_chain(worker["pid"], proc.pid)
            dump(
                directory / "verified-worker.json",
                {"worker": worker, "launched_pid": proc.pid, "verified_chain": chain},
            )
            for index in group["indices"]:
                runid = group["action_run_ids"][str(index)]
                action = directory / str(index)
                action.mkdir()
                title = "T179 " + runid
                initial_state = database_state(spec["course_id"])
                if initial_state != manifest["initial_database_state"]:
                    raise RuntimeError("Initial database workload state changed")
                with get_session_factory()() as session:
                    service = ExamService(session)
                    exam = service.create_exam(
                        spec["course_id"],
                        title,
                        teacher_id=spec["teacher_id"],
                        question_ids=qids[:2],
                    )
                    before = service.preview_assembly(
                        exam.id, teacher_id=spec["teacher_id"]
                    )
                    exam_id = exam.id
                request = AssemblyRequest.model_validate(case["request"])
                node_spec = {
                    "run_id": runid,
                    "case_id": case["case_id"],
                    "url": url,
                    "title": title,
                    "sat": case["case_id"].endswith("-SAT"),
                    "count": request.question_count,
                    "counts": {
                        r.question_type: r.count for r in request.type_distribution
                    },
                    "total": str(request.total_score),
                    "label": request.knowledge_coverage[0].knowledge_point,
                    "output": str((action / "browser.json").resolve()),
                    "screenshot": str((action / "browser.png").resolve()),
                }
                dump(action / "input.json", node_spec)
                env = os.environ.copy()
                env["T179_PLAYWRIGHT"] = args.playwright
                node = subprocess.Popen(
                    [
                        args.node,
                        str(Path(__file__).with_name("browser_runner.cjs")),
                        str(action / "input.json"),
                    ],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )
                received_start = None
                ipc_delay = None
                with (action / "browser-events.jsonl").open(
                    "x", encoding="utf-8"
                ) as events:
                    for line in node.stdout:
                        events.write(line)
                        events.flush()
                        try:
                            message = json.loads(line)
                        except ValueError:
                            continue
                        if message.get("event") == "start":
                            received_start = time.perf_counter()
                            ipc_delay = (
                                datetime.now(UTC)
                                - datetime.fromisoformat(message["started_at"])
                            ).total_seconds()
                            received_start -= max(0, ipc_delay)
                            sampler = AssemblyResourceSampler(
                                pid=worker["pid"],
                                pg_container=isol["pg_container"],
                                redis_container=isol["redis_container"],
                                output_path=action / "resources.jsonl",
                            ).start()
                _, err = node.communicate(timeout=10)
                (action / "browser-stderr.txt").write_text(err, encoding="utf-8")
                browser = (
                    json.loads((action / "browser.json").read_text(encoding="utf-8"))
                    if (action / "browser.json").exists()
                    else {
                        "passed": False,
                        "error": "browser did not write result",
                        "elapsed_ms": None,
                    }
                )
                if sampler:
                    sampler.stop()
                resources = (
                    action_resources(sampler, browser, received_start)
                    if sampler
                    else {
                        "complete_at_requested_sampling_resolution": False,
                        "peaks": {},
                    }
                )
                if ipc_delay is None or not -0.005 <= ipc_delay <= 0.05:
                    resources["complete_at_requested_sampling_resolution"] = False
                    resources["alignment_error"] = (
                        "same-host browser start IPC delay outside -5ms..50ms bound"
                    )
                if "scope" in resources:
                    resources["scope"][
                        "sum"
                    ] = "independent app/PostgreSQL/Redis probes concurrently inside one complete cycle; observed sum, not unique physical or continuous peak"
                row = {
                    "run_id": runid,
                    "case_id": case["case_id"],
                    "kind": (
                        "warmup"
                        if group["kind"] == "warm" and index == 0
                        else group["kind"]
                    ),
                    "index": index,
                    "worker_pid": worker["pid"],
                    "exam_id": exam_id,
                    "initial_database_state": initial_state,
                    "browser": browser,
                    "resources": resources,
                    "memory_budget": evaluate_v2_memory_budget(resources),
                    "observed_start_ipc_delay_seconds": ipc_delay,
                    "controller_window": "browser monotonic duration mapped to same-host start UTC for resource selection; screenshots/cleanup excluded; elapsed always browser monotonic",
                    "node_exit": node.returncode,
                    "passed": False,
                }
                try:
                    with get_session_factory()() as session:
                        after = ExamService(session).preview_assembly(
                            exam_id, teacher_id=spec["teacher_id"]
                        )
                        if node_spec["sat"]:
                            check_request(after, request)
                        else:
                            assert (
                                facts(before) == facts(after)
                                and after.assembly_constraints.request == request
                                and any(not c.satisfied for c in after.conditions)
                            )
                        dump(action / "before.json", before.model_dump(mode="json"))
                        dump(action / "after.json", after.model_dump(mode="json"))
                        row["passed"] = browser["passed"] and node.returncode == 0
                except Exception as error:  # noqa: BLE001
                    row["business_error"] = {
                        "type": type(error).__name__,
                        "message": str(error),
                    }
                row["latency_target_pass"] = (
                    row["passed"]
                    and browser.get("elapsed_ms") is not None
                    and browser["elapsed_ms"] < 3000
                )
                remove_owned_draft(
                    UUID(exam_id),
                    UUID(spec["teacher_id"]),
                )
                row["post_cleanup_database_state"] = database_state(spec["course_id"])
                if (
                    row["post_cleanup_database_state"]
                    != manifest["initial_database_state"]
                ):
                    raise RuntimeError(
                        "Owned draft cleanup failed to restore workload state"
                    )
                dump(action / "result.json", row)
                results.append(row)
                dump(args.output / "runs.json", results)
                print(
                    json.dumps(
                        {
                            "run_id": runid,
                            "passed": row["passed"],
                            "ms": browser.get("elapsed_ms"),
                            "memory_complete": resources.get(
                                "complete_at_requested_sampling_resolution"
                            ),
                        }
                    ),
                    flush=True,
                )
                sampler = None
        finally:
            if sampler:
                sampler.stop()
            # Exact controller-owned launched worker only, not shared services or browsers.
            if proc.poll() is None:
                cleanup = subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                dump(
                    directory / "worker-cleanup.json",
                    {
                        "launched_pid": proc.pid,
                        "exit_code": cleanup.returncode,
                        "scope": "exact live controller-owned subtree only",
                    },
                )
            proc.wait(timeout=15)
            log.close()
    timed = [r for r in results if r["kind"] != "warmup"]
    summary = {
        "planned": manifest["timed_planned"],
        "attempted": len(timed),
        "business_passed": sum(r["passed"] for r in timed),
        "latency_passed": sum(r["latency_target_pass"] for r in timed),
        "resource_complete": sum(
            r["memory_budget"]["resource_complete"] for r in timed
        ),
        "memory_passed": sum(r["memory_budget"]["budget_pass"] is True for r in timed),
        "completed_at": datetime.now(UTC).isoformat(),
    }
    summary["passed"] = all(
        summary[k] == summary["planned"]
        for k in [
            "attempted",
            "business_passed",
            "latency_passed",
            "resource_complete",
            "memory_passed",
        ]
    )
    dump(args.output / "summary.json", summary)
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
