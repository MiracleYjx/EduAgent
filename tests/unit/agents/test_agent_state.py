"""T065 Agent 输入/输出共用状态类型失败优先测试。

任务编号：T065（M4 Agent + Workflow；实现见 `backend/app/ai/agents/state.py`）。
必要性：`.specify/contracts/agent-workflow.md` 要求 Supervisor、Question、Grading、Reviewer
四类 Agent 都"接收任务上下文并返回结构化结果"，且低置信度必须进入 Pending Review、只有教师
才能确认或修改低置信度结果。若共用类型缺失、Trace 状态与 Workflow 状态混用，或 Agent 输出可以
直接声明教师结论，则 T066-T070 的 Agent、T072 的 LangGraph 节点与 T073 的检查点都会各自
发明状态字段，导致校验边界、追踪和复核授权失控。
覆盖内容：
1. `AgentStatus` 取值必须与 T063 `AgentRun` 的 CHECK 约束一致，且与 `WorkflowStatus` 分离；
2. 四类 Agent 的输入/输出合同：Supervisor 路由、Question 候选、Grading 评分与置信度、
   Reviewer 的 `accept/revise/regrade`；
3. 状态自洽：`failure` 必须有脱敏错误、`pending_review` 必须显式需要复核，且待复核不等于失败；
4. Agent 不得输出人工复核结论（复用 `HUMAN_DECIDED_REVIEW_STATES`），Reviewer 建议不等于
   教师 `Confirmed`/`Modified`；
5. `RetrievedContextItem` 保真映射 M2 `RetrievedChunk`：保留来源标识、metadata、各阶段可空
   分数与来源模式，缺失分数保持 `None` 而不是 0；
6. 自由参数字段只允许递归 JSON 值，拒绝 Session/Provider/ORM 实体等不可序列化对象；
7. `ConfidenceDecision`（frozen dataclass）与 `ConfidenceDecisionDTO` 双向适配，历史阈值原样
   恢复而不重新判定。
执行方法（先红后绿）：``python -m pytest tests/unit/agents -q``（实现前应因模块缺失而失败）。
"""

from __future__ import annotations

import json
import re
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import CheckConstraint

from backend.app.ai.agents.state import (
    AGENT_STATUS_TRACE_VALUES,
    AgentError,
    AgentInput,
    AgentOutput,
    AgentStatus,
    AgentType,
    QuestionGenerationRequest,
    RetrievedContextItem,
    ReviewDecision,
    ReviewerOutcome,
    SupervisorAction,
    SupervisorDecision,
    confidence_decision_from_snapshot,
    confidence_decision_snapshot,
)
from backend.app.ai.retrieval.base import RetrievedChunk
from backend.app.domain.enums import (
    QuestionType,
    ReviewStatus,
    ValidationStatus,
    WorkflowStatus,
)
from backend.app.models.agent_run import AgentRun
from backend.app.schemas.ai import GradingResult, QuestionCandidate
from backend.app.schemas.grading import SubmissionContext
from backend.app.services.grading.confidence_policy import (
    HUMAN_DECIDED_REVIEW_STATES,
    ConfidenceDecision,
)

#: 非选择题候选题的必填字段；用于构造可校验的候选题 DTO。
_CANDIDATE_KWARGS = {
    "content": "请说明极限存在的条件。",
    "reference_answer": "左右极限存在且相等。",
    "scoring_rubric": "要点齐全得分，缺一要点扣一半。",
    "difficulty": "中等",
    "knowledge_points": ["函数极限"],
    "score": 10.0,
}


def _grading_result(**overrides: object) -> GradingResult:
    """构造单题评分结果；默认是已校验的自动结果。"""

    payload: dict[str, object] = {
        "question_type": QuestionType.SHORT_ANSWER,
        "score": 8.5,
        "max_score": 10.0,
        "reason": "要点基本齐全。",
        "correct_points": ["左右极限相等"],
        "missing_knowledge_points": ["连续性"],
        "knowledge_points": ["函数极限"],
        "suggestions": ["复习连续性与极限的关系。"],
        "confidence": 0.9,
        "validation_status": ValidationStatus.VALIDATED.value,
        "review_status": ReviewStatus.NOT_REQUIRED.value,
        "retrieved_context_ids": ["chunk-1"],
        "answer_id": "answer-1",
        "submission_id": "submission-1",
    }
    payload.update(overrides)
    return GradingResult(**payload)


