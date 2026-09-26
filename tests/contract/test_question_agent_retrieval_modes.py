"""P4.4：Question Agent 四种检索模式的公开入口契约。"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import pytest

from backend.app.ai.agents import question_agent as question_module
from backend.app.ai.agents.question_agent import (
    QUESTION_INSUFFICIENT_CONTEXT,
    QUESTION_RERANK_FAILED,
    QUESTION_RERANK_PROVIDER_NOT_READY,
    QUESTION_RETRIEVAL_FAILED,
    QUESTION_UNKNOWN_SOURCE_CONTEXT,
    QuestionAgent,
    build_generation_context,
    resolve_generation_retriever,
)
from backend.app.ai.agents.state import (
    AgentInput,
    AgentStatus,
    AgentType,
    QuestionGenerationRequest,
)
from backend.app.ai.retrieval.base import RetrievalMode, RetrievalQuery
from backend.app.ai.retrieval.hybrid_search import HybridSearchRetriever
from backend.app.ai.retrieval.reranker import (
    LLMRerankAdapter,
    LLMRerankResponse,
    RerankProviderNotReadyError,
)
from backend.app.domain.enums import QuestionType
from tests.support.question_generation_doubles import (
    COURSE_UUID,
    StubEmbeddingProvider,
    StubQuestionProvider,
    StubRetriever,
    make_candidate,
    make_chunk,
)
from tests.unit.settings_helpers import build_test_settings


def _input() -> AgentInput:
    return AgentInput(
        agent_type=AgentType.QUESTION,
        request_id="p44-question",
        generation_request=QuestionGenerationRequest(
            course_id=COURSE_UUID,
            knowledge_points=["变量"],
            difficulty="中等",
            question_type=QuestionType.SINGLE_CHOICE,
            count=1,
        ),
    )


class _RerankProvider:
    """使用真实 LLMRerankAdapter 的结构化 Provider 替身，不发网络请求。"""

    def __init__(self, scores: dict[str, float]) -> None:
        self.scores = scores
        self.calls: list[dict[str, Any]] = []

    async def generate_structured(
        self, messages: Any, schema: Any, model: Any = None
    ) -> LLMRerankResponse:
        self.calls.append({"messages": messages, "schema": schema, "model": model})
        return schema(
            rankings=[
                {"chunk_id": chunk_id, "score": score}
                for chunk_id, score in self.scores.items()
            ]
        )


@pytest.mark.parametrize("mode", list(RetrievalMode))
def test_question_agent_four_modes_use_correct_query_and_course_scope(
    mode: RetrievalMode,
) -> None:
    """四条公开路径均从真实 Agent 入口成功生成，检索调用形状各不相同。"""

    provider = StubQuestionProvider(candidates=[make_candidate()])
    embedding = StubEmbeddingProvider()
    retriever = StubRetriever([make_chunk("chunk-1")])
    rerank_provider = _RerankProvider({"chunk-1": 0.9})
    reranker = LLMRerankAdapter(
        provider=rerank_provider, max_candidates=3, timeout=5.0
    )

    output = asyncio.run(
        QuestionAgent(provider=provider).generate(
            None,  # type: ignore[arg-type] - 注入的检索替身不使用数据库
            _input(),
            mode=mode,
            retriever=retriever,
            reranker=reranker,
            embedding_provider=embedding,
            settings=build_test_settings(),
        )
    )

    assert output.status is AgentStatus.SUCCESS
    assert output.retrieved_context_ids == ["chunk-1"]
    assert output.question_candidates[0].source_context_ids == ["chunk-1"]
    assert len(provider.calls) == 1
    assert retriever.calls[0]["filters"].course_ids == (UUID(COURSE_UUID),)
    query = retriever.calls[0]["query"]
    if mode is RetrievalMode.KEYWORD_ONLY:
        assert isinstance(query, str) and "变量" in query
        assert embedding.queries == []
    elif mode is RetrievalMode.VECTOR_ONLY:
        assert query == tuple(embedding.vector)
        assert embedding.queries
    else:
        assert isinstance(query, RetrievalQuery)
        assert query.embedding == tuple(embedding.vector)
        assert embedding.queries == [query.text]
    assert len(rerank_provider.calls) == (1 if mode is RetrievalMode.HYBRID_RERANK else 0)


def test_keyword_only_never_constructs_embedding_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关键词路既不调用也不创建模型；无 Embedding 配置仍可出题。"""

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Keyword Only 不得创建 Embedding Provider")

    monkeypatch.setattr(question_module, "create_embedding_provider", fail)
    monkeypatch.setattr(question_module, "get_embedding_provider", fail)
    output = asyncio.run(
        QuestionAgent(provider=StubQuestionProvider(candidates=[make_candidate()])).generate(
            None,  # type: ignore[arg-type]
            _input(),
            mode="keyword_only",
            retriever=StubRetriever([make_chunk("chunk-1")]),
            settings=build_test_settings(),
        )
    )
    assert output.status is AgentStatus.SUCCESS


