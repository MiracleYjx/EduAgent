"""T040 检索契约测试（失败优先）。

契约以 ``.specify/contracts/rag-retrieval.md`` 为准：四种检索模式、九个必需结果字段、
来源追踪、空上下文与模式选择。本文件先于 T041/T042 实现编写，因此在实现落地前应当
失败；实现完成后同一套断言必须全部通过。

本文件不访问真实模型：语义检索只接收调用方传入的 query embedding，关键词检索只接收
查询文本。
"""

from __future__ import annotations

from collections.abc import Generator, Sequence
from dataclasses import fields
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.ai.retrieval.base import (
    DEFAULT_TOP_K,
    MAX_TOP_K,
    RETRIEVAL_INVALID_INPUT,
    RETRIEVAL_MODE_NOT_IMPLEMENTED,
    BaseRetriever,
    RetrievalFilters,
    RetrievalInputError,
    RetrievalMode,
    RetrievalModeNotImplementedError,
    RetrievedChunk,
    get_retriever,
    normalize_mode,
    normalize_query_text,
    normalize_query_vector,
    normalize_top_k,
    resolve_filters,
)
from backend.app.core.config import get_settings
from backend.app.core.database import Base, create_database_engine
from backend.app.domain.enums import UserRole
from backend.app.models import (
    Course,
    Document,
    DocumentChunk,
    KnowledgeBase,
    Role,
    User,
)
from backend.app.models.document_chunk import EMBEDDING_VECTOR_DIMENSION

DOCUMENT_ID = UUID("11111111-1111-4111-8111-111111111111")
COURSE_ID = UUID("22222222-2222-4222-8222-222222222222")
KNOWLEDGE_BASE_ID = UUID("33333333-3333-4333-8333-333333333333")

#: 契约要求的结果字段（``score`` 为本地便捷属性，不属于契约字段）。
CONTRACT_FIELDS = {
    "chunk_id",
    "course_id",
    "document_id",
    "content",
    "semantic_score",
    "keyword_score",
    "fusion_score",
    "rerank_score",
    "rank",
}


class FakeDocumentChunk:
    """模拟 ``DocumentChunk`` 实体，用于校验来源追踪字段的映射。"""

    def __init__(self) -> None:
        self.id = DOCUMENT_ID
        self.course_id = COURSE_ID
        self.document_id = DOCUMENT_ID
        self.content = "检索增强生成结合检索与生成。"
        self.chunk_metadata = {
            "document_id": str(DOCUMENT_ID),
            "course_id": str(COURSE_ID),
            "chunk_index": 2,
            "location": "第 2 段",
        }


def test_retrieval_modes_match_contract() -> None:
    """四种检索模式必须与契约和 Benchmark 配置名一致。"""

    assert {mode.value for mode in RetrievalMode} == {
        "vector_only",
        "keyword_only",
        "hybrid",
        "hybrid_rerank",
    }
    assert normalize_mode("VECTOR_ONLY") is RetrievalMode.VECTOR_ONLY
    assert normalize_mode("hybrid") is RetrievalMode.HYBRID
    with pytest.raises(RetrievalInputError):
        normalize_mode("semantic")


def test_retrieved_chunk_exposes_required_fields_and_source_tracking() -> None:
    """结果必须暴露契约字段，并保留 chunk/课程/资料来源。"""

    assert {item.name for item in fields(RetrievedChunk)} >= CONTRACT_FIELDS

    candidate = RetrievedChunk.from_document_chunk(
        FakeDocumentChunk(), rank=0, semantic_score=0.87
    )

    assert candidate.chunk_id == str(DOCUMENT_ID)
    assert candidate.course_id == str(COURSE_ID)
    assert candidate.document_id == str(DOCUMENT_ID)
    assert candidate.content
    assert candidate.semantic_score == 0.87
    assert candidate.score == 0.87
    assert candidate.rank == 0
    assert candidate.metadata["chunk_index"] == 2
    payload = candidate.as_dict()
    assert payload["chunk_id"] == str(DOCUMENT_ID)
    assert payload["course_id"] == str(COURSE_ID)
    assert payload["document_id"] == str(DOCUMENT_ID)


