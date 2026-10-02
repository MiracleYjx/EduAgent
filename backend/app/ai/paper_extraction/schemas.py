"""LLM output contains source page numbers, never trusted file IDs or paths."""

from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

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
    evidence: dict[
        Literal["reference_answer", "scoring_rubric", "analysis"], SourceExcerpt
    ] = Field(
        default_factory=dict,
        description=(
            "Only reference_answer, scoring_rubric and analysis are evidence keys. "
            "Every non-null answer field needs its own literal source excerpt; "
            "never include content, options, score or knowledge_points here."
        ),
    )

    @model_validator(mode="after")
    def answer_fields_have_literal_evidence(self) -> Self:
        # Source page existence and exact excerpt provenance are checked by the extractor.
        for name in ("reference_answer", "scoring_rubric", "analysis"):
            text = getattr(self, name)
            if text is None:
                continue
            excerpt = self.evidence.get(name)
            if excerpt is None or not text.strip():
                raise ValueError("answer/analysis requires an original excerpt")
            if "".join(text.split()) not in "".join(excerpt.text.split()):
                raise ValueError("answer/analysis is not a literal source excerpt")
        return self


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
    question_index: int = Field(
        gt=0,
        strict=True,
        description=(
            "Copy an index from the input allowed_update_indices/completed_questions. "
            "It is not an original question_number or an index of this batch's new questions."
        ),
    )


class ExtractionBatch(ExtractionModel):
    questions: list[Candidate] = Field(default_factory=list)
    updates: list[AnswerUpdate] = Field(default_factory=list)
    pending: list[SourceExcerpt] = Field(default_factory=list)