def _confidence_decision(**overrides: object) -> ConfidenceDecision:
    """构造一次置信度决策（T053 事实对象）。

    ``review_status``/``grading_status`` 默认随 ``requires_review`` 推导，避免构造出与事实
    不一致的决策对象。
    """

    requires_review = bool(overrides.pop("requires_review", False))
    payload: dict[str, object] = {
        "confidence": 0.9,
        "threshold": 0.7,
        "requires_review": requires_review,
        "review_status": (
            ReviewStatus.PENDING_REVIEW.value
            if requires_review
            else ReviewStatus.NOT_REQUIRED.value
        ),
        "grading_status": "Pending Review" if requires_review else "Accepted",
        "reason": "置信度与阈值的比较结果。",
    }
    payload.update(overrides)
    return ConfidenceDecision(**payload)  # type: ignore[arg-type]


def _submission_context() -> SubmissionContext:
    """构造权威答卷上下文，供评分/复核输入使用。"""

    return SubmissionContext.model_validate(
        {
            "submission_id": "submission-1",
            "exam_id": "exam-1",
            "student_id": "student-1",
            "expected_answers": [
                {
                    "order": 1,
                    "answer_id": "answer-1",
                    "question_id": "question-1",
                    "question_type": QuestionType.SHORT_ANSWER.value,
                    "max_score": "10.00",
                    "knowledge_points": ["函数极限"],
                }
            ],
        }
    )


def _retrieved_chunk(**overrides: object) -> RetrievedChunk:
    """构造 M2 检索候选；默认缺失阶段分数以验证保真映射。"""

    payload: dict[str, object] = {
        "chunk_id": "chunk-1",
        "course_id": "course-1",
        "document_id": "document-1",
        "content": "极限存在的条件说明。",
        "metadata": {"page": 3, "section": "2.1"},
        "rank": 1,
        "source_mode": "both",
    }
    payload.update(overrides)
    return RetrievedChunk(**payload)  # type: ignore[arg-type]


def _agent_run_status_trace_values() -> set[str]:
    """从 T063 `AgentRun` 模型读出 Trace 状态 CHECK 约束取值。"""

    for constraint in AgentRun.__table_args__:
        if (
            isinstance(constraint, CheckConstraint)
            and constraint.name == "ck_agent_runs_status_trace_value"
        ):
            return set(re.findall(r"'([a-z_]+)'", str(constraint.sqltext)))
    raise AssertionError("AgentRun 缺少 Trace 状态 CHECK 约束，无法核对 AgentStatus 取值。")


def test_agent_status_values_follow_agent_run_trace_constraint() -> None:
    """AgentStatus 必须与 T063 AgentRun 的 Trace 状态取值一致，并与 WorkflowStatus 分离。"""

    assert _agent_run_status_trace_values() == {"success", "failure", "pending_review"}
    assert {status.value for status in AgentStatus} == AGENT_STATUS_TRACE_VALUES
    assert AGENT_STATUS_TRACE_VALUES == {"success", "failure", "pending_review"}
    # 待复核/失败与工作流状态不得混用同一枚举。
    assert AGENT_STATUS_TRACE_VALUES & {status.value for status in WorkflowStatus} == set()


def test_agent_input_requires_request_id_and_allows_standalone_workflow() -> None:
    """共用信封必须带 request_id；独立 Agent 调用允许 workflow_id 为空。"""

    standalone = AgentInput(
        agent_type=AgentType.QUESTION,
        request_id="request-1",
        generation_request=QuestionGenerationRequest(
            course_id="course-1",
            knowledge_points=["函数极限"],
            difficulty="中等",
            question_type=QuestionType.SHORT_ANSWER,
            count=3,
        ),
    )
    assert standalone.workflow_id is None

    with pytest.raises(ValidationError):
        AgentInput(agent_type=AgentType.QUESTION)


