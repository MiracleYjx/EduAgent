"""Real decimal and historical-basis invariants shared by T171 and T174."""

from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from uuid import UUID

import pytest

from backend.app.domain.enums import QuestionType
from backend.app.schemas.exam_scoring import ScoringBasis
from backend.app.services.exam_scoring_rules import (
    default_points,
    validate_scoring_basis,
)


def basis(*, target="10.00", confirmed=None, confirmation=False):
    default = "3.33" if target == "10.00" else "0.67"
    values = confirmed or [default] * 3
    return ScoringBasis.model_validate(
        {
            "kind": "subjective",
            "points": [
                {
                    "key": str(index),
                    "label": f"实际要点 {index}",
                    "base_points": "1.00",
                    "default_points": default,
                    "confirmed_points": value,
                }
                for index, value in enumerate(values, start=1)
            ],
            "additive": True,
            "rounding_delta": "0.01" if target == "10.00" else "-0.01",
            "confirmation": (
                {
                    "teacher_id": UUID("00000000-0000-4000-8000-000000000001"),
                    "confirmed_at": datetime(2026, 10, 4, tzinfo=UTC),
                    "reason": "合成规则测试中的显式核对",
                }
                if confirmation
                else None
            ),
        }
    )


def validate(value, *, score="10.00", base_score="3.00", require_confirmation=True):
    return validate_scoring_basis(
        score=Decimal(score),
        base_score=Decimal(base_score),
        question_type=QuestionType.SHORT_ANSWER,
        basis=value,
        require_confirmation=require_confirmation,
    )


@pytest.mark.parametrize(
    ("base", "score", "maximum", "expected"),
    [
        ("0.03", "1.00", "6.00", "0.01"),
        ("1.00", "10.00", "3.00", "3.33"),
        ("1.00", "2.00", "3.00", "0.67"),
        ("0.00", "10.00", "3.00", "0.00"),
        ("999999.99", "999999.99", "999999.99", "999999.99"),
    ],
)
def test_default_points_multiplies_before_dividing_and_rounds_once(
    base, score, maximum, expected
):
    with localcontext() as context:
        context.prec = 3
        actual = default_points(
            base_points=Decimal(base), score=Decimal(score), base_score=Decimal(maximum)
        )
        assert actual == Decimal(expected)
        assert context.prec == 3


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("score", "0"),
        ("score", "-1"),
        ("score", "1000000.00"),
        ("score", "1.001"),
        ("score", "NaN"),
        ("score", "Infinity"),
        ("base_score", "0"),
        ("base_score", "1000000.00"),
        ("base_points", "-0.01"),
        ("base_points", "1.001"),
        ("base_points", "4.00"),
    ],
)
def test_default_points_rejects_invalid_or_out_of_range_inputs(field, value):
    arguments = {
        "score": Decimal("10.00"),
        "base_score": Decimal("3.00"),
        "base_points": Decimal("1.00"),
    }
    arguments[field] = Decimal(value)
    with pytest.raises(ValueError):
        default_points(**arguments)


@pytest.mark.parametrize(
    ("target", "confirmed"),
    [
        ("10.00", ["3.34", "3.33", "3.33"]),
        ("2.00", ["0.66", "0.67", "0.67"]),
    ],
)
def test_signed_rounding_delta_is_not_distributed_and_needs_explicit_confirmation(
    target, confirmed
):
    prepared = basis(target=target)
    before = prepared.model_dump(mode="json")
    validate(prepared, score=target, require_confirmation=False)
    assert prepared.model_dump(mode="json") == before
    with pytest.raises(ValueError):
        validate(prepared, score=target)
    confirmed_basis = basis(target=target, confirmed=confirmed, confirmation=True)
    validate(confirmed_basis, score=target)
    assert [point.default_points for point in confirmed_basis.points] == [
        prepared.points[0].default_points
    ] * 3
    assert [point.confirmed_points for point in confirmed_basis.points] == list(
        map(Decimal, confirmed)
    )


def test_claimed_confirmation_always_requires_correct_final_sum_even_during_prepare():
    with pytest.raises(ValueError):
        validate(basis(confirmation=True), require_confirmation=False)


