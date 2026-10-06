"""T168 field baseline with user-confirmed thresholds; original comparison retained."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from benchmark.t168.import_baseline import (
    CASES,
    REFERENCE,
    compare,
    read,
    sha,
    write_csv,
    write_json,
)

ROOT = Path(__file__).resolve().parents[2]


def validate_slots(runs: list[dict]) -> None:
    expected = {(case, repeat) for case in CASES for repeat in (1, 2, 3)}
    if (
        len(runs) != len(expected)
        or {(r["case_id"], r["repeat_index"]) for r in runs} != expected
    ):
        raise ValueError(
            "Require every original case exactly once in each of three rounds"
        )


def assess(rows: list[dict], identities: list[dict], policy: dict) -> list[dict]:
    results = []
    for field, target in policy["targets"].items():
        identity = field == "identity"
        members = [r for r in rows if r["field"] == field and r["evaluable"]]
        numerator = (
            sum(i["matched_identities"] for i in identities)
            if identity
            else sum(r["correct"] is True for r in members)
        )
        denominator = (
            sum(i["reference_question_count"] for i in identities)
            if identity
            else len(members)
        )
        value = numerator / denominator if denominator else None
        unique = not identity or not any(
            i["duplicate_actual_identities"] or i["unmatched_actual_questions"]
            for i in identities
        )
        results.append(
            {
                "field": field,
                "numerator": numerator,
                "denominator": denominator,
                "value": value,
                "target": target,
                "met": value is not None and value >= target and unique,
            }
        )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--thresholds", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    batch, output = args.batch.resolve(), args.output.resolve()
    if not batch.is_relative_to(
        ROOT / "benchmark/results"
    ) or not output.is_relative_to(ROOT / "benchmark/results"):
        raise ValueError("Use repository result directories")
    policy = read(args.thresholds)
    if policy["status"] != "confirmed_by_user":
        raise ValueError("Thresholds require recorded user confirmation")
    reference = read(ROOT / REFERENCE)
    labels = {e["case_id"]: e["labels"]["questions"] for e in reference["entries"]}
    paths = sorted(batch.glob("*/runs/*/result.json"))
    runs = [read(path) for path in paths]
    validate_slots(runs)
    rows, identities = [], []
    for path, run in zip(paths, runs, strict=True):
        if (
            sha(ROOT / "benchmark/corpus/v2-draft-20261001" / run["source_ref"])
            != run["source_sha256"]
        ):
            raise ValueError("Actual run source differs from the frozen input")
        fields, identity = compare(
            labels[run["case_id"]],
            run["automatic_snapshot"],
            run | {"evidence_ref": str(path.relative_to(output.parent))},
            "automatic",
        )
        rows.extend(fields)
        identities.append(identity)
    assessments = assess(rows, identities, policy)
    summary = {
        "task": "T168 field acceptance",
        "reference": "AI-assisted + developer review",
        "independent_teachers": 0,
        "reference_sha256": sha(ROOT / REFERENCE),
        "thresholds_sha256": sha(args.thresholds),
        "planned_imports": 15,
        "attempted_imports": len(runs),
        "completed_pending_review": sum(
            r["automatic_status"] == "Pending Review" for r in runs
        ),
        "assessments": assessments,
        "field_thresholds_passed": all(r["met"] for r in assessments),
        "all_fields_diagnostic": [
            sum(i["all_evaluable_fields_match_questions"] for i in identities),
            sum(i["reference_question_count"] for i in identities),
        ],
        "excluded_from_thresholds": policy["excluded"],
        "known_limitations_not_blocking_t168": policy[
            "known_limitations_not_blocking_t168"
        ],
        "full_regression_still_required": True,
        "field_unknown_counts": {
            field: sum(not r["evaluable"] for r in rows if r["field"] == field)
            for field in sorted({r["field"] for r in rows})
        },
        "comparison": policy["comparison"],
    }
    output.mkdir(parents=True, exist_ok=False)
    write_csv(output / "fields.csv", rows)
    write_csv(output / "identities.csv", identities)
    write_json(output / "summary.json", summary)
    write_json(
        output / "mismatches.json",
        [r for r in rows if r["evaluable"] and r["correct"] is False],
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
