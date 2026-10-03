"""Real persisted teaching references used by focused approval regression fixtures."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.domain.enums import DocumentStatus
from backend.app.models import (
    Document,
    DocumentChunk,
    KnowledgeBase,
    Question,
    QuestionSourceChunk,
)
from backend.app.services.content_validation_service import ContentValidationService
from tests.support.semantic_validation_doubles import StubSemanticProvider


def seed_teaching_reference(session: Session, question_id: UUID | str) -> DocumentChunk:
    question = session.get(Question, UUID(str(question_id)))
    assert question is not None
    basis = KnowledgeBase(course_id=question.course_id, name="Controlled review basis")
    session.add(basis)
    session.flush()
    document = Document(
        course_id=question.course_id,
        knowledge_base_id=basis.id,
        uploaded_by=question.created_by,
        original_filename="controlled-review-basis.txt",
        file_format="txt",
        status=DocumentStatus.READY,
    )
    session.add(document)
    session.flush()
    chunk = DocumentChunk(
        document_id=document.id,
        course_id=question.course_id,
        knowledge_base_id=basis.id,
        chunk_index=0,
        content="Controlled teaching evidence for protocol tests; not independent teacher truth.",
        chunk_metadata={},
    )
    session.add(chunk)
    session.flush()
    session.add(
        QuestionSourceChunk(
            question_id=question.id,
            chunk_id=chunk.id,
            live_chunk_id=chunk.id,
            document_id=document.id,
            course_id=question.course_id,
            source_order=0,
            content_snapshot=chunk.content,
            source_file=document.original_filename,
            chunk_index=0,
            retrieval_rank=1,
            score_kind="controlled",
            score_value=Decimal(1),
        )
    )
    session.commit()
    return chunk


def persist_current_semantic_pass(
    session: Session, question_id: UUID | str, teacher_id: UUID | str
):
    identity = UUID(str(question_id))
    if (
        session.scalar(
            select(QuestionSourceChunk.id).where(
                QuestionSourceChunk.question_id == identity
            )
        )
        is None
    ):
        seed_teaching_reference(session, identity)
    report = asyncio.run(
        ContentValidationService(session).run_validation(
            identity, actor_id=UUID(str(teacher_id)), provider=StubSemanticProvider()
        )
    )
    assert report.outcome == "passed" and report.can_review
    return report
