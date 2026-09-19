"""T069 Grading Agent 阅卷编排的失败优先测试。

任务编号：T069（M4；实现见 `backend/app/ai/agents/grading_agent.py`）。

必要性：`plan.md` §4 要求 Grading Agent“分析学生答案、结合评分标准、调用 RAG”并输出结构化评分
结果；`agent-workflow.md` 规定客观题必须走确定性规则且不得调用 LLM（FR-030）、主观题必须包含
题目/标准答案/评分标准/学生答案与课程检索上下文（FR-031/FR-032）、置信度低于阈值必须进入
Pending Review（FR-035）。若阅卷 Agent 自行实现分数计算、重复构建检索上下文、在客观题上调用
置信度策略或返回身份不匹配的结果，M3 既有评分链路与 T072 的图编排都会失去唯一事实源。

覆盖内容：
1. **只编排不重算**：客观题调用既有 `ObjectiveGrader`，主观题调用既有 `SubjectiveGrader`；
   返回的分数与理由来自既有服务产物，客观题路径不调用主观题评分器；
2. **主观题唯一检索边界**：上下文、Hybrid 检索与 Rerank 由 `SubjectiveGrader` 内部完成，
   调用方只组装 `SubjectiveGradingSource`；Retriever 与 Reranker 各只调用一次；
3. **置信度语义**：客观题不进入置信度复核、不产生决策快照；主观题经当次策略记录真实阈值，
   低置信度得 `pending_review` + `requires_review=True`，高置信度得 `success`；
4. **整卷合同**：`score/score_async` 逐题编排、`ResultAggregator.aggregate()` 只调用一次、
   返回非空 `exam_result`、任一题失败即抛出且不返回部分成功；
5. **错误映射**：上下文不足、Provider 调用失败、结构化失败、缺评分标准、缺会话、身份或
   满分不匹配、未通过结构化校验均映射为脱敏失败（保留来源码与可重试语义）；
6. **异步边界**：事件循环内调用同步入口报 `GRADING_AGENT_ASYNC_REQUIRED`，不嵌套 `asyncio.run`；
7. **追溯与状态映射**：`AgentInvocation` 原样保留 `request_id`/`workflow_id`，空白 ID 被拒；
   槽位补丁可通过 T065 `workflow_state_to_json` 校验。

执行方法（先红后绿）：``python -m pytest tests/unit/agents -q``；实现前本文件因模块缺失而失败。
本仓库测试约定：异步入口在同步用例中用 ``asyncio.run`` 驱动。
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Coroutine
from dataclasses import asdict
from decimal import Decimal
from typing import Any

import pytest

from backend.app.ai.agents.grading_agent import (
    GRADING_AGENT_ASYNC_REQUIRED,
    GRADING_AGENT_MISSING_SESSION,
    GRADING_AGENT_RESULT_MISMATCH,
    GRADING_AGENT_RESULT_NOT_VALIDATED,
    GradingAgent,
    GradingAgentError,
    grading_output_to_state_patch,
)
from backend.app.ai.agents.invocation import (
    AGENT_MISSING_REQUEST_ID,
    AgentInvocation,
    AgentInvocationError,
)
from backend.app.ai.agents.state import AgentStatus, AgentType
from backend.app.ai.workflows.state import ANSWER_SLOT_FIELDS, workflow_state_to_json
from backend.app.core.config import AppSettings
from backend.app.core.retry_policy import ProviderErrorInfo, ProviderExecutionError
from backend.app.domain.enums import QuestionType, ValidationStatus, WorkflowStatus
from backend.app.schemas.ai import GradingResult
from backend.app.services.grading.confidence_policy import (
    ConfidenceDecision,
    ConfidencePolicy,
)
from backend.app.services.grading.grading_task_service import (
    GradingOutcome,
    GradingTargetAnswer,
    SubmissionSnapshot,
)
from backend.app.services.grading.objective_grader import ObjectiveGrader
from backend.app.services.grading.result_aggregator import (
    DuplicateResultError,
    ResultAggregator,
)
from backend.app.services.grading.subjective_grader import (
    SUBJECTIVE_GRADING_PROMPT_VERSION,
    SubjectiveGrader,
    SubjectiveGradingPayload,
)
from tests.support.question_generation_doubles import COURSE_UUID
from tests.support.subjective_grading_doubles import (
    StubEmbeddingProvider,
    StubReranker,
    StubRetriever,
    StubScoringProvider,
    make_chunk,
)
from tests.unit.settings_helpers import build_test_settings


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """在同步用例中驱动异步入口；与仓库既有测试约定保持一致。"""

    return asyncio.run(coroutine)


def _objective_target(
    *,
    answer_id: str = "answer-1",
    order: int = 1,
    student_answer: Any = "True",
    reference_answer: str = "True",
) -> GradingTargetAnswer:
    """构造判断题目标；客观题路径不调用 LLM。"""

    return GradingTargetAnswer(
        order=order,
        answer_id=answer_id,
        question_id=f"question-{order}",
        question_type=QuestionType.TRUE_FALSE,
        max_score=Decimal(2),
        knowledge_points=("变量",),
        content="变量用于保存数据。",
        reference_answer=reference_answer,
        student_answer=student_answer,
    )


def _subjective_target(
    *,
    answer_id: str = "answer-2",
    order: int = 2,
    student_answer: Any = "变量用于保存数据。",
    scoring_rubric: str | None = "要点齐全得 10 分。",
) -> GradingTargetAnswer:
    """构造简答题目标；主观题路径需要检索上下文与结构化输出。"""

    return GradingTargetAnswer(
        order=order,
        answer_id=answer_id,
        question_id=f"question-{order}",
        question_type=QuestionType.SHORT_ANSWER,
        max_score=Decimal(10),
        knowledge_points=("变量",),
        content="请说明变量的作用。",
        reference_answer="变量用于保存数据。",
        scoring_rubric=scoring_rubric,
        student_answer=student_answer,
    )


def _snapshot(*targets: GradingTargetAnswer) -> SubmissionSnapshot:
    """构造答卷只读快照（M3 类型，不新建重复类型）。"""

    return SubmissionSnapshot(
        submission_id="submission-1",
        exam_id="exam-1",
        student_id="student-1",
        course_id=COURSE_UUID,
        status="Submitted",
        answers=tuple(targets),
    )


def _subjective_result(
    target: GradingTargetAnswer,
    snapshot: SubmissionSnapshot,
    *,
    score: float = 6.0,
    confidence: float = 0.9,
    validation_status: str = ValidationStatus.VALIDATED.value,
    answer_id: str | None = None,
) -> GradingResult:
    """构造主观题评分结果替身产物。"""

    return GradingResult(
        question_type=target.question_type,
        score=score,
        max_score=float(target.max_score),
        reason="说明了变量的作用。",
        correct_points=["保存数据"],
        missing_knowledge_points=["引用数据"],
        knowledge_points=list(target.knowledge_points),
        suggestions=["补充变量引用。"],
        confidence=confidence,
        validation_status=validation_status,
        review_status="Not Required",
        answer_id=answer_id if answer_id is not None else target.answer_id,
        submission_id=snapshot.submission_id,
    )


class _RecordingObjectiveGrader:
    """记录调用的客观题评分替身；返回带有可识别数值的既有 DTO。"""

    def __init__(self, result: GradingResult | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._result = result

    def grade(self, **kwargs: Any) -> GradingResult:
        self.calls.append(kwargs)
        if self._result is not None:
            return self._result
        return GradingResult(
            question_type=kwargs["question_type"],
            score=1.5,
            max_score=float(kwargs["max_score"]),
            reason="替身客观题理由。",
            correct_points=["保存数据"],
            missing_knowledge_points=["引用数据"],
            knowledge_points=list(kwargs["knowledge_points"]),
            suggestions=["补充变量引用。"],
            confidence=1.0,
            validation_status=ValidationStatus.VALIDATED.value,
            review_status="Not Required",
            answer_id=kwargs["answer_id"],
            submission_id=kwargs["submission_id"],
        )


class _RecordingSubjectiveGrader:
    """记录调用的异步主观题评分替身。"""

    def __init__(self, result: GradingResult, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = result
        self.error = error

    async def grade(self, session: Any, source: Any, **kwargs: Any) -> GradingResult:
        self.calls.append({"session": session, "source": source, **kwargs})
        if self.error is not None:
            raise self.error
        return self.result


class _FailingProvider:
    """总是抛出 Provider 执行错误的替身。"""

    def __init__(self, code: str, *, retryable: bool, attempt_count: int = 1) -> None:
        self.error = ProviderExecutionError(
            ProviderErrorInfo(
                code=code,
                message="Provider 调用失败。",
                attempt_count=attempt_count,
                retryable=retryable,
                status=code,
            )
        )

    async def generate_structured(self, messages: Any, schema: Any, **kwargs: Any) -> Any:
        raise self.error


class _CountingAggregator:
    """包装真实汇总器并统计调用次数的替身。"""

    def __init__(self) -> None:
        self._inner = ResultAggregator()
        self.calls = 0

    def aggregate(self, context: Any, *, results: Any, decisions: Any = None) -> Any:
        self.calls += 1
        return self._inner.aggregate(context, results=results, decisions=decisions)


class _FakeSession:
    """最小会话替身；只记录是否被关闭。"""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _settings(**overrides: Any) -> AppSettings:
    """构造测试配置；默认阈值高于高置信度分支，可被覆盖。"""

    values: dict[str, Any] = {"confidence_threshold": 0.8}
    values.update(overrides)
    return build_test_settings(**values)


def _agent(**overrides: Any) -> GradingAgent:
    """构造阅卷 Agent；默认注入替身检索、Embedding 与会话工厂。"""

    kwargs: dict[str, Any] = {
        "retriever": StubRetriever([make_chunk("chunk-1")]),
        "reranker": StubReranker(),
        "embedding_provider": StubEmbeddingProvider(),
        "settings": _settings(),
        "session_factory": _FakeSession,
    }
    kwargs.update(overrides)
    return GradingAgent(**kwargs)


def test_objective_answer_uses_deterministic_grader_without_llm() -> None:
    """客观题只走既有确定性规则：不调用主观题评分器，也不生成置信度决策。"""

    objective = _RecordingObjectiveGrader()
    subjective = _RecordingSubjectiveGrader(
        _subjective_result(_subjective_target(), _snapshot(_subjective_target()))
    )
    snapshot = _snapshot(_objective_target())
    invocation = _agent(
        objective_grader=objective,
        subjective_grader=subjective,
    ).grade_answer(snapshot, snapshot.answers[0], request_id="request-1")

    assert isinstance(invocation, AgentInvocation)
    assert invocation.request_id == "request-1"
    assert invocation.workflow_id is None
    output = invocation.output
    assert output.agent_type is AgentType.GRADING
    assert output.status is AgentStatus.SUCCESS
    assert output.requires_review is False
    assert output.confidence_decision is None
    assert output.prompt_version is None
    assert output.model is None
    assert len(objective.calls) == 1
    assert objective.calls[0]["answer_id"] == "answer-1"
    assert subjective.calls == []
    # 分数与理由来自既有服务产物，Agent 不重算。
    assert output.grading_result is not None
    assert output.grading_result.score == 1.5
    assert output.grading_result.reason == "替身客观题理由。"


def test_subjective_answer_orchestrates_existing_grader_once() -> None:
    """主观题只组装来源并调用既有评分器一次；分数与理由原样透传。"""

    target = _subjective_target()
    snapshot = _snapshot(target)
    subjective = _RecordingSubjectiveGrader(
        _subjective_result(target, snapshot, score=6.0, confidence=0.9)
    )
    invocation = _agent(subjective_grader=subjective).grade_answer(
        snapshot,
        target,
        request_id="request-1",
        workflow_id="workflow-1",
    )

    output = invocation.output
    assert invocation.workflow_id == "workflow-1"
    assert output.status is AgentStatus.SUCCESS
    assert output.validation_status is ValidationStatus.VALIDATED
    assert output.prompt_version == SUBJECTIVE_GRADING_PROMPT_VERSION
    assert len(subjective.calls) == 1
    call = subjective.calls[0]
    assert call["source"].answer_id == "answer-2"
    assert call["source"].student_answer == "变量用于保存数据。"
    assert call["max_score"] == Decimal(10)
    assert output.grading_result is not None
    assert output.grading_result.score == 6.0
    assert output.confidence == 0.9
    assert output.confidence_decision is not None
    assert output.confidence_decision.threshold == 0.8
    assert output.confidence_decision.requires_review is False
    assert output.requires_review is False


def test_low_confidence_subjective_answer_pauses_for_review() -> None:
    """低置信度主观题必须进入待人工复核，并带回当次决策快照。"""

    target = _subjective_target()
    snapshot = _snapshot(target)
    subjective = _RecordingSubjectiveGrader(
        _subjective_result(target, snapshot, score=6.0, confidence=0.3)
    )
    output = _agent(subjective_grader=subjective).grade_answer(
        snapshot,
        target,
        request_id="request-1",
    ).output

    assert output.status is AgentStatus.PENDING_REVIEW
    assert output.requires_review is True
    assert output.error is None
    assert output.confidence_decision is not None
    assert output.confidence_decision.confidence == 0.3
    assert output.confidence_decision.review_status == "Pending Review"
    assert output.grading_result is not None
    assert output.grading_result.review_status == "Pending Review"


def test_real_subjective_grader_runs_hybrid_retrieval_once() -> None:
    """真实主观题评分器路径：上下文由评分器独占构建，检索与重排各一次。"""

    target = _subjective_target()
    snapshot = _snapshot(target)
    retriever = StubRetriever([make_chunk("chunk-1")])
    reranker = StubReranker()
    provider = StubScoringProvider(score=8.0, confidence=0.95)
    output = _agent(
        provider=provider,
        retriever=retriever,
        reranker=reranker,
    ).grade_answer(snapshot, target, request_id="request-1").output

    assert output.status is AgentStatus.SUCCESS
    assert len(retriever.calls) == 1
    assert len(reranker.calls) == 1
    assert len(provider.calls) == 1
    assert provider.calls[0]["schema"] is SubjectiveGradingPayload
    assert output.grading_result is not None
    assert output.grading_result.score == 8.0
    assert output.validation_status is ValidationStatus.VALIDATED


def test_insufficient_context_is_mapped_to_desensitized_failure() -> None:
    """检索上下文不足时映射为脱敏失败，不产生任何评分结果。"""

    target = _subjective_target()
    snapshot = _snapshot(target)
    output = _agent(retriever=StubRetriever([])).grade_answer(
        snapshot,
        target,
        request_id="request-1",
    ).output

    assert output.status is AgentStatus.FAILURE
    assert output.error is not None
    assert output.error.error_code == "GRADING_MISSING_CONTEXT"
    assert output.error.retryable is False
    assert output.grading_result is None
    assert output.confidence_decision is None


def test_provider_failures_keep_source_code_and_retryable() -> None:
    """Provider 调用失败与结构化失败必须保留来源码与可重试语义。"""

    target = _subjective_target()
    snapshot = _snapshot(target)

    timeout = _agent(provider=_FailingProvider("ProviderTimeout", retryable=True)).grade_answer(
        snapshot,
        target,
        request_id="request-1",
    ).output
    assert timeout.error is not None
    assert timeout.error.error_code == "GRADING_PROVIDER_FAILED"
    assert timeout.error.retryable is True
    assert timeout.error.source_code == "ProviderTimeout"

    invalid = _agent(
        provider=_FailingProvider("StructuredOutputFailed", retryable=True, attempt_count=2)
    ).grade_answer(snapshot, target, request_id="request-1").output
    assert invalid.error is not None
    assert invalid.error.error_code == "GRADING_INVALID_LLM_RESPONSE"
    assert invalid.error.source_code == "StructuredOutputFailed"
    assert invalid.grading_result is None


def test_missing_rubric_and_missing_session_fail_explicitly() -> None:
    """缺少评分标准或缺会话时必须显式失败，不伪造分数。"""

    without_rubric = _subjective_target(scoring_rubric=None)
    snapshot = _snapshot(without_rubric)
    output = _agent().grade_answer(snapshot, without_rubric, request_id="request-1").output
    assert output.status is AgentStatus.FAILURE
    assert output.error is not None
    assert output.error.error_code == "GRADING_INVALID_INPUT"

    target = _subjective_target()
    no_session = _snapshot(target)
    output = _run(
        GradingAgent(
            retriever=StubRetriever([make_chunk("chunk-1")]),
            reranker=StubReranker(),
            embedding_provider=StubEmbeddingProvider(),
            settings=_settings(),
        )
        .grade_answer_async(
            no_session,
            target,
            request_id="request-1",
            session=None,
        )
    ).output
    assert output.status is AgentStatus.FAILURE
    assert output.error is not None
    assert output.error.error_code == GRADING_AGENT_MISSING_SESSION


def test_result_identity_and_validation_mismatch_fail() -> None:
    """结果身份或校验状态与输入不一致时必须失败，不静默修正。"""

    target = _subjective_target()
    snapshot = _snapshot(target)
    wrong_identity = _RecordingSubjectiveGrader(
        _subjective_result(target, snapshot, answer_id="answer-other")
    )
    output = _agent(subjective_grader=wrong_identity).grade_answer(
        snapshot,
        target,
        request_id="request-1",
    ).output
    assert output.error is not None
    assert output.error.error_code == GRADING_AGENT_RESULT_MISMATCH

    not_validated = _RecordingSubjectiveGrader(
        _subjective_result(target, snapshot, validation_status=ValidationStatus.PENDING.value)
    )
    output = _agent(subjective_grader=not_validated).grade_answer(
        snapshot,
        target,
        request_id="request-1",
    ).output
    assert output.error is not None
    assert output.error.error_code == GRADING_AGENT_RESULT_NOT_VALIDATED


def test_score_aggregates_once_and_never_returns_partial_success() -> None:
    """整卷编排：汇总只调用一次、低置信度不计入总分、任一题失败即抛出。"""

    objective = _objective_target()
    subjective = _subjective_target()
    snapshot = _snapshot(objective, subjective)
    aggregator = _CountingAggregator()
    agent = _agent(
        objective_grader=_RecordingObjectiveGrader(),
        subjective_grader=_RecordingSubjectiveGrader(
            _subjective_result(subjective, snapshot, confidence=0.3)
        ),
        aggregator=aggregator,
        session_factory=_FakeSession,
    )

    outcome = agent.score(snapshot)
    assert isinstance(outcome, GradingOutcome)
    assert aggregator.calls == 1
    assert outcome.exam_result is not None
    assert outcome.exam_result.is_final is False
    counted = {item.answer_id: item.counted for item in outcome.exam_result.items}
    assert counted == {"answer-1": True, "answer-2": False}

    failing = _agent(
        objective_grader=_RecordingObjectiveGrader(),
        subjective_grader=_RecordingSubjectiveGrader(
            _subjective_result(subjective, snapshot),
            error=RuntimeError("主观题评分失败。"),
        ),
        aggregator=_CountingAggregator(),
        session_factory=_FakeSession,
    )
    with pytest.raises(GradingAgentError):
        failing.score(snapshot)


def test_sync_entry_rejects_running_event_loop() -> None:
    """事件循环内不得嵌套 asyncio.run：同步入口必须显式报错。"""

    objective = _objective_target()
    snapshot = _snapshot(objective)
    agent = _agent(objective_grader=_RecordingObjectiveGrader())

    async def _attempt() -> str:
        with pytest.raises(GradingAgentError) as error:
            agent.score(snapshot)
        return str(error.value.error_code)

    assert _run(_attempt()) == GRADING_AGENT_ASYNC_REQUIRED


def test_invocation_requires_request_id() -> None:
    """追溯 ID 不得静默丢弃：空白 request_id 必须被拒绝。"""

    objective = _objective_target()
    snapshot = _snapshot(objective)
    with pytest.raises(AgentInvocationError) as error:
        _agent(objective_grader=_RecordingObjectiveGrader()).grade_answer(
            snapshot,
            objective,
            request_id="   ",
        )
    assert error.value.error_code == AGENT_MISSING_REQUEST_ID


def test_state_patch_is_serializable_and_has_no_human_conclusion() -> None:
    """槽位补丁必须能被 T065 状态校验接受，且不写教师人工结论。"""

    objective = _objective_target()
    subjective = _subjective_target()
    snapshot = _snapshot(objective, subjective)
    accepted = _agent(
        objective_grader=_RecordingObjectiveGrader(),
        subjective_grader=_RecordingSubjectiveGrader(_subjective_result(subjective, snapshot)),
    ).grade_answer(snapshot, subjective, request_id="request-1")
    pending = _agent(
        objective_grader=_RecordingObjectiveGrader(),
        subjective_grader=_RecordingSubjectiveGrader(
            _subjective_result(subjective, snapshot, confidence=0.3)
        ),
    ).grade_answer(snapshot, subjective, request_id="request-1")

    identity = {
        "workflow_id": "workflow-1",
        "request_id": "request-1",
        "submission_id": "submission-1",
    }
    accepted_patch = grading_output_to_state_patch(accepted, current_answer_order=2)
    pending_patch = grading_output_to_state_patch(pending, current_answer_order=2)

    allowed = set(ANSWER_SLOT_FIELDS) | {"pause_reason", "resumable"}
    assert set(accepted_patch) <= allowed
    assert set(pending_patch) <= allowed
    assert pending_patch["pause_reason"].strip()
    assert accepted_patch["current_answer_id"] == "answer-2"
    assert accepted_patch["validation_status"] == ValidationStatus.VALIDATED

    workflow_state_to_json({**identity, **accepted_patch, "status": WorkflowStatus.RUNNING})
    workflow_state_to_json({**identity, **pending_patch, "status": WorkflowStatus.PAUSED})
    # 自动决策可以写入复核状态，但绝不能是教师人工结论。
    assert accepted_patch["review_status"] not in {"Confirmed", "Modified", "Final", "Re-grade"}
    assert pending_patch["review_status"] == "Pending Review"


def test_objective_and_subjective_use_same_settings_for_confidence() -> None:
    """同一个 AppSettings 决定阈值：局部配置覆盖全局配置。"""

    target = _subjective_target()
    snapshot = _snapshot(target)
    subjective = _RecordingSubjectiveGrader(_subjective_result(target, snapshot, confidence=0.5))
    strict = _agent(
        subjective_grader=subjective,
        settings=_settings(confidence_threshold=0.9),
    ).grade_answer(snapshot, target, request_id="request-1").output
    lenient = _agent(
        subjective_grader=_RecordingSubjectiveGrader(
            _subjective_result(target, snapshot, confidence=0.5)
        ),
        settings=_settings(confidence_threshold=0.2),
    ).grade_answer(snapshot, target, request_id="request-1").output

    assert strict.confidence_decision is not None
    assert strict.confidence_decision.threshold == 0.9
    assert strict.status is AgentStatus.PENDING_REVIEW
    assert lenient.confidence_decision is not None
    assert lenient.confidence_decision.threshold == 0.2
    assert lenient.status is AgentStatus.SUCCESS


def test_per_answer_entry_scores_each_answer_exactly_once() -> None:
    """H01：逐题入口是 Graph 合同——每题一次调用、答案标识与结果一致，且不触发整卷汇总。

    TCR：
    - 必要性：`score_async` 是整卷兼容入口，若不固定逐题合同，T072 节点可能在整卷编排之上再自行
      逐题循环，导致同一题被评分两次；
    - 契约依据：`plan.md` §5 的逐题迭代（Next Answer）与 `.specify/contracts/agent-workflow.md`，
      以及 H01 要求“每个答案只被评分一次、`current_answer_id` 与结果 ID 一致”；
    - 覆盖行为：按题循环调用 `grade_answer_async` 时，客观/主观评分服务各被调用一次、题数一致；
      逐题入口不调用 `ResultAggregator`；返回结果的答案标识与入参题目标识逐题一致。
    """

    objective = _objective_target()
    subjective = _subjective_target()
    snapshot = _snapshot(objective, subjective)
    objective_grader = _RecordingObjectiveGrader()
    subjective_grader = _RecordingSubjectiveGrader(_subjective_result(subjective, snapshot))
    aggregator = _CountingAggregator()
    agent = _agent(
        objective_grader=objective_grader,
        subjective_grader=subjective_grader,
        aggregator=aggregator,
    )

    async def _walk() -> list[Any]:
        return [
            await agent.grade_answer_async(
                snapshot,
                target,
                request_id="request-1",
                workflow_id="workflow-1",
            )
            for target in snapshot.answers
        ]

    invocations = _run(_walk())

    assert len(objective_grader.calls) == 1
    assert len(subjective_grader.calls) == 1
    # 逐题入口不验收整卷：汇总与统一结果属 T072 的 `unified_result` 节点职责。
    assert aggregator.calls == 0
    assert [invocation.workflow_id for invocation in invocations] == ["workflow-1"] * 2
    for target, invocation in zip(snapshot.answers, invocations, strict=True):
        assert invocation.output.grading_result is not None
        assert invocation.output.grading_result.answer_id == target.answer_id


def test_duplicate_answer_ids_never_produce_partial_success() -> None:
    """H01：整卷入口遇到重复答案标识时不得返回部分成功（复用 M3 既有去重规则）。"""

    objective = _objective_target()
    repeated = _snapshot(objective, objective)
    agent = _agent(
        objective_grader=_RecordingObjectiveGrader(),
        aggregator=_CountingAggregator(),
    )

    with pytest.raises(DuplicateResultError):
        agent.score(repeated)


def test_local_settings_win_over_global_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """H02：注入的局部配置决定阈值，且不读取全局配置（M04 配置贯穿的延续）。

    TCR：
    - 必要性：T069 的四个入口都接受显式 `settings`，若任一层回退到全局 `get_settings()`，
      M04 的“局部配置与全局配置不一致”缺陷会重现；
    - 契约依据：`plan.md` §4 Agent 分工的配置贯穿要求与 H02（同一 `AppSettings` 贯穿
      ConfidencePolicy 与评分器）；
    - 覆盖行为：把全局配置工厂替换为“被调用即记录”的替身，注入阈值 0.9 而全局为 0.1 时，
      决策快照阈值为 0.9（进入 Pending Review），且全局工厂一次也未被访问。
    """

    calls: list[str] = []

    def _global_settings() -> AppSettings:
        calls.append("get_settings")
        return _settings(confidence_threshold=0.1)

    monkeypatch.setattr(
        "backend.app.services.grading.confidence_policy.get_settings",
        _global_settings,
    )
    target = _subjective_target()
    snapshot = _snapshot(target)
    subjective = _RecordingSubjectiveGrader(_subjective_result(target, snapshot, confidence=0.5))
    output = _agent(
        subjective_grader=subjective,
        settings=_settings(confidence_threshold=0.9),
    ).grade_answer(snapshot, target, request_id="request-1").output

    assert calls == []
    assert output.confidence_decision is not None
    assert output.confidence_decision.threshold == 0.9
    assert output.status is AgentStatus.PENDING_REVIEW


def test_per_call_settings_override_constructor_settings() -> None:
    """H02：逐次 `settings` 优先于构造期配置，且不改变构造期配置（不传时仍用构造期值）。"""

    target = _subjective_target()
    snapshot = _snapshot(target)
    agent = _agent(
        subjective_grader=_RecordingSubjectiveGrader(
            _subjective_result(target, snapshot, confidence=0.5)
        ),
        settings=_settings(confidence_threshold=0.2),
    )

    strict = agent.grade_answer(
        snapshot,
        target,
        request_id="request-1",
        settings=_settings(confidence_threshold=0.9),
    ).output
    lenient = agent.grade_answer(snapshot, target, request_id="request-1").output

    assert strict.confidence_decision is not None
    assert strict.confidence_decision.threshold == 0.9
    assert strict.status is AgentStatus.PENDING_REVIEW
    assert lenient.confidence_decision is not None
    assert lenient.confidence_decision.threshold == 0.2
    assert lenient.status is AgentStatus.SUCCESS


def test_settings_reach_every_resolved_component_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """H02：未注入的组件由同一 `AppSettings` 解析，且检索与重排各一次、不重复装配。

    验证链路：注入的评分器替身不参与本用例；Provider/Embedding/Reranker 走 M3 既有工厂，
    工厂收到的必须是同一个 `AppSettings` 实例（`is` 断言），并被真实调用一次。
    """

    from backend.app.services.grading import grading_context
    from backend.app.services.grading import (
        subjective_grader as subjective_grader_module,
    )

    settings = _settings(confidence_threshold=0.8)
    embedding = StubEmbeddingProvider()
    reranker = StubReranker()
    provider = StubScoringProvider(score=8.0, confidence=0.95)
    seen: dict[str, AppSettings | None] = {}

    def _embedding_factory(passed: AppSettings | None) -> StubEmbeddingProvider:
        seen["embedding"] = passed
        return embedding

    def _reranker_factory(*, settings: AppSettings | None = None) -> StubReranker:
        seen["reranker"] = settings
        return reranker

    def _provider_factory(passed: AppSettings | None = None) -> StubScoringProvider:
        seen["provider"] = passed
        return provider

    monkeypatch.setattr(grading_context, "create_embedding_provider", _embedding_factory)
    monkeypatch.setattr(grading_context, "build_reranker", _reranker_factory)
    monkeypatch.setattr(subjective_grader_module, "create_llm_provider", _provider_factory)

    target = _subjective_target()
    snapshot = _snapshot(target)
    retriever = StubRetriever([make_chunk("chunk-1")])
    output = _agent(
        retriever=retriever,
        reranker=None,
        embedding_provider=None,
        provider=None,
        settings=settings,
    ).grade_answer(snapshot, target, request_id="request-1").output

    assert output.status is AgentStatus.SUCCESS
    assert output.grading_result is not None
    assert output.grading_result.score == 8.0
    # 三个工厂都只收到同一个 AppSettings 实例，且各被调用一次。
    assert seen == {"embedding": settings, "reranker": settings, "provider": settings}
    assert all(value is settings for value in seen.values())
    assert len(retriever.calls) == 1
    assert len(reranker.calls) == 1
    assert len(provider.calls) == 1
    assert provider.calls[0]["schema"] is SubjectiveGradingPayload


def test_real_services_satisfy_agent_orchestration_contract() -> None:
    """真实服务满足 Agent 编排契约：客观题复用既有评分器，主观题接受检索与会话关键字。

    TCR：
    - 必要性：只编排不重算的结论必须有可执行证据，不能只靠源码字符串断言；
    - 契约依据：`plan.md` §4 Agent 分工、`.specify/contracts/agent-workflow.md`、B01/B02
      （主观题上下文由 `SubjectiveGrader` 独占构建，Agent 只传同一个 `AppSettings` 与检索组件）；
    - 覆盖行为：客观题输出与真实 `ObjectiveGrader` 的确定性结果逐项一致（未重算）；
      真实 `SubjectiveGrader.grade` 为异步入口且接受 Agent 传入的 `max_score`/`top_k`/
      `retriever`/`reranker`/`embedding_provider`/`settings`。
    """

    subjective_grade = inspect.signature(SubjectiveGrader.grade)
    assert inspect.iscoroutinefunction(SubjectiveGrader.grade)
    for keyword in (
        "max_score",
        "top_k",
        "retriever",
        "reranker",
        "embedding_provider",
        "settings",
    ):
        assert keyword in subjective_grade.parameters, keyword

    target = _objective_target()
    snapshot = _snapshot(target)
    expected = ObjectiveGrader().grade(
        question_type=target.question_type,
        reference_answer=target.reference_answer,
        student_answer=target.student_answer,
        max_score=float(target.max_score),
        knowledge_points=target.knowledge_points,
        answer_id=target.answer_id,
        submission_id=snapshot.submission_id,
    )
    output = _agent(objective_grader=ObjectiveGrader()).grade_answer(
        snapshot,
        target,
        request_id="request-1",
    ).output

    assert output.grading_result is not None
    assert output.grading_result.score == expected.score
    assert output.grading_result.reason == expected.reason


def test_agent_module_does_not_reimplement_grading_arithmetic() -> None:
    """阅卷 Agent 只编排：不引入分数舍入与比例计算等评分逻辑。"""

    from pathlib import Path

    import backend.app.ai.agents.grading_agent as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "quantize" not in source
    assert "ROUND_HALF_UP" not in source
    assert "ObjectiveGrader" in source
    assert "SubjectiveGrader" in source
    assert "ResultAggregator" in source


def test_confidence_decision_snapshot_reuses_m3_facts() -> None:
    """决策快照复用 M3 事实（阈值与复核结论），不重新判定阈值。"""

    target = _subjective_target()
    snapshot = _snapshot(target)
    subjective = _RecordingSubjectiveGrader(_subjective_result(target, snapshot, confidence=0.9))
    output = _agent(subjective_grader=subjective).grade_answer(
        snapshot,
        target,
        request_id="request-1",
    ).output

    decision = ConfidenceDecision(
        confidence=0.9,
        threshold=0.8,
        requires_review=False,
        review_status="Not Required",
        grading_status="Accepted",
        reason="置信度达标。",
    )
    snapshot_dto = output.confidence_decision
    assert snapshot_dto is not None
    assert (
        snapshot_dto.confidence,
        snapshot_dto.threshold,
        snapshot_dto.requires_review,
        snapshot_dto.review_status,
    ) == (
        decision.confidence,
        decision.threshold,
        decision.requires_review,
        decision.review_status,
    )


@pytest.mark.parametrize(
    ("confidence", "expected_status"),
    [(0.799, AgentStatus.PENDING_REVIEW), (0.8, AgentStatus.SUCCESS),
     (0.95, AgentStatus.SUCCESS)],
)
def test_default_grader_reuses_single_decision(
    monkeypatch: pytest.MonkeyPatch, confidence: float, expected_status: AgentStatus,
) -> None:
    """真实默认评分器一次判定并回填，Agent 原样使用结果和六字段决策。"""

    decisions: list[ConfidenceDecision] = []
    graded_results: list[GradingResult] = []
    original_evaluate = ConfidencePolicy.evaluate
    original_grade = SubjectiveGrader.grade

    def observe_evaluate(policy, value, *, question_type=None):
        decision = original_evaluate(policy, value, question_type=question_type)
        decisions.append(decision)
        return decision

    async def observe_grade(grader, *args, **kwargs):
        result = await original_grade(grader, *args, **kwargs)
        graded_results.append(result)
        return result

    def reject_global_settings():
        raise AssertionError("显式配置存在时不得读取全局置信度配置。")

    monkeypatch.setattr(ConfidencePolicy, "evaluate", observe_evaluate)
    monkeypatch.setattr(SubjectiveGrader, "grade", observe_grade)
    monkeypatch.setattr(
        "backend.app.services.grading.confidence_policy.get_settings",
        reject_global_settings,
    )
    target = _subjective_target()
    snapshot = _snapshot(target)
    provider = StubScoringProvider(score=8.0, confidence=confidence)
    agent = _agent(provider=provider, settings=_settings(confidence_threshold=0.99))

    output = agent.grade_answer(
        snapshot, target, request_id="request-single-decision",
        settings=_settings(confidence_threshold=0.8),
    ).output

    assert output.error is None
    assert output.status is expected_status
    assert len(decisions) == len(graded_results) == len(provider.calls) == 1
    assert output.grading_result is graded_results[0]
    assert output.confidence_decision is not None
    assert output.confidence_decision.model_dump() == asdict(decisions[0])
    assert decisions[0].threshold == 0.8
    assert graded_results[0].review_status == decisions[0].review_status
    assert output.requires_review is decisions[0].requires_review


@pytest.mark.parametrize("custom_grader", [False, True])
def test_agent_preserves_injected_policy(custom_grader: bool) -> None:
    """注入策略仍执行原覆盖逻辑，自定义评分器仍经过 Agent 末端确认。"""

    events: list[str] = []
    marker = "（自定义策略）"

    class CustomPolicy(ConfidencePolicy):
        def evaluate(self, value, *, question_type=None):
            events.append("evaluate")
            return super().evaluate(value, question_type=question_type)

        def apply(self, result):
            events.append("apply")
            updated = super().apply(result)
            return updated.model_copy(update={"reason": updated.reason + marker})

    target = _subjective_target()
    snapshot = _snapshot(target)
    subjective = (
        _RecordingSubjectiveGrader(_subjective_result(target, snapshot, confidence=0.9))
        if custom_grader else None
    )
    output = _agent(
        subjective_grader=subjective,
        policy=CustomPolicy(threshold=0.95),
        provider=StubScoringProvider(confidence=0.9),
        settings=_settings(confidence_threshold=0.1),
    ).grade_answer(snapshot, target, request_id="request-injected-policy").output

    assert output.error is None
    assert output.status is AgentStatus.PENDING_REVIEW
    assert output.confidence_decision is not None
    assert output.confidence_decision.threshold == 0.95
    assert output.grading_result is not None
    assert output.grading_result.reason.count(marker) == (1 if custom_grader else 2)
    if custom_grader:
        assert subjective is not None
        assert len(subjective.calls) == 1
        assert events == ["evaluate", "apply", "evaluate"]
    else:
        assert events == ["apply", "evaluate", "evaluate", "apply", "evaluate"]
