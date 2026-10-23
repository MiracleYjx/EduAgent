"""T178 fixed student input and authorized images; synthetic review fixtures (TCR §33)."""

import base64
import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy.orm import Session

from backend.app.domain.enums import SubmissionStatus
from backend.app.models import Exam, Question, QuestionAsset, Submission
from backend.app.schemas.exam_scoring import ScoringBasis
from backend.app.schemas.image_assessment import (
    ConfirmedCondition,
    ImageAssessment,
    ImageFinding,
    ImageManualCheck,
)
from backend.app.services.content_validation_service import ContentValidationService
from backend.app.services.submission_service import (
    SubmissionService,
    SubmissionServiceError,
)
from tests.postgres_helpers import isolated_test_resource_profile
from tests.support.exam_scoring_fixtures import confirm_synthetic_exam_basis
from tests.unit.models.sqlite_support import create_sqlite_engine, seed_submission


@pytest.fixture
def scenario(tmp_path):
    engine = create_sqlite_engine()
    with isolated_test_resource_profile((tmp_path / "profile").resolve()) as profile, Session(engine) as session:
        info = seed_submission(session, status=SubmissionStatus.DRAFT)
        confirm_synthetic_exam_basis(session, info.exam_id)
        exam = session.get(Exam, info.exam_id)
        first, second = exam.exam_question_links
        first.order_index, second.order_index = 7, 8
        session.flush()
        first.order_index, second.order_index = 2, 1
        first.score, second.score = Decimal("12.34"), Decimal("5.66")
        basis = ScoringBasis.model_validate(first.scoring_basis)
        first.scoring_basis = basis.model_copy(
            update={
                "points": [
                    point.model_copy(
                        update={
                            "default_points": first.score,
                            "confirmed_points": first.score,
                        }
                    )
                    for point in basis.points
                ]
            }
        ).model_dump(mode="json")
        first.published_knowledge_points = ["fixed objective"]
        second.published_knowledge_points = ["fixed subjective"]
        first.question.score = second.question.score = Decimal("99.00")
        first.question.knowledge_points = second.question.knowledge_points = [
            "bank label"
        ]
        first.question.options = {"D": "last", "A": "first", "C": "middle"}
        session.commit()
        yield session, info, profile.storage_root
    engine.dispose()


def add_image(scenario, *, visible=True, necessary=True):
    session, info, root = scenario
    data = BytesIO()
    with Image.new("RGB", (8, 6), "white") as image:
        image.save(data, format="PNG")
    content = data.getvalue()
    path = root / "assets" / "student.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    question = session.get(Question, info.objective_question_id)
    asset = QuestionAsset(
        question=question,
        asset_type="diagram",
        width=8,
        height=6,
        caption="Controlled figure",
        order_index=1,
        student_visible=visible,
        _file_path="assets/student.png",
        _file_metadata={
            "media_type": "image/png",
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "migration": {"status": "not_required", "latest_attempt": None},
        },
    )
    session.add(asset)
    session.flush()
    validation = ContentValidationService(session, root=root)
    refs, _ = validation._read_images(validation._image_refs(question), info.teacher_id)
    check = ImageManualCheck(
        id=uuid4(),
        check_no=1,
        context_revision=0,
        run_no=0,
        run_id=None,
        input_refs=refs,
        status="confirmed",
        confirmed_conditions=(
            [
                ConfirmedCondition(
                    condition_id=uuid4(),
                    asset_id=asset.id,
                    text="Fixture necessary figure condition",
                    evidence_region=None,
                    source_condition_id=None,
                )
            ]
            if necessary
            else []
        ),
        image_findings=[
            ImageFinding(
                asset_id=asset.id,
                finding="conditions_confirmed" if necessary else "no_conditions_needed",
                reason="Synthetic fixture reviewed by test author",
            )
        ],
        issues=[],
        issue_resolutions=[],
        teacher_id=info.teacher_id,
        checked_at=datetime.now(UTC),
        explanation="Controlled synthetic review, not independent teacher annotation",
    )
    question.image_assessment = ImageAssessment(manual_checks=[check]).model_dump(
        mode="json"
    )
    session.commit()
    return asset, path, content


def test_student_detail_and_summary_use_same_fixed_order_values_and_labels(scenario):
    session, info, root = scenario
    service = SubmissionService(session, root=root)
    actual = service.get_exam_detail(info.exam_id, student_id=info.student_id)
    assert [q.id for q in actual.questions] == [
        str(info.subjective_question_id),
        str(info.objective_question_id),
    ]
    assert [q.position for q in actual.questions] == [1, 2]
    assert [q.score for q in actual.questions] == [Decimal("5.66"), Decimal("12.34")]
    assert [q.knowledge_points for q in actual.questions] == [
        ["fixed subjective"],
        ["fixed objective"],
    ]
    assert list(actual.questions[1].options) == ["D", "A", "C"]
    assert actual.total_score == Decimal("18.00")
    assert [a.question_id for a in actual.submission.answers] == actual.question_ids
    assert all(q.exam_question_id for q in actual.questions)
    public = actual.model_dump_json()
    assert all(
        key not in public
        for key in [
            "reference_answer",
            "scoring_rubric",
            "analysis",
            "scoring_basis",
            "source_page_id",
            "storage_path",
        ]
    )