@pytest.mark.parametrize(
    "mode",
    [RetrievalMode.VECTOR_ONLY, RetrievalMode.KEYWORD_ONLY],
)
def test_implemented_modes_return_retriever(mode: RetrievalMode) -> None:
    """vector/keyword 两种模式必须能取得实现，且声明自己的模式。"""

    retriever = get_retriever(mode)

    assert isinstance(retriever, BaseRetriever)
    assert retriever.mode is mode


@pytest.mark.parametrize("mode", [RetrievalMode.HYBRID, RetrievalMode.HYBRID_RERANK])
def test_unimplemented_modes_fail_explicitly(mode: RetrievalMode) -> None:
    """HYBRID/HYBRID_RERANK 在 T043/T044 前必须明确失败，不能返回伪造候选。"""

    with pytest.raises(RetrievalModeNotImplementedError) as error:
        get_retriever(mode)

    assert error.value.error_code == RETRIEVAL_MODE_NOT_IMPLEMENTED
    assert error.value.retryable is False
    assert error.value.user_message


def test_top_k_is_validated() -> None:
    """Top-K 必须落在契约允许的范围内。"""

    assert normalize_top_k() == DEFAULT_TOP_K
    assert normalize_top_k(3) == 3
    assert normalize_top_k(MAX_TOP_K) == MAX_TOP_K
    for invalid in (0, -1, MAX_TOP_K + 1, True, "3"):
        with pytest.raises(RetrievalInputError):
            normalize_top_k(invalid)  # type: ignore[arg-type]


def test_query_inputs_are_validated_per_mode() -> None:
    """语义检索只接受向量，关键词检索只接受文本。"""

    assert normalize_query_vector([0.1, 0.2]) == [0.1, 0.2]
    assert normalize_query_text("  向量检索  ") == "向量检索"

    for invalid_vector in ("向量", b"bytes", []):
        with pytest.raises(RetrievalInputError):
            normalize_query_vector(invalid_vector)  # type: ignore[arg-type]
    for invalid_text in ("", "   ", None, [0.1]):
        with pytest.raises(RetrievalInputError):
            normalize_query_text(invalid_text)  # type: ignore[arg-type]

    assert RETRIEVAL_INVALID_INPUT in str(RetrievalInputError("检索输入不合法"))


def test_filters_are_normalized_and_described() -> None:
    """过滤条件支持课程/知识库/资料范围，并拒绝非法标识。"""

    filters = resolve_filters(
        {
            "course_ids": [str(COURSE_ID)],
            "knowledge_base_ids": [KNOWLEDGE_BASE_ID],
        }
    )

    assert filters.course_ids == (COURSE_ID,)
    assert filters.knowledge_base_ids == (KNOWLEDGE_BASE_ID,)
    assert filters.document_ids == ()
    assert filters.is_empty is False
    assert "课程范围" in filters.describe()
    assert resolve_filters(None).is_empty is True

    with pytest.raises(RetrievalInputError):
        RetrievalFilters(course_ids=("not-a-uuid",))  # type: ignore[arg-type]
    with pytest.raises(RetrievalInputError):
        resolve_filters({"course_ids": "课程标识"})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# T041 语义检索契约（SQLite 精确基线 + PostgreSQL pgvector 集成）
# ---------------------------------------------------------------------------


def _vector(seed: int, *, dimension: int = EMBEDDING_VECTOR_DIMENSION) -> list[float]:
    """生成确定性向量：主导分量落在 ``seed`` 上，便于断言最近邻归属。"""

    values = [0.05] * dimension
    values[seed % dimension] = 1.0
    values[(seed + 1) % dimension] = 0.4
    return values