def test_four_agent_inputs_and_outputs_are_structured_dtos() -> None:
    """Supervisor/Question/Grading/Reviewer 四类输入输出都必须由明确 DTO 承载。"""

    context = _submission_context()
    decision = confidence_decision_snapshot(_confidence_decision())
    grading = _grading_result()

    grade_input = AgentInput(
        agent_type=AgentType.GRADING,
        request_id="request-2",
        workflow_id="workflow-1",
        submission_context=context,
        answer_id="answer-1",
        question_type=QuestionType.SHORT_ANSWER,
        query="【题目】极限存在的条件",
        retrieved_context_ids=["chunk-1"],
        retrieved_context=[RetrievedContextItem.from_chunk(_retrieved_chunk())],
        parameters={"confidence_threshold": 0.7, "retry_allowed": True},
    )
    assert grade_input.submission_context is context

    supervisor_output = AgentOutput(
        agent_type=AgentType.SUPERVISOR,
        status=AgentStatus.SUCCESS,
        supervisor_decision=SupervisorDecision(
            action=SupervisorAction.ROUTE,
            next_agent=AgentType.GRADING,
            tool_names=["retrieve_context"],
            reason="主观题需要检索与结构化评分。",
        ),
    )
    question_output = AgentOutput(
        agent_type=AgentType.QUESTION,
        status=AgentStatus.SUCCESS,
        question_candidates=[
            QuestionCandidate(question_type=QuestionType.SHORT_ANSWER, **_CANDIDATE_KWARGS)
        ],
        retrieved_context_ids=["chunk-1"],
        prompt_version="question-prompt-v1",
    )
    grading_output = AgentOutput(
        agent_type=AgentType.GRADING,
        status=AgentStatus.SUCCESS,
        question_type=QuestionType.SHORT_ANSWER,
        grading_result=grading,
        confidence=0.9,
        confidence_decision=decision,
        validation_status=ValidationStatus.VALIDATED,
        requires_review=False,
        retrieved_context_ids=["chunk-1"],
        model="deepseek-chat",
    )
    reviewer_output = AgentOutput(
        agent_type=AgentType.REVIEWER,
        status=AgentStatus.SUCCESS,
        review_outcome=ReviewerOutcome(
            decision=ReviewDecision.REVISE,
            reason="理由与知识点不一致，建议修订分数。",
            revised_grading_result=_grading_result(score=6.0, reason="要点缺失较多。"),
        ),
        review_status=ReviewStatus.PENDING_REVIEW,
        requires_review=True,
    )

    for output in (supervisor_output, question_output, grading_output, reviewer_output):
        payload = json.loads(json.dumps(output.model_dump(mode="json"), ensure_ascii=False))
        restored = AgentOutput.model_validate(payload)
        assert restored.agent_type is output.agent_type
        assert restored.status is output.status

    # 四类输出各自的专属字段必须落在明确 DTO 上，而不是任意 JSON 字典。
    assert supervisor_output.supervisor_decision is not None
    assert supervisor_output.supervisor_decision.action is SupervisorAction.ROUTE
    assert question_output.question_candidates[0].status == "Candidate Generation"
    assert grading_output.grading_result is not None
    assert grading_output.confidence_decision is not None
    assert reviewer_output.review_outcome is not None
    assert reviewer_output.review_outcome.decision is ReviewDecision.REVISE


def test_supervisor_decision_requires_next_agent_only_when_routing() -> None:
    """路由决策必须给出下一个 Agent；结束或暂停不得残留路由目标。"""

    with pytest.raises(ValidationError):
        SupervisorDecision(action=SupervisorAction.ROUTE, reason="缺少路由目标。")
    with pytest.raises(ValidationError):
        SupervisorDecision(
            action=SupervisorAction.FINISH,
            next_agent=AgentType.GRADING,
            reason="已结束但残留路由目标。",
        )
    finished = SupervisorDecision(action=SupervisorAction.FINISH, reason="答卷已全部完成。")
    assert finished.next_agent is None