def test_unknown_historical_score_is_not_bank_default(scenario):
    session, info, root = scenario
    link = session.get(Exam, info.exam_id).exam_question_links[0]
    link.score = None
    session.commit()
    service = SubmissionService(session, root=root)
    assert (
        service.get_available_exam(info.exam_id, student_id=info.student_id).total_score
        is None
    )
    detail = service.get_exam_detail(info.exam_id, student_id=info.student_id)
    assert (
        next(q for q in detail.questions if q.id == str(link.question_id)).score is None
    )


def test_actual_visible_image_is_inline_and_has_no_private_provenance(scenario):
    asset, _path, content = add_image(scenario)
    session, info, root = scenario
    detail = SubmissionService(session, root=root).get_exam_detail(
        info.exam_id, student_id=info.student_id, include_images=True
    )
    image = next(q for q in detail.questions if q.id == str(asset.question_id)).assets[
        0
    ]
    assert image.image_data == "data:image/png;base64," + base64.b64encode(
        content
    ).decode("ascii")
    public = detail.model_dump_json()
    assert all(
        key not in public
        for key in [
            "image_data",
            "source_page_id",
            "region",
            "storage_path",
            "reference_answer",
        ]
    )
    assert "iVBOR" not in public


@pytest.mark.parametrize(
    "change,code",
    [
        ("hidden", "EXAM_REQUIRED_IMAGE_UNAVAILABLE"),
        ("missing", "FILE_MISSING"),
        ("changed", "FILE_CONTENT_CHANGED"),
    ],
)
def test_required_hidden_or_missing_image_cannot_start_or_submit(
    scenario, change, code
):
    asset, path, _content = add_image(scenario)
    session, info, root = scenario
    if change == "hidden":
        asset.student_visible = False
        session.commit()
    elif change == "missing":
        path.unlink()
    else:
        path.write_bytes(b"changed fixture bytes")
    service = SubmissionService(session, root=root)
    for operation in [
        lambda: service.create_submission(
            info.exam_id, student_id=info.student_id, initialize_answers=True
        ),
        lambda: service.submit_submission(
            info.submission_id,
            student_id=info.student_id,
            answers={
                str(info.objective_question_id): "D",
                str(info.subjective_question_id): "text",
            },
        ),
    ]:
        with pytest.raises(SubmissionServiceError, match=code):
            operation()
        assert (
            session.get(Submission, info.submission_id).status is SubmissionStatus.DRAFT
        )


def test_teacher_only_image_without_necessary_conditions_does_not_become_public(
    scenario,
):
    asset, _path, _content = add_image(scenario, visible=False, necessary=False)
    session, info, root = scenario
    detail = SubmissionService(session, root=root).get_exam_detail(
        info.exam_id, student_id=info.student_id, include_images=True
    )
    assert (
        next(q for q in detail.questions if q.id == str(asset.question_id)).assets == []
    )
    assert str(asset.id) not in detail.model_dump_json()


@pytest.mark.parametrize("operation", ["start", "save", "submit"])
def test_unknown_historical_basis_keeps_legacy_submission_rules(scenario, operation):
    from backend.app.domain.enums import AnswerStatus
    from backend.app.services.grading.grading_task_service import (
        DatabaseGradingSubmissionReader,
        GradingNotAllowedError,
    )

    session, info, root = scenario
    link = session.get(Exam, info.exam_id).exam_question_links[0]
    link.score = None
    link.base_score = None
    link.scoring_basis = None
    link.published_knowledge_points = None
    for answer in session.get(Submission, info.submission_id).answers:
        answer.status = AnswerStatus.DRAFT
    session.commit()
    service = SubmissionService(session, root=root)
    actions = {
        "start": lambda: service.create_submission(
            info.exam_id, student_id=info.student_id
        ),
        "save": lambda: service.save_answers(
            info.submission_id,
            {str(info.objective_question_id): "D"},
            student_id=info.student_id,
        ),
        "submit": lambda: service.submit_submission(
            info.submission_id,
            student_id=info.student_id,
            answers={
                str(info.objective_question_id): "D",
                str(info.subjective_question_id): "text",
            },
        ),
    }
    result = actions[operation]()
    if operation == "save":
        assert [answer.question_id for answer in result] == [
            str(info.objective_question_id)
        ]
        assert result[0].content == "D"
    else:
        assert result.id == str(info.submission_id)
    assert (
        service.get_available_exam(info.exam_id, student_id=info.student_id).total_score
        is None
    )
    assert session.get(Exam, info.exam_id).exam_question_links[0].score is None
    if operation == "submit":
        snapshot = DatabaseGradingSubmissionReader(session=session, root=root).load(
            str(info.submission_id)
        )
        with pytest.raises(GradingNotAllowedError, match="EXAM_SCORING_BASIS_MISSING"):
            snapshot.require_scoring_ready()