def _seed_course(
    session: Session,
    *,
    name: str,
    vectors: Sequence[Sequence[float]],
    contents: Sequence[str] | None = None,
) -> tuple[Course, KnowledgeBase, Document, list[DocumentChunk]]:
    """写入课程、知识库、资料与带向量的知识片段，用于检索断言。"""

    teacher = User(
        username=f"retrieval-{name}",
        email=f"retrieval-{name}@example.com",
        password_hash="hashed-password",
    )
    # 教师角色属于共享基础数据，已存在时直接复用，避免唯一约束冲突。
    existing_role = session.scalar(select(Role).where(Role.name == UserRole.TEACHER))
    teacher.roles.append(existing_role or Role(name=UserRole.TEACHER, description="教师"))
    course = Course(name=f"课程-{name}", creator=teacher)
    knowledge_base = KnowledgeBase(name=f"知识库-{name}", course=course)
    document = Document(
        original_filename=f"{name}.txt",
        file_format="txt",
        course=course,
        knowledge_base=knowledge_base,
        uploader=teacher,
    )
    session.add(document)
    session.commit()

    texts = list(contents or [f"{name} 第 {index} 段内容。" for index in range(len(vectors))])
    chunks = [
        DocumentChunk(
            document_id=document.id,
            course_id=course.id,
            knowledge_base_id=knowledge_base.id,
            chunk_index=index,
            content=texts[index],
            embedding=list(vector),
            chunk_metadata={
                "document_id": str(document.id),
                "course_id": str(course.id),
                "chunk_index": index,
                "location": f"第 {index} 段",
            },
        )
        for index, vector in enumerate(vectors)
    ]
    session.add_all(chunks)
    session.commit()
    return course, knowledge_base, document, chunks


@pytest.fixture
def sqlite_session() -> Generator[Session, None, None]:
    """提供 SQLite 精确基线会话（不需要 PostgreSQL）。"""

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_vector_search_returns_tracked_candidates(sqlite_session: Session) -> None:
    """语义检索结果必须带来源标识、内容、分数与元数据。"""

    _, _, document, chunks = _seed_course(
        sqlite_session,
        name="semantic",
        vectors=[_vector(0), _vector(1), _vector(2)],
    )
    retriever = get_retriever(RetrievalMode.VECTOR_ONLY)

    results = retriever.search(sqlite_session, _vector(1), top_k=2)

    assert len(results) == 2
    assert results[0].chunk_id == str(chunks[1].id)
    assert results[0].semantic_score is not None
    assert results[0].semantic_score > results[1].semantic_score
    expected_index = {str(chunk.id): chunk.chunk_index for chunk in chunks}
    for rank, item in enumerate(results):
        assert item.rank == rank
        assert item.document_id == str(document.id)
        assert item.course_id == str(chunks[0].course_id)
        assert item.metadata["chunk_index"] == expected_index[item.chunk_id]
        assert item.content.strip()


def test_vector_search_empty_context_returns_empty_list(sqlite_session: Session) -> None:
    """没有匹配范围时必须返回空列表，而不是抛异常或返回伪造片段。"""

    _seed_course(sqlite_session, name="empty", vectors=[_vector(0)])
    retriever = get_retriever(RetrievalMode.VECTOR_ONLY)

    results = retriever.search(
        sqlite_session,
        _vector(0),
        filters=RetrievalFilters(course_ids=(uuid4(),)),
    )

    assert results == []


def test_vector_search_rejects_text_query(sqlite_session: Session) -> None:
    """语义检索只接受向量；文本查询必须明确失败。"""

    retriever = get_retriever(RetrievalMode.VECTOR_ONLY)

    with pytest.raises(RetrievalInputError):
        retriever.search(sqlite_session, "什么是检索增强生成？")


def test_vector_search_filter_scope_limits_results(sqlite_session: Session) -> None:
    """课程、知识库与资料过滤必须真正收窄检索范围。"""

    _, first_base, first_document, first_chunks = _seed_course(
        sqlite_session, name="scope-a", vectors=[_vector(0), _vector(1)]
    )
    _seed_course(sqlite_session, name="scope-b", vectors=[_vector(0)])
    retriever = get_retriever(RetrievalMode.VECTOR_ONLY)

    by_knowledge_base = retriever.search(
        sqlite_session,
        _vector(0),
        top_k=MAX_TOP_K,
        filters=RetrievalFilters(knowledge_base_ids=(first_base.id,)),
    )
    by_document = retriever.search(
        sqlite_session,
        _vector(0),
        top_k=MAX_TOP_K,
        filters=RetrievalFilters(document_ids=(first_document.id,)),
    )

    assert len(by_knowledge_base) == len(first_chunks)
    assert len(by_document) == len(first_chunks)
    assert {item.chunk_id for item in by_knowledge_base} == {
        str(chunk.id) for chunk in first_chunks
    }


