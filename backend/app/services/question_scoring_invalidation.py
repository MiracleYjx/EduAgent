"""Invalidate only editable draft scoring facts when actual question input changes."""

from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session

from backend.app.domain.enums import ExamStatus
from backend.app.models import Exam, ExamQuestion, Question, Submission


def bump_question_validation(session: Session, question: Question) -> None:
    """Caller holds the Question lock; never invert it by acquiring an Exam lock."""
    question.validation_revision += 1
    editable = select(Exam.id).where(
        Exam.status == ExamStatus.DRAFT,
        ~exists().where(Submission.exam_id == Exam.id),
    )
    # Invalidating related facts must not flush unrelated pending assets before
    # their caller has recorded file receipts for rollback/failure handling.
    with session.no_autoflush:
        session.execute(
            update(ExamQuestion)
            .where(
                ExamQuestion.question_id == question.id,
                ExamQuestion.exam_id.in_(editable),
            )
            .values(
                base_score=None, scoring_basis=None, published_knowledge_points=None
            )
            .execution_options(synchronize_session="fetch")
        )


__all__ = ["bump_question_validation"]
