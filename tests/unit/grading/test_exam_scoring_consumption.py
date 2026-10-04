"""T177 actual per-exam standards, decimal results and image scoring (TCR §32)."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from uuid import uuid4

import pytest
from PIL import Image

from backend.app.ai.agents.grading_agent import GradingAgent
from backend.app.ai.retrieval.base import (
    RetrievalFilters,
    RetrievalMode,
    RetrievedChunk,
)
from backend.app.ai.vision.base import VisionImage
from backend.app.domain.enums import QuestionType
from backend.app.schemas.grading import ScoringInput
from backend.app.services.grading.grading_context import (
    GradingContext,
    SubjectiveGradingSource,
)
from backend.app.services.grading.grading_task_service import (
    GradingTargetAnswer,
    SubmissionSnapshot,
)
from backend.app.services.grading.objective_grader import ObjectiveGrader
from backend.app.services.grading.subjective_grader import (
    ScoreOutOfRangeError,
    SubjectiveGrader,
    SubjectiveGradingPayload,
    build_grading_messages,
    parse_subjective_payload,
)
from tests.unit.settings_helpers import build_test_settings


def fixed_input():
    ids = {
        name: str(uuid4())
        for name in (
            "exam_id",
            "exam_question_id",
            "question_id",
            "submission_id",
            "answer_id",
            "student_id",
            "course_id",
        )
    }
    teacher = uuid4()
    value = ScoringInput.model_validate(
        {
            **ids,
            "order": 1,
            "question_validation_revision": 1,
            "question_type": "SHORT_ANSWER",
            "question_content": "Explain the three steps.",
            "options": None,
            "order_preserved": True,
            "reference_answer": "prepare execute record",
            "source_rubric": "Original three-point rubric: prepare, execute, record.",
            "effective_score": "10.00",
            "base_score": "3.00",
            "scoring_basis": {
                "kind": "subjective",
                "additive": True,
                "rounding_delta": "0.01",
                "points": [
                    {
                        "key": key,
                        "label": label,
                        "base_points": "1.00",
                        "default_points": "3.33",
                        "confirmed_points": points,
                    }
                    for key, label, points in [
                        ("p1", "prepare", "3.34"),
                        ("p2", "execute", "3.33"),
                        ("p3", "record", "3.33"),
                    ]
                ],
                "confirmation": {
                    "teacher_id": str(teacher),
                    "confirmed_at": datetime.now(UTC).isoformat(),
                    "reason": "Controlled teacher review of the tail.",
                },
            },
            "published_knowledge_points": ["Fixed K"],
            "student_answer": "prepare execute",
        }
    )
    return value, str(teacher)


def source(value, teacher):
    return SubjectiveGradingSource(
        question_type=value.question_type,
        course_id=value.course_id,
        question_content=value.question_content,
        reference_answer=value.reference_answer,
        scoring_rubric=value.source_rubric,
        student_answer=value.student_answer,
        knowledge_points=tuple(value.published_knowledge_points),
        question_id=value.question_id,
        answer_id=value.answer_id,
        submission_id=value.submission_id,
        scoring_input=value,
        teacher_id=teacher,
    )


def context(src):
    chunk = RetrievedChunk(
        chunk_id="actual-chunk",
        course_id=src.course_id,
        document_id="actual-document",
        content="Real three-step course material.",
        metadata={"location": "actual paragraph"},
    )
    return GradingContext(
        source=src,
        query_text="real query",
        retrieval_mode=RetrievalMode.HYBRID_RERANK,
        filters=RetrievalFilters(course_ids=(src.course_id,)),
        chunks=(chunk,),
        retrieved_context_ids=(chunk.chunk_id,),
        final_context="Real final context actually passed to the model.",
        candidate_count=1,
    )


def payload(score="6.675"):
    return {
        "score": score,
        "confidence": 0.9,
        "reason": "Two of the three steps were given.",
        "correct_points": ["prepare", "execute"],
        "missing_knowledge_points": ["record"],
        "suggestions": ["Explain the record step."],
    }


@pytest.mark.parametrize("score, expected", [("6.675", "6.68"), ("0.005", "0.01")])
def test_exact_decimal_result_and_real_exam_identity(score, expected):
    result = parse_subjective_payload(
        payload(score),
        question_type=QuestionType.SHORT_ANSWER,
        max_score=Decimal("10.00"),
        answer_id="a",
        submission_id="s",
        exam_question_id="real-link",
    )
    assert result.score == Decimal(expected) and isinstance(result.score, Decimal)
    assert (
        result.max_score == Decimal("10.00") and result.exam_question_id == "real-link"
    )
    assert result.model_dump(mode="json")["score"] == expected


@pytest.mark.parametrize("raw", ["10.00000000000000000001", "-0.00000000000000000001"])
def test_raw_score_rejected_before_rounding(raw):
    with pytest.raises(ScoreOutOfRangeError):
        parse_subjective_payload(
            payload(raw),
            question_type=QuestionType.SHORT_ANSWER,
            max_score=Decimal("10.00"),
        )


def test_original_json_numeric_precision_is_not_lost():
    raw = '{"score":10.00000000000000000001,"confidence":0.9,"reason":"r","correct_points":[],"missing_knowledge_points":[],"suggestions":["s"]}'
    with pytest.raises(ScoreOutOfRangeError):
        parse_subjective_payload(
            raw, question_type=QuestionType.SHORT_ANSWER, max_score=Decimal("10.00")
        )


def test_messages_use_confirmed_exam_points_without_second_scaling():
    value, teacher = fixed_input()
    messages = build_grading_messages(
        context(source(value, teacher)), max_score=Decimal("10.00")
    )
    user = messages[1]["content"]
    assert value.source_rubric in user
    assert "3.34" in user and "3.33" in user and "10.00" in user
    assert "不二次" in user or "不得再次" in user


def test_source_rejects_wrong_fixed_question_without_rebuilding_input():
    value, teacher = fixed_input()
    with pytest.raises(Exception) as failure:
        replace(source(value, teacher), question_id=str(uuid4()))
    assert getattr(failure.value, "error_code", None) == "GRADING_INVALID_INPUT"


class Provider:
    def __init__(self, vision=True):
        self.calls = []
        self.closed = False
        self.vision = vision

    def supports_vision(self):
        return self.vision

    async def generate_structured(self, messages, schema):
        self.calls.append((messages, schema))
        return schema.model_validate(payload())

    async def aclose(self):
        self.closed = True


def prepare_context(monkeypatch, src):
    async def actual_context(*args, **kwargs):
        return context(src)

    monkeypatch.setattr(
        "backend.app.services.grading.subjective_grader.build_grading_context",
        actual_context,
    )


def test_real_retrieval_enrichment_preserves_all_fixed_facts(monkeypatch):
    value, teacher = fixed_input()
    src = source(value, teacher)
    prepare_context(monkeypatch, src)
    received = []
    provider = Provider()
    result = asyncio.run(
        SubjectiveGrader(provider=provider, on_scoring_input=received.append).grade(
            object(), src, max_score=Decimal("10.00"), settings=build_test_settings()
        )
    )
    assert (
        result.score == Decimal("6.68")
        and result.exam_question_id == value.exam_question_id
    )
    assert len(received) == 1
    enriched = received[0]
    assert enriched.course_context == context(src).final_context
    assert enriched.source_references[0].chunk_id == "actual-chunk"
    assert enriched.source_references[0].metadata == {"location": "actual paragraph"}
    assert value.course_context is None and value.source_references is None


def image():
    stream = BytesIO()
    Image.new("RGB", (4, 3), (40, 80, 120)).save(stream, format="PNG")
    return VisionImage.from_bytes(stream.getvalue())


def test_actual_images_use_independent_vision_provider_and_close(monkeypatch):
    from backend.app.services.grading import subjective_grader as module

    value, teacher = fixed_input()
    asset = {
        "id": uuid4(),
        "question_id": value.question_id,
        "file_id": "controlled-original",
        "asset_type": "diagram",
        "width": 4,
        "height": 3,
        "caption": "Actual diagram",
        "source_page_id": None,
        "region": None,
        "order_index": 1,
        "student_visible": True,
    }
    from backend.app.schemas.question_assets import QuestionAssetView

    value = value.model_copy(
        update={"assets": [QuestionAssetView.model_validate(asset)]}
    )
    src = source(value, teacher)
    prepare_context(monkeypatch, src)
    text = Provider()
    vision = Provider()
    monkeypatch.setattr(module, "create_vision_provider", lambda settings: vision)
    monkeypatch.setattr(
        module, "prepare_scoring_images", lambda *args, **kwargs: (image(),)
    )
    result = asyncio.run(
        SubjectiveGrader(provider=text).grade(
            object(), src, max_score=Decimal("10.00"), settings=build_test_settings()
        )
    )
    assert result.score == Decimal("6.68")
    assert text.calls == [] and len(vision.calls) == 1 and vision.closed
    parts = vision.calls[0][0][1]["content"]
    assert isinstance(parts, list) and any(p["type"] == "image" for p in parts)
    part = next(p for p in parts if p["type"] == "image")
    assert part["image"].value == __import__("base64").b64encode(image().data).decode(
        "ascii"
    )
    assert vision.calls[0][1] is SubjectiveGradingPayload


def test_no_image_capability_preserves_error_without_text_fallback(monkeypatch):
    from backend.app.schemas.question_assets import QuestionAssetView
    from backend.app.services.grading import subjective_grader as module

    value, teacher = fixed_input()
    value = value.model_copy(
        update={
            "assets": [
                QuestionAssetView.model_validate(
                    {
                        "id": uuid4(),
                        "question_id": value.question_id,
                        "file_id": "controlled-original",
                        "asset_type": "diagram",
                        "width": 4,
                        "height": 3,
                        "caption": None,
                        "source_page_id": None,
                        "region": None,
                        "order_index": 1,
                    }
                )
            ]
        }
    )
    src = source(value, teacher)
    prepare_context(monkeypatch, src)
    text = Provider()
    vision = Provider(False)
    monkeypatch.setattr(module, "create_vision_provider", lambda settings: vision)
    with pytest.raises(Exception) as failure:
        asyncio.run(
            SubjectiveGrader(provider=text).grade(
                object(),
                src,
                max_score=Decimal("10.00"),
                settings=build_test_settings(),
            )
        )
    assert failure.value.error_code == "VISION_NOT_SUPPORTED"
    assert vision.closed and text.calls == [] and vision.calls == []


def test_objective_decimal_grade_has_real_exam_link_without_provider():
    result = ObjectiveGrader().grade(
        question_type=QuestionType.SINGLE_CHOICE,
        reference_answer="A",
        student_answer="A",
        max_score=Decimal("17.23"),
        answer_id="a",
        submission_id="s",
        exam_question_id="link",
    )
    assert result.score == Decimal("17.23") and isinstance(result.score, Decimal)
    assert result.exam_question_id == "link" and result.confidence == 1


def test_agent_envelope_carries_actual_enriched_input(monkeypatch):
    value, teacher = fixed_input()
    src = source(value, teacher)
    prepare_context(monkeypatch, src)
    target = GradingTargetAnswer.from_scoring_input(value, teacher_id=teacher)
    snapshot = SubmissionSnapshot(
        submission_id=value.submission_id,
        exam_id=value.exam_id,
        student_id=value.student_id,
        course_id=value.course_id,
        status="Submitted",
        answers=(target,),
        scoring_basis_error=None,
    )
    invocation = asyncio.run(
        GradingAgent(
            provider=Provider(), settings=build_test_settings()
        ).grade_answer_async(
            snapshot, target, request_id="controlled-request", session=object()
        )
    )
    assert invocation.output.error is None
    assert invocation.output.grading_result.exam_question_id == value.exam_question_id
    assert invocation.input.scoring_input.course_context == context(src).final_context
    assert (
        invocation.input.scoring_input.source_references[0].chunk_id == "actual-chunk"
    )


@pytest.mark.parametrize("original_failure", [True, False])
def test_vision_cleanup_failure_preserves_original_error_or_fails_success(
    monkeypatch, original_failure
):
    from backend.app.schemas.question_assets import QuestionAssetView
    from backend.app.services.grading import subjective_grader as module
    from backend.app.services.grading.scoring_images import GradingImageError

    value, teacher = fixed_input()
    value = value.model_copy(
        update={
            "assets": [
                QuestionAssetView.model_validate(
                    {
                        "id": uuid4(),
                        "question_id": value.question_id,
                        "file_id": "actual-original",
                        "asset_type": "diagram",
                        "width": 4,
                        "height": 3,
                        "caption": None,
                        "source_page_id": None,
                        "region": None,
                        "order_index": 1,
                    }
                )
            ]
        }
    )
    src = source(value, teacher)
    prepare_context(monkeypatch, src)

    class ClosingFailureProvider(Provider):
        async def aclose(self):
            self.closed = True
            raise RuntimeError("controlled cleanup failure")

    vision = ClosingFailureProvider()
    monkeypatch.setattr(module, "create_vision_provider", lambda settings: vision)

    def load(*args, **kwargs):
        if original_failure:
            raise GradingImageError("FILE_MISSING", "真实原件缺失。")
        return (image(),)

    monkeypatch.setattr(module, "prepare_scoring_images", load)
    with pytest.raises(GradingImageError) as failure:
        asyncio.run(
            SubjectiveGrader(provider=Provider()).grade(
                object(),
                src,
                max_score=Decimal("10.00"),
                settings=build_test_settings(),
            )
        )
    assert vision.closed
    assert failure.value.error_code == (
        "FILE_MISSING" if original_failure else "VISION_PROVIDER_CLOSE_FAILED"
    )
    assert len(vision.calls) == (0 if original_failure else 1)
    if original_failure:
        assert "保留原始" in failure.value.__notes__[0]
