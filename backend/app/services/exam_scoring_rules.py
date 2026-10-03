"""Pure checks for supplied historical or draft exam scoring facts.

These functions do not read current question values, infer rubric weights, or
assign rounding differences. Callers own authorization and evidence provenance.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, localcontext

from backend.app.domain.enums import (
    OBJECTIVE_QUESTION_TYPES,
    SUBJECTIVE_QUESTION_TYPES,
    QuestionType,
)
from backend.app.schemas.exam_scoring import ScoringBasis, parse_amount

_CENT = Decimal("0.01")
_ZERO = Decimal("0.00")
_MAX_SCORE = Decimal("999999.99")


def _positive_score(value: Decimal) -> Decimal:
    amount = parse_amount(value)
    if amount <= 0 or amount > _MAX_SCORE:
        raise ValueError("基准和本场满分必须大于零且不超过 999999.99。")
    return amount


def default_points(
    *, base_points: Decimal, score: Decimal, base_score: Decimal
) -> Decimal:
    """Scale one explicit point, multiplying first and quantizing only the result."""
    target = _positive_score(score)
    source = _positive_score(base_score)
    point = parse_amount(base_points)
    if point < 0 or point > source:
        raise ValueError("基准要点分值必须在零和基准满分之间。")
    with localcontext() as context:
        context.prec = max(context.prec, 28)
        return (point * target / source).quantize(_CENT, rounding=ROUND_HALF_UP)


def validate_scoring_basis(
    *,
    score: Decimal,
    base_score: Decimal,
    question_type: QuestionType,
    basis: ScoringBasis,
    require_confirmation: bool = True,
) -> None:
    """Validate a basis against the supplied facts without changing those facts.

    Preparation may retain unconfirmed rounding differences. A claimed teacher
    confirmation always has to be internally consistent; final use additionally
    requires confirmation of rounding changes and nonadditive correspondence.
    """
    target = _positive_score(score)
    source = _positive_score(base_score)
    if question_type in OBJECTIVE_QUESTION_TYPES:
        expected_kind = "objective"
    elif question_type in SUBJECTIVE_QUESTION_TYPES:
        expected_kind = "subjective"
    else:
        raise ValueError("题型没有可核对的评分路由。")
    if basis.kind != expected_kind:
        raise ValueError("本场评分依据种类与实际题型不一致。")

    with localcontext() as context:
        context.prec = max(context.prec, 28)
        for point in basis.points:
            expected = default_points(
                base_points=point.base_points, score=target, base_score=source
            )
            if point.default_points != expected:
                raise ValueError("默认要点分值必须等于先乘后除的 ROUND_HALF_UP 结果。")
            if point.default_points > target or point.confirmed_points > target:
                raise ValueError("默认和确认要点分值不得超过本场满分。")

        delta = None
        if basis.additive:
            if sum((point.base_points for point in basis.points), _ZERO) != source:
                raise ValueError("可加总基准要点合计必须等于基准满分。")
            delta = target - sum(
                (point.default_points for point in basis.points), _ZERO
            )
            if basis.rounding_delta != delta:
                raise ValueError("必须保存本场满分减默认要点合计的真实有符号尾差。")
        elif basis.rounding_delta is not None:
            raise ValueError("定性或非加总标准没有可加总尾差。")

        manually_changed = any(
            point.confirmed_points != point.default_points for point in basis.points
        )
        confirmation_needed = not basis.additive or delta != _ZERO or manually_changed
        if require_confirmation and confirmation_needed and basis.confirmation is None:
            raise ValueError("尾差、人工改点或定性/非加总标准须有真实教师确认。")
        if (
            basis.additive
            and (require_confirmation or basis.confirmation is not None)
            and sum((point.confirmed_points for point in basis.points), _ZERO) != target
        ):
            raise ValueError("教师最终确认的可加总要点合计必须等于本场满分。")


__all__ = ["default_points", "validate_scoring_basis"]
