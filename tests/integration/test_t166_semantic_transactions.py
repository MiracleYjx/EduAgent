"""T166 real PostgreSQL command, amendment and approval race boundaries; TCR section 20."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from backend.app.domain.enums import QuestionStatus
from backend.app.models import (
    Question,
    QuestionRevisionComment,
    QuestionValidationResult,
)
from backend.app.services.content_validation_service import ContentValidationService
from backend.app.services.question_service import (
    QuestionApprovedImmutableError,
    QuestionService,
)
from tests.integration.test_content_validation_persistence import pg_case as _pg_case
from tests.unit.services.test_t166_semantic_validation import SemanticProvider

pg_case = _pg_case


class PausedProvider(SemanticProvider):
    def __init__(self, entered, release, behavior="fail"):
        super().__init__(behavior)
        self.entered, self.release = entered, release

    async def generate_structured(self, messages, schema, model=None):
        result = await super().generate_structured(messages, schema, model)
        self.entered.set()
        if not await asyncio.to_thread(self.release.wait, 15):
            raise TimeoutError("Coordinated semantic test was not released")
        return result


def test_provider_wait_releases_question_lock_and_changed_revision_keeps_late_result_historical(
    pg_case,
):
    engine, question_id, teacher_id, refs, root = pg_case
    entered, release = Event(), Event()
    provider = PausedProvider(entered, release)

    def run():
        with Session(engine, expire_on_commit=False, autoflush=False) as session:
            return asyncio.run(
                ContentValidationService(session, root=root).run_validation(
                    question_id, actor_id=teacher_id, input_refs=refs, provider=provider
                )
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run)
        try:
            assert entered.wait(5), "Command never reached the real provider boundary."
            with Session(engine, autoflush=False) as editor:
                editor.execute(text("SET LOCAL lock_timeout = '2s'"))
                changed = QuestionService(editor).update_question(
                    question_id,
                    content="Actually revised after the call started",
                    teacher_id=teacher_id,
                )
                assert changed.content == "Actually revised after the call started"
        finally:
            release.set()
        old = future.result(timeout=10)
    assert old.outcome == "failed" and old.stale and not old.can_review
    assert provider.calls[0]["fields"]["content"] == "Real fixture condition"
    with Session(engine) as reader:
        current = reader.get(Question, question_id)
        assert current.validation_revision == 1
        assert current.status is QuestionStatus.PENDING_REVIEW
        assert reader.scalar(select(QuestionRevisionComment)) is None
        row = reader.get(QuestionValidationResult, old.id)
        assert row.input_revision == 0 and row.outcome == "failed"


def test_superseded_call_failure_cannot_replace_newest_passed_approval_basis(pg_case):
    engine, question_id, teacher_id, refs, root = pg_case
    entered, release = Event(), Event()

    def older():
        with Session(engine, expire_on_commit=False, autoflush=False) as session:
            return asyncio.run(
                ContentValidationService(session, root=root).run_validation(
                    question_id,
                    actor_id=teacher_id,
                    input_refs=refs,
                    provider=PausedProvider(entered, release),
                )
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(older)
        try:
            assert entered.wait(5)
            with Session(engine, expire_on_commit=False, autoflush=False) as latest:
                latest.execute(text("SET LOCAL lock_timeout = '2s'"))
                service = ContentValidationService(latest, root=root)
                new = asyncio.run(
                    service.run_validation(
                        question_id,
                        actor_id=teacher_id,
                        input_refs=refs,
                        provider=SemanticProvider(),
                    )
                )
                assert new.outcome == "passed" and new.run_no == 2 and new.can_review
        finally:
            release.set()
        old = future.result(timeout=10)
    assert old.outcome == "failed" and old.run_no == 1 and old.stale
    with Session(engine, expire_on_commit=False, autoflush=False) as approver:
        question = approver.get(Question, question_id)
        current = ContentValidationService(approver, root=root).require_can_approve(
            question, teacher_id
        )
        assert current.id == new.id
        approved = QuestionService(approver).update_question_status(
            question_id, QuestionStatus.APPROVED, teacher_id=teacher_id
        )
        assert approved.frozen_at is not None
    with Session(engine) as reader:
        assert reader.get(Question, question_id).status is QuestionStatus.APPROVED
        assert reader.scalar(select(QuestionRevisionComment)) is None


def test_approval_question_lock_prevents_concurrent_editor_from_replacing_validated_content(
    pg_case,
):
    engine, question_id, teacher_id, refs, root = pg_case
    with Session(engine, expire_on_commit=False, autoflush=False) as setup:
        view = asyncio.run(
            ContentValidationService(setup, root=root).run_validation(
                question_id,
                actor_id=teacher_id,
                input_refs=refs,
                provider=SemanticProvider(),
            )
        )
        assert view.can_review
    started, completed = Event(), Event()

    def edit():
        with Session(engine, autoflush=False) as writer:
            started.set()
            try:
                QuestionService(writer).update_question(
                    question_id,
                    content="Unapproved concurrent content",
                    teacher_id=teacher_id,
                )
            except QuestionApprovedImmutableError:
                writer.rollback()
                return "blocked by approved state"
            finally:
                completed.set()
        return "unexpected update"

    with Session(engine, expire_on_commit=False, autoflush=False) as approver:
        current = approver.get(Question, question_id)
        assert (
            ContentValidationService(approver, root=root)
            .require_can_approve(current, teacher_id)
            .can_review
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(edit)
            try:
                assert started.wait(5)
                assert not completed.wait(0.2), "Edit passed the live approval lock."
                approved = QuestionService(approver).update_question_status(
                    question_id, QuestionStatus.APPROVED, teacher_id=teacher_id
                )
                assert approved.frozen_at is not None
            finally:
                approver.rollback()
            assert future.result(timeout=10) == "blocked by approved state"
    with Session(engine) as reader:
        assert reader.get(Question, question_id).content == "Real fixture condition"
        assert reader.get(Question, question_id).status is QuestionStatus.APPROVED


def test_explicit_changed_basis_is_committed_before_call_and_late_pass_cannot_revive_previous_input(
    pg_case,
):
    from backend.app.models import DocumentChunk
    from backend.app.services.question_service import QuestionValidationError
    from tests.support.semantic_validation_doubles import StubSemanticProvider

    engine, question_id, teacher_id, refs, root = pg_case
    with Session(engine, expire_on_commit=False) as setup:
        original = setup.get(DocumentChunk, refs.evidence[0].source_id)
        other = DocumentChunk(
            document_id=original.document_id,
            course_id=original.course_id,
            knowledge_base_id=original.knowledge_base_id,
            chunk_index=1,
            content="Other actual teaching basis",
            chunk_metadata={"location": "page 2"},
        )
        setup.add(other)
        setup.commit()
        other_id = other.id
        first = asyncio.run(
            ContentValidationService(setup, root=root).validate_current(
                question_id,
                actor_id=teacher_id,
                teaching_chunk_ids=[original.id],
                provider=StubSemanticProvider(),
            )
        )
        assert first.can_review
    entered, release = Event(), Event()

    def call():
        with Session(engine, expire_on_commit=False, autoflush=False) as session:
            return asyncio.run(
                ContentValidationService(session, root=root).validate_current(
                    question_id,
                    actor_id=teacher_id,
                    teaching_chunk_ids=[other_id],
                    provider=PausedProvider(entered, release, behavior="pass"),
                )
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(call)
        try:
            assert entered.wait(5)
            with Session(engine, autoflush=False) as approver:
                approver.execute(text("SET LOCAL lock_timeout = '2s'"))
                old = ContentValidationService(approver, root=root).get_validation(
                    question_id, first.id, actor_id=teacher_id
                )
                assert old.stale and not old.can_review
                assert approver.get(Question, question_id).validation_revision == 1
                try:
                    QuestionService(approver).update_question_status(
                        question_id, QuestionStatus.APPROVED, teacher_id=teacher_id
                    )
                except QuestionValidationError:
                    approver.rollback()
                else:
                    raise AssertionError("Running new basis must block approval.")
            with Session(engine, autoflush=False) as editor:
                editor.execute(text("SET LOCAL lock_timeout = '2s'"))
                QuestionService(editor).update_question(
                    question_id,
                    content="Actual new condition during explicit call",
                    teacher_id=teacher_id,
                )
        finally:
            release.set()
        late = future.result(timeout=10)
    assert (
        late.outcome == "passed"
        and late.input_revision == 1
        and late.stale
        and not late.can_review
    )
    with Session(engine) as reader:
        current = reader.get(Question, question_id)
        assert (
            current.validation_revision == 2
            and current.status is QuestionStatus.PENDING_REVIEW
        )
        assert reader.get(QuestionValidationResult, first.id).input_revision == 0
