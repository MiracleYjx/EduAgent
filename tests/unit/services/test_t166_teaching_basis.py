"""T166 explicit teaching basis snapshots and invalidation; TCR section 20."""

import asyncio
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.app.domain.enums import DocumentStatus
from backend.app.models import (
    Course,
    DocumentChunk,
    KnowledgeBase,
    QuestionSourceChunk,
    QuestionValidationResult,
)
from backend.app.services.content_validation_service import ContentValidationError
from tests.support.semantic_validation_doubles import StubSemanticProvider
from tests.unit.services.test_content_validation_service import semantic_case
from tests.unit.services.test_file_storage_service import (
    owned_document as _owned_document,
)

owned_document = _owned_document


def test_explicit_chunks_capture_real_current_text_without_changing_generation_sources(
    owned_document,
):
    session, doc, teacher, _root = owned_document
    service, question, refs = semantic_case(owned_document)
    provider = StubSemanticProvider()
    report = asyncio.run(
        service.validate_current(
            question.id,
            actor_id=teacher.id,
            teaching_chunk_ids=[refs.evidence[0].source_id],
            provider=provider,
        )
    )
    assert report.can_review and report.input_revision == 0
    evidence = report.input_refs.evidence[0]
    assert (
        evidence.kind == "chunk"
        and evidence.source_data == refs.evidence[0].source_data
    )
    assert provider.calls[0]["fields"]["content"] == question.content
    assert evidence.source_data["source_file"] == doc.original_filename
    assert not session.in_transaction()
    assert session.scalar(select(QuestionSourceChunk)) is None


def test_basis_change_increments_revision_and_new_running_blocks_previous_pass(
    owned_document,
):
    session, doc, teacher, _root = owned_document
    service, question, refs = semantic_case(owned_document)
    chunk_id = refs.evidence[0].source_id
    first = asyncio.run(
        service.validate_current(
            question.id,
            actor_id=teacher.id,
            teaching_chunk_ids=[chunk_id],
            provider=StubSemanticProvider(),
        )
    )
    original = deepcopy(session.get(QuestionValidationResult, first.id).input_refs)
    other = DocumentChunk(
        document_id=doc.id,
        course_id=doc.course_id,
        knowledge_base_id=doc.knowledge_base_id,
        chunk_index=1,
        content="Other actual teaching evidence",
        chunk_metadata={},
    )
    session.add(other)
    session.commit()

    class ObserveCurrent(StubSemanticProvider):
        async def generate_structured(self, messages, schema, model=None):
            old = service.get_validation(question.id, first.id, actor_id=teacher.id)
            assert old.stale and not old.can_review
            assert question.validation_revision == first.input_revision + 1
            return await super().generate_structured(messages, schema, model)

    second = asyncio.run(
        service.validate_current(
            question.id,
            actor_id=teacher.id,
            teaching_chunk_ids=[other.id],
            provider=ObserveCurrent(),
        )
    )
    assert second.input_revision == 1 and second.run_no == 2 and second.can_review
    assert session.get(QuestionValidationResult, first.id).input_refs == original
    unchanged = asyncio.run(
        service.validate_current(
            question.id,
            actor_id=teacher.id,
            teaching_chunk_ids=[other.id],
            provider=StubSemanticProvider(),
        )
    )
    assert unchanged.input_revision == 1 and unchanged.run_no == 3


def test_same_chunk_changed_text_is_a_new_input_revision(owned_document):
    session, _doc, teacher, _root = owned_document
    service, question, refs = semantic_case(owned_document)
    chunk = session.get(DocumentChunk, refs.evidence[0].source_id)
    first = asyncio.run(
        service.validate_current(
            question.id,
            actor_id=teacher.id,
            teaching_chunk_ids=[chunk.id],
            provider=StubSemanticProvider(),
        )
    )
    previous_text = first.input_refs.evidence[0].source_data["content_snapshot"]
    chunk.content = "Actually revised teaching source"
    session.commit()
    second = asyncio.run(
        service.validate_current(
            question.id,
            actor_id=teacher.id,
            teaching_chunk_ids=[chunk.id],
            provider=StubSemanticProvider(),
        )
    )
    assert second.input_revision == first.input_revision + 1
    assert (
        second.input_refs.evidence[0].source_data["content_snapshot"] == chunk.content
    )
    assert first.input_refs.evidence[0].source_data["content_snapshot"] == previous_text


@pytest.mark.parametrize(
    "invalid", ["empty", "missing", "not_ready", "foreign", "illegal_knowledge_base"]
)
def test_explicit_basis_rejects_unusable_or_foreign_sources_without_side_effects(
    owned_document, invalid
):
    session, doc, teacher, _root = owned_document
    service, question, refs = semantic_case(owned_document)
    ids = [refs.evidence[0].source_id]
    if invalid == "empty":
        ids = []
    elif invalid == "missing":
        ids = [uuid4()]
    elif invalid == "not_ready":
        doc.status = DocumentStatus.UPLOADED
    elif invalid == "foreign":
        other = Course(name="Other actual course", created_by=teacher.id)
        session.add(other)
        session.flush()
        session.get(DocumentChunk, ids[0]).course_id = other.id
    else:
        other = Course(name="Other actual course", created_by=teacher.id)
        session.add(other)
        session.flush()
        kb = KnowledgeBase(name="Foreign actual KB", course_id=other.id)
        session.add(kb)
        session.flush()
        doc.knowledge_base_id = kb.id
        session.get(DocumentChunk, ids[0]).knowledge_base_id = kb.id
    session.commit()
    provider = StubSemanticProvider()
    with pytest.raises(ContentValidationError):
        asyncio.run(
            service.validate_current(
                question.id,
                actor_id=teacher.id,
                teaching_chunk_ids=ids,
                provider=provider,
            )
        )
    assert provider.calls == []
    assert session.scalar(select(QuestionValidationResult)) is None
    assert question.validation_revision == 0


