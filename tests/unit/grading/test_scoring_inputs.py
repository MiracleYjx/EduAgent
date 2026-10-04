"""T176 fixed exam scoring inputs and repository ownership (TCR §31)."""

from copy import deepcopy
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.domain.enums import AnswerStatus, ExamStatus, SubmissionStatus
from backend.app.models import Answer, Exam, ExamQuestion, GradingResult, Submission
from backend.app.schemas.ai import GradingResult as ResultPayload
from backend.app.schemas.grading import (
    ScoringInput,
    same_fixed_scoring_input,
    scoring_input_identity,
)
from backend.app.services.grading.grading_repository import DatabaseGradingRepository
from backend.app.services.grading.grading_task_service import (
    DatabaseGradingSubmissionReader,
    GradingNotAllowedError,
    GradingResultOwnershipError,
)
from tests.support.exam_scoring_fixtures import confirm_synthetic_exam_basis
from tests.unit.models.sqlite_support import create_sqlite_engine, seed_submission


@pytest.fixture
def database():
    engine = create_sqlite_engine()
    with Session(engine) as session:
        info = seed_submission(session)
        confirm_synthetic_exam_basis(session, info.exam_id)
        question = session.get(Exam, info.exam_id).exam_question_links[0].question
        question.options = {"D": "last", "A": "first", "C": "middle"}
        session.commit()
    yield engine, info
    engine.dispose()


def snapshot(database):
    engine, info = database
    with Session(engine) as session:
        return DatabaseGradingSubmissionReader(session=session).load(
            str(info.submission_id)
        )


def test_reader_carries_real_identity_basis_options_and_unretrieved_state(database):
    current = snapshot(database)
    engine, info = database
    value = current.scoring_inputs[str(info.objective_answer_id)]
    with Session(engine) as session:
        link = session.scalar(
            select(ExamQuestion).where(
                ExamQuestion.exam_id == info.exam_id,
                ExamQuestion.question_id == info.objective_question_id,
            )
        )
        assert value.exam_question_id == str(link.id)
        assert value.scoring_basis.model_dump(mode="json") == link.scoring_basis
    assert scoring_input_identity(value) == (
        str(info.exam_id),
        value.exam_question_id,
        str(info.objective_question_id),
        str(info.submission_id),
        str(info.objective_answer_id),
        str(info.student_id),
        str(info.course_id),
    )
    assert value.effective_score == value.base_score == Decimal("10.00")
    assert list(value.options) == ["D", "A", "C"]
    assert value.course_context is None and value.source_references is None
    assert value.assets == [] and value.verified_image_conditions is None
    context = current.to_context()
    assert context.expected_answers[0].exam_question_id == value.exam_question_id


def test_fixed_compare_keeps_option_order_and_all_business_identities(database):
    value = next(iter(snapshot(database).scoring_inputs.values()))
    raw = value.model_dump(mode="json")
    raw["ordered_options"] = list(reversed(raw["ordered_options"]))
    assert not same_fixed_scoring_input(value, ScoringInput.model_validate(raw))
    for key in ("exam_question_id", "exam_id", "answer_id", "submission_id"):
        changed = value.model_copy(update={key: str(uuid4())})
        assert not same_fixed_scoring_input(value, changed)


def test_fixed_compare_allows_only_actual_retrieval_enrichment(database):
    value = next(iter(snapshot(database).scoring_inputs.values()))
    enriched = value.model_copy(
        update={"course_context": "Actual retrieved text", "source_references": []}
    )
    assert same_fixed_scoring_input(value, enriched)
    assert not same_fixed_scoring_input(
        value, value.model_copy(update={"student_answer": "Other actual answer"})
    )


def test_input_json_round_trip_preserves_decimal_and_options_order(database):
    value = next(iter(snapshot(database).scoring_inputs.values()))
    assert value.model_dump(mode="json")["effective_score"] == "10.00"
    assert same_fixed_scoring_input(
        value, ScoringInput.model_validate_json(value.model_dump_json())
    )
    raw = value.model_dump(mode="json")
    raw["effective_score"] = 10.0
    with pytest.raises(ValidationError):
        ScoringInput.model_validate(raw)


def test_snapshot_rejects_foreign_or_missing_fixed_input(database):
    current = snapshot(database)
    current.require_fixed_inputs(current.scoring_inputs)
    wrong = deepcopy(current.scoring_inputs)
    key = next(iter(wrong))
    wrong[key] = wrong[key].model_copy(update={"exam_question_id": str(uuid4())})
    with pytest.raises(GradingResultOwnershipError):
        current.require_fixed_inputs(wrong)
    with pytest.raises(GradingResultOwnershipError):
        current.require_fixed_inputs({key: current.scoring_inputs[key]})


