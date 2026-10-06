"""TCR48: user-confirmed field thresholds, preserving true facts and unknowns."""

from copy import deepcopy

import pytest

from benchmark.t168.field_acceptance import assess, validate_slots
from benchmark.t168.import_baseline import CASES, FIELDS, compare


def test_threshold_boundary_and_no_evaluable_denominator():
    policy = {"targets": {"identity": 1.0, "content": 0.8}}
    identities = [
        {
            "reference_question_count": 10,
            "matched_identities": 10,
            "duplicate_actual_identities": 0,
            "unmatched_actual_questions": 0,
        }
    ]
    rows = [
        {"field": "content", "evaluable": True, "correct": n < 8} for n in range(10)
    ]
    assert all(item["met"] for item in assess(rows, identities, policy))
    rows[7]["correct"] = False
    assert assess(rows, identities, policy)[1]["met"] is False
    rows = [{"field": "content", "evaluable": False, "correct": None}]
    missing = assess(rows, identities, policy)[1]
    assert (
        missing["denominator"] == 0
        and missing["value"] is None
        and missing["met"] is False
    )


def test_identity_100_percent_and_duplicates_cannot_pass():
    identities = [
        {
            "reference_question_count": 10,
            "matched_identities": 9,
            "duplicate_actual_identities": 0,
            "unmatched_actual_questions": 0,
        }
    ]
    policy = {"targets": {"identity": 1.0}}
    assert assess([], identities, policy)[0]["met"] is False
    identities[0]["matched_identities"] = 10
    identities[0]["duplicate_actual_identities"] = 1
    assert assess([], identities, policy)[0]["met"] is False


def test_unknown_is_excluded_known_missing_is_evaluated_and_assets_are_diagnostic():
    label = dict.fromkeys(FIELDS, None)
    label.update(
        question_identity="original-q",
        question_number="9",
        order_index=1,
        source_page_numbers=[1],
        knowledge_points=[],
        assets=[],
        unknown=["source_regions"],
    )
    actual = deepcopy(label)
    actual.update(
        id="actual-q", source_page_ids=["page-1"], assets=None, knowledge_points=None
    )
    rows, identity = compare(
        [label],
        {
            "pages": [{"id": "page-1", "page_number": 1, "ocr_text": None}],
            "questions": [actual],
        },
        {
            "run_id": "run",
            "case_id": "IMP-TEXT",
            "repeat_index": 1,
            "evidence_ref": "run.json",
        },
        "automatic",
    )
    by_field = {row["field"]: row for row in rows}
    assert by_field["source_regions"]["evaluable"] is False
    assert by_field["knowledge_points"]["evaluable"] is True
    assert by_field["knowledge_points"]["correct"] is False
    assert by_field["assets"]["correct"] is False
    assert all(
        item["met"] for item in assess(rows, [identity], {"targets": {"identity": 1.0}})
    )


def test_complete_three_round_protocol_rejects_missing_and_duplicate_slots():
    slots = [
        {"case_id": case, "repeat_index": repeat}
        for case in CASES
        for repeat in (1, 2, 3)
    ]
    validate_slots(slots)
    with pytest.raises(ValueError):
        validate_slots(slots[:-1])
    with pytest.raises(ValueError):
        validate_slots(slots[:-1] + [slots[0]])
