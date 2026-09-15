"""T053 可配置置信度阈值与 Pending Review 决策失败优先测试。

任务编号：T053（M3 AI 阅卷；实现见 `backend/app/services/grading/confidence_policy.py`）。
必要性：FR-035/FR-036 要求每个主观题评分结果都执行 Confidence Check，低置信度结果必须进入
`Pending Review`；data-model 的 GradingResult 状态图同时规定客观题直接进入接受路径，以及
`Confirmed`/`Modified`/`Final` 等人工状态不得被自动流程覆盖。若阈值硬编码、检查可被跳过，
或 `apply` 覆盖人工复核状态，低置信度结果就可能绕过人工复核直接进入成绩与诊断。
覆盖内容：
1. 阈值默认来自 `AppSettings.confidence_threshold`（测试以替换 settings 获取函数的方式验证，
   不依赖本机 `.env`），显式阈值优先；
2. 决策边界：`confidence == threshold` 接受，低于阈值进入 `Pending Review`，并携带
   `ReviewStatus`/`GradingStatus` 语义；
3. Objective 题型按状态图直通接受；
4. `apply` 只回填 `review_status`，分数、知识点与溯源字段不变；拒绝非 `Validated` 输入；
   拒绝覆盖人工复核状态（`Confirmed`/`Modified`/`Final`/`Re-grade`）；
5. 非法阈值与非法置信度显式失败，不静默取默认值；
6. 未注入策略时，正式评分入口仍执行置信度检查（默认接线）。
执行方法（先红后绿）：``python -m pytest tests/unit/grading/test_confidence_policy.py -q``；
仓库未安装 pytest-asyncio，异步用例沿用 `asyncio.run` 包装。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal
from functools import wraps
from typing import Any

import pytest

from backend.app.ai.embedding.base import BaseEmbeddingProvider
from backend.app.ai.llm.base import BaseLLMProvider, LLMMessages
from backend.app.ai.retrieval.base import (
    DEFAULT_TOP_K,
    BaseRetriever,
    RetrievalFilters,
    RetrievalMode,
    RetrievedChunk,
)
from backend.app.ai.retrieval.reranker import BaseReranker
from backend.app.domain.enums import GradingStatus, QuestionType, ReviewStatus
from backend.app.schemas.ai import GradingResult
from backend.app.services.grading import confidence_policy as confidence_policy_module
from backend.app.services.grading.confidence_policy import (
    GRADING_INVALID_CONFIDENCE,
    GRADING_INVALID_CONFIDENCE_THRESHOLD,
    GRADING_MANUAL_REVIEW_STATE,
    GRADING_RESULT_NOT_VALIDATED,
    ConfidenceDecision,
    ConfidencePolicy,
    ConfidencePolicyError,
    InvalidConfidenceError,
    InvalidConfidenceThresholdError,
    ManualReviewStateError,
    NotValidatedResultError,
)
from backend.app.services.grading.grading_context import SubjectiveGradingSource
from backend.app.services.grading.subjective_grader import SubjectiveGrader
from tests.unit.settings_helpers import build_test_settings

COURSE_ID = "3f1a8c2e-0d4f-4b7a-9c1e-2b8d5f6a7c90"
SESSION = object()
HUMAN_REVIEW_STATES = (
    ReviewStatus.CONFIRMED,
    ReviewStatus.MODIFIED,
    ReviewStatus.FINAL,
    ReviewStatus.RE_GRADE,
)


def async_test(function: Any) -> Any:
    @wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(function(*args, **kwargs))

    return wrapper


def _result(
    *,
    confidence: float = 0.9,
    question_type: QuestionType = QuestionType.SHORT_ANSWER,
    validation_status: str = "Validated",
    review_status: str | None = None,
) -> GradingResult:
    """构造一条主观题评分结果。"""

    return GradingResult(
        question_type=question_type,
        score=6.0,
        max_score=10.0,
        reason="结构化评分理由。",
        correct_points=["局部作用域"],
        missing_knowledge_points=["全局作用域"],
        knowledge_points=["变量作用域"],
        suggestions=["补充全局作用域。"],
        confidence=confidence,
        validation_status=validation_status,
        review_status=review_status or ReviewStatus.NOT_REQUIRED.value,
        retrieved_context_ids=["chunk-1"],
        answer_id="3f1a8c2e-0d4f-4b7a-9c1e-2b8d5f6a7c91",
    )


def _policy(**overrides: Any) -> ConfidencePolicy:
    settings = build_test_settings(confidence_threshold=0.8)
    return ConfidencePolicy(settings=settings, **overrides)


# --- 阈值来源与校验 ---


def test_threshold_defaults_to_settings_value() -> None:
    """默认阈值来自 AppSettings，而不是硬编码常量。"""

    policy = ConfidencePolicy(settings=build_test_settings(confidence_threshold=0.62))

    assert policy.threshold == 0.62


def test_threshold_falls_back_to_runtime_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未显式传入 settings 时读取运行时配置；测试替换使用处的获取函数。"""

    monkeypatch.setattr(
        confidence_policy_module,
        "get_settings",
        lambda: build_test_settings(confidence_threshold=0.25),
    )

    assert ConfidencePolicy().threshold == 0.25


