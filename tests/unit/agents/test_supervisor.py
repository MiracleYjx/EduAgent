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

执行方法（先红后绿）：``python -m pytest tests/unit/agents -q``；实现前本文件因模块缺失而失败。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
from backend.app.schemas.grading import ConfidenceDecisionDTO


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


def _finalize_state(**overrides: Any) -> dict[str, Any]:
    """构造整卷只读快照；默认只有逐题最终结果。"""

    state: dict[str, Any] = {
        "workflow_id": "workflow-1",
        "request_id": "request-1",
        "submission_id": "submission-1",
        "final_results": [{"order": 1}],
    }
    state.update(overrides)
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


def test_finalize_requires_unified_result_and_diagnosis() -> None:
    """整卷收尾必须等汇总结果与诊断齐备（二者由 T072 产生），否则显式失败。"""

    from backend.app.ai.agents.supervisor import (
        SUPERVISOR_FINALIZE_NOT_READY,
        AgentTaskKind,
        SupervisorAgent,
    )

    agent = SupervisorAgent()
    not_ready = agent.decide(
        AgentTaskKind.FINALIZE,
        _agent_input(question_type=None),
        workflow_state=_finalize_state(),
    )
    assert not_ready.status is AgentStatus.FAILURE
    assert not_ready.error is not None
    assert not_ready.error.error_code == SUPERVISOR_FINALIZE_NOT_READY

    ready = agent.decide(
        AgentTaskKind.FINALIZE,
        _agent_input(question_type=None),
        workflow_state=_finalize_state(exam_result={"items": []}, diagnosis={"status": "Ready"}),
    )
    assert ready.status is AgentStatus.SUCCESS
    decision = ready.supervisor_decision
    assert decision is not None
    assert decision.action is SupervisorAction.FINISH
    assert decision.next_agent is None
    assert decision.tool_names == []


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
