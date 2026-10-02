"""Durable semantic execution facts; teacher decisions append separately."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.models.base import Base, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from backend.app.models.question import Question


def _json():
    return JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")


REPORT_STATE_CHECK = (
    "(outcome = 'running' AND completed_at IS NULL AND checks IS NULL AND issues IS NULL AND error IS NULL)"
    " OR (outcome IN ('passed', 'failed') AND completed_at IS NOT NULL AND checks IS NOT NULL AND issues IS NOT NULL AND error IS NULL)"
    " OR (outcome = 'technical_error' AND completed_at IS NOT NULL AND error IS NOT NULL AND checks IS NULL AND issues IS NULL)"
)


class QuestionValidationResult(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "question_validation_results"
    __table_args__ = (
        UniqueConstraint("question_id", "run_no", name="uq_question_validation_run"),
        CheckConstraint(
            "input_revision >= 0 AND run_no > 0", name="ck_question_validation_counters"
        ),
        CheckConstraint(
            "outcome IN ('running', 'passed', 'failed', 'technical_error')",
            name="ck_question_validation_outcome",
        ),
        CheckConstraint(
            "executor_kind IN ('service', 'agent')",
            name="ck_question_validation_executor",
        ),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at",
            name="ck_question_validation_times",
        ),
        CheckConstraint(REPORT_STATE_CHECK, name="ck_question_validation_result_state"),
        *[
            CheckConstraint(
                f"jsonb_typeof({field}) = 'object'",
                name=f"ck_question_validation_{field}",
            ).ddl_if(dialect="postgresql")
            for field in ("input_refs", "provenance")
        ],
        CheckConstraint(
            "jsonb_typeof(manual_dispositions) = 'array'",
            name="ck_question_validation_dispositions",
        ).ddl_if(dialect="postgresql"),
        *[
            CheckConstraint(
                f"{field} IS NULL OR jsonb_typeof({field}) = '{kind}'",
                name=f"ck_question_validation_{field}",
            ).ddl_if(dialect="postgresql")
            for field, kind in (
                ("checks", "array"),
                ("issues", "array"),
                ("error", "object"),
            )
        ],
    )
    question_id: Mapped[UUID] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"), nullable=False
    )
    input_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    run_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    input_refs: Mapped[dict[str, Any]] = mapped_column(_json(), nullable=False)
    checks: Mapped[list[dict[str, Any]] | None] = mapped_column(_json())
    issues: Mapped[list[dict[str, Any]] | None] = mapped_column(_json())
    error: Mapped[dict[str, Any] | None] = mapped_column(_json())
    executor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    executor_name: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    agent_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    provenance: Mapped[dict[str, Any]] = mapped_column(_json(), nullable=False)
    manual_dispositions: Mapped[list[dict[str, Any]]] = mapped_column(
        _json(), default=list, server_default="[]", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    question: Mapped[Question] = relationship(
        "Question", back_populates="validation_results"
    )
