"""T174 real PG scoring preparation/confirmation and source invalidation (TCR §29)."""

from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from backend.app.domain.enums import ExamStatus, QuestionStatus
from backend.app.models import Exam, Question
from backend.app.schemas.exam_assembly import ExamQuestionPatchRequest
from backend.app.schemas.exam_scoring import (
    ScoringConfirmRequest,
    ScoringPrepareRequest,
)
from backend.app.services.exam_assembly_service import AssemblyError
from backend.app.services.exam_scoring_service import (
    ExamScoringService,
    require_scoring_ready,
)
from backend.app.services.exam_service import ExamService
from backend.app.services.question_service import QuestionService
from tests.integration.test_exam_assembly import database as _assembly_database

database = _assembly_database


def view(service, info):
    return service.get_scoring_basis(
        info["exam"], info["questions"][1], teacher_id=info["actor"]
    )


def prepare_payload(current, *, additive=True, points=None):
    return ScoringPrepareRequest.model_validate(
        {
            "expected_basis": (
                current.basis.model_dump(mode="json") if current.basis else None
            ),
            "expected_question_validation_revision": current.question_validation_revision,
            "expected_effective_score": str(current.effective_score),
            "expected_base_score": (
                str(current.base_score) if current.base_score is not None else None
            ),
            "additive": additive,
            "points": (
                points
                if points is not None
                else [
                    {
                        "key": str(i),
                        "label": f"Controlled point {i}",
                        "base_points": "1.00",
                    }
                    for i in range(3)
                ]
            ),
        }
    )


def confirm_payload(current, values=None, reason="Explicit controlled teacher review"):
    return ScoringConfirmRequest.model_validate(
        {
            "preparation_id": current.basis.preparation_id,
            "expected_basis": current.basis.model_dump(mode="json"),
            "expected_question_validation_revision": current.question_validation_revision,
            "expected_effective_score": str(current.effective_score),
            "expected_base_score": str(current.base_score),
            "confirmed_points": [
                {"key": point.key, "points": value}
                for point, value in zip(current.basis.points, values or [], strict=True)
            ],
            "reason": reason,
        }
    )


def set_score(session, info, score):
    ExamService(session).patch_exam_question(
        info["exam"],
        info["questions"][1],
        ExamQuestionPatchRequest(score=score),
        teacher_id=info["actor"],
    )


def prepare(service, info, payload):
    return service.prepare_scoring_basis(
        info["exam"], info["questions"][1], payload, teacher_id=info["actor"]
    )


def confirm(service, info, payload):
    return service.confirm_scoring_basis(
        info["exam"], info["questions"][1], payload, teacher_id=info["actor"]
    )


@pytest.mark.parametrize(
    "score,defaults,delta,final",
    [
        ("10.00", "3.33", "0.01", ["3.34", "3.33", "3.33"]),
        ("1.01", "0.34", "-0.01", ["0.33", "0.34", "0.34"]),
    ],
)
def test_signed_rounding_and_real_confirmation(database, score, defaults, delta, final):
    engine, info = database
    with Session(engine) as session:
        set_score(session, info, score)
        service = ExamScoringService(session)
        current = prepare(service, info, prepare_payload(view(service, info)))
        assert current.base_score == Decimal("3.00")
        assert [p.default_points for p in current.basis.points] == [
            Decimal(defaults)
        ] * 3
        assert (
            current.basis.rounding_delta == Decimal(delta)
            and current.basis.confirmation is None
        )
        exam = session.get(Exam, info["exam"])
        with pytest.raises(AssemblyError) as error:
            require_scoring_ready(exam, exam.exam_question_links[0])
        assert error.value.code == "RUBRIC_ROUNDING_UNCONFIRMED"
        payload = confirm_payload(current, final)
        result = confirm(service, info, payload)
        assert [p.default_points for p in result.basis.points] == [
            Decimal(defaults)
        ] * 3
        assert [p.confirmed_points for p in result.basis.points] == list(
            map(Decimal, final)
        )
        assert result.basis.confirmation.teacher_id == info["actor"]
        assert result.basis.confirmation.confirmed_at.utcoffset().total_seconds() == 0
        assert confirm(service, info, payload).basis == result.basis
        assert session.get(Question, info["questions"][1]).score == Decimal("3.00")


