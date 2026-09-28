"""T081 知识查询工具：复用题目服务、课程所有权服务及 M2 Hybrid 检索器。"""

from __future__ import annotations

import asyncio
import ntpath
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.app.ai.embedding.base import BaseEmbeddingProvider, EmbeddingProviderError
from backend.app.ai.embedding.factory import (
    EmbeddingProviderFactoryError,
    get_embedding_provider,
)
from backend.app.ai.retrieval.base import (
    DEFAULT_TOP_K,
    MAX_TOP_K,
    RetrievalError,
    RetrievalFilters,
    RetrievalQuery,
    RetrievedChunk,
)
from backend.app.ai.retrieval.hybrid_search import HybridSearchRetriever
from backend.app.domain.enums import QuestionStatus, QuestionType
from backend.app.domain.permissions import Permission
from backend.app.mcp.registry import (
    ToolContext,
    ToolExecutionError,
    ToolRegistry,
    ToolSpec,
)
from backend.app.services.course_service import (
    CourseNotFoundError,
    CoursePermissionError,
    CourseService,
    CourseServiceError,
)
from backend.app.services.question_service import (
    QuestionNotFoundError,
    QuestionPermissionError,
    QuestionService,
    QuestionServiceError,
    QuestionSummary,
    QuestionValidationError,
)

_QUESTION_EXCERPT_CHARS = 300
_CHUNK_EXCERPT_CHARS = 500


class SearchQuestionsArguments(BaseModel):
    """课程必填；状态交由 QuestionService 筛选，其他筛选作用于授权摘要。"""

    model_config = ConfigDict(extra="forbid")

    course_id: UUID
    query: str = Field(min_length=1, max_length=2000)
    question_type: QuestionType | None = None
    knowledge_point: str | None = Field(default=None, max_length=160)
    status: QuestionStatus | None = None

    @field_validator("query", "knowledge_point")
    @classmethod
    def nonblank_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("查询文本与知识点不能只包含空白字符。")
        return normalized


class QueryKnowledgeArguments(BaseModel):
    """只允许单课程检索；调用者身份来自 JWT，不能作为工具参数提供。"""

    model_config = ConfigDict(extra="forbid")

    course_id: UUID
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=DEFAULT_TOP_K, ge=1, le=MAX_TOP_K)

    @field_validator("query")
    @classmethod
    def nonblank_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("查询文本不能只包含空白字符。")
        return normalized


def _excerpt(content: str, max_chars: int) -> str:
    return content if len(content) <= max_chars else content[:max_chars] + "…"


def _question_matches(
    question: QuestionSummary, args: SearchQuestionsArguments
) -> bool:
    return (
        args.query.casefold() in question.content.casefold()
        and (args.question_type is None or question.type == args.question_type)
        and (
            args.knowledge_point is None
            or any(
                point.casefold() == args.knowledge_point.casefold()
                for point in question.knowledge_points
            )
        )
    )


def _question_result(question: QuestionSummary) -> dict[str, Any]:
    return {
        "content": _excerpt(question.content, _QUESTION_EXCERPT_CHARS),
        "type": question.type.value,
        "difficulty": question.difficulty,
        "status": question.status.value,
        "knowledge_points": question.knowledge_points,
    }


def _chunk_result(chunk: RetrievedChunk) -> dict[str, Any]:
    """投影现有 RetrievedChunk；不透传原始 metadata 与内部主键。"""

    source_file = ntpath.basename(str(chunk.metadata.get("original_filename") or ""))
    index = chunk.metadata.get("chunk_index")
    return {
        "content": _excerpt(chunk.content, _CHUNK_EXCERPT_CHARS),
        "source_file": source_file,
        "chunk_index": index
        if isinstance(index, int) and not isinstance(index, bool)
        else None,
        "rank": chunk.rank,
        "score": chunk.score,
    }


