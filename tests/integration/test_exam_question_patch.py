"""T173 real PG association patch and teacher projection (TCR §28)."""

from copy import deepcopy
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from backend.app.models import Exam, Question
from backend.app.schemas.exam_assembly import ExamQuestionPatchRequest
from backend.app.services.exam_assembly_service import (
    AssemblyError,
    ExamAssemblyService,
)
from backend.app.services.exam_service import ExamService
from tests.integration.test_exam_assembly import database as _assembly_database
from tests.integration.test_exam_assembly import facts, request
from tests.support.exam_scoring_fixtures import confirm_synthetic_exam_basis

database = _assembly_database


def patch(session, info, qid, **values):
    return ExamService(session).patch_exam_question(
        info["exam"],
        qid,
        ExamQuestionPatchRequest.model_validate(values),
        teacher_id=info["actor"],
    )


def test_move_both_directions_preserves_identity_and_basis(database):
    engine, info = database
    with Session(engine) as session:
        ExamService(session).add_questions(
            info["exam"],
            [info["questions"][0], info["questions"][2]],
            teacher_id=info["actor"],
        )
        confirm_synthetic_exam_basis(session, info["exam"])
        session.commit()
        before = facts(session, info["exam"])
        last = before[-1][1]
        result = patch(session, info, last, order_index=1)
        assert [q.question_id for q in result.exam_questions] == [
            last,
            *[row[1] for row in before[:-1]],
        ]
        result = patch(session, info, last, order_index=3)
        assert facts(session, info["exam"]) == before
        assert [q.order_index for q in result.exam_questions] == [1, 2, 3]


def test_replacement_new_identity_keeps_position_and_explicit_score(database):
    engine, info = database
    with Session(engine) as session:
        before = facts(session, info["exam"])[0]
        result = patch(
            session,
            info,
            info["questions"][1],
            replacement_question_id=info["questions"][3],
        )
        row = result.exam_questions[0]
        assert row.id != before[0]
        assert row.question_id == info["questions"][3] and row.order_index == 1
        assert row.score == Decimal("3.00") and row.effective_score == Decimal("3.00")
        assert row.base_score is None and row.scoring_basis is None
        assert session.get(Question, info["questions"][3]).score == Decimal("2.00")


def test_same_score_keeps_confirmation_but_actual_change_or_null_clears(database):
    engine, info = database
    with Session(engine) as session:
        before = facts(session, info["exam"])[0]
        same = patch(session, info, info["questions"][1], score="3.00")
        assert same.exam_questions[0].scoring_basis.model_dump(mode="json") == before[6]
        changed = patch(session, info, info["questions"][1], score="7.25")
        assert changed.exam_questions[0].scoring_basis is None
        cleared = patch(session, info, info["questions"][1], score=None)
        assert cleared.exam_questions[0].score is None
        assert cleared.exam_questions[0].effective_score == Decimal("3.00")


def test_patch_preserves_latest_intent_and_recomputes_actual_facts(database):
    engine, info = database
    with Session(engine) as session:
        initial = ExamAssemblyService(session).assemble(
            info["exam"], request(info), actor_id=info["actor"]
        )
        intent = initial.assembly_constraints.model_dump(mode="json")
        result = patch(session, info, info["questions"][0], score="4.00")
        assert result.assembly_constraints.model_dump(mode="json") == intent
        assert result.total_score == Decimal("7.00")
        assert any(
            item.kind == "total_score" and not item.satisfied
            for item in result.conditions
        )
        loaded = ExamService(session).preview_assembly(
            info["exam"], teacher_id=info["actor"]
        )
        assert loaded.model_dump(mode="json") == result.model_dump(mode="json")


def test_full_teacher_preview_has_current_order_content_and_real_defaults(database):
    engine, info = database
    with Session(engine) as session:
        result = ExamService(session).preview_assembly(
            info["exam"], teacher_id=info["actor"]
        )
        row = result.exam_questions[0]
        question = session.get(Question, row.question_id)
        assert row.content == question.content
        assert row.reference_answer == question.reference_answer
        assert row.scoring_rubric == question.scoring_rubric
        assert row.analysis == question.analysis
        assert row.options == question.options


