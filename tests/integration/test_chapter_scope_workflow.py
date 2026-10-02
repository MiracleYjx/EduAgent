"""T161 persisted-original re-splits, atomic replacement and source snapshot lifecycle."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.ai.embedding.base import EmbeddingProviderError
from backend.app.domain.enums import DocumentStatus, QuestionStatus, QuestionType
from backend.app.models import Document, DocumentChunk, Question, QuestionSourceChunk
from backend.app.schemas.chapter_scope import (
    ChapterWrite,
    ChunkScopeUpdate,
    SourceSplit,
)
from backend.app.services.knowledge_base_service import (
    KnowledgeBaseService,
    KnowledgeBaseValidationError,
)
from tests.postgres_helpers import isolated_postgres_engine
from tests.unit.services.test_knowledge_base_ingestion import (
    StubEmbeddingProvider,
    add_course,
    add_teacher,
    build_factory,
)

PAYLOAD = b"A" * 120 + b"B" * 120


def _persisted_source(session, storage_root):
    teacher = add_teacher(session)
    course = add_course(session, teacher)
    service = KnowledgeBaseService(session, storage_root=storage_root)
    knowledge = service.create_knowledge_base(
        course_id=course.id, name="Source", teacher_id=teacher.id
    )
    document = service.upload_document(
        knowledge_base_id=knowledge.id,
        uploaded_by=teacher.id,
        original_filename="source.txt",
        content=PAYLOAD,
        teacher_id=teacher.id,
    )
    result = service.ingest_document(
        document.id,
        teacher_id=teacher.id,
        ingestion_service_factory=build_factory(provider=StubEmbeddingProvider()),
    )
    assert result.status is DocumentStatus.READY
    chunk = session.scalar(
        select(DocumentChunk).where(DocumentChunk.document_id == UUID(document.id))
    )
    chapter = service.create_chapter(
        course.id,
        ChapterWrite(title="Chapter", sections=[{"section_order": 1, "title": "Part"}]),
        teacher.id,
    )
    service.confirm_chunk_scope(
        chunk.id,
        ChunkScopeUpdate(
            chapter_id=chapter["id"],
            section_order=1,
            knowledge_points=["Confirmed label"],
        ),
        teacher.id,
    )
    question = Question(
        course_id=UUID(course.id),
        type=QuestionType.SHORT_ANSWER,
        content="From original source",
        score=Decimal("2.00"),
        status=QuestionStatus.DRAFT,
        created_by=teacher.id,
    )
    session.add(question)
    session.flush()
    source = QuestionSourceChunk(
        question_id=question.id,
        chunk_id=chunk.id,
        live_chunk_id=chunk.id,
        document_id=chunk.document_id,
        course_id=chunk.course_id,
        source_order=0,
        content_snapshot=chunk.content,
        source_file="source.txt",
        chunk_index=chunk.chunk_index,
    )
    session.add(source)
    session.commit()
    return teacher.id, service, UUID(document.id), chunk.id, source.id


def test_resplit_original_publishes_complete_chunks_and_preserves_historical_snapshot(
    tmp_path,
) -> None:
    with isolated_postgres_engine() as engine, Session(engine) as session:
        teacher, service, document, old_chunk, source_id = _persisted_source(
            session, tmp_path
        )
        preview = service.document_source_sections(document, teacher)
        assert preview[0]["text"].encode() == PAYLOAD
        observed = []

        @event.listens_for(session, "after_commit")
        def observe_commit(_session):
            with Session(engine) as reader:
                status = reader.get(Document, document).status
                chunks = reader.scalars(
                    select(DocumentChunk).where(DocumentChunk.document_id == document)
                ).all()
                observed.append(
                    (
                        status,
                        {row.id for row in chunks},
                        all(
                            row.embedding is not None and row.search_vector
                            for row in chunks
                        ),
                    )
                )

        provider = StubEmbeddingProvider()
        result = service.resplit_document(
            document,
            [SourceSplit(section_index=1, cut_points=[120])],
            teacher,
            ingestion_service_factory=build_factory(provider=provider),
        )
        assert result.status is DocumentStatus.READY
        with Session(engine) as reader:
            stored = reader.scalars(
                select(DocumentChunk)
                .where(DocumentChunk.document_id == document)
                .order_by(DocumentChunk.chunk_index)
            ).all()
            assert [row.content for row in stored] == ["A" * 120, "B" * 120]
            assert len(stored) == result.chunk_count == 2
            assert old_chunk not in {row.id for row in stored}
            assert provider.calls == [["A" * 120, "B" * 120]]
            for row in stored:
                assert row.embedding == pytest.approx(provider._vector(row.content))
                assert row.search_vector
                assert row.chapter_id is None and row.section_order is None
                assert "knowledge_points" not in row.chunk_metadata
                assert "scope_confirmation" not in row.chunk_metadata
                start, end = (
                    row.chunk_metadata["start_char"],
                    row.chunk_metadata["end_char"],
                )
                assert row.content == preview[0]["text"][start:end]
            source = reader.get(QuestionSourceChunk, source_id)
            assert source.live_chunk_id is None
            assert source.chunk_id == old_chunk
            assert source.content_snapshot.encode() == PAYLOAD
            assert source.chunk_index == 0
        assert observed
        for status, ids, complete in observed:
            assert complete
            if status is DocumentStatus.READY:
                assert old_chunk not in ids and len(ids) == 2
            else:
                assert status in {
                    DocumentStatus.PARSING,
                    DocumentStatus.CHUNKING,
                    DocumentStatus.EMBEDDING,
                }
                assert ids == {old_chunk}


@pytest.mark.parametrize("failure", ["embedding", "commit"])
def test_resplit_failure_retains_original_chunks_and_reports_real_failed_state(
    tmp_path, failure
) -> None:
    with isolated_postgres_engine() as engine, Session(engine) as session:
        teacher, service, document, old_chunk, source_id = _persisted_source(
            session, tmp_path
        )
        old = session.get(DocumentChunk, old_chunk)
        metadata, content, vector, search_vector = (
            deepcopy(old.chunk_metadata),
            old.content,
            old.embedding,
            old.search_vector,
        )

        class FailingEmbedding(StubEmbeddingProvider):
            async def embed_documents(self, documents):
                raise EmbeddingProviderError("Controlled re-split embedding failure")

        def fail_ready_commit(_session):
            if session.get(Document, document).status is DocumentStatus.READY:
                raise SQLAlchemyError("Controlled re-split publication failure")

        provider = (
            FailingEmbedding() if failure == "embedding" else StubEmbeddingProvider()
        )
        if failure == "commit":
            event.listen(session, "before_commit", fail_ready_commit)
        try:
            result = service.resplit_document(
                document,
                [SourceSplit(section_index=1, cut_points=[120])],
                teacher,
                ingestion_service_factory=build_factory(provider=provider),
            )
        finally:
            if failure == "commit":
                event.remove(session, "before_commit", fail_ready_commit)
        assert result.status is DocumentStatus.FAILED
        assert result.chunk_count == 0 and result.error_code and result.error_message
        with Session(engine) as reader:
            saved = reader.get(Document, document)
            assert saved.status is DocumentStatus.FAILED
            assert saved.error_code == result.error_code
            chunks = reader.scalars(
                select(DocumentChunk).where(DocumentChunk.document_id == document)
            ).all()
            assert len(chunks) == 1 and chunks[0].id == old_chunk
            assert chunks[0].content == content
            assert chunks[0].embedding == vector
            assert chunks[0].search_vector == search_vector
            assert chunks[0].chunk_metadata == metadata
            source = reader.get(QuestionSourceChunk, source_id)
            assert source.live_chunk_id == old_chunk
            assert source.content_snapshot.encode() == PAYLOAD
        retry = service.resplit_document(
            document,
            [SourceSplit(section_index=1, cut_points=[120])],
            teacher,
            ingestion_service_factory=build_factory(provider=StubEmbeddingProvider()),
        )
        assert retry.status is DocumentStatus.READY
        with Session(engine) as reader:
            assert reader.get(QuestionSourceChunk, source_id).live_chunk_id is None
            assert (
                reader.get(QuestionSourceChunk, source_id).content_snapshot.encode()
                == PAYLOAD
            )


def test_stale_ready_session_cannot_start_second_resplit_during_embedding(
    tmp_path,
) -> None:
    from threading import Event, Thread

    with isolated_postgres_engine() as engine, Session(engine) as seed:
        teacher, _service, document, old_chunk, source_id = _persisted_source(
            seed, tmp_path
        )
        entered, release = Event(), Event()
        outcomes = []

        class PausedEmbedding(StubEmbeddingProvider):
            async def embed_documents(self, documents):
                entered.set()
                if not release.wait(timeout=10):
                    raise EmbeddingProviderError(
                        "Coordinated test embedding was not released"
                    )
                return await super().embed_documents(documents)

        def first_writer():
            try:
                with Session(engine) as writer:
                    service = KnowledgeBaseService(writer, storage_root=tmp_path)
                    outcomes.append(
                        service.resplit_document(
                            document,
                            [SourceSplit(section_index=1, cut_points=[120])],
                            teacher,
                            ingestion_service_factory=build_factory(
                                provider=PausedEmbedding()
                            ),
                        )
                    )
            except Exception as exc:  # noqa: BLE001 -- Preserve worker failures for main-thread assertions.
                outcomes.append(exc)

        with Session(engine) as contender:
            cached = contender.get(Document, document)
            assert cached.status is DocumentStatus.READY
            worker = Thread(target=first_writer)
            worker.start()
            try:
                assert entered.wait(timeout=5), (
                    "First writer never reached real Embedding"
                )
                assert (
                    cached.status is DocumentStatus.READY
                )  # Deliberately stale identity-map row.
                with pytest.raises(KnowledgeBaseValidationError, match="正在处理"):
                    KnowledgeBaseService(
                        contender, storage_root=tmp_path
                    ).resplit_document(
                        document,
                        [SourceSplit(section_index=1, cut_points=[60])],
                        teacher,
                        ingestion_service_factory=build_factory(
                            provider=StubEmbeddingProvider()
                        ),
                    )
            finally:
                contender.rollback()
                release.set()
                worker.join(timeout=10)
            assert not worker.is_alive()
        assert len(outcomes) == 1
        assert not isinstance(outcomes[0], Exception), outcomes
        assert outcomes[0].status is DocumentStatus.READY
        with Session(engine) as reader:
            chunks = reader.scalars(
                select(DocumentChunk)
                .where(DocumentChunk.document_id == document)
                .order_by(DocumentChunk.chunk_index)
            ).all()
            assert [row.content for row in chunks] == ["A" * 120, "B" * 120]
            assert old_chunk not in {row.id for row in chunks}
            source = reader.get(QuestionSourceChunk, source_id)
            assert (
                source.live_chunk_id is None
                and source.content_snapshot.encode() == PAYLOAD
            )
