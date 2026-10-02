"""T162 PostgreSQL scope contract: filtering must happen before recall Top-K."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.app.ai.retrieval.base import (
    RetrievalFilters,
    RetrievalMode,
    RetrievalQuery,
    RetrievalScopeInvalidError,
    RetrievalScopeNotReadyError,
    get_retriever,
)
from backend.app.ai.retrieval.reranker import BaseReranker
from backend.app.core.database import create_database_engine
from backend.app.domain.enums import DocumentPurpose, DocumentStatus
from backend.app.models import Base, Chapter
from backend.app.schemas.retrieval_scope import SectionRange
from tests.contract.test_retrieval_contract import _seed_course, _vector


@pytest.fixture
def scope_session():
    engine = create_database_engine(connect_timeout=3)
    schema = "t162_scope_" + uuid4().hex
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated = engine.execution_options(schema_translate_map={None: schema})
    try:
        Base.metadata.create_all(isolated)
        with Session(isolated, expire_on_commit=False) as session:
            yield session
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


def seed_scope(session):
    course, kb, document, chunks = _seed_course(
        session,
        name=uuid4().hex[:8],
        vectors=[_vector(0), _vector(1), _vector(0), _vector(0)],
        contents=[
            "scopeword scopeword scopeword",
            "scopeword teaching evidence",
            "scopeword",
            "scopeword",
        ],
    )
    chapter = Chapter(
        course_id=course.id,
        title="Chapter",
        sections=[
            {"section_order": 1, "title": "one"},
            {"section_order": 2, "title": "two"},
            {"section_order": 3, "title": "three"},
        ],
        confirmed_by=document.uploaded_by,
        confirmed_at=datetime.now(UTC),
    )
    other = Chapter(
        course_id=course.id,
        title="Same title is another identity",
        sections=[],
        confirmed_by=document.uploaded_by,
        confirmed_at=datetime.now(UTC),
    )
    session.add_all([chapter, other])
    session.flush()
    for item in chunks:
        item.chapter_id = chapter.id
        item.section_order = 1
    chunks[1].section_order = 2
    chunks[2].chapter_id = other.id
    chunks[2].section_order = None
    chunks[3].chapter_id = None
    chunks[3].section_order = None
    session.commit()
    return course, kb, document, chunks, chapter, other


class RecordingReranker(BaseReranker):
    def __init__(self):
        self.ids = []

    def rerank(self, query, candidates, top_k=5):
        self.ids = [item.chunk_id for item in candidates]
        return list(candidates[:top_k])


@pytest.mark.parametrize("mode", list(RetrievalMode))
def test_all_modes_filter_scope_before_top_k_and_rerank(scope_session, mode):
    course, kb, document, chunks, chapter, _ = seed_scope(scope_session)
    query = RetrievalQuery(
        "scopeword",
        _vector(0),
        chapter_ids=(chapter.id,),
        section_range=SectionRange(chapter_id=chapter.id, start_order=2, end_order=2),
    )
    reranker = RecordingReranker()
    kwargs = (
        {"candidate_k": 1}
        if mode is RetrievalMode.HYBRID
        else {"candidate_k": 1, "fusion_top_k": 1, "reranker": reranker}
        if mode is RetrievalMode.HYBRID_RERANK
        else {}
    )
    retriever = get_retriever(mode, **kwargs)
    results = retriever.search(
        scope_session,
        query,
        top_k=1,
        filters=RetrievalFilters(
            course_ids=(course.id,),
            knowledge_base_ids=(kb.id,),
            document_ids=(document.id,),
        ),
    )
    assert [item.chunk_id for item in results] == [str(chunks[1].id)]
    if mode is RetrievalMode.HYBRID_RERANK:
        assert reranker.ids == [str(chunks[1].id)]


@pytest.mark.parametrize("mode", list(RetrievalMode))
def test_empty_intersection_does_not_expand_range_or_invoke_rerank(scope_session, mode):
    course, _, _, _, chapter, other = seed_scope(scope_session)
    query = RetrievalQuery(
        "scopeword",
        _vector(0),
        chapter_ids=(other.id,),
        section_range=SectionRange(chapter_id=chapter.id, start_order=2, end_order=3),
    )
    reranker = RecordingReranker()
    kwargs = {"reranker": reranker} if mode is RetrievalMode.HYBRID_RERANK else {}
    assert (
        get_retriever(mode, **kwargs).search(
            scope_session, query, filters=RetrievalFilters(course_ids=(course.id,))
        )
        == []
    )
    assert reranker.ids == []


@pytest.mark.parametrize("mode", list(RetrievalMode))
def test_confirmed_json_labels_match_any_exact_member(scope_session, mode):
    course, _, _, chunks, _, _ = seed_scope(scope_session)
    confirmation = {
        "scope_confirmation": {
            "knowledge_points": {
                "confirmed_by": str(uuid4()),
                "confirmed_at": datetime.now(UTC).isoformat(),
            }
        }
    }
    chunks[0].chunk_metadata = {
        "knowledge_points": ["Newton II"]
    }  # unconfirmed legacy tag
    chunks[1].chunk_metadata = {
        **confirmation,
        "knowledge_points": ["Newton II", "A  B"],
    }
    chunks[2].chunk_metadata = {
        **confirmation,
        "knowledge_points": ["newton II", "Newton III"],
    }
    chunks[3].chunk_metadata = {**confirmation, "knowledge_points": []}
    scope_session.commit()
    reranker = RecordingReranker()
    kwargs = {"reranker": reranker} if mode is RetrievalMode.HYBRID_RERANK else {}
    retriever = get_retriever(mode, **kwargs)
    query = RetrievalQuery(
        "scopeword", _vector(0), knowledge_points=(" missing ", " Newton II ")
    )
    hits = retriever.search(
        scope_session, query, top_k=5, filters=RetrievalFilters(course_ids=(course.id,))
    )
    assert [item.chunk_id for item in hits] == [str(chunks[1].id)]
    assert (
        retriever.search(
            scope_session,
            RetrievalQuery("scopeword", _vector(0), knowledge_points=("Newton",)),
            filters=RetrievalFilters(course_ids=(course.id,)),
        )
        == []
    )


@pytest.mark.parametrize("labels", [None, [], "Newton II", {"Newton II": True}])
def test_null_empty_or_non_array_labels_do_not_match(scope_session, labels):
    course, _, _, chunks, _, _ = seed_scope(scope_session)
    for item in chunks:
        item.chunk_metadata = {
            "knowledge_points": labels,
            "scope_confirmation": {"knowledge_points": {}},
        }
    scope_session.commit()
    assert (
        get_retriever("vector_only").search(
            scope_session,
            RetrievalQuery("scopeword", _vector(0), knowledge_points=("Newton II",)),
            filters=RetrievalFilters(course_ids=(course.id,)),
        )
        == []
    )


def test_scope_requires_authorized_course_and_rejects_foreign_chapter_document(
    scope_session,
):
    course, _, _, _, chapter, _ = seed_scope(scope_session)
    foreign, _, document, _, foreign_chapter, _ = seed_scope(scope_session)
    retriever = get_retriever("vector_only")
    for query, filters in [
        (RetrievalQuery("scopeword", _vector(0), chapter_ids=(chapter.id,)), None),
        (
            RetrievalQuery("scopeword", _vector(0), chapter_ids=(foreign_chapter.id,)),
            RetrievalFilters(course_ids=(course.id,)),
        ),
        (
            RetrievalQuery("scopeword", _vector(0), chapter_ids=(uuid4(),)),
            RetrievalFilters(course_ids=(course.id,)),
        ),
        (
            RetrievalQuery("scopeword", _vector(0)),
            RetrievalFilters(course_ids=(course.id,), document_ids=(document.id,)),
        ),
    ]:
        with pytest.raises(RetrievalScopeInvalidError):
            retriever.search(scope_session, query, filters=filters)
    assert foreign.id != course.id


def test_range_catalogue_errors_are_distinct_from_valid_empty_scope(scope_session):
    course, _, _, _, chapter, other = seed_scope(scope_session)
    retriever = get_retriever("vector_only")
    filters = RetrievalFilters(course_ids=(course.id,))
    with pytest.raises(RetrievalScopeNotReadyError):
        retriever.search(
            scope_session,
            RetrievalQuery(
                "scopeword",
                _vector(0),
                section_range=SectionRange(
                    chapter_id=other.id, start_order=1, end_order=1
                ),
            ),
            filters=filters,
        )
    with pytest.raises(RetrievalScopeInvalidError):
        retriever.search(
            scope_session,
            RetrievalQuery(
                "scopeword",
                _vector(0),
                section_range=SectionRange(
                    chapter_id=chapter.id, start_order=1, end_order=4
                ),
            ),
            filters=filters,
        )
    assert (
        retriever.search(
            scope_session,
            RetrievalQuery(
                "scopeword",
                _vector(0),
                section_range=SectionRange(
                    chapter_id=chapter.id, start_order=3, end_order=3
                ),
            ),
            filters=filters,
        )
        == []
    )


@pytest.mark.parametrize("mode", list(RetrievalMode))
def test_no_scope_keeps_legacy_unknown_chunks_and_ready_purpose_filter(
    scope_session, mode
):
    course, _, document, chunks, _, _ = seed_scope(scope_session)
    kwargs = (
        {"reranker": RecordingReranker()} if mode is RetrievalMode.HYBRID_RERANK else {}
    )
    retriever = get_retriever(mode, **kwargs)
    query = (
        _vector(0)
        if mode is RetrievalMode.VECTOR_ONLY
        else "scopeword"
        if mode is RetrievalMode.KEYWORD_ONLY
        else RetrievalQuery("scopeword", _vector(0))
    )
    filters = RetrievalFilters(course_ids=(course.id,))
    assert {
        item.chunk_id
        for item in retriever.search(scope_session, query, top_k=10, filters=filters)
    } == {str(item.id) for item in chunks}
    document.status = DocumentStatus.FAILED
    scope_session.commit()
    assert retriever.search(scope_session, query, filters=filters) == []
    document.status = DocumentStatus.READY
    document.purpose = DocumentPurpose.PAPER_SOURCE
    document.knowledge_base_id = None
    scope_session.commit()
    assert retriever.search(scope_session, query, filters=filters) == []


@pytest.mark.parametrize("mode", list(RetrievalMode))
def test_generation_context_consumes_explicit_scope_in_all_modes(scope_session, mode):
    import asyncio

    from sqlalchemy import func

    from backend.app.ai.agents.question_agent import (
        build_generation_context,
        build_generation_query,
    )
    from backend.app.ai.agents.state import QuestionGenerationRequest
    from backend.app.schemas.retrieval_scope import RetrievalScope
    from tests.support.question_generation_doubles import StubEmbeddingProvider
    from tests.unit.settings_helpers import build_test_settings

    course, _, document, chunks, chapter, _ = seed_scope(scope_session)
    selected = RetrievalScope(
        document_ids=(document.id,),
        chapter_ids=(chapter.id,),
        section_range=SectionRange(chapter_id=chapter.id, start_order=2, end_order=2),
    )
    request = QuestionGenerationRequest(
        course_id=str(course.id),
        knowledge_points=["legacy prompt label"],
        retrieval_scope=selected,
    )
    text_query = build_generation_query(request)
    for item in chunks:
        item.content = text_query + " teaching evidence"
        item.search_vector = func.to_tsvector("simple", item.content)
    scope_session.commit()
    embedding = StubEmbeddingProvider(_vector(0))
    context = asyncio.run(
        build_generation_context(
            scope_session,
            request,
            mode=mode,
            embedding_provider=embedding,
            reranker=RecordingReranker(),
            settings=build_test_settings(),
        )
    )
    assert context.retrieved_context_ids == (str(chunks[1].id),)
    assert embedding.queries == (
        [] if mode is RetrievalMode.KEYWORD_ONLY else [text_query]
    )


def test_generation_scope_failure_preserves_reason_and_prevents_provider_calls(
    scope_session,
):
    import asyncio

    from backend.app.ai.agents.question_agent import QuestionAgent
    from backend.app.ai.agents.state import (
        AgentInput,
        AgentType,
        QuestionGenerationRequest,
    )
    from backend.app.schemas.retrieval_scope import RetrievalScope
    from tests.support.question_generation_doubles import (
        StubEmbeddingProvider,
        StubQuestionProvider,
    )
    from tests.unit.settings_helpers import build_test_settings

    course, _, _, _, _, _ = seed_scope(scope_session)
    embedding = StubEmbeddingProvider(_vector(0))
    provider = StubQuestionProvider()
    output = asyncio.run(
        QuestionAgent(provider=provider).generate(
            scope_session,
            AgentInput(
                agent_type=AgentType.QUESTION,
                request_id="scope-error",
                generation_request=QuestionGenerationRequest(
                    course_id=str(course.id),
                    retrieval_scope=RetrievalScope(chapter_ids=(uuid4(),)),
                ),
            ),
            embedding_provider=embedding,
            settings=build_test_settings(),
        )
    )
    assert output.error.error_code == "RETRIEVAL_SCOPE_INVALID"
    assert "章节" in output.error.message
    assert not embedding.queries and not provider.calls


def test_chapter_union_and_unknown_section_preserve_real_scope(scope_session):
    course, _, _, chunks, chapter, other = seed_scope(scope_session)
    chunks[0].section_order = None
    scope_session.commit()
    hits = get_retriever("vector_only").search(
        scope_session,
        RetrievalQuery("scopeword", _vector(0), chapter_ids=(chapter.id, other.id)),
        top_k=10,
        filters=RetrievalFilters(course_ids=(course.id,)),
    )
    assert {item.chunk_id for item in hits} == {str(item.id) for item in chunks[:3]}
    narrowed = get_retriever("vector_only").search(
        scope_session,
        RetrievalQuery(
            "scopeword",
            _vector(0),
            chapter_ids=(chapter.id,),
            section_range=SectionRange(
                chapter_id=chapter.id, start_order=1, end_order=2
            ),
        ),
        top_k=10,
        filters=RetrievalFilters(course_ids=(course.id,)),
    )
    assert [item.chunk_id for item in narrowed] == [str(chunks[1].id)]
