"""Validated per-exam scoring facts; no content snapshots or inferred weights."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    StrictBool,
    field_validator,
    model_validator,
)

_LIMIT = Decimal("999999.99")
_MONEY_TEXT = re.compile(r"^-?\d+(?:\.\d{1,2})?$")


def parse_amount(value: Any) -> Decimal:
    """Public amounts are decimal strings; services may supply exact Decimal values."""
    if isinstance(value, str):
        if not _MONEY_TEXT.fullmatch(value):
            raise ValueError("金额必须为最多两位小数的十进制字符串。")
        try:
            amount = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("金额不是有效十进制数。") from exc
    elif isinstance(value, Decimal):
        amount = value
    else:
        # ValueError is required for a Pydantic validation failure.
        raise ValueError(  # noqa: TRY004
            "金额必须为十进制字符串，不能使用浮点数或布尔值。"
        )
    exponent = amount.as_tuple().exponent
    if not amount.is_finite() or not isinstance(exponent, int) or exponent < -2:
        raise ValueError("金额必须有限且最多两位小数。")
    return amount


def positive_amount(value: Decimal) -> Decimal:
    if value <= 0 or value > _LIMIT:
        raise ValueError("单题金额必须大于 0 且不超过 999999.99。")
    return value


def nonnegative_amount(value: Decimal) -> Decimal:
    if value < 0 or value > _LIMIT:
        raise ValueError("要点金额必须在 0 到 999999.99 之间。")
    return value


def positive_total(value: Decimal) -> Decimal:
    if value <= 0:
        raise ValueError("总分必须大于 0。")
    return value


def money_text(value: Decimal) -> str:
    return format(value, ".2f")


SignedAmount = Annotated[
    Decimal,
    BeforeValidator(parse_amount),
    PlainSerializer(money_text, return_type=str, when_used="json"),
]
Amount = Annotated[SignedAmount, AfterValidator(positive_amount)]
NonNegativeAmount = Annotated[SignedAmount, AfterValidator(nonnegative_amount)]
TotalAmount = Annotated[SignedAmount, AfterValidator(positive_total)]


class ScoringModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScoringConfirmation(ScoringModel):
    teacher_id: UUID
    confirmed_at: AwareDatetime
    reason: str = Field(min_length=1)

    @field_validator("confirmed_at")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("教师确认必须说明实际处置理由。")
        return value.strip()


class ScoringPoint(ScoringModel):
    key: str = Field(min_length=1)
    label: str = Field(min_length=1)
    base_points: NonNegativeAmount
    default_points: NonNegativeAmount
    confirmed_points: NonNegativeAmount

    @field_validator("key", "label")
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("要点标识及说明不能为空。")
        return value.strip()


class ScoringBasis(ScoringModel):
    kind: Literal["objective", "subjective"]
    rounding_mode: Literal["ROUND_HALF_UP"] = "ROUND_HALF_UP"
    points: list[ScoringPoint]
    additive: StrictBool
    rounding_delta: SignedAmount | None
    confirmation: ScoringConfirmation | None = None

    @model_validator(mode="after")
    def validate_point_identity_and_delta(self) -> Self:
        if len({point.key for point in self.points}) != len(self.points):
            raise ValueError("评分要点 key 不能重复。")
        if not self.additive and self.rounding_delta is not None:
            raise ValueError("非加总标准不存在数值尾差。")
        return self


__all__ = [
    "Amount",
    "NonNegativeAmount",
    "ScoringBasis",
    "ScoringConfirmation",
    "ScoringPoint",
    "SignedAmount",
    "TotalAmount",
    "money_text",
    "parse_amount",
]
