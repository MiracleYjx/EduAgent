"""Per-exam question identity, order and scoring facts on the original table."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy.orm import Mapped, relationship, validates

from backend.app.domain.enums import ExamStatus
from backend.app.models.associations import exam_questions
from backend.app.models.base import Base

if TYPE_CHECKING:
    from backend.app.models.exam import Exam
    from backend.app.models.question import Question


class ExamQuestion(Base):
    __table__ = exam_questions

    id: Mapped[UUID]
    exam_id: Mapped[UUID]
    question_id: Mapped[UUID]
    order_index: Mapped[int]
    score: Mapped[Decimal | None]
    base_score: Mapped[Decimal | None]
    published_knowledge_points: Mapped[list[str] | None]
    scoring_basis: Mapped[dict[str, Any] | None]
    created_at: Mapped[datetime]

    exam: Mapped[Exam] = relationship("Exam", back_populates="exam_question_links")
    question: Mapped[Question] = relationship(
        "Question", back_populates="exam_question_links"
    )

    @property
    def effective_score(self) -> Decimal:
        """Only an editable draft may use the current bank default."""
        if self.score is not None:
            return self.score
        if self.exam.status not in (None, ExamStatus.DRAFT):
            raise ValueError("EXAM_SCORING_BASIS_MISSING: 历史考试的本场分值尚未核对。")
        return self.question.score

    @validates("scoring_basis")
    def valid_scoring_basis(
        self, key: str, value: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        from backend.app.schemas.exam_scoring import ScoringBasis

        return ScoringBasis.model_validate(value).model_dump(mode="json")

    @validates("published_knowledge_points")
    def valid_published_knowledge_points(
        self, key: str, value: list[str] | None
    ) -> list[str] | None:
        if value is not None and (
            not isinstance(value, list)
            or any(not isinstance(item, str) or not item.strip() for item in value)
            or len(set(value)) != len(value)
        ):
            raise ValueError("发布知识点必须为真实且不重复的非空标签列表。")
        return value


__all__ = ["ExamQuestion"]
