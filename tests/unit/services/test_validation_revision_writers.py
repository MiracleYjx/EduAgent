"""T163/TCR §17: direct producers invalidate semantic evidence once per real edit."""
from decimal import Decimal
from uuid import UUID

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.core.database import Base
from backend.app.domain.enums import QuestionStatus, QuestionType, UserRole
from backend.app.models import Course, Question, Role, User
from backend.app.services.question_service import QuestionService


def test_answer_changes_and_resubmission_advance_revision_but_classification_does_not():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        teacher = User(username="revision-teacher", email="revision@example.test", password_hash="test")
        teacher.roles.append(Role(name=UserRole.TEACHER))
        course = Course(name="Revision course", creator=teacher)
        session.add(course)
        session.commit()
        service = QuestionService(session)
        created = service.create_question(course.id, question_type=QuestionType.SHORT_ANSWER,
            content="A", reference_answer="answer", scoring_rubric="rubric", score=Decimal(2),
            created_by=teacher.id)
        question = session.get(Question, UUID(created.id))
        assert question is not None and question.validation_revision == 0
        service.update_question(question.id, content="B", reference_answer="changed", teacher_id=teacher.id)
        assert question.validation_revision == 1
        service.update_question(question.id, content="A", reference_answer="answer", teacher_id=teacher.id)
        assert question.validation_revision == 2
        service.update_question(question.id, content="A", knowledge_points=["classification"],
            difficulty="easy", teacher_id=teacher.id)
        assert question.validation_revision == 2
        service.update_question_status(question.id, QuestionStatus.PENDING_REVIEW, teacher_id=teacher.id)
        assert question.validation_revision == 2
        service.update_question_status(question.id, QuestionStatus.NEEDS_REVISION, teacher_id=teacher.id,
            revision_comment="Teacher requests revision before resubmission.")
        assert question.validation_revision == 2
        service.update_question_status(question.id, QuestionStatus.PENDING_REVIEW, teacher_id=teacher.id)
        assert question.validation_revision == 3
    engine.dispose()
