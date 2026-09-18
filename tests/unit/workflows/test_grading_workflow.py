"""T071 阅卷工作流路由与状态交接的失败优先测试。

TCR（测试契约记录）：

- 必要性：T072 的 LangGraph 图不得在整卷编排之上再自行逐题循环、不得让待复核结果进入最终成绩，
  也不得在恢复时丢失原 `workflow_id`/`request_id`；这些约束必须在写图之前用可执行断言固定。
- 契约依据：`.specify/plan.md` §5 状态图与控制逻辑、`.specify/contracts/agent-workflow.md`
  （Grading Workflow States / Required State / Contract Rules）、FR-029/FR-030（题型分流与客观题
  确定性）、FR-035/FR-036（阈值、待复核与教师确认）、FR-038（诊断仅消费已确认结果），
  以及评审 H01–H04。
- 覆盖行为：节点顺序与条件边；逐题评分一次且汇总一次；settings 贯穿；Reviewer 不变量；
  失败态不伪装空结果；低置信度暂停并可恢复；`regrade` 按原标识恢复；只有接受或教师复核后的
  结果才能进入 `final_results` 与诊断门槛。
"""

from __future__ import annotations

from collections.abc import Coroutine
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from backend.app.ai.agents.invocation import AgentInvocation
from backend.app.ai.agents.reviewer_agent import ReviewerAgent
from backend.app.ai.agents.state import (
    AgentInput,
    AgentOutput,
    AgentStatus,
    AgentType,
    ReviewDecision,
    SupervisorAction,
)
from backend.app.ai.agents.supervisor import AgentTaskKind, SupervisorAgent
from backend.app.ai.workflows.grading_handoff import (
    ACCEPTED_REVIEW_STATES,
    CLASSIFY_EDGES,
    GENERATE_DIAGNOSIS,
    LOAD_SUBMISSION,
    NODE_LABELS,
    PENDING_REVIEW,
    REGRADE,
    UNIFIED_RESULT,
    WORKFLOW_NODE_ORDER,
    diagnosis_allowed,
    grading_handoff,
    may_enter_final_results,
    regrade_resume_state,
    regrade_target_node,
    reviewer_handoff,
    unified_result_patch,
)
from backend.app.ai.workflows.grading_workflow import (
    DEFAULT_MAX_REGRADES_PER_ANSWER,
    GRADING_WORKFLOW_AGGREGATION_FAILED,
    GRADING_WORKFLOW_ASYNC_REQUIRED,
    GRADING_WORKFLOW_IDENTITY_MISMATCH,
    GRADING_WORKFLOW_INVALID_INPUT,
    GRADING_WORKFLOW_MISSING_DECISION,
    GRADING_WORKFLOW_REGRADE_BUDGET_EXHAUSTED,
    GRADING_WORKFLOW_RESULT_NOT_VALIDATED,
    GradingWorkflow,
    GradingWorkflowDeps,
    GradingWorkflowError,
)
from backend.app.ai.workflows.state import workflow_state_to_json
from backend.app.core.config import AppSettings
from backend.app.domain.enums import GradingMode, QuestionType, WorkflowStatus
from backend.app.schemas.ai import GradingResult
from backend.app.schemas.grading import (
    ConfidenceDecisionDTO,
    DiagnosisReportDTO,
    DiagnosisStatus,
)
from backend.app.services.grading.grading_task_service import (
    GradingTargetAnswer,
    SubmissionSnapshot,
)
from backend.app.services.grading.result_aggregator import ResultAggregator
from tests.support.question_generation_doubles import COURSE_UUID
from tests.support.subjective_grading_doubles import (
    StubEmbeddingProvider,
    StubReranker,
    StubRetriever,
    StubScoringProvider,
    make_chunk,
)
from tests.unit.settings_helpers import build_test_settings

WORKFLOW_ID = "workflow-1"
REQUEST_ID = "request-1"


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """在同步用例中驱动异步入口；与仓库既有测试约定一致。"""

    import asyncio

    return asyncio.run(coroutine)


def _objective_target(*, answer_id: str = "answer-1", order: int = 1) -> GradingTargetAnswer:
    """构造判断题目标；客观题路径不得调用 LLM。"""

    return GradingTargetAnswer(
        order=order,
        answer_id=answer_id,
        question_id=f"question-{order}",
        question_type=QuestionType.TRUE_FALSE,
        max_score=Decimal(2),
        knowledge_points=("变量",),
        content="变量用于保存数据。",
        reference_answer="True",
        student_answer="True",
    )


def _subjective_target(*, answer_id: str = "answer-2", order: int = 2) -> GradingTargetAnswer:
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
        scoring_rubric="要点齐全得 10 分。",
        student_answer="变量用于保存数据。",
    )


def _snapshot(*targets: GradingTargetAnswer) -> SubmissionSnapshot:
    """构造答卷只读快照（复用 M3 类型）。"""

    return SubmissionSnapshot(
        submission_id="submission-1",
        exam_id="exam-1",
        student_id="student-1",
        course_id=COURSE_UUID,
        status="Submitted",
        answers=tuple(targets),
    )


def _result(
    target: GradingTargetAnswer,
    snapshot: SubmissionSnapshot,
    *,
    score: float = 6.0,
    confidence: float = 0.9,
    review_status: str = "Not Required",
    answer_id: str | None = None,
) -> GradingResult:
    """构造与目标一致的评分结果替身产物。"""

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
        validation_status="Validated",
        review_status=review_status,
        answer_id=answer_id if answer_id is not None else target.answer_id,
        submission_id=snapshot.submission_id,
    )


