"""Same-response admission, never an automatic second semantic request."""
from importlib import import_module
from uuid import uuid4

import pytest

from backend.app.schemas.content_blocks import RevisionContent


def content(**changes):
    value = {"question_type": "single_choice",
             "stem_blocks": [{"type": "text", "text": "Choose one."}],
             "options": [{"id": k, "blocks": [{"type": "text", "text": k}]} for k in ("A","B")]}
    value.update(changes)
    return RevisionContent.model_validate(value)


def quality(**changes):
    value = {
        "answer_basis": "The stated facts imply option A.",
        "assessment": {"checks": [
            {"kind": kind, "verdict": "pass", "reason": "Synthetic explicit derivation"}
            for kind in ("answer_correctness", "condition_sufficiency", "option_ambiguity", "rubric_clarity")
        ], "issues": []},
    }
    value.update(changes)
    return value


def evaluate(**changes):
    module = import_module("backend.app.services.question_validator")
    function = getattr(module, "personal_admission", None)
    assert function is not None, "T037_IMPLEMENTATION_REQUIRED"
    args = {"content": content(), "reference_answer": "A", "scoring_rubric": None,
            "score": 10, "quality": quality(), "allowed_evidence_ids": set(),
            "uncertainties": [], "confirmed_asset_ids": set()}
    args.update(changes)
    return function(**args)


def test_same_response_complete_local_structure_is_eligible():
    assert evaluate() == ("eligible", [])


@pytest.mark.parametrize("changes,issue", [
    ({"reference_answer": None}, "ANSWER_MISSING"),
    ({"reference_answer": "Z"}, "ANSWER_ENCODING_INVALID"),
    ({"score": 0}, "SCORE_INVALID"),
    ({"uncertainties": ["Unreadable condition"]}, "CONTENT_UNCERTAIN"),
    ({"content": content(question_type="short_answer", options=[]), "scoring_rubric": None}, "RUBRIC_MISSING"),
])
def test_deterministic_failures_never_become_eligible(changes, issue):
    state, issues = evaluate(**changes)
    assert state != "eligible" and issue in issues


def test_model_warning_or_failed_check_blocks_without_second_call():
    for kind in ("answer_correctness", "condition_sufficiency", "option_ambiguity", "rubric_clarity"):
        value = quality()
        next(c for c in value["assessment"]["checks"] if c["kind"] == kind)["verdict"] = "needs_review"
        assert evaluate(quality=value)[0] == "needs_correction"
    value = quality()
    value["assessment"]["issues"] = [{"code":"UNCERTAIN", "severity":"warning","message":"Not proved"}]
    assert evaluate(quality=value)[0] == "needs_correction"


def test_fabricated_evidence_and_empty_basis_are_not_usable():
    value = quality()
    value["assessment"]["checks"][0]["evidence_refs"] = [str(uuid4())]
    assert "UNKNOWN_EVIDENCE" in evaluate(quality=value)[1]
    assert evaluate(quality=quality(answer_basis=" "))[0] != "eligible"


def test_key_image_without_confirmed_input_stays_pending():
    image = uuid4()
    value = content(stem_blocks=[{"type": "image", "asset_id": image}])
    assert "IMAGE_EVIDENCE_REQUIRED" in evaluate(content=value)[1]


def test_chart_answer_data_must_equal_the_actual_public_chart():
    chart = {"kind":"line", "title":"Measured values", "x_axis":{"label":"time","unit":"s"},
             "y_axis":{"label":"length","unit":"m"}, "points":[{"x":1.0,"y":2.0}],
             "basis":"Given measurements"}
    value = content(stem_blocks=[{"type":"chart","chart":chart}])
    assert "CHART_BASIS_INVALID" in evaluate(content=value)[1]
    q = quality(chart_answer_points={"0":[{"x":1.0,"y":99.0}]})
    assert "CHART_BASIS_INVALID" in evaluate(content=value,quality=q)[1]
    q = quality(chart_answer_points={"0":chart["points"]})
    assert evaluate(content=value,quality=q)[0] == "eligible"

@pytest.mark.parametrize("value", [
    content(stem_blocks=[{"type":"text","text":"   "}]),
    content(options=[{"id":"A","blocks":[{"type":"text","text":"  "}]},{"id":"B","blocks":[{"type":"text","text":"nonempty"}]}]),
])
def test_blank_stem_or_option_cannot_be_admitted_even_if_model_claims_pass(value):
    assert "CONTENT_EMPTY" in evaluate(content=value)[1]
