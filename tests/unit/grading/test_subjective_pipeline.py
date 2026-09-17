"""T056 主观题评分链路适配测试：装配、决策记录与错误码保真。

TCR（2026-09-16，T056 / B02、B03）：主观题评分器必须把**当次**置信度决策交给调用方，
以便仓储写入决策快照；既有业务错误码与 ``retryable`` 必须原样保留；上下文不足或
Provider 未就绪时显式失败，绝不伪造分数。

TCR（2026-09-17，M04）：显式 ``AppSettings`` 必须贯穿评分 Provider、Embedding、
Hybrid Retriever、Reranker 与置信策略。新增局部配置和全局配置不同的场景，直接断言
实际被调用的是局部配置创建的组件；仅检查工厂参数不足以证明运行链路没有回落到全局配置。
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from backend.app.domain.enums import QuestionType
from backend.app.services.grading import grading_context as grading_context_module
from backend.app.services.grading import subjective_grader as subjective_grader_module
from backend.app.services.grading.grading_context import GradingContextError
from backend.app.services.grading.grading_task_service import (
    GradingTargetAnswer,
    GradingTaskError,
    SubmissionSnapshot,
)
from backend.app.services.grading.subjective_grader import ProviderNotReadyError
from backend.app.services.grading.subjective_pipeline import (
    build_subjective_scorer,
    build_subjective_source,
)
from tests.support.subjective_grading_doubles import (
    StubEmbeddingProvider,
    StubReranker,
    StubRetriever,
    StubScoringProvider,
    make_chunk,
)
from tests.unit.models.sqlite_support import (
    SubmissionFixture,
    create_sqlite_engine,
    seed_submission,
)
from tests.unit.settings_helpers import build_test_settings


@pytest.fixture
def engine() -> Iterator[Engine]:
    """内存 SQLite 引擎；检索替身替代真实向量检索。"""

    active = create_sqlite_engine()
    try:
        yield active
    finally:
        active.dispose()


@pytest.fixture
def fixture(engine: Engine) -> SubmissionFixture:
    """一份含客观题与主观题的最小答卷。"""

    with Session(engine) as session:
        return seed_submission(session)


def _snapshot(fixture: SubmissionFixture) -> SubmissionSnapshot:
    """答卷快照：只保留评分链路需要的字段。"""

    return SubmissionSnapshot(
        submission_id=str(fixture.submission_id),
        exam_id=str(fixture.exam_id),
        student_id=str(fixture.student_id),
        course_id=str(fixture.course_id),
        status="Submitted",
        answers=(_target(fixture),),
    )


def _target(
    fixture: SubmissionFixture,
    *,
    scoring_rubric: str | None = "说明保存和引用数据即可。",
) -> GradingTargetAnswer:
    """主观题评分目标。"""

    return GradingTargetAnswer(
        order=2,
        answer_id=str(fixture.subjective_answer_id),
        question_id=str(fixture.subjective_question_id),
        question_type=QuestionType.SHORT_ANSWER,
        max_score=Decimal("10.00"),
        knowledge_points=("变量",),
        content="解释变量的作用。",
        reference_answer="变量用于保存数据。",
        scoring_rubric=scoring_rubric,
        student_answer="变量用于保存数据。",
    )


def _scorer(
    engine: Engine,
    *,
    provider: StubScoringProvider | None = None,
    retriever: StubRetriever | None = None,
):
    """构造使用真实评分配置与替身外部依赖的主观题评分器。"""

    return build_subjective_scorer(
        session_factory=lambda: Session(engine),
        settings=build_test_settings(confidence_threshold=0.8),
        provider=provider or StubScoringProvider(),
        retriever=retriever if retriever is not None else StubRetriever([make_chunk()]),
        reranker=StubReranker(),
        embedding_provider=StubEmbeddingProvider(),
    )


def test_scorer_returns_result_with_recorded_decision(
    engine: Engine,
    fixture: SubmissionFixture,
) -> None:
    """高置信度评分返回结果与当次决策（阈值来自运行配置）。"""

    provider = StubScoringProvider(score=6.0, confidence=0.9)
    score = _scorer(engine, provider=provider)

    result, decision = score(_snapshot(fixture), _target(fixture))

    assert result.score == 6.0
    assert result.review_status == "Not Required"
    assert result.knowledge_points == ["变量"]
    assert result.retrieved_context_ids == ["chunk-1"]
    assert result.answer_id == str(fixture.subjective_answer_id)
    assert decision.threshold == 0.8
    assert decision.confidence == 0.9
    assert decision.requires_review is False
    assert len(provider.calls) == 1


def test_explicit_settings_select_actual_scoring_components(
    engine: Engine,
    fixture: SubmissionFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """局部配置必须选择并实际调用整条评分链路中的局部组件。"""

    local_settings = build_test_settings(
        deepseek_model="local-score-model",
        embedding_model="local-embedding-model",
        rerank_provider="llm",
        rerank_model="local-rerank-model",
        rerank_max_candidates=3,
        hybrid_vector_weight=0.75,
        confidence_threshold=0.95,
    )
    global_settings = build_test_settings(
        deepseek_model="global-score-model",
        embedding_model="global-embedding-model",
        rerank_provider="llm",
        rerank_model="global-rerank-model",
        rerank_max_candidates=5,
        hybrid_vector_weight=0.2,
        confidence_threshold=0.1,
    )
    local_provider = StubScoringProvider(score=6.0, confidence=0.9)
    global_provider = StubScoringProvider(score=1.0, confidence=0.99)
    local_embedding = StubEmbeddingProvider((0.1, 0.2, 0.3))
    global_embedding = StubEmbeddingProvider((0.9, 0.8, 0.7))
    local_retriever = StubRetriever([make_chunk("local-chunk")])
    global_retriever = StubRetriever([make_chunk("global-chunk")])
    local_reranker = StubReranker()
    global_reranker = StubReranker()

    monkeypatch.setattr(subjective_grader_module, "get_settings", lambda: global_settings)
    monkeypatch.setattr(grading_context_module, "get_settings", lambda: global_settings)

    def create_scoring_provider(settings=None):
        return local_provider if settings is local_settings else global_provider

    def create_embedding_provider(settings=None):
        return local_embedding if settings is local_settings else global_embedding

    def get_retriever(_mode, **kwargs):
        if kwargs == {"candidate_k": 3, "vector_weight": 0.75}:
            return local_retriever
        return global_retriever

    def build_reranker(*, settings=None, **_kwargs):
        return local_reranker if settings is local_settings else global_reranker

    monkeypatch.setattr(
        subjective_grader_module, "create_llm_provider", create_scoring_provider
    )
    monkeypatch.setattr(
        grading_context_module,
        "create_embedding_provider",
        create_embedding_provider,
        raising=False,
    )
    monkeypatch.setattr(
        grading_context_module,
        "get_embedding_provider",
        lambda: global_embedding,
    )
    monkeypatch.setattr(grading_context_module, "get_retriever", get_retriever)
    monkeypatch.setattr(grading_context_module, "build_reranker", build_reranker)

    score = build_subjective_scorer(
        session_factory=lambda: Session(engine),
        settings=local_settings,
    )
    result, decision = score(_snapshot(fixture), _target(fixture))

    assert result.score == 6.0
    assert result.retrieved_context_ids == ["local-chunk"]
    assert result.review_status == "Pending Review"
    assert decision.threshold == 0.95
    assert len(local_provider.calls) == 1
    assert len(local_embedding.queries) == 1
    assert len(local_retriever.calls) == 1
    assert len(local_reranker.calls) == 1
    assert global_provider.calls == []
    assert global_embedding.queries == []
    assert global_retriever.calls == []
    assert global_reranker.calls == []


def test_low_confidence_result_enters_pending_review(
    engine: Engine,
    fixture: SubmissionFixture,
) -> None:
    """低置信度评分返回待复核决策，供仓储写入决策快照。"""

    score = _scorer(engine, provider=StubScoringProvider(confidence=0.5))

    result, decision = score(_snapshot(fixture), _target(fixture))

    assert result.review_status == "Pending Review"
    assert decision.requires_review is True
    assert decision.review_status == "Pending Review"
    assert decision.confidence == 0.5


def test_missing_context_fails_with_business_code_and_no_llm_call(
    engine: Engine,
    fixture: SubmissionFixture,
) -> None:
    """无检索上下文时保留上下文错误码，且不调用评分模型。"""

    provider = StubScoringProvider()
    score = _scorer(engine, provider=provider, retriever=StubRetriever([]))

    with pytest.raises(GradingTaskError) as error:
        score(_snapshot(fixture), _target(fixture))

    assert error.value.error_code == "GRADING_MISSING_CONTEXT"
    assert error.value.retryable is False
    assert provider.calls == []


def test_provider_not_ready_preserves_error_code(
    engine: Engine,
    fixture: SubmissionFixture,
) -> None:
    """Provider 未就绪时保留既有错误码，不降级为通用任务失败。"""

    score = _scorer(
        engine,
        provider=StubScoringProvider(error=ProviderNotReadyError("评分 Provider 未就绪。")),
    )

    with pytest.raises(GradingTaskError) as error:
        score(_snapshot(fixture), _target(fixture))

    assert error.value.error_code == "GRADING_PROVIDER_NOT_READY"
    assert error.value.retryable is False


def test_missing_rubric_is_rejected_before_calling_provider(
    engine: Engine,
    fixture: SubmissionFixture,
) -> None:
    """缺少评分标准时按输入错误码拒绝，且不调用模型。"""

    provider = StubScoringProvider()
    score = _scorer(engine, provider=provider)

    with pytest.raises(GradingTaskError) as error:
        score(_snapshot(fixture), _target(fixture, scoring_rubric=None))

    assert error.value.error_code == "GRADING_INVALID_INPUT"
    assert provider.calls == []


def test_source_builder_rejects_missing_reference_answer(
    fixture: SubmissionFixture,
) -> None:
    """标准答案缺失时构造输入即失败，不用占位文本代替。"""

    target = GradingTargetAnswer(
        order=2,
        answer_id=str(fixture.subjective_answer_id),
        question_id=str(fixture.subjective_question_id),
        question_type=QuestionType.SHORT_ANSWER,
        max_score=Decimal("10.00"),
        knowledge_points=("变量",),
        content="解释变量的作用。",
        reference_answer=None,
        scoring_rubric="说明保存和引用数据即可。",
        student_answer="变量用于保存数据。",
    )

    with pytest.raises(GradingContextError) as error:
        build_subjective_source(_snapshot(fixture), target)

    assert error.value.error_code == "GRADING_MISSING_REFERENCE_ANSWER"