def test_none_reuses_only_actual_existing_generation_sources_not_prior_selected_basis(
    owned_document,
):
    _session, _doc, teacher, _root = owned_document
    service, question, refs = semantic_case(owned_document)
    first = asyncio.run(
        service.validate_current(
            question.id,
            actor_id=teacher.id,
            teaching_chunk_ids=[refs.evidence[0].source_id],
            provider=StubSemanticProvider(),
        )
    )
    with pytest.raises(ContentValidationError):
        asyncio.run(
            service.validate_current(
                question.id, actor_id=teacher.id, provider=StubSemanticProvider()
            )
        )
    assert len(service.list_validations(question.id, actor_id=teacher.id)) == 1
    assert service.get_validation(question.id, first.id, actor_id=teacher.id).can_review


def test_explicit_history_context_survives_true_revision_and_requires_new_checks(
    owned_document,
):
    from backend.app.domain.enums import QuestionStatus
    from backend.app.schemas.content_validation import (
        ManualContextReference,
        ManualDispositionRequest,
    )
    from backend.app.services.question_service import QuestionService

    session, _doc, teacher, _root = owned_document
    service, question, refs = semantic_case(owned_document)
    chunk_id = refs.evidence[0].source_id
    first = asyncio.run(
        service.validate_current(
            question.id,
            actor_id=teacher.id,
            teaching_chunk_ids=[chunk_id],
            provider=StubSemanticProvider(verdict="fail"),
        )
    )
    assert question.status is QuestionStatus.NEEDS_REVISION
    disposed = service.dispose_validation(
        question.id,
        first.id,
        ManualDispositionRequest(
            check_kind="answer_correctness",
            action="provide_evidence",
            reason="Actual teacher explained the missing condition",
            evidence_refs=[first.input_refs.evidence[0].evidence_id],
        ),
        actor_id=teacher.id,
    )
    event = disposed.manual_dispositions[0]
    original = deepcopy(
        session.get(QuestionValidationResult, first.id).manual_dispositions
    )
    QuestionService(session).update_question_status(
        question.id, QuestionStatus.PENDING_REVIEW, teacher_id=teacher.id
    )
    assert question.validation_revision > event.input_revision
    provider = StubSemanticProvider()
    second = asyncio.run(
        service.validate_current(
            question.id,
            actor_id=teacher.id,
            teaching_chunk_ids=[chunk_id],
            manual_context=[
                ManualContextReference(
                    validation_result_id=first.id, disposition_id=event.id
                )
            ],
            provider=provider,
        )
    )
    context = provider.calls[0]["manual_context"][0]
    assert (
        second.can_review
        and second.run_no == 2
        and second.input_revision == question.validation_revision
    )
    assert (
        context["id"] == str(event.id)
        and context["input_revision"] == event.input_revision
    )
    assert context["handled_by"] == str(teacher.id) and context["handled_at"].endswith(
        "Z"
    )
    assert (
        session.get(QuestionValidationResult, first.id).manual_dispositions == original
    )
    assert not service.get_validation(
        question.id, first.id, actor_id=teacher.id
    ).can_review
    assert second.manual_dispositions == []


@pytest.mark.parametrize("bad_reference", ["other_question", "missing_disposition"])
def test_manual_history_context_still_requires_explicit_authentic_same_question_facts(
    owned_document, bad_reference
):
    from backend.app.domain.enums import QuestionStatus
    from backend.app.models import Question
    from backend.app.schemas.content_validation import (
        ManualContextReference,
        ManualDispositionRequest,
    )

    session, doc, teacher, _root = owned_document
    service, question, refs = semantic_case(owned_document)
    other = Question(
        course_id=doc.course_id,
        created_by=teacher.id,
        type="SHORT_ANSWER",
        content="Another actual question",
        reference_answer="Other answer",
        scoring_rubric="Other criterion",
        score=1,
        status=QuestionStatus.PENDING_REVIEW,
    )
    session.add(other)
    session.commit()
    first = asyncio.run(
        service.validate_current(
            other.id,
            actor_id=teacher.id,
            teaching_chunk_ids=[refs.evidence[0].source_id],
            provider=StubSemanticProvider(),
        )
    )
    event = service.dispose_validation(
        other.id,
        first.id,
        ManualDispositionRequest(
            check_kind="answer_correctness",
            action="provide_evidence",
            reason="Other question actual teacher explanation",
            evidence_refs=[first.input_refs.evidence[0].evidence_id],
        ),
        actor_id=teacher.id,
    ).manual_dispositions[0]
    provider = StubSemanticProvider()
    identity = other.id if bad_reference == "missing_disposition" else question.id
    with pytest.raises(ContentValidationError) as error:
        asyncio.run(
            service.validate_current(
                identity,
                actor_id=teacher.id,
                teaching_chunk_ids=[refs.evidence[0].source_id],
                manual_context=[
                    ManualContextReference(
                        validation_result_id=first.id,
                        disposition_id=(
                            uuid4()
                            if bad_reference == "missing_disposition"
                            else event.id
                        ),
                    )
                ],
                provider=provider,
            )
        )
    assert error.value.code == "CONTENT_SOURCE_INVALID" and provider.calls == []
    assert len(service.list_validations(other.id, actor_id=teacher.id)) == 1