@pytest.mark.parametrize("case", ["row_id", "outside_order", "duplicate"])
def test_invalid_patch_changes_no_facts_or_intent(database, case):
    engine, info = database
    with Session(engine) as session:
        ExamService(session).add_questions(
            info["exam"], [info["questions"][0]], teacher_id=info["actor"]
        )
        before = facts(session, info["exam"])
        values = {"order_index": 3} if case == "outside_order" else {"order_index": 1}
        qid = before[0][0] if case == "row_id" else before[0][1]
        if case == "duplicate":
            values = {"replacement_question_id": before[1][1]}
        with pytest.raises(AssemblyError):
            patch(session, info, qid, **values)
        assert facts(session, info["exam"]) == before
        assert session.get(Exam, info["exam"]).assembly_constraints is None


def test_replacement_and_explicit_null_use_replacement_default(database):
    engine, info = database
    with Session(engine) as session:
        old = facts(session, info["exam"])[0]
        response = patch(
            session,
            info,
            info["questions"][1],
            replacement_question_id=info["questions"][3],
            score=None,
        )
        row = response.exam_questions[0]
        assert row.id != old[0] and row.score is None
        assert row.effective_score == Decimal("2.00")
        assert (
            row.base_score is None
            and row.published_knowledge_points is None
            and row.scoring_basis is None
        )
    with Session(engine) as session:
        loaded = ExamService(session).preview_assembly(
            info["exam"], teacher_id=info["actor"]
        )
        assert loaded.model_dump(mode="json") == response.model_dump(mode="json")


def test_old_question_identity_rejected_after_replacement(database):
    engine, info = database
    with Session(engine) as session:
        patch(
            session,
            info,
            info["questions"][1],
            replacement_question_id=info["questions"][3],
        )
        before = facts(session, info["exam"])
        with pytest.raises(AssemblyError) as error:
            patch(session, info, info["questions"][1], score="9.00")
        assert error.value.http_status == 404
        assert facts(session, info["exam"]) == before


@pytest.mark.parametrize("case", ["published", "closed", "archived", "history"])
def test_patch_rejects_frozen_status_and_submission_history(database, case):
    from backend.app.domain.enums import ExamStatus, UserRole
    from backend.app.models import Submission
    from tests.unit.services.test_submission_service import add_user

    engine, info = database
    with Session(engine) as session:
        exam = session.get(Exam, info["exam"])
        if case == "history":
            student = add_user(
                session,
                UserRole.STUDENT,
                username="patch-student",
                email="patch-student@example.com",
            )
            session.flush()
            session.add(Submission(exam_id=exam.id, student_id=student.id))
        else:
            exam.status = {
                "published": ExamStatus.PUBLISHED,
                "closed": ExamStatus.CLOSED,
                "archived": ExamStatus.ARCHIVED,
            }[case]
        session.commit()
        before = facts(session, info["exam"])
        with pytest.raises(AssemblyError) as error:
            patch(session, info, info["questions"][1], score="9.00")
        assert error.value.code == "EXAM_PUBLISHED_IMMUTABLE"
        assert facts(session, info["exam"]) == before
        assert session.get(Exam, info["exam"]).assembly_constraints is None


def prepare_three_questions(session, info):
    payload = request(
        info,
        question_count=3,
        type_distribution=[
            {"question_type": "SHORT_ANSWER", "count": 2},
            {"question_type": "SINGLE_CHOICE", "count": 1},
        ],
        total_score="8.00",
    )
    ExamAssemblyService(session).assemble(info["exam"], payload, actor_id=info["actor"])
    confirm_synthetic_exam_basis(session, info["exam"])
    session.commit()


