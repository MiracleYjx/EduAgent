"""Real PG proof that grading labels are trace text, never required textbook terms."""

import asyncio
from copy import deepcopy

import pytest
from sqlalchemy import select

from backend.app.ai.retrieval.base import (
    RetrievalFilters,
    RetrievalMode,
    RetrievalQuery,
)
from backend.app.ai.retrieval.keyword_search import KeywordSearchRetriever
from backend.app.domain.enums import DocumentStatus
from backend.app.models.document import Document
from backend.app.models.document_chunk import DocumentChunk
from backend.app.schemas.retrieval_scope import RetrievalScope
from backend.app.services.grading.grading_context import (
    InsufficientContextError,
    SubjectiveGradingSource,
    build_grading_context,
    build_query_text,
)
from tests.integration import test_learning_material_versions as materials
from tests.unit.grading.test_grading_context import StubEmbeddingProvider

space = materials.space


def test_real_keyword_grading_finds_bounded_facts_but_keeps_ready_and_confirmation_gates(space):
    sid = materials.subject(space)
    doc = materials.upload(space, sid, "actual.md", b"# Intro\n\n## alpha\n\nalpha beta gamma delta").json()
    suggested = space.client.post(f"/api/learning/subjects/{sid}/knowledge/suggest", json={})
    assert suggested.status_code == 200, suggested.text
    value = suggested.json()
    point = next(p for p in value["knowledge_points"] if p["name"] == "alpha")
    chapter = next(c for c in value["chapters"] if c["title"] == "Intro / alpha")
    confirmed = space.client.post(f"/api/learning/subjects/{sid}/knowledge/confirm", json={
        "knowledge_points": [{"id": point["id"], "name": point["name"]}],
        "chapters": [{"id": chapter["id"], "title": chapter["title"],
            "knowledge_point_ids": [point["id"]]}],
    })
    assert confirmed.status_code == 200, confirmed.text
    materials.index(space, doc["id"])
    source = SubjectiveGradingSource(question_type="short_answer", subject_id=sid,
        question_content="alpha", reference_answer="beta", scoring_rubric="gamma", student_answer="delta",
        grounding="uploaded_material", retrieval_scope=RetrievalScope(
            document_ids=[doc["id"]], knowledge_points=[point["id"]]))
    embedding = StubEmbeddingProvider()
    with space.subjects.sessions() as session:
        filters = RetrievalFilters(subject_ids=(sid,), document_ids=(doc["id"],))
        old_query = RetrievalQuery(text=build_query_text(source), embedding=(), knowledge_points=(point["id"],))
        assert KeywordSearchRetriever().search(session, old_query, filters=filters) == []
        context = asyncio.run(build_grading_context(session, source, mode=RetrievalMode.KEYWORD_ONLY,
            embedding_provider=embedding))
        assert context.is_sufficient and len(context.chunks) == 1
        assert context.chunks[0].document_id == doc["id"]
        assert context.query_text == build_query_text(source)
        assert embedding.queries == []
        chunk = session.scalar(select(DocumentChunk).where(DocumentChunk.document_id == doc["id"]))
        metadata = deepcopy(chunk.chunk_metadata)
        chunk.chunk_metadata = {**metadata, "scope_confirmation": {"knowledge_points": {point["id"]: "pending"}}}
        session.commit()
        with pytest.raises(InsufficientContextError):
            asyncio.run(build_grading_context(session, source, mode=RetrievalMode.KEYWORD_ONLY))
        chunk.chunk_metadata = metadata
        document = session.get(Document, doc["id"])
        document.status = DocumentStatus.PARSING
        session.commit()
        with pytest.raises(InsufficientContextError):
            asyncio.run(build_grading_context(session, source, mode=RetrievalMode.KEYWORD_ONLY))
