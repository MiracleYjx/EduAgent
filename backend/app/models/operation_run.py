"""Durable confirmed scope and actual sends; never a billing account."""

from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from backend.app.models.question_revision import CONTENT_JSON


class OperationRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "operation_runs"
    __table_args__ = (
        UniqueConstraint("environment_id", "idempotency_key"),
        UniqueConstraint("environment_id", "active_slot"),
        CheckConstraint(
            "request_budget >= 0 AND actual_requests >= 0 AND actual_requests <= request_budget",
            name="ck_operation_requests",
        ),
        CheckConstraint(
            "state IN ('prepared','confirmed','running','succeeded','failed','partial','outcome_unknown')",
            name="ck_operation_state",
        ),
    )
    environment_id: Mapped[UUID] = mapped_column(Uuid)
    idempotency_key: Mapped[str] = mapped_column(String(160))
    purpose: Mapped[str] = mapped_column(String(80))
    preview_digest: Mapped[str] = mapped_column(String(64))
    plan_digest: Mapped[str] = mapped_column(String(64))
    runtime_snapshot_id: Mapped[UUID] = mapped_column(Uuid)
    state: Mapped[str] = mapped_column(String(32), server_default="prepared")
    request_budget: Mapped[int]
    actual_requests: Mapped[int] = mapped_column(server_default="0")
    steps: Mapped[list[dict[str, Any]]] = mapped_column(CONTENT_JSON, nullable=False)
    active_slot: Mapped[str | None] = mapped_column(String(16))
    retry_of_id: Mapped[UUID | None] = mapped_column(Uuid)
    execution_id: Mapped[UUID | None] = mapped_column(Uuid)
    executor_identity: Mapped[dict[str, Any] | None] = mapped_column(CONTENT_JSON)