@pytest.mark.parametrize("operation", ["move", "replace", "score"])
def test_flush_failure_restores_all_associations_and_latest_intent(database, operation):
    from sqlalchemy import event
    from sqlalchemy.exc import SQLAlchemyError

    from backend.app.models import ExamQuestion

    engine, info = database
    with Session(engine) as session:
        prepare_three_questions(session, info)
        before = facts(session, info["exam"])
        intent = deepcopy(session.get(Exam, info["exam"]).assembly_constraints)

        def fail_after_fact_flush(current, _context):
            if any(
                isinstance(item, ExamQuestion)
                for item in [*current.new, *current.dirty, *current.deleted]
            ):
                raise SQLAlchemyError("controlled patch fact flush failure")

        event.listen(session, "after_flush", fail_after_fact_flush)
        values = (
            {"order_index": 1}
            if operation == "move"
            else (
                {"replacement_question_id": info["questions"][3]}
                if operation == "replace"
                else {"score": "9.00"}
            )
        )
        try:
            with pytest.raises(AssemblyError) as error:
                patch(session, info, before[-1][1], **values)
            assert error.value.http_status == 503
        finally:
            event.remove(session, "after_flush", fail_after_fact_flush)
        assert facts(session, info["exam"]) == before
        assert session.get(Exam, info["exam"]).assembly_constraints == intent
    with Session(engine) as session:
        assert facts(session, info["exam"]) == before
        assert session.get(Exam, info["exam"]).assembly_constraints == intent


def test_publication_rejects_saved_intent_gap_but_draft_can_be_corrected(database):
    from backend.app.domain.enums import ExamStatus

    engine, info = database
    with Session(engine) as session:
        initial = ExamAssemblyService(session).assemble(
            info["exam"], request(info), actor_id=info["actor"]
        )
        result = patch(session, info, info["questions"][0], score="4.00")
        before = facts(session, info["exam"])
        with pytest.raises(AssemblyError) as error:
            ExamService(session).publish_exam(info["exam"], teacher_id=info["actor"])
        assert error.value.code == "EXAM_ASSEMBLY_UNSATISFIED"
        assert session.get(Exam, info["exam"]).status == ExamStatus.DRAFT
        assert facts(session, info["exam"]) == before
        assert result.assembly_constraints == initial.assembly_constraints
        corrected = patch(session, info, info["questions"][0], score="2.00")
        assert all(item.satisfied for item in corrected.conditions)
        assert corrected.assembly_constraints == initial.assembly_constraints


@pytest.mark.parametrize("case", ["not_approved", "stale_report", "unknown_id"])
def test_ineligible_replacement_preserves_original_facts(database, case):
    from uuid import uuid4

    from backend.app.domain.enums import QuestionStatus
    from backend.app.services.question_service import QuestionService

    engine, info = database
    with Session(engine) as session:
        candidate_id = info["questions"][0]
        if case == "not_approved":
            QuestionService(session).update_question_status(
                candidate_id,
                QuestionStatus.NEEDS_REVISION,
                teacher_id=info["actor"],
                revision_comment="Synthetic fixture requests another review.",
            )
        elif case == "stale_report":
            session.get(Question, candidate_id).validation_revision += 1
            session.commit()
        else:
            candidate_id = uuid4()
        before = facts(session, info["exam"])
        with pytest.raises(AssemblyError):
            patch(
                session,
                info,
                info["questions"][1],
                replacement_question_id=candidate_id,
            )
        assert facts(session, info["exam"]) == before


def test_moved_question_order_flows_to_reader_and_context(database):
    from backend.app.domain.enums import AnswerStatus, SubmissionStatus, UserRole
    from backend.app.models import Answer, Submission
    from backend.app.services.grading.grading_task_service import (
        DatabaseGradingSubmissionReader,
    )
    from tests.unit.services.test_submission_service import add_user

    engine, info = database
    with Session(engine) as session:
        prepare_three_questions(session, info)
        before = facts(session, info["exam"])
        moved = patch(session, info, before[-1][1], order_index=1)
        expected = [item.question_id for item in moved.exam_questions]
        student = add_user(
            session,
            UserRole.STUDENT,
            username="reader-patch",
            email="reader-patch@example.com",
        )
        session.flush()
        submission = Submission(
            exam_id=info["exam"],
            student_id=student.id,
            status=SubmissionStatus.SUBMITTED,
        )
        session.add(submission)
        session.flush()
        session.add_all(
            Answer(
                submission_id=submission.id,
                question_id=identity,
                content="A synthetic answer",
                status=AnswerStatus.SUBMITTED,
            )
            for identity in reversed(expected)
        )
        session.commit()
        submission_id = submission.id
    with Session(engine) as session:
        snapshot = DatabaseGradingSubmissionReader(session=session).load(
            str(submission_id)
        )
        assert [item.question_id for item in snapshot.answers] == [
            str(identity) for identity in expected
        ]
        assert [item.order for item in snapshot.answers] == [1, 2, 3]
        assert [
            item.question_id for item in snapshot.to_context().expected_answers
        ] == [str(identity) for identity in expected]
        snapshot.require_scoring_ready()


