"""T081 知识工具：从真实 JWT 调用边界验证服务授权与检索结果。"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.app.ai.embedding.base import BaseEmbeddingProvider
from backend.app.ai.retrieval.base import (
    BaseRetriever,
    RetrievalFilters,
    RetrievalMode,
    RetrievedChunk,
)
from backend.app.ai.retrieval.hybrid_search import HybridSearchRetriever
from backend.app.core.database import Base
from backend.app.domain.enums import QuestionStatus, QuestionType, UserRole
from backend.app.mcp.registry import ToolRegistry
from backend.app.mcp.server import MCPToolServer
from backend.app.mcp.tools.knowledge_tools import register_knowledge_tools
from backend.app.models import Role, User
from backend.app.services.auth_service import AuthService
from backend.app.services.course_service import CourseService
from backend.app.services.question_service import QuestionService

SECRET = "test-knowledge-jwt-secret-not-for-production"


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as active:
        yield active
    engine.dispose()


def _teacher(session: Session) -> User:
    identifier = uuid4().hex[:8]
    user = User(
        username=f"knowledge-{identifier}",
        email=f"knowledge-{identifier}@example.com",
        password_hash="unused-test-hash",
    )
    role = session.scalar(select(Role).where(Role.name == UserRole.TEACHER))
    user.roles.append(role or Role(name=UserRole.TEACHER))
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _token(session: Session, user: User) -> str:
    return AuthService(session, secret_key=SECRET).issue_access_token(user)


class StubEmbeddingProvider(BaseEmbeddingProvider):
    provider_name = "stub"

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def embed_documents(self, documents: Sequence[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in documents]

    async def embed_query(self, query: str) -> list[float]:
        self.queries.append(query)
        return [1.0, 0.0]


class FixedBranchRetriever(BaseRetriever):
    mode = RetrievalMode.VECTOR_ONLY

    def __init__(self, hits: list[RetrievedChunk]) -> None:
        self.hits = hits
        self.filters: list[RetrievalFilters | None] = []

    def search(
        self,
        session: Session,
        query: str | Sequence[float],
        *,
        top_k: int = 5,
        filters: RetrievalFilters | None = None,
    ) -> list[RetrievedChunk]:
        self.filters.append(filters)
        return self.hits[:top_k]


def _server(
    provider: StubEmbeddingProvider | None = None,
    *,
    hits: list[RetrievedChunk] | None = None,
) -> tuple[MCPToolServer, StubEmbeddingProvider, FixedBranchRetriever]:
    embedding = provider or StubEmbeddingProvider()
    vector = FixedBranchRetriever(hits or [])
    keyword = FixedBranchRetriever(hits or [])
    hybrid = HybridSearchRetriever(
        vector_weight=0.5,
        vector_retriever=vector,
        keyword_retriever=keyword,
    )
    registry = ToolRegistry()
    register_knowledge_tools(registry, embedding_provider=embedding, retriever=hybrid)
    return MCPToolServer(secret_key=SECRET, registry=registry), embedding, vector


def test_search_questions_returns_authorized_filtered_safe_summaries(
    session: Session,
) -> None:
    teacher = _teacher(session)
    course = CourseService(session).create_course("数学", teacher_id=teacher.id)
    target = QuestionService(session).create_question(
        course.id,
        QuestionType.SHORT_ANSWER,
        "解释二次函数的顶点形式" + "A" * 600,
        difficulty="中",
        knowledge_points=["二次函数"],
        teacher_id=teacher.id,
    )
    QuestionService(session).create_question(
        course.id,
        QuestionType.TRUE_FALSE,
        "二次函数是线性函数",
        knowledge_points=["二次函数"],
        teacher_id=teacher.id,
    )
    QuestionService(session).create_question(
        course.id,
        QuestionType.SHORT_ANSWER,
        "二次函数图像的对称轴",
        knowledge_points=["几何"],
        teacher_id=teacher.id,
    )
    pending = QuestionService(session).create_question(
        course.id,
        QuestionType.SHORT_ANSWER,
        "二次函数的判别式",
        knowledge_points=["二次函数"],
        teacher_id=teacher.id,
    )
    QuestionService(session).update_question_status(
        pending.id, QuestionStatus.PENDING_REVIEW, teacher_id=teacher.id
    )
    server, _, _ = _server()
    result = server.call_tool(
        "search_questions",
        {
            "course_id": course.id,
            "query": "二次函数",
            "question_type": "SHORT_ANSWER",
            "knowledge_point": "二次函数",
            "status": "Draft",
        },
        token=_token(session, teacher),
        session=session,
    )

    assert result.ok and result.data is not None
    assert result.data["count"] == 1
    assert result.data["questions"] == [
        {
            "content": "解释二次函数的顶点形式" + "A" * 289 + "…",
            "type": QuestionType.SHORT_ANSWER.value,
            "difficulty": "中",
            "status": QuestionStatus.DRAFT.value,
            "knowledge_points": ["二次函数"],
        }
    ]
    assert str(course.id) not in result.model_dump_json()
    assert target.id not in result.model_dump_json()


def test_search_questions_rejects_foreign_course_and_reports_empty(
    session: Session,
) -> None:
    owner = _teacher(session)
    other = _teacher(session)
    course = CourseService(session).create_course("仅本人", teacher_id=owner.id)
    server, _, _ = _server()

    denied = server.call_tool(
        "search_questions",
        {"course_id": course.id, "query": "题目"},
        token=_token(session, other),
        session=session,
    )
    empty = server.call_tool(
        "search_questions",
        {"course_id": course.id, "query": "不存在的题目"},
        token=_token(session, owner),
        session=session,
    )

    assert denied.error is not None and denied.error.code == "COURSE_FORBIDDEN"
    assert empty.ok and empty.data == {
        "questions": [],
        "count": 0,
        "message": "未找到符合条件的题目。",
    }


def test_query_knowledge_uses_owned_scope_and_returns_safe_source(
    session: Session,
) -> None:
    teacher = _teacher(session)
    course = CourseService(session).create_course("物理", teacher_id=teacher.id)
    private_id = str(uuid4())
    document_id = str(uuid4())
    hit = RetrievedChunk(
        chunk_id=private_id,
        course_id=course.id,
        document_id=document_id,
        content="牛顿第二定律" + "B" * 600,
        metadata={
            "original_filename": "C:\\private\\mechanics.pdf",
            "chunk_index": 3,
            "document_id": private_id,
            "live_chunk_id": private_id,
            "internal_path": "C:\\private\\secret.txt",
        },
        semantic_score=0.8,
        keyword_score=0.6,
    )
    server, provider, vector = _server(hits=[hit])
    result = server.call_tool(
        "query_knowledge",
        {"course_id": course.id, "query": "牛顿定律", "top_k": 1},
        token=_token(session, teacher),
        session=session,
    )

    assert result.ok and result.data is not None
    assert provider.queries == ["牛顿定律"]
    assert vector.filters == [RetrievalFilters(course_ids=(course.id,))]
    assert result.data["count"] == 1
    assert result.data["chunks"][0] == {
        "content": "牛顿第二定律" + "B" * 494 + "…",
        "source_file": "mechanics.pdf",
        "chunk_index": 3,
        "rank": 0,
        "score": 1.0,
    }
    serialized = result.model_dump_json()
    assert private_id not in serialized
    assert document_id not in serialized
    assert "C:\\private" not in serialized
    assert str(course.id) not in serialized


def test_query_knowledge_rejects_unauthed_and_foreign_before_embedding(
    session: Session,
) -> None:
    owner = _teacher(session)
    other = _teacher(session)
    course = CourseService(session).create_course("私有知识", teacher_id=owner.id)
    server, provider, vector = _server()

    unauthenticated = server.call_tool(
        "query_knowledge",
        {"course_id": course.id, "query": "资料", "top_k": 2},
        token=None,
        session=session,
    )
    denied = server.call_tool(
        "query_knowledge",
        {"course_id": course.id, "query": "资料", "top_k": 2},
        token=_token(session, other),
        session=session,
    )

    assert (
        unauthenticated.error is not None
        and unauthenticated.error.code == "TOOL_UNAUTHENTICATED"
    )
    assert denied.error is not None and denied.error.code == "COURSE_FORBIDDEN"
    assert provider.queries == []
    assert vector.filters == []


def test_query_knowledge_empty_result_is_explicit(session: Session) -> None:
    teacher = _teacher(session)
    course = CourseService(session).create_course("空课程", teacher_id=teacher.id)
    server, _, _ = _server()

    result = server.call_tool(
        "query_knowledge",
        {"course_id": course.id, "query": "缺失", "top_k": 3},
        token=_token(session, teacher),
        session=session,
    )

    assert result.ok and result.data == {
        "chunks": [],
        "count": 0,
        "message": "未检索到相关知识片段。",
    }
