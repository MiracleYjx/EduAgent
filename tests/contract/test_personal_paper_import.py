"""Personal paper contract: three types, honest sources and explicit confirmation."""

from importlib import import_module
from uuid import uuid4

import pytest
from pydantic import ValidationError


def schema(name):
    module = import_module("backend.app.ai.paper_extraction.schemas")
    value = getattr(module, name, None)
    assert value is not None, f"T035_IMPLEMENTATION_REQUIRED: {name}"
    return value


def candidate(question_type="single_choice", **changes):
    value = {
        "question_type": question_type,
        "stem_blocks": [{"type": "text", "text": "Choose the correct statement."}],
        "options": [] if question_type == "short_answer" else [
            {"id": "A", "blocks": [{"type": "text", "text": "First"}]},
            {"id": "B", "blocks": [{"type": "text", "text": "Second"}]},
        ],
        "source_locations": [{"document_id": str(uuid4()), "anchor": "paragraph-1"}],
    }
    value.update(changes)
    return value


@pytest.mark.parametrize("kind", ["single_choice", "multiple_choice", "short_answer"])
def test_three_personal_types_preserve_missing_original_answer(kind):
    value = schema("PersonalPaperCandidate").model_validate(candidate(kind))
    assert value.question_type == kind
    assert value.reference_answer is None and value.answer_origin is None
    assert value.source_locations[0].anchor == "paragraph-1"


@pytest.mark.parametrize("kind", ["true_false", "essay", "single-choice"])
def test_legacy_or_unknown_types_do_not_enter_personal_import(kind):
    with pytest.raises(ValidationError):
        schema("PersonalPaperCandidate").model_validate(candidate(kind))


def test_original_answer_needs_evidence_and_cannot_be_guessed():
    with pytest.raises(ValidationError):
        schema("PersonalPaperCandidate").model_validate(
            candidate(reference_answer="A", answer_origin="paper")
        )
    location = candidate()["source_locations"][0]
    value = schema("PersonalPaperCandidate").model_validate(candidate(
        source_locations=[location], reference_answer="A", answer_origin="paper",
        evidence={"reference_answer": {"source": location, "text": "Answer: A"}},
    ))
    assert value.answer_origin == "paper"


def test_original_evidence_must_cover_the_claimed_answer():
    location = candidate()["source_locations"][0]
    with pytest.raises(ValidationError):
        schema("PersonalPaperCandidate").model_validate(candidate(
            source_locations=[location], reference_answer="B", answer_origin="paper",
            evidence={"reference_answer": {"source": location, "text": "Answer: A"}},
        ))


def test_model_cannot_forge_user_correction_or_ai_completion_origin():
    for origin in ("user_correction", "ai_completion"):
        with pytest.raises(ValidationError):
            schema("PersonalPaperCandidate").model_validate(
                candidate(reference_answer="A", answer_origin=origin)
            )


def test_source_requires_an_actual_page_or_object_anchor():
    with pytest.raises(ValidationError):
        schema("PersonalPaperCandidate").model_validate(
            candidate(source_locations=[{"document_id": str(uuid4())}])
        )


def test_ordered_stem_and_option_images_are_not_flattened():
    first, second = uuid4(), uuid4()
    value = schema("PersonalPaperCandidate").model_validate(candidate(
        stem_blocks=[
            {"type": "text", "text": "Before"},
            {"type": "image", "asset_id": str(first)},
            {"type": "text", "text": "After"},
        ],
        options=[
            {"id": "A", "blocks": [{"type": "image", "asset_id": str(second)}]},
            {"id": "B", "blocks": [{"type": "text", "text": "Other"}]},
        ],
    ))
    assert [block.type for block in value.stem_blocks] == ["text", "image", "text"]
    assert value.stem_blocks[1].asset_id == first
    assert value.options[0].blocks[0].asset_id == second


def test_model_paths_and_duplicate_option_ids_are_rejected():
    with pytest.raises(ValidationError):
        schema("PersonalPaperCandidate").model_validate(candidate(
            stem_blocks=[{"type": "image", "asset_id": str(uuid4()), "file_path": "../secret"}],
        ))
    payload = candidate()
    payload["options"][1]["id"] = "A"
    with pytest.raises(ValidationError):
        schema("PersonalPaperCandidate").model_validate(payload)


def test_uncertainty_and_optional_original_fields_are_preserved():
    value = schema("PersonalPaperCandidate").model_validate(candidate(
        difficulty=None, score=None, knowledge_point_ids=[],
        uncertainties=["FORMULA_UNCERTAIN"], question_number="3",
    ))
    assert value.uncertainties == ["FORMULA_UNCERTAIN"]
    assert value.score is None and value.difficulty is None
    assert value.question_number == "3"


def test_confirmation_requires_a_nonempty_unique_selected_scope():
    confirm = schema("PersonalPaperConfirm")
    for ids in ([], [str(uuid4())] * 2):
        with pytest.raises(ValidationError):
            confirm.model_validate({"question_ids": ids, "expected_revisions": {}})
    identity = str(uuid4())
    value = confirm.model_validate({
        "question_ids": [identity], "expected_revisions": {identity: 1},
    })
    assert value.question_ids == [__import__("uuid").UUID(identity)]


def test_original_explanation_cannot_be_invented_without_source_evidence():
    from pydantic import ValidationError

    from backend.app.ai.paper_extraction.schemas import PersonalPaperCandidate
    with pytest.raises(ValidationError, match="explanation"):
        PersonalPaperCandidate.model_validate({
            **candidate(), "explanation_blocks": [{"type": "text", "text": "Invented explanation"}],
        })


@pytest.mark.parametrize("value",[{},{"questions":[]}])
def test_empty_personal_extraction_is_not_a_successful_question_batch(value):
    with pytest.raises(ValidationError):
        schema("PersonalPaperBatch").model_validate(value)