def test_vector_search_scores_are_sorted_and_bounded(sqlite_session: Session) -> None:
    """余弦相似度必须降序且在合法区间内。"""

    chunks = _seed_course(
        sqlite_session, name="scores", vectors=[_vector(0), _vector(1), _vector(2)]
    )[3]
    retriever = get_retriever(RetrievalMode.VECTOR_ONLY)

    results = retriever.search(sqlite_session, chunks[2].embedding or [], top_k=3)

    scores = [item.semantic_score or 0.0 for item in results]
    assert scores == sorted(scores, reverse=True)
    assert all(-1.0 <= score <= 1.0 for score in scores)
    assert scores[0] == pytest.approx(1.0, abs=1e-6)
    assert results[0].chunk_id == str(chunks[2].id)


@pytest.fixture
def postgres_session() -> Generator[Session, None, None]:
    """提供带种子数据的真实 PostgreSQL 会话；数据库不可用时跳过。"""

    engine = create_database_engine(get_settings(), connect_timeout=3)
    session: Session | None = None
    try:
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
                tables = set(inspect(connection).get_table_names())
        except Exception as exc:  # noqa: BLE001  # 环境不可用时跳过而不是伪造通过
            pytest.skip(f"PostgreSQL 不可用，跳过检索集成契约测试：{type(exc).__name__}")
        if "document_chunks" not in tables:
            pytest.skip("document_chunks 表不存在，请先执行 alembic upgrade head。")

        session = Session(bind=engine, expire_on_commit=False)
        unique = uuid4().hex[:8]
        course, knowledge_base, document, chunks = _seed_course(
            session,
            name=f"pg-{unique}",
            vectors=[_vector(0), _vector(1), _vector(2), _vector(3)],
            contents=[
                "向量检索使用余弦距离衡量语义相似度。",
                "关键词检索使用 tsvector 与 GIN 索引。",
                "混合检索融合语义与关键词候选。",
                "重排对候选进行精排并保留来源。",
            ],
        )
        session.info["retrieval_seed"] = {
            "user_id": document.uploaded_by,
            "course_id": course.id,
            "knowledge_base_id": knowledge_base.id,
            "document_id": document.id,
            "chunk_ids": [chunk.id for chunk in chunks],
        }
        yield session
    finally:
        if session is not None:
            seed = session.info.get("retrieval_seed") or {}
            try:
                # 删除课程即可由 ORM 级联清理知识库、资料与知识片段。
                course = session.get(Course, seed.get("course_id"))
                if course is not None:
                    session.delete(course)
                teacher = session.get(User, seed.get("user_id"))
                if teacher is not None:
                    session.delete(teacher)
                session.commit()
            finally:
                session.close()
        engine.dispose()


def test_postgres_vector_search_matches_exact_baseline(postgres_session: Session) -> None:
    """HNSW 近似结果与精确近邻基线在种子数据上必须给出同一最近邻。"""

    chunk_ids = postgres_session.info["retrieval_seed"]["chunk_ids"]
    approximate = get_retriever(RetrievalMode.VECTOR_ONLY)
    exact = get_retriever(RetrievalMode.VECTOR_ONLY, exact=True)

    for index in range(len(chunk_ids)):
        query = _vector(index)
        approximate_hits = approximate.search(postgres_session, query, top_k=2)
        exact_hits = exact.search(postgres_session, query, top_k=2)

        assert approximate_hits, "语义检索应返回候选"
        assert exact_hits, "精确基线应返回候选"
        assert approximate_hits[0].chunk_id == exact_hits[0].chunk_id
        assert approximate_hits[0].chunk_id == str(chunk_ids[index])
        assert approximate_hits[0].semantic_score == pytest.approx(1.0, abs=1e-6)


def test_postgres_vector_search_keeps_sources_and_filters(postgres_session: Session) -> None:
    """PostgreSQL 路径同样保留来源字段，并支持知识库范围过滤。"""

    seed = postgres_session.info["retrieval_seed"]
    retriever = get_retriever(RetrievalMode.VECTOR_ONLY)

    scoped = retriever.search(
        postgres_session,
        _vector(0),
        top_k=MAX_TOP_K,
        filters=RetrievalFilters(knowledge_base_ids=(seed["knowledge_base_id"],)),
    )
    outside = retriever.search(
        postgres_session,
        _vector(0),
        filters=RetrievalFilters(knowledge_base_ids=(uuid4(),)),
    )

    assert len(scoped) == len(seed["chunk_ids"])
    assert all(
        item.document_id == str(seed["document_id"]) for item in scoped
    )
    assert all(item.course_id == str(seed["course_id"]) for item in scoped)
    assert outside == []