@pytest.mark.parametrize(
    "case",
    [
        "kind",
        "base_sum",
        "default",
        "delta",
        "delta_missing",
        "base_limit",
        "confirmed_limit",
    ],
)
def test_basis_rejects_contextual_inconsistencies(case):
    data = basis(confirmed=["3.34", "3.33", "3.33"], confirmation=True).model_dump(
        mode="json"
    )
    if case == "kind":
        data["kind"] = "objective"
    elif case == "base_sum":
        data["points"][0]["base_points"] = "0.99"
    elif case == "default":
        data["points"][0]["default_points"] = "3.34"
    elif case == "delta":
        data["rounding_delta"] = "0.00"
    elif case == "delta_missing":
        data["rounding_delta"] = None
    elif case == "base_limit":
        data["points"][0]["base_points"] = "3.01"
    elif case == "confirmed_limit":
        data["points"][0]["confirmed_points"] = "10.01"
    with pytest.raises(ValueError):
        validate(ScoringBasis.model_validate(data))


def test_zero_delta_default_values_need_no_fabricated_confirmation():
    value = ScoringBasis.model_validate(
        {
            "kind": "subjective",
            "additive": True,
            "rounding_delta": "0.00",
            "points": [
                {
                    "key": "first",
                    "label": "第一要点",
                    "base_points": "1.00",
                    "default_points": "2.00",
                    "confirmed_points": "2.00",
                },
                {
                    "key": "second",
                    "label": "第二要点",
                    "base_points": "2.00",
                    "default_points": "4.00",
                    "confirmed_points": "4.00",
                },
            ],
        }
    )
    validate(value, score="6.00")
    assert value.confirmation is None


def test_manual_redistribution_even_without_rounding_delta_needs_confirmation():
    data = {
        "kind": "subjective",
        "additive": True,
        "rounding_delta": "0.00",
        "points": [
            {
                "key": "a",
                "label": "A",
                "base_points": "1.00",
                "default_points": "1.00",
                "confirmed_points": "0.50",
            },
            {
                "key": "b",
                "label": "B",
                "base_points": "1.00",
                "default_points": "1.00",
                "confirmed_points": "1.50",
            },
        ],
    }
    with pytest.raises(ValueError):
        validate(ScoringBasis.model_validate(data), score="2.00", base_score="2.00")
    data["confirmation"] = basis(confirmation=True).model_dump(mode="json")[
        "confirmation"
    ]
    validate(ScoringBasis.model_validate(data), score="2.00", base_score="2.00")


@pytest.mark.parametrize("numeric", [False, True])
def test_qualitative_and_nonadditive_standards_preserve_semantics_without_invented_sum(
    numeric,
):
    data = {
        "kind": "subjective",
        "additive": False,
        "rounding_delta": None,
        "points": (
            [
                {
                    "key": "threshold",
                    "label": "相互重叠的门槛",
                    "base_points": "1.00",
                    "default_points": "3.33",
                    "confirmed_points": "3.00",
                }
            ]
            if numeric
            else []
        ),
    }
    value = ScoringBasis.model_validate(data)
    validate(value, require_confirmation=False)
    with pytest.raises(ValueError):
        validate(value)
    data["confirmation"] = basis(confirmation=True).model_dump(mode="json")[
        "confirmation"
    ]
    confirmed = ScoringBasis.model_validate(data)
    before = deepcopy(confirmed.model_dump(mode="json"))
    validate(confirmed)
    assert confirmed.model_dump(mode="json") == before


def test_independent_historical_basis_uses_supplied_values_and_retains_exact_money_in_low_context():
    value = ScoringBasis.model_validate(
        {
            "kind": "objective",
            "additive": True,
            "rounding_delta": "0.00",
            "points": [
                {
                    "key": "correct",
                    "label": "依据历史标准正确作答",
                    "base_points": "999999.99",
                    "default_points": "123456.78",
                    "confirmed_points": "123456.78",
                },
            ],
        }
    )
    with localcontext() as context:
        context.prec = 3
        validate_scoring_basis(
            score=Decimal("123456.78"),
            base_score=Decimal("999999.99"),
            question_type=QuestionType.SINGLE_CHOICE,
            basis=value,
        )
        assert context.prec == 3
