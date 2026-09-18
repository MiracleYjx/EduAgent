"""T070 Reviewer Agent 失败优先测试。

TCR（测试契约记录）：

- 必要性：T065 的 ``AgentOutput`` 只校验通用状态，不校验评分专属一致性；复核决策必须证明
  ``accept``/``revise``/``regrade`` 三类决策的边界，以及 Agent 不改分、不代替教师、不解除人工复核。
- 契约依据：``.specify/plan.md`` §4 Agent 分工与 §5 ``Needs Review`` 节点、
  ``.specify/contracts/agent-workflow.md``、FR-030（客观题确定性、不进入人工复核）、
  FR-035~FR-037（阈值、待复核与教师最终确认），以及 T065 ``ReviewDecision``/``ReviewerOutcome``
  的字段约束（``revise`` 必带修订结果，``accept``/``regrade`` 不得带）。
- 覆盖行为：三类决策正反例；``revise`` 只改 ``review_status``（其余字段逐字段相同）；
  异常输入/未校验结果/已带人工结论/空理由的拒绝；身份与满分交叉校验；
  ``request_id``/``workflow_id`` 保留与空白拒绝；状态补丁只含控制字段且可被 T065 校验序列化。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from backend.app.ai.agents.invocation import (
    AGENT_MISSING_REQUEST_ID,
    AgentInvocationError,
)
from backend.app.ai.agents.reviewer_agent import (
    REVIEWER_EMPTY_REASON,
    REVIEWER_HUMAN_DECISION_PRESENT,
    REVIEWER_INVALID_INPUT,
    REVIEWER_RESULT_MISMATCH,
    REVIEWER_RESULT_NOT_VALIDATED,
    ReviewerAgent,
    ReviewerAgentError,
    reviewer_output_to_state_patch,
)
from backend.app.ai.agents.state import (
    AgentOutput,
    AgentStatus,
    AgentType,
    ReviewDecision,
)
from backend.app.ai.workflows.state import workflow_state_to_json
from backend.app.domain.enums import QuestionType, ValidationStatus, WorkflowStatus
from backend.app.schemas.ai import GradingResult
from backend.app.schemas.grading import ConfidenceDecisionDTO


def _result(
    *,
    question_type: QuestionType = QuestionType.SHORT_ANSWER,
    score: float = 6.0,
    max_score: float = 10.0,
    confidence: float = 0.9,
    validation_status: str = ValidationStatus.VALIDATED.value,
    review_status: str = "Not Required",
    reason: str = "说明了变量的作用。",
    answer_id: str = "answer-2",
    submission_id: str = "submission-1",
) -> GradingResult:
    """构造单题评分结果；默认与 ``_decision()`` 一致，便于反向用例逐项改值。"""

    return GradingResult(
        question_type=question_type,
        score=score,
        max_score=max_score,
        reason=reason,
        correct_points=["保存数据"],
        missing_knowledge_points=["引用数据"],
        knowledge_points=["变量"],
        suggestions=["补充变量引用。"],
        confidence=confidence,
        validation_status=validation_status,
        review_status=review_status,
        answer_id=answer_id,
        submission_id=submission_id,
    )


def _decision(
    *,
    confidence: float = 0.9,
    threshold: float = 0.8,
    requires_review: bool = False,
    review_status: str = "Not Required",
    grading_status: str = "Accepted",
    reason: str = "置信度达标。",
) -> ConfidenceDecisionDTO:
    """构造置信度决策快照（T053/M3 事实，不做阈值重判）。"""

    return ConfidenceDecisionDTO(
        confidence=confidence,
        threshold=threshold,
        requires_review=requires_review,
        review_status=review_status,
        grading_status=grading_status,
        reason=reason,
    )


def _review(
    result: object,
    decision: object,
    **overrides: Any,
) -> AgentOutput:
    """调用复核入口并取回输出。"""

    kwargs: dict[str, Any] = {"request_id": "request-1", "workflow_id": "workflow-1"}
    kwargs.update(overrides)
    return ReviewerAgent().review(result, decision, **kwargs).output


def test_consistent_result_is_accepted_without_revision() -> None:
    """一致的评分结果与决策 → ``accept``，且不得携带修订结果。"""

    output = _review(_result(), _decision())

    assert output.status is AgentStatus.SUCCESS
    assert output.error is None
    assert output.review_outcome is not None
    assert output.review_outcome.decision is ReviewDecision.ACCEPT
    assert output.review_outcome.revised_grading_result is None
    assert output.requires_review is False
    # Agent 不得输出教师人工复核结论。
    assert output.review_status is None
    assert output.grading_result is None


def test_low_confidence_accept_keeps_pending_review_for_teacher() -> None:
    """低置信度结果即使被 accept，仍保持待人工复核且不写复核状态。"""

    output = _review(
        _result(confidence=0.3, review_status="Pending Review"),
        _decision(confidence=0.3, requires_review=True, review_status="Pending Review"),
    )

    assert output.review_outcome is not None
    assert output.review_outcome.decision is ReviewDecision.ACCEPT
    assert output.requires_review is True
    assert output.review_status is None

    patch = reviewer_output_to_state_patch(
        ReviewerAgent().review(
            _result(confidence=0.3, review_status="Pending Review"),
            _decision(confidence=0.3, requires_review=True, review_status="Pending Review"),
            request_id="request-1",
        ),
        current_answer_id="answer-2",
    )
    assert set(patch) == {"pause_reason", "resumable"}
    assert str(patch["pause_reason"]).strip()
    assert patch["resumable"] is True


def test_decision_application_mismatch_is_revised_without_touching_score() -> None:
    """决策应用不一致 → ``revise``，且只改 ``review_status``。"""

    stale = _result(review_status="Pending Review")
    output = _review(stale, _decision(review_status="Not Required"))

    assert output.review_outcome is not None
    assert output.review_outcome.decision is ReviewDecision.REVISE
    revised = output.review_outcome.revised_grading_result
    assert revised is not None
    assert revised.review_status == "Not Required"

    original = stale.model_dump()
    changed = revised.model_dump()
    assert {key for key in original if original[key] != changed[key]} == {"review_status"}
    assert revised.score == stale.score
    assert revised.reason == stale.reason
    assert revised.knowledge_points == stale.knowledge_points
    assert revised.answer_id == stale.answer_id
    assert revised.submission_id == stale.submission_id


def test_revise_can_mark_result_pending_review() -> None:
    """反向用例：决策要求人工复核而结果未标注 → ``revise`` 并保持 ``requires_review=True``。"""

    output = _review(
        _result(review_status="Not Required"),
        _decision(requires_review=True, review_status="Pending Review"),
    )

    assert output.review_outcome is not None
    assert output.review_outcome.decision is ReviewDecision.REVISE
    revised = output.review_outcome.revised_grading_result
    assert revised is not None
    assert revised.review_status == "Pending Review"
    assert output.requires_review is True


def test_confidence_fact_conflict_needs_regrade() -> None:
    """置信度事实不一致属不可对齐冲突 → ``regrade``，不得带修订结果。"""

    output = _review(_result(confidence=0.9), _decision(confidence=0.4))

    assert output.review_outcome is not None
    assert output.review_outcome.decision is ReviewDecision.REGRADE
    assert output.review_outcome.revised_grading_result is None


def test_objective_result_marked_for_review_needs_regrade() -> None:
    """客观题由确定性规则评分 → 被标为待复核时转 ``regrade``，不进入人工复核（FR-030）。"""

    output = _review(
        _result(question_type=QuestionType.TRUE_FALSE, review_status="Pending Review"),
        _decision(requires_review=True, review_status="Pending Review"),
    )

    assert output.review_outcome is not None
    assert output.review_outcome.decision is ReviewDecision.REGRADE
    assert output.requires_review is False


@pytest.mark.parametrize(
    ("result", "decision"),
    [
        ({"score": 6.0}, _decision()),
        (_result(), {"confidence": 0.9}),
        (None, None),
    ],
)
def test_non_contract_inputs_are_rejected(result: object, decision: object) -> None:
    """非 ``GradingResult``/非决策快照的输入 → 脱敏失败，不产出复核结论。"""

    output = _review(result, decision)

    assert output.status is AgentStatus.FAILURE
    assert output.error is not None
    assert output.error.error_code == REVIEWER_INVALID_INPUT
    assert output.review_outcome is None


def test_unvalidated_result_is_rejected() -> None:
    """未通过结构化校验的结果不得复核。"""

    output = _review(_result(validation_status="Pending"), _decision())

    assert output.error is not None
    assert output.error.error_code == REVIEWER_RESULT_NOT_VALIDATED
    assert output.review_outcome is None


@pytest.mark.parametrize("review_status", ["Confirmed", "Modified", "Final", "Re-grade"])
def test_human_decided_review_status_is_never_overwritten(review_status: str) -> None:
    """已带教师人工结论的结果或决策一律拒绝（Agent 不得覆盖，也不得生成）。"""

    from_result = _review(_result(review_status=review_status), _decision())
    from_decision = _review(_result(), _decision(review_status=review_status))

    for output in (from_result, from_decision):
        assert output.error is not None
        assert output.error.error_code == REVIEWER_HUMAN_DECISION_PRESENT
        assert output.review_outcome is None


def test_blank_reason_is_rejected() -> None:
    """理由为空白时拒绝复核（防御性：绕过结构化校验构造的结果同样不得放行）。"""

    hollow = GradingResult.model_construct(
        **{**_result().model_dump(), "reason": "   "}
    )
    output = _review(hollow, _decision())

    assert output.error is not None
    assert output.error.error_code == REVIEWER_EMPTY_REASON
    assert output.review_outcome is None


def test_identity_mismatch_is_rejected() -> None:
    """调用方给出的题型/答案/答卷/满分与结果不一致时拒绝，不做静默修正。"""

    mismatches = (
        {"question_type": QuestionType.TRUE_FALSE},
        {"answer_id": "answer-9"},
        {"submission_id": "submission-9"},
        {"max_score": Decimal(5)},
    )
    for overrides in mismatches:
        output = _review(_result(), _decision(), **overrides)
        assert output.error is not None, overrides
        assert output.error.error_code == REVIEWER_RESULT_MISMATCH, overrides
        assert output.review_outcome is None


def test_trace_identifiers_are_preserved_and_blank_request_id_rejected() -> None:
    """追溯 ID 不得静默丢弃：原样带回，空白 ``request_id`` 拒绝。"""

    invocation = ReviewerAgent().review(
        _result(),
        _decision(),
        request_id="request-7",
        workflow_id="workflow-9",
    )
    assert invocation.request_id == "request-7"
    assert invocation.workflow_id == "workflow-9"

    standalone = ReviewerAgent().review(_result(), _decision(), request_id="request-7")
    assert standalone.workflow_id is None

    with pytest.raises(AgentInvocationError) as error:
        ReviewerAgent().review(_result(), _decision(), request_id="  ")
    assert error.value.error_code == AGENT_MISSING_REQUEST_ID


def test_review_consumes_grading_agent_output() -> None:
    """便捷入口消费 T069 输出；无评分结果的失败输出不得被当作可复核输入。"""

    accepted = ReviewerAgent().review_agent_output(
        AgentOutput(
            agent_type=AgentType.GRADING,
            status=AgentStatus.SUCCESS,
            grading_result=_result(),
            confidence_decision=_decision(),
            question_type=QuestionType.SHORT_ANSWER,
        ),
        request_id="request-1",
    )
    assert accepted.output.review_outcome is not None
    assert accepted.output.review_outcome.decision is ReviewDecision.ACCEPT

    failed = ReviewerAgent().review_agent_output(
        AgentOutput(
            agent_type=AgentType.GRADING,
            status=AgentStatus.FAILURE,
            error={"error_code": "GRADING_MISSING_CONTEXT", "message": "上下文不足。"},
        ),
        request_id="request-1",
    )
    assert failed.output.error is not None
    assert failed.output.error.error_code == REVIEWER_INVALID_INPUT


def test_state_patch_only_carries_control_fields_and_serializes() -> None:
    """状态补丁只含控制字段、可被 T065 校验序列化，且不含复核状态与分数。"""

    regrade = ReviewerAgent().review(
        _result(confidence=0.9),
        _decision(confidence=0.4),
        request_id="request-1",
        workflow_id="workflow-1",
    )
    accepted = ReviewerAgent().review(_result(), _decision(), request_id="request-1")

    regrade_patch = reviewer_output_to_state_patch(regrade, current_answer_id="answer-2")
    accepted_patch = reviewer_output_to_state_patch(accepted, current_answer_id="answer-2")

    assert set(regrade_patch) == {"pause_reason", "resumable"}
    assert regrade_patch["resumable"] is True
    # 不需要暂停时不得写出暂停字段（不自动置真、也不解除人工复核）。
    assert accepted_patch == {}

    identity = {
        "workflow_id": "workflow-1",
        "request_id": "request-1",
        "submission_id": "submission-1",
    }
    workflow_state_to_json(
        {
            **identity,
            **regrade_patch,
            "status": WorkflowStatus.PAUSED,
            "current_answer_id": "answer-2",
            "review_status": "Pending Review",
        }
    )
    workflow_state_to_json({**identity, **accepted_patch, "status": WorkflowStatus.RUNNING})

    assert "review_status" not in regrade_patch
    assert "review_status" not in accepted_patch
    assert "grading_result" not in regrade_patch

    with pytest.raises(ReviewerAgentError):
        reviewer_output_to_state_patch(regrade, current_answer_id="   ")
    with pytest.raises(ReviewerAgentError):
        reviewer_output_to_state_patch("not-an-invocation", current_answer_id="answer-2")  # type: ignore[arg-type]


def test_reviewer_module_does_not_call_llm_or_write_database() -> None:
    """复核只做决策：模块不调用 Provider、不直接使用数据库会话。"""

    import backend.app.ai.agents.reviewer_agent as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "generate_structured" not in source
    assert "sqlalchemy" not in source
    assert "asyncio" not in source