def test_explicit_threshold_wins_over_settings() -> None:
    """显式阈值优先于配置值。"""

    policy = ConfidencePolicy(0.3, settings=build_test_settings(confidence_threshold=0.9))

    assert policy.threshold == 0.3


@pytest.mark.parametrize(
    "threshold",
    [float("nan"), float("inf"), float("-inf"), -0.1, 1.5, "0.8", True, Decimal("sNaN"), 10**400],
)
def test_invalid_threshold_is_rejected(threshold: Any) -> None:
    """非法阈值必须显式失败，不得静默回退到配置默认值。"""

    with pytest.raises(InvalidConfidenceThresholdError) as excinfo:
        ConfidencePolicy(threshold, settings=build_test_settings(confidence_threshold=0.8))

    assert excinfo.value.error_code == GRADING_INVALID_CONFIDENCE_THRESHOLD
    assert not isinstance(excinfo.value, (ValueError, OverflowError))


@pytest.mark.parametrize(
    "confidence",
    [float("nan"), float("inf"), float("-inf"), -0.01, 1.01, "0.5", True, None],
)
def test_invalid_confidence_is_rejected(confidence: Any) -> None:
    """非法置信度必须显式失败。"""

    with pytest.raises(InvalidConfidenceError) as excinfo:
        _policy().evaluate(confidence)

    assert excinfo.value.error_code == GRADING_INVALID_CONFIDENCE
    assert isinstance(excinfo.value, ConfidencePolicyError)


# --- 决策边界 ---


def test_confidence_equal_to_threshold_is_accepted() -> None:
    """置信度等于阈值视为达标。"""

    decision = _policy().evaluate(0.8)

    assert isinstance(decision, ConfidenceDecision)
    assert decision.requires_review is False
    assert decision.review_status == ReviewStatus.NOT_REQUIRED.value
    assert decision.grading_status == GradingStatus.ACCEPTED.value
    assert decision.confidence == 0.8
    assert decision.threshold == 0.8
    assert decision.reason


def test_confidence_below_threshold_requires_review() -> None:
    """低于阈值必须进入待人工复核。"""

    decision = _policy().evaluate(0.799)

    assert decision.requires_review is True
    assert decision.review_status == ReviewStatus.PENDING_REVIEW.value
    assert decision.grading_status == GradingStatus.PENDING_REVIEW.value


@pytest.mark.parametrize("confidence", [0.0, 0.2, 0.499999])
def test_zero_threshold_accepts_every_confidence(confidence: float) -> None:
    """阈值下界：阈值为 0 时任何合法置信度都被接受。"""

    decision = ConfidencePolicy(
        0.0, settings=build_test_settings(confidence_threshold=0.8)
    ).evaluate(confidence)

    assert decision.requires_review is False


@pytest.mark.parametrize("confidence", [0.0, 0.999999])
def test_threshold_one_requires_certainty(confidence: float) -> None:
    """阈值上界：阈值为 1 时只有满分置信度被接受。"""

    policy = ConfidencePolicy(1.0, settings=build_test_settings(confidence_threshold=0.8))

    assert policy.evaluate(confidence).requires_review is True
    assert policy.evaluate(1.0).requires_review is False


