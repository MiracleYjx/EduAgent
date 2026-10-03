"""Explicit synthetic scoring facts for tests that execute grading (TCR §26).

This helper only authors current test fixtures. It never repairs historical data or
claims that a human teacher reviewed these synthetic confirmations.
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from backend.app.domain.enums import OBJECTIVE_QUESTION_TYPES, UserRole
from backend.app.models import Exam, User
from backend.app.schemas.exam_scoring import (
    ScoringBasis,
    ScoringConfirmation,
    ScoringPoint,
)


def confirm_synthetic_exam_basis(session: Session, exam: Exam | UUID) -> None:
    """Set exact, identity-backed test input facts; caller owns the commit."""
    session.flush()
    record = session.get(Exam, exam) if isinstance(exam, UUID) else exam
    assert record is not None
    teacher = session.get(User, record.created_by)
    assert teacher is not None and teacher.is_active
    assert any(role.name == UserRole.TEACHER for role in teacher.roles)
    assert record.course.created_by == teacher.id
    for link in record.exam_question_links:
        question = link.question
        amount = Decimal(str(question.score))
        objective = question.type in OBJECTIVE_QUESTION_TYPES
        points = (
            [
                ScoringPoint(
                    key="correct_answer",
                    label="答对得本题满分，答错不得分（合成夹具）",
                    base_points=amount,
                    default_points=amount,
                    confirmed_points=amount,
                )
            ]
            if objective
            else []
        )
        basis = ScoringBasis(
            kind="objective" if objective else "subjective",
            points=points,
            additive=objective,
            rounding_delta=Decimal("0.00") if objective else None,
            confirmation=ScoringConfirmation(
                teacher_id=teacher.id,
                confirmed_at=datetime.now(UTC),
                reason="自动测试创建的合成教师确认：沿用本夹具原题满分与原文字标准；不代表真实教师标注。",
            ),
        )
        link.score = amount
        link.base_score = amount
        link.published_knowledge_points = list(question.knowledge_points)
        link.scoring_basis = basis.model_dump(mode="json")
    session.flush()