def test_qualitative_requires_explicit_confirmation_and_preserves_unknown_points(
    database,
):
    engine, info = database
    with Session(engine) as session:
        set_score(session, info, None)
        service = ExamScoringService(session)
        current = prepare(
            service,
            info,
            prepare_payload(view(service, info), additive=False, points=[]),
        )
        assert (
            current.explicit_score is None
            and current.basis.points == []
            and current.basis.rounding_delta is None
        )
        exam = session.get(Exam, info["exam"])
        with pytest.raises(AssemblyError) as error:
            require_scoring_ready(exam, exam.exam_question_links[0])
        assert error.value.code == "RUBRIC_REVIEW_REQUIRED"
        result = confirm(service, info, confirm_payload(current))
        assert result.basis.points == [] and result.basis.confirmation.reason


def test_prepare_same_input_keeps_current_identity_and_confirmation(database):
    engine, info = database
    with Session(engine) as session:
        set_score(session, info, "10.00")
        service = ExamScoringService(session)
        payload = prepare_payload(view(service, info))
        first = prepare(service, info, payload)
        checked = confirm(
            service, info, confirm_payload(first, ["3.34", "3.33", "3.33"])
        )
        assert prepare(service, info, payload).basis == checked.basis
        assert prepare(service, info, prepare_payload(checked)).basis == checked.basis


def test_score_a_b_a_never_revives_old_preparation(database):
    engine, info = database
    with Session(engine) as session:
        set_score(session, info, "10.00")
        service = ExamScoringService(session)
        old = prepare(service, info, prepare_payload(view(service, info)))
        delayed = confirm_payload(old, ["3.34", "3.33", "3.33"])
        set_score(session, info, "20.00")
        set_score(session, info, "10.00")
        new = prepare(service, info, prepare_payload(view(service, info)))
        assert new.basis.preparation_id != old.basis.preparation_id
        with pytest.raises(AssemblyError) as error:
            confirm(service, info, delayed)
        assert error.value.code == "EXAM_SCORING_CONTEXT_CHANGED"
        assert view(service, info).basis == new.basis


@pytest.mark.parametrize(
    "change",
    [
        {"content": "A genuinely changed controlled prompt."},
        {"reference_answer": "Different real answer."},
        {"scoring_rubric": "Different real rubric."},
        {"analysis": "Different real explanation."},
        {"score": "4.00"},
    ],
)
def test_real_source_edit_invalidates_draft_but_same_value_does_not(database, change):
    engine, info = database
    with Session(engine) as session:
        service = ExamScoringService(session)
        initial = prepare(
            service,
            info,
            prepare_payload(view(service, info), additive=False, points=[]),
        )
        confirm(service, info, confirm_payload(initial))
        questions = QuestionService(session)
        questions.update_question_status(
            info["questions"][1],
            QuestionStatus.NEEDS_REVISION,
            teacher_id=info["actor"],
            revision_comment="Actual controlled revision request",
        )
        before = view(service, info)
        old_content = session.get(Question, info["questions"][1]).content
        questions.update_question(
            info["questions"][1], content=old_content, teacher_id=info["actor"]
        )
        assert view(service, info).basis == before.basis
        questions.update_question(
            info["questions"][1], teacher_id=info["actor"], **change
        )
        after = view(service, info)
        assert after.basis is None and after.base_score is None
        assert (
            after.question_validation_revision
            == before.question_validation_revision + 1
        )


def test_prepare_bad_sum_and_confirmation_bad_total_preserve_basis(database):
    engine, info = database
    with Session(engine) as session:
        set_score(session, info, "10.00")
        service = ExamScoringService(session)
        initial = view(service, info)
        with pytest.raises(AssemblyError):
            prepare(
                service,
                info,
                prepare_payload(
                    initial,
                    points=[
                        {
                            "key": "a",
                            "label": "Insufficient original weight",
                            "base_points": "1.00",
                        }
                    ],
                ),
            )
        assert view(service, info).basis is None
        ready = prepare(service, info, prepare_payload(view(service, info)))
        with pytest.raises(AssemblyError):
            confirm(service, info, confirm_payload(ready, ["3.33", "3.33", "3.33"]))
        assert view(service, info).basis == ready.basis


