"""T066 Supervisor Agent 路由与工具选择的失败优先测试。

任务编号：T066（M4；实现见 `backend/app/ai/agents/supervisor.py`）。

必要性：`plan.md` §4 要求 Supervisor Agent“理解任务、选择工具、决定调用哪个 Agent、控制
Workflow”，`.specify/contracts/agent-workflow.md` 规定置信度低于阈值必须进入 Pending Review
并暂停流程、只有教师能确认或修改低置信度结果、客观题不得调用 LLM（FR-030）。若路由输入合同
不完整（用 `agent_type` 代替任务种类）、低置信度仍被路由到 Reviewer、或整卷尚未汇总就结束
流程，则 T072 的 LangGraph 条件边会把“待人工复核”的评分当成终态，也会让客观题走上 LLM 路径。

覆盖内容：
1. 路由表覆盖全部任务种类，且只允许 `SupervisorDecision` 声明的目标 Agent；
2. 出题任务必须有 `generation_request`，评分任务必须有 `question_type`，缺失即失败；
3. 客观题评分只选确定性规则工具，不选任何 RAG/LLM 工具（FR-030）；
4. 主观题评分必须同时选择课程检索与结构化评分工具；
5. 待人工复核优先 `pause`，不路由 Reviewer；只有显式 `review` 任务才路由 Reviewer，
   且 Reviewer 建议不得解除人工复核状态、不得写教师人工结论；
6. `finalize` 必须等整卷结果与诊断都就绪，只读既有 `GradingWorkflowState` 快照，不推断；
7. 失败输出使用脱敏错误码、`retryable=False`，且不得声明需要人工复核；
8. 决策是纯函数：同输入同输出，不访问数据库与 Provider。

验收修复（H01）附加覆盖内容：
9. 整卷收尾的就绪判定只看真实 `ExamResultDTO`/`DiagnosisReportDTO`：整卷已最终确认、无缺题/失败/未校验题目、
   诊断 `Ready` 且经 `DiagnosisService.is_current` 判定对应当前整卷结果时才能 `finish`；
10. 诊断 `Not Ready`/`Failed`/`Stale`、旧版本 `Ready` 诊断、诊断属于其它答卷、整卷未最终确认、缺题或自造字典
   均保留 `SUPERVISOR_FINALIZE_NOT_READY`；整卷仍有待复核题目时返回 `pause`；
11. 逐题最终结果集合必须非空且数量与预期题目数量一致，不能用“对象非空”代替状态判断。

执行方法（先红后绿）：``python -m pytest tests/unit/agents -q``；实现前本文件因模块缺失而失败。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from backend.app.ai.agents import supervisor
from backend.app.ai.agents.state import (
    AgentInput,
    AgentOutput,
    AgentStatus,
    AgentType,
    QuestionGenerationRequest,
    SupervisorAction,
)
from backend.app.domain.enums import QuestionType, ReviewStatus, ValidationStatus
from backend.app.schemas.ai import GradingResult
from backend.app.schemas.grading import (
    ConfidenceDecisionDTO,
    DiagnosisReportDTO,
    DiagnosisStatus,
    ExamResultDTO,
    ExamResultStatus,
    QuestionResultDTO,
)


def _grading_result(*, review_status: str = "Not Required") -> GradingResult:
    """构造已通过结构化校验的单题评分结果。"""

    return GradingResult(
        question_type=QuestionType.SHORT_ANSWER,
        score=6.0,
        max_score=10.0,
        reason="说明了变量的作用。",
        correct_points=["保存数据"],
        missing_knowledge_points=["引用数据"],
        knowledge_points=["变量"],
        suggestions=["补充变量引用。"],
        confidence=0.5,
        validation_status=ValidationStatus.VALIDATED.value,
        review_status=review_status,
    )


def _confidence_decision(
    *,
    requires_review: bool,
    review_status: str,
) -> ConfidenceDecisionDTO:
    """构造置信度决策快照；阈值原样保留，不重新判定。"""

    return ConfidenceDecisionDTO(
        confidence=0.5,
        threshold=0.7,
        requires_review=requires_review,
        review_status=review_status,
        grading_status="Pending Review" if requires_review else "Accepted",
        reason="置信度低于阈值，需要人工复核。" if requires_review else "置信度达标。",
    )


#: 整卷汇总时间；诊断就绪判定需要与之比对。
_AGGREGATED_AT = datetime(2026, 9, 18, 8, 0, tzinfo=UTC)


def _question_result(
    answer_id: str = "answer-1",
    *,
    order: int = 1,
    requires_review: bool = False,
    counted: bool = True,
    review_status: str | None = None,
) -> QuestionResultDTO:
    """构造逐题汇总结果；未计入总分的题目按“缺结果”表示。"""

    return QuestionResultDTO(
        order=order,
        answer_id=answer_id,
        question_id=f"question-{order}",
        question_type=QuestionType.SINGLE_CHOICE,
        max_score=Decimal(2),
        score=Decimal(2) if counted else None,
        effective_score=Decimal(2) if counted else None,
        counted=counted,
        missing=not counted,
        requires_review=requires_review,
        grading_status="Accepted" if counted else "Missing",
        review_status=review_status,
    )


def _exam_result(
    *,
    items: list[QuestionResultDTO] | None = None,
    is_final: bool = True,
    pending_review: int = 0,
    missing: list[str] | None = None,
    failed: list[str] | None = None,
    not_validated: list[str] | None = None,
    expected_answer_count: int = 1,
    aggregated_at: datetime = _AGGREGATED_AT,
) -> ExamResultDTO:
    """构造真实整卷结果 DTO；默认是已最终确认的单题整卷。"""

    resolved_items = list(items) if items is not None else [_question_result()]
    return ExamResultDTO(
        submission_id="submission-1",
        exam_id="exam-1",
        student_id="student-1",
        result_status=ExamResultStatus.FINAL if is_final else ExamResultStatus.PENDING,
        is_final=is_final,
        final_total_score=Decimal(2) if is_final else None,
        confirmed_subtotal=Decimal(2),
        confirmed_subtotal_label="已确认 2 分",
        total_max_score=Decimal(2),
        expected_answer_count=expected_answer_count,
        graded_answer_count=len([item for item in resolved_items if not item.missing]),
        counted_answer_count=len([item for item in resolved_items if item.counted]),
        pending_review_answer_count=pending_review,
        missing_answer_ids=list(missing or ()),
        failed_answer_ids=list(failed or ()),
        not_validated_answer_ids=list(not_validated or ()),
        items=resolved_items,
        aggregated_at=aggregated_at,
    )


def _diagnosis(
    exam_result: ExamResultDTO,
    *,
    status: DiagnosisStatus = DiagnosisStatus.READY,
    source_updated_at: datetime | None = None,
    submission_id: str | None = None,
    generated_at: datetime | None = None,
) -> DiagnosisReportDTO:
    """构造诊断报告 DTO；默认与整卷结果对应且已就绪。"""

    if generated_at is None and status is DiagnosisStatus.READY:
        generated_at = _AGGREGATED_AT + timedelta(minutes=5)
    return DiagnosisReportDTO(
        exam_result_id=f"exam-result:{exam_result.submission_id}",
        submission_id=submission_id or exam_result.submission_id,
        student_id=exam_result.student_id,
        status=status,
        source_exam_result_updated_at=(
            source_updated_at if source_updated_at is not None else exam_result.aggregated_at
        ),
        generated_at=generated_at,
        error_code="DIAGNOSIS_PROVIDER_FAILED" if status is DiagnosisStatus.FAILED else None,
    )


def _agent_input(**overrides: Any) -> AgentInput:
    """构造 Supervisor 输入信封；默认是客观题评分任务。"""

    payload: dict[str, Any] = {
        "agent_type": AgentType.SUPERVISOR,
        "request_id": "request-1",
        "workflow_id": "workflow-1",
        "question_type": QuestionType.SINGLE_CHOICE,
    }
    payload.update(overrides)
    return AgentInput(**payload)


def _finalize_state(
    *,
    exam_result: ExamResultDTO | Any = None,
    diagnosis: DiagnosisReportDTO | Any = None,
    final_results: list[QuestionResultDTO] | None = None,
    review_status: Any = None,
    grading_results: dict[str, Any] | None = None,
    current_answer_id: str | None = None,
) -> dict[str, Any]:
    """构造整卷只读快照；默认是“整卷最终确认 + 诊断与当前结果对应”的真实 DTO。"""

    resolved_exam = exam_result if exam_result is not None else _exam_result()
    state: dict[str, Any] = {
        "workflow_id": "workflow-1",
        "request_id": "request-1",
        "submission_id": "submission-1",
        "exam_result": resolved_exam,
        "diagnosis": (
            diagnosis if diagnosis is not None else _diagnosis(resolved_exam)
        ),
        "final_results": list(final_results if final_results is not None else [_question_result()]),
    }
    if review_status is not None:
        state["review_status"] = review_status
    if grading_results is not None:
        state["grading_results"] = grading_results
    if current_answer_id is not None:
        state["current_answer_id"] = current_answer_id
    return state


def test_route_table_covers_every_task_kind() -> None:
    """路由表必须覆盖全部任务种类，避免出现未定义路由导致运行时静默兜底。"""

    from backend.app.ai.agents.supervisor import (
        SUPERVISOR_ROUTE_TABLE,
        AgentTaskKind,
    )

    assert set(SUPERVISOR_ROUTE_TABLE) == set(AgentTaskKind)
    assert SUPERVISOR_ROUTE_TABLE[AgentTaskKind.QUESTION_GENERATION] == frozenset(
        {AgentType.QUESTION}
    )
    assert SUPERVISOR_ROUTE_TABLE[AgentTaskKind.GRADING] == frozenset({AgentType.GRADING})
    assert SUPERVISOR_ROUTE_TABLE[AgentTaskKind.REVIEW] == frozenset({AgentType.REVIEWER})
    # 整卷收尾只允许结束流程，不得路由到任何 Agent。
    assert SUPERVISOR_ROUTE_TABLE[AgentTaskKind.FINALIZE] == frozenset()


def test_question_generation_routes_to_question_with_retrieval_tool() -> None:
    """出题任务应路由 Question Agent，并只选择课程检索工具。"""

    from backend.app.ai.agents.supervisor import (
        AgentTaskKind,
        SupervisorAgent,
        SupervisorTool,
    )

    output = SupervisorAgent().decide(
        AgentTaskKind.QUESTION_GENERATION,
        _agent_input(
            question_type=None,
            generation_request=QuestionGenerationRequest(
                course_id="course-1",
                knowledge_points=["变量"],
                difficulty="中等",
                question_type=QuestionType.SINGLE_CHOICE,
                count=2,
            ),
        ),
    )

    assert output.status is AgentStatus.SUCCESS
    decision = output.supervisor_decision
    assert decision is not None
    assert decision.action is SupervisorAction.ROUTE
    assert decision.next_agent is AgentType.QUESTION
    assert set(decision.tool_names) == {SupervisorTool.KNOWLEDGE_RETRIEVAL.value}
    assert decision.reason.strip()


def test_question_generation_without_request_fails() -> None:
    """缺少整个出题条件时必须失败，不能以空条件调用 Question Agent。"""

    from backend.app.ai.agents.supervisor import (
        SUPERVISOR_MISSING_GENERATION_REQUEST,
        AgentTaskKind,
        SupervisorAgent,
    )

    output = SupervisorAgent().decide(
        AgentTaskKind.QUESTION_GENERATION,
        _agent_input(question_type=None),
    )

    assert output.status is AgentStatus.FAILURE
    assert output.error is not None
    assert output.error.error_code == SUPERVISOR_MISSING_GENERATION_REQUEST
    assert output.error.retryable is False
    assert output.requires_review is False
    assert output.supervisor_decision is None


def test_grading_task_without_question_type_fails() -> None:
    """评分任务缺少题型时无法路由，必须显式失败而不是走默认分支。"""

    from backend.app.ai.agents.supervisor import (
        SUPERVISOR_MISSING_QUESTION_TYPE,
        AgentTaskKind,
        SupervisorAgent,
    )

    output = SupervisorAgent().decide(
        AgentTaskKind.GRADING,
        _agent_input(question_type=None),
    )

    assert output.status is AgentStatus.FAILURE
    assert output.error is not None
    assert output.error.error_code == SUPERVISOR_MISSING_QUESTION_TYPE


def test_objective_grading_uses_only_deterministic_tool() -> None:
    """客观题必须走确定性规则工具，即使携带检索上下文也不得选择 RAG/LLM 工具。"""

    from backend.app.ai.agents.supervisor import (
        AgentTaskKind,
        SupervisorAgent,
        SupervisorTool,
    )

    output = SupervisorAgent().decide(
        AgentTaskKind.GRADING,
        _agent_input(
            question_type=QuestionType.TRUE_FALSE,
            retrieved_context_ids=["chunk-1"],
        ),
    )

    decision = output.supervisor_decision
    assert decision is not None
    assert decision.next_agent is AgentType.GRADING
    assert set(decision.tool_names) == {SupervisorTool.OBJECTIVE_RULE_GRADE.value}
    assert SupervisorTool.SUBJECTIVE_RAG_GRADING.value not in decision.tool_names
    assert SupervisorTool.KNOWLEDGE_RETRIEVAL.value not in decision.tool_names


def test_subjective_grading_selects_retrieval_and_structured_grading() -> None:
    """主观题评分必须同时选择课程检索与结构化评分工具（FR-032）。"""

    from backend.app.ai.agents.supervisor import (
        AgentTaskKind,
        SupervisorAgent,
        SupervisorTool,
    )

    output = SupervisorAgent().decide(
        AgentTaskKind.GRADING,
        _agent_input(question_type=QuestionType.SHORT_ANSWER),
    )

    decision = output.supervisor_decision
    assert decision is not None
    assert decision.next_agent is AgentType.GRADING
    assert set(decision.tool_names) == {
        SupervisorTool.KNOWLEDGE_RETRIEVAL.value,
        SupervisorTool.SUBJECTIVE_RAG_GRADING.value,
    }


def test_pending_review_pauses_instead_of_routing_reviewer() -> None:
    """自动评分后的待人工复核优先暂停，不得由 Agent 自行交给 Reviewer 后继续放行。"""

    from backend.app.ai.agents.supervisor import AgentTaskKind, SupervisorAgent

    output = SupervisorAgent().decide(
        AgentTaskKind.GRADING,
        _agent_input(
            question_type=QuestionType.SHORT_ANSWER,
            grading_result=_grading_result(review_status=ReviewStatus.PENDING_REVIEW.value),
            confidence_decision=_confidence_decision(
                requires_review=True,
                review_status=ReviewStatus.PENDING_REVIEW.value,
            ),
        ),
    )

    assert output.status is AgentStatus.PENDING_REVIEW
    assert output.requires_review is True
    assert output.error is None
    assert output.review_status is None
    decision = output.supervisor_decision
    assert decision is not None
    assert decision.action is SupervisorAction.PAUSE
    assert decision.next_agent is None


def test_explicit_review_task_routes_reviewer_and_keeps_requires_review() -> None:
    """显式 review 任务才路由 Reviewer，且 Reviewer 建议不得解除人工复核。"""

    from backend.app.ai.agents.supervisor import (
        AgentTaskKind,
        SupervisorAgent,
        SupervisorTool,
    )

    output = SupervisorAgent().decide(
        AgentTaskKind.REVIEW,
        _agent_input(
            question_type=QuestionType.SHORT_ANSWER,
            grading_result=_grading_result(review_status=ReviewStatus.PENDING_REVIEW.value),
            confidence_decision=_confidence_decision(
                requires_review=True,
                review_status=ReviewStatus.PENDING_REVIEW.value,
            ),
        ),
    )

    assert output.status is AgentStatus.SUCCESS
    assert output.requires_review is True
    # Agent 不得写教师人工结论，复核状态仍由 T074/T077 的复核服务负责。
    assert output.review_status is None
    decision = output.supervisor_decision
    assert decision is not None
    assert decision.action is SupervisorAction.ROUTE
    assert decision.next_agent is AgentType.REVIEWER
    assert set(decision.tool_names) == {SupervisorTool.REVIEWER_CHECK.value}


def test_review_task_without_grading_result_fails() -> None:
    """没有可复核的评分结果时，review 任务必须失败而不是空跑 Reviewer。"""

    from backend.app.ai.agents.supervisor import (
        SUPERVISOR_MISSING_GRADING_RESULT,
        AgentTaskKind,
        SupervisorAgent,
    )

    output = SupervisorAgent().decide(
        AgentTaskKind.REVIEW,
        _agent_input(question_type=QuestionType.SHORT_ANSWER),
    )

    assert output.status is AgentStatus.FAILURE
    assert output.error is not None
    assert output.error.error_code == SUPERVISOR_MISSING_GRADING_RESULT


def test_finalize_finishes_only_with_final_result_and_current_diagnosis() -> None:
    """整卷最终确认且诊断对应当前结果时才能 finish（H01 就绪判定基线）。"""

    from backend.app.ai.agents.supervisor import AgentTaskKind, SupervisorAgent

    output = SupervisorAgent().decide(
        AgentTaskKind.FINALIZE,
        _agent_input(question_type=None),
        workflow_state=_finalize_state(),
    )

    assert output.status is AgentStatus.SUCCESS
    decision = output.supervisor_decision
    assert decision is not None
    assert decision.action is SupervisorAction.FINISH
    assert decision.next_agent is None
    assert decision.tool_names == []


def test_finalize_rejects_plain_dicts_as_results() -> None:
    """自造字典不能代替真实 DTO：对象非空不等于结果就绪。"""

    from backend.app.ai.agents.supervisor import (
        SUPERVISOR_FINALIZE_NOT_READY,
        AgentTaskKind,
        SupervisorAgent,
    )

    output = SupervisorAgent().decide(
        AgentTaskKind.FINALIZE,
        _agent_input(question_type=None),
        workflow_state=_finalize_state(
            exam_result={"items": []},
            diagnosis={"status": "Ready"},
        ),
    )

    assert output.status is AgentStatus.FAILURE
    assert output.error is not None
    assert output.error.error_code == SUPERVISOR_FINALIZE_NOT_READY


def test_finalize_requires_final_exam_result() -> None:
    """整卷尚未形成最终成绩时不得结束流程。"""

    from backend.app.ai.agents.supervisor import (
        SUPERVISOR_FINALIZE_NOT_READY,
        AgentTaskKind,
        SupervisorAgent,
    )

    exam_result = _exam_result(is_final=False, items=[_question_result(counted=False)])
    output = SupervisorAgent().decide(
        AgentTaskKind.FINALIZE,
        _agent_input(question_type=None),
        workflow_state=_finalize_state(
            exam_result=exam_result,
            diagnosis=_diagnosis(exam_result, status=DiagnosisStatus.NOT_READY),
            final_results=[_question_result(counted=False)],
        ),
    )

    assert output.status is AgentStatus.FAILURE
    assert output.error is not None
    assert output.error.error_code == SUPERVISOR_FINALIZE_NOT_READY


def test_finalize_fails_when_answers_are_missing() -> None:
    """缺题（缺结果、失败或未通过校验）时不得结束流程。"""

    from backend.app.ai.agents.supervisor import (
        SUPERVISOR_FINALIZE_NOT_READY,
        AgentTaskKind,
        SupervisorAgent,
    )

    agent = SupervisorAgent()
    missing_item = _question_result(counted=False)
    missing_exam = _exam_result(
        items=[missing_item],
        is_final=False,
        missing=["answer-1"],
        expected_answer_count=2,
    )
    missing_output = agent.decide(
        AgentTaskKind.FINALIZE,
        _agent_input(question_type=None),
        workflow_state=_finalize_state(
            exam_result=missing_exam,
            diagnosis=_diagnosis(missing_exam, status=DiagnosisStatus.NOT_READY),
            final_results=[missing_item],
        ),
    )
    assert missing_output.error is not None
    assert missing_output.error.error_code == SUPERVISOR_FINALIZE_NOT_READY

    # 逐题最终结果数量少于预期题目数量同样不得收尾。
    short_output = agent.decide(
        AgentTaskKind.FINALIZE,
        _agent_input(question_type=None),
        workflow_state=_finalize_state(
            exam_result=_exam_result(expected_answer_count=2),
            final_results=[_question_result()],
        ),
    )
    assert short_output.status is AgentStatus.FAILURE
    assert short_output.error is not None
    assert short_output.error.error_code == SUPERVISOR_FINALIZE_NOT_READY

    failed_exam = _exam_result(failed=["answer-1"], is_final=False)
    failed_output = agent.decide(
        AgentTaskKind.FINALIZE,
        _agent_input(question_type=None),
        workflow_state=_finalize_state(
            exam_result=failed_exam,
            diagnosis=_diagnosis(failed_exam, status=DiagnosisStatus.NOT_READY),
        ),
    )
    assert failed_output.error is not None
    assert failed_output.error.error_code == SUPERVISOR_FINALIZE_NOT_READY

    not_validated_exam = _exam_result(not_validated=["answer-1"], is_final=False)
    not_validated_output = agent.decide(
        AgentTaskKind.FINALIZE,
        _agent_input(question_type=None),
        workflow_state=_finalize_state(
            exam_result=not_validated_exam,
            diagnosis=_diagnosis(not_validated_exam, status=DiagnosisStatus.NOT_READY),
        ),
    )
    assert not_validated_output.error is not None
    assert not_validated_output.error.error_code == SUPERVISOR_FINALIZE_NOT_READY


def test_finalize_pauses_when_review_is_pending() -> None:
    """整卷仍有待人工复核题目时返回 pause，而不是失败或结束。"""

    from backend.app.ai.agents.supervisor import AgentTaskKind, SupervisorAgent

    exam_result = _exam_result(is_final=False, pending_review=1)
    output = SupervisorAgent().decide(
        AgentTaskKind.FINALIZE,
        _agent_input(question_type=None),
        workflow_state=_finalize_state(
            exam_result=exam_result,
            diagnosis=_diagnosis(exam_result, status=DiagnosisStatus.NOT_READY),
        ),
    )

    assert output.status is AgentStatus.PENDING_REVIEW
    assert output.requires_review is True
    assert output.error is None
    decision = output.supervisor_decision
    assert decision is not None
    assert decision.action is SupervisorAction.PAUSE


def test_finalize_fails_on_not_ready_or_failed_diagnosis() -> None:
    """诊断 Not Ready / Failed 均不得当作就绪。"""

    from backend.app.ai.agents.supervisor import (
        SUPERVISOR_FINALIZE_NOT_READY,
        AgentTaskKind,
        SupervisorAgent,
    )

    agent = SupervisorAgent()
    exam_result = _exam_result()
    for status in (DiagnosisStatus.NOT_READY, DiagnosisStatus.FAILED):
        output = agent.decide(
            AgentTaskKind.FINALIZE,
            _agent_input(question_type=None),
            workflow_state=_finalize_state(
                exam_result=exam_result,
                diagnosis=_diagnosis(exam_result, status=status),
            ),
        )
        assert output.status is AgentStatus.FAILURE
        assert output.error is not None
        assert output.error.error_code == SUPERVISOR_FINALIZE_NOT_READY


def test_finalize_fails_on_stale_or_old_diagnosis() -> None:
    """诊断 Stale、旧版本 Ready（汇总时间不同）或属于其它答卷时不得收尾。"""

    from backend.app.ai.agents.supervisor import (
        SUPERVISOR_FINALIZE_NOT_READY,
        AgentTaskKind,
        SupervisorAgent,
    )

    agent = SupervisorAgent()
    exam_result = _exam_result()
    cases = (
        _diagnosis(exam_result, status=DiagnosisStatus.STALE),
        _diagnosis(exam_result, source_updated_at=_AGGREGATED_AT - timedelta(minutes=30)),
        _diagnosis(exam_result, submission_id="submission-2"),
    )
    for diagnosis in cases:
        output = agent.decide(
            AgentTaskKind.FINALIZE,
            _agent_input(question_type=None),
            workflow_state=_finalize_state(
                exam_result=exam_result,
                diagnosis=diagnosis,
            ),
        )
        assert output.status is AgentStatus.FAILURE
        assert output.error is not None
        assert output.error.error_code == SUPERVISOR_FINALIZE_NOT_READY


def test_finalize_without_workflow_state_fails() -> None:
    """缺少整卷只读快照时不得推断完成，必须失败。"""

    from backend.app.ai.agents.supervisor import (
        SUPERVISOR_MISSING_WORKFLOW_STATE,
        AgentTaskKind,
        SupervisorAgent,
    )

    output = SupervisorAgent().decide(
        AgentTaskKind.FINALIZE,
        _agent_input(question_type=None),
    )

    assert output.status is AgentStatus.FAILURE
    assert output.error is not None
    assert output.error.error_code == SUPERVISOR_MISSING_WORKFLOW_STATE


def test_unknown_task_kind_fails_with_desensitized_error() -> None:
    """未知任务种类必须失败，错误信息只保留脱敏说明。"""

    from backend.app.ai.agents.supervisor import (
        SUPERVISOR_INVALID_INPUT,
        SupervisorAgent,
    )

    output = SupervisorAgent().decide("unknown-kind", _agent_input())  # type: ignore[arg-type]

    assert output.status is AgentStatus.FAILURE
    assert output.error is not None
    assert output.error.error_code == SUPERVISOR_INVALID_INPUT
    assert output.error.retryable is False
    assert output.requires_review is False
    assert output.error.message.strip()
    assert "unknown-kind" not in output.error.message


def test_decide_is_deterministic_and_has_no_side_effects() -> None:
    """路由是纯函数：同一输入重复调用结果完全一致。"""

    from backend.app.ai.agents.supervisor import AgentTaskKind, SupervisorAgent

    agent = SupervisorAgent()
    payload = _agent_input(question_type=QuestionType.SHORT_ANSWER)
    first: AgentOutput = agent.decide(AgentTaskKind.GRADING, payload)
    second: AgentOutput = agent.decide(AgentTaskKind.GRADING, payload)

    assert first.model_dump() == second.model_dump()


def test_supervisor_module_does_not_import_database_or_provider() -> None:
    """Supervisor 只做路由决策：不引入数据库会话、ORM 与 LLM Provider 依赖。"""

    source = Path(supervisor.__file__).read_text(encoding="utf-8")

    assert "sqlalchemy" not in source
    assert "backend.app.models" not in source
    assert "ai.llm" not in source


def test_teacher_confirmed_decision_is_not_overridden_by_history() -> None:
    """H02：教师已确认时，历史低置信度不得重新暂停工作流。

    契约依据：`tests/unit/workflows/test_workflow_state.py::
    test_teacher_decision_may_supersede_automatic_decision` 与 `HUMAN_DECIDED_REVIEW_STATES`。
    """

    from backend.app.ai.agents.supervisor import AgentTaskKind, SupervisorAgent

    agent_input = _agent_input(
        question_type=QuestionType.SHORT_ANSWER,
        grading_result=_grading_result(review_status=ReviewStatus.CONFIRMED.value),
        confidence_decision=_confidence_decision(
            requires_review=True,
            review_status=ReviewStatus.PENDING_REVIEW.value,
        ),
    )
    output = SupervisorAgent().decide(AgentTaskKind.GRADING, agent_input)

    assert output.status is AgentStatus.SUCCESS
    assert output.requires_review is False
    decision = output.supervisor_decision
    assert decision is not None
    assert decision.action is SupervisorAction.ROUTE
    assert decision.next_agent is AgentType.GRADING
    # 历史自动决策字段原样保留，不通过删除记录或改为 False 绕过暂停。
    assert agent_input.confidence_decision is not None
    assert agent_input.confidence_decision.requires_review is True
    assert agent_input.confidence_decision.threshold == 0.7
    # Agent 不得自行生成教师结论。
    assert output.review_status is None


@pytest.mark.parametrize(
    "review_status",
    [
        ReviewStatus.CONFIRMED.value,
        ReviewStatus.MODIFIED.value,
        ReviewStatus.FINAL.value,
    ],
)
def test_human_decided_statuses_are_authoritative(review_status: str) -> None:
    """H02：Confirmed/Modified/Final 均为权威结论，历史 requires_review 不再触发 pause。"""

    from backend.app.ai.agents.supervisor import AgentTaskKind, SupervisorAgent

    state = _finalize_state(review_status=review_status)
    output = SupervisorAgent().decide(
        AgentTaskKind.GRADING,
        _agent_input(
            question_type=QuestionType.SHORT_ANSWER,
            grading_result=_grading_result(review_status=review_status),
            confidence_decision=_confidence_decision(
                requires_review=True,
                review_status=ReviewStatus.PENDING_REVIEW.value,
            ),
        ),
        workflow_state=state,
    )

    assert output.status is AgentStatus.SUCCESS
    assert output.requires_review is False


def test_state_level_and_per_answer_review_status_are_authoritative() -> None:
    """H02：整卷槽位与逐题结果里的教师结论都能压过历史自动决策。"""

    from backend.app.ai.agents.supervisor import AgentTaskKind, SupervisorAgent

    agent_input = _agent_input(
        question_type=QuestionType.SHORT_ANSWER,
        grading_result=_grading_result(review_status=ReviewStatus.PENDING_REVIEW.value),
        confidence_decision=_confidence_decision(
            requires_review=True,
            review_status=ReviewStatus.PENDING_REVIEW.value,
        ),
    )
    state = _finalize_state(
        review_status=ReviewStatus.MODIFIED,
        current_answer_id="answer-1",
        grading_results={
            "answer-1": _grading_result(review_status=ReviewStatus.CONFIRMED.value)
        },
    )
    output = SupervisorAgent().decide(AgentTaskKind.GRADING, agent_input, workflow_state=state)

    assert output.status is AgentStatus.SUCCESS
    assert output.requires_review is False


def test_finalize_completes_after_teacher_decision_with_history_preserved() -> None:
    """H02：教师结论后仍可完成整卷收尾，历史决策保留且不写入教师结论。"""

    from backend.app.ai.agents.supervisor import AgentTaskKind, SupervisorAgent

    agent_input = _agent_input(
        question_type=None,
        grading_result=_grading_result(review_status=ReviewStatus.CONFIRMED.value),
        confidence_decision=_confidence_decision(
            requires_review=True,
            review_status=ReviewStatus.PENDING_REVIEW.value,
        ),
    )
    output = SupervisorAgent().decide(
        AgentTaskKind.FINALIZE,
        agent_input,
        workflow_state=_finalize_state(review_status=ReviewStatus.CONFIRMED),
    )

    assert output.status is AgentStatus.SUCCESS
    decision = output.supervisor_decision
    assert decision is not None
    assert decision.action is SupervisorAction.FINISH
    assert output.review_status is None
    assert agent_input.confidence_decision is not None
    assert agent_input.confidence_decision.requires_review is True


def test_regrade_schedules_grading_without_pausing() -> None:
    """H02：Re-grade 需要重新评分，可调度评分，但不暂停也不等于最终接受。"""

    from backend.app.ai.agents.supervisor import (
        SUPERVISOR_FINALIZE_NOT_READY,
        AgentTaskKind,
        SupervisorAgent,
    )

    agent = SupervisorAgent()
    regrade_input = _agent_input(
        question_type=QuestionType.SHORT_ANSWER,
        grading_result=_grading_result(review_status=ReviewStatus.RE_GRADE.value),
        confidence_decision=_confidence_decision(
            requires_review=True,
            review_status=ReviewStatus.RE_GRADE.value,
        ),
    )
    routed = agent.decide(AgentTaskKind.GRADING, regrade_input)
    assert routed.status is AgentStatus.SUCCESS
    assert routed.requires_review is False
    decision = routed.supervisor_decision
    assert decision is not None
    assert decision.action is SupervisorAction.ROUTE
    assert decision.next_agent is AgentType.GRADING

    # 重新评分不算最终接受：整卷未最终确认时依然不得收尾。
    # 逐题权威状态为 Re-grade，因此不计入“待人工复核”，但仍不能收尾。
    exam_result = _exam_result(
        is_final=False,
        items=[
            _question_result(
                counted=False,
                review_status=ReviewStatus.RE_GRADE.value,
            )
        ],
    )
    not_finished = agent.decide(
        AgentTaskKind.FINALIZE,
        regrade_input,
        workflow_state=_finalize_state(
            exam_result=exam_result,
            diagnosis=_diagnosis(exam_result, status=DiagnosisStatus.NOT_READY),
            final_results=[],
            review_status=ReviewStatus.RE_GRADE,
        ),
    )
    assert not_finished.status is AgentStatus.FAILURE
    assert not_finished.error is not None
    assert not_finished.error.error_code == SUPERVISOR_FINALIZE_NOT_READY


def test_unreviewed_low_confidence_still_pauses() -> None:
    """H02：没有权威教师结论时，历史低置信度仍然暂停等待人工复核。"""

    from backend.app.ai.agents.supervisor import AgentTaskKind, SupervisorAgent

    output = SupervisorAgent().decide(
        AgentTaskKind.GRADING,
        _agent_input(
            question_type=QuestionType.SHORT_ANSWER,
            confidence_decision=_confidence_decision(
                requires_review=True,
                review_status=ReviewStatus.PENDING_REVIEW.value,
            ),
        ),
    )

    assert output.status is AgentStatus.PENDING_REVIEW
    assert output.requires_review is True
    decision = output.supervisor_decision
    assert decision is not None
    assert decision.action is SupervisorAction.PAUSE
