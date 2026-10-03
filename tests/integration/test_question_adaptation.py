"""T165 real PostgreSQL adaptation transactions and durable fresh reads."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session

from backend.app.domain.enums import (
    DocumentStatus,
    QuestionSourceType,
    QuestionStatus,
    QuestionType,
    UserRole,
)
from backend.app.models import Course, Document, DocumentChunk, KnowledgeBase, Question
from tests.postgres_helpers import isolated_postgres_engine
from tests.unit.services.test_question_adaptation_service import (
    test_adaptation_has_new_question_parent_and_real_teaching_sources as exercise_sources,
)
from tests.unit.services.test_question_adaptation_service import (
    test_adaptation_reuses_original_bytes_with_new_identity_and_no_inherited_review as exercise_image,
)
from tests.unit.services.test_question_adaptation_service import (
    test_parent_changed_during_call_rejects_stale_adaptation as exercise_stale,
)
from tests.unit.services.test_question_adaptation_service import (
    test_parent_edge_write_failure_rolls_back_all_candidate_records as exercise_rollback,
)
from tests.unit.services.test_question_adaptation_service import (
    test_reused_asset_failure_preserves_parent_bytes_and_atomic_candidate_boundary as exercise_image_failure,
)
from tests.unit.services.test_submission_service import add_user


@pytest.fixture
def database() -> Any:
    with isolated_postgres_engine() as engine:
        with Session(engine) as session:
            teacher = add_user(
                session,
                UserRole.TEACHER,
                username="t165-teacher",
                email="t165@example.com",
            )
            course = Course(name="T165 course", creator=teacher)
            session.add(course)
            session.flush()
            kb = KnowledgeBase(course_id=course.id, name="Teaching evidence")
            session.add(kb)
            session.flush()
            document = Document(
                course_id=course.id,
                knowledge_base_id=kb.id,
                uploaded_by=teacher.id,
                original_filename="lesson.txt",
                file_format="txt",
                status=DocumentStatus.READY,
            )
            session.add(document)
            session.flush()
            chunk = DocumentChunk(
                document_id=document.id,
                course_id=course.id,
                knowledge_base_id=kb.id,
                chunk_index=0,
                content="Variables store data.",
                chunk_metadata={},
            )
            parent = Question(
                course_id=course.id,
                type=QuestionType.SINGLE_CHOICE,
                content="Original question",
                options={"C": "third", "A": "first"},
                reference_answer="C",
                analysis="Original analysis",
                scoring_rubric="Correct gets 2 points.",
                score=Decimal(2),
                status=QuestionStatus.APPROVED,
                source_type=QuestionSourceType.MANUAL,
                created_by=teacher.id,
            )
            session.add_all([chunk, parent])
            session.commit()
            info = {
                "actor_id": str(teacher.id),
                "course_id": str(course.id),
                "parent_id": str(parent.id),
                "chunk_id": str(chunk.id),
                "document_id": str(document.id),
            }
        yield engine, info


def test_real_postgres_adaptation_sources(database: Any) -> None:
    exercise_sources(database)


def test_real_postgres_adaptation_rollback(database: Any) -> None:
    exercise_rollback(database)


def test_real_postgres_adaptation_stale_input(database: Any) -> None:
    exercise_stale(database)


def test_real_postgres_original_image_reuse(database: Any) -> None:
    exercise_image(database)


def test_real_postgres_image_transaction_failure(database: Any) -> None:
    exercise_image_failure(database)


def test_real_postgres_parent_reference_protection_and_legal_child_delete(
    database: Any,
) -> None:
    from uuid import UUID

    from sqlalchemy import event, func, select

    from backend.app.models import (
        QuestionGenerationMetadata,
        QuestionSourceChunk,
        QuestionSourcePaper,
    )
    from backend.app.services.question_service import (
        QuestionConflictError,
        QuestionService,
    )
    from tests.unit.services.test_question_adaptation_service import generate

    engine, info = database
    response, _provider = generate(database)
    child_id = UUID(response.candidates[0].candidate_id)
    with Session(engine) as session:
        with pytest.raises(QuestionConflictError):
            QuestionService(session).delete_question(
                info["parent_id"], teacher_id=info["actor_id"]
            )
        session.rollback()
        assert session.get(Question, child_id) is not None
        assert (
            session.scalar(select(func.count()).select_from(QuestionSourcePaper)) == 1
        )
    locks = []

    def observe(_connection, _cursor, statement, _parameters, _context, _executemany):
        statement = statement.lower()
        if "for update" in statement:
            if "from courses" in statement:
                locks.append("Course")
            elif "from questions" in statement:
                locks.append("Question")

    event.listen(engine, "before_cursor_execute", observe)
    try:
        with Session(engine) as session:
            QuestionService(session).delete_question(
                child_id, teacher_id=info["actor_id"]
            )
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert locks[:2] == ["Course", "Question"]
    with Session(engine) as session:
        assert session.get(Question, child_id) is None
        assert (
            session.get(Question, UUID(info["parent_id"])).content
            == "Original question"
        )
        for model in (
            QuestionSourcePaper,
            QuestionSourceChunk,
            QuestionGenerationMetadata,
        ):
            assert session.scalar(select(func.count()).select_from(model)) == 0