def test_same_question_two_exams_use_independent_fixed_scores(database):
    engine, info = database
    with Session(engine) as session:
        first = session.get(Exam, info.exam_id)
        question = session.get(ExamQuestion, first.exam_question_links[0].id).question
        second = Exam(
            course_id=info.course_id,
            created_by=info.teacher_id,
            title="Separate controlled exam",
            status=ExamStatus.PUBLISHED,
        )
        session.add(second)
        session.flush()
        raw = deepcopy(first.exam_question_links[0].scoring_basis)
        raw["points"][0]["default_points"] = "20.00"
        raw["points"][0]["confirmed_points"] = "20.00"
        session.add(
            ExamQuestion(
                exam=second,
                question=question,
                order_index=1,
                score=Decimal("20.00"),
                base_score=Decimal("10.00"),
                published_knowledge_points=["本场已发布知识点"],
                scoring_basis=raw,
            )
        )
        submission = Submission(
            exam=second, student_id=info.student_id, status=SubmissionStatus.SUBMITTED
        )
        session.add(submission)
        session.flush()
        answer = Answer(
            submission_id=submission.id,
            question_id=question.id,
            content="A",
            status=AnswerStatus.SUBMITTED,
        )
        session.add(answer)
        session.commit()
        second_snapshot = DatabaseGradingSubmissionReader(session=session).load(
            str(submission.id)
        )
        value = second_snapshot.scoring_inputs[str(answer.id)]
        assert value.effective_score == Decimal("20.00")
        assert value.base_score == Decimal("10.00")
        assert value.published_knowledge_points == ["本场已发布知识点"]
        assert second_snapshot.answers[0].max_score == Decimal("20.00")
        assert (
            list(second_snapshot.answers[0].knowledge_points)
            == value.published_knowledge_points
        )
        assert (
            value.exam_question_id
            != next(iter(snapshot(database).scoring_inputs.values())).exam_question_id
        )
        with pytest.raises(GradingNotAllowedError) as error:
            second_snapshot.require_scoring_ready()
        assert error.value.error_code == "EXAM_SCORING_INPUT_NOT_SUPPORTED"


def test_missing_historical_basis_never_fabricates_fixed_input(database):
    engine, info = database
    with Session(engine) as session:
        link = session.get(Exam, info.exam_id).exam_question_links[0]
        link.score = link.base_score = link.scoring_basis = (
            link.published_knowledge_points
        ) = None
        session.commit()
        current = DatabaseGradingSubmissionReader(session=session).load(
            str(info.submission_id)
        )
        assert current.answers[0].scoring_input is None
        assert current.answers[0].max_score is None
        with pytest.raises(GradingNotAllowedError):
            current.to_context()
        with pytest.raises(GradingNotAllowedError) as error:
            current.require_scoring_ready()
        assert error.value.error_code == "EXAM_SCORING_BASIS_MISSING"


def test_repository_rejects_result_using_another_exam_full_score(database):
    engine, info = database
    repository = DatabaseGradingRepository(session_factory=lambda: Session(engine))
    payload = ResultPayload(
        question_type="SINGLE_CHOICE",
        score=20.0,
        max_score=20.0,
        reason="Controlled wrong exam basis",
        correct_points=[],
        missing_knowledge_points=[],
        knowledge_points=["数据类型"],
        suggestions=[],
        confidence=1.0,
        answer_id=str(info.objective_answer_id),
        submission_id=str(info.submission_id),
    )
    with Session(engine) as session:
        submission = session.get(Submission, info.submission_id)
        with pytest.raises(GradingResultOwnershipError):
            repository._upsert_result(session, submission, payload, {})
        session.rollback()
        assert session.scalar(select(GradingResult)) is None


@pytest.mark.parametrize(
    "options", [["list", "tuple"], [["A", "first"], ["B", "second"]], {}, None]
)
def test_original_list_mapping_and_null_options_keep_distinct_shapes(database, options):
    current = next(iter(snapshot(database).scoring_inputs.values()))
    value = current.model_copy(update={"options": options})
    rebuilt = ScoringInput.model_validate_json(value.model_dump_json())
    assert rebuilt.options == options and type(rebuilt.options) is type(options)
    assert same_fixed_scoring_input(value, rebuilt)


def test_production_worker_cannot_replace_legacy_missing_checkpoint_inputs(database):
    from datetime import UTC, datetime

    from backend.app.schemas.grading import GradingTaskStatus, GradingTaskStatusDTO
    from backend.app.services.grading.grading_task_service import (
        InlineGradingTaskExecutor,
    )

    engine, info = database
    repository = DatabaseGradingRepository(session_factory=lambda: Session(engine))
    task = GradingTaskStatusDTO(
        task_id="missing-fixed-checkpoint",
        submission_id=str(info.submission_id),
        status=GradingTaskStatus.QUEUED,
        durable=True,
        created_at=datetime.now(UTC),
    )
    repository.save_task(task, request_id="real-legacy-missing-input")

    class Pipeline:
        calls = 0

        def score(self, value):
            self.calls += 1
            raise AssertionError("Missing persisted input must block before grading")

    pipeline = Pipeline()
    InlineGradingTaskExecutor(
        repository=repository,
        reader=DatabaseGradingSubmissionReader(session_factory=lambda: Session(engine)),
        pipeline=pipeline,
    ).execute(task.task_id, task.submission_id)
    assert pipeline.calls == 0
    assert (
        repository.get_task(task.task_id).error_code
        == "GRADING_RESULT_OWNERSHIP_MISMATCH"
    )


@pytest.mark.parametrize("field", ["exam_id", "student_id"])
def test_whole_result_cannot_override_actual_submission_owner(database, field):
    from backend.app.services.grading.result_aggregator import ResultAggregator

    engine, _info = database
    value = ResultAggregator().aggregate(snapshot(database).to_context(), results=[])
    value = value.model_copy(update={field: str(uuid4())})
    repository = DatabaseGradingRepository(session_factory=lambda: Session(engine))
    with pytest.raises(GradingResultOwnershipError):
        repository.save_exam_result(value)


def test_workflow_context_cannot_claim_foreign_association(database):
    engine, info = database
    current = snapshot(database)
    context = current.to_context()
    entries = list(context.expected_answers)
    entries[0] = entries[0].model_copy(update={"exam_question_id": str(uuid4())})
    context = context.model_copy(update={"expected_answers": entries})
    repository = DatabaseGradingRepository(session_factory=lambda: Session(engine))

    def never_write_state(session):
        raise AssertionError("Invalid context must fail before writing any state")

    with pytest.raises(GradingResultOwnershipError):
        repository.save_workflow_outcome(
            str(info.submission_id),
            context=context,
            scoring_inputs=current.scoring_inputs,
            state_writer=never_write_state,
        )
