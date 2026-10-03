"""SQL association tables shared with mapped association entities."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Table,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

from backend.app.core.database import Base

user_roles = Table(
    "user_roles",
    Base.metadata,
    Column(
        "user_id",
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "role_id",
        Uuid(as_uuid=True),
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)

exam_questions = Table(
    "exam_questions",
    Base.metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True, default=uuid4),
    Column(
        "exam_id",
        Uuid(as_uuid=True),
        ForeignKey("exams.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "question_id",
        Uuid(as_uuid=True),
        ForeignKey("questions.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("order_index", Integer, nullable=False),
    Column("score", Numeric(8, 2), nullable=True),
    Column("base_score", Numeric(8, 2), nullable=True),
    Column(
        "published_knowledge_points",
        JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql"),
        nullable=True,
    ),
    Column(
        "scoring_basis",
        JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql"),
        nullable=True,
    ),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    ),
    UniqueConstraint("exam_id", "question_id", name="uq_exam_questions_exam_question"),
    UniqueConstraint("exam_id", "order_index", name="uq_exam_questions_exam_order"),
    CheckConstraint("order_index >= 1", name="ck_exam_questions_order"),
    CheckConstraint(
        "score IS NULL OR (score > 0 AND score <= 999999.99)",
        name="ck_exam_questions_score",
    ),
    CheckConstraint(
        "base_score IS NULL OR (base_score > 0 AND base_score <= 999999.99)",
        name="ck_exam_questions_base_score",
    ),
    CheckConstraint(
        "published_knowledge_points IS NULL OR jsonb_typeof(published_knowledge_points) = 'array'",
        name="ck_exam_questions_knowledge_shape",
    ).ddl_if(dialect="postgresql"),
    CheckConstraint(
        "scoring_basis IS NULL OR jsonb_typeof(scoring_basis) = 'object'",
        name="ck_exam_questions_basis_shape",
    ).ddl_if(dialect="postgresql"),
    Index("ix_exam_questions_question_id", "question_id"),
)

__all__ = ["exam_questions", "user_roles"]