def test_hybrid_rerank_uses_hybrid_retriever_with_configured_weight() -> None:
    """不经同步 HybridRerankRetriever.search；使用现有 Hybrid 融合召回。"""

    settings = build_test_settings(hybrid_vector_weight=0.25)
    retriever = resolve_generation_retriever(
        RetrievalMode.HYBRID_RERANK,
        candidate_k=3,
        settings=settings,
    )
    assert isinstance(retriever, HybridSearchRetriever)
    assert retriever.mode is RetrievalMode.HYBRID
    assert retriever.vector_weight == 0.25
    assert retriever.candidate_k == 3


def test_hybrid_rerank_awaits_adapter_and_only_cites_final_context() -> None:
    """在事件循环中融合多个候选，异步重排后只引用写进 Prompt 的片段。"""

    hits = [make_chunk(f"chunk-{index}") for index in range(1, 4)]
    retriever = StubRetriever(hits)
    rerank_provider = _RerankProvider(
        {"chunk-1": 0.1, "chunk-2": 0.2, "chunk-3": 0.95}
    )
    reranker = LLMRerankAdapter(
        provider=rerank_provider, max_candidates=3, timeout=5.0
    )
    request = _input().generation_request
    assert request is not None
    context = asyncio.run(
        build_generation_context(
            None,  # type: ignore[arg-type]
            request,
            mode="hybrid_rerank",
            top_k=1,
            candidate_k=3,
            retriever=retriever,
            reranker=reranker,
            embedding_provider=StubEmbeddingProvider(),
            settings=build_test_settings(),
        )
    )
    assert retriever.calls[0]["top_k"] == 3
    assert isinstance(retriever.calls[0]["query"], RetrievalQuery)
    assert context.retrieved_context_ids == ("chunk-3",)
    assert context.chunks[0].rerank_score == 0.95
    assert "chunk-3" in context.final_context
    assert "chunk-1" not in context.final_context
    assert rerank_provider.calls[0]["schema"] is LLMRerankResponse

    provider = StubQuestionProvider(
        candidates=[make_candidate(source_context_ids=["chunk-3"])]
    )
    output = asyncio.run(
        QuestionAgent(provider=provider).generate(
            None,  # type: ignore[arg-type]
            _input(),
            mode=RetrievalMode.HYBRID_RERANK,
            top_k=1,
            candidate_k=3,
            retriever=StubRetriever(hits),
            reranker=reranker,
            embedding_provider=StubEmbeddingProvider(),
            settings=build_test_settings(),
        )
    )
    assert output.status is AgentStatus.SUCCESS
    assert output.retrieved_context_ids == ["chunk-3"]
    assert output.question_candidates[0].source_context_ids == ["chunk-3"]
    assert "chunk-1" not in str(provider.calls[0]["messages"][1]["content"])

    wrong_source = StubQuestionProvider(
        candidates=[make_candidate(source_context_ids=["chunk-1"])]
    )
    rejected = asyncio.run(
        QuestionAgent(provider=wrong_source).generate(
            None,  # type: ignore[arg-type]
            _input(),
            mode=RetrievalMode.HYBRID_RERANK,
            top_k=1,
            candidate_k=3,
            retriever=StubRetriever(hits),
            reranker=reranker,
            embedding_provider=StubEmbeddingProvider(),
            settings=build_test_settings(),
        )
    )
    assert rejected.status is AgentStatus.FAILURE
    assert rejected.error is not None
    assert rejected.error.error_code == QUESTION_UNKNOWN_SOURCE_CONTEXT


