from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    ValidationInfo,
    field_validator,
    model_validator,
)


def _reject_bool(value: Any) -> Any:
    if isinstance(value, bool):
        raise ValueError("confidence must be a number, not a boolean")  # noqa: TRY004 - Pydantic validators require ValueError.
    return value


Confidence = Annotated[
    Decimal, BeforeValidator(_reject_bool), Field(ge=0, le=1, allow_inf_nan=False)
]
Pixel = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]


class OCRRegion(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    bbox: tuple[Pixel, Pixel, Pixel, Pixel]
    text: StrictStr
    confidence: Confidence | None

    @field_validator("bbox", mode="before")
    @classmethod
    def reject_boolean_coordinates(cls, value: Any) -> Any:
        if isinstance(value, (tuple, list)) and any(isinstance(v, bool) for v in value):
            raise ValueError("coordinates must be numbers, not booleans")
        return value

    @field_validator("bbox")
    @classmethod
    def validate_bounds(
        cls, value: tuple[float, float, float, float], info: ValidationInfo
    ) -> tuple[float, float, float, float]:
        x0, y0, x1, y1 = value
        if x0 >= x1 or y0 >= y1:
            raise ValueError("bbox must have positive area")
        if info.context and "image_size" in info.context:
            width, height = info.context["image_size"]
            if x1 > width or y1 > height:
                raise ValueError("bbox is outside the original image")
        return value


class OCRResult(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    text: StrictStr
    confidence: Confidence | None
    regions: list[OCRRegion]


class OCRProviderInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    provider: StrictStr
    model: StrictStr | None
    ready: StrictBool
    reason: StrictStr | None

    @model_validator(mode="after")
    def validate_reason(self) -> OCRProviderInfo:
        if not self.ready and (not self.reason or not self.reason.strip()):
            raise ValueError("not-ready provider requires an actionable reason")
        return self
