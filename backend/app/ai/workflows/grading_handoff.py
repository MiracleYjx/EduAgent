"""阅卷工作流的节点与状态交接契约（H04）。

契约依据：``.specify/plan.md`` §5（状态图与控制逻辑）、``.specify/contracts/agent-workflow.md``
（Grading Workflow States / Required State / Contract Rules）、FR-029（题型分流）、FR-030（客观题
确定性）、FR-035/FR-036（阈值、待复核与教师确认）与 FR-038（诊断只消费已确认结果）。

职责边界：

- **不实现 LangGraph 图**（T072）：本模块只固定节点 id/顺序、条件边与 Agent 输出到
  :class:`~backend.app.ai.workflows.state.GradingWorkflowState` 的交接映射，供 T071 锁定、T072 消费。
- **不复制门槛判定**：整卷收尾与诊断门槛继续由 T066 ``SupervisorAgent`` 与 M3 ``DiagnosisService``
  （要求 ``exam_result.is_final``）负责；本模块只提供逐题提交与整卷汇总的状态增量。
- **不写教师结论**：交接映射只写自动决策，绝不写 ``Confirmed``/``Modified``/``Final``/``Re-grade``。
- **不重复评分**：汇总调用既有 M3 ``ResultAggregator.aggregate``（每次调用只执行一次），逐题结果必须
  覆盖答卷全部题目，缺题即失败，不返回部分成功。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from backend.app.ai.agents.grading_agent import grading_output_to_state_patch
from backend.app.ai.agents.invocation import AgentInvocation
from backend.app.ai.agents.reviewer_agent import reviewer_output_to_state_patch
from backend.app.ai.agents.state import (
    ReviewDecision,
    confidence_decision_from_snapshot,
)
from backend.app.domain.enums import (
    GradingMode,
    QuestionType,
    ReviewStatus,
    WorkflowStatus,
)
from backend.app.schemas.ai import GradingResult
from backend.app.schemas.grading import ConfidenceDecisionDTO
from backend.app.services.grading.grading_task_service import SubmissionSnapshot
from backend.app.services.grading.question_router import (
    GradingRoutingError,
    QuestionRouter,
    normalize_question_type,
)
from backend.app.services.grading.result_aggregator import ResultAggregator

#: 交接输入不合法（缺少身份字段、题序或逐题结果不完整）。
GRADING_HANDOFF_INVALID_INPUT: Final[str] = "GRADING_HANDOFF_INVALID_INPUT"

# --- 节点 id（`plan.md` §5 的机读形式；标签见 ``NODE_LABELS``） ---
LOAD_SUBMISSION: Final[str] = "load_submission"
CLASSIFY_QUESTION: Final[str] = "classify_question"
OBJECTIVE_RULE_GRADE: Final[str] = "objective_rule_grade"
SUBJECTIVE_RETRIEVE_GRADE: Final[str] = "subjective_retrieve_grade"
STRUCTURED_VALIDATION: Final[str] = "structured_validation"
CONFIDENCE_CHECK: Final[str] = "confidence_check"
ACCEPT: Final[str] = "accept"
PENDING_REVIEW: Final[str] = "pending_review"
REVIEWER_AGENT: Final[str] = "reviewer_agent"
REGRADE: Final[str] = "regrade"
NEXT_ANSWER: Final[str] = "next_answer"
UNIFIED_RESULT: Final[str] = "unified_result"
GENERATE_DIAGNOSIS: Final[str] = "generate_diagnosis"

#: 节点顺序（`plan.md` §5 的纵向主线；条件边另见 ``CLASSIFY_EDGES``）。
WORKFLOW_NODE_ORDER: Final[tuple[str, ...]] = (
    LOAD_SUBMISSION,
    CLASSIFY_QUESTION,
    OBJECTIVE_RULE_GRADE,
    SUBJECTIVE_RETRIEVE_GRADE,
    STRUCTURED_VALIDATION,
    CONFIDENCE_CHECK,
    ACCEPT,
    PENDING_REVIEW,
    REVIEWER_AGENT,
    REGRADE,
    NEXT_ANSWER,
    UNIFIED_RESULT,
    GENERATE_DIAGNOSIS,
)

#: 节点标签：与 `plan.md` §5 图上的文字逐字对应，便于报告与日志直接引用。
NODE_LABELS: Final[Mapping[str, str]] = {
    LOAD_SUBMISSION: "Load Submission",
    CLASSIFY_QUESTION: "Classify Question",
    OBJECTIVE_RULE_GRADE: "Objective Rule Grade",
    SUBJECTIVE_RETRIEVE_GRADE: "Subjective Retrieve/Grade",
    STRUCTURED_VALIDATION: "Structured Validation",
    CONFIDENCE_CHECK: "Confidence Check",
    ACCEPT: "Accept",
    PENDING_REVIEW: "Pending Review",
    REVIEWER_AGENT: "Reviewer Agent",
    REGRADE: "Re-grade",
    NEXT_ANSWER: "Next Answer",
    UNIFIED_RESULT: "Unified Result",
    GENERATE_DIAGNOSIS: "Generate Diagnosis",
}

#: `Classify Question` 的条件边：客观题走确定性规则，主观题走检索与评分（FR-029/FR-030）。
CLASSIFY_EDGES: Final[Mapping[GradingMode, str]] = {
    GradingMode.OBJECTIVE: OBJECTIVE_RULE_GRADE,
    GradingMode.SUBJECTIVE: SUBJECTIVE_RETRIEVE_GRADE,
}

#: 允许进入 `final_results` 与诊断的复核状态：自动接受或教师结论（`Re-grade`/待复核除外）。
ACCEPTED_REVIEW_STATES: Final[frozenset[str]] = frozenset(
    {
        ReviewStatus.NOT_REQUIRED.value,
        ReviewStatus.CONFIRMED.value,
        ReviewStatus.MODIFIED.value,
        ReviewStatus.FINAL.value,
    }
)

#: 失败输出的题型不可用时的保守节点：上下文/Provider 失败只发生在主观链路。
FALLBACK_FAILURE_NODE: Final[str] = SUBJECTIVE_RETRIEVE_GRADE


class GradingHandoffError(RuntimeError):
    """交接映射的调用方错误；保留脱敏错误码。"""

    error_code: str = GRADING_HANDOFF_INVALID_INPUT


def _required(value: object, label: str) -> str:
    """校验必填文本；空白或非文本显式失败，不静默丢弃或生成占位值。"""

    if not isinstance(value, str) or not value.strip():
        raise GradingHandoffError(f"工作流交接缺少 {label}，拒绝写入不完整状态。")
    return value.strip()


def regrade_target_node(question_type: QuestionType | str | None) -> str:
    """返回重新评分应回到的节点：客观题 → 规则评分，主观题 → 检索与评分。

    复用 M3 ``QuestionRouter`` 的分流口径；题型缺失或无法识别时显式失败，不猜测节点。
    """

    try:
        mode = QuestionRouter().route_type(normalize_question_type(question_type))
    except (TypeError, ValueError, GradingRoutingError) as error:
        raise GradingHandoffError("题型无法分流，无法确定重新评分的节点。") from error
    return CLASSIFY_EDGES[mode]


def may_enter_final_results(
    grading_result: GradingResult,
    *,
    decision: ConfidenceDecisionDTO | None = None,
) -> bool:
    """判断单题结果是否可以进入 `final_results` 与诊断。

    以**权威复核状态**为准：给出置信度决策时使用决策快照的 ``review_status``（本次实际决策），
    否则使用结果自身的 ``review_status``。待复核与 ``Re-grade`` 一律不予放行。
    """

    if not isinstance(grading_result, GradingResult):
        raise GradingHandoffError("判定最终成绩需要已校验的 GradingResult。")
    authoritative = (
        decision.review_status
        if isinstance(decision, ConfidenceDecisionDTO)
        else grading_result.review_status
    )
    return authoritative in ACCEPTED_REVIEW_STATES


def diagnostic_status_source(state: Mapping[str, Any]) -> bool:
    """诊断是否可用：只有已最终确认且 `final_results` 与整卷明细同源时才是就绪的。"""

    exam_result = state.get("exam_result")
    if exam_result is None:
        return False
    final_results = state.get("final_results")
    if not isinstance(final_results, Sequence) or isinstance(final_results, (str, bytes)):
        return False
    return bool(exam_result.is_final) and list(final_results) == list(exam_result.items)


def diagnosis_allowed(state: Mapping[str, Any]) -> bool:
    """`Generate Diagnosis` 节点的准入门槛（与 M3 ``DiagnosisService`` 的 ``is_final`` 口径一致）。"""

    return diagnostic_status_source(state)


def grading_handoff(
    invocation: AgentInvocation,
    *,
    submission_id: str,
    current_answer_order: int,
    current_node: str | None = None,
) -> dict[str, object]:
    """把 T069 逐题输出映射为工作流状态增量（身份 + 逐题槽位 + 运行控制）。

    - 失败：``Failed`` + ``error``，**不带** ``resumable``（失败不得自动置真）；
    - 待复核：``Paused`` + ``pause_reason`` + ``resumable=True``（等待教师复核，可恢复）；
    - 其余：``Running`` 并推进到 ``Accept``。

    ``current_node`` 未给出时按输出推导：失败回到该题的评分节点，待复核停在 ``Pending Review``，
    其余进入 ``Accept``。
    """

    if not isinstance(invocation, AgentInvocation):
        raise GradingHandoffError("阅卷交接需要 AgentInvocation。")
    workflow_id = _required(invocation.workflow_id, "workflow_id")
    submission = _required(submission_id, "submission_id")
    if isinstance(current_answer_order, bool) or not isinstance(current_answer_order, int):
        raise GradingHandoffError("题序必须是 >= 1 的整数。")
    if current_answer_order < 1:
        raise GradingHandoffError("题序必须是 >= 1 的整数。")

    output = invocation.output
    failed = output.error is not None
    if failed:
        status = WorkflowStatus.FAILED
    elif output.requires_review:
        status = WorkflowStatus.PAUSED
    else:
        status = WorkflowStatus.RUNNING
    node = current_node if current_node is not None else _grading_node(output, failed=failed)

    patch: dict[str, object] = {
        "workflow_id": workflow_id,
        "request_id": invocation.request_id,
        "submission_id": submission,
        "status": status,
        "current_node": node,
    }
    patch.update(grading_output_to_state_patch(invocation, current_answer_order=current_answer_order))
    return patch


def _grading_node(output: Any, *, failed: bool) -> str:
    """按阅卷输出推导所在节点；失败时回到该题的评分节点。"""

    if not failed:
        return PENDING_REVIEW if output.requires_review else ACCEPT
    try:
        return regrade_target_node(output.question_type)
    except GradingHandoffError:
        return FALLBACK_FAILURE_NODE


def reviewer_handoff(
    invocation: AgentInvocation,
    *,
    submission_id: str,
    current_answer_id: str,
) -> dict[str, object]:
    """把 T070 复核输出映射为工作流状态增量（身份 + 流程控制）。

    ``regrade`` 或仍需人工复核时暂停且可恢复；其余情况只回到 ``Running``。**不写**复核状态、
    分数与身份槽位（教师结论由 T074/T077 写入）。
    """

    if not isinstance(invocation, AgentInvocation):
        raise GradingHandoffError("复核交接需要 AgentInvocation。")
    workflow_id = _required(invocation.workflow_id, "workflow_id")
    submission = _required(submission_id, "submission_id")
    answer_id = _required(current_answer_id, "current_answer_id")

    outcome = invocation.output.review_outcome
    decision = outcome.decision if outcome is not None else None
    needs_pause = decision is ReviewDecision.REGRADE or invocation.output.requires_review
    patch: dict[str, object] = {
        "workflow_id": workflow_id,
        "request_id": invocation.request_id,
        "submission_id": submission,
        "status": WorkflowStatus.PAUSED if needs_pause else WorkflowStatus.RUNNING,
        "current_node": REGRADE if decision is ReviewDecision.REGRADE else REVIEWER_AGENT,
    }
    patch.update(reviewer_output_to_state_patch(invocation, current_answer_id=answer_id))
    return patch


def regrade_resume_state(state: Mapping[str, Any]) -> dict[str, object]:
    """按原身份恢复被 ``regrade`` 暂停的工作流：只改运行控制，不动身份与结果集合。"""

    if not isinstance(state, Mapping):
        raise GradingHandoffError("恢复状态需要工作流状态映射。")
    resumed: dict[str, object] = {
        "workflow_id": _required(state.get("workflow_id"), "workflow_id"),
        "request_id": _required(state.get("request_id"), "request_id"),
        "submission_id": _required(state.get("submission_id"), "submission_id"),
        "status": WorkflowStatus.RUNNING,
        "pause_reason": None,
        "resumable": False,
    }
    current_node = state.get("current_node")
    if isinstance(current_node, str) and current_node.strip():
        resumed["current_node"] = current_node
    return resumed


def unified_result_patch(
    state: Mapping[str, Any],
    snapshot: SubmissionSnapshot,
    *,
    aggregator: Any | None = None,
) -> dict[str, object]:
    """`Unified Result` 节点：把逐题槽位汇总为整卷结果（``aggregate`` 只调用一次）。

    逐题结果必须覆盖答卷全部题目且标识一致，缺题或多余都显式失败，不返回部分成功；
    只有 ``exam_result.is_final`` 为真时才写出 ``final_results``，且与 ``exam_result.items`` 同源
    （T065 要求两者一致，不得出现两份版本）。
    """

    if not isinstance(state, Mapping):
        raise GradingHandoffError("汇总需要工作流状态映射。")
    if not isinstance(snapshot, SubmissionSnapshot):
        raise GradingHandoffError("汇总需要答卷快照。")
    results = state.get("grading_results")
    if not isinstance(results, Mapping):
        raise GradingHandoffError("汇总缺少逐题结果集合 grading_results。")

    expected = [target.answer_id for target in snapshot.answers]
    missing = [answer_id for answer_id in expected if answer_id not in results]
    unexpected = [answer_id for answer_id in results if answer_id not in set(expected)]
    if missing or unexpected:
        raise GradingHandoffError(
            "逐题结果与答卷题目集合不一致，拒绝汇总部分成绩。"
        )

    ordered: list[GradingResult] = []
    for answer_id in expected:
        item = results[answer_id]
        if not isinstance(item, GradingResult):
            raise GradingHandoffError("逐题结果必须是已校验的 GradingResult。")
        ordered.append(item)

    raw_decisions = state.get("confidence_decisions") or {}
    if not isinstance(raw_decisions, Mapping):
        raise GradingHandoffError("汇总的置信度决策集合必须是映射。")
    decisions = {
        str(answer_id): confidence_decision_from_snapshot(dto)
        for answer_id, dto in raw_decisions.items()
        if isinstance(dto, ConfidenceDecisionDTO)
    }

    active = aggregator if aggregator is not None else ResultAggregator()
    exam_result = active.aggregate(
        snapshot.to_context(),
        results=ordered,
        decisions=decisions,
    )
    patch: dict[str, object] = {"exam_result": exam_result}
    if exam_result.is_final:
        patch["final_results"] = list(exam_result.items)
    return patch


__all__ = [
    "ACCEPT",
    "ACCEPTED_REVIEW_STATES",
    "CLASSIFY_EDGES",
    "CLASSIFY_QUESTION",
    "CONFIDENCE_CHECK",
    "GENERATE_DIAGNOSIS",
    "GRADING_HANDOFF_INVALID_INPUT",
    "LOAD_SUBMISSION",
    "NEXT_ANSWER",
    "NODE_LABELS",
    "OBJECTIVE_RULE_GRADE",
    "PENDING_REVIEW",
    "REGRADE",
    "REVIEWER_AGENT",
    "STRUCTURED_VALIDATION",
    "SUBJECTIVE_RETRIEVE_GRADE",
    "UNIFIED_RESULT",
    "WORKFLOW_NODE_ORDER",
    "GradingHandoffError",
    "diagnosis_allowed",
    "grading_handoff",
    "may_enter_final_results",
    "regrade_resume_state",
    "regrade_target_node",
    "reviewer_handoff",
    "unified_result_patch",
]
