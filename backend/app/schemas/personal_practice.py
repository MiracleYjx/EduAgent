"""Personal selection, explicit generation and frozen answering contracts."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)

from backend.app.schemas.content_blocks import (
    PersonalQuestionType,
    QuestionContentView,
    RevisionContent,
)
from backend.app.schemas.personal_learning import LearningOperationView, PersonalQuality

Score = Annotated[Decimal, Field(gt=0, max_digits=8, decimal_places=2)]


class PracticeConditions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["uploaded", "ai", "mixed"] = "mixed"
    mode: Literal["random", "balanced", "weak"] = "balanced"
    count: int = Field(default=20, gt=0, strict=True)
    prefer_unseen: StrictBool = True
    allow_practiced: StrictBool = False
    knowledge_point_ids: list[UUID] = Field(default_factory=list)
    question_types: list[PersonalQuestionType] = Field(
        default=["single_choice", "multiple_choice", "short_answer"], min_length=1
    )
    difficulty: str | None = Field(default=None, min_length=1, max_length=32)
    source_document_ids: list[UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_filters(self):
        for values in (
            self.knowledge_point_ids,
            self.question_types,
            self.source_document_ids,
        ):
            if len(set(values)) != len(values):
                raise ValueError("Explicit filter values must be unique")
        return self


class GenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grounding: Literal["uploaded_material", "general_knowledge"]
    material_version_ids: list[UUID] = Field(default_factory=list)
    idempotency_key: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def actual_grounding(self):
        if (self.grounding == "uploaded_material") != bool(self.material_version_ids):
            raise ValueError(
                "Uploaded material requires selected versions; general knowledge has no material references"
            )
        if len(set(self.material_version_ids)) != len(self.material_version_ids):
            raise ValueError("Selected material versions must be unique")
        return self


class PersonalGeneratedQuestion(RevisionContent):
    reference_answer: str = Field(min_length=1)
    scoring_rubric: str | None = None
    quality: PersonalQuality
    knowledge_point_ids: list[UUID] = Field(default_factory=list)
    evidence_chunk_ids: list[UUID] = Field(default_factory=list)
    difficulty: str | None = Field(default=None, min_length=1, max_length=32)
    uncertainties: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_references(self):
        if len(set(self.knowledge_point_ids)) != len(self.knowledge_point_ids) or len(
            set(self.evidence_chunk_ids)
        ) != len(self.evidence_chunk_ids):
            raise ValueError("Generated references must be unique")
        return self


class PersonalGenerationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    questions: list[PersonalGeneratedQuestion] = Field(min_length=1)


class PracticePreviewItem(BaseModel):
    question_id: UUID
    canonical_id: UUID
    revision_id: UUID
    question_type: PersonalQuestionType
    source_kind: Literal["uploaded", "ai"]
    grounding: Literal["uploaded_material", "general_knowledge"]
    knowledge_point_ids: list[UUID]
    default_score: Decimal


class PracticePreviewView(BaseModel):
    id: UUID
    subject_id: UUID
    conditions: PracticeConditions
    preview_digest: str
    eligible_count: int
    uploaded_count: int
    ai_count: int
    unseen_count: int
    practiced_count: int
    excluded_count: int
    candidate_shortage: int
    unseen_shortage: int
    selected_count: int
    coverage: dict[str, int]
    weak_knowledge_point_ids: list[UUID]
    other_count: int
    original_wrong_count: int
    can_start: bool
    items: list[PracticePreviewItem]
    generation_operation: LearningOperationView | None = None
    generation_operation_id: UUID | None = None
    generation_grounding: Literal["uploaded_material", "general_knowledge"] | None = (
        None
    )
    generation_material_version_ids: list[UUID] = Field(default_factory=list)


class PracticeStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    scores: dict[UUID, Score] = Field(default_factory=dict)

    @field_validator("scores", mode="before")
    @classmethod
    def no_boolean_score(cls, value):
        if isinstance(value, dict) and any(isinstance(v, bool) for v in value.values()):
            raise ValueError("Boolean scores are invalid")
        return value


class PracticeAnswerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    choice_ids: list[str] | None = None
    text_answer: str | None = None

    @model_validator(mode="after")
    def one_kind(self):
        if self.choice_ids is not None and self.text_answer is not None:
            raise ValueError("Choose one answer input kind")
        if self.choice_ids is not None and len(set(self.choice_ids)) != len(
            self.choice_ids
        ):
            raise ValueError("Choice IDs must be unique")
        return self


class PracticeAnswerView(BaseModel):
    item_id: UUID
    answer_state: Literal["answered", "unanswered"]
    choice_ids: list[str] | None = None
    text_answer: str | None = None


class PracticeItemView(BaseModel):
    id: UUID
    position: int
    canonical_id: UUID
    score: Decimal
    knowledge_point_ids: list[UUID]
    source_kind: Literal["uploaded", "ai"]
    grounding: Literal["uploaded_material", "general_knowledge"]
    content: QuestionContentView
    answer: PracticeAnswerView


class PersonalPracticeView(BaseModel):
    id: UUID
    subject_id: UUID
    state: str
    items: list[PracticeItemView]
    grading_operation_id: UUID | None = None


class PracticePreviewChanged(BaseModel):
    code: Literal["PRACTICE_PREVIEW_CHANGED"] = "PRACTICE_PREVIEW_CHANGED"
    added_question_ids: list[UUID]
    removed_question_ids: list[UUID]
    changed_question_ids: list[UUID]
    current_preview: PracticePreviewView


class PracticeSubmitRequest(BaseModel):
    """整组提交；空白题需用户显式确认后才继续。"""

    model_config = ConfigDict(extra="forbid")
    blank_confirmed: StrictBool = False


class PracticeGradingPreviewRequest(PracticeSubmitRequest):
    idempotency_key: str = Field(min_length=1, max_length=160)


class PracticeScoringFeedback(BaseModel):
    """Structured basis returned by a subjective grader; never invented locally."""

    correct_points: list[str] = Field(default_factory=list)
    missing_knowledge_points: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    retrieved_context_ids: list[str] = Field(default_factory=list)


class PracticeAttemptView(BaseModel):
    """One append-only grading round, shown for old/new comparison."""

    id: UUID
    round: int
    score: Decimal | None = None
    max_score: Decimal | None = None
    validation_state: Literal["valid", "failed", "needs_review", "outcome_unknown"]
    execution_source: Literal["local_objective", "local_unanswered", "model"]
    reason: str
    error_code: str | None = None
    confidence: float | None = None
    runtime_snapshot_id: UUID | None = None
    feedback: PracticeScoringFeedback | None = None
    created_at: datetime
    question_revision_id: UUID | None = None
    reference_answer: str | None = None
    scoring_rubric: str | None = None
    operation_id: UUID | None = None


class PracticeItemResult(BaseModel):
    item_id: UUID
    position: int
    question_type: str
    answer_state: Literal["answered", "unanswered"]
    score: Decimal | None = None
    max_score: Decimal
    validation_state: Literal["valid", "failed", "needs_review", "outcome_unknown"]
    execution_source: Literal["local_objective", "local_unanswered", "model"]
    reason: str
    error_code: str | None = None
    disputed: bool = False
    dispute_id: UUID | None = None
    latest_attempt_id: UUID | None = None
    content: QuestionContentView
    reference_answer: str = ""
    scoring_rubric: str | None = None
    answer_text: str | None = None
    choice_ids: list[str] | None = None
    feedback: PracticeScoringFeedback | None = None
    history: list[PracticeAttemptView] = Field(default_factory=list)


class PracticeReport(BaseModel):
    """方案 A：完成率看已答/总题，得分率按逐题得分率简单平均（含未答零）。"""

    practice_id: UUID
    subject_id: UUID
    state: Literal["completed", "partial_failed"]
    round: int
    total_count: int
    answered_count: int
    unanswered_count: int
    valid_count: int
    invalid_count: int
    answered_zero_count: int
    completion_rate: Decimal
    group_score_rate: Decimal | None = None
    choice_correct_rate: Decimal | None = None
    choice_sample_count: int = 0
    items: list[PracticeItemResult]
    blank_item_ids: list[UUID] = Field(default_factory=list)
    unresolved_dispute_count: int = 0


class PracticeBlankConfirmation(BaseModel):
    code: Literal["PRACTICE_BLANK_ANSWERS"] = "PRACTICE_BLANK_ANSWERS"
    item_ids: list[UUID]


class ScoreDisputeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    item_id: UUID
    reason: str = Field(min_length=1, max_length=1000)


class ScoreDisputeRegradeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=160)
    revision_id: UUID | None = None
    expected_attempt_id: UUID | None = None


class ScoreDisputeAcceptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: UUID


class ScoreDisputeView(BaseModel):
    id: UUID
    item_id: UUID
    answer_id: UUID
    state: Literal["pending", "awaiting_acceptance", "resolved"]
    reason: str
    disputed_attempt_id: UUID
    accepted_attempt_id: UUID | None = None
    regrade_operation_id: UUID | None = None