def test_agent_output_failure_and_pending_review_rules() -> None:
    """失败必须带脱敏错误，待复核必须显式需要复核且不得当作失败。"""

    error = AgentError(
        error_code="AGENT_PROVIDER_UNAVAILABLE",
        message="Provider 未就绪，无法完成评分。",
        retryable=True,
        attempt_count=2,
    )
    failed = AgentOutput(
        agent_type=AgentType.GRADING,
        status=AgentStatus.FAILURE,
        error=error,
    )
    assert failed.error is not None
    assert json.loads(json.dumps(failed.model_dump(mode="json")))["error"] == {
        "error_code": "AGENT_PROVIDER_UNAVAILABLE",
        "message": "Provider 未就绪，无法完成评分。",
        "retryable": True,
        "source_code": None,
        "attempt_count": 2,
    }

    # 失败缺少错误、或同时声明需要复核（待复核不等于失败）都不合法。
    with pytest.raises(ValidationError):
        AgentOutput(agent_type=AgentType.GRADING, status=AgentStatus.FAILURE)
    with pytest.raises(ValidationError):
        AgentOutput(
            agent_type=AgentType.GRADING,
            status=AgentStatus.FAILURE,
            error=error,
            requires_review=True,
        )
    # 待复核必须是明确的复核要求，且不得携带错误。
    with pytest.raises(ValidationError):
        AgentOutput(agent_type=AgentType.GRADING, status=AgentStatus.PENDING_REVIEW)
    with pytest.raises(ValidationError):
        AgentOutput(
            agent_type=AgentType.GRADING,
            status=AgentStatus.PENDING_REVIEW,
            requires_review=True,
            error=error,
        )
    accepted = AgentOutput(
        agent_type=AgentType.GRADING,
        status=AgentStatus.SUCCESS,
        requires_review=False,
        grading_result=_grading_result(),
    )
    assert accepted.error is None


def test_agent_output_rejects_human_decided_review_status() -> None:
    """Agent 不得输出教师人工结论；Reviewer 建议不等于教师确认。"""

    assert ReviewStatus.CONFIRMED.value in HUMAN_DECIDED_REVIEW_STATES
    with pytest.raises(ValidationError):
        AgentOutput(
            agent_type=AgentType.REVIEWER,
            status=AgentStatus.SUCCESS,
            review_status=ReviewStatus.CONFIRMED,
        )
    with pytest.raises(ValidationError):
        AgentOutput(
            agent_type=AgentType.GRADING,
            status=AgentStatus.SUCCESS,
            grading_result=_grading_result(review_status=ReviewStatus.MODIFIED.value),
        )
    # 待复核与无人工结论仍可正常输出。
    pending = AgentOutput(
        agent_type=AgentType.GRADING,
        status=AgentStatus.PENDING_REVIEW,
        requires_review=True,
        review_status=ReviewStatus.PENDING_REVIEW,
    )
    assert pending.review_status is ReviewStatus.PENDING_REVIEW


def test_reviewer_outcome_rules_preserve_accept_revise_regrade_contract() -> None:
    """Reviewer 决策取值保持 accept/revise/regrade，并校验与修订结果的一致性。"""

    assert {decision.value for decision in ReviewDecision} == {"accept", "revise", "regrade"}
    assert ReviewerOutcome(decision=ReviewDecision.ACCEPT, reason="分数与理由一致。")
    with pytest.raises(ValidationError):
        ReviewerOutcome(decision=ReviewDecision.REVISE, reason="缺少修订结果。")
    with pytest.raises(ValidationError):
        ReviewerOutcome(
            decision=ReviewDecision.REGRADE,
            reason="不应同时给出修订结果。",
            revised_grading_result=_grading_result(score=6.0),
        )
    with pytest.raises(ValidationError):
        ReviewerOutcome(
            decision=ReviewDecision.ACCEPT,
            reason="接受但给出修订结果。",
            revised_grading_result=_grading_result(score=9.0),
        )
    with pytest.raises(ValidationError):
        ReviewerOutcome(decision=ReviewDecision.REVISE, reason="   ")