def test_stale_session_cannot_overwrite_new_teacher_confirmation(database):
    engine, info = database
    with Session(engine) as old:
        set_score(old, info, "10.00")
        service = ExamScoringService(old)
        initial = prepare(service, info, prepare_payload(view(service, info)))
        old.commit()
        stale = confirm_payload(initial, ["3.34", "3.33", "3.33"], reason="Old request")
        with Session(engine) as fresh:
            updated = confirm(
                ExamScoringService(fresh),
                info,
                confirm_payload(
                    initial, ["3.33", "3.34", "3.33"], reason="First committed review"
                ),
            )
        with pytest.raises(AssemblyError):
            confirm(service, info, stale)
        assert view(service, info).basis == updated.basis


def test_unknown_published_values_remain_readable_and_cannot_prepare(database):
    engine, info = database
    with Session(engine) as session:
        exam = session.get(Exam, info["exam"])
        link = exam.exam_question_links[0]
        link.score = link.base_score = link.scoring_basis = None
        exam.status = ExamStatus.PUBLISHED
        session.commit()
        service = ExamScoringService(session)
        current = view(service, info)
        assert (
            current.effective_score is None
            and current.base_score is None
            and current.basis is None
            and not current.editable
        )
        payload = ScoringPrepareRequest.model_validate(
            {
                "expected_question_validation_revision": current.question_validation_revision,
                "expected_effective_score": "3.00",
                "expected_base_score": None,
                "expected_basis": None,
                "additive": False,
                "points": [],
            }
        )
        with pytest.raises(AssemblyError):
            prepare(service, info, payload)


def test_prepare_schema_rejects_floats_duplicate_keys_and_fake_confirmation():
    raw = {
        "expected_question_validation_revision": 1,
        "expected_effective_score": "3.00",
        "expected_base_score": None,
        "expected_basis": None,
        "additive": True,
        "points": [{"key": "a", "label": "A", "base_points": "3.00"}],
    }
    for change in (
        {"expected_effective_score": 3.0},
        {"points": [raw["points"][0]] * 2},
        {"teacher_id": str(uuid4())},
    ):
        with pytest.raises(ValidationError):
            ScoringPrepareRequest.model_validate(raw | change)


def test_history_manifest_cannot_forge_current_preparation_identity():
    from backend.app.services.exam_history_service import HistoricalQuestionFacts

    with pytest.raises(ValidationError, match="准备|preparation"):
        HistoricalQuestionFacts.model_validate(
            {
                "question_id": str(uuid4()),
                "order_index": 1,
                "score": "3.00",
                "base_score": "3.00",
                "published_knowledge_points": [],
                "scoring_basis": {
                    "kind": "subjective",
                    "additive": False,
                    "rounding_delta": None,
                    "points": [],
                    "preparation_id": str(uuid4()),
                },
                "evidence_refs": {},
            }
        )


@pytest.mark.parametrize("same_round", [False, True])
def test_delayed_different_prepare_cannot_replace_current_confirmation(
    database, same_round
):
    engine, info = database
    with Session(engine) as session:
        service = ExamScoringService(session)
        first = prepare(service, info, prepare_payload(view(service, info)))
        delayed = prepare_payload(
            first,
            points=[{"key": "whole", "label": "Whole standard", "base_points": "3.00"}],
        )
        if same_round:
            current = confirm(service, info, confirm_payload(first, ["1.00"] * 3))
        else:
            newer = prepare(
                service,
                info,
                prepare_payload(
                    first,
                    points=[
                        {"key": "a", "label": "A", "base_points": "2.00"},
                        {"key": "b", "label": "B", "base_points": "1.00"},
                    ],
                ),
            )
            current = confirm(service, info, confirm_payload(newer, ["2.00", "1.00"]))
        with pytest.raises(AssemblyError) as error:
            prepare(service, info, delayed)
        assert error.value.code == "EXAM_SCORING_CONTEXT_CHANGED"
        assert view(service, info).basis == current.basis


