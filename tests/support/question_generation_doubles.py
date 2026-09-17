"""T067 Question Agent 测试替身：只替换外部调用（检索、Embedding、LLM Provider）。

任务编号：T067（实现见 `backend/app/ai/agents/question_agent.py`）。

替身不做业务判断：`StubRetriever` 只返回固定候选，`StubEmbeddingProvider` 只返回固定向量，
`StubQuestionProvider` 只返回给定的结构化 Payload 并记录调用参数，便于断言“LLM 调用只经
`generate_structured`、Schema 为 `QuestionGenerationPayload`”。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from backend.app.ai.agents.question_agent import QuestionGenerationPayload
from backend.app.ai.retrieval.base import (
    DEFAULT_TOP_K,
    RetrievalFilters,
    RetrievedChunk,
)
from backend.app.domain.enums import QuestionType
from backend.app.schemas.ai import QuestionCandidate

#: 测试用课程标识；检索过滤条件要求合法 UUID。
COURSE_UUID = "11111111-1111-1111-1111-111111111111"
#: 测试用文档标识。
DOCUMENT_UUID = "22222222-2222-2222-2222-222222222222"


def make_chunk(
    chunk_id: str = "chunk-1",
    *,
    content: str = "变量用于保存数据，并可在后续语句中引用。",
    course_id: str = COURSE_UUID,
    document_id: str = DOCUMENT_UUID,
    rank: int = 1,
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


def make_candidate(**overrides: Any) -> QuestionCandidate:
    """构造一道单选题候选；默认参考答案为列表选项的全文。"""

    payload: dict[str, Any] = {
        "question_type": QuestionType.SINGLE_CHOICE,
        "content": "下列哪项正确描述变量的作用？",
        "options": ["变量用于保存数据。", "变量不能被任何语句引用。"],
        "reference_answer": "变量用于保存数据。",
        "scoring_rubric": "选择正确选项得 2 分。",
        "difficulty": "中等",
        "knowledge_points": ["变量"],
        "score": 2.0,
        "source_context_ids": ["chunk-1"],
    }
    payload.update(overrides)
    return QuestionCandidate(**payload)


class StubEmbeddingProvider:
    """返回固定向量的 Embedding 替身；记录被嵌入的查询文本。"""

    provider_name = "stub-embedding"

    def __init__(self, vector: Sequence[float] | None = None) -> None:
        self.vector = list(vector or (0.1, 0.2, 0.3))
        self.queries: list[str] = []

    async def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return list(self.vector)

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [list(self.vector) for _ in texts]


class StubRetriever:
    """返回固定候选的检索替身；记录检索入参供断言使用。"""

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


class StubQuestionProvider:
    """返回固定结构化出题 Payload 的 Provider 替身。"""

    provider_name = "stub-question"

    def __init__(
        self,
        *,
        candidates: Sequence[QuestionCandidate] | None = None,
        payload: Any | None = None,
        error: Exception | None = None,
        model_name: str | None = None,
    ) -> None:
        self.candidates = list(candidates or ())
        self.payload = payload
        self.error = error
        if model_name is not None:
            self.model_name = model_name
        self.calls: list[dict[str, Any]] = []

    async def generate_structured(
        self,
        messages: Sequence[Mapping[str, Any]],
        schema: type[QuestionGenerationPayload],
        model: str | None = None,
    ) -> QuestionGenerationPayload:
        self.calls.append(
            {"messages": list(messages), "schema": schema, "model": model}
        )
        if self.error is not None:
            raise self.error
        if self.payload is not None:
            return self.payload
        return schema(candidates=list(self.candidates))


__all__ = [
    "COURSE_UUID",
    "DOCUMENT_UUID",
    "StubEmbeddingProvider",
    "StubQuestionProvider",
    "StubRetriever",
    "make_candidate",
    "make_chunk",
]
