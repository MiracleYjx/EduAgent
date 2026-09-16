"""T054 Objective/Subjective 结果汇总与最终分数计算。

契约依据：FR-033（客观与主观结果汇总为统一考试结果）、FR-034（只有通过校验的结果进入
统一结果）、FR-035～FR-037（置信度分流与复核后的最终评分）、plan.md §5.1/§5.2、
data-model.md 的 GradingResult 状态图。

状态判定表（逐题 → 整卷）：

| 单题输入状态 | 单题判定 | 是否计入总分 | 整卷影响 |
| --- | --- | --- | --- |
| 缺结果 | ``grading_status=Pending`` | 否 | ``Pending``，写入 ``missing_answer_ids`` |
| 客观题且 ``Validated`` | ``Accepted`` | 是 | 正常计入 |
| 主观题 ``Validated`` 且已记录自动接受决策 | ``Accepted`` | 是 | 正常计入 |
| 主观题 ``Pending Review`` | ``Pending Review`` | 否 | 整卷 ``Pending Review``，不给出最终总分 |
| 人工 ``Confirmed``/``Modified``/``Final`` | ``Final`` | 是（使用回写后的正式得分） | 旧的低置信度决策被忽略 |
| ``Re-grade`` | ``Pending`` | 否 | 整卷 ``Pending``，等待重新评分 |
| ``ValidationStatus=Failed`` | ``Failed`` | 否 | 整卷 ``Failed``（优先级最高） |
| 未知复核状态 | 拒绝 | — | 抛 ``GRADING_UNKNOWN_STATE`` |

整卷 ``result_status`` 优先次序：``Failed`` > ``Pending Review`` > ``Pending`` > ``Final``。
只有全部预期题目均为最终接受结果时才给出 ``final_total_score``；未完成时该字段为空，已确认
部分只写入独立的 ``confirmed_subtotal``。

精度：逐题得分经 ``Decimal(str(value))`` 转换后**不中途舍入**地累加，仅在出口按
``ROUND_HALF_UP`` 量化到两位小数（与 ``Question.score`` 的 ``Numeric(8, 2)`` 对齐）。
汇总过程不调用任何 LLM Provider，也不读取运行配置重新判定置信度。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import ClassVar, Final

from backend.app.domain.enums import (
    OBJECTIVE_QUESTION_TYPES,
    ReviewStatus,
    ValidationStatus,
)
from backend.app.schemas.ai import GradingResult
from backend.app.schemas.grading import (
    ConfidenceDecisionDTO,
    ExamResultDTO,
    ExamResultStatus,
    ExpectedAnswer,
    QuestionResultDTO,
    SubmissionContext,
)
from backend.app.services.grading.confidence_policy import ConfidenceDecision

#: 汇总失败的通用错误码。
GRADING_AGGREGATION_FAILED: Final[str] = "GRADING_AGGREGATION_FAILED"
#: 预期题目集合为空。
GRADING_AGGREGATION_EMPTY_EXPECTED: Final[str] = "GRADING_AGGREGATION_EMPTY_EXPECTED"
#: 同一题出现重复结果或预期集合本身重复。
GRADING_DUPLICATE_RESULT: Final[str] = "GRADING_DUPLICATE_RESULT"
#: 结果中出现预期集合之外的答案标识。
GRADING_UNEXPECTED_RESULT: Final[str] = "GRADING_UNEXPECTED_RESULT"
#: 结果属于其它答卷。
GRADING_SUBMISSION_MISMATCH: Final[str] = "GRADING_SUBMISSION_MISMATCH"
#: 结果缺少答卷或答案关联，无法证明归属。
GRADING_IDENTITY_MISSING: Final[str] = "GRADING_IDENTITY_MISSING"
#: 得分或满分与题目定义不一致。
GRADING_SCORE_INVALID: Final[str] = "GRADING_SCORE_INVALID"
#: 结果题型与题目定义不一致。
GRADING_QUESTION_TYPE_MISMATCH: Final[str] = "GRADING_QUESTION_TYPE_MISMATCH"
#: 复核状态未知，或主观题缺少可信的置信度决策。
GRADING_UNKNOWN_STATE: Final[str] = "GRADING_UNKNOWN_STATE"

#: 输出分数的量化精度。
SCORE_QUANTUM: Final[Decimal] = Decimal("0.01")

#: 已确认部分小计的展示文案，避免与最终总分混淆。
CONFIRMED_SUBTOTAL_LABEL: Final[str] = (
    "已确认部分小计（不含待复核、未完成与失败题目），不得作为最终成绩。"
)

#: 已有人工最终结论的复核状态：自动决策不得覆盖，得分直接计入。
_HUMAN_FINAL_REVIEW_STATES: Final[frozenset[str]] = frozenset(
    {
        ReviewStatus.CONFIRMED.value,
        ReviewStatus.MODIFIED.value,
        ReviewStatus.FINAL.value,
    }
)
#: 等待人工复核的复核状态。
_PENDING_REVIEW_STATES: Final[frozenset[str]] = frozenset(
    {ReviewStatus.PENDING_REVIEW.value}
)
#: 等待重新评分的复核状态。
_REGRADE_STATES: Final[frozenset[str]] = frozenset({ReviewStatus.RE_GRADE.value})


class GradingAggregationError(RuntimeError):
    """汇总失败基类；全部按不可重试的业务问题处理。"""

    error_code: ClassVar[str] = GRADING_AGGREGATION_FAILED
    retryable: ClassVar[bool] = False

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.error_code}：{detail}")
        self.detail = detail


class EmptyExpectedAnswersError(GradingAggregationError):
    """预期题目集合为空，无法汇总成绩。"""

    error_code: ClassVar[str] = GRADING_AGGREGATION_EMPTY_EXPECTED


class DuplicateResultError(GradingAggregationError):
    """同一题出现重复结果，或预期集合自身重复。"""

    error_code: ClassVar[str] = GRADING_DUPLICATE_RESULT


class UnexpectedResultError(GradingAggregationError):
    """结果包含预期集合之外的答案标识。"""

    error_code: ClassVar[str] = GRADING_UNEXPECTED_RESULT


class SubmissionMismatchError(GradingAggregationError):
    """结果与本次汇总的答卷不一致。"""

    error_code: ClassVar[str] = GRADING_SUBMISSION_MISMATCH


class IdentityMissingError(GradingAggregationError):
    """结果缺少答卷或答案关联。"""

    error_code: ClassVar[str] = GRADING_IDENTITY_MISSING


class InvalidScoreError(GradingAggregationError):
    """得分或满分非法。"""

    error_code: ClassVar[str] = GRADING_SCORE_INVALID


class QuestionTypeMismatchError(GradingAggregationError):
    """结果题型与题目定义不一致。"""

    error_code: ClassVar[str] = GRADING_QUESTION_TYPE_MISMATCH


class UnknownGradingStateError(GradingAggregationError):
    """复核状态未知，或主观题缺少可信的置信度决策。"""

    error_code: ClassVar[str] = GRADING_UNKNOWN_STATE


def _as_score(value: float | Decimal) -> Decimal:
    """把得分转换为有限 Decimal；不使用二进制近似直接构造 Decimal。"""

    try:
        resolved = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:  # pragma: no cover - 防御性分支
        raise InvalidScoreError("得分无法转换为十进制数值。") from exc
    if not resolved.is_finite():
        raise InvalidScoreError("得分必须是有限数值。")
    return resolved


def _to_score(value: Decimal) -> Decimal:
    """在输出边界统一量化到两位小数。"""

    return value.quantize(SCORE_QUANTUM, rounding=ROUND_HALF_UP)


class ResultAggregator:
    """把单题评分结果汇总为整卷结果，并判定最终状态。

    :param: 无状态依赖；预期题目集合、结果与决策均通过 :meth:`aggregate` 传入。
    """

    def aggregate(
        self,
        context: SubmissionContext,
        *,
        results: Sequence[GradingResult],
        decisions: Mapping[str, ConfidenceDecision] | None = None,
        now: datetime | None = None,
    ) -> ExamResultDTO:
        """汇总一份答卷的评分结果。

        :param context: 服务端权威的答卷上下文与预期题目集合。
        :param results: 已产生的单题评分结果。
        :param decisions: ``answer_id`` 到本次置信度决策的映射；主观题自动接受必须提供。
        :param now: 汇总时间；``None`` 时使用当前 UTC 时间。
        :raises GradingAggregationError: 输入不可信或状态无法映射时。
        """

        expected = self._resolve_expected(context)
        indexed = self._index_results(context, expected, results)
        resolved_decisions = dict(decisions or {})

        items: list[QuestionResultDTO] = []
        missing: list[str] = []
        failed: list[str] = []
        not_validated: list[str] = []
        pending_review = 0
        counted = 0
        accumulated = Decimal(0)
        total_max = Decimal(0)

        for entry in expected:
            total_max += entry.max_score
            result = indexed.get(entry.answer_id)
            if result is None:
                missing.append(entry.answer_id)
                items.append(self._missing_item(entry))
                continue
            item = self._evaluate_item(
                entry,
                result,
                resolved_decisions.get(entry.answer_id),
            )
            items.append(item)
            if item.counted:
                counted += 1
                accumulated += item.effective_score or Decimal(0)
            if item.requires_review:
                pending_review += 1
            if item.validation_status == ValidationStatus.FAILED.value:
                failed.append(entry.answer_id)
            if item.validation_status != ValidationStatus.VALIDATED.value:
                not_validated.append(entry.answer_id)

        is_final = counted == len(expected)
        result_status = self._resolve_status(
            failed=failed,
            pending_review=pending_review,
            is_final=is_final,
        )
        subtotal = _to_score(accumulated)

        return ExamResultDTO(
            submission_id=context.submission_id,
            exam_id=context.exam_id,
            student_id=context.student_id,
            result_status=result_status,
            is_final=is_final,
            final_total_score=subtotal if is_final else None,
            confirmed_subtotal=subtotal,
            confirmed_subtotal_label=CONFIRMED_SUBTOTAL_LABEL,
            total_max_score=_to_score(total_max),
            expected_answer_count=len(expected),
            graded_answer_count=len(indexed),
            counted_answer_count=counted,
            pending_review_answer_count=pending_review,
            missing_answer_ids=missing,
            failed_answer_ids=failed,
            not_validated_answer_ids=not_validated,
            items=items,
            aggregated_at=now if now is not None else datetime.now(UTC),
        )

    def _resolve_expected(
        self,
        context: SubmissionContext,
    ) -> tuple[ExpectedAnswer, ...]:
        """校验并返回预期题目集合。"""

        expected = tuple(context.expected_answers)
        if not expected:
            raise EmptyExpectedAnswersError("预期题目集合为空，不能汇总成绩。")
        answer_ids = [entry.answer_id for entry in expected]
        orders = [entry.order for entry in expected]
        if len(set(answer_ids)) != len(answer_ids) or len(set(orders)) != len(orders):
            raise DuplicateResultError("预期题目集合存在重复的答案标识或题序。")
        return expected

    def _index_results(
        self,
        context: SubmissionContext,
        expected: tuple[ExpectedAnswer, ...],
        results: Sequence[GradingResult],
    ) -> dict[str, GradingResult]:
        """按答案标识索引结果，并校验归属与完整性。"""

        expected_ids = {entry.answer_id for entry in expected}
        indexed: dict[str, GradingResult] = {}
        for result in results:
            answer_id = result.answer_id
            if answer_id is None or result.submission_id is None:
                raise IdentityMissingError("评分结果缺少答案或答卷关联，无法证明归属。")
            if result.submission_id != context.submission_id:
                raise SubmissionMismatchError(
                    f"结果属于答卷 {result.submission_id}，与当前答卷不一致。"
                )
            if answer_id not in expected_ids:
                raise UnexpectedResultError(f"结果 {answer_id} 不属于本次考试题目。")
            if answer_id in indexed:
                raise DuplicateResultError(f"答案 {answer_id} 出现多条评分结果。")
            indexed[answer_id] = result
        return indexed

    def _missing_item(self, entry: ExpectedAnswer) -> QuestionResultDTO:
        """构造缺少结果时的逐题占位明细。"""

        return QuestionResultDTO(
            order=entry.order,
            answer_id=entry.answer_id,
            question_id=entry.question_id,
            question_type=entry.question_type,
            max_score=entry.max_score,
            counted=False,
            missing=True,
            grading_status="Pending",
            knowledge_points=list(entry.knowledge_points),
        )

    def _evaluate_item(
        self,
        entry: ExpectedAnswer,
        result: GradingResult,
        decision: ConfidenceDecision | None,
    ) -> QuestionResultDTO:
        """判定单题结果并返回逐题明细。"""

        score = self._resolve_score(entry, result)
        review_status = result.review_status
        validation_status = result.validation_status
        counted = False
        requires_review = False
        effective_score: Decimal | None = None
        decision_dto: ConfidenceDecisionDTO | None = None

        if validation_status != ValidationStatus.VALIDATED.value:
            grading_status = (
                "Failed"
                if validation_status == ValidationStatus.FAILED.value
                else "Pending"
            )
        elif review_status in _HUMAN_FINAL_REVIEW_STATES:
            grading_status = "Final"
            counted = True
            effective_score = score
        elif review_status in _PENDING_REVIEW_STATES:
            grading_status = "Pending Review"
            requires_review = True
        elif review_status in _REGRADE_STATES:
            grading_status = "Pending"
        elif review_status == ReviewStatus.NOT_REQUIRED.value:
            if entry.question_type not in OBJECTIVE_QUESTION_TYPES:
                decision_dto = self._resolve_subjective_decision(entry, decision)
            grading_status = "Accepted"
            counted = True
            effective_score = score
        else:
            raise UnknownGradingStateError(
                f"答案 {entry.answer_id} 的复核状态 {review_status} 无法映射到状态机。"
            )

        return QuestionResultDTO(
            order=entry.order,
            answer_id=entry.answer_id,
            question_id=entry.question_id,
            question_type=entry.question_type,
            max_score=entry.max_score,
            score=score,
            effective_score=effective_score,
            counted=counted,
            missing=False,
            requires_review=requires_review,
            grading_status=grading_status,
            review_status=review_status,
            validation_status=validation_status,
            reason=result.reason,
            knowledge_points=list(entry.knowledge_points),
            missing_knowledge_points=list(result.missing_knowledge_points),
            decision=decision_dto,
        )

    def _resolve_score(
        self,
        entry: ExpectedAnswer,
        result: GradingResult,
    ) -> Decimal:
        """校验题型、满分与得分后返回 Decimal 得分。"""

        if result.question_type != entry.question_type:
            raise QuestionTypeMismatchError(
                f"答案 {entry.answer_id} 的结果题型 {result.question_type} "
                f"与题目定义 {entry.question_type} 不一致。"
            )
        max_score = _as_score(result.max_score)
        score = _as_score(result.score)
        if max_score != entry.max_score:
            raise InvalidScoreError(
                f"答案 {entry.answer_id} 的单题满分 {max_score} "
                f"与题目定义 {entry.max_score} 不一致。"
            )
        if score > max_score:
            raise InvalidScoreError(
                f"答案 {entry.answer_id} 的得分 {score} 超过满分 {max_score}。"
            )
        return score

    def _resolve_subjective_decision(
        self,
        entry: ExpectedAnswer,
        decision: ConfidenceDecision | None,
    ) -> ConfidenceDecisionDTO:
        """主观题自动接受必须能证明本次执行过置信度检查。"""

        if decision is None:
            raise UnknownGradingStateError(
                f"答案 {entry.answer_id} 为主观题且标记为自动接受，"
                "但缺少本次置信度决策，无法证明已执行 Confidence Check。"
            )
        if decision.requires_review or decision.review_status != (
            ReviewStatus.NOT_REQUIRED.value
        ):
            raise UnknownGradingStateError(
                f"答案 {entry.answer_id} 的置信度决策要求人工复核，"
                "但结果仍标记为自动接受，状态不可信。"
            )
        return _decision_dto(decision)

    def _resolve_status(
        self,
        *,
        failed: Sequence[str],
        pending_review: int,
        is_final: bool,
    ) -> ExamResultStatus:
        """按优先次序判定整卷状态。"""

        if failed:
            return ExamResultStatus.FAILED
        if pending_review:
            return ExamResultStatus.PENDING_REVIEW
        if is_final:
            return ExamResultStatus.FINAL
        return ExamResultStatus.PENDING


def _decision_dto(decision: ConfidenceDecision) -> ConfidenceDecisionDTO:
    """把既有决策对象转换为可序列化快照。"""

    return ConfidenceDecisionDTO(
        confidence=decision.confidence,
        threshold=decision.threshold,
        requires_review=decision.requires_review,
        review_status=decision.review_status,
        grading_status=decision.grading_status,
        reason=decision.reason,
    )


__all__ = [
    "CONFIRMED_SUBTOTAL_LABEL",
    "GRADING_AGGREGATION_EMPTY_EXPECTED",
    "GRADING_AGGREGATION_FAILED",
    "GRADING_DUPLICATE_RESULT",
    "GRADING_IDENTITY_MISSING",
    "GRADING_QUESTION_TYPE_MISMATCH",
    "GRADING_SCORE_INVALID",
    "GRADING_SUBMISSION_MISMATCH",
    "GRADING_UNEXPECTED_RESULT",
    "GRADING_UNKNOWN_STATE",
    "SCORE_QUANTUM",
    "DuplicateResultError",
    "EmptyExpectedAnswersError",
    "GradingAggregationError",
    "IdentityMissingError",
    "InvalidScoreError",
    "QuestionTypeMismatchError",
    "ResultAggregator",
    "SubmissionMismatchError",
    "UnexpectedResultError",
    "UnknownGradingStateError",
]
