"""Assembly intent and association projections; facts remain on ExamQuestion."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from backend.app.domain.enums import QuestionType
from backend.app.schemas.exam_scoring import (
    Amount,
    ScoringBasis,
    SignedAmount,
    TotalAmount,
)
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


class ExamQuestionPatchRequest(AssemblyModel):
    order_index: int | None = Field(default=None, strict=True, gt=0, le=200)
    replacement_question_id: UUID | None = None
    score: Amount | None = None

    @model_validator(mode="after")
    def meaningful_patch(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("至少提供题序、替换题目或本场分值一项。")
        for name in ("order_index", "replacement_question_id"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} 不允许为 null。")
        return self


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
    question_type: QuestionType
    content: str
    options: dict[str, Any] | list[Any] | None
    reference_answer: str | None
    scoring_rubric: str | None
    analysis: str | None
    knowledge_points: list[str]
    assets: list[QuestionAssetView] = Field(default_factory=list)


class AssemblyCondition(AssemblyModel):
    kind: str
    target: int | str
    actual: int | str | None
    satisfied: bool
    reason: str | None = None


class PublicationCheck(AssemblyModel):
    code: str
    message: str
    question_id: UUID | None = None


class AssemblyResponse(AssemblyModel):
    exam_id: UUID
    current_status: str
    exam_questions: list[ExamQuestionView]
    conditions: list[AssemblyCondition]
    total_score: SignedAmount | None
    publication_checks: list[PublicationCheck]
    assembly_constraints: AssemblyConstraints | None

    @field_validator("total_score")
    @classmethod
    def nonnegative_total(cls, value: SignedAmount | None) -> SignedAmount | None:
        if value is not None and value < 0:
            raise ValueError("预览总分不能为负数。")
        return value


__all__ = [
    "AssemblyCondition",
    "AssemblyConstraints",
    "AssemblyQuestionType",
    "AssemblyRequest",
    "AssemblyResponse",
    "ExamQuestionPatchRequest",
    "ExamQuestionView",
    "KnowledgeCoverage",
    "PublicationCheck",
    "ScoreOverride",
    "TypeDistribution",
]
