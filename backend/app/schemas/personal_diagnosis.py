"""Read-only diagnosis facts; ratios are 0..1, never mastery probabilities."""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from backend.app.schemas.personal_practice import PracticeConditions


class Performance(BaseModel):
    count: int
    score_rate: Decimal | None = None
    choice_count: int = 0
    choice_correct_rate: Decimal | None = None


class DiagnosisStatistics(BaseModel):
    practice_count: int
    valid_count: int
    excluded_count: int
    unanswered_count: int
    exclusion_counts: dict[str, int]
    first: Performance
    repractice: Performance
    recent: Performance
    previous: Performance
    status: Literal["insufficient_evidence", "reinforce", "observed"]
    repeated_wrong_question_ids: list[UUID]


class ReviewSource(BaseModel):
    document_id: UUID
    chunk_id: UUID
    material_version_id: UUID | None = None
    title: str
    location: dict
    excerpt: str


class LearningSuggestion(BaseModel):
    knowledge_point_id: UUID
    message: str
    sources: list[ReviewSource]
    practice_conditions: PracticeConditions


class KnowledgeDiagnosis(BaseModel):
    id: UUID
    name: str
    statistics: DiagnosisStatistics
    suggestion: LearningSuggestion


class DiagnosisEvidence(BaseModel):
    answer_id: UUID
    item_id: UUID
    practice_id: UUID
    question_id: UUID
    revision_id: UUID
    attempt_id: UUID | None
    submitted_at: datetime
    knowledge_point_ids: list[UUID]
    question_type: str
    source_kind: str
    answer_origin: str | None
    score: Decimal | None
    max_score: Decimal
    exclusion: str | None


class PersonalDiagnosis(BaseModel):
    subject_id: UUID
    source_kind: Literal["uploaded", "ai"] | None
    answer_origin: str | None
    statistics: DiagnosisStatistics
    knowledge_points: list[KnowledgeDiagnosis]
    evidence: list[DiagnosisEvidence] = Field(default_factory=list)
