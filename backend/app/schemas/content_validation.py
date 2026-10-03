"""Typed persistent semantic reports and authenticated teacher commands."""

from __future__ import annotations

from decimal import Decimal
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
from backend.app.schemas.image_assessment import TechnicalError

CheckKind = Literal[
    "answer_correctness", "condition_sufficiency", "option_ambiguity", "rubric_clarity"
]
CHECK_KINDS = {
    "answer_correctness",
    "condition_sufficiency",
    "option_ambiguity",
    "rubric_clarity",
}


class ValidationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="after")
    @classmethod
    def nonblank_strings(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            raise ValueError("Text must not be blank.")
        return value


class ImageReviewReference(ValidationModel):
    owner_kind: Literal["question", "extracted_question"]
    owner_id: UUID
    check_id: UUID
    binding_id: UUID | None = None


class ValidationEvidence(ValidationModel):
    evidence_id: UUID
    kind: Literal["chunk", "question_source_chunk", "question_asset"]
    source_id: UUID
    source_data: dict[str, Any]

    @model_validator(mode="after")
    def required_source_fields(self) -> Self:
        fields = (
            {
                "file_id",
                "source_page_id",
                "region",
                "image_review_ref",
                "confirmed_conditions",
            }
            if self.kind == "question_asset"
            else {
                "chunk_id",
                "document_id",
                "course_id",
                "source_file",
                "location",
                "content_snapshot",
            }
        )
        if set(self.source_data) != fields:
            raise ValueError("Evidence must preserve its actual source fields.")
        if self.kind == "question_asset":
            ImageReviewReference.model_validate(self.source_data["image_review_ref"])
        elif (
            not isinstance(self.source_data["content_snapshot"], str)
            or not self.source_data["content_snapshot"].strip()
        ):
            raise ValueError("Teaching evidence must contain the actual used text.")
        return self


class ManualContextReference(ValidationModel):
    validation_result_id: UUID
    disposition_id: UUID


class SemanticValidationRequest(ValidationModel):
    """Teacher chooses real evidence identities; current facts come from persistence."""

    teaching_chunk_ids: list[UUID] | None = None
    manual_context: list[ManualContextReference] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_selections(self) -> Self:
        if self.teaching_chunk_ids is not None and len(
            set(self.teaching_chunk_ids)
        ) != len(self.teaching_chunk_ids):
            raise ValueError("Teaching chunk identities must not repeat.")
        contexts = [
            (item.validation_result_id, item.disposition_id)
            for item in self.manual_context
        ]
        if len(set(contexts)) != len(contexts):
            raise ValueError("Manual context references must not repeat.")
        return self


class ValidationInputRefs(ValidationModel):
    fields: list[str] = Field(min_length=1)
    evidence: list[ValidationEvidence] = Field(default_factory=list)
    manual_context: list[ManualContextReference] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_references(self) -> Self:
        if len(set(self.fields)) != len(self.fields):
            raise ValueError("Input fields must not repeat.")
        if len({item.evidence_id for item in self.evidence}) != len(self.evidence):
            raise ValueError("Evidence IDs must be unique.")
        return self


class ValidationCheck(ValidationModel):
    kind: CheckKind
    verdict: Literal["pass", "fail", "insufficient_evidence", "needs_review"]
    reason: str = Field(min_length=1)
    evidence_refs: list[UUID] = Field(default_factory=list)


class ValidationIssue(ValidationModel):
    issue_id: UUID
    code: str = Field(min_length=1)
    field: str | None = None
    severity: Literal["info", "warning", "error"]
    message: str = Field(min_length=1)
    evidence_refs: list[UUID] = Field(default_factory=list)


class ValidationOutput(ValidationModel):
    checks: list[ValidationCheck]
    issues: list[ValidationIssue]

    @model_validator(mode="after")
    def all_four_checks(self) -> Self:
        if (
            len(self.checks) != 4
            or {check.kind for check in self.checks} != CHECK_KINDS
        ):
            raise ValueError(
                "A semantic report must contain each of the four checks once."
            )
        if len({issue.issue_id for issue in self.issues}) != len(self.issues):
            raise ValueError("Issue IDs must be unique.")
        return self


class ValidationProvenance(ValidationModel):
    agent_type: str | None = None
    provider_name: str | None = None
    model: str | None = None
    model_version: str | None = None
    prompt_version: str | None = None


class ManualDispositionRequest(ValidationModel):
    issue_ids: list[UUID] = Field(default_factory=list)
    check_kind: CheckKind | None = None
    action: Literal["request_revision", "provide_evidence", "resolve_issue"]
    reason: str = Field(min_length=1, max_length=2000)
    evidence_refs: list[UUID] = Field(default_factory=list)
    additional_evidence: list[ValidationEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def identifies_problem(self) -> Self:
        if not self.issue_ids and self.check_kind is None:
            raise ValueError("Disposition must identify an issue or check.")
        if len(set(self.issue_ids)) != len(self.issue_ids) or len(
            set(self.evidence_refs)
        ) != len(self.evidence_refs):
            raise ValueError("References must not repeat.")
        if (
            self.action != "request_revision"
            and not self.evidence_refs
            and not self.additional_evidence
        ):
            raise ValueError("Provide actual evidence for the disposition.")
        return self


class ManualDisposition(ManualDispositionRequest):
    id: UUID
    input_revision: int = Field(ge=0, strict=True)
    handled_by: UUID
    handled_at: AwareDatetime
    revision_comment_id: UUID | None = None


class ValidationReportView(ValidationModel):
    id: UUID
    question_id: UUID
    input_revision: int
    run_no: int
    outcome: Literal["running", "passed", "failed", "technical_error"]
    input_refs: ValidationInputRefs
    checks: list[ValidationCheck] | None
    issues: list[ValidationIssue] | None
    error: TechnicalError | None
    executor_kind: Literal["service", "agent"]
    executor_name: str
    requested_by: UUID | None
    agent_run_id: UUID | None
    provenance: ValidationProvenance
    manual_dispositions: list[ManualDisposition]
    created_at: AwareDatetime
    completed_at: AwareDatetime | None
    is_current: bool
    stale: bool
    can_review: bool
    requires_manual_review: bool


class SemanticQuestionFields(ValidationModel):
    """One immutable-use projection of the persisted candidate, not another answer source."""

    type: QuestionType
    content: str = Field(min_length=1)
    options: dict[str, Any] | list[Any] | None
    reference_answer: str = Field(min_length=1)
    scoring_rubric: str = Field(min_length=1)
    analysis: str | None
    score: Decimal = Field(
        gt=0, le=Decimal("999999.99"), max_digits=8, decimal_places=2
    )


class SemanticValidationInput(ValidationModel):
    question_id: UUID
    input_revision: int = Field(ge=0, strict=True)
    run_no: int = Field(ge=1, strict=True)
    fields: SemanticQuestionFields
    evidence: list[ValidationEvidence]
    manual_context: list[dict[str, Any]] = Field(default_factory=list)


class SemanticMachineIssue(ValidationModel):
    code: str = Field(min_length=1)
    field: str | None = None
    severity: Literal["info", "warning", "error"]
    message: str = Field(min_length=1)
    evidence_refs: list[UUID] = Field(default_factory=list)


class SemanticMachineOutput(ValidationModel):
    """Provider can report conclusions; it cannot assign persistent identities or approval."""

    checks: list[ValidationCheck]
    issues: list[SemanticMachineIssue]

    @model_validator(mode="after")
    def all_four_checks(self) -> Self:
        if len(self.checks) != 4 or {item.kind for item in self.checks} != CHECK_KINDS:
            raise ValueError("The provider must return each semantic check once.")
        return self
