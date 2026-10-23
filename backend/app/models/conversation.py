"""Chat ownership is separate from immutable learning and send accounting."""

from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from backend.app.models.question_revision import CONTENT_JSON


class Conversation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "conversations"
    subject_id: Mapped[UUID] = mapped_column(
        ForeignKey("subjects.id", ondelete="RESTRICT"), index=True
    )
    title: Mapped[str] = mapped_column(String(160))
    fee_conditions: Mapped[str | None] = mapped_column(String(64))
    pending_operation_id: Mapped[UUID | None] = mapped_column(Uuid)


class Message(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "conversation_messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "request_key"),
        UniqueConstraint("operation_id", "role"),
        CheckConstraint(
            "role IN ('user','assistant')", name="ck_conversation_message_role"
        ),
    )
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))
    text: Mapped[str]
    state: Mapped[str] = mapped_column(String(32))
    operation_id: Mapped[UUID] = mapped_column(Uuid)
    request_key: Mapped[str | None] = mapped_column(String(160))
    request: Mapped[dict[str, Any] | None] = mapped_column(CONTENT_JSON, nullable=True)
    context_digest: Mapped[str | None] = mapped_column(String(64))
    fee_digest: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[dict[str, Any] | None] = mapped_column(CONTENT_JSON, nullable=True)
    citations: Mapped[list[dict[str, Any]] | None] = mapped_column(
        CONTENT_JSON, nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    runtime_snapshot_id: Mapped[UUID | None] = mapped_column(Uuid)
