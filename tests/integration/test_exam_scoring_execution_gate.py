"""T171: persisted history remains readable; execution requires expressible fixed facts."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from backend.app.api.reviews import ReviewQueryService
from backend.app.domain.enums import (
    AnswerStatus,
    ExamStatus,
    QuestionStatus,
    QuestionType,
    ReviewStatus,
    SubmissionStatus,
    ValidationStatus,
)
from backend.app.models import (
    Answer,
    Course,
    Exam,
    ExamQuestion,
    Question,
    Submission,
    User,
)
from backend.app.models.grading_result import GradingResult
from backend.app.services.grading.grading_task_service import (
    DatabaseGradingSubmissionReader,
    GradingNotAllowedError,
    GradingTaskService,
    InlineGradingTaskExecutor,
    SubmissionSnapshot,
)
from tests.postgres_helpers import isolated_postgres_engine
from tests.support.grading_doubles import (
    InMemoryGradingRepository,
    RecordingExecutor,
    StubScoringPipeline,
    make_task,
)


@pytest.fixture
def historical():
    with isolated_postgres_engine() as engine, Session(engine) as session:
        teacher = User(
            username="gate-teacher",
            email="gate-teacher@test.invalid",
            password_hash="synthetic",
        )
        student = User(
            username="gate-student",
            email="gate-student@test.invalid",
            password_hash="synthetic",
        )
        course = Course(name="Synthetic gate course", creator=teacher)
        question = Question(
            course=course,
            creator=teacher,
            type=QuestionType.SINGLE_CHOICE,
            content="Synthetic: choose A",
            options={"A": "yes", "B": "no"},
            reference_answer="A",
            scoring_rubric="Correct full score",
            knowledge_points=["K"],
            score=Decimal("5.00"),
            status=QuestionStatus.APPROVED,
        )
        exam = Exam(
            course=course, creator=teacher, title="History", status=ExamStatus.PUBLISHED
        )
        exam.exam_question_links = [ExamQuestion(question=question, order_index=1)]
        submission = Submission(
            exam=exam, student=student, status=SubmissionStatus.SUBMITTED
        )
        answer = Answer(
            submission=submission,
            question=question,
            content="A",
            status=AnswerStatus.GRADED,
        )
        session.add_all([exam, answer])
        session.flush()
        result = GradingResult(
            answer_id=answer.id,
            submission_id=submission.id,
            question_type=question.type,
            score=Decimal("2.00"),
            max_score=Decimal("3.00"),
            reason="Persisted earlier result",
            confidence=1,
            knowledge_points=["old"],
            review_status=ReviewStatus.CONFIRMED,
            validation_status=ValidationStatus.VALIDATED,
        )
        session.add(result)
        session.commit()
        yield session, teacher, exam, question, submission, answer, result


def _fixed(exam, teacher, score="5.00"):
    link = exam.exam_question_links[0]
    link.score = Decimal(score)
    link.base_score = Decimal("5.00")
    link.published_knowledge_points = ["K"]
    link.scoring_basis = {
        "kind": "objective",
        "points": [
            {
                "key": "correct",
                "label": "Correct full score",
                "base_points": "5.00",
                "default_points": score,
                "confirmed_points": score,
            }
        ],
        "additive": True,
        "rounding_delta": "0.00",
        "confirmation": {
            "teacher_id": str(teacher.id),
            "confirmed_at": datetime.now(UTC).isoformat(),
            "reason": "Explicit synthetic fixture verification",
        },
    }


def test_unknown_history_readable_but_trigger_rejected(historical):
    session, teacher, _exam, _question, submission, answer, result = historical
    reader = DatabaseGradingSubmissionReader(session=session)
    snapshot = reader.load_for_teacher(str(submission.id), str(teacher.id))
    assert snapshot.scoring_basis_error == "EXAM_SCORING_BASIS_MISSING"
    detail = ReviewQueryService(session=session).get_answer_detail(
        str(teacher.id), str(submission.id), str(answer.id)
    )
    assert detail.max_score == Decimal("3.00")
    assert detail.score == Decimal("2.00")
    repo = InMemoryGradingRepository()
    executor = RecordingExecutor()
    service = GradingTaskService(repository=repo, reader=reader, executor=executor)
    with pytest.raises(GradingNotAllowedError, match="EXAM_SCORING_BASIS_MISSING"):
        service.trigger(str(submission.id), teacher_id=str(teacher.id), regrade=True)
    assert not repo.tasks
    assert session.get(GradingResult, result.id).max_score == Decimal("3.00")


@pytest.mark.parametrize(
    "score,expected", [("5.00", None), ("7.00", "EXAM_SCORING_INPUT_NOT_SUPPORTED")]
)
def test_only_complete_equivalent_fixed_basis_can_run(historical, score, expected):
    session, teacher, exam, _question, submission, *_ = historical
    _fixed(exam, teacher, score)
    session.commit()
    snapshot = DatabaseGradingSubmissionReader(session=session).load(str(submission.id))
    assert snapshot.scoring_basis_error == expected
    if expected:
        with pytest.raises(GradingNotAllowedError, match=expected):
            snapshot.require_scoring_ready()
    else:
        snapshot.require_scoring_ready()


def test_current_bank_change_does_not_reauthorize_history(historical):
    session, teacher, exam, question, submission, *_ = historical
    _fixed(exam, teacher)
    session.commit()
    question.score = Decimal("99.00")
    session.commit()
    snapshot = DatabaseGradingSubmissionReader(session=session).load(str(submission.id))
    assert snapshot.scoring_basis_error == "EXAM_SCORING_INPUT_NOT_SUPPORTED"


def test_background_execution_checks_again_before_pipeline(historical):
    session, _teacher, _exam, _question, submission, *_ = historical
    reader = DatabaseGradingSubmissionReader(session=session)
    repo = InMemoryGradingRepository()
    task = make_task("synthetic-task", submission_id=str(submission.id))
    repo.save_task(task)
    pipeline = StubScoringPipeline()
    executor = InlineGradingTaskExecutor(
        repository=repo, reader=reader, pipeline=pipeline
    )
    executor.execute(task.task_id, str(submission.id))
    saved = repo.get_task(task.task_id)
    assert saved.status.value == "Failed"
    assert saved.error_code == "EXAM_SCORING_BASIS_MISSING"
    assert not pipeline.calls


def test_unknown_snapshot_never_defaults_to_execution_permission():
    snapshot = SubmissionSnapshot(
        submission_id="s",
        exam_id="e",
        student_id="u",
        course_id="c",
        status="Submitted",
        answers=(),
    )
    with pytest.raises(GradingNotAllowedError, match="EXAM_SCORING_BASIS_MISSING"):
        snapshot.require_scoring_ready()