def _search_questions(
    context: ToolContext, args: SearchQuestionsArguments
) -> dict[str, Any]:
    try:
        questions = QuestionService(context.session).list_questions(
            course_id=args.course_id,
            status=args.status,
            teacher_id=context.actor_id,
        )
    except QuestionPermissionError as exc:
        raise ToolExecutionError("COURSE_FORBIDDEN", "无权访问该课程。") from exc
    except QuestionNotFoundError as exc:
        raise ToolExecutionError("COURSE_NOT_FOUND", "课程不存在。") from exc
    except QuestionValidationError as exc:
        raise ToolExecutionError(
            "TOOL_INVALID_ARGUMENTS", "题目查询条件无效。"
        ) from exc
    except QuestionServiceError as exc:
        raise ToolExecutionError("QUESTION_QUERY_FAILED", "题目查询失败。") from exc

    matches = [
        _question_result(item) for item in questions if _question_matches(item, args)
    ]
    return {
        "questions": matches,
        "count": len(matches),
        "message": "未找到符合条件的题目。" if not matches else "",
    }


def _embed_query(provider: BaseEmbeddingProvider, query: str) -> list[float]:
    """T080 是同步边界；同步工作线程内桥接现有异步 Embedding 接口。"""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(provider.embed_query(query))
    raise ToolExecutionError(
        "TOOL_EXECUTION_CONTEXT", "请在同步工作线程中调用知识检索工具。"
    )


def _query_knowledge(
    context: ToolContext,
    args: QueryKnowledgeArguments,
    *,
    embedding_provider: BaseEmbeddingProvider | None,
    retriever: HybridSearchRetriever | None,
) -> dict[str, Any]:
    try:
        CourseService(context.session).get_course(
            args.course_id, teacher_id=context.actor_id
        )
    except CoursePermissionError as exc:
        raise ToolExecutionError("COURSE_FORBIDDEN", "无权访问该课程。") from exc
    except CourseNotFoundError as exc:
        raise ToolExecutionError("COURSE_NOT_FOUND", "课程不存在。") from exc
    except CourseServiceError as exc:
        raise ToolExecutionError("COURSE_QUERY_FAILED", "无法读取课程信息。") from exc

    try:
        provider = embedding_provider or get_embedding_provider()
        embedding = _embed_query(provider, args.query)
        active_retriever = retriever or HybridSearchRetriever()
        chunks = active_retriever.search(
            context.session,
            RetrievalQuery(text=args.query, embedding=tuple(embedding)),
            top_k=args.top_k,
            filters=RetrievalFilters(course_ids=(args.course_id,)),
        )
    except EmbeddingProviderFactoryError as exc:
        raise ToolExecutionError(
            "EMBEDDING_PROVIDER_NOT_READY", "Embedding Provider 未就绪。"
        ) from exc
    except EmbeddingProviderError as exc:
        raise ToolExecutionError(exc.error_code, "查询向量生成失败。") from exc
    except RetrievalError as exc:
        raise ToolExecutionError(exc.error_code, exc.user_message) from exc

    results = [_chunk_result(chunk) for chunk in chunks]
    return {
        "chunks": results,
        "count": len(results),
        "message": "未检索到相关知识片段。" if not results else "",
    }


def register_knowledge_tools(
    registry: ToolRegistry,
    *,
    embedding_provider: BaseEmbeddingProvider | None = None,
    retriever: HybridSearchRetriever | None = None,
) -> None:
    """注册只读工具；替身仅用于测试，生产时按调用期解析真实组件。"""

    registry.register(
        ToolSpec(
            name="search_questions",
            description="按课程与筛选条件查询当前教师有权访问的题目摘要。",
            permission=Permission.MANAGE_QUESTION_BANK,
            arguments_model=SearchQuestionsArguments,
            handler=_search_questions,
        )
    )
    registry.register(
        ToolSpec(
            name="query_knowledge",
            description="在当前教师拥有的课程中执行 Hybrid 知识片段检索。",
            permission=Permission.MANAGE_KNOWLEDGE_BASES,
            arguments_model=QueryKnowledgeArguments,
            handler=lambda context, args: _query_knowledge(
                context,
                args,
                embedding_provider=embedding_provider,
                retriever=retriever,
            ),
        )
    )


__all__ = [
    "QueryKnowledgeArguments",
    "SearchQuestionsArguments",
    "register_knowledge_tools",
]
