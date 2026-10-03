"""Recompute T168 summaries from frozen raw runs and reviewed matches, offline."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
KINDS = (
    "answer_correctness",
    "condition_sufficiency",
    "option_ambiguity",
    "rubric_clarity",
)


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def ratio(n: int, d: int) -> float | None:
    return n / d if d else None


def csv_write(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def summarize(batch: Path) -> None:
    plan = read(batch / "frozen-plan.json")
    semantic = [read(p) for p in sorted((batch / "semantic/runs").glob("*.json"))]
    checks = read(batch / "semantic/semantic_checks.json")
    vision = read(batch / "vision/runs.json")
    review = read(batch / "vision/condition_review.json")
    imported = read(batch / "import/summary.json")
    if len(semantic) != 33 or len(checks) != 132 or len(vision) != 12:
        raise ValueError("The frozen three-round run set is incomplete")
    if len({(r["repeat_index"], r["case_id"]) for r in semantic}) != 33:
        raise ValueError("Duplicate semantic slots")
    if len({(r["round"], r["case_id"]) for r in vision}) != 12:
        raise ValueError("Duplicate image slots")
    if (
        hashlib.sha256((batch / "vision/runs.json").read_bytes()).hexdigest()
        != review["raw_runs_sha256"]
    ):
        raise ValueError("Image review refers to another raw run set")
    metrics: list[dict[str, Any]] = []

    def metric(
        stage: str, name: str, n: int, d: int, evidence: str, run_id: str = ""
    ) -> None:
        metrics.append(
            {
                "batch_id": plan["batch_id"],
                "run_id": run_id,
                "case_id": "",
                "stage": stage,
                "metric": name,
                "unit": "ratio",
                "numerator": n,
                "denominator": d,
                "value": ratio(n, d),
                "status": "observed" if d else "no_evaluable_data",
                "reason": "repeated fixed cases; not independent sample expansion",
                "evidence_ref": evidence,
            }
        )

    semantic_counts: dict[str, Any] = {}
    for kind in KINDS:
        rows = [r for r in checks if r["check_kind"] == kind]
        eligible = [
            r for r in rows if r["applicable"] and r["expected_problem"] is not None
        ]
        assessed = [r for r in eligible if r["predicted_problem"] is not None]
        counts = {k: 0 for k in ("TP", "FP", "TN", "FN")}
        for row in assessed:
            actual, expected = row["predicted_problem"], row["expected_problem"]
            key = (
                ("TP" if expected else "FP") if actual else ("FN" if expected else "TN")
            )
            counts[key] += 1
        tp, fp, tn, fn = (counts[k] for k in ("TP", "FP", "TN", "FN"))
        positive = sum(r["expected_problem"] is True for r in eligible)
        negative = len(eligible) - positive
        statuses = Counter(r["status"] for r in eligible)
        semantic_counts[kind] = {
            **counts,
            "planned_positive": positive,
            "planned_negative": negative,
            "known_applicable_labels": len(eligible),
            "assessed": len(assessed),
            "unknown_label_excluded": sum(
                r["applicable"] and r["expected_problem"] is None for r in rows
            ),
            "not_applicable_excluded": sum(not r["applicable"] for r in rows),
            "input_not_ready": statuses["input_not_ready"],
            "technical_failed": statuses["technical_error"],
            "abstained": statuses["abstained"],
            "coverage": ratio(len(assessed), len(eligible)),
            "recall_on_assessed": ratio(tp, tp + fn),
            "false_negative_rate_on_assessed": ratio(fn, tp + fn),
            "false_positive_rate_on_assessed": ratio(fp, fp + tn),
            "positive_detection_over_planned": ratio(tp, positive),
            "accuracy_on_assessed": ratio(tp + tn, len(assessed)),
        }
        for name, n, d in (
            ("coverage", len(assessed), len(eligible)),
            ("recall", tp, tp + fn),
            ("false_negative_rate", fn, tp + fn),
            ("false_positive_rate", fp, fp + tn),
            ("positive_detection_over_planned", tp, positive),
        ):
            metric(
                "semantic_automatic",
                kind + "." + name,
                n,
                d,
                "semantic/semantic_checks.json",
            )
    metric(
        "semantic_automatic",
        "coverage_total",
        sum(v["assessed"] for v in semantic_counts.values()),
        sum(v["known_applicable_labels"] for v in semantic_counts.values()),
        "semantic/semantic_checks.json",
    )

    vision_rounds: list[dict[str, Any]] = []
    reviewed = {r["run_id"]: r for r in review["entries"]}
    if set(reviewed) != {r["run_id"] for r in vision}:
        raise ValueError("Image review set differs from actual calls")
    for repeat in range(1, 4):
        clear = [
            r for r in vision if r["round"] == repeat and r["case_id"] != "IMG-UNCLEAR"
        ]
        matched = outputs = reference = 0
        for r in clear:
            rv = reviewed[r["run_id"]]
            indexes = [m["output_index"] for m in rv["matches"] if m["matched"]]
            refs = [m["reference_key"] for m in rv["matches"]]
            if len(indexes) != len(set(indexes)) or len(refs) != len(set(refs)):
                raise ValueError("Image conditions are not matched one-to-one")
            for index in indexes:
                if (
                    not r["output"]
                    or not 1 <= index <= len(r["output"]["conditions"])
                    or r["output"]["conditions"][index - 1]["image_index"] != 1
                ):
                    raise ValueError("Invalid output condition match")
            matched += len(indexes)
            outputs += len(r["output"]["conditions"]) if r["output"] else 0
            reference += rv["reference_conditions"]
        vision_rounds.append(
            {
                "round": repeat,
                "matched": matched,
                "assessable_outputs": outputs,
                "reference_conditions": reference,
                "precision": ratio(matched, outputs),
                "completeness": ratio(matched, reference),
            }
        )
        metric(
            "vision_automatic",
            "condition_precision",
            matched,
            outputs,
            "vision/condition_review.json",
            f"round-{repeat}",
        )
        metric(
            "vision_automatic",
            "condition_completeness",
            matched,
            reference,
            "vision/condition_review.json",
            f"round-{repeat}",
        )
    matched = sum(r["matched"] for r in vision_rounds)
    outputs = sum(r["assessable_outputs"] for r in vision_rounds)
    references = sum(r["reference_conditions"] for r in vision_rounds)
    unclear = [r for r in vision if r["case_id"] == "IMG-UNCLEAR"]
    handoffs = sum(
        reviewed[r["run_id"]]["manual_handoff_correct"] is True for r in unclear
    )
    metric(
        "vision_automatic",
        "condition_precision",
        matched,
        outputs,
        "vision/condition_review.json",
    )
    metric(
        "vision_automatic",
        "condition_completeness",
        matched,
        references,
        "vision/condition_review.json",
    )
    metric(
        "vision_manual_handoff",
        "unclear_handoff_correct",
        handoffs,
        len(unclear),
        "vision/condition_review.json",
    )
    for stage in imported["stages"]:
        for field in stage["fields"]:
            metric(
                "import_" + stage["stage"],
                field["metric"],
                field["numerator"],
                field["denominator"],
                "import/fields.csv",
            )
        value = stage["all_evaluable_fields_match"]
        metric(
            "import_" + stage["stage"],
            "all_evaluable_fields_match",
            value["numerator"],
            value["denominator"],
            "import/summary.json",
        )

    attempts = [e for r in semantic for e in r["trace_attempts"]] + [
        e for r in vision for e in r["provider_trace_events"]
    ]
    raw = [e for r in semantic for e in r.get("actual_raw_responses", [])] + [
        e for r in vision for e in r.get("actual_raw_responses", [])
    ]
    usage = {
        k: sum(e.get("usage", {}).get(k, 0) for e in raw)
        for k in ("prompt_tokens", "completion_tokens", "total_tokens")
    }
    failures = [
        r
        for r in checks
        if r["applicable"]
        and r["expected_problem"] is not None
        and (
            r["predicted_problem"] is None
            or r["predicted_problem"] != r["expected_problem"]
        )
    ]
    cases = []
    timings = []
    for family, rows in (("semantic", semantic), ("vision", vision)):
        for r in rows:
            state = r["status"] if family == "semantic" else r["outcome"]
            ref = (
                f"semantic/runs/{r['run_id']}.json"
                if family == "semantic"
                else f"vision/{r['run_id']}.json"
            )
            cases.append(
                {
                    "batch_id": plan["batch_id"],
                    "run_id": r["run_id"],
                    "case_id": r["case_id"],
                    "repeat_index": r.get("repeat_index", r.get("round")),
                    "stage": "automatic",
                    "family": family,
                    "status": state,
                    "evidence_ref": ref,
                }
            )
            timings.append(
                {
                    "batch_id": plan["batch_id"],
                    "run_id": r["run_id"],
                    "case_id": r["case_id"],
                    "stage": "automatic",
                    "workload": family,
                    "cold_warm": "quality_repeat",
                    "start_event": "application Provider boundary",
                    "end_event": "validated output or actual failure/input block",
                    "started_at_utc": r.get("started_at_utc", r.get("started_at")),
                    "completed_at_utc": r.get(
                        "completed_at_utc", r.get("completed_at")
                    ),
                    "elapsed_ms": r["elapsed_ms"],
                    "timing_source": "perf_counter",
                    "target": None,
                    "target_met": None,
                    "resource_complete": None,
                    "reason": "quality calls; no performance/resource acceptance claim",
                    "evidence_ref": ref,
                }
            )
    csv_write(batch / "metrics.csv", metrics)
    csv_write(batch / "cases.csv", cases)
    csv_write(batch / "timings.csv", timings)
    with (batch / "failures.jsonl").open("w", encoding="utf-8") as stream:
        for r in failures:
            stream.write(
                json.dumps(
                    {**r, "evidence_ref": "semantic/" + r["evidence_ref"]},
                    ensure_ascii=False,
                )
                + "\n"
            )
    summary = {
        "task": "T168",
        "recorded_at_utc": datetime.now(UTC).isoformat(),
        "reference_standard": "AI 辅助 + 开发者审查",
        "independent_teacher_count": 0,
        "annotation_version": plan["annotation_version"],
        "dataset_version": plan["dataset_version"],
        "semantic": semantic_counts,
        "semantic_run_statuses": dict(Counter(r["status"] for r in semantic)),
        "vision": {
            "rounds": vision_rounds,
            "matched": matched,
            "assessable_outputs": outputs,
            "reference_conditions": references,
            "precision": ratio(matched, outputs),
            "completeness": ratio(matched, references),
            "unclear_handoffs": handoffs,
            "unclear_planned": len(unclear),
            "unassessable_lowres_outputs": sum(
                len(r["output"]["conditions"]) for r in unclear if r["output"]
            ),
            "run_statuses": dict(Counter(r["outcome"] for r in vision)),
        },
        "new_provider_attempts": len(attempts),
        "new_raw_responses": len(raw),
        "request_models": sorted({e["model"] for e in attempts}),
        "response_models": sorted({e["model"] for e in raw if "model" in e}),
        "actual_usage": usage,
        "monetary_cost": None,
        "historical_import_requests_reused": imported["historical_model_requests"],
        "import_summary_ref": "import/summary.json",
        "quality_thresholds": None,
        "threshold_status": "pending_confirmation",
        "quality_acceptance": "not_claimed",
        "new_business_gate_acceptance": "not_run; prior technical receipts remain separate",
        "scope": "Real Provider outputs and same-source historical import reaggregation; frozen synthetic learning-project references",
    }
    thresholds_file = batch / "thresholds.json"
    if thresholds_file.exists():
        thresholds = read(thresholds_file)
        if (
            thresholds["status"] != "confirmed_by_user"
            or thresholds["confirmation"] is None
        ):
            raise ValueError("Threshold decision is not confirmed")
        assessments: list[dict[str, Any]] = []

        def assess(
            metric_name: str, actual: float | None, operator: str, target: float
        ) -> None:
            met = (
                None
                if actual is None
                else (actual >= target if operator == ">=" else actual <= target)
            )
            assessments.append(
                {
                    "metric": metric_name,
                    "actual": actual,
                    "operator": operator,
                    "target": target,
                    "met": met,
                    "status": (
                        "not_evaluable" if met is None else "met" if met else "not_met"
                    ),
                }
            )

        for threshold in thresholds["thresholds"]:
            family, name, stage = (
                threshold["family"],
                threshold["metric"],
                threshold["stage"],
            )
            operator, target = threshold["operator"], threshold["value"]
            if family == "import":
                value = next(v for v in imported["stages"] if v["stage"] == stage)
                if name == "each_evaluable_field_accuracy":
                    for field in value["fields"]:
                        assess(
                            "import." + stage + "." + field["field"],
                            field["value"],
                            operator,
                            target,
                        )
                else:
                    actual = value[
                        (
                            "identity_recall"
                            if name == "identity_recall"
                            else "all_evaluable_fields_match"
                        )
                    ]["value"]
                    assess("import." + stage + "." + name, actual, operator, target)
            elif family == "semantic":
                key = {
                    "each_check_recall_on_assessed": "recall_on_assessed",
                    "each_check_false_positive_rate_on_assessed": "false_positive_rate_on_assessed",
                    "each_check_coverage": "coverage",
                }[name]
                for kind, value in semantic_counts.items():
                    assess("semantic." + kind + "." + key, value[key], operator, target)
            elif family == "vision":
                actual = (
                    ratio(handoffs, len(unclear))
                    if stage == "manual_handoff"
                    else ratio(matched, outputs if "precision" in name else references)
                )
                assess("vision." + name, actual, operator, target)
            else:
                attempted = [
                    r for r in semantic if r["status"] != "input_not_ready"
                ] + vision
                failed = sum(
                    r.get("status", r.get("outcome")) == "technical_error"
                    for r in attempted
                )
                assess(
                    "execution.technical_failure_rate",
                    ratio(failed, len(attempted)),
                    operator,
                    target,
                )
        summary["quality_thresholds"] = thresholds["thresholds"]
        summary["threshold_status"] = "confirmed_by_user"
        summary["threshold_decision_ref"] = "thresholds.json"
        summary["threshold_assessments"] = assessments
        summary["quality_acceptance"] = (
            "not_met"
            if any(v["met"] is False for v in assessments)
            else "met_on_evaluable_metrics"
        )
        summary["task_completion_scope"] = (
            "Quality baseline measured and user-confirmed thresholds registered; task completion does not imply quality acceptance"
        )
    write(batch / "summary.json", summary)
    manifest = {
        "protocol_version": plan["protocol_version"],
        "batch_id": plan["batch_id"],
        "dataset_version": plan["dataset_version"],
        "annotation_version": plan["annotation_version"],
        "reference_standard": summary["reference_standard"],
        "quality_thresholds": summary["quality_thresholds"],
        "threshold_status": summary["threshold_status"],
        "source_plan_ref": "frozen-plan.json",
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "evidence_files": [
            {
                "path": p.relative_to(batch).as_posix(),
                "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                "bytes": p.stat().st_size,
            }
            for p in sorted(batch.rglob("*"))
            if p.is_file() and p.name != "manifest.json"
        ],
    }
    write(batch / "manifest.json", manifest)
    print(
        json.dumps(
            {
                "status": "recomputed",
                "semantic_assessed": sum(
                    v["assessed"] for v in semantic_counts.values()
                ),
                "vision_matched": matched,
                "provider_attempts": len(attempts),
                "tokens": usage,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--batch", type=Path, default=ROOT / "benchmark/results/v2/t168-20261003"
    )
    summarize(parser.parse_args().batch.resolve())
