"""Assembly intent and association projections; facts remain on ExamQuestion."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from backend.app.schemas.exam_scoring import Amount, ScoringBasis, TotalAmount
from backend.app.schemas.question_assets import QuestionAssetView

AssemblyQuestionType = Literal["SINGLE_CHOICE", "TRUE_FALSE", "SHORT_ANSWER"]


class AssemblyModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TypeDistribution(AssemblyModel):
    question_type: AssemblyQuestionType
    count: int = Field(strict=True, gt=0, le=200)


class KnowledgeCoverage(AssemblyModel):
    knowledge_point: str = Field(min_length=1)
    min_questions: int = Field(strict=True, gt=0, le=200)

    @field_validator("knowledge_point")
    @classmethod
    def normalized_label(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("知识点标签不能为空。")
        return value.strip()


class ScoreOverride(AssemblyModel):
    question_id: UUID
    score: Amount


class AssemblyRequest(AssemblyModel):
    course_id: UUID
    question_count: int = Field(strict=True, gt=0, le=200)
    type_distribution: list[TypeDistribution] = Field(min_length=1)
    knowledge_coverage: list[KnowledgeCoverage] = Field(default_factory=list)
    total_score: TotalAmount
    score_overrides: list[ScoreOverride] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_requirements(self) -> Self:
        if len({item.question_type for item in self.type_distribution}) != len(
            self.type_distribution
        ):
            raise ValueError("题型分布不能重复。")
        if sum(item.count for item in self.type_distribution) != self.question_count:
            raise ValueError("题型数量合计必须等于要求题数。")
        if len({item.knowledge_point for item in self.knowledge_coverage}) != len(
            self.knowledge_coverage
        ):
            raise ValueError("知识点覆盖要求不能重复。")
        if any(
            item.min_questions > self.question_count for item in self.knowledge_coverage
        ):
            raise ValueError("知识点最低题数不能超过整卷题数。")
        if len({item.question_id for item in self.score_overrides}) != len(
            self.score_overrides
        ):
            raise ValueError("显式题目分值不能重复。")
        return self


class AssemblyConstraints(AssemblyModel):
    request: AssemblyRequest
    recorded_by: UUID
    recorded_at: AwareDatetime

    @field_validator("recorded_at")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class ExamQuestionView(AssemblyModel):
    id: UUID
    exam_id: UUID
    question_id: UUID
    order_index: int = Field(strict=True, gt=0)
    score: Amount | None
    effective_score: Amount | None
    base_score: Amount | None = None
    published_knowledge_points: list[str] | None = None
    scoring_basis: ScoringBasis | None = None
    assets: list[QuestionAssetView] = Field(default_factory=list)


__all__ = [
    "AssemblyConstraints",
    "AssemblyQuestionType",
    "AssemblyRequest",
    "ExamQuestionView",
    "KnowledgeCoverage",
    "ScoreOverride",
    "TypeDistribution",
]
