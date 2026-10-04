"""T175 actual reference protection and cascade boundaries (TCR §30)."""

from copy import deepcopy
from io import BytesIO

import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.domain.enums import (
    ExamStatus,
    QuestionStatus,
    SubmissionStatus,
    UserRole,
)
from backend.app.models import Answer, Course, Exam, Question, Submission
from backend.app.services.course_service import CourseConflictError, CourseService
from backend.app.services.file_storage_service import FileStorageError
from backend.app.services.question_adaptation_service import (
    QuestionAdaptationError,
    QuestionAdaptationService,
)
from backend.app.services.question_asset_service import QuestionAssetService
from backend.app.services.question_service import (
    QuestionApprovedImmutableError,
    QuestionPublishedImmutableError,
    QuestionService,
)
from tests.integration.test_exam_assembly import database as _assembly_database
from tests.unit.services.test_submission_service import add_user

database = _assembly_database


def freeze(session, info, status=ExamStatus.PUBLISHED):
    session.get(Exam, info["exam"]).status = status
    session.commit()


def png():
    stream = BytesIO()
    Image.new("RGB", (8, 8), "white").save(stream, format="PNG")
    return stream.getvalue()


@pytest.mark.parametrize(
    "status", [ExamStatus.PUBLISHED, ExamStatus.CLOSED, ExamStatus.ARCHIVED]
)
@pytest.mark.parametrize(
    "action", ["content", "analysis", "revision", "delete", "asset"]
)
def test_all_published_lifecycle_references_block_in_place_changes(
    database, tmp_path, status, action
):
    engine, info = database
    qid = info["questions"][1]
    with Session(engine) as session:
        freeze(session, info, status)
        question = session.get(Question, qid)
        before = (
            question.content,
            question.analysis,
            question.status,
            deepcopy(question.image_assessment),
        )
        error_type = (
            FileStorageError
            if action == "asset"
            else (
                QuestionApprovedImmutableError
                if action in {"content", "analysis"}
                else QuestionPublishedImmutableError
            )
        )
        with pytest.raises(error_type) as caught:
            if action == "asset":
                QuestionAssetService(session, root=tmp_path).upload_question(
                    qid,
                    content=png(),
                    asset_type="figure",
                    caption=None,
                    actor_id=info["actor"],
                )
            elif action == "revision":
                QuestionService(session).update_question_status(
                    qid,
                    QuestionStatus.NEEDS_REVISION,
                    teacher_id=info["actor"],
                    revision_comment="Actual synthetic reviewer request.",
                )
            elif action == "delete":
                QuestionService(session).delete_question(qid, teacher_id=info["actor"])
            else:
                QuestionService(session).update_question(
                    qid,
                    teacher_id=info["actor"],
                    **{action: "Changed after publication"},
                )
        assert caught.value.code == (
            "QUESTION_APPROVED_IMMUTABLE"
            if action in {"content", "analysis"}
            else "QUESTION_REFERENCED_IMMUTABLE"
        )
        session.rollback()
        question = session.get(Question, qid)
        assert (
            question.content,
            question.analysis,
            question.status,
            question.image_assessment,
        ) == before
        assert session.get(Exam, info["exam"]).status == status


@pytest.mark.parametrize(
    "status", [ExamStatus.PUBLISHED, ExamStatus.CLOSED, ExamStatus.ARCHIVED]
)
def test_course_cascade_cannot_erase_published_references(database, status):
    engine, info = database
    with Session(engine) as session:
        freeze(session, info, status)
        with pytest.raises(CourseConflictError) as caught:
            CourseService(session).delete_course(
                info["course"], teacher_id=info["actor"]
            )
        assert caught.value.code == "COURSE_REFERENCED_IMMUTABLE"
        session.rollback()
        assert session.get(Course, info["course"]) is not None
        assert session.get(Exam, info["exam"]).status == status
        assert session.get(Question, info["questions"][1]) is not None


def test_cached_draft_exam_does_not_allow_revision_after_other_session_publication(
    database,
):
    engine, info = database
    with Session(engine) as cached:
        question = cached.get(Question, info["questions"][1])
        assert question.exams[0].status == ExamStatus.DRAFT
        cached.commit()
        # Preserve a cached relationship so a fresh SQL protection query is necessary.
        cached.expire_on_commit = False
        assert question.exams[0].status == ExamStatus.DRAFT
        with Session(engine) as other:
            freeze(other, info)
        with pytest.raises(QuestionPublishedImmutableError) as caught:
            QuestionService(cached).update_question_status(
                question.id,
                QuestionStatus.NEEDS_REVISION,
                teacher_id=info["actor"],
                revision_comment="Cached pre-publication request.",
            )
        assert caught.value.code == "QUESTION_REFERENCED_IMMUTABLE"


def test_direct_answer_history_protects_question_even_without_current_exam_link(
    database,
):
    engine, info = database
    qid = info["questions"][0]
    with Session(engine) as session:
        student = add_user(
            session,
            UserRole.STUDENT,
            username="historical-student",
            email="historical-student@example.com",
        )
        submission = Submission(
            exam_id=info["exam"], student_id=student.id, status=SubmissionStatus.DRAFT
        )
        session.add(submission)
        session.flush()
        session.add(
            Answer(
                submission_id=submission.id,
                question_id=qid,
                content="Retained old answer",
            )
        )
        session.commit()
        with pytest.raises(QuestionPublishedImmutableError) as caught:
            QuestionService(session).update_question_status(
                qid,
                QuestionStatus.NEEDS_REVISION,
                teacher_id=info["actor"],
                revision_comment="Retained answer must prevent revision.",
            )
        assert caught.value.code == "QUESTION_REFERENCED_IMMUTABLE"