def _settings(**overrides: Any) -> AppSettings:
    """构造测试配置；默认阈值高于高置信度分支。"""

    values: dict[str, Any] = {"confidence_threshold": 0.8}
    values.update(overrides)
    return build_test_settings(**values)


class _RecordingObjectiveGrader:
    """记录调用的客观题评分替身。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def grade(self, **kwargs: Any) -> GradingResult:
        self.calls.append(kwargs)
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
            validation_status="Validated",
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


class _CountingAggregator:
    """包装真实汇总器并统计调用次数。"""

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


def _agent(**overrides: Any) -> Any:
    """构造阅卷 Agent（T069），默认注入替身检索、Embedding 与会话工厂。"""

    from backend.app.ai.agents.grading_agent import GradingAgent

    kwargs: dict[str, Any] = {
        "retriever": StubRetriever([make_chunk("chunk-1")]),
        "reranker": StubReranker(),
        "embedding_provider": StubEmbeddingProvider(),
        "settings": _settings(),
        "session_factory": _FakeSession,
    }
    kwargs.update(overrides)
    return GradingAgent(**kwargs)


def _identity() -> dict[str, Any]:
    """工作流身份字段（T065 Required State 的身份子集）。"""

    return {
        "workflow_id": WORKFLOW_ID,
        "request_id": REQUEST_ID,
        "submission_id": "submission-1",
    }


def _walk_answers(
    agent: Any,
    snapshot: SubmissionSnapshot,
    *,
    settings: AppSettings | None = None,
) -> dict[str, Any]:
    """按 T072 节点方式逐题评分，并把 T069 输出交接进工作流状态（不涉及 LangGraph）。"""

    state: dict[str, Any] = {
        **_identity(),
        "status": WorkflowStatus.RUNNING,
        "current_node": LOAD_SUBMISSION,
    }
    grading_results: dict[str, GradingResult] = {}
    decisions: dict[str, ConfidenceDecisionDTO] = {}
    for target in snapshot.answers:
        invocation = _run(
            agent.grade_answer_async(
                snapshot,
                target,
                request_id=REQUEST_ID,
                workflow_id=WORKFLOW_ID,
                settings=settings,
            )
        )
        state = {
            **state,
            **grading_handoff(
                invocation,
                submission_id=snapshot.submission_id,
                current_answer_order=target.order,
            ),
        }
        if invocation.output.grading_result is not None:
            grading_results[target.answer_id] = invocation.output.grading_result
        if invocation.output.confidence_decision is not None:
            decisions[target.answer_id] = invocation.output.confidence_decision
        state = {
            **state,
            "grading_results": dict(grading_results),
            "confidence_decisions": dict(decisions),
        }
    return state


def test_node_order_and_labels_match_plan_section_five() -> None:
    """节点顺序与标签必须逐项对应 `plan.md` §5，且条件边按题型分流。"""

    expected = {
        "load_submission": "Load Submission",
        "classify_question": "Classify Question",
        "objective_rule_grade": "Objective Rule Grade",
        "subjective_retrieve_grade": "Subjective Retrieve/Grade",
        "structured_validation": "Structured Validation",
        "confidence_check": "Confidence Check",
        "accept": "Accept",
        "pending_review": "Pending Review",
        "reviewer_agent": "Reviewer Agent",
        "regrade": "Re-grade",
        "next_answer": "Next Answer",
        "unified_result": "Unified Result",
        "generate_diagnosis": "Generate Diagnosis",
    }
    assert dict(NODE_LABELS) == expected
    assert WORKFLOW_NODE_ORDER == tuple(expected)
    assert WORKFLOW_NODE_ORDER.index(LOAD_SUBMISSION) == 0
    assert WORKFLOW_NODE_ORDER[-1] == GENERATE_DIAGNOSIS
    assert WORKFLOW_NODE_ORDER.index(PENDING_REVIEW) < WORKFLOW_NODE_ORDER.index(UNIFIED_RESULT)
    assert CLASSIFY_EDGES[GradingMode.OBJECTIVE] == "objective_rule_grade"
    assert CLASSIFY_EDGES[GradingMode.SUBJECTIVE] == "subjective_retrieve_grade"
    assert regrade_target_node(QuestionType.TRUE_FALSE) == "objective_rule_grade"
    assert regrade_target_node(QuestionType.SHORT_ANSWER) == "subjective_retrieve_grade"


def test_per_answer_nodes_score_once_and_unified_result_aggregates_once() -> None:
    """H01：逐题节点每题只评分一次，汇总节点只调用一次 `aggregate`。"""

    objective = _objective_target()
    subjective = _subjective_target()
    snapshot = _snapshot(objective, subjective)
    objective_grader = _RecordingObjectiveGrader()
    subjective_grader = _RecordingSubjectiveGrader(_result(subjective, snapshot))
    aggregator = _CountingAggregator()
    agent = _agent(
        objective_grader=objective_grader,
        subjective_grader=subjective_grader,
        aggregator=aggregator,
    )

    state = _walk_answers(agent, snapshot)

    assert len(objective_grader.calls) == 1
    assert len(subjective_grader.calls) == 1
    # 逐题阶段不得触发整卷汇总，也不得改用整卷入口。
    assert aggregator.calls == 0
    assert state["current_answer_id"] == subjective.answer_id
    assert set(state["grading_results"]) == {"answer-1", "answer-2"}
    assert state["current_node"] == "accept"

    patch = unified_result_patch(state, snapshot, aggregator=aggregator)

    assert aggregator.calls == 1
    exam_result = patch["exam_result"]
    assert exam_result.expected_answer_count == 2
    assert exam_result.is_final is True
    assert patch["final_results"] == exam_result.items
    workflow_state_to_json({**state, **patch, "status": WorkflowStatus.COMPLETED})


def test_failed_answer_is_recorded_as_error_not_empty_result() -> None:
    """H04：逐题失败进入 `error` 与失败状态，不得伪装成空结果。"""

    subjective = _subjective_target()
    snapshot = _snapshot(subjective)
    failing = _RecordingSubjectiveGrader(
        _result(subjective, snapshot),
        error=RuntimeError("主观题评分失败。"),
    )
    agent = _agent(subjective_grader=failing)

    state = _walk_answers(agent, snapshot)

    assert state["status"] is WorkflowStatus.FAILED
    assert state["error"].error_code
    # 失败态不得声明可恢复：既不写 True，也不自动置真。
    assert state.get("resumable") is not True
    assert "pause_reason" not in state
    assert "grading_result" not in state
    assert state["grading_results"] == {}
    workflow_state_to_json(state)


def test_low_confidence_pauses_workflow_and_supervisor_refuses_finish() -> None:
    """H04：低置信度进入 `Pending Review`、工作流暂停且可恢复，Supervisor 不得 `finish`。"""

    subjective = _subjective_target()
    snapshot = _snapshot(subjective)
    grader = _RecordingSubjectiveGrader(_result(subjective, snapshot, confidence=0.3))
    agent = _agent(subjective_grader=grader, settings=_settings(confidence_threshold=0.8))

    state = _walk_answers(agent, snapshot)

    assert state["status"] is WorkflowStatus.PAUSED
    assert state["review_status"] == "Pending Review"
    assert state["resumable"] is True
    assert str(state["pause_reason"]).strip()
    workflow_state_to_json(state)

    decision = SupervisorAgent().decide(
        AgentTaskKind.FINALIZE,
        AgentInput(
            agent_type=AgentType.GRADING,
            request_id=REQUEST_ID,
            workflow_id=WORKFLOW_ID,
            grading_result=state["grading_result"],
        ),
        workflow_state=state,
    )
    assert decision.supervisor_decision is not None
    assert decision.supervisor_decision.action is SupervisorAction.PAUSE


def test_per_call_settings_reach_workflow_state_decision_snapshot() -> None:
    """H02：逐次 `settings` 在节点调用中生效，并把真实阈值写入状态快照。"""

    subjective = _subjective_target()
    snapshot = _snapshot(subjective)
    grader = _RecordingSubjectiveGrader(_result(subjective, snapshot, confidence=0.5))
    agent = _agent(subjective_grader=grader, settings=_settings(confidence_threshold=0.2))

    state = _walk_answers(agent, snapshot, settings=_settings(confidence_threshold=0.9))

    assert state["confidence_decision"].threshold == 0.9
    assert state["confidence_decision"].confidence == 0.5
    assert state["review_status"] == "Pending Review"
    assert state["status"] is WorkflowStatus.PAUSED


def test_reviewer_accept_keeps_pending_review_facts() -> None:
    """H04：Reviewer `accept` 不清除待复核事实，也不写复核状态与分数。"""

    subjective = _subjective_target()
    snapshot = _snapshot(subjective)
    state = _walk_answers(
        _agent(subjective_grader=_RecordingSubjectiveGrader(_result(subjective, snapshot, confidence=0.3))),
        snapshot,
    )

    reviewed = ReviewerAgent().review_agent_output(
        AgentOutput(
            agent_type=AgentType.GRADING,
            status=AgentStatus.PENDING_REVIEW,
            grading_result=state["grading_result"],
            confidence_decision=state["confidence_decision"],
            question_type=subjective.question_type,
            requires_review=True,
        ),
        request_id=REQUEST_ID,
        workflow_id=WORKFLOW_ID,
    )

    assert reviewed.output.review_outcome is not None
    assert reviewed.output.review_outcome.decision is ReviewDecision.ACCEPT
    assert reviewed.output.requires_review is True
    assert reviewed.output.review_status is None

    patch = reviewer_handoff(
        reviewed,
        submission_id=snapshot.submission_id,
        current_answer_id=state["current_answer_id"],
    )

    assert patch["status"] is WorkflowStatus.PAUSED
    assert patch["resumable"] is True
    assert str(patch["pause_reason"]).strip()
    assert "review_status" not in patch
    assert "grading_result" not in patch
    # 教师尚未确认：该结果不得进入最终成绩。
    assert may_enter_final_results(state["grading_result"]) is False
    workflow_state_to_json({**state, **patch})


def test_regrade_resumes_with_original_identifiers() -> None:
    """H04：`regrade` 按原 `workflow_id`/`request_id` 恢复，转向确定性规则节点。"""

    subjective = _subjective_target()
    snapshot = _snapshot(subjective)
    result = _result(subjective, snapshot, confidence=0.9)
    decision = ConfidenceDecisionDTO(
        confidence=0.4,
        threshold=0.8,
        requires_review=True,
        review_status="Pending Review",
        grading_status="Pending",
        reason="置信度低于阈值。",
    )
    state = {
        **_identity(),
        "status": WorkflowStatus.PAUSED,
        "pause_reason": "等待教师复核。",
        "resumable": True,
        "current_node": PENDING_REVIEW,
    }

    reviewed = ReviewerAgent().review(
        result,
        decision,
        request_id=REQUEST_ID,
        workflow_id=WORKFLOW_ID,
    )
    patch = reviewer_handoff(
        reviewed,
        submission_id=snapshot.submission_id,
        current_answer_id=subjective.answer_id,
    )

    assert reviewed.output.review_outcome is not None
    assert reviewed.output.review_outcome.decision is ReviewDecision.REGRADE
    assert patch["status"] is WorkflowStatus.PAUSED
    assert patch["current_node"] == REGRADE
    # 权威复核状态来自本次决策（Pending Review）：结果自身仍标 Not Required 也不得放行。
    assert may_enter_final_results(result, decision=decision) is False

    resumed = regrade_resume_state({**state, **patch, **{k: v for k, v in _identity().items()}})

    assert resumed["workflow_id"] == WORKFLOW_ID
    assert resumed["request_id"] == REQUEST_ID
    assert resumed["submission_id"] == "submission-1"
    assert resumed["status"] is WorkflowStatus.RUNNING
    assert resumed["pause_reason"] is None
    assert resumed["resumable"] is False
    workflow_state_to_json(resumed)
    assert regrade_target_node(result.question_type) == "objective_rule_grade" or (
        regrade_target_node(result.question_type) == "subjective_retrieve_grade"
    )


def test_only_accepted_or_teacher_reviewed_results_reach_final_and_diagnosis() -> None:
    """H04：待复核结果不得进入 `final_results` 与诊断；教师复核后才放行。"""

    objective = _objective_target()
    subjective = _subjective_target()
    snapshot = _snapshot(objective, subjective)
    state = _walk_answers(
        _agent(
            objective_grader=_RecordingObjectiveGrader(),
            subjective_grader=_RecordingSubjectiveGrader(
                _result(subjective, snapshot, confidence=0.3)
            ),
        ),
        snapshot,
    )
    aggregator = _CountingAggregator()

    blocked = unified_result_patch(state, snapshot, aggregator=aggregator)

    exam_result = blocked["exam_result"]
    assert exam_result.is_final is False
    assert "final_results" not in blocked
    assert diagnosis_allowed({**state, **blocked}) is False

    # 教师复核后（Confirmed 且不再需要人工复核）才允许进入最终成绩与诊断。
    confirmed = {
        **state,
        "grading_results": {
            **state["grading_results"],
            "answer-2": _result(subjective, snapshot, review_status="Confirmed").model_copy(
                update={"confidence": state["grading_results"]["answer-2"].confidence}
            ),
        },
        "confidence_decisions": {
            **state["confidence_decisions"],
            "answer-2": ConfidenceDecisionDTO(
                confidence=state["confidence_decisions"]["answer-2"].confidence,
                threshold=0.8,
                requires_review=False,
                review_status="Confirmed",
                grading_status="Accepted",
                reason="教师确认。",
            ),
        },
    }
    confirmed["review_status"] = "Confirmed"

    allowed = unified_result_patch(confirmed, snapshot, aggregator=_CountingAggregator())

    assert allowed["exam_result"].is_final is True
    assert allowed["final_results"] == allowed["exam_result"].items
    assert diagnosis_allowed({**confirmed, **allowed}) is True
    assert ACCEPTED_REVIEW_STATES == {"Not Required", "Confirmed", "Modified", "Final"}
    assert all(
        may_enter_final_results(item_result) for item_result in confirmed["grading_results"].values()
    )


def test_handoff_requires_workflow_identity() -> None:
    """交接映射必须携带工作流身份；缺失或空白显式失败，不静默丢弃追溯标识。"""

    from backend.app.ai.workflows.grading_handoff import GradingHandoffError

    objective = _objective_target()
    snapshot = _snapshot(objective)
    invocation = _agent(
        objective_grader=_RecordingObjectiveGrader(),
    ).grade_answer(snapshot, objective, request_id=REQUEST_ID)

    assert isinstance(invocation, AgentInvocation)
    assert invocation.workflow_id is None
    with pytest.raises(GradingHandoffError):
        grading_handoff(
            invocation,
            submission_id=snapshot.submission_id,
            current_answer_order=objective.order,
        )
    with pytest.raises(GradingHandoffError):
        grading_handoff(
            _agent(objective_grader=_RecordingObjectiveGrader()).grade_answer(
                snapshot,
                objective,
                request_id=REQUEST_ID,
                workflow_id=WORKFLOW_ID,
            ),
            submission_id="   ",
            current_answer_order=objective.order,
        )


# --------------------------------------------------------------------------- T072 图用例


class _StubGradingAgent:
    """按题目返回预置评分输出；拒绝整卷入口，用于构造图分支场景。"""

    def __init__(
        self,
        *,
        results: dict[str, GradingResult],
        decisions: dict[str, ConfidenceDecisionDTO] | None = None,
        later_decisions: dict[str, ConfidenceDecisionDTO] | None = None,
        subjects: dict[str, QuestionType] | None = None,
    ) -> None:
        self._results = results
        self._decisions = decisions or {}
        self._later_decisions = later_decisions or {}
        self._subjects = subjects or {}
        self.calls: list[str] = []

    async def grade_answer_async(
        self,
        snapshot: SubmissionSnapshot,
        target: GradingTargetAnswer,
        *,
        request_id: str,
        workflow_id: str | None = None,
        session: Any = None,
        settings: AppSettings | None = None,
    ) -> AgentInvocation:
        repeat = self.calls.count(target.answer_id)
        self.calls.append(target.answer_id)
        result = self._results[target.answer_id]
        if repeat == 0:
            decision = self._decisions.get(target.answer_id)
        else:
            decision = self._later_decisions.get(
                target.answer_id,
                self._decisions.get(target.answer_id),
            )
        requires_review = bool(decision.requires_review) if decision is not None else False
        return AgentInvocation(
            request_id=request_id,
            workflow_id=workflow_id,
            output=AgentOutput(
                agent_type=AgentType.GRADING,
                status=AgentStatus.PENDING_REVIEW if requires_review else AgentStatus.SUCCESS,
                validation_status=result.validation_status,
                question_type=self._subjects.get(target.answer_id, target.question_type),
                grading_result=result,
                confidence=result.confidence,
                confidence_decision=decision,
                requires_review=requires_review,
            ),
        )

    async def score_async(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("图必须逐题调用 grade_answer_async，不得调用整卷 score_async。")

    def score(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("图必须逐题调用 grade_answer_async，不得调用整卷 score。")


class _StubDiagnosisService:
    """诊断服务替身：可配置失败，并记录调用次数。"""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    async def generate(self, exam_result: Any) -> DiagnosisReportDTO:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return DiagnosisReportDTO(
            submission_id=exam_result.submission_id,
            student_id=exam_result.student_id,
            status=DiagnosisStatus.READY,
            generated_at=datetime.now(UTC),
            source_exam_result_updated_at=exam_result.aggregated_at,
        )


def _workflow(
    snapshot: SubmissionSnapshot,
    *,
    agent: Any,
    reviewer: Any = None,
    diagnosis: Any = None,
    aggregator: Any = None,
    checkpointer: Any = None,
    max_regrades: int = DEFAULT_MAX_REGRADES_PER_ANSWER,
) -> GradingWorkflow:
    """构造图工作流；默认无检查点（不可恢复），需要时传入内存检查点。"""

    deps = GradingWorkflowDeps(
        snapshot=snapshot,
        agent=agent,
        reviewer=reviewer if reviewer is not None else ReviewerAgent(),
        diagnosis_service=diagnosis if diagnosis is not None else _StubDiagnosisService(),
        aggregator=aggregator if aggregator is not None else _CountingAggregator(),
        session_factory=_FakeSession,
        settings=_settings(),
        max_regrades=max_regrades,
    )
    return GradingWorkflow(deps, checkpointer=checkpointer)


def test_t072_graph_topology_uses_contract_nodes_and_edges() -> None:
    """T072：图拓扑必须与 T071 契约一致（节点集合与关键条件边）。"""

    objective = _objective_target()
    snapshot = _snapshot(objective)
    graph = _workflow(snapshot, agent=_StubGradingAgent(results={})).compiled.get_graph()

    business_nodes = {name for name in graph.nodes if not name.startswith("__")}
    assert business_nodes == set(WORKFLOW_NODE_ORDER)

    edges = {(edge.source, edge.target) for edge in graph.edges}
    expected_edges = {
        (LOAD_SUBMISSION, "classify_question"),
        ("classify_question", "objective_rule_grade"),
        ("classify_question", "subjective_retrieve_grade"),
        ("objective_rule_grade", "structured_validation"),
        ("subjective_retrieve_grade", "structured_validation"),
        ("structured_validation", "confidence_check"),
        ("confidence_check", "accept"),
        ("confidence_check", "pending_review"),
        ("accept", "next_answer"),
        ("pending_review", "reviewer_agent"),
        ("reviewer_agent", "regrade"),
        ("next_answer", "unified_result"),
        ("unified_result", "generate_diagnosis"),
    }
    assert expected_edges <= edges


def test_t072_real_components_complete_mixed_paper_once() -> None:
    """T072：真实图 + 真实评分器/置信策略/汇总器，仅替身外部边界，走通整卷并生成诊断。"""

    objective = _objective_target()
    subjective = _subjective_target()
    snapshot = _snapshot(objective, subjective)
    retriever = StubRetriever([make_chunk("chunk-1")])
    reranker = StubReranker()
    provider = StubScoringProvider(score=8.0, confidence=0.95)
    aggregator = _CountingAggregator()
    diagnosis = _StubDiagnosisService()
    agent = _agent(
        provider=provider,
        retriever=retriever,
        reranker=reranker,
        aggregator=aggregator,
    )
    workflow = _workflow(snapshot, agent=agent, diagnosis=diagnosis, aggregator=aggregator)

    outcome = _run(
        workflow.run_async(
            request_id=REQUEST_ID,
            workflow_id=WORKFLOW_ID,
            submission_id=snapshot.submission_id,
        )
    )

    state = outcome.state
    assert outcome.interrupted is False
    assert state["status"] is WorkflowStatus.COMPLETED
    assert state["exam_result"].is_final is True
    assert state["final_results"] == state["exam_result"].items
    assert state["diagnosis"] is not None
    assert set(state["grading_results"]) == {"answer-1", "answer-2"}
    # 真实检索、重排与结构化生成各被调用一次；汇总只调用一次。
    assert len(retriever.calls) == 1
    assert len(reranker.calls) == 1
    assert len(provider.calls) == 1
    assert aggregator.calls == 1
    workflow_state_to_json(state)


def test_t072_low_confidence_pauses_and_resumes_without_rescoring() -> None:
    """T072：低置信度在 pending_review 真正中断；同线程恢复不重复评分，教师确认后才推进。

    TCR：
    - 必要性：只写 `Paused`/`resumable` 字段不能阻止后续节点运行，必须验证原生中断、
      同线程恢复与“不重复评分”；
    - 契约依据：`plan.md` §5（人工复核可中断可恢复）、`agent-workflow.md`（低置信度必须暂停、
      仅教师可确认）、评审 B01/B02；
    - 覆盖行为：首次运行在待复核处中断且无最终结果与诊断；未获教师结论时恢复只再次暂停；
      写入教师结论后恢复推进到 `Completed`，且已完成题目不再评分。
    """

    objective = _objective_target()
    subjective = _subjective_target()
    snapshot = _snapshot(objective, subjective)
    objective_grader = _RecordingObjectiveGrader()
    subjective_grader = _RecordingSubjectiveGrader(
        _result(subjective, snapshot, confidence=0.3)
    )
    diagnosis = _StubDiagnosisService()
    agent = _agent(
        objective_grader=objective_grader,
        subjective_grader=subjective_grader,
        settings=_settings(confidence_threshold=0.8),
    )
    workflow = _workflow(
        snapshot,
        agent=agent,
        diagnosis=diagnosis,
        checkpointer=InMemorySaver(),
    )

    paused = _run(
        workflow.run_async(
            request_id=REQUEST_ID,
            workflow_id=WORKFLOW_ID,
            submission_id=snapshot.submission_id,
            thread_id="thread-1",
        )
    )

    assert paused.interrupted is True
    assert paused.pending_answer_ids == (subjective.answer_id,)
    assert paused.state["status"] is WorkflowStatus.PAUSED
    assert paused.state["review_status"] == "Pending Review"
    assert paused.state["resumable"] is True
    assert paused.state.get("exam_result") is None
    assert not paused.state.get("final_results")
    assert paused.state.get("diagnosis") is None
    assert diagnosis.calls == 0
    assert len(subjective_grader.calls) == 1
    workflow_state_to_json(paused.state)

    # 未获教师结论时恢复只会再次停在待复核节点，且不重复评分。
    again = _run(workflow.resume_async(thread_id="thread-1", resume_value=None))
    assert again.interrupted is True
    assert len(subjective_grader.calls) == 1
    assert len(objective_grader.calls) == 1

    # 模拟 T074 写入教师结论后，恢复应继续推进到整卷完成。
    workflow.mark_teacher_decision(
        thread_id="thread-1",
        answer_id=subjective.answer_id,
        review_status="Confirmed",
    )
    finished = _run(workflow.resume_async(thread_id="thread-1", resume_value=None))

    assert finished.interrupted is False
    assert finished.state["status"] is WorkflowStatus.COMPLETED
    assert finished.state["exam_result"].is_final is True
    assert finished.state["diagnosis"] is not None
    assert len(subjective_grader.calls) == 1
    assert diagnosis.calls == 1
    workflow_state_to_json(finished.state)


def test_t072_regrade_replaces_decision_and_keeps_review_requirement() -> None:
    """H01：重评后决策快照按答案替换为最新事实，人工复核要求用 review_status 表达。

    TCR：
    - 必要性：T065 要求 `confidence_decisions` 以 `answer_id` 为键且重评按答案替换；
      若保留触发复核的旧快照，状态中的置信度事实会与重评结果不一致，并把过期数据交给 T074；
    - 契约依据：`workflows/state.py`（集合按答案替换、不得残留旧状态）、`agent-workflow.md`
      （低置信度必须暂停且仅教师可确认）、复核 H01；
    - 覆盖行为：重评后该答案的决策置信度为最新值（而非旧值）、仍保持 `requires_review=True`
      与 `Pending Review`、集合键不增长，且未产生最终成绩与诊断。
    """

    subjective = _subjective_target()
    snapshot = _snapshot(subjective)
    fresh_result = _result(subjective, snapshot, confidence=0.9)
    stale_decision = ConfidenceDecisionDTO(
        confidence=0.3,
        threshold=0.8,
        requires_review=True,
        review_status="Pending Review",
        grading_status="Pending",
        reason="置信度低于阈值。",
    )
    fresh_decision = ConfidenceDecisionDTO(
        confidence=0.9,
        threshold=0.8,
        requires_review=False,
        review_status="Not Required",
        grading_status="Accepted",
        reason="置信度达标。",
    )
    agent = _StubGradingAgent(
        results={subjective.answer_id: fresh_result},
        decisions={subjective.answer_id: stale_decision},
        later_decisions={subjective.answer_id: fresh_decision},
    )
    workflow = _workflow(snapshot, agent=agent, checkpointer=InMemorySaver())

    first = _run(
        workflow.run_async(
            request_id=REQUEST_ID,
            workflow_id=WORKFLOW_ID,
            submission_id=snapshot.submission_id,
            thread_id="thread-h01",
        )
    )
    assert first.interrupted is True

    resumed = _run(workflow.resume_async(thread_id="thread-h01"))

    state = resumed.state
    decisions = state["confidence_decisions"]
    assert list(decisions) == [subjective.answer_id]
    # 最新事实被写入（不是触发复核的旧快照），同时保留人工复核要求。
    assert decisions[subjective.answer_id].confidence == 0.9
    assert decisions[subjective.answer_id].threshold == 0.8
    assert decisions[subjective.answer_id].requires_review is True
    assert decisions[subjective.answer_id].review_status == "Pending Review"
    assert len(state["grading_results"]) == 1
    assert state["retry_count"] == 1
    assert resumed.interrupted is True
    assert not state.get("final_results")
    assert state.get("diagnosis") is None


def test_t072_objective_answer_accepts_without_confidence_decision() -> None:
    """T072：客观题经校验后直接接受，不读取不存在的决策快照（B04）。"""

    objective = _objective_target()
    snapshot = _snapshot(objective)
    result = _result(objective, snapshot, score=2.0, confidence=1.0)
    agent = _StubGradingAgent(results={objective.answer_id: result})

    outcome = _run(
        _workflow(snapshot, agent=agent).run_async(
            request_id=REQUEST_ID,
            workflow_id=WORKFLOW_ID,
            submission_id=snapshot.submission_id,
        )
    )

    assert outcome.state["status"] is WorkflowStatus.COMPLETED
    assert outcome.state["confidence_decision"] is None
    assert outcome.state["grading_results"][objective.answer_id].review_status == "Not Required"


def test_t072_subjective_answer_without_decision_fails() -> None:
    """T072：主观题缺少本次决策快照即失败，不得默认 requires_review=False（B04）。"""

    subjective = _subjective_target()
    snapshot = _snapshot(subjective)
    result = _result(subjective, snapshot, confidence=0.9)
    agent = _StubGradingAgent(results={subjective.answer_id: result})

    outcome = _run(
        _workflow(snapshot, agent=agent).run_async(
            request_id=REQUEST_ID,
            workflow_id=WORKFLOW_ID,
            submission_id=snapshot.submission_id,
        )
    )

    assert outcome.state["status"] is WorkflowStatus.FAILED
    assert outcome.state["error"].error_code == GRADING_WORKFLOW_MISSING_DECISION
    assert outcome.state.get("exam_result") is None


def test_t072_unvalidated_result_terminates_with_error() -> None:
    """T072：结构化校验失败在 structured_validation 终止，不得进入汇总与诊断。"""

    subjective = _subjective_target()
    snapshot = _snapshot(subjective)
    decision = ConfidenceDecisionDTO(
        confidence=0.9,
        threshold=0.8,
        requires_review=False,
        review_status="Not Required",
        grading_status="Accepted",
        reason="置信度达标。",
    )
    hollow = GradingResult.model_construct(
        **{
            **_result(subjective, snapshot, confidence=0.9).model_dump(),
            "validation_status": "Pending",
        }
    )
    agent = _StubGradingAgent(
        results={subjective.answer_id: hollow},
        decisions={subjective.answer_id: decision},
    )
    diagnosis = _StubDiagnosisService()

    outcome = _run(
        _workflow(snapshot, agent=agent, diagnosis=diagnosis).run_async(
            request_id=REQUEST_ID,
            workflow_id=WORKFLOW_ID,
            submission_id=snapshot.submission_id,
        )
    )

    assert outcome.state["status"] is WorkflowStatus.FAILED
    assert outcome.state["error"].error_code == GRADING_WORKFLOW_RESULT_NOT_VALIDATED
    assert outcome.state.get("exam_result") is None
    assert diagnosis.calls == 0


def test_t072_regrade_loop_rescores_once_then_pauses() -> None:
    """T072：Reviewer 建议重评时回到评分节点，重评后仍停在待复核（B05/B01）。"""

    subjective = _subjective_target()
    snapshot = _snapshot(subjective)
    result = _result(subjective, snapshot, confidence=0.9)
    stale_decision = ConfidenceDecisionDTO(
        confidence=0.3,
        threshold=0.8,
        requires_review=True,
        review_status="Pending Review",
        grading_status="Pending",
        reason="置信度低于阈值。",
    )
    agent = _StubGradingAgent(
        results={subjective.answer_id: result},
        decisions={subjective.answer_id: stale_decision},
    )
    workflow = _workflow(snapshot, agent=agent, checkpointer=InMemorySaver())

    outcome = _run(
        workflow.run_async(
            request_id=REQUEST_ID,
            workflow_id=WORKFLOW_ID,
            submission_id=snapshot.submission_id,
            thread_id="thread-2",
        )
    )

    # 首次运行在待复核处中断：尚未取复核建议，也未重评。
    assert outcome.interrupted is True
    assert agent.calls == [subjective.answer_id]

    # 复核建议为重评（置信度事实不一致）→ 回到评分节点，重评后仍停在待复核。
    resumed = _run(workflow.resume_async(thread_id="thread-2"))

    assert resumed.interrupted is True
    assert agent.calls == [subjective.answer_id, subjective.answer_id]
    assert resumed.state["retry_count"] == 1
    assert resumed.state["status"] is WorkflowStatus.PAUSED
    assert resumed.state["current_node"] == "pending_review"
    # 重评后的新决策不得覆盖已记录的待复核决策（人工复核要求不自动解除）。
    assert resumed.state["confidence_decisions"][subjective.answer_id].requires_review is True
    assert resumed.pending_answer_ids == (subjective.answer_id,)


def test_t072_regrade_budget_exhausted_fails_bounded() -> None:
    """T072：重评预算为 0 时不得重评，必须有界失败（B05）。"""

    subjective = _subjective_target()
    snapshot = _snapshot(subjective)
    result = _result(subjective, snapshot, confidence=0.9)
    stale_decision = ConfidenceDecisionDTO(
        confidence=0.3,
        threshold=0.8,
        requires_review=True,
        review_status="Pending Review",
        grading_status="Pending",
        reason="置信度低于阈值。",
    )
    agent = _StubGradingAgent(
        results={subjective.answer_id: result},
        decisions={subjective.answer_id: stale_decision},
    )

    outcome = _run(
        _workflow(snapshot, agent=agent, max_regrades=0).run_async(
            request_id=REQUEST_ID,
            workflow_id=WORKFLOW_ID,
            submission_id=snapshot.submission_id,
        )
    )

    assert agent.calls == [subjective.answer_id]
    assert outcome.state["status"] is WorkflowStatus.FAILED
    assert outcome.state["error"].error_code == GRADING_WORKFLOW_REGRADE_BUDGET_EXHAUSTED


def test_t072_aggregation_failure_never_returns_partial_result() -> None:
    """T072：逐题结果与答卷不一致时汇总失败，不产生部分成绩与诊断。"""

    first = _objective_target()
    second = _objective_target(answer_id="answer-2", order=2)
    snapshot = _snapshot(first, second)
    agent = _StubGradingAgent(
        results={
            first.answer_id: _result(first, snapshot, score=2.0, confidence=1.0),
            second.answer_id: _result(
                second,
                snapshot,
                score=2.0,
                confidence=1.0,
                answer_id="answer-other",
            ),
        }
    )
    diagnosis = _StubDiagnosisService()

    outcome = _run(
        _workflow(snapshot, agent=agent, diagnosis=diagnosis).run_async(
            request_id=REQUEST_ID,
            workflow_id=WORKFLOW_ID,
            submission_id=snapshot.submission_id,
        )
    )

    assert outcome.state["status"] is WorkflowStatus.FAILED
    assert outcome.state["error"].error_code == GRADING_WORKFLOW_AGGREGATION_FAILED
    assert outcome.state.get("exam_result") is None
    assert not outcome.state.get("final_results")
    assert outcome.state.get("diagnosis") is None
    assert diagnosis.calls == 0

    # 重复答案标识属数据完整性错误：入口即拒绝（不允许进入逐题与汇总）。
    duplicated = _snapshot(_objective_target(order=1), _objective_target(order=2))
    blocked = _run(
        _workflow(duplicated, agent=agent).run_async(
            request_id=REQUEST_ID,
            workflow_id=WORKFLOW_ID,
            submission_id=duplicated.submission_id,
        )
    )

    assert blocked.state["status"] is WorkflowStatus.FAILED
    assert blocked.state["error"].error_code == GRADING_WORKFLOW_INVALID_INPUT
    assert blocked.state.get("exam_result") is None


def test_t072_diagnosis_failure_keeps_exam_result() -> None:
    """T072：诊断失败不得抹去已形成的整卷结果，且必须保留脱敏错误（B06）。"""

    objective = _objective_target()
    snapshot = _snapshot(objective)
    result = _result(objective, snapshot, score=2.0, confidence=1.0)
    agent = _StubGradingAgent(results={objective.answer_id: result})
    diagnosis = _StubDiagnosisService(error=RuntimeError("诊断 Provider 未就绪。"))

    outcome = _run(
        _workflow(snapshot, agent=agent, diagnosis=diagnosis).run_async(
            request_id=REQUEST_ID,
            workflow_id=WORKFLOW_ID,
            submission_id=snapshot.submission_id,
        )
    )

    state = outcome.state
    assert state["exam_result"] is not None
    assert state["exam_result"].is_final is True
    assert state["status"] is WorkflowStatus.PAUSED
    assert state["error"] is not None
    assert state.get("diagnosis") is None
    assert diagnosis.calls == 1
    workflow_state_to_json(state)


def test_t072_identity_mismatch_fails_and_clears_stale_state() -> None:
    """T072：身份不一致显式失败，且旧诊断/旧错误/上一题槽位不被沿用。"""

    objective = _objective_target()
    snapshot = _snapshot(objective)
    agent = _StubGradingAgent(
        results={objective.answer_id: _result(objective, snapshot, score=2.0)}
    )
    workflow = _workflow(snapshot, agent=agent)

    outcome = _run(
        workflow.run_async(
            request_id=REQUEST_ID,
            workflow_id=WORKFLOW_ID,
            submission_id="submission-9",
        )
    )

    assert outcome.state["status"] is WorkflowStatus.FAILED
    assert outcome.state["error"].error_code == GRADING_WORKFLOW_IDENTITY_MISMATCH
    assert outcome.state.get("diagnosis") is None
    assert agent.calls == []

    stale = {
        **_identity(),
        "status": WorkflowStatus.PAUSED,
        "pause_reason": "上一轮暂停。",
        "resumable": True,
        "current_answer_id": "answer-old",
        "grading_result": _result(objective, snapshot, score=2.0),
        "diagnosis": None,
        "error": AgentOutput(
            agent_type=AgentType.GRADING,
            status=AgentStatus.FAILURE,
            error={"error_code": "OLD", "message": "旧错误。"},
        ).error,
    }
    cleaned = _run(
        workflow.nodes[LOAD_SUBMISSION]({**stale, "submission_id": snapshot.submission_id})
    )

    assert cleaned.get("current_answer_id") is None
    assert cleaned.get("grading_result") is None
    assert cleaned.get("error") is None
    assert cleaned.get("pause_reason") is None
    assert cleaned.get("diagnosis") is None
    assert cleaned["current_answer_order"] == 1


def test_t072_sync_run_rejects_running_event_loop() -> None:
    """T072：同步入口在事件循环内显式报错（不嵌套 asyncio.run，与 T069 一致）。"""

    objective = _objective_target()
    snapshot = _snapshot(objective)
    workflow = _workflow(
        snapshot,
        agent=_StubGradingAgent(
            results={objective.answer_id: _result(objective, snapshot, score=2.0)}
        ),
    )

    async def _attempt() -> str:
        with pytest.raises(GradingWorkflowError) as error:
            workflow.run(
                request_id=REQUEST_ID,
                workflow_id=WORKFLOW_ID,
                submission_id=snapshot.submission_id,
            )
        return str(error.value.error_code)

    assert _run(_attempt()) == GRADING_WORKFLOW_ASYNC_REQUIRED
