"""LLM output contains source page numbers, never trusted file IDs or paths."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from backend.app.domain.enums import QuestionType
from backend.app.schemas.paper_import import Amount


class ExtractionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceExcerpt(ExtractionModel):
    page_number: int = Field(gt=0, strict=True)
    text: str = Field(min_length=1)


class AnswerFields(ExtractionModel):
    reference_answer: str | None = None
    scoring_rubric: str | None = None
    analysis: str | None = None
    evidence: dict[str, SourceExcerpt] = Field(default_factory=dict)


class Candidate(AnswerFields):
    source_pages: list[StrictInt] = Field(min_length=1)
    question_number: str | None = None
    question_type: QuestionType | None = None
    content: str | None = None
    options: dict[str, Any] | list[Any] | None = None
    score: Amount | None = None
    knowledge_points: list[str] | None = None

    @field_validator("source_pages")
    @classmethod
    def unique_positive_pages(cls, value: list[int]) -> list[int]:
        if any(n < 1 for n in value) or len(set(value)) != len(value):
            raise ValueError("source pages must be positive and unique")
        return value


class AnswerUpdate(AnswerFields):
    question_index: int = Field(gt=0, strict=True)


class ExtractionBatch(ExtractionModel):
    questions: list[Candidate] = Field(default_factory=list)
    updates: list[AnswerUpdate] = Field(default_factory=list)
    pending: list[SourceExcerpt] = Field(default_factory=list)