def test_retrieved_context_item_maps_retrieved_chunk_fields_faithfully() -> None:
    """检索 DTO 必须保留来源与各阶段分数；缺失分数保持 None，且不声明派生 score。"""

    chunk = _retrieved_chunk(
        semantic_score=0.8,
        fusion_score=0.75,
        rerank_score=None,
        rank=2,
    )
    item = RetrievedContextItem.from_chunk(chunk)

    assert item.chunk_id == "chunk-1"
    assert item.course_id == "course-1"
    assert item.document_id == "document-1"
    assert item.content == "极限存在的条件说明。"
    assert item.metadata == {"page": 3, "section": "2.1"}
    assert item.semantic_score == 0.8
    assert item.keyword_score is None
    assert item.fusion_score == 0.75
    assert item.rerank_score is None
    assert item.rank == 2
    assert item.source_mode == "both"
    # 缺失分数不得伪装成 0，也不得声明与 extra="forbid" 冲突的派生 `score` 字段。
    assert "score" not in RetrievedContextItem.model_fields
    assert item.keyword_score != 0.0

    # rank=0 表示未编号，映射为 None；空白来源标识同样退化为 None。
    unranked = RetrievedContextItem.from_chunk(_retrieved_chunk(rank=0, source_mode=None))
    assert unranked.rank is None
    assert unranked.source_mode is None
    assert json.loads(json.dumps(unranked.model_dump(mode="json")))["rank"] is None


def test_retrieved_context_item_rejects_non_json_metadata() -> None:
    """检索 metadata 必须是 JSON 可序列化值，不得静默丢弃或写入任意对象。"""

    with pytest.raises(ValidationError):
        RetrievedContextItem(
            chunk_id="chunk-1",
            content="正文。",
            metadata={"session": object()},
        )


def test_agent_input_parameters_only_allow_recursive_json_values() -> None:
    """自由参数字段只允许递归 JSON 值，禁止 Session/Provider/ORM 实体。"""

    allowed = AgentInput(
        agent_type=AgentType.QUESTION,
        request_id="request-3",
        parameters={"count": 3, "knowledge_points": ["极限", "连续"], "nested": {"ok": None}},
    )
    assert allowed.parameters["count"] == 3
    assert json.dumps(allowed.model_dump(mode="json"), ensure_ascii=False)

    with pytest.raises(ValidationError):
        AgentInput(
            agent_type=AgentType.QUESTION,
            request_id="request-4",
            parameters={"session": object()},
        )
    with pytest.raises(ValidationError):
        AgentInput(
            agent_type=AgentType.QUESTION,
            request_id="request-5",
            parameters={"nested": {"deep": [object()]}},
        )


def test_confidence_decision_adapters_round_trip_without_redeciding() -> None:
    """快照与 T053 决策对象互转必须原样保留历史阈值与状态。"""

    decision = _confidence_decision(confidence=0.42, threshold=0.85, requires_review=True)
    snapshot = confidence_decision_snapshot(decision)

    assert snapshot.threshold == 0.85
    assert snapshot.confidence == 0.42
    assert snapshot.requires_review is True
    assert snapshot.review_status == ReviewStatus.PENDING_REVIEW.value

    restored = confidence_decision_from_snapshot(snapshot)
    assert restored == decision
    assert restored.threshold == 0.85
    assert restored.grading_status == decision.grading_status


def test_grading_result_dto_is_reused_and_not_redefined() -> None:
    """评分结果必须复用 T010/T052 的 GradingResult，而不是第二套同名类型。"""

    grading = _grading_result()
    output = AgentOutput(
        agent_type=AgentType.GRADING,
        status=AgentStatus.SUCCESS,
        grading_result=grading,
        validation_status=ValidationStatus.VALIDATED,
    )
    assert output.grading_result is grading
    assert isinstance(output.grading_result.score, float)
    assert output.validation_status is ValidationStatus.VALIDATED
    # Decimal 型汇总字段不属于单题评分 DTO，避免与 T054 汇总结果混淆。
    assert not isinstance(output.grading_result.score, Decimal)
