"""Current reference protection under the existing Course transaction lock."""

from uuid import UUID

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session

from backend.app.domain.enums import ExamStatus
from backend.app.models import (
    Answer,
    Course,
    Exam,
    ExamQuestion,
    ExamResult,
    Submission,
)

_PROTECTED_STATES = (ExamStatus.PUBLISHED, ExamStatus.CLOSED, ExamStatus.ARCHIVED)


def lock_course(session: Session, course_id: UUID) -> Course | None:
    """Acquire the common first lock and refresh ownership; callers map errors."""
    with session.no_autoflush:
        return session.scalar(
            select(Course)
            .where(Course.id == course_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )


def question_is_protected(session: Session, question_id: UUID) -> bool:
    """Query current references, including retained answers outside current links."""
    protected_exam = exists(
        select(ExamQuestion.id)
        .join(Exam, Exam.id == ExamQuestion.exam_id)
        .where(
            ExamQuestion.question_id == question_id,
            or_(
                Exam.status.in_(_PROTECTED_STATES),
                exists().where(Submission.exam_id == Exam.id),
                exists().where(ExamResult.exam_id == Exam.id),
            ),
        )
    )
    with session.no_autoflush:
        return bool(
            session.scalar(
                select(
                    or_(
                        protected_exam,
                        exists().where(Answer.question_id == question_id),
                    )
                )
            )
        )


def exam_has_protected_history(session: Session, exam_id: UUID) -> bool:
    """Historic rows keep even an anomalous Draft exam immutable."""
    with session.no_autoflush:
        return bool(
            session.scalar(
                select(
                    or_(
                        exists().where(Submission.exam_id == exam_id),
                        exists().where(ExamResult.exam_id == exam_id),
                    )
                )
            )
        )


def course_has_protected_history(session: Session, course_id: UUID) -> bool:
    """Course cascade must not erase publication or retained business history."""
    from backend.app.models import Question

    protected_exam = exists().where(
        Exam.course_id == course_id,
        or_(
            Exam.status.in_(_PROTECTED_STATES),
            exists().where(Submission.exam_id == Exam.id),
            exists().where(ExamResult.exam_id == Exam.id),
        ),
    )
    retained_answer = exists(
        select(Answer.id)
        .join(Question, Question.id == Answer.question_id)
        .where(Question.course_id == course_id)
    )
    with session.no_autoflush:
        return bool(session.scalar(select(or_(protected_exam, retained_answer))))


__all__ = [
    "course_has_protected_history",
    "exam_has_protected_history",
    "lock_course",
    "question_is_protected",
]