def test_actual_asset_upload_invalidates_prepared_draft(database, tmp_path):
    from io import BytesIO

    from PIL import Image

    from backend.app.services.question_asset_service import QuestionAssetService

    engine, info = database
    with Session(engine) as session:
        service = ExamScoringService(session, root=tmp_path)
        original = prepare(service, info, prepare_payload(view(service, info)))
        QuestionService(session).update_question_status(
            info["questions"][1],
            QuestionStatus.NEEDS_REVISION,
            teacher_id=info["actor"],
            revision_comment="Review original image content",
        )
        content = BytesIO()
        Image.new("RGB", (12, 12), "white").save(content, format="PNG")
        QuestionAssetService(session, root=tmp_path).upload_question(
            info["questions"][1],
            content=content.getvalue(),
            asset_type="figure",
            caption="Controlled figure",
            actor_id=info["actor"],
        )
        current = view(service, info)
        assert current.basis is None and current.base_score is None
        assert (
            current.question_validation_revision
            == original.question_validation_revision + 1
        )


def test_invalidation_keeps_published_and_submission_protected_facts(database):
    from backend.app.domain.enums import UserRole
    from backend.app.models import ExamQuestion, Submission
    from backend.app.services.question_scoring_invalidation import (
        bump_question_validation,
    )
    from tests.unit.services.test_submission_service import add_user

    engine, info = database
    with Session(engine) as session:
        service = ExamScoringService(session)
        original = prepare(service, info, prepare_payload(view(service, info)))
        student = add_user(
            session,
            UserRole.STUDENT,
            username="scoring-student",
            email="scoring-student@example.com",
        )
        question = session.get(Question, info["questions"][1])
        protected = []
        for status in [
            ExamStatus.PUBLISHED,
            ExamStatus.CLOSED,
            ExamStatus.ARCHIVED,
            ExamStatus.DRAFT,
        ]:
            exam = Exam(
                course_id=info["course"],
                created_by=info["actor"],
                title=f"Retained {status}",
                status=status,
            )
            exam.exam_question_links.append(
                ExamQuestion(
                    question=question,
                    order_index=1,
                    score=Decimal("3.00"),
                    base_score=Decimal("3.00"),
                    published_knowledge_points=["Historical label"],
                    scoring_basis=original.basis.model_dump(mode="json"),
                )
            )
            session.add(exam)
            session.flush()
            if status == ExamStatus.DRAFT:
                session.add(Submission(exam_id=exam.id, student_id=student.id))
            protected.append(exam.id)
        session.commit()
        question = QuestionService(session)._load_question(question.id)
        bump_question_validation(session, question)
        session.commit()
        assert (
            session.get(Exam, info["exam"]).exam_question_links[0].scoring_basis is None
        )
        for identity in protected:
            row = session.get(Exam, identity).exam_question_links[0]
            assert (
                row.scoring_basis == original.basis.model_dump(mode="json")
                and row.base_score == Decimal("3.00")
                and row.published_knowledge_points == ["Historical label"]
            )


def test_real_parent_attachment_advances_revision_without_images(database):
    from backend.app.services.question_adaptation_service import (
        QuestionAdaptationService,
    )

    engine, info = database
    with Session(engine) as session:
        service = ExamScoringService(session)
        original = prepare(service, info, prepare_payload(view(service, info)))
        QuestionService(session).update_question_status(
            info["questions"][1],
            QuestionStatus.NEEDS_REVISION,
            teacher_id=info["actor"],
            revision_comment="Review additional authentic parent source",
        )
        adaptation = QuestionAdaptationService(session)
        snapshot = adaptation.prepare(
            course_id=info["course"],
            actor_id=info["actor"],
            source_question_id=info["questions"][0],
            adaptation_type="extend",
        )
        adaptation.lock_and_validate(snapshot, actor_id=info["actor"])
        question = QuestionService(session)._load_question(info["questions"][1])
        adaptation.attach(snapshot, question, actor_id=info["actor"], stored_files=[])
        session.commit()
        current = view(service, info)
        assert current.basis is None and current.base_score is None
        assert (
            current.question_validation_revision
            == original.question_validation_revision + 1
        )
