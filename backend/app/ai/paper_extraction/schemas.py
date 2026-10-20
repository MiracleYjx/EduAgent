"""LLM output contains source page numbers, never trusted file IDs or paths."""

from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from backend.app.domain.enums import QuestionType
from backend.app.schemas.content_blocks import RevisionContent, SourceLocation
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
    source_anchor: SourceExcerpt | None = Field(
        default=None,
        description="Literal unique question start, including its original number when present; used only to prove reading order.",
    )
    option_sources: dict[str, SourceExcerpt] | None = Field(
        default=None,
        description="Each option key has its own minimal literal unique excerpt containing only its label and value. Never copy a shared whole multi-option line; excerpts must start at distinct real positions. Not persisted.",
    )
    question_number: str | None = None
    question_type: QuestionType | None = None
    content: str | None = None
    options: dict[str, Any] | list[Any] | None = Field(
        default=None,
        description="Dict only for original explicit option labels; list for unlabelled options (including true/false). Preserve original values and order; never invent keys or null option values.",
    )
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


# Personal extraction uses the shared ordered-content contract. Legacy course
# extraction remains on Candidate/ExtractionBatch above.
class PersonalFieldEvidence(ExtractionModel):
    source: SourceLocation
    text: str = Field(min_length=1)


class PersonalPaperCandidate(RevisionContent):
    source_locations: list[SourceLocation] = Field(min_length=1)
    question_number: str | None = None
    reference_answer: str | None = None
    scoring_rubric: str | None = None
    answer_origin: Literal["paper"] | None = None
    score: Amount | None = None
    difficulty: str | None = None
    knowledge_point_ids: list[UUID] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    evidence: dict[
        Literal["reference_answer", "scoring_rubric", "explanation"],
        PersonalFieldEvidence,
    ] = Field(default_factory=dict)

    @field_validator("knowledge_point_ids")
    @classmethod
    def unique_knowledge(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("Knowledge point identities must be unique")
        return value

    @model_validator(mode="after")
    def honest_answer_source(self) -> Self:
        if self.reference_answer is None:
            if self.answer_origin is not None:
                raise ValueError("Missing answers cannot claim an answer origin")
        elif not self.reference_answer.strip() or self.answer_origin != "paper":
            raise ValueError("Extracted answers require their original paper source")
        for name in ("reference_answer", "scoring_rubric"):
            value = getattr(self, name)
            if value is None:
                continue
            evidence = self.evidence.get(name)
            if (
                evidence is None
                or evidence.source not in self.source_locations
                or not "".join(value.split())
                or "".join(value.split()) not in "".join(evidence.text.split())
            ):
                raise ValueError("Original answer fields require matching source evidence")
        if self.explanation_blocks:
            evidence = self.evidence.get("explanation")
            if evidence is None:
                raise ValueError("Original explanation requires source evidence")
            for block in self.explanation_blocks:
                if block.type == "text" and "".join(block.text.split()) not in "".join(evidence.text.split()):
                    raise ValueError("Original explanation must match its source evidence")
        for evidence in self.evidence.values():
            if evidence.source not in self.source_locations:
                raise ValueError("Field evidence must belong to the question sources")
        return self


class PersonalPaperBatch(ExtractionModel):
    questions: list[PersonalPaperCandidate] = Field(min_length=1)


class PersonalPaperConfirm(ExtractionModel):
    question_ids: list[UUID] = Field(min_length=1)
    expected_revisions: dict[UUID, Annotated[int, Field(ge=1, strict=True)]]

    @model_validator(mode="after")
    def exact_selected_scope(self) -> Self:
        if len(set(self.question_ids)) != len(self.question_ids):
            raise ValueError("Selected question identities must be unique")
        if set(self.expected_revisions) != set(self.question_ids):
            raise ValueError("Every selected question requires its exact revision")
        return self
