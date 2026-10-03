"""Validate frozen T146 reference inputs; does not execute business or cloud models."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

PACKAGE = Path("benchmark/corpus/t146-ai-authorized-20261003")
KINDS = (
    "answer_correctness",
    "condition_sufficiency",
    "option_ambiguity",
    "rubric_clarity",
)


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def dereference(package: Path, ref: str) -> Any:
    name, pointer = ref.split("#", 1)
    value = read(package / name)
    for part in pointer.strip("/").split("/"):
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def validate(root: Path) -> dict[str, Any]:
    package = root / PACKAGE
    manifest = read(package / "manifest.json")
    index = read(package / "annotations.json")
    ids = manifest["case_ids"]
    require(len(ids) == 61 and len(set(ids)) == 61, "61 unique frozen cases required")
    old = root / "benchmark/corpus/v2-draft-20261001"
    originals = read(old / "cases.json")
    require({e["case_id"] for e in originals} <= set(ids), "original case removed")
    require([e["case_id"] for e in index["entries"]] == ids, "annotation order differs")
    require(
        manifest["independent_teacher_labeled_cases"] == 0,
        "teacher provenance falsified",
    )
    require(
        not index["is_independent_teacher_truth"], "AI reference is not teacher truth"
    )
    for record in manifest["frozen_files"]:
        path = root / record["path"]
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        require(actual == record["sha256"], f"input changed: {path}")
        require(path.stat().st_size == record["bytes"], f"byte count changed: {path}")
    for entry in index["entries"]:
        data = dereference(package, entry["annotation_ref"])
        require(data["case_id"] == entry["case_id"], "annotation pointer mismatch")
    with (package / "cases.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    require([row["case_id"] for row in rows] == ids, "case CSV differs")

    semantic = read(package / "semantic_annotations.json")["entries"]
    eligible: dict[str, Counter[str]] = {kind: Counter() for kind in KINDS}
    expected_csv = {}
    for case in semantic:
        require(
            not case["human_reviewed"] and not case["is_independent_teacher_truth"],
            "AI provenance changed",
        )
        for kind in KINDS:
            value = case["problem_labels"][kind]
            check = case["checks"][kind]
            require(value is None or isinstance(value, bool), "invalid tri-state label")
            require(check["problem_present"] is value, "label and evidence differ")
            usable = check["applicable"] and value is not None
            if usable:
                eligible[kind]["problem" if value else "no_problem"] += 1
            expected_csv[(case["case_id"], kind)] = (value, check["applicable"], usable)
    for kind, counts in eligible.items():
        require(
            counts["problem"] > 0 and counts["no_problem"] > 0,
            f"missing evaluable class: {kind}",
        )
    with (package / "semantic_labels.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    require(len(rows) == 44, "44 semantic checks required")
    seen = set()
    for row in rows:
        key = (row["case_id"], row["check_kind"])
        require(key not in seen, "duplicate semantic CSV row")
        seen.add(key)
        value, applicable, usable = expected_csv[key]
        require(json.loads(row["problem_present_json"]) is value, "CSV label changed")
        require(
            json.loads(row["applicable_json"]) is applicable,
            "CSV applicability changed",
        )
        require(
            json.loads(row["eligible_json"]) is usable,
            "unknown or non-applicable counted",
        )

    imports = read(package / "import_annotations.json")["entries"]
    import_counts = {}
    for case in imports:
        questions = case["labels"]["questions"]
        import_counts[case["case_id"]] = len(questions)
        require(
            [q["order_index"] for q in questions] == list(range(1, len(questions) + 1)),
            "question order invalid",
        )
        for q in questions:
            require(
                q["source_page_numbers"] and q["source_regions"] is None,
                "unknown boundary invented",
            )
            require(not q["teacher_verified"], "teacher verification invented")
            require(Decimal(q["score"]) > 0, "invalid known score")
            for missing in q["known_missing"]:
                require(
                    q[missing] is None or q[missing] == [],
                    "missing source field invented",
                )
            if q["question_number"] == "1" and case["family"] == "import":
                require(
                    list(q["options"]) == ["C", "A", "B", "D"],
                    "original options reordered",
                )
    require(
        [
            import_counts[x]
            for x in ["IMP-TEXT", "IMP-SCAN", "IMP-IMAGE", "IMP-MIXED", "IMP-CROSS"]
        ]
        == [3, 3, 3, 6, 1],
        "import question coverage changed",
    )
    require(
        [
            import_counts[x]
            for x in ["PERF-IMPORT-1", "PERF-IMPORT-10", "PERF-IMPORT-50"]
        ]
        == [3, 20, 100],
        "workload size changed",
    )

    assembly = read(package / "assembly_annotations.json")
    pool = {q["question_id"]: q for q in read(old / "question_pool.json")["candidates"]}
    require(
        len(pool) == len(assembly["candidate_annotations"]) == 300,
        "candidate coverage changed",
    )
    for q in assembly["candidate_annotations"]:
        require(
            q["derived_answer"] == pool[q["question_id"]]["reference_answer"],
            "candidate answer differs",
        )
        require(
            q["actual_approval_status"] == "not_created",
            "fixture intended approval treated as actual",
        )
    for case in assembly["assembly_annotations"]:
        if "actual" not in case:
            continue
        actual = case["actual"]
        rows = actual["exam_questions_reference"]
        require(len({q["question_id"] for q in rows}) == len(rows), "duplicate witness")
        require(
            sum((Decimal(q["effective_score"]) for q in rows), Decimal(0))
            == Decimal(actual["total_score"]),
            "witness total differs",
        )
    for case in assembly["scoring_annotations"]:
        if "confirmed_points" in case:
            require(
                case["confirmed_points"] is None and case["confirmation"] is None,
                "teacher rounding confirmation invented",
            )
    stats = read(package / "statistics_annotations.json")["statistics_annotations"]
    for case in stats:
        for exam in case["exams"]:
            mean = exam["mean"]
            if mean["denominator"]:
                require(
                    Decimal(mean["value"]) * mean["denominator"]
                    == Decimal(mean["numerator"]),
                    "mean differs",
                )
            else:
                require(mean["value"] is None, "zero population reported as mean zero")
            if case["case_id"] == "STATS-MIXED":
                require(
                    exam["counts"]["participated"] is None,
                    "unknown participation inferred",
                )
            if case["case_id"] == "STATS-STARTED":
                require(
                    exam["counts"]["participated"] == 5,
                    "declared participation differs",
                )
    with (package / "statistics_reference.csv").open(
        encoding="utf-8", newline=""
    ) as stream:
        rows = list(csv.DictReader(stream))
    require(len(rows) == 156, "statistics CSV incomplete")
    return {
        "status": "passed",
        "validated_at_utc": datetime.now(UTC).isoformat(),
        "case_count": len(ids),
        "frozen_file_count": len(manifest["frozen_files"]),
        "semantic_case_count": len(semantic),
        "semantic_eligible_class_counts": eligible,
        "import_question_instances": sum(import_counts.values()),
        "candidate_count": len(pool),
        "statistics_csv_rows": len(rows),
        "independent_teacher_labeled_cases": 0,
        "model_calls": 0,
        "business_acceptance_performed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[2]
    )
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    result = validate(args.root.resolve())
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.receipt:
        args.receipt.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