def test_objective_question_type_bypasses_threshold() -> None:
    """客观题按 data-model 状态图直接接受，不参与主观题置信度复核。"""

    decision = _policy().evaluate(0.01, question_type=QuestionType.SINGLE_CHOICE)

    assert decision.requires_review is False
    assert decision.review_status == ReviewStatus.NOT_REQUIRED.value
    assert "客观题" in decision.reason


# --- apply 适用范围 ---


def test_apply_marks_low_confidence_result_for_review() -> None:
    """apply 只回填复核状态，其它字段保持不变。"""

    result = _result(confidence=0.3)

    updated = _policy().apply(result)

    assert updated.review_status == ReviewStatus.PENDING_REVIEW.value
    assert updated.validation_status == "Validated"
    assert updated.score == result.score
    assert updated.confidence == result.confidence
    assert updated.correct_points == result.correct_points
    assert updated.missing_knowledge_points == result.missing_knowledge_points
    assert updated.knowledge_points == result.knowledge_points
    assert updated.retrieved_context_ids == result.retrieved_context_ids
    assert updated.answer_id == result.answer_id
    assert result.review_status == ReviewStatus.NOT_REQUIRED.value


def test_apply_keeps_high_confidence_result_accepted() -> None:
    """高置信度结果保持自动接受状态。"""

    updated = _policy().apply(_result(confidence=0.95))

    assert updated.review_status == ReviewStatus.NOT_REQUIRED.value


def test_apply_accepts_objective_result_with_low_confidence() -> None:
    """客观题结果即使置信度低也不得被标记为待复核。"""

    updated = _policy().apply(
        _result(confidence=0.05, question_type=QuestionType.TRUE_FALSE)
    )

    assert updated.review_status == ReviewStatus.NOT_REQUIRED.value


def test_apply_rejects_not_validated_result() -> None:
    """未通过结构化校验的结果不得进入接受或待复核决策。"""

    result = _result(confidence=0.3, validation_status="Failed")

    with pytest.raises(NotValidatedResultError) as excinfo:
        _policy().apply(result)

    assert excinfo.value.error_code == GRADING_RESULT_NOT_VALIDATED


@pytest.mark.parametrize("review_status", [state.value for state in HUMAN_REVIEW_STATES])
def test_apply_rejects_manual_review_states(review_status: str) -> None:
    """不得用自动策略覆盖人工复核状态。"""

    result = _result(confidence=0.3, review_status=review_status)

    with pytest.raises(ManualReviewStateError) as excinfo:
        _policy().apply(result)

    assert excinfo.value.error_code == GRADING_MANUAL_REVIEW_STATE
    assert result.review_status == review_status


def test_apply_returns_new_object() -> None:
    """apply 不得原地修改输入对象。"""

    result = _result(confidence=0.3)

    updated = _policy().apply(result)

    assert updated is not result
    assert updated.model_dump()["score"] == result.model_dump()["score"]


# --- 默认接线：正式评分入口必执行置信度检查 ---


