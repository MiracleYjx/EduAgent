"""Subject teacher protocol; messages cannot carry grade mutation commands."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.app.schemas.personal_learning import LearningOperationView


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(default="新的对话", min_length=1, max_length=160)


class TeacherSend(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    text: str = Field(min_length=1, max_length=4000)
    idempotency_key: str = Field(min_length=1, max_length=160)
    grounding: Literal["uploaded_material", "general_knowledge"] = "uploaded_material"
    # Explicit per-operation keyword lookup, like the existing generation path.
    # It does not change the configured vector/retrieval bindings.
    retrieval_mode: Literal["keyword_only", "none"] = "keyword_only"
    material_version_ids: list[UUID] = Field(default_factory=list)
    knowledge_base_ids: list[UUID] = Field(default_factory=list)
    practice_item_id: UUID | None = None

    @model_validator(mode="after")
    def exact_scope(self):
        if self.grounding == "general_knowledge" and (
            self.material_version_ids
            or self.knowledge_base_ids
            or self.retrieval_mode != "none"
        ):
            raise ValueError("General knowledge must not retrieve uploaded materials")
        if (
            self.grounding == "uploaded_material"
            and self.retrieval_mode != "keyword_only"
        ):
            raise ValueError("Select the explicit keyword lookup or general knowledge")
        if any(
            len(v) != len(set(v))
            for v in (self.material_version_ids, self.knowledge_base_ids)
        ):
            raise ValueError("Scope identities must be unique")
        return self


class TeacherReply(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    answer_body: str = Field(min_length=1, max_length=16000)
    basis: Literal["uploaded_material", "model_knowledge", "mixed"]
    citations: list[str] = Field(default_factory=list, max_length=20)
    uncertainties: list[str] = Field(default_factory=list, max_length=20)
    practice_knowledge_point_ids: list[UUID] = Field(
        default_factory=list, max_length=20
    )


class TeacherCitation(BaseModel):
    id: str
    document_id: UUID | None = None
    chunk_id: UUID | None = None
    material_version_id: UUID | None = None
    location: dict = Field(default_factory=dict)
    practice_id: UUID | None = None
    title: str


class ConversationSummary(BaseModel):
    id: UUID
    subject_id: UUID
    title: str
    created_at: datetime
    pending_operation_id: UUID | None = None


class TeacherMessageView(BaseModel):
    id: UUID
    role: Literal["user", "assistant"]
    text: str
    state: str
    operation_id: UUID
    reply: TeacherReply | None = None
    citations: list[TeacherCitation] = Field(default_factory=list)
    error_code: str | None = None
    runtime_snapshot_id: UUID | None = None
    created_at: datetime


class ConversationView(ConversationSummary):
    messages: list[TeacherMessageView]


class TeacherTurn(BaseModel):
    conversation: ConversationView
    operation: LearningOperationView
    requires_confirmation: bool
