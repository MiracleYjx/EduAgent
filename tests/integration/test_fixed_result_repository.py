"""T177 actual PostgreSQL result identities and unchanged result rows (TCR §32)."""

from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import ExamQuestion, GradingResult, Question
from backend.app.services.grading.grading_repository import DatabaseGradingRepository
from backend.app.services.grading.grading_task_service import (
    DatabaseGradingSubmissionReader,
    DefaultScoringPipeline,
    GradingResultOwnershipError,
)
from tests.postgres_helpers import isolated_postgres_engine
from tests.support.exam_scoring_fixtures import confirm_synthetic_exam_basis
from tests.unit.models.sqlite_support import seed_submission


@pytest.fixture
def database():
    with isolated_postgres_engine() as engine:
        with Session(engine) as session:
            info = seed_submission(session)
            confirm_synthetic_exam_basis(session, info.exam_id)
            session.commit()
        repository = DatabaseGradingRepository(session_factory=lambda: Session(engine))
        with Session(engine) as session:
            current = DatabaseGradingSubmissionReader(session=session).load(
                str(info.submission_id)
            )
        objective = replace(current, answers=current.answers[:1])
        yield engine, info, repository, objective


def test_new_fixed_result_round_trip_keeps_actual_link_and_same_row(database):
    engine, info, repository, snapshot = database
    item = DefaultScoringPipeline().score(snapshot).exam_result.items[0]
    repository.save_single_result(snapshot.submission_id, item)
    with Session(engine) as session:
        row = session.scalar(
            select(GradingResult).where(
                GradingResult.answer_id == info.objective_answer_id
            )
        )
        original_id = row.id
        assert str(row.exam_question_id) == item.exam_question_id
        question = session.get(Question, info.objective_question_id)
        question.score = Decimal("99.00")
        question.knowledge_points = ["changed bank label"]
        session.commit()
    repository.save_single_result(snapshot.submission_id, item)
    actual = repository.get_single_result(snapshot.submission_id, item.answer_id)
    assert actual.exam_question_id == item.exam_question_id
    assert actual.score == Decimal("10.00") and actual.max_score == Decimal("10.00")
    assert actual.knowledge_points == item.knowledge_points
    with Session(engine) as session:
        row = session.scalar(
            select(GradingResult).where(
                GradingResult.answer_id == info.objective_answer_id
            )
        )
        assert row.id == original_id


def test_historical_missing_link_stays_unknown_even_if_current_link_exists(database):
    engine, info, repository, snapshot = database
    item = DefaultScoringPipeline().score(snapshot).exam_result.items[0]
    repository.save_single_result(
        snapshot.submission_id, item.model_copy(update={"exam_question_id": None})
    )
    actual = repository.get_single_result(snapshot.submission_id, item.answer_id)
    assert actual.exam_question_id is None
    assert actual.score == item.score
    with Session(engine) as session:
        assert (
            session.scalar(
                select(ExamQuestion.id).where(
                    ExamQuestion.question_id == info.objective_question_id
                )
            )
            is not None
        )


@pytest.mark.parametrize("wrong_identity", [None, "foreign"])
def test_recorded_identity_cannot_be_removed_or_reassigned(database, wrong_identity):
    engine, info, repository, snapshot = database
    item = DefaultScoringPipeline().score(snapshot).exam_result.items[0]
    repository.save_single_result(snapshot.submission_id, item)
    with Session(engine) as session:
        original_id = session.scalar(
            select(GradingResult.id).where(
                GradingResult.answer_id == info.objective_answer_id
            )
        )
    changed = item.model_copy(
        update={"exam_question_id": str(uuid4()) if wrong_identity else None}
    )
    with pytest.raises(GradingResultOwnershipError):
        repository.save_single_result(snapshot.submission_id, changed)
    actual = repository.get_single_result(snapshot.submission_id, item.answer_id)
    assert (
        actual.exam_question_id == item.exam_question_id and actual.score == item.score
    )
    with Session(engine) as session:
        assert (
            session.scalar(
                select(GradingResult.id).where(
                    GradingResult.answer_id == info.objective_answer_id
                )
            )
            == original_id
        )
