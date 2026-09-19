"""可配置置信度阈值策略与 Pending Review 决策。

契约依据：FR-035、FR-036，plan.md §5.4，``.specify/contracts/agent-workflow.md`` 的
``Confidence Check`` 节点，以及 ``.specify/data-model.md`` 的 GradingResult 状态图。

规则：

- **阈值来源**：未显式传入阈值时读取 ``AppSettings.confidence_threshold``；不把硬编码常量
  当作唯一真相，也不在测试中依赖本机 ``.env``（可替换使用处的 settings 获取函数）。
- **决策**：``confidence >= threshold`` 自动接受；``< threshold`` 进入 ``Pending Review``。
  不做四舍五入、不引入未知的中间状态。
- **客观题直通**：按 data-model 状态图，Objective 结果直接进入接受路径，不参与主观题置信度
  复核；客观题分数由确定性规则保证。
- **apply 适用范围**：只用于新生成的自动评分结果。拒绝非 ``Validated`` 输入；
  若 ``review_status`` 已是 ``Confirmed`` / ``Modified`` / ``Final`` / ``Re-grade`` 等人工结论，
  显式拒绝覆盖。人工复核与重新评分的状态转换仍归后续 Workflow / Review Service。
- **输出**：``grading_status`` 只存在于 :class:`ConfidenceDecision`，本模块不向
  ``GradingResult`` 增加字段。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, ClassVar, Final

from backend.app.core.config import AppSettings, get_settings
from backend.app.domain.enums import (
    OBJECTIVE_QUESTION_TYPES,
    GradingStatus,
    QuestionType,
    ReviewStatus,
    ValidationStatus,
)
from backend.app.schemas.ai import GradingResult
from backend.app.services.grading.question_router import normalize_question_type

#: 置信度阈值非法（非数值、非有限值或超出 [0, 1]）。
GRADING_INVALID_CONFIDENCE_THRESHOLD: Final[str] = "GRADING_INVALID_CONFIDENCE_THRESHOLD"
#: 置信度非法（非数值、非有限值或超出 [0, 1]）。
GRADING_INVALID_CONFIDENCE: Final[str] = "GRADING_INVALID_CONFIDENCE"
#: 结果未通过结构化校验，不得进入接受或待复核决策。
GRADING_RESULT_NOT_VALIDATED: Final[str] = "GRADING_RESULT_NOT_VALIDATED"
#: 结果已有人工复核结论，自动策略不得覆盖。
GRADING_MANUAL_REVIEW_STATE: Final[str] = "GRADING_MANUAL_REVIEW_STATE"

#: 已有教师人工结论的复核状态；自动策略不得覆盖。
HUMAN_DECIDED_REVIEW_STATES: Final[frozenset[str]] = frozenset(
    {
        ReviewStatus.CONFIRMED.value,
        ReviewStatus.MODIFIED.value,
        ReviewStatus.FINAL.value,
        ReviewStatus.RE_GRADE.value,
    }
)


class ConfidencePolicyError(RuntimeError):
    """置信度策略失败基类；按不可重试的业务问题处理。"""

    error_code: ClassVar[str] = GRADING_INVALID_CONFIDENCE
    retryable: ClassVar[bool] = False

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.error_code}：{detail}")
        self.detail = detail


class InvalidConfidenceThresholdError(ConfidencePolicyError):
    """阈值非法；不得静默回退到配置默认值。"""

    error_code: ClassVar[str] = GRADING_INVALID_CONFIDENCE_THRESHOLD


class InvalidConfidenceError(ConfidencePolicyError):
    """置信度非法。"""

    error_code: ClassVar[str] = GRADING_INVALID_CONFIDENCE


class NotValidatedResultError(ConfidencePolicyError):
    """结果未通过结构化校验。"""

    error_code: ClassVar[str] = GRADING_RESULT_NOT_VALIDATED


class ManualReviewStateError(ConfidencePolicyError):
    """结果已有人工复核结论。"""

    error_code: ClassVar[str] = GRADING_MANUAL_REVIEW_STATE


@dataclass(frozen=True, slots=True)
class ConfidenceDecision:
    """一次置信度检查的完整决策。"""

    confidence: float
    threshold: float
    requires_review: bool
    review_status: str
    grading_status: str
    reason: str


def _resolve_unit_number(
    value: Any,
    *,
    label: str,
    error: type[ConfidencePolicyError],
) -> float:
    """校验并转换 [0, 1] 区间内的数值；非法值显式失败。"""

    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise error(f"{label}必须是 0 到 1 之间的数值，收到类型 {type(value).__name__}。")
    number: float | None = None
    failed = False
    try:
        number = float(value)
    except (ValueError, OverflowError):
        failed = True
    if failed or number is None:
        raise error(f"{label}必须是可比较的有限数值，收到 {value!r}。")
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise error(f"{label}必须落在 [0, 1] 区间，收到 {value!r}。")
    return number


class ConfidencePolicy:
    """可配置的置信度阈值策略。

    :param threshold: 显式阈值；``None`` 时读取 :attr:`AppSettings.confidence_threshold`。
    :param settings: 运行配置；用于读取阈值，便于测试注入而不依赖环境变量。
    """

    def __init__(
        self,
        threshold: float | None = None,
        *,
        settings: AppSettings | None = None,
    ) -> None:
        if threshold is None:
            runtime_settings = settings if settings is not None else get_settings()
            raw: Any = runtime_settings.confidence_threshold
        else:
            raw = threshold
        self._threshold = _resolve_unit_number(
            raw,
            label="置信度阈值",
            error=InvalidConfidenceThresholdError,
        )

    @property
    def threshold(self) -> float:
        """返回生效阈值。"""

        return self._threshold

    def evaluate(
        self,
        confidence: Any,
        *,
        question_type: QuestionType | str | None = None,
    ) -> ConfidenceDecision:
        """按阈值给出接受或待人工复核的决策。"""

        value = _resolve_unit_number(
            confidence,
            label="置信度",
            error=InvalidConfidenceError,
        )
        if question_type is not None:
            normalized_type = normalize_question_type(question_type)
            if normalized_type in OBJECTIVE_QUESTION_TYPES:
                return ConfidenceDecision(
                    confidence=value,
                    threshold=self._threshold,
                    requires_review=False,
                    review_status=ReviewStatus.NOT_REQUIRED.value,
                    grading_status=GradingStatus.ACCEPTED.value,
                    reason=(
                        "客观题使用确定性规则评分，按状态图直接接受，"
                        "不进入主观题置信度复核。"
                    ),
                )
        if value >= self._threshold:
            return ConfidenceDecision(
                confidence=value,
                threshold=self._threshold,
                requires_review=False,
                review_status=ReviewStatus.NOT_REQUIRED.value,
                grading_status=GradingStatus.ACCEPTED.value,
                reason=(
                    f"置信度 {value} 不低于阈值 {self._threshold}，自动接受。"
                ),
            )
        return ConfidenceDecision(
            confidence=value,
            threshold=self._threshold,
            requires_review=True,
            review_status=ReviewStatus.PENDING_REVIEW.value,
            grading_status=GradingStatus.PENDING_REVIEW.value,
            reason=(
                f"置信度 {value} 低于阈值 {self._threshold}，进入待人工复核。"
            ),
        )

    def apply(self, result: GradingResult) -> GradingResult:
        """回填自动评分状态，拒绝未校验结果与已有人工结论。"""

        updated, _ = self._apply_with_decision(result)
        return updated

    def _apply_with_decision(
        self, result: GradingResult,
    ) -> tuple[GradingResult, ConfidenceDecision]:
        """完成一次校验、判定与回填，并交回实际使用的决策。"""

        if result.validation_status != ValidationStatus.VALIDATED.value:
            raise NotValidatedResultError(
                f"仅接受 Validated 结果，当前 validation_status="
                f"{result.validation_status}。"
            )
        if result.review_status in HUMAN_DECIDED_REVIEW_STATES:
            raise ManualReviewStateError(
                f"复核状态 {result.review_status} 已有人工结论，"
                "自动策略不得覆盖。"
            )
        decision = self.evaluate(result.confidence, question_type=result.question_type)
        updated = result.model_copy(update={"review_status": decision.review_status})
        return updated, decision


__all__ = [
    "GRADING_INVALID_CONFIDENCE",
    "GRADING_INVALID_CONFIDENCE_THRESHOLD",
    "GRADING_MANUAL_REVIEW_STATE",
    "GRADING_RESULT_NOT_VALIDATED",
    "HUMAN_DECIDED_REVIEW_STATES",
    "ConfidenceDecision",
    "ConfidencePolicy",
    "ConfidencePolicyError",
    "InvalidConfidenceError",
    "InvalidConfidenceThresholdError",
    "ManualReviewStateError",
    "NotValidatedResultError",
]
