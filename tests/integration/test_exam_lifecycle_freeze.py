"""T175 real PostgreSQL stale commands and publication lifecycle (TCR §30)."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from queue import Queue
from time import monotonic, sleep

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from backend.app.domain.enums import ExamStatus, UserRole
from backend.app.models import Course, Exam, ExamParticipant, Submission
from backend.app.services.exam_service import ExamService, ExamValidationError
from backend.app.services.submission_service import (
    SubmissionNotAvailableError,
    SubmissionService,
)
from tests.integration.test_exam_assembly import database as _database
from tests.integration.test_exam_assembly import facts
from tests.unit.services.test_submission_service import add_user

database = _database


def mutate(service, info, operation):
    if operation == "metadata":
        return service.update_exam(
            info["exam"], title="Late title", teacher_id=info["actor"]
        )
    if operation == "add":
        return service.add_questions(
            info["exam"], [info["questions"][0]], teacher_id=info["actor"]
        )
    return service.remove_questions(
        info["exam"], [info["questions"][1]], teacher_id=info["actor"]
    )


@pytest.mark.parametrize("operation", ["metadata", "add", "remove"])
def test_cached_draft_cannot_mutate_after_publication(database, operation):
    engine, info = database
    with Session(engine, expire_on_commit=False) as stale:
        cached = stale.get(Exam, info["exam"])
        list(cached.exam_question_links)
        list(cached.questions)
        stale.commit()
        with Session(engine) as current:
            ExamService(current).publish_exam(info["exam"], teacher_id=info["actor"])
            before = facts(current, info["exam"])
        with pytest.raises(ExamValidationError) as error:
            mutate(ExamService(stale), info, operation)
        assert getattr(error.value, "code", None) == "EXAM_PUBLISHED_IMMUTABLE"
        stale.rollback()
    with Session(engine) as fresh:
        assert fresh.get(Exam, info["exam"]).status == ExamStatus.PUBLISHED
        assert fresh.get(Exam, info["exam"]).title == "Assembly exam"
        assert facts(fresh, info["exam"]) == before


@pytest.mark.parametrize("operation", ["metadata", "add", "remove"])
def test_anomalous_draft_with_submission_remains_protected(database, operation):
    engine, info = database
    with Session(engine) as session:
        student = add_user(
            session,
            UserRole.STUDENT,
            username="historical-student",
            email="historical-student@example.com",
        )
        session.add(Submission(exam_id=info["exam"], student_id=student.id))
        session.commit()
        before = facts(session, info["exam"])
        with pytest.raises(ExamValidationError) as error:
            mutate(ExamService(session), info, operation)
        assert getattr(error.value, "code", None) == "EXAM_PUBLISHED_IMMUTABLE"
        session.rollback()
        assert session.get(Exam, info["exam"]).title == "Assembly exam"
        assert facts(session, info["exam"]) == before


def test_cached_published_cannot_reverse_archived_to_closed(database):
    engine, info = database
    with Session(engine) as session:
        ExamService(session).publish_exam(info["exam"], teacher_id=info["actor"])
    with Session(engine, expire_on_commit=False) as stale:
        cached = stale.get(Exam, info["exam"])
        assert cached.status == ExamStatus.PUBLISHED
        stale.commit()
        with Session(engine) as current:
            ExamService(current).update_exam_status(
                info["exam"], ExamStatus.ARCHIVED, teacher_id=info["actor"]
            )
        with pytest.raises(ExamValidationError):
            ExamService(stale).update_exam_status(
                info["exam"], ExamStatus.CLOSED, teacher_id=info["actor"]
            )
        stale.rollback()
    with Session(engine) as fresh:
        assert fresh.get(Exam, info["exam"]).status == ExamStatus.ARCHIVED


def test_cached_student_cannot_start_submission_after_close(database):
    engine, info = database
    with Session(engine) as session:
        student = add_user(
            session,
            UserRole.STUDENT,
            username="late-student",
            email="late-student@example.com",
        )
        student_id = student.id
        session.add(ExamParticipant(exam_id=info["exam"], student_id=student_id))
        session.commit()
        ExamService(session).publish_exam(info["exam"], teacher_id=info["actor"])
    with Session(engine, expire_on_commit=False) as stale:
        cached = stale.get(Exam, info["exam"])
        list(cached.questions)
        stale.commit()
        with Session(engine) as current:
            ExamService(current).update_exam_status(
                info["exam"], ExamStatus.CLOSED, teacher_id=info["actor"]
            )
        with pytest.raises(SubmissionNotAvailableError):
            SubmissionService(stale).create_submission(
                info["exam"], student_id, now=datetime.now(UTC)
            )
        stale.rollback()
    with Session(engine) as fresh:
        assert (
            fresh.scalar(
                select(Submission.id).where(Submission.exam_id == info["exam"])
            )
            is None
        )


def test_old_exam_writer_waits_course_lock_then_observes_publication(database):
    engine, info = database
    pid_queue = Queue()

    def late_writer():
        with Session(engine) as session:
            pid_queue.put(session.scalar(text("SELECT pg_backend_pid()")))
            try:
                mutate(ExamService(session), info, "metadata")
                return "unexpected_success"
            except ExamValidationError as error:
                session.rollback()
                return getattr(error, "code", None)

    with Session(engine) as publishing, ThreadPoolExecutor(max_workers=1) as workers:
        publishing.scalar(
            select(Course).where(Course.id == info["course"]).with_for_update()
        )
        future = workers.submit(late_writer)
        writer_pid = pid_queue.get(timeout=5)
        try:
            deadline = monotonic() + 5
            blocked = False
            while monotonic() < deadline and not future.done():
                with engine.connect() as observer:
                    blocked = bool(
                        observer.scalar(
                            text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"),
                            {"pid": writer_pid},
                        )
                    )
                if blocked:
                    break
                sleep(0.02)
            assert (
                blocked
            ), "old mutation must serialize behind the publication Course lock"
            ExamService(publishing).publish_exam(info["exam"], teacher_id=info["actor"])
        finally:
            publishing.rollback()
        assert future.result(timeout=10) == "EXAM_PUBLISHED_IMMUTABLE"