def test_hybrid_rerank_uses_same_settings_for_reranker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未注入重排器时按本次配置解析，不另行读取全局模型设置。"""

    settings = build_test_settings(rerank_provider="llm", rerank_model="p44-model")
    seen: list[Any] = []
    llm = _RerankProvider({"chunk-1": 0.8})

    def build(*, settings: Any) -> LLMRerankAdapter:
        seen.append(settings)
        return LLMRerankAdapter(
            provider=llm, max_candidates=3, timeout=5.0
        )

    monkeypatch.setattr(question_module, "build_reranker", build)
    output = asyncio.run(
        QuestionAgent(provider=StubQuestionProvider(candidates=[make_candidate()])).generate(
            None,  # type: ignore[arg-type]
            _input(),
            mode=RetrievalMode.HYBRID_RERANK,
            retriever=StubRetriever([make_chunk("chunk-1")]),
            embedding_provider=StubEmbeddingProvider(),
            settings=settings,
        )
    )
    assert output.status is AgentStatus.SUCCESS
    assert seen == [settings]
    assert len(llm.calls) == 1


def test_invalid_mode_and_rerank_candidate_count_fail_explicitly() -> None:
    """注入召回器不能绕过非法模式/候选数校验。"""

    for mode, candidate_k in (("unknown", 3), (RetrievalMode.HYBRID_RERANK, 0)):
        provider = StubQuestionProvider(candidates=[make_candidate()])
        output = asyncio.run(
            QuestionAgent(provider=provider).generate(
                None,  # type: ignore[arg-type]
                _input(),
                mode=mode,
                candidate_k=candidate_k,
                retriever=StubRetriever([make_chunk("chunk-1")]),
                embedding_provider=StubEmbeddingProvider(),
                settings=build_test_settings(),
            )
        )
        assert output.status is AgentStatus.FAILURE
        assert output.error is not None
        assert output.error.error_code == QUESTION_RETRIEVAL_FAILED
        assert output.error.source_code == "RETRIEVAL_INVALID_INPUT"
        assert provider.calls == []


def test_hybrid_rerank_empty_context_does_not_load_reranker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有融合候选就维持上下文不足，不加载无用的重排模型或调用出题模型。"""

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("空候选不得初始化重排器")

    monkeypatch.setattr(question_module, "build_reranker", fail)
    provider = StubQuestionProvider(candidates=[make_candidate()])
    output = asyncio.run(
        QuestionAgent(provider=provider).generate(
            None,  # type: ignore[arg-type]
            _input(),
            mode=RetrievalMode.HYBRID_RERANK,
            retriever=StubRetriever([]),
            embedding_provider=StubEmbeddingProvider(),
            settings=build_test_settings(),
        )
    )
    assert output.status is AgentStatus.FAILURE
    assert output.error is not None
    assert output.error.error_code == QUESTION_INSUFFICIENT_CONTEXT
    assert provider.calls == []


def test_hybrid_rerank_unavailable_is_explicit_not_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重排未就绪时不得返回未重排结果，且错误提示不能泄露异常细节。"""

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RerankProviderNotReadyError("private-reranker-detail")

    monkeypatch.setattr(question_module, "build_reranker", fail)
    provider = StubQuestionProvider(candidates=[make_candidate()])
    output = asyncio.run(
        QuestionAgent(provider=provider).generate(
            None,  # type: ignore[arg-type]
            _input(),
            mode=RetrievalMode.HYBRID_RERANK,
            retriever=StubRetriever([make_chunk("chunk-1")]),
            embedding_provider=StubEmbeddingProvider(),
            settings=build_test_settings(),
        )
    )
    assert output.status is AgentStatus.FAILURE
    assert output.error is not None
    assert output.error.error_code == QUESTION_RERANK_PROVIDER_NOT_READY
    assert output.error.source_code == "RERANK_PROVIDER_NOT_READY"
    assert output.error.retryable is False
    assert "private-reranker-detail" not in output.error.message
    assert provider.calls == []


def test_hybrid_rerank_bad_source_fails_without_generation() -> None:
    """重排模型返回未知来源不得污染候选，失败保留来源码与可重试语义。"""

    reranker = LLMRerankAdapter(
        provider=_RerankProvider({"not-a-candidate": 0.99}),
        max_candidates=3,
        timeout=5.0,
    )
    provider = StubQuestionProvider(candidates=[make_candidate()])
    output = asyncio.run(
        QuestionAgent(provider=provider).generate(
            None,  # type: ignore[arg-type]
            _input(),
            mode=RetrievalMode.HYBRID_RERANK,
            retriever=StubRetriever([make_chunk("chunk-1")]),
            reranker=reranker,
            embedding_provider=StubEmbeddingProvider(),
            settings=build_test_settings(),
        )
    )
    assert output.status is AgentStatus.FAILURE
    assert output.error is not None
    assert output.error.error_code == QUESTION_RERANK_FAILED
    assert output.error.source_code == "RERANK_FAILED"
    assert output.error.retryable is True
    assert "not-a-candidate" not in output.error.message
    assert provider.calls == []
