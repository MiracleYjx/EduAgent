"""T176 PostgreSQL identities, JSONB option order and actual image evidence (TCR §31)."""

import hashlib
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.domain.enums import AnswerStatus, ExamStatus, SubmissionStatus
from backend.app.models import (
    Answer,
    Exam,
    ExamQuestion,
    Question,
    QuestionAsset,
    Submission,
    WorkflowRun,
)
from backend.app.schemas.grading import (
    GradingTaskStatus,
    GradingTaskStatusDTO,
    same_fixed_scoring_input,
)
from backend.app.schemas.image_assessment import (
    ConfirmedCondition,
    ImageAssessment,
    ImageFinding,
    ImageManualCheck,
)
from backend.app.services.content_validation_service import ContentValidationService
from backend.app.services.grading.grading_repository import DatabaseGradingRepository
from backend.app.services.grading.grading_task_service import (
    DatabaseGradingSubmissionReader,
    GradingNotAllowedError,
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
            question = session.get(Question, info.objective_question_id)
            question.options = {"D": "four", "B": "two", "A": "one"}
            question.order_preserved = (
                False  # The stored order is real; original lost order stays unknown.
            )
            session.commit()
        yield engine, info


def load(database, *, root=None):
    engine, info = database
    with Session(engine) as session:
        return DatabaseGradingSubmissionReader(session=session, root=root).load(
            str(info.submission_id)
        )


def test_real_postgres_jsonb_checkpoint_preserves_actual_order_and_identity(database):
    engine, info = database
    current = load(database)
    repository = DatabaseGradingRepository(session_factory=lambda: Session(engine))
    task_id = str(uuid4())
    task = GradingTaskStatusDTO(
        task_id=task_id,
        submission_id=str(info.submission_id),
        status=GradingTaskStatus.QUEUED,
        durable=True,
        created_at=datetime.now(UTC),
    )
    repository.save_task(
        task, request_id="t176-real-pg-input", scoring_inputs=current.scoring_inputs
    )
    actual = repository.get_scoring_inputs(task_id)
    current.require_fixed_inputs(actual)
    value = actual[str(info.objective_answer_id)]
    assert list(value.options) == ["D", "B", "A"] and value.order_preserved is False
    with Session(engine) as session:
        row = session.scalar(
            select(WorkflowRun).where(WorkflowRun.workflow_id == task_id)
        )
        row_id = row.id
        assert row.checkpoint["scoring_inputs"][str(info.objective_answer_id)][
            "ordered_options"
        ] == [["D", "four"], ["B", "two"], ["A", "one"]]
    repository.save_task(
        task, request_id="t176-real-pg-input", scoring_inputs=current.scoring_inputs
    )
    with Session(engine) as session:
        assert (
            session.scalar(
                select(WorkflowRun).where(WorkflowRun.workflow_id == task_id)
            ).id
            == row_id
        )
    assert same_fixed_scoring_input(
        value, load(database).scoring_inputs[str(info.objective_answer_id)]
    )


def test_real_postgres_two_exams_do_not_reuse_question_score_or_labels(database):
    engine, info = database
    first = load(database).scoring_inputs[str(info.objective_answer_id)]
    with Session(engine) as session:
        source = session.get(Exam, info.exam_id).exam_question_links[0]
        raw = deepcopy(source.scoring_basis)
        raw["points"][0]["default_points"] = raw["points"][0]["confirmed_points"] = (
            "20.00"
        )
        exam = Exam(
            course_id=info.course_id,
            created_by=info.teacher_id,
            title="Second PG exam",
            status=ExamStatus.PUBLISHED,
        )
        session.add(exam)
        session.flush()
        link = ExamQuestion(
            exam=exam,
            question_id=info.objective_question_id,
            order_index=1,
            score=Decimal("20.00"),
            base_score=Decimal("10.00"),
            published_knowledge_points=["Published second exam label"],
            scoring_basis=raw,
        )
        session.add(link)
        submission = Submission(
            exam=exam, student_id=info.student_id, status=SubmissionStatus.SUBMITTED
        )
        session.add(submission)
        session.flush()
        answer = Answer(
            submission_id=submission.id,
            question_id=info.objective_question_id,
            content="A",
            status=AnswerStatus.SUBMITTED,
        )
        session.add(answer)
        session.commit()
        value = (
            DatabaseGradingSubmissionReader(session=session)
            .load(str(submission.id))
            .scoring_inputs[str(answer.id)]
        )
        assert value.question_id == first.question_id
        assert (
            value.exam_question_id != first.exam_question_id
            and value.answer_id != first.answer_id
        )
        assert value.effective_score == Decimal(
            "20.00"
        ) and first.effective_score == Decimal("10.00")
        assert value.published_knowledge_points == ["Published second exam label"]
        assert session.get(Question, info.objective_question_id).score == Decimal(
            "10.00"
        )


def image_fixture(database, root):
    engine, info = database
    pixels = BytesIO()
    Image.new("RGB", (8, 6), "white").save(pixels, format="PNG")
    data = pixels.getvalue()
    image_path = root / "assets" / "verified.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(data)
    with Session(engine) as session:
        question = session.get(Question, info.objective_question_id)
        asset = QuestionAsset(
            question=question,
            asset_type="diagram",
            width=8,
            height=6,
            caption="Synthetic image",
            order_index=1,
            student_visible=True,
            _file_path="assets/verified.png",
            _file_metadata={
                "media_type": "image/png",
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "migration": {"status": "not_required", "latest_attempt": None},
            },
        )
        session.add(asset)
        session.flush()
        service = ContentValidationService(session, root=root)
        refs, _ = service._read_images(service._image_refs(question), info.teacher_id)
        check = ImageManualCheck(
            id=uuid4(),
            check_no=1,
            context_revision=0,
            run_no=0,
            run_id=None,
            input_refs=refs,
            status="confirmed",
            confirmed_conditions=[
                ConfirmedCondition(
                    condition_id=uuid4(),
                    asset_id=asset.id,
                    text="Synthetic reviewed condition",
                    evidence_region=None,
                    source_condition_id=None,
                )
            ],
            image_findings=[
                ImageFinding(
                    asset_id=asset.id,
                    finding="conditions_confirmed",
                    reason="Controlled fixture authoring",
                )
            ],
            issues=[],
            issue_resolutions=[],
            teacher_id=info.teacher_id,
            checked_at=datetime.now(UTC),
            explanation="Synthetic current event; not independent teacher annotation",
        )
        question.image_assessment = ImageAssessment(manual_checks=[check]).model_dump(
            mode="json"
        )
        session.commit()
        return image_path, asset.id, check.id


def test_real_current_image_review_is_carried_without_bytes_paths_or_new_events(
    database, tmp_path
):
    image_path, asset_id, check_id = image_fixture(database, tmp_path)
    current = load(database, root=tmp_path)
    value = current.scoring_inputs[str(database[1].objective_answer_id)]
    evidence = value.verified_image_conditions
    assert evidence.check.id == check_id and evidence.review_ref.check_id == check_id
    assert evidence.check.teacher_id == database[1].teacher_id
    assert evidence.input_refs.images[0].asset_id == asset_id
    assert (
        evidence.input_refs.images[0].width == 8
        and evidence.input_refs.images[0].height == 6
    )
    assert evidence.check.confirmed_conditions[0].text == "Synthetic reviewed condition"
    encoded = value.model_dump_json()
    assert (
        str(image_path) not in encoded
        and "storage_path" not in encoded
        and "iVBOR" not in encoded
    )
    assert same_fixed_scoring_input(
        value, load(database, root=tmp_path).scoring_inputs[value.answer_id]
    )
    current.require_scoring_ready()


@pytest.mark.parametrize(
    "change,code",
    [
        ("missing", "FILE_MISSING"),
        ("changed", "FILE_CONTENT_CHANGED"),
        ("unreviewed", "VISION_REVIEW_REQUIRED"),
    ],
)
def test_real_image_failure_never_produces_confirmed_input(
    database, tmp_path, change, code
):
    path, _asset, _check = image_fixture(database, tmp_path)
    if change == "missing":
        path.unlink()
    elif change == "changed":
        path.write_bytes(b"Changed registered bytes")
    else:
        with Session(database[0]) as session:
            session.get(
                Question, database[1].objective_question_id
            ).image_assessment = None
            session.commit()
    current = load(database, root=tmp_path)
    assert (
        current.scoring_inputs[
            str(database[1].objective_answer_id)
        ].verified_image_conditions
        is None
    )
    with pytest.raises(GradingNotAllowedError) as error:
        current.require_scoring_ready()
    assert error.value.error_code == code