class StubEmbeddingProvider(BaseEmbeddingProvider):
    provider_name = "stub"
    model_name = "stub-model"
    dimension = 3

    async def embed_documents(self, documents: Sequence[str]) -> list[list[float]]:
        raise NotImplementedError("替身不提供文档向量。")

    async def embed_query(self, query: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class StubRetriever(BaseRetriever):
    mode = RetrievalMode.HYBRID

    def __init__(self, hits: Sequence[RetrievedChunk]) -> None:
        self._hits = list(hits)

    def search(
        self,
        session: Any,
        query: Any,
        *,
        top_k: int = DEFAULT_TOP_K,
        filters: RetrievalFilters | None = None,
    ) -> list[RetrievedChunk]:
        return list(self._hits)


class StubReranker(BaseReranker):
    provider_name = "stub"

    def rerank(
        self,
        query: str,
        candidates: Sequence[RetrievedChunk],
        top_k: int = DEFAULT_TOP_K,
    ) -> list[RetrievedChunk]:
        ordered = sorted(candidates, key=lambda item: item.score, reverse=True)
        return [replace(chunk, rank=index) for index, chunk in enumerate(ordered[:top_k])]


class FakeGradingProvider(BaseLLMProvider):
    provider_name = "fake"

    def __init__(self, confidence: float) -> None:
        self._confidence = confidence

    async def generate_structured(
        self,
        messages: LLMMessages,
        schema: type[Any],
        model: str | None = None,
    ) -> Any:
        return schema.model_validate(
            {
                "score": 6.0,
                "confidence": self._confidence,
                "reason": "结构化评分理由。",
                "correct_points": ["局部作用域"],
                "missing_knowledge_points": ["全局作用域"],
                "suggestions": ["补充全局作用域。"],
            }
        )


def _source() -> SubjectiveGradingSource:
    return SubjectiveGradingSource(
        question_type=QuestionType.SHORT_ANSWER,
        course_id=COURSE_ID,
        question_content="请说明变量作用域的区别。",
        reference_answer="局部作用域只在函数内可见。",
        student_answer="变量在函数内部定义时只在函数内可见。",
        scoring_rubric="答出局部与全局各得一半分值。",
        knowledge_points=("变量作用域",),
    )


def _grade(confidence: float, *, settings: Any = None) -> GradingResult:
    grader = SubjectiveGrader(provider=FakeGradingProvider(confidence))
    hit = RetrievedChunk(
        chunk_id="chunk-1",
        course_id=COURSE_ID,
        document_id="document-1",
        content="课程片段内容。",
        fusion_score=1.0,
    )
    return asyncio.run(
        grader.grade(
            SESSION,
            _source(),
            max_score=10.0,
            retriever=StubRetriever([hit]),
            reranker=StubReranker(),
            embedding_provider=StubEmbeddingProvider(),
            settings=settings
            if settings is not None
            else build_test_settings(confidence_threshold=0.8),
        )
    )


def test_entry_applies_default_policy_without_injection() -> None:
    """未注入策略时，正式评分入口仍执行置信度检查。"""

    result = _grade(0.2, settings=build_test_settings(confidence_threshold=0.8))

    assert result.review_status == ReviewStatus.PENDING_REVIEW.value
    assert result.validation_status == "Validated"


def test_entry_accepts_result_meeting_threshold() -> None:
    """达到阈值的结果保持自动接受。"""

    result = _grade(0.85, settings=build_test_settings(confidence_threshold=0.8))

    assert result.review_status == ReviewStatus.NOT_REQUIRED.value


def test_entry_respects_configured_threshold() -> None:
    """阈值可配置：同一置信度在不同阈值下得到不同决策。"""

    strict = _grade(0.5, settings=build_test_settings(confidence_threshold=0.8))
    loose = _grade(0.5, settings=build_test_settings(confidence_threshold=0.3))

    assert strict.review_status == ReviewStatus.PENDING_REVIEW.value
    assert loose.review_status == ReviewStatus.NOT_REQUIRED.value


def test_entry_honours_injected_policy() -> None:
    """注入策略时由策略决定复核状态。"""

    class AlwaysReviewPolicy:
        def apply(self, result: GradingResult) -> GradingResult:
            return result.model_copy(
                update={"review_status": ReviewStatus.PENDING_REVIEW.value}
            )

    grader = SubjectiveGrader(
        provider=FakeGradingProvider(0.99),
        policy=AlwaysReviewPolicy(),
    )
    hit = RetrievedChunk(
        chunk_id="chunk-1",
        course_id=COURSE_ID,
        document_id="document-1",
        content="课程片段内容。",
        fusion_score=1.0,
    )

    result = asyncio.run(
        grader.grade(
            SESSION,
            _source(),
            max_score=10.0,
            retriever=StubRetriever([hit]),
            reranker=StubReranker(),
            embedding_provider=StubEmbeddingProvider(),
            settings=build_test_settings(confidence_threshold=0.1),
        )
    )

    assert result.review_status == ReviewStatus.PENDING_REVIEW.value
