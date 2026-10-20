"""Fixed practice basis, answer facts and append-only grading rounds."""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from backend.app.models.question_revision import CONTENT_JSON


class PracticeSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "practice_sessions"
    __table_args__ = (
        UniqueConstraint("id", "subject_id"),
        CheckConstraint(
            "state IN ('preview','in_progress','submitted','grading','completed','partial_failed')",
            name="ck_practice_state",
        ),
    )
    subject_id: Mapped[UUID] = mapped_column(
        ForeignKey("subjects.id", ondelete="RESTRICT")
    )
    state: Mapped[str] = mapped_column(String(32), server_default="preview")
    conditions: Mapped[dict[str, Any]] = mapped_column(CONTENT_JSON, nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PracticeItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "practice_items"
    __table_args__ = (
        UniqueConstraint("session_id", "position"),
        UniqueConstraint("session_id", "question_id"),
        ForeignKeyConstraint(
            ["session_id", "subject_id"],
            ["practice_sessions.id", "practice_sessions.subject_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["question_id", "subject_id"],
            ["questions.id", "questions.subject_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["question_revision_id", "question_id"],
            ["question_revisions.id", "question_revisions.question_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "position > 0 AND fixed_max_score > 0", name="ck_practice_item_positive"
        ),
    )
    session_id: Mapped[UUID] = mapped_column(Uuid)
    subject_id: Mapped[UUID] = mapped_column(Uuid)
    position: Mapped[int]
    question_id: Mapped[UUID] = mapped_column(Uuid)
    question_revision_id: Mapped[UUID] = mapped_column(Uuid)
    fixed_max_score: Mapped[Decimal] = mapped_column(Numeric(8, 2))
    scoring_input: Mapped[dict[str, Any]] = mapped_column(CONTENT_JSON, nullable=False)
    knowledge_point_ids: Mapped[list[str]] = mapped_column(CONTENT_JSON, nullable=False)


class PracticeAnswer(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "practice_answers"
    __table_args__ = (
        UniqueConstraint("practice_item_id"),
        CheckConstraint(
            "answer_state IN ('answered','unanswered') AND submission_state IN ('draft','submitted')",
            name="ck_answer_state",
        ),
        CheckConstraint(
            "(answer_state='unanswered' AND choice_ids IS NULL AND text_answer IS NULL) OR (answer_state='answered' AND ((choice_ids IS NOT NULL AND text_answer IS NULL) OR (choice_ids IS NULL AND text_answer IS NOT NULL)))",
            name="ck_answer_content",
        ),
    )
    practice_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("practice_items.id", ondelete="RESTRICT")
    )
    answer_state: Mapped[str] = mapped_column(String(32))
    submission_state: Mapped[str] = mapped_column(String(32), server_default="draft")
    choice_ids: Mapped[list[str] | None] = mapped_column(CONTENT_JSON, nullable=True)
    text_answer: Mapped[str | None]
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GradeAttempt(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "grade_attempts"
    __table_args__ = (
        UniqueConstraint("answer_id", "round"),
        UniqueConstraint("id", "answer_id"),
        CheckConstraint("round > 0", name="ck_grade_round"),
        CheckConstraint(
            "validation_state IN ('valid','failed','needs_review','outcome_unknown')",
            name="ck_grade_state",
        ),
        CheckConstraint(
            "execution_source IN ('local_objective','local_unanswered','model')",
            name="ck_grade_execution",
        ),
        CheckConstraint(
            "(validation_state='valid' AND score IS NOT NULL AND max_score IS NOT NULL AND score>=0 AND max_score>0 AND score<=max_score) OR (validation_state<>'valid' AND score IS NULL)",
            name="ck_grade_score",
        ),
        CheckConstraint(
            "(execution_source='model' AND (validation_state<>'valid' OR runtime_snapshot_id IS NOT NULL)) OR (execution_source<>'model' AND runtime_snapshot_id IS NULL AND confidence IS NULL AND usage IS NULL)",
            name="ck_grade_runtime",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence>=0 AND confidence<=1)",
            name="ck_grade_confidence",
        ),
    )
    answer_id: Mapped[UUID] = mapped_column(
        ForeignKey("practice_answers.id", ondelete="RESTRICT")
    )
    round: Mapped[int]
    validation_state: Mapped[str] = mapped_column(String(32))
    execution_source: Mapped[str] = mapped_column(String(32))
    score: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    max_score: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    reason: Mapped[str]
    scoring_input: Mapped[dict[str, Any]] = mapped_column(CONTENT_JSON, nullable=False)
    runtime_snapshot_id: Mapped[UUID | None] = mapped_column(Uuid)
    confidence: Mapped[float | None]
    usage: Mapped[dict[str, Any] | None] = mapped_column(CONTENT_JSON, nullable=True)
    #: 结构化评分依据：命中点、缺失点、建议与实际检索引用；不覆盖冻结输入。
    feedback: Mapped[dict[str, Any] | None] = mapped_column(CONTENT_JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64))