def test_cross_course_replacement_rejected_with_current_review(database):
    from backend.app.domain.enums import QuestionStatus, QuestionType
    from backend.app.models import Course
    from backend.app.services.question_service import QuestionService
    from tests.support.question_validation_fixtures import persist_current_semantic_pass

    engine, info = database
    with Session(engine) as session:
        course = Course(name="Another authorized course", created_by=info["actor"])
        session.add(course)
        session.flush()
        question = Question(
            course_id=course.id,
            created_by=info["actor"],
            type=QuestionType.SHORT_ANSWER,
            content="Actual other-course question",
            reference_answer="Other-course answer",
            scoring_rubric="Answer earns 2 points.",
            score=Decimal("2.00"),
            status=QuestionStatus.PENDING_REVIEW,
        )
        session.add(question)
        session.commit()
        persist_current_semantic_pass(session, question.id, info["actor"])
        QuestionService(session).update_question_status(
            question.id, "Approved", teacher_id=info["actor"]
        )
        before = facts(session, info["exam"])
        with pytest.raises(AssemblyError):
            patch(
                session, info, info["questions"][1], replacement_question_id=question.id
            )
        assert facts(session, info["exam"]) == before


def test_original_image_survives_replacement_preview_and_missing_file_rejects(
    database, tmp_path, monkeypatch
):
    from backend.app.services.content_validation_service import ContentValidationService
    from backend.app.services.file_storage_service import FileStorageService
    from backend.app.services.question_asset_service import QuestionAssetService
    from backend.app.services.question_service import QuestionService
    from tests.support.question_validation_fixtures import persist_current_semantic_pass
    from tests.unit.services.test_content_validation_service import manual
    from tests.unit.services.test_question_asset_service import png
    from tests.unit.settings_helpers import build_test_settings

    engine, info = database
    settings = build_test_settings(storage_root=tmp_path)
    monkeypatch.setattr(
        "backend.app.services.file_storage_service.get_settings", lambda: settings
    )
    with Session(engine) as session:
        identity = info["questions"][0]
        questions = QuestionService(session)
        questions.update_question_status(
            identity,
            "Needs Revision",
            teacher_id=info["actor"],
            revision_comment="Synthetic source-image replacement scenario.",
        )
        original = png()
        asset = QuestionAssetService(session, root=tmp_path).upload_question(
            identity,
            content=original,
            asset_type="figure",
            caption="Original synthetic image",
            actor_id=info["actor"],
        )
        questions.update_question_status(
            identity, "Pending Review", teacher_id=info["actor"]
        )
        validation = ContentValidationService(session, root=tmp_path)
        view = validation.get_image_assessment(
            "question", identity, actor_id=info["actor"]
        )
        validation.manual_image_check(
            "question",
            identity,
            manual(
                view,
                asset,
                explanation="Synthetic verification of actual original bytes; no independent teacher claim.",
            ),
            actor_id=info["actor"],
        )
        persist_current_semantic_pass(session, identity, info["actor"], root=tmp_path)
        questions.update_question_status(identity, "Approved", teacher_id=info["actor"])
        response = patch(
            session, info, info["questions"][1], replacement_question_id=identity
        )
        assert response.exam_questions[0].assets[0].id == asset.id
        assert response.exam_questions[0].assets[0].file_id == asset.file_id
        preview = ExamService(session).preview_assembly(
            info["exam"], teacher_id=info["actor"]
        )
        assert preview.model_dump(mode="json") == response.model_dump(mode="json")
        path, _ = FileStorageService(session, root=tmp_path).download(
            asset.file_id, actor_id=info["actor"]
        )
        assert path.read_bytes() == original
        patch(session, info, identity, replacement_question_id=info["questions"][1])
        assert path.is_relative_to(tmp_path.resolve())
        path.unlink()
        before = facts(session, info["exam"])
        with pytest.raises(AssemblyError):
            patch(session, info, info["questions"][1], replacement_question_id=identity)
        assert facts(session, info["exam"]) == before
