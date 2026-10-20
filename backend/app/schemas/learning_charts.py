"""Finite data charts; no model code, inferred samples or answer leakage."""

from collections.abc import Sequence
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    field_validator,
    model_validator,
)


class ChartModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChartAxis(ChartModel):
    label: str = Field(min_length=1)
    unit: str = Field(min_length=1)

    @field_validator("label", "unit")
    @classmethod
    def meaningful(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("An explicit axis label/unit is required")
        return value


class ChartPoint(ChartModel):
    x: Annotated[FiniteFloat, Field(strict=True)]
    y: Annotated[FiniteFloat, Field(strict=True)]


class LearningChart(ChartModel):
    kind: Literal["line", "bar"]
    title: str = Field(min_length=1)
    x_axis: ChartAxis
    y_axis: ChartAxis
    points: list[ChartPoint] = Field(min_length=1)
    basis: str = Field(min_length=1)

    @field_validator("title", "basis")
    @classmethod
    def meaningful(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Chart title and source basis are required")
        return value

    @model_validator(mode="after")
    def ordered_data(self):
        if any(left.x >= right.x for left, right in zip(self.points, self.points[1:])):
            raise ValueError("Explicit x values must be unique and increasing; do not silently sort")
        return self


ChartReview = Literal["consistent", "needs_confirmation", "conflicting"]


def review_chart(
    chart: LearningChart, *, answer_points: Sequence[ChartPoint] | None,
    claimed_consistent: bool | None,
) -> ChartReview:
    """Compare deterministic data relations, not a proof of model correctness.

    Answer basis stays separate from the public chart. A later admission caller
    must supply it; a model's unsubstantiated 'consistent' is insufficient.
    """
    if claimed_consistent is False:
        return "conflicting"
    if not answer_points:
        return "needs_confirmation"
    if list(answer_points) != chart.points:
        return "conflicting"
    return "consistent" if claimed_consistent is True else "needs_confirmation"
