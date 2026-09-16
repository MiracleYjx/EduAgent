"""T056/T058 主观题评分链路替身：检索候选、Embedding、重排与评分 Provider。

替身只替换**外部依赖**（检索候选来源、Embedding、LLM Provider），应用装配、评分管道、
置信度策略、汇总与持久化仍使用真实实现，避免用假服务证明生产链路已接通。

替身不做业务判断：``StubRetriever`` 只返回固定候选，``StubReranker`` 只按输入顺序截断，
``StubScoringProvider`` 只返回固定结构化 Payload 并记录调用。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from backend.app.ai.retrieval.base import (
    DEFAULT_TOP_K,
    RetrievalFilters,
    RetrievedChunk,
)
from backend.app.services.grading.subjective_grader import SubjectiveGradingPayload


def make_chunk(
    chunk_id: str = "chunk-1",
    *,
    content: str = "变量用于保存数据，并可在后续语句中引用。",
    course_id: str = "course-1",
    document_id: str = "document-1",
    rank: int = 0,
    semantic_score: float = 0.9,
) -> RetrievedChunk:
    """构造一条检索候选。"""

    return RetrievedChunk(
        chunk_id=chunk_id,
        course_id=course_id,
        document_id=document_id,
        content=content,
        rank=rank,
        semantic_score=semantic_score,
    )


class StubEmbeddingProvider:
    """返回固定向量的 Embedding 替身；记录被嵌入的查询文本。"""

    def __init__(self, vector: Sequence[float] | None = None) -> None:
        self.vector = list(vector or (0.1, 0.2, 0.3))
        self.queries: list[str] = []

    async def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return list(self.vector)

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [list(self.vector) for _ in texts]


class StubRetriever:
    """返回固定候选的检索替身；记录检索入参供调用次数断言。"""

    def __init__(self, chunks: Sequence[RetrievedChunk] = ()) -> None:
        self.chunks = list(chunks)
        self.calls: list[dict[str, Any]] = []

    def search(
        self,
        session: Any,
        query: Any,
        *,
        top_k: int = DEFAULT_TOP_K,
        filters: RetrievalFilters | None = None,
    ) -> list[RetrievedChunk]:
        self.calls.append({"query": query, "top_k": top_k, "filters": filters})
        return list(self.chunks)


class StubReranker:
    """按输入顺序截断的重排替身；不改变候选顺序。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def rerank_async(
        self,
        query: str,
        candidates: Sequence[RetrievedChunk],
        top_k: int,
    ) -> list[RetrievedChunk]:
        self.calls.append((query, top_k))
        return list(candidates)[:top_k]


class StubScoringProvider:
    """返回固定结构化评分 Payload 的 Provider 替身。"""

    def __init__(
        self,
        *,
        score: float = 6.0,
        confidence: float = 0.9,
        reason: str = "说明了数据保存作用。",
        correct_points: Sequence[str] = ("保存数据",),
        missing_knowledge_points: Sequence[str] = ("引用数据",),
        suggestions: Sequence[str] = ("补充变量引用。",),
        error: Exception | None = None,
    ) -> None:
        self.score = score
        self.confidence = confidence
        self.reason = reason
        self.correct_points = list(correct_points)
        self.missing_knowledge_points = list(missing_knowledge_points)
        self.suggestions = list(suggestions)
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def generate_structured(
        self,
        messages: Sequence[Mapping[str, Any]],
        schema: type[SubjectiveGradingPayload],
        **kwargs: Any,
    ) -> SubjectiveGradingPayload:
        self.calls.append({"messages": list(messages), "schema": schema, "kwargs": kwargs})
        if self.error is not None:
            raise self.error
        return schema(
            score=self.score,
            confidence=self.confidence,
            reason=self.reason,
            correct_points=list(self.correct_points),
            missing_knowledge_points=list(self.missing_knowledge_points),
            suggestions=list(self.suggestions),
        )


__all__ = [
    "StubEmbeddingProvider",
    "StubReranker",
    "StubRetriever",
    "StubScoringProvider",
    "make_chunk",
]
