"""模型之间的多对多关联表。"""

from sqlalchemy import Column, ForeignKey, Table, Uuid

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
    Column(
        "exam_id",
        Uuid(as_uuid=True),
        ForeignKey("exams.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "question_id",
        Uuid(as_uuid=True),
        ForeignKey("questions.id", ondelete="RESTRICT"),
        primary_key=True,
    ),
)


__all__ = ["exam_questions", "user_roles"]
