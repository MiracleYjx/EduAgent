"""Recompute T168 import baseline from frozen real T160 runs, without cloud or DB."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

CASES = ("IMP-TEXT", "IMP-SCAN", "IMP-IMAGE", "IMP-MIXED", "IMP-CROSS")
FIELDS = (
    "question_number",
    "order_index",
    "source_page_numbers",
    "question_type",
    "content",
    "options",
    "reference_answer",
    "analysis",
    "score",
    "scoring_rubric",
    "knowledge_points",
    "source_regions",
    "assets",
)
SOURCE_PATHS = (
    "backend/app/ai/paper_extraction",
    "backend/app/ai/ingestion/paper_pipeline.py",
    "backend/app/ai/ingestion/ocr",
    "backend/app/services/paper_import_service.py",
    "backend/app/services/question_correction_service.py",
    "backend/app/schemas/paper_import.py",
    "backend/app/models/extracted_question.py",
)
REFERENCE = Path("benchmark/corpus/t146-ai-authorized-20261003/import_annotations.json")
OLD_REFERENCE = Path("benchmark/corpus/t160-assisted-20261003/annotations.draft.json")
HISTORY = Path("benchmark/results/v2/t160-retest-20261003/quality")
OUTPUT = Path("benchmark/results/v2/t168-20261003/import")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalized(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if isinstance(value, list):
        return [normalized(item) for item in value]
    if isinstance(value, dict):
        return [(normalized(key), normalized(item)) for key, item in value.items()]
    return value


def equal(field: str, expected: Any, actual: Any) -> bool:
    if field == "score" and expected is not None and actual is not None:
        try:
            return Decimal(str(expected)) == Decimal(str(actual))
        except (ValueError, ArithmeticError):
            return False
    if field == "knowledge_points" and expected is not None and actual is not None:
        return {normalized(item) for item in expected} == {
            normalized(item) for item in actual
        }
    return bool(normalized(expected) == normalized(actual))


def expected_value(label: dict[str, Any], field: str) -> Any:
    if field == "assets":
        return [
            {
                "asset_type": item["kind"],
                "source_page_number": item["source_page_number"],
                "order_index": item["order_index"],
            }
            for item in label["assets"]
        ]
    # QuestionType is a StrEnum whose supported name and value are equal.
    return label[field]


def metric(name: str, numerator: int, denominator: int, **extra: Any) -> dict[str, Any]:
    return {
        "metric": name,
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
        **extra,
    }


def compare(
    labels: list[dict[str, Any]],
    snapshot: dict[str, Any],
    run: dict[str, Any],
    stage: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    pages = {item["id"]: item for item in snapshot["pages"]}
    page_by_number = {item["page_number"]: item for item in snapshot["pages"]}
    grouped: dict[tuple[Any, tuple[Any, ...]], list[dict[str, Any]]] = defaultdict(list)
    for original in snapshot["questions"]:
        question = original.copy()
        question["source_page_numbers"] = [
            pages.get(page_id, {}).get("page_number")
            for page_id in question["source_page_ids"]
        ]
        question["assets"] = (
            None
            if question["assets"] is None
            else [
                {
                    "asset_type": asset["asset_type"],
                    "source_page_number": pages.get(asset["source_page_id"], {}).get(
                        "page_number"
                    ),
                    "order_index": index,
                }
                for index, asset in enumerate(question["assets"], 1)
            ]
        )
        grouped[
            (
                normalized(question["question_number"]),
                tuple(question["source_page_numbers"]),
            )
        ].append(question)
    rows: list[dict[str, Any]] = []
    matches, complete = 0, 0
    matched_ids: set[str] = set()
    for label in labels:
        candidates = grouped.get(
            (normalized(label["question_number"]), tuple(label["source_page_numbers"])),
            [],
        )
        matched = candidates[0] if len(candidates) == 1 else None
        reason = (
            "matched"
            if matched
            else ("duplicate_identity" if candidates else "missing_identity")
        )
        matches += matched is not None
        if matched:
            matched_ids.add(matched["id"])
        source_pages = [
            page_by_number.get(number) for number in label["source_page_numbers"]
        ]
        modality = (
            "unknown"
            if any(page is None for page in source_pages)
            else "ocr_required"
            if any(
                page is not None and page["ocr_text"] is not None
                for page in source_pages
            )
            else "native_text"
        )
        known_results = []
        for field in FIELDS:
            unknown = field in label.get("unknown", []) or (
                label.get("field_states", {}).get(field) == "unknown"
            )
            target = expected_value(label, field)
            actual = matched.get(field) if matched else None
            correct = (
                None if unknown else bool(matched and equal(field, target, actual))
            )
            if not unknown:
                known_results.append(correct)
            rows.append(
                {
                    "run_id": run["run_id"],
                    "case_id": run["case_id"],
                    "repeat_index": run["repeat_index"],
                    "stage": stage,
                    "reference_question_identity": label["question_identity"],
                    "extracted_question_id": matched["id"] if matched else None,
                    "modality": modality,
                    "field": field,
                    "reference_state": label.get("field_states", {}).get(
                        field, "source_provided"
                    ),
                    "evaluable": not unknown,
                    "correct": correct,
                    "expected": target,
                    "actual": actual,
                    "reason": "reference_unknown" if unknown else reason,
                    "evidence_ref": run["evidence_ref"],
                }
            )
        complete += bool(known_results and all(known_results))
    identities = {
        "run_id": run["run_id"],
        "case_id": run["case_id"],
        "repeat_index": run["repeat_index"],
        "stage": stage,
        "reference_question_count": len(labels),
        "actual_question_count": len(snapshot["questions"]),
        "matched_identities": matches,
        "missing_identities": len(labels) - matches,
        "duplicate_actual_identities": sum(
            max(0, len(items) - 1) for items in grouped.values()
        ),
        "unmatched_actual_questions": len(snapshot["questions"]) - len(matched_ids),
        "all_evaluable_fields_match_questions": complete,
        "unknown_fields_excluded": sum(not row["evaluable"] for row in rows),
        "evidence_ref": run["evidence_ref"],
    }
    return rows, identities


def aggregate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["stage"], "all", "all", row["field"])].append(row)
        groups[(row["stage"], "modality", row["modality"], row["field"])].append(row)
        groups[(row["stage"], "case", row["case_id"], row["field"])].append(row)
        groups[
            (row["stage"], "reference_state", row["reference_state"], row["field"])
        ].append(row)
    results = []
    for (stage, layer, stratum, field), members in groups.items():
        evaluable = [row for row in members if row["evaluable"]]
        results.append(
            metric(
                "strict_" + field + "_reference_match",
                sum(bool(row["correct"]) for row in evaluable),
                len(evaluable),
                stage=stage,
                layer=layer,
                stratum=stratum,
                field=field,
                planned_fields=len(members),
                unknown_excluded=len(members) - len(evaluable),
            )
        )
    return results


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    require(bool(rows), "CSV has no observations")
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(value, ensure_ascii=False)
                    if isinstance(value, (dict, list))
                    else value
                    for key, value in row.items()
                }
            )


def run(root: Path) -> dict[str, Any]:
    source = root / HISTORY
    current = read(root / REFERENCE)
    historical = read(root / OLD_REFERENCE)
    manifest = read(source / "manifest.json")
    require(
        sha(root / OLD_REFERENCE) == manifest["annotation_sha256"],
        "historical reference changed",
    )
    require(
        not historical["is_independent_teacher_truth"], "historical provenance changed"
    )
    require(
        not current["provenance"]["is_independent_teacher_truth"],
        "AI labels misrepresented",
    )
    current_cases = {entry["case_id"]: entry for entry in current["entries"]}
    old_cases = {entry["case_id"]: entry for entry in historical["entries"]}
    require(set(old_cases) == set(CASES), "historical normal-case plan changed")
    inputs, equivalence = [], []
    for case in CASES:
        labels = current_cases[case]["labels"]["questions"]
        old_labels = old_cases[case]["labels"]["questions"]
        require(len(labels) == len(old_labels), f"reference count differs: {case}")
        for label, old in zip(labels, old_labels, strict=True):
            require(
                label["question_identity"] == old["question_identity"],
                "reference identity differs",
            )
            require(
                label.get("unknown", []) == old.get("unknown", []),
                "unknown policy differs",
            )
            require(
                label.get("field_states", {}) == old.get("field_states", {}),
                "field policy differs",
            )
            for field in FIELDS:
                require(
                    equal(
                        field, expected_value(label, field), expected_value(old, field)
                    ),
                    f"metric reference differs: {case}/{label['question_number']}/{field}",
                )
        source_ref = old_cases[case]["source_refs"][0]
        source_hash = sha(root / "benchmark/corpus/v2-draft-20261001" / source_ref)
        require(
            all(label["source_sha256"] == source_hash for label in labels),
            "input differs",
        )
        inputs.append(
            {"case_id": case, "source_ref": source_ref, "sha256": source_hash}
        )
        equivalence.append(
            {
                "case_id": case,
                "question_count": len(labels),
                "compared_fields": list(FIELDS),
                "metric_reference_equal": True,
                "non_metric_note": "IMP-CROSS asset source_file is added in T146; metric only compares type/page/order.",
            }
        )
    commit = manifest["source_commit"]
    changed_committed = subprocess.run(
        ["git", "diff", "--name-only", commit, "HEAD", "--", *SOURCE_PATHS],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    changed_worktree = subprocess.run(
        ["git", "diff", "--name-only", "HEAD", "--", *SOURCE_PATHS],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    require(
        not changed_committed and not changed_worktree,
        "import source changed; audit rerun before reuse",
    )
    current_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    artifacts: list[dict[str, Any]] = []

    def record(path: Path) -> str:
        ref = path.relative_to(root).as_posix()
        artifacts.append(
            {"path": ref, "sha256": sha(path), "bytes": path.stat().st_size}
        )
        return ref

    record(source / "manifest.json")
    record(root / REFERENCE)
    record(root / OLD_REFERENCE)
    rows: list[dict[str, Any]] = []
    identities: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    slots: Counter[tuple[str, int]] = Counter()
    discrepancies, requests, known_crop_comparisons = 0, 0, 0
    ocr_pages, native_pages = 0, 0
    import_statuses: Counter[str] = Counter()
    after_rows: list[dict[str, Any]] = []
    for path in sorted((source / "runs").glob("*/result.json")):
        evidence_ref = record(path)
        result = read(path)
        case = result["case_id"]
        require(case in CASES, f"unexpected case: {case}")
        slots[(case, result["repeat_index"])] += 1
        require(
            result["source_sha256"]
            == next(item["sha256"] for item in inputs if item["case_id"] == case),
            "result input receipt differs",
        )
        require(result["evidence_kind"] == "real_provider", "non-real evidence")
        result["evidence_ref"] = evidence_ref
        requests += result["actual_http_attempts"]
        import_statuses[result["automatic_status"]] += 1
        if result.get("runner_error") or result["automatic_status"] != "Pending Review":
            failures.append(
                {
                    "stage": "import",
                    "run_id": result["run_id"],
                    "evidence_ref": evidence_ref,
                    "status": result["automatic_status"],
                    "runner_error": result.get("runner_error"),
                }
            )
        labels = current_cases[case]["labels"]["questions"]
        for stage, key, old_key in (
            ("automatic", "automatic_snapshot", "automatic_comparison"),
            (
                "assisted_corrected",
                "corrected_snapshot",
                "assisted_corrected_comparison",
            ),
        ):
            require(
                key in result, f"stage snapshot missing: {result['run_id']}/{stage}"
            )
            comparisons, identity = compare(labels, result[key], result, stage)
            old_rows = {
                (row["reference_question_identity"], row["field"]): row
                for row in result[old_key]["fields"]
            }
            for row in comparisons:
                old = old_rows[(row["reference_question_identity"], row["field"])]
                discrepancies += (
                    old["correct"] != row["correct"]
                    or old["evaluable"] != row["evaluable"]
                )
            rows.extend(comparisons)
            identities.append(identity)
        for page in result["automatic_snapshot"]["pages"]:
            ocr_pages += page["ocr_text"] is not None
            native_pages += page["ocr_text"] is None
        for action in result.get("correction_actions", []):
            if action["status"] != "persisted":
                failures.append(
                    {
                        "stage": "assisted_corrected",
                        "run_id": result["run_id"],
                        "action": action,
                    }
                )
        after_path = source / "image-assistance" / (result["run_id"] + ".json")
        after = read(after_path)
        after_ref = record(after_path)
        require(
            after["reference_sha256"] == manifest["annotation_sha256"],
            "crop reference differs",
        )
        after_result = {**result, "evidence_ref": after_ref}
        after_comparisons, after_identity = compare(
            labels, after["snapshot"], after_result, "after_image_assistance"
        )
        after_rows.extend(after_comparisons)
        identities.append(after_identity)
        old_images = {
            row["reference_identity"]: row
            for row in after["image_association_comparison"]
        }
        for row in after_comparisons:
            if row["field"] == "assets":
                require(
                    row["correct"]
                    == old_images[row["reference_question_identity"]]["correct"],
                    "crop comparison differs",
                )
                known_crop_comparisons += 1
    planned = {(case, repeat) for case in CASES for repeat in range(1, 4)}
    require(
        set(slots) == planned and all(count == 1 for count in slots.values()),
        "fixed 15-slot evidence incomplete",
    )
    require(discrepancies == 0, "old comparison and raw recomputation differ")
    crops_path = source / "image-assistance/actions.json"
    crops = read(crops_path)
    record(crops_path)
    crop_actions = []
    for action in crops["actions"]:
        successful = action["status"] == "persisted"
        receipt = {
            "run_id": action["run_id"],
            "case_id": action["case_id"],
            "reference_identity": action["reference_identity"],
            "status": action["status"],
            "teacher_review_created": action["teacher_review_created"],
            "crop_sha256": action.get("crop_sha256"),
        }
        if successful:
            crop = source / action["crop_evidence_ref"].replace("\\", "/")
            require(sha(crop) == action["crop_sha256"], "crop evidence differs")
            receipt["crop_evidence_ref"] = record(crop)
        else:
            failures.append({"stage": "after_image_assistance", "action": action})
        crop_actions.append(receipt)
    all_rows = rows + after_rows
    metrics = aggregate(all_rows)
    stage_summaries = []
    for stage in ("automatic", "assisted_corrected", "after_image_assistance"):
        members = [row for row in identities if row["stage"] == stage]
        stage_summaries.append(
            {
                "stage": stage,
                "identity_recall": metric(
                    "unique_original_number_and_source_page_identity_recall",
                    sum(row["matched_identities"] for row in members),
                    48,
                ),
                "actual_question_count": sum(
                    row["actual_question_count"] for row in members
                ),
                "missing_identities": sum(row["missing_identities"] for row in members),
                "duplicate_actual_identities": sum(
                    row["duplicate_actual_identities"] for row in members
                ),
                "unmatched_actual_questions": sum(
                    row["unmatched_actual_questions"] for row in members
                ),
                "all_evaluable_fields_match": metric(
                    "all_evaluable_fields_match",
                    sum(row["all_evaluable_fields_match_questions"] for row in members),
                    48,
                ),
                "unknown_fields_excluded": sum(
                    row["unknown_fields_excluded"] for row in members
                ),
                "fields": [
                    item
                    for item in metrics
                    if item["stage"] == stage and item["layer"] == "all"
                ],
            }
        )
    summary = {
        "task_id": "T168",
        "generated_at": datetime.now(UTC).isoformat(),
        "baseline_source": "AI assisted + developer review (learning project)",
        "annotation_authors": ["AI (session delegation)", "developer"],
        "independent_teacher_truth": False,
        "reference_version": current["annotation_version"],
        "runner_sha256": sha(Path(__file__)),
        "reference_sha256": sha(root / REFERENCE),
        "evidence_kind": "historical_real_provider_runs_recomputed_against_current_reference",
        "new_model_calls": 0,
        "historical_model_requests": requests,
        "planned_imports": 15,
        "observed_imports": len(slots),
        "planned_question_instances": 48,
        "import_status_counts": dict(import_statuses),
        "execution_failure_count": len(failures),
        "field_mismatch_counts": {
            stage: sum(
                row["evaluable"] and row["correct"] is False
                for row in all_rows
                if row["stage"] == stage
            )
            for stage in ("automatic", "assisted_corrected", "after_image_assistance")
        },
        "evidence_protocol_complete": True,
        "quality_threshold": None,
        "quality_acceptance": "not_claimed_pending_user_threshold_confirmation",
        "stages": stage_summaries,
        "ocr": {
            "actual_ocr_page_instances": ocr_pages,
            "native_page_instances": native_pages,
            "cer": None,
            "wer": None,
            "reason": "No frozen full-page verbatim transcript/read-order/tokenization reference. Structured question fields cannot be an OCR CER/WER denominator.",
            "ocr_confidence_is_not_accuracy": True,
        },
        "image_assistance": {
            "persisted_crops": sum(
                row["status"] == "persisted" for row in crop_actions
            ),
            "failed_crops": sum(row["status"] != "persisted" for row in crop_actions),
            "recomputed_question_asset_associations": known_crop_comparisons,
            "pixel_crop_accuracy": None,
            "reason": "Actual historical source crops and ordered type/page association; original pixel boundaries are unknown. Not automatic image recognition.",
        },
        "limitations": [
            "15/15 Pending Review is import completion, not field quality acceptance.",
            "Strict text comparison only normalizes line endings and trim; type prefixes/internal spaces remain mismatches.",
            "Automatic assets null is distinct from reference [] even on 15 no-image question instances; all 33 required image associations were also absent automatically.",
            "Corrected stages apply the AI-assisted reference through a synthetic role executor; no independent teacher correction or teacher performance is claimed.",
            "Source regions (48 instances) and cross-page question type (3 instances) remain unknown/excluded.",
            "Five synthetic ordinary cases cannot establish accuracy on real unseen papers.",
        ],
    }
    proof = {
        "historical_source_commit": commit,
        "current_source_commit": current_commit,
        "historical_run_manifest": manifest,
        "current_reference_provenance": current["provenance"],
        "code_scope": list(SOURCE_PATHS),
        "changed_committed_paths": changed_committed,
        "changed_worktree_paths": changed_worktree,
        "relevant_import_code_unchanged": True,
        "input_receipts": inputs,
        "reference_equivalence": equivalence,
        "old_raw_field_comparison_rows_recomputed": len(rows),
        "old_comparison_discrepancies": discrepancies,
        "input_artifacts": artifacts,
        "policy": {
            "identity": "unique original question number + ordered actual source pages",
            "normalization": "line endings and trim only; Decimal score; exact knowledge-point set; option/asset order preserved",
            "unknown": "excluded and separately counted; explicit source-missing fields remain evaluable",
            "source_versions": "traceability of reused measurement; no claim of new execution",
        },
    }
    output = root / OUTPUT
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "summary.json", summary)
    write_json(output / "reuse-proof.json", proof)
    write_csv(output / "fields.csv", all_rows)
    write_csv(output / "metrics.csv", metrics)
    write_csv(output / "identities.csv", identities)
    (output / "mismatches.jsonl").write_text(
        "".join(
            json.dumps(row, ensure_ascii=False) + "\n"
            for row in all_rows
            if row["evaluable"] and row["correct"] is False
        ),
        encoding="utf-8",
    )
    write_json(output / "crop-actions.json", crop_actions)
    (output / "failures.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in failures),
        encoding="utf-8",
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root", type=Path, default=Path(__file__).resolve().parents[2]
    )
    args = parser.parse_args()
    summary = run(args.repo_root.resolve())
    print(
        json.dumps(
            {
                "task_id": "T168",
                "observed_imports": summary["observed_imports"],
                "new_model_calls": summary["new_model_calls"],
                "execution_failure_count": summary["execution_failure_count"],
                "output": OUTPUT.as_posix(),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
