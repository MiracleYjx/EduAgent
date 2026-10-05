"""T179 real PostgreSQL blocking proof for publication/revision/assets (TCR 34)."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from queue import Queue
from time import monotonic, sleep
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from backend.app.domain.enums import ExamStatus, QuestionStatus
from backend.app.models import Course, Exam, Question, QuestionAsset
from backend.app.services.exam_service import ExamService, ExamValidationError
from backend.app.services.file_storage_service import FileStorageError
from backend.app.services.question_asset_service import QuestionAssetService
from backend.app.services.question_service import (
    QuestionPublishedImmutableError,
    QuestionService,
)
from tests.integration.test_exam_assembly import database as _assembly_database
from tests.integration.test_reference_lifecycle import png

database = _assembly_database


def assert_blocked_by(engine, waiter, holder, future):
    deadline = monotonic() + 5
    while monotonic() < deadline and not future.done():
        with engine.connect() as observer:
            if observer.scalar(
                text("SELECT :holder = ANY(pg_blocking_pids(:waiter))"),
                {"holder": holder, "waiter": waiter},
            ):
                return
        sleep(0.02)
    raise AssertionError("Actual competing PostgreSQL session never blocked on holder")


@pytest.mark.parametrize("action", ["revision", "asset"])
def test_publication_first_rejects_inflight_revision_and_asset_write(
    database, tmp_path, action
):
    engine, info = database
    qid = info["questions"][1]
    pid_queue = Queue()
    with Session(engine) as original:
        question = original.get(Question, qid)
        before = (question.content, question.status, question.validation_revision)
        basis = deepcopy(
            original.get(Exam, info["exam"]).exam_question_links[0].scoring_basis
        )

    def writer():
        with Session(engine) as session:
            pid_queue.put(session.scalar(text("SELECT pg_backend_pid()")))
            try:
                if action == "revision":
                    QuestionService(session).update_question_status(
                        qid,
                        QuestionStatus.NEEDS_REVISION,
                        teacher_id=info["actor"],
                        revision_comment="Explicit synthetic concurrent review request",
                    )
                else:
                    QuestionAssetService(session, root=tmp_path).upload_question(
                        qid,
                        content=png(),
                        asset_type="figure",
                        caption=None,
                        actor_id=info["actor"],
                    )
                raise AssertionError(
                    "Concurrent protected writer unexpectedly succeeded"
                )
            except (QuestionPublishedImmutableError, FileStorageError) as error:
                session.rollback()
                return error.code

    with Session(engine) as publishing, ThreadPoolExecutor(max_workers=1) as pool:
        publishing.scalar(
            select(Course).where(Course.id == info["course"]).with_for_update()
        )
        holder = publishing.scalar(text("SELECT pg_backend_pid()"))
        future = pool.submit(writer)
        waiter = pid_queue.get(timeout=5)
        try:
            assert_blocked_by(engine, waiter, holder, future)
            ExamService(publishing).publish_exam(info["exam"], teacher_id=info["actor"])
        finally:
            publishing.rollback()
        assert future.result(timeout=10) == "QUESTION_REFERENCED_IMMUTABLE"
    with Session(engine) as fresh:
        question = fresh.get(Question, qid)
        assert (
            question.content,
            question.status,
            question.validation_revision,
        ) == before
        exam = fresh.get(Exam, info["exam"])
        assert exam.status == ExamStatus.PUBLISHED
        assert exam.exam_question_links[0].scoring_basis == basis
        assert (
            list(
                fresh.scalars(
                    select(QuestionAsset).where(QuestionAsset.question_id == qid)
                )
            )
            == []
        )
    assert list(tmp_path.rglob("*")) == []


def test_revision_first_invalidates_inflight_publication(database):
    engine, info = database
    qid = info["questions"][1]
    pid_queue = Queue()
    with Session(engine) as original:
        before_basis = deepcopy(
            original.get(Exam, info["exam"]).exam_question_links[0].scoring_basis
        )

    def publisher():
        with Session(engine) as session:
            pid_queue.put(session.scalar(text("SELECT pg_backend_pid()")))
            try:
                ExamService(session).publish_exam(
                    info["exam"], teacher_id=info["actor"]
                )
                raise AssertionError("Publication used stale approved/scoring facts")
            except ExamValidationError as error:
                session.rollback()
                return str(error)

    with Session(engine) as revising, ThreadPoolExecutor(max_workers=1) as pool:
        revising.scalar(
            select(Course).where(Course.id == info["course"]).with_for_update()
        )
        holder = revising.scalar(text("SELECT pg_backend_pid()"))
        future = pool.submit(publisher)
        waiter = pid_queue.get(timeout=5)
        try:
            assert_blocked_by(engine, waiter, holder, future)
            QuestionService(revising).update_question_status(
                qid,
                QuestionStatus.NEEDS_REVISION,
                teacher_id=info["actor"],
                revision_comment="Explicit synthetic revision before publication commits",
            )
        finally:
            revising.rollback()
        assert "Needs Revision" in future.result(timeout=10)
    with Session(engine) as fresh:
        assert fresh.get(Question, qid).status == QuestionStatus.NEEDS_REVISION
        exam = fresh.get(Exam, info["exam"])
        assert exam.status == ExamStatus.DRAFT
        assert exam.exam_question_links[0].question_id == qid
        # Status-only revision does not rewrite unchanged scoring input facts.
        assert exam.exam_question_links[0].scoring_basis == before_basis


def test_selected_question_read_preserves_filter_and_authorization(database):
    """The UI bulk read may select metadata, never broaden course ownership."""
    from backend.app.services.question_service import QuestionPermissionError

    engine, info = database
    with Session(engine) as session:
        service = QuestionService(session)
        ids = [info["questions"][2], info["questions"][0]]
        selected = service.list_questions(
            course_id=info["course"], teacher_id=info["actor"], question_ids=ids
        )
        assert {UUID(question.id) for question in selected} == set(ids)
        assert all(question.status == QuestionStatus.APPROVED for question in selected)
        assert (
            service.list_questions(
                course_id=info["course"], teacher_id=info["actor"], question_ids=[]
            )
            == []
        )
        assert (
            service.list_questions(
                course_id=info["course"],
                teacher_id=info["actor"],
                question_ids=[UUID(int=0)],
            )
            == []
        )
        with pytest.raises(QuestionPermissionError):
            service.list_questions(
                course_id=info["course"], teacher_id=UUID(int=1), question_ids=ids
            )


def test_foreign_historical_association_retains_explicit_conflict(database):
    """Corrupt historical linkage keeps its explicit per-question diagnostic."""
    from backend.app.domain.enums import UserRole
    from backend.app.models import ExamQuestion
    from tests.unit.services.test_submission_service import add_user

    engine, info = database
    with Session(engine) as session:
        other = add_user(
            session,
            UserRole.TEACHER,
            username="foreign-reference-owner",
            email="foreign-reference-owner@example.com",
        )
        course = Course(name="foreign historical reference", creator=other)
        session.add(course)
        session.flush()
        # Explicit historical corruption, not a successful public association command.
        question = session.get(Question, info["questions"][1])
        question.course_id = course.id
        session.commit()
        link = session.scalars(
            select(ExamQuestion).where(ExamQuestion.exam_id == info["exam"])
        ).one()
        assert link.question_id == question.id
        preview = ExamService(session).preview_assembly(
            info["exam"], teacher_id=info["actor"]
        )
        assert any(
            check.code == "EXAM_QUESTION_COURSE_CONFLICT"
            and check.question_id == question.id
            for check in preview.publication_checks
        )
        assert preview.exam_questions == []