def test_existing_protected_derived_question_cannot_receive_new_parent_edge(database):
    engine, info = database
    with Session(engine) as session:
        freeze(session, info)
        service = QuestionAdaptationService(session)
        snapshot = service.prepare(
            course_id=info["course"],
            actor_id=info["actor"],
            source_question_id=info["questions"][0],
            adaptation_type="rewrite",
        )
        service.lock_and_validate(snapshot, actor_id=info["actor"])
        with pytest.raises(QuestionAdaptationError) as caught:
            service.attach(
                snapshot,
                session.get(Question, info["questions"][1]),
                actor_id=info["actor"],
                stored_files=[],
            )
        assert caught.value.error_code == "QUESTION_REFERENCED_IMMUTABLE"


def test_metadata_exception_and_approved_guard_remain_distinct(database):
    engine, info = database
    with Session(engine) as session:
        freeze(session, info)
        exam = session.get(Exam, info["exam"])
        before = deepcopy(exam.exam_question_links[0].published_knowledge_points)
        changed = QuestionService(session).update_question(
            info["questions"][1],
            knowledge_points=["Bank maintenance"],
            difficulty="medium",
            teacher_id=info["actor"],
        )
        assert changed.knowledge_points == ["Bank maintenance"]
        assert (
            session.get(Exam, info["exam"])
            .exam_question_links[0]
            .published_knowledge_points
            == before
        )
        with pytest.raises(QuestionApprovedImmutableError) as caught:
            QuestionService(session).update_question(
                info["questions"][0],
                content="Unprotected approved edit",
                teacher_id=info["actor"],
            )
        assert caught.value.code == "QUESTION_APPROVED_IMMUTABLE"


def test_history_references_still_protect_anomalous_nonapproved_question(database):
    engine, info = database
    with Session(engine) as session:
        freeze(session, info)
        question = session.get(Question, info["questions"][1])
        question.status = QuestionStatus.NEEDS_REVISION
        session.commit()
        with pytest.raises(QuestionPublishedImmutableError) as caught:
            QuestionService(session).update_question(
                question.id,
                analysis="Would destroy historical explanation",
                teacher_id=info["actor"],
            )
        assert caught.value.code == "QUESTION_REFERENCED_IMMUTABLE"


def test_new_derived_candidate_may_reference_protected_parent_without_rewriting_it(
    database,
):
    from backend.app.models import QuestionSourcePaper

    engine, info = database
    with Session(engine) as session:
        freeze(session, info)
        parent = session.get(Question, info["questions"][1])
        before = (parent.content, parent.status, parent.validation_revision)
        service = QuestionAdaptationService(session)
        snapshot = service.prepare(
            course_id=info["course"],
            actor_id=info["actor"],
            source_question_id=parent.id,
            adaptation_type="rewrite",
        )
        service.lock_and_validate(snapshot, actor_id=info["actor"])
        candidate = Question(
            course_id=parent.course_id,
            type=parent.type,
            content="New candidate text",
            reference_answer=parent.reference_answer,
            scoring_rubric=parent.scoring_rubric,
            score=parent.score,
            created_by=info["actor"],
            status=QuestionStatus.DRAFT,
        )
        session.add(candidate)
        session.flush()
        service.attach(snapshot, candidate, actor_id=info["actor"], stored_files=[])
        session.commit()
        assert (
            session.scalar(
                select(QuestionSourcePaper.source_question_id).where(
                    QuestionSourcePaper.derived_question_id == candidate.id
                )
            )
            == parent.id
        )
        assert (parent.content, parent.status, parent.validation_revision) == before
        assert candidate.status == QuestionStatus.DRAFT


@pytest.mark.parametrize(
    "current,next_status",
    [
        (QuestionStatus.DRAFT, QuestionStatus.PENDING_REVIEW),
        (QuestionStatus.PENDING_REVIEW, QuestionStatus.APPROVED),
        (QuestionStatus.NEEDS_REVISION, QuestionStatus.PENDING_REVIEW),
    ],
)
def test_retained_reference_blocks_every_actual_review_transition(
    database, current, next_status
):
    engine, info = database
    qid = info["questions"][1]
    with Session(engine) as session:
        freeze(session, info)
        question = session.get(Question, qid)
        question.status = current  # Explicitly reproduce anomalous historical state.
        session.commit()
        before = (question.status, question.frozen_at, question.validation_revision)
        with pytest.raises(QuestionPublishedImmutableError) as caught:
            QuestionService(session).update_question_status(
                qid, next_status, teacher_id=info["actor"]
            )
        assert caught.value.code == "QUESTION_REFERENCED_IMMUTABLE"
        session.rollback()
        question = session.get(Question, qid)
        assert (
            question.status,
            question.frozen_at,
            question.validation_revision,
        ) == before
        same = QuestionService(session).update_question_status(
            qid, current, teacher_id=info["actor"]
        )
        assert (same.status, same.frozen_at) == before[:2]
        assert question.validation_revision == before[2]
