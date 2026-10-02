"""Aggregate actual passive-browser UI receipts; no browser/model/DB access."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path):
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def utc(value):
    return datetime.fromisoformat(value)


def numeric(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def resource_window(folder, end):
    pid = end.get("service_pid")
    start = utc(end["started_at"])
    stop = utc(end["ended_at"])
    raw = read_jsonl(folder / "ui-resources.jsonl")
    rows = []
    for row in raw:
        if (
            row.get("pid") != pid
            or not numeric(row.get("perf_counter"))
            or not numeric(row.get("sample_completed_perf"))
        ):
            continue
        sampled = utc(row["sampled_at"])
        finished = sampled + timedelta(
            seconds=row["sample_completed_perf"] - row["perf_counter"]
        )
        if start <= sampled <= finished <= stop:
            rows.append(row)
    names = [
        "app_working_set_bytes",
        "postgres_container_process_rss_bytes",
        "redis_container_process_rss_bytes",
        "simultaneous_observed_sum_bytes",
    ]
    values = {}
    for name in names:
        present = [
            r[name] for r in rows if numeric(r.get(name)) and not r.get("errors")
        ]
        values[name] = max(present) if present else None
    return {
        "status": (
            "partial_observation" if rows else "no_complete_sample_inside_action_window"
        ),
        "sample_count": len(rows),
        "correct_service_pid": pid,
        "planned_interval_seconds": 1.0,
        "resource_budget_acceptance": None,
        "reason": "Subsecond actions cannot be fully described by sparse one-second resource observations; missing samples are null.",
        "peaks_inside_complete_sample_cycles": values,
        "source": (
            str(folder / "ui-resources.jsonl")
            if (folder / "ui-resources.jsonl").exists()
            else None
        ),
        "scope": "Actual UI worker PID; all PostgreSQL processes in shared container; Redis named container. Container RSS can count shared pages repeatedly.",
    }


def actual_screenshot(folder, run_id):
    valid = []
    for record in read_jsonl(folder / "browser-screenshots.jsonl"):
        if record.get("run_id") != run_id:
            continue
        path = folder / "screenshots" / Path(record["path"]).name
        if not path.is_file():
            continue
        content = path.read_bytes()
        if not content.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):
            continue
        if record.get("bytes") != len(content):
            continue
        valid.append(
            {
                "path": str(path),
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
                "capture_source": record.get("capture_source"),
            }
        )
    return {
        "status": (
            "verified_received_file"
            if len(valid) == 1
            else "missing" if not valid else "multiple_captures"
        ),
        "captures": valid,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="UI group directories, including preserved setup/preflight directories.",
    )
    parser.add_argument(
        "--fixtures",
        type=Path,
        required=True,
        help="The actual 27 prepared instance manifest.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Fresh aggregation directory; historical output is never overwritten.",
    )
    args = parser.parse_args()
    root = args.input.resolve()
    if args.output.exists():
        raise ValueError("Fresh output directory required.")
    fixtures = read_json(args.fixtures)
    planned = {r["instance_key"]: r for r in fixtures["instances"]}
    if len(planned) != 27:
        raise ValueError(
            "Expected 27 distinct planned browser actions: 24 timed, 3 warmup."
        )
    exclusions = []
    attempts = []
    parsing_errors = []
    for folder in sorted(root.iterdir()):
        if not folder.is_dir():
            continue
        try:
            records = read_jsonl(folder / "browser-measurements.jsonl")
        except (ValueError, OSError) as exc:
            parsing_errors.append(
                {
                    "directory": str(folder),
                    "status": "unreadable_measurement_receipt",
                    "error_type": type(exc).__name__,
                }
            )
            continue
        starts = [r for r in records if r.get("event") == "start"]
        if not starts:
            exclusions.append(
                {
                    "directory": str(folder),
                    "reason": "setup_preflight_no_actual_browser_start",
                    "diagnostic_ref": (
                        str(folder / "setup-diagnostic.json")
                        if (folder / "setup-diagnostic.json").exists()
                        else None
                    ),
                    "has_service_receipt": (folder / "service.json").exists(),
                    "is_formal_measurement": False,
                }
            )
            continue
        for start in starts:
            run_id = start["run_id"]
            ends = [
                r
                for r in records
                if r.get("event") == "end"
                and r.get("run_id") == run_id
                and r.get("service_pid") == start.get("service_pid")
            ]
            base = planned.get(run_id)
            if base is None:
                parsing_errors.append(
                    {
                        "directory": str(folder),
                        "run_id": run_id,
                        "status": "unplanned_browser_action",
                    }
                )
                continue
            row = {
                "run_id": run_id,
                "workload": base["workload"],
                "case_id": base["case_id"],
                "temperature": base["temperature"],
                "repeat_index": base["repeat_index"],
                "warmup": base["warmup"],
                "action_kind": base["action_kind"],
                "directory": str(folder),
                "source_kind": "source_backed_replay_for_ui_timing",
                "attempted": True,
                "status": (
                    "missing_end"
                    if not ends
                    else (
                        "multiple_ends"
                        if len(ends) > 1
                        else ends[0].get("status", "unknown")
                    )
                ),
                "target_ms": 500,
                "elapsed_ms": None,
                "target_met": False,
                "start_event": start.get("start_event"),
                "end_event": None,
                "started_at": start.get("started_at"),
                "ended_at": None,
                "service_pid": start.get("service_pid"),
                "measurement_ref": str(folder / "browser-measurements.jsonl"),
                "screenshot": actual_screenshot(folder, run_id),
                "resource": None,
            }
            if len(ends) == 1:
                end = ends[0]
                duration = end.get("elapsed_ms")
                row.update(
                    {
                        "elapsed_ms": (
                            duration if numeric(duration) and duration >= 0 else None
                        ),
                        "ended_at": end.get("ended_at"),
                        "end_event": end.get("end_event"),
                        "reason": end.get("reason"),
                        "measurement_source": end.get("measurement_source"),
                        "actual_snapshot": end.get("actual_snapshot"),
                    }
                )
                if row["elapsed_ms"] is None:
                    row["status"] = "invalid_duration"
                row["target_met"] = (
                    row["status"] == "success"
                    and row["elapsed_ms"] is not None
                    and row["elapsed_ms"] < 500
                )
                try:
                    row["resource"] = resource_window(folder, end)
                except (ValueError, KeyError, OSError) as exc:
                    row["resource"] = {
                        "status": "resource_receipt_unusable",
                        "error_type": type(exc).__name__,
                        "sample_count": 0,
                        "peaks_inside_complete_sample_cycles": None,
                        "resource_budget_acceptance": None,
                    }
            attempts.append(row)
    multiplicities = {
        run_id: sum(r["run_id"] == run_id for r in attempts) for run_id in planned
    }
    missing = [run_id for run_id, count in multiplicities.items() if count == 0]
    duplicate = [run_id for run_id, count in multiplicities.items() if count > 1]
    timed = [r for r in attempts if not r["warmup"]]
    warming = [r for r in attempts if r["warmup"]]
    by_group = {}
    for workload in ["text", "figure", "cross"]:
        for temperature in ["cold", "warm"]:
            rows = [
                r
                for r in timed
                if r["workload"] == workload and r["temperature"] == temperature
            ]
            durations = sorted(
                r["elapsed_ms"]
                for r in rows
                if r["status"] == "success" and r["elapsed_ms"] is not None
            )
            n = len(durations)
            median = (
                durations[n // 2]
                if n % 2
                else (durations[n // 2 - 1] + durations[n // 2]) / 2 if n else None
            )
            by_group[f"{workload}-{temperature}"] = {
                "planned": 3 if temperature == "cold" else 5,
                "attempted": len(rows),
                "success": len(durations),
                "below_500_ms": sum(r["target_met"] for r in rows),
                "failed_or_incomplete": sum(r["status"] != "success" for r in rows),
                "min_ms": min(durations) if durations else None,
                "median_ms": median,
                "max_ms": max(durations) if durations else None,
            }
    summary = {
        "aggregated_at": datetime.now(UTC).isoformat(),
        "status": (
            "complete_protocol_attempts"
            if not missing and not duplicate and not parsing_errors
            else "incomplete_or_ambiguous"
        ),
        "planned_timed": 24,
        "planned_warmup": 3,
        "attempted_timed": len(timed),
        "attempted_warmup": len(warming),
        "successful_timed": sum(r["status"] == "success" for r in timed),
        "timed_below_500_ms": sum(r["target_met"] for r in timed),
        "target_acceptance": not missing
        and not duplicate
        and not parsing_errors
        and len(timed) == 24
        and all(r["target_met"] for r in timed),
        "business_render_acceptance": not missing
        and not duplicate
        and not parsing_errors
        and all(r["status"] == "success" for r in attempts),
        "resource_budget_acceptance": None,
        "missing_run_ids": missing,
        "duplicate_run_ids": duplicate,
        "parsing_errors": parsing_errors,
        "preflight_excluded_count": len(exclusions),
        "by_group": by_group,
        "reference_origin": "Actual first-round raw source replay, not new extraction or teacher quality assessment.",
        "timing_source": "Browser performance.now from actual native target selection to target fields/page/images loaded and queue idle/stable doubleRAF.",
        "tool_transport_in_duration": False,
        "screenshot_verified_actions": sum(
            r["screenshot"]["status"] == "verified_received_file" for r in attempts
        ),
    }
    args.output.mkdir(parents=True)
    for name, value in [
        ("summary.json", summary),
        ("attempts.json", attempts),
        ("preflight-exclusions.json", exclusions),
    ]:
        (args.output / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
    fields = [
        "run_id",
        "workload",
        "case_id",
        "temperature",
        "repeat_index",
        "warmup",
        "action_kind",
        "status",
        "target_ms",
        "elapsed_ms",
        "target_met",
        "started_at",
        "ended_at",
        "start_event",
        "end_event",
        "service_pid",
        "measurement_source",
        "measurement_ref",
    ]
    with (args.output / "timings.csv").open(
        "w", encoding="utf-8", newline=""
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(attempts)
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False), flush=True)


if __name__ == "__main__":
    main()
