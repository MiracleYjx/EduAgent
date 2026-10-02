"""图片应用边界 DTO：实际图像 bytes 与模型可回答的条件，不含业务身份。"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from io import BytesIO
from typing import Literal, Self

from PIL import Image, UnidentifiedImageError
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from backend.app.schemas.image_assessment import Provenance
from backend.app.schemas.paper_import import Pixel, PixelRegion

_IMAGE_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "GIF": "image/gif", "WEBP": "image/webp"}
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_REQUEST_BYTES = 48 * 1024 * 1024
MAX_IMAGE_SIDE = 8192


class VisionFailure(RuntimeError):
    """真实阶段与脱敏底层原因；未调用 Provider 时 provenance 为空。"""

    def __init__(
        self, code: str, message: str, *, stage: str, retryable: bool = False,
        cause: str | None = None, provenance: Provenance | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = stage
        self.retryable = retryable
        self.cause = cause
        self.provenance = provenance


@dataclass(frozen=True, slots=True)
class VisionImage:
    """调用方完成文件授权后交付的实际图像；不接受磁盘路径/外部 URL。"""

    data: bytes = field(repr=False)
    mime_type: str
    width: int
    height: int

    @classmethod
    def from_bytes(cls, data: bytes) -> VisionImage:
        if not isinstance(data, bytes) or not data or len(data) > MAX_IMAGE_BYTES:
            raise VisionFailure(
                "VISION_IMAGE_TRANSPORT_UNAVAILABLE", "图像为空或超过单图传输限制。",
                stage="image_transport",
            )
        try:
            with Image.open(BytesIO(data)) as image:
                mime = _IMAGE_FORMATS.get(image.format or "")
                width, height = image.size
                if mime is None or width > MAX_IMAGE_SIDE or height > MAX_IMAGE_SIDE:
                    raise VisionFailure(
                        "VISION_IMAGE_TRANSPORT_UNAVAILABLE", "图像格式或尺寸不在适配器支持范围。",
                        stage="image_transport",
                    )
                image.verify()
        except VisionFailure:
            raise
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
            raise VisionFailure(
                "VISION_IMAGE_TRANSPORT_UNAVAILABLE", "图像 bytes 无法读取为真实受支持图像。",
                stage="image_transport", cause="UnreadableImage",
            ) from None
        return cls(data=data, mime_type=mime, width=width, height=height)


class ProviderImage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    encoding: Literal["base64", "url", "local_path"]
    value: str = Field(min_length=1, repr=False)
    mime_type: str = Field(min_length=1)


class _VisionDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="after", check_fields=False)
    @classmethod
    def nonempty_text(cls, value: object) -> object:
        if isinstance(value, str):
            if not value.strip():
                raise ValueError("已提供文本不能为空。")
            return value.strip()
        return value


class VisionObservation(_VisionDTO):
    image_index: int = Field(ge=1, le=5, strict=True)
    kind: str = Field(min_length=1)
    description: str = Field(min_length=1)
    # 左上/右下 (x0,y0,x1,y1)，与既有 PixelRegion/crop 保持同一事实源。
    bbox: tuple[Pixel, Pixel, Pixel, Pixel] | None = None

    @field_validator("bbox")
    @classmethod
    def valid_bbox(cls, value: tuple[Pixel, Pixel, Pixel, Pixel] | None) -> tuple[Pixel, Pixel, Pixel, Pixel] | None:
        if value is not None:
            PixelRegion(bbox=value)
        return value


class VisionCondition(_VisionDTO):
    image_index: int = Field(ge=1, le=5, strict=True)
    text: str = Field(min_length=1)
    evidence_region: PixelRegion | None


class VisionIssue(_VisionDTO):
    image_indices: list[StrictInt] | None
    message: str = Field(min_length=1)

    @field_validator("image_indices")
    @classmethod
    def known_indices(cls, value: list[int] | None) -> list[int] | None:
        if value is not None and (not value or len(value) != len(set(value)) or any(type(index) is not int or not 1 <= index <= 5 for index in value)):
            raise ValueError("已知图片索引须为非空、不重复的 1–5 整数；未知保持 null。")
        return value


class VisionResult(_VisionDTO):
    observations: list[VisionObservation]
    conditions: list[VisionCondition]
    unresolved_issues: list[VisionIssue]
    requires_manual_review: StrictBool
    # 模型必须返回 null；服务在真实调用后核对并附加实际实例来源。
    provenance: Provenance | None = None

    @model_validator(mode="after")
    def truthful_review_flag(self) -> Self:
        if self.unresolved_issues and not self.requires_manual_review:
            raise ValueError("有未知/矛盾问题时必须要求人工核对。")
        return self

    def validate_images(self, images: list[VisionImage]) -> None:
        """格式合法之外，检查索引与真实输入尺寸对应；不声称语义已正确。"""
        items: list[VisionObservation | VisionCondition] = [*self.observations, *self.conditions]
        for item in items:
            if item.image_index > len(images):
                raise ValueError("模型引用不存在的本轮图片索引。")
            image = images[item.image_index - 1]
            bbox = item.bbox if isinstance(item, VisionObservation) else (item.evidence_region.bbox if item.evidence_region else None)
            if bbox is not None:
                if not all(math.isfinite(value) for value in bbox):
                    raise ValueError("图像坐标非有限数。")
                PixelRegion(bbox=bbox).within(image.width, image.height)
        if any(index > len(images) for issue in self.unresolved_issues for index in issue.image_indices or []):
            raise ValueError("模型问题引用不存在的本轮图片索引。")


__all__ = [
    "ProviderImage", "VisionCondition", "VisionFailure", "VisionImage",
    "VisionIssue", "VisionObservation", "VisionResult",
]
