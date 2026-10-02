"""导入与校正的受校验对象；未知和未提供字段分别保留。"""
from __future__ import annotations

import math
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)

from backend.app.domain.enums import (
    ExtractedBy,
    ExtractedQuestionStatus,
    PaperImportStatus,
    QuestionStatus,
    QuestionType,
)

Pixel = StrictFloat | StrictInt
Amount = Annotated[Decimal, Field(gt=0, max_digits=8, decimal_places=2, allow_inf_nan=False)]


class PixelRegion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bbox: tuple[Pixel, Pixel, Pixel, Pixel]

    @model_validator(mode="after")
    def valid_area(self) -> Self:
        x0, y0, x1, y1 = self.bbox
        if not all(math.isfinite(x) for x in self.bbox) or not (0 <= x0 < x1 and 0 <= y0 < y1):
            raise ValueError("像素框必须为有限非负坐标且具有正面积。")
        return self

    def within(self, width: int, height: int) -> None:
        if self.bbox[2] > width or self.bbox[3] > height:
            raise ValueError("像素框超出真实原页尺寸。")


class SourceRegion(PixelRegion):
    source_page_id: UUID


class StagedAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID | None = None
    file_id: str = Field(min_length=1)
    asset_type: Literal["figure", "table", "diagram"]
    source_page_id: UUID
    region: PixelRegion | None = None
    caption: str | None = None
    student_visible: bool = Field(default=False, strict=True)


class CorrectionFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question_type: QuestionType | None = None
    content: str | None = None
    options: dict[str, Any] | list[Any] | None = None
    reference_answer: str | None = None
    scoring_rubric: str | None = None
    score: Amount | None = None
    correction_notes: str | None = None
    question_number: str | None = None
    analysis: str | None = None
    knowledge_points: list[str] | None = Field(default=None, max_length=64)
    source_regions: list[SourceRegion] | None = None
    assets: list[StagedAsset] | None = Field(default=None, max_length=5)

    @field_validator("content", "question_number", "analysis", "reference_answer", "scoring_rubric", "correction_notes")
    @classmethod
    def nonempty_text(cls, value: str | None) -> str | None:
        if value is not None:
            if not value.strip():
                raise ValueError("已提供文本不能为空。")
            return value.strip()
        return None

    @field_validator("knowledge_points")
    @classmethod
    def normalize_points(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        normalized: list[str] = []
        for point in values:
            point = point.strip()
            if not point or len(point) > 160:
                raise ValueError("知识点须非空且不超过 160 字。")
            if point not in normalized:
                normalized.append(point)
        return normalized

    @field_validator("assets")
    @classmethod
    def unique_assets(cls, assets: list[StagedAsset] | None) -> list[StagedAsset] | None:
        ids = [asset.id for asset in assets or [] if asset.id is not None]
        if len(ids) != len(set(ids)):
            raise ValueError("题图身份重复。")
        return assets


class ExtractedQuestionData(CorrectionFields):
    source_page_ids: list[UUID] = Field(default_factory=list)
    extracted_by: ExtractedBy
    extraction_confidence: Decimal | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)

    @field_validator("source_page_ids")
    @classmethod
    def unique_pages(cls, values: list[UUID]) -> list[UUID]:
        if len(values) != len(set(values)):
            raise ValueError("来源页不能重复。")
        return values


class CorrectionPayload(CorrectionFields):
    order_index: int | None = Field(default=None, ge=1, strict=True)
    action: Literal["edit", "reject"] = "edit"
    source_page_ids: list[UUID] = Field(default_factory=list)
    @field_validator("source_page_ids")
    @classmethod
    def unique_pages(cls, values: list[UUID]) -> list[UUID]:
        return ExtractedQuestionData.unique_pages(values)

    @model_validator(mode="after")
    def rejection_reason(self) -> Self:
        if self.action == "reject" and not self.correction_notes:
            raise ValueError("拒绝须填写真实理由。")
        return self

class SourcePageView(BaseModel):
    id: UUID
    paper_import_id: UUID
    page_number: int
    file_id: str
    width: int
    height: int
    ocr_text: str | None
    ocr_confidence: Decimal | None


class ExtractedQuestionView(ExtractedQuestionData):
    @field_validator("created_at", "updated_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    id: UUID
    paper_import_id: UUID
    order_index: int | None
    order_preserved: bool
    status: ExtractedQuestionStatus
    question_id: UUID | None
    image_assessment: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


    @field_validator("image_assessment")
    @classmethod
    def validate_assessment(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        from backend.app.schemas.image_assessment import ImageAssessment
        return ImageAssessment.model_validate(value).model_dump(mode="json")


class PaperImportView(BaseModel):
    @field_validator("created_at", "updated_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    id: UUID
    course_id: UUID
    uploaded_by: UUID
    document_id: UUID
    original_filename: str
    page_count: int | None
    status: PaperImportStatus
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    original_file_id: str
    parsed_page_count: int
    question_count: int
    pages: list[SourcePageView]
    questions: list[ExtractedQuestionView]
    file_diagnostics: list[dict[str, str]] = Field(default_factory=list)

class CommitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question_ids: list[UUID] = Field(min_length=1)

    @field_validator("question_ids")
    @classmethod
    def unique_ids(cls, values: list[UUID]) -> list[UUID]:
        if len(values) != len(set(values)):
            raise ValueError("批次题目不得重复。")
        return values


class CommitQuestionView(BaseModel):
    extracted_question_id: UUID
    question_id: UUID
    question_status: QuestionStatus
    order_preserved: bool
    completion_status: Literal["complete", "needs_completion"]


class CommitResponse(BaseModel):
    paper_import_id: UUID
    status: PaperImportStatus
    questions: list[CommitQuestionView]
