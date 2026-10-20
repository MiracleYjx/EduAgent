"""Regression cases for the five retrieval safety findings in Feature 002."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.app.ai.retrieval.base import (
    RetrievalFilters,
    RetrievalMode,
    RetrievalQuery,
    RetrievalScopeInvalidError,
    get_retriever,
)
from backend.app.domain.enums import DocumentStatus
from backend.app.models import Document, DocumentChunk
from backend.app.models.knowledge_base import KnowledgeBase
from backend.app.models.subject import MaterialSeries, MaterialVersion, Subject
from backend.app.services.settings.index_identity import IndexIdentityError
from tests.contract.test_retrieval_contract import _vector
from tests.contract.test_retrieval_scope_contract import RecordingReranker, seed_scope
from tests.postgres_helpers import isolated_postgres_engine

IDENTITY = {
    "provider": "openai_compatible",
    "adapter": "openai_compatible",
    "endpoint_identity": "http://127.0.0.1:9/v1",
    "model_id": "scope-safety-model",
    "model_version": "1",
    "dimension": 1024,
    "semantic_parameters": {},
    "status": "native",
}


@pytest.fixture
def safety_session():
    with isolated_postgres_engine() as engine, Session(
        engine, expire_on_commit=False
    ) as session:
        yield session


@pytest.fixture
def personal_scope(safety_session):
    session = safety_session
    subjects = [Subject(name="Scope A"), Subject(name="Scope B")]
    session.add_all(subjects)
    session.flush()
    kbs = [
        KnowledgeBase(subject_id=subject.id, name="Materials", embedding_identity=IDENTITY)
        for subject in subjects
    ]
    session.add_all(kbs)
    session.flush()
    documents = [
        Document(
            subject_id=subjects[owner].id,
            knowledge_base_id=kbs[owner].id,
            original_filename=f"scope-{index}.txt",
            file_format="txt",
            status=DocumentStatus.READY,
        )
        for index, owner in enumerate((0, 0, 1))
    ]
    session.add_all(documents)
    session.flush()
    chunks = [
        DocumentChunk(
            subject_id=document.subject_id,
            knowledge_base_id=document.knowledge_base_id,
            document_id=document.id,
            chunk_index=0,
            content="scopeword",
            embedding=list(_vector(0)),
            search_vector=func.to_tsvector("simple", "scopeword"),
            chunk_metadata={},
        )
        for document in documents
    ]
    session.add_all(chunks)
    series = MaterialSeries(subject_id=subjects[0].id, name="Editions")
    session.add(series)
    session.flush()
    versions = [
        MaterialVersion(
            subject_id=subjects[0].id,
            series_id=series.id,
            document_id=document.id,
            label=f"v{index}",
        )
        for index, document in enumerate(documents[:2])
    ]
    session.add_all(versions)
    session.commit()
    return SimpleNamespace(
        session=session, subjects=subjects, kbs=kbs, documents=documents,
        chunks=chunks, versions=versions,
    )


def retriever_for(mode, **kwargs):
    if mode is RetrievalMode.HYBRID_RERANK:
        kwargs["reranker"] = RecordingReranker()
    return get_retriever(mode, **kwargs)


@pytest.mark.parametrize("mode", list(RetrievalMode))
def test_explicit_knowledge_base_remains_an_intersection(personal_scope, mode):
    data = personal_scope
    retriever = retriever_for(mode)
    query = RetrievalQuery("scopeword", _vector(0))
    both_subjects = tuple(subject.id for subject in data.subjects)
    hits = retriever.search(
        data.session, query, top_k=10,
        filters=RetrievalFilters(
            subject_ids=both_subjects, knowledge_base_ids=(data.kbs[0].id,),
        ),
    )
    assert {hit.document_id for hit in hits} == {
        str(document.id) for document in data.documents[:2]
    }
    assert retriever.search(
        data.session, query,
        filters=RetrievalFilters(
            subject_ids=(data.subjects[0].id,), knowledge_base_ids=(data.kbs[1].id,),
        ),
    ) == []
    automatic = retriever.search(
        data.session, query, top_k=10,
        filters=RetrievalFilters(subject_ids=both_subjects),
    )
    assert {hit.document_id for hit in automatic} == {
        str(document.id) for document in data.documents
    }


@pytest.mark.parametrize("mode", [
    RetrievalMode.VECTOR_ONLY, RetrievalMode.HYBRID, RetrievalMode.HYBRID_RERANK,
])
@pytest.mark.parametrize("selection", ["version", "document", "unrestricted"])
def test_candidate_identity_is_checked_without_explicit_kb(personal_scope, mode, selection):
    data = personal_scope
    scope = (
        RetrievalFilters(material_version_ids=(data.versions[0].id,))
        if selection == "version"
        else RetrievalFilters(document_ids=(data.documents[0].id,))
        if selection == "document"
        else RetrievalFilters()
    )
    incompatible = {**IDENTITY, "model_id": "different-model-same-dimension"}
    with pytest.raises(IndexIdentityError) as failure:
        retriever_for(mode, index_identity=incompatible).search(
            data.session, RetrievalQuery("scopeword", _vector(0)), filters=scope,
        )
    assert failure.value.code == "EMBEDDING_INCOMPATIBLE"
    hits = retriever_for(mode, index_identity=IDENTITY).search(
        data.session, RetrievalQuery("scopeword", _vector(0)), top_k=10, filters=scope,
    )
    expected = data.documents if selection == "unrestricted" else data.documents[:1]
    assert {hit.document_id for hit in hits} == {str(doc.id) for doc in expected}


@pytest.mark.parametrize("mode", list(RetrievalMode))
def test_version_intersections_and_empty_candidates_stay_closed(personal_scope, mode):
    data = personal_scope
    # An unrelated selected subject must not force its model onto this version.
    data.kbs[1].embedding_identity = {**IDENTITY, "model_id": "another-subject-model"}
    data.session.commit()
    retriever = retriever_for(mode, **(
        {} if mode is RetrievalMode.KEYWORD_ONLY else {"index_identity": IDENTITY}
    ))
    query = RetrievalQuery("scopeword", _vector(0))
    hits = retriever.search(
        data.session, query,
        filters=RetrievalFilters(
            subject_ids=tuple(subject.id for subject in data.subjects),
            material_version_ids=(data.versions[0].id,),
        ),
    )
    assert [hit.document_id for hit in hits] == [str(data.documents[0].id)]
    for scope in (
        RetrievalFilters(
            material_version_ids=(data.versions[0].id,),
            document_ids=(data.documents[1].id,),
        ),
        RetrievalFilters(
            material_version_ids=(data.versions[0].id,),
            knowledge_base_ids=(data.kbs[1].id,),
        ),
    ):
        assert retriever.search(data.session, query, filters=scope) == []
    for scope in (
        RetrievalFilters(material_version_ids=(uuid4(),)),
        RetrievalFilters(
            subject_ids=(data.subjects[1].id,),
            material_version_ids=(data.versions[0].id,),
        ),
    ):
        with pytest.raises(RetrievalScopeInvalidError):
            retriever.search(data.session, query, filters=scope)


@pytest.mark.parametrize("mode", list(RetrievalMode))
def test_only_the_matching_confirmed_personal_point_is_retrieved(personal_scope, mode):
    data = personal_scope
    point, other = str(uuid4()), str(uuid4())
    data.chunks[0].chunk_metadata = {
        "knowledge_points": [point],
        "scope_confirmation": {"knowledge_points": {point: "confirmed"}},
    }
    data.chunks[1].chunk_metadata = {
        "knowledge_points": [point, other],
        "scope_confirmation": {"knowledge_points": {point: "pending", other: "confirmed"}},
    }
    data.session.commit()
    hits = retriever_for(mode).search(
        data.session, RetrievalQuery("scopeword", _vector(0), knowledge_points=(point,)),
        top_k=10, filters=RetrievalFilters(subject_ids=(data.subjects[0].id,)),
    )
    assert [hit.chunk_id for hit in hits] == [str(data.chunks[0].id)]


@pytest.mark.parametrize("mode", [RetrievalMode.VECTOR_ONLY, RetrievalMode.KEYWORD_ONLY])
@pytest.mark.parametrize("confirmation", [
    {}, None, [], "confirmed", {"other": "confirmed"}, {"point": "pending"},
    {"confirmed_by": str(uuid4())},
    {"confirmed_by": str(uuid4()), "confirmed_at": datetime.now(UTC).isoformat()},
])
def test_invalid_personal_confirmation_cannot_grant_scope(personal_scope, mode, confirmation):
    data = personal_scope
    for chunk in data.chunks:
        chunk.chunk_metadata = {
            "knowledge_points": ["point"],
            "scope_confirmation": {"knowledge_points": confirmation},
        }
    data.session.commit()
    assert retriever_for(mode).search(
        data.session, RetrievalQuery("scopeword", _vector(0), knowledge_points=("point",)),
        filters=RetrievalFilters(subject_ids=(data.subjects[0].id,)),
    ) == []


@pytest.mark.parametrize("mode", list(RetrievalMode))
def test_empty_confirmation_cannot_grant_legacy_scope(safety_session, mode):
    course, _, _, chunks, _, _ = seed_scope(safety_session)
    for chunk in chunks:
        chunk.chunk_metadata = {
            "knowledge_points": ["point"],
            "scope_confirmation": {"knowledge_points": {}},
        }
    safety_session.commit()
    assert retriever_for(mode).search(
        safety_session, RetrievalQuery("scopeword", _vector(0), knowledge_points=("point",)),
        filters=RetrievalFilters(course_ids=(course.id,)),
    ) == []
