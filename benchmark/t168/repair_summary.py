"""Summarize fresh repair runs against unchanged T146 references and T168 targets."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from benchmark.t168.import_baseline import (
    CASES,
    FIELDS,
    aggregate,
    compare,
    equal,
    expected_value,
    read,
    sha,
    write_csv,
    write_json,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, required=True)
    args = parser.parse_args()
    batch = args.batch.resolve()
    if not batch.is_relative_to(ROOT / "benchmark/results"):
        raise ValueError("Use repository result files")
    targets_file = ROOT / "benchmark/results/v2/t168-20261003/thresholds.json"
    targets = read(targets_file)
    assert targets["status"] == "confirmed_by_user"
    labels = read(
        ROOT / "benchmark/corpus/t146-ai-authorized-20261003/import_annotations.json"
    )
    old = read(ROOT / "benchmark/corpus/t160-assisted-20261003/annotations.draft.json")
    current = {e["case_id"]: e["labels"]["questions"] for e in labels["entries"]}
    previous = {e["case_id"]: e["labels"]["questions"] for e in old["entries"]}
    assert not labels["provenance"]["is_independent_teacher_truth"]
    for case in CASES:
        assert len(current[case]) == len(previous[case])
        for a, b in zip(current[case], previous[case], strict=True):
            assert a["question_identity"] == b["question_identity"]
            assert a.get("unknown", []) == b.get("unknown", [])
            assert a.get("field_states", {}) == b.get("field_states", {})
            assert all(
                equal(field, expected_value(a, field), expected_value(b, field))
                for field in FIELDS
            )
    source = batch / "import-execution-v5"
    results = sorted(source.glob("*/runs/*/result.json"))
    runs = [(path, read(path)) for path in results]
    assert {(r["case_id"], r["repeat_index"]) for _, r in runs} == {
        (c, n) for c in CASES for n in (1, 2, 3)
    }
    assert len(runs) == 15
    rows, identities = [], []
    for path, run in runs:
        assert run["source_sha256"] == sha(
            ROOT / "benchmark/corpus/v2-draft-20261001" / run["source_ref"]
        )
        for stage, key in (
            ("automatic", "automatic_snapshot"),
            ("assisted_corrected", "corrected_snapshot"),
        ):
            # A failed import keeps empty facts and the fixed planned denominator.
            snapshot = run.get(key, run["automatic_snapshot"])
            fields, identity = compare(
                current[run["case_id"]],
                snapshot,
                run | {"evidence_ref": str(path.relative_to(batch))},
                stage,
            )
            rows.extend(fields)
            identities.append(identity)
    metrics = aggregate(rows)
    stages = {}
    for stage in ("automatic", "assisted_corrected"):
        members = [i for i in identities if i["stage"] == stage]
        stages[stage] = {
            "identity": [sum(i["matched_identities"] for i in members), 48],
            "all_fields": [
                sum(i["all_evaluable_fields_match_questions"] for i in members),
                48,
            ],
            "fields": {
                m["field"]: [m["numerator"], m["denominator"]]
                for m in metrics
                if m["stage"] == stage and m["layer"] == "all"
            },
            "unknown_fields_excluded": sum(
                i["unknown_fields_excluded"] for i in members
            ),
        }
    checks = read(batch / "semantic/semantic_checks.json")
    assert len(checks) == 132
    semantic = {}
    for kind in (
        "answer_correctness",
        "condition_sufficiency",
        "option_ambiguity",
        "rubric_clarity",
    ):
        eligible = [
            r
            for r in checks
            if r["check_kind"] == kind
            and r["applicable"]
            and r["expected_problem"] is not None
        ]
        assessed = [r for r in eligible if r["predicted_problem"] is not None]
        counts = Counter(
            (
                ("TP" if r["expected_problem"] else "FP")
                if r["predicted_problem"]
                else ("FN" if r["expected_problem"] else "TN")
            )
            for r in assessed
        )
        semantic[kind] = dict(counts) | {
            "recall": ratio(counts["TP"], counts["TP"] + counts["FN"]),
            "false_positive_rate": ratio(counts["FP"], counts["FP"] + counts["TN"]),
            "coverage": ratio(len(assessed), len(eligible)),
            "assessed": len(assessed),
            "planned": len(eligible),
            "input_blocked": sum(r["status"] == "input_not_ready" for r in eligible),
            "abstained": sum(r["status"] == "abstained" for r in eligible),
        }
    vision = read(batch / "vision-v2/runs.json")
    review = read(batch / "vision-v2/condition_review.json")
    assert (
        hashlib.sha256((batch / "vision-v2/runs.json").read_bytes()).hexdigest()
        == review["raw_runs_sha256"]
    )
    reviewed = {e["run_id"]: e for e in review["entries"]}
    assert set(reviewed) == {r["run_id"] for r in vision} and len(vision) == 12
    matched = outputs = references = handoffs = 0
    for r in vision:
        entry = reviewed[r["run_id"]]
        if r["case_id"] == "IMG-UNCLEAR":
            handoffs += entry["manual_handoff_correct"] is True
            continue
        matches = [m for m in entry["matches"] if m["matched"]]
        indexes = [m["output_index"] for m in matches]
        assert len(indexes) == len(set(indexes))
        assert all(1 <= i <= len(r["output"]["conditions"]) for i in indexes)
        matched += len(matches)
        outputs += len(r["output"]["conditions"]) if r["output"] else 0
        references += entry["reference_conditions"]
    observations = []

    def assess(name, actual, target, operator):
        met = (
            None
            if actual is None
            else actual >= target if operator == ">=" else actual <= target
        )
        observations.append(
            {
                "metric": name,
                "actual": actual,
                "target": target,
                "operator": operator,
                "met": met,
            }
        )

    for t in targets["thresholds"]:
        family, stage, name = t["family"], t["stage"], t["metric"]
        if family == "import":
            value = stages.get(stage)
            if value is None:
                assess("import." + stage + "." + name, None, t["value"], t["operator"])
            elif name == "each_evaluable_field_accuracy":
                for field, pair in value["fields"].items():
                    assess(
                        "import." + stage + "." + field,
                        ratio(*pair),
                        t["value"],
                        t["operator"],
                    )
            else:
                pair = value["identity" if name == "identity_recall" else "all_fields"]
                assess(
                    "import." + stage + "." + name,
                    ratio(*pair),
                    t["value"],
                    t["operator"],
                )
        elif family == "semantic":
            key = {
                "each_check_recall_on_assessed": "recall",
                "each_check_false_positive_rate_on_assessed": "false_positive_rate",
                "each_check_coverage": "coverage",
            }[name]
            for kind, value in semantic.items():
                assess(
                    "semantic." + kind + "." + key,
                    value[key],
                    t["value"],
                    t["operator"],
                )
        elif family == "vision":
            actual = (
                ratio(handoffs, 3)
                if stage == "manual_handoff"
                else ratio(matched, outputs if "precision" in name else references)
            )
            assess("vision." + name, actual, t["value"], t["operator"])
        else:
            technical = sum(
                r["status"] == "technical_error"
                for r in read_runs(batch / "semantic/runs")
            ) + sum(r["outcome"] != "completed" for r in vision)
            assess(family + "." + name, ratio(technical, 42), t["value"], t["operator"])
    summary = {
        "task": "T168 scoped repair",
        "reference": "AI-assisted + developer review",
        "independent_teachers": 0,
        "reference_sha256": sha(
            ROOT
            / "benchmark/corpus/t146-ai-authorized-20261003/import_annotations.json"
        ),
        "thresholds_ref": str(targets_file.relative_to(ROOT)),
        "thresholds_sha256": sha(targets_file),
        "import": stages,
        "import_pending_review": sum(
            r["automatic_status"] == "Pending Review" for _, r in runs
        ),
        "semantic": semantic,
        "vision": {
            "matched": matched,
            "outputs": outputs,
            "references": references,
            "precision": ratio(matched, outputs),
            "completeness": ratio(matched, references),
            "unclear_handoffs": [handoffs, 3],
        },
        "assessments": observations,
        "quality_gate_passed": all(a["met"] is True for a in observations),
        "limitations": [
            "Automatic assets remain unknown null; no automatic crops implemented.",
            "After-image-assistance not rerun; old crops are not reused for new page IDs.",
            "SEM-ADAPT-GOOD lacks a student-visible formula; original negative label preserved as disputed.",
            "NO-BASIS remains input-blocked, never counted as a correct negative.",
            "Strict text comparison unchanged; no internal whitespace normalization.",
            "Startup performance gate remains failed independently.",
        ],
    }
    write_json(batch / "repair-summary.json", summary)
    write_csv(batch / "import-fields.csv", rows)
    write_csv(batch / "import-identities.csv", identities)
    write_csv(batch / "import-metrics.csv", metrics)
    write_json(
        batch / "semantic-disagreements.json",
        [
            r
            for r in checks
            if r["applicable"]
            and r["expected_problem"] is not None
            and r["predicted_problem"] != r["expected_problem"]
        ],
    )
    print(
        json.dumps(
            {
                "quality_gate_passed": summary["quality_gate_passed"],
                "import_pending_review": summary["import_pending_review"],
                "vision_completeness": summary["vision"]["completeness"],
            }
        )
    )


def ratio(n, d):
    return n / d if d else None


def read_runs(directory):
    return [read(p) for p in directory.glob("*.json")]


if __name__ == "__main__":
    main()
