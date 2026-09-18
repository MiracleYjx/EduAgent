"""Reviewer Agent：复核决策（T070）。

契约依据：``.specify/plan.md`` §4 Agent 分工（Reviewer 检查分数、理由与知识点并给出
``accept``/``revise``/``regrade``）、§5 状态图 ``Needs Review`` 节点、
``.specify/contracts/agent-workflow.md``、FR-030（客观题确定性、不进入人工复核）与
FR-035~FR-037（置信度阈值、待复核与教师最终确认）。

职责边界：

- **只决策，不改分**：不重算分数、不写数据库、不调用 LLM/Provider；决策严格使用 T065
  :class:`ReviewDecision` 的 ``accept``/``revise``/``regrade`` 与 :class:`ReviewerOutcome`
  （``revise`` 必带修订结果，``accept``/``regrade`` 不得带）。
- **只做有数据依据的结构校验**：不凭字段列表推断"评分错误"。本 Agent 没有标准答案、评分标准
  或学生答案等不可变证据，因此不判断语义对错，也不假装完成理由审查；分数依据是否正确最终
  由教师（T074/T077）判定。
- **只修允许的自动状态组合**：``revise`` 用 ``model_copy`` 只对齐 ``review_status``，分数、理由、
  知识点与身份字段逐字段保持输入值；置信度事实不一致属无法用状态对齐修复的冲突，转 ``regrade``。
- **不代替教师**：不生成 ``Confirmed``/``Modified``/``Final``/``Re-grade``，不解除人工复核，
  不把 ``accept`` 当作教师确认；已带人工结论或未通过结构化校验的输入一律拒绝。
- **暂停语义**：:func:`reviewer_output_to_state_patch` 只产出 ``pause_reason``/``resumable``
  控制字段；``regrade`` 后的重新调度、``ExamResult`` 重新汇总与诊断门禁属 T072/T074。
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Final

from backend.app.ai.agents.invocation import AgentInvocation, normalize_trace_context
from backend.app.ai.agents.state import (
    AgentError,
    AgentOutput,
    AgentStatus,
    AgentType,
    ReviewDecision,
    ReviewerOutcome,
)
from backend.app.domain.enums import (
    GradingMode,
    QuestionType,
    ReviewStatus,
    ValidationStatus,
)
from backend.app.schemas.ai import GradingResult
from backend.app.schemas.grading import ConfidenceDecisionDTO
from backend.app.services.grading.confidence_policy import HUMAN_DECIDED_REVIEW_STATES
from backend.app.services.grading.question_router import (
    QuestionRouter,
    normalize_question_type,
)

#: 复核输入不是评分结果与置信度决策快照，或题型无法分流。
REVIEWER_INVALID_INPUT: Final[str] = "REVIEWER_INVALID_INPUT"
#: 评分结果未通过结构化校验，不得进入复核。
REVIEWER_RESULT_NOT_VALIDATED: Final[str] = "REVIEWER_RESULT_NOT_VALIDATED"
#: 评分结果的身份字段（题型/答案/答卷/满分）与调用方给出的目标不一致。
REVIEWER_RESULT_MISMATCH: Final[str] = "REVIEWER_RESULT_MISMATCH"
#: 评分理由为空白，无法复核（防御性检查：结构化校验应已拒绝）。
REVIEWER_EMPTY_REASON: Final[str] = "REVIEWER_EMPTY_REASON"
#: 知识点列表不合法（非文本、空白元素或非法的题目声明）。
REVIEWER_INVALID_KNOWLEDGE_POINTS: Final[str] = "REVIEWER_INVALID_KNOWLEDGE_POINTS"
#: 结果或决策已带教师人工复核结论，Agent 不得覆盖。
REVIEWER_HUMAN_DECISION_PRESENT: Final[str] = "REVIEWER_HUMAN_DECISION_PRESENT"

#: 允许自动对齐的复核状态；教师人工结论（T074/T077）不在其中。
AUTOMATIC_REVIEW_STATES: Final[frozenset[str]] = frozenset(
    {
        ReviewStatus.NOT_REQUIRED.value,
        ReviewStatus.PENDING_REVIEW.value,
    }
)


class ReviewerAgentError(RuntimeError):
    """复核 Agent 边界的调用方错误；保留脱敏错误码。"""

    #: 脱敏错误码。
    error_code: str = REVIEWER_INVALID_INPUT


def _blank(value: object) -> bool:
    """判断文本是否为空白；非文本一律视为空白（防御性）。"""

    return not isinstance(value, str) or not value.strip()


def _invalid_items(values: object) -> bool:
    """判断列表是否含非文本或空白元素；不可迭代同样视为非法。"""

    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        return True
    return any(_blank(item) for item in values)


def _normalized(values: Sequence[str]) -> list[str]:
    """把要点列表归一为去空白的文本列表，便于逐项比较。"""

    return [item.strip() for item in values]


def _same_score(left: object, right: object) -> bool:
    """比较满分是否等价；两侧都必须能转成十进制数值。"""

    try:
        return Decimal(str(left)) == Decimal(str(right))
    except (ArithmeticError, ValueError):
        return False


class ReviewerAgent:
    """复核 Agent：按有数据依据的结构规则给出 ``accept``/``revise``/``regrade``。

    :param router: 题型分流器；``None`` 时复用 M3 :class:`QuestionRouter`（FR-030 客观题口径）。
    """

    def __init__(self, *, router: QuestionRouter | None = None) -> None:
        self._router = router if router is not None else QuestionRouter()

    # ------------------------------------------------------------------ 公开入口

    def review(
        self,
        grading_result: object,
        confidence_decision: object,
        *,
        request_id: str,
        workflow_id: str | None = None,
        question_type: QuestionType | str | None = None,
        answer_id: str | None = None,
        submission_id: str | None = None,
        max_score: float | Decimal | None = None,
        knowledge_points: Sequence[str] | None = None,
    ) -> AgentInvocation:
        """复核单题评分结果，返回带追溯标识的 :class:`AgentInvocation`。

        ``question_type``/``answer_id``/``submission_id``/``max_score`` 用于交叉校验结果身份；
        未给出的字段不做比较（不推断、不补默认值）。``knowledge_points`` 为题目声明的知识点，
        用于校验结果的要点列表是否越出声明范围；未给出时不做该项校验。
        """

        request_id, workflow_id = normalize_trace_context(request_id, workflow_id)
        return AgentInvocation(
            request_id=request_id,
            workflow_id=workflow_id,
            output=self._decide(
                grading_result,
                confidence_decision,
                question_type=question_type,
                answer_id=answer_id,
                submission_id=submission_id,
                max_score=max_score,
                knowledge_points=knowledge_points,
            ),
        )

    def review_agent_output(
        self,
        output: object,
        *,
        request_id: str,
        workflow_id: str | None = None,
    ) -> AgentInvocation:
        """便捷入口：直接消费 T069 阅卷 Agent 的输出（``grading_result``+``confidence_decision``）。

        T069 的 ``AgentOutput`` 不含 ``max_score``/``answer_id``/``submission_id`` 的独立副本，
        因此本入口不伪造这些目标值，只用输出自带的题型做交叉校验。
        """

        request_id, workflow_id = normalize_trace_context(request_id, workflow_id)
        if not isinstance(output, AgentOutput):
            return AgentInvocation(
                request_id=request_id,
                workflow_id=workflow_id,
                output=self._failure(
                    REVIEWER_INVALID_INPUT,
                    "复核输入必须是阅卷 Agent 的结构化输出。",
                ),
            )
        return self.review(
            output.grading_result,
            output.confidence_decision,
            request_id=request_id,
            workflow_id=workflow_id,
            question_type=output.question_type,
        )

    # ------------------------------------------------------------------ 决策规则

    def _decide(
        self,
        grading_result: object,
        confidence_decision: object,
        *,
        question_type: QuestionType | str | None,
        answer_id: str | None,
        submission_id: str | None,
        max_score: float | Decimal | None,
        knowledge_points: Sequence[str] | None = None,
    ) -> AgentOutput:
        """按"异常输入 → regrade → revise → accept"的优先级给出决策。"""

        if not isinstance(grading_result, GradingResult) or not isinstance(
            confidence_decision, ConfidenceDecisionDTO
        ):
            return self._failure(
                REVIEWER_INVALID_INPUT,
                "复核输入必须是已校验的单题评分结果与置信度决策快照。",
            )
        result = grading_result
        decision = confidence_decision

        if result.validation_status != ValidationStatus.VALIDATED.value:
            return self._failure(
                REVIEWER_RESULT_NOT_VALIDATED,
                f"评分结果未通过结构化校验（validation_status={result.validation_status}）。",
            )
        if _blank(result.reason):
            return self._failure(
                REVIEWER_EMPTY_REASON,
                "评分理由为空白，缺少可复核的依据说明。",
            )
        if not self._knowledge_points_usable(result, declared=knowledge_points):
            return self._failure(
                REVIEWER_INVALID_KNOWLEDGE_POINTS,
                "命中的要点、缺失的要点或题目声明的知识点含非法元素，无法复核。",
            )
        if (
            result.review_status in HUMAN_DECIDED_REVIEW_STATES
            or decision.review_status in HUMAN_DECIDED_REVIEW_STATES
        ):
            return self._failure(
                REVIEWER_HUMAN_DECISION_PRESENT,
                "评分结果或置信度决策已带教师人工复核结论，Agent 不得覆盖。",
            )
        mismatch = self._identity_mismatch(
            result,
            question_type=question_type,
            answer_id=answer_id,
            submission_id=submission_id,
            max_score=max_score,
        )
        if mismatch is not None:
            return mismatch

        mode = self._resolve_mode(result)
        if mode is None:
            return self._failure(
                REVIEWER_INVALID_INPUT,
                "题型无法分流，无法判断是否属于确定性客观题复核范围。",
            )

        # 事实不一致：置信度与决策快照不同，或客观题被错误标为待人工复核（FR-030）。
        if result.confidence != decision.confidence:
            return self._success(
                result,
                ReviewerOutcome(
                    decision=ReviewDecision.REGRADE,
                    reason=(
                        "评分结果记录的置信度与本次置信度决策不一致，"
                        "无法用复核状态对齐修复，需要重新评分。"
                    ),
                ),
                requires_review=bool(decision.requires_review),
            )
        if mode is GradingMode.OBJECTIVE and decision.requires_review:
            return self._success(
                result,
                ReviewerOutcome(
                    decision=ReviewDecision.REGRADE,
                    reason=(
                        "客观题由确定性规则评分，不应进入人工复核，"
                        "需要按规则重新评分而不是等待教师。"
                    ),
                ),
                requires_review=False,
            )

        conflict = self._knowledge_point_conflict(result, declared=knowledge_points)
        if conflict is not None:
            return self._success(
                result,
                ReviewerOutcome(decision=ReviewDecision.REGRADE, reason=conflict),
                requires_review=bool(decision.requires_review),
            )

        # 决策应用不一致：只对齐允许的自动复核状态，不改分数、理由与知识点。
        if result.review_status != decision.review_status:
            if decision.review_status not in AUTOMATIC_REVIEW_STATES:
                return self._failure(
                    REVIEWER_HUMAN_DECISION_PRESENT,
                    "置信度决策携带教师人工复核结论，Agent 不得写入该状态。",
                )
            revised = result.model_copy(update={"review_status": decision.review_status})
            return self._success(
                result,
                ReviewerOutcome(
                    decision=ReviewDecision.REVISE,
                    reason=(
                        "评分结果携带的复核状态与本次置信度决策不一致，"
                        "已按决策对齐复核状态，分数与理由保持原值。"
                    ),
                    revised_grading_result=revised,
                ),
                requires_review=bool(decision.requires_review),
            )

        return self._success(
            result,
            ReviewerOutcome(
                decision=ReviewDecision.ACCEPT,
                reason=(
                    "评分结果已通过结构化校验，且置信度、复核状态与决策一致，"
                    "未发现可验证的结构冲突；语义正确性仍由教师判定。"
                ),
            ),
            requires_review=bool(decision.requires_review),
        )

    @staticmethod
    def _knowledge_points_usable(
        result: GradingResult,
        *,
        declared: Sequence[str] | None,
    ) -> bool:
        """知识点列表必须是文本列表且不含空白项；声明的知识点同样要求。"""

        for values in (
            result.knowledge_points,
            result.correct_points,
            result.missing_knowledge_points,
        ):
            if _invalid_items(values):
                return False
        return declared is None or not _invalid_items(declared)

    @staticmethod
    def _knowledge_point_conflict(
        result: GradingResult,
        *,
        declared: Sequence[str] | None,
    ) -> str | None:
        """只校验可验证的字段不变量；不做语义判定，也不推断应得分数。

        不变量：同一要点不得既命中又缺失；要点列表不得重复；声明的知识点范围内不得越出。
        命中任一违反即视为事实自相矛盾，且 Reviewer 不改分，因此交由 ``regrade`` 重新评分。
        """

        hits = _normalized(result.correct_points)
        misses = _normalized(result.missing_knowledge_points)
        combined = hits + misses
        overlaps = sorted(set(hits) & set(misses))
        if overlaps:
            return (
                "命中的要点与缺失的要点存在交集，评分依据自相矛盾，"
                "无法用复核状态对齐修复，需要重新评分。"
            )
        duplicates = sorted({item for item in combined if combined.count(item) > 1})
        if duplicates:
            return (
                "要点列表存在重复项，评分依据不可信，需要重新评分。"
            )
        if declared is not None:
            allowed = set(_normalized(declared))
            outside = sorted({item for item in combined if item not in allowed})
            if outside:
                return (
                    "要点列表包含题目未声明的知识点，评分依据越出本题范围，需要重新评分。"
                )
        return None

    def _resolve_mode(self, result: GradingResult) -> GradingMode | None:
        """复用 M3 分流器判断客观/主观；无法分流时返回 ``None``（不猜测）。"""

        try:
            return self._router.route_type(normalize_question_type(result.question_type))
        except (TypeError, ValueError):
            return None

    def _identity_mismatch(
        self,
        result: GradingResult,
        *,
        question_type: QuestionType | str | None,
        answer_id: str | None,
        submission_id: str | None,
        max_score: float | Decimal | None,
    ) -> AgentOutput | None:
        """只校验调用方显式给出的目标字段；不一致即拒绝，不做静默修正。"""

        mismatched = (
            question_type is not None and str(result.question_type) != str(question_type)
        ) or (answer_id is not None and result.answer_id != answer_id)
        mismatched = mismatched or (
            submission_id is not None and result.submission_id != submission_id
        )
        mismatched = mismatched or (
            max_score is not None and not _same_score(result.max_score, max_score)
        )
        if not mismatched:
            return None
        return self._failure(
            REVIEWER_RESULT_MISMATCH,
            "评分结果的题型、答案、答卷或满分与复核目标不一致，已拒绝复核该结果。",
        )

    # ------------------------------------------------------------------ 输出构造

    def _success(
        self,
        result: GradingResult,
        outcome: ReviewerOutcome,
        *,
        requires_review: bool,
    ) -> AgentOutput:
        """构造复核成功输出；不写 ``review_status``，也不代替教师确认。"""

        return AgentOutput(
            agent_type=AgentType.REVIEWER,
            status=AgentStatus.SUCCESS,
            summary=(
                f"第 {result.answer_id} 题复核完成：{outcome.decision.value}；"
                f"是否需要人工复核：{requires_review}。"
            ),
            question_type=result.question_type,
            requires_review=requires_review,
            review_outcome=outcome,
        )

    def _failure(self, code: str, detail: str) -> AgentOutput:
        """构造脱敏失败输出；失败不得同时声明需要人工复核。"""

        return AgentOutput(
            agent_type=AgentType.REVIEWER,
            status=AgentStatus.FAILURE,
            error=AgentError(error_code=code, message=detail, retryable=False),
        )


def reviewer_output_to_state_patch(
    invocation: AgentInvocation,
    *,
    current_answer_id: str,
) -> dict[str, object]:
    """把复核输出映射为 T065 ``GradingWorkflowState`` 的流程控制补丁。

    只写 ``pause_reason``/``resumable``：``regrade`` 或待人工复核时暂停且可恢复；其余情况不写
    字段（不解除、也不代写人工结论）。**不写** ``review_status``、分数与身份槽位；
    ``GradingWorkflowState`` 的写入与检查点持久化属 T072/T073。
    """

    if not isinstance(invocation, AgentInvocation):
        raise ReviewerAgentError("复核状态补丁需要 AgentInvocation。")
    if _blank(current_answer_id):
        raise ReviewerAgentError("复核状态补丁需要当前题目的答案标识。")
    outcome = invocation.output.review_outcome
    if outcome is None:  # pragma: no cover - 失败输出没有复核结论
        return {}
    if outcome.decision is ReviewDecision.REGRADE:
        return {
            "pause_reason": f"第 {current_answer_id} 题需要重新评分，等待重新调度阅卷。",
            "resumable": True,
        }
    if invocation.output.requires_review:
        return {
            "pause_reason": f"第 {current_answer_id} 题的评分结果等待教师复核。",
            "resumable": True,
        }
    return {}


__all__ = [
    "AUTOMATIC_REVIEW_STATES",
    "REVIEWER_EMPTY_REASON",
    "REVIEWER_HUMAN_DECISION_PRESENT",
    "REVIEWER_INVALID_INPUT",
    "REVIEWER_INVALID_KNOWLEDGE_POINTS",
    "REVIEWER_RESULT_MISMATCH",
    "REVIEWER_RESULT_NOT_VALIDATED",
    "ReviewerAgent",
    "ReviewerAgentError",
    "reviewer_output_to_state_patch",
]
