"""Strict practice/generation DTOs and answer-only payload boundaries."""
from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.app.schemas.personal_practice import (
    GenerationRequest,
    PracticeAnswerInput,
    PracticeConditions,
    PracticeStartRequest,
)


def test_defaults_match_real_first_balanced_unseen_twenty():
    value = PracticeConditions()
    assert (value.source,value.mode,value.count,value.prefer_unseen,value.allow_practiced) == (
        "mixed","balanced",20,True,False)


@pytest.mark.parametrize("source",["uploaded","ai","mixed"])
@pytest.mark.parametrize("mode",["random","balanced","weak"])
def test_all_source_mode_pairs_are_explicit(source,mode):
    assert PracticeConditions(source=source,mode=mode).source == source


@pytest.mark.parametrize("data",[
    {"count":False},{"count":0},{"count":"20"},{"prefer_unseen":0},{"allow_practiced":{}},
    {"knowledge_point_ids":False},{"knowledge_point_ids":[str(uuid4())]*2},
    {"source":"anything"},{"mode":"daily"},{"difficulty":""},{"chapter_ids":[]},
])
def test_invalid_filter_values_cannot_be_silently_ignored(data):
    with pytest.raises(ValidationError):
        PracticeConditions(**data)


def test_generation_grounding_has_no_implicit_fallback():
    with pytest.raises(ValidationError):
        GenerationRequest(idempotency_key="missing-grounding")
    with pytest.raises(ValidationError):
        GenerationRequest(grounding="uploaded_material",idempotency_key="no-selected-material")
    with pytest.raises(ValidationError):
        GenerationRequest(grounding="general_knowledge",material_version_ids=[uuid4()],idempotency_key="fake-source")
    assert GenerationRequest(grounding="general_knowledge",idempotency_key="explicit").grounding == "general_knowledge"


@pytest.mark.parametrize("score",[0,-1,True,"NaN","Infinity","1.001"])
def test_start_scores_must_be_storable_positive_finite(score):
    with pytest.raises(ValidationError):
        PracticeStartRequest(preview_digest="a"*64,scores={uuid4():score})


def test_answers_only_allow_one_input_kind_and_never_reference_answer():
    with pytest.raises(ValidationError):
        PracticeAnswerInput(choice_ids=["A"],text_answer="also")
    with pytest.raises(ValidationError):
        PracticeAnswerInput(reference_answer="leak")
    with pytest.raises(ValidationError):
        PracticeAnswerInput(choice_ids=["A","A"])
    assert PracticeAnswerInput().choice_ids is None
