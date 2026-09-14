"""T044 Rerank 适配器单元测试：契约、两条路线与未就绪语义。

LLM 路线使用替身 Provider，Cross Encoder 路线使用注入的假模型加载器；两者都不访问
网络、不下载模型，因此可以稳定纳入普通回归。

TCR（2026-09-14，H01）：原同步测试无法暴露已有事件循环中的崩溃，新增异步调用、
同步误用、超时、取消和既有同步实现兼容测试。沿用 pytest 和 asyncio.run 驱动真实
事件循环，不新增测试依赖、不修改 Cross Encoder 实现或放宽既有断言。
先运行新增用例确认旧实现缺少异步入口，再验证修复。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace

import pytest
from pydantic import BaseModel

from backend.app.ai.llm.base import BaseLLMProvider, LLMMessage
from backend.app.ai.llm.factory import LLMProviderError
from backend.app.ai.retrieval import reranker as reranker_module
from backend.app.ai.retrieval.base import (
    DEFAULT_TOP_K,
    RetrievalInputError,
    RetrievalMode,
    RetrievalQuery,
    RetrievedChunk,
)
from backend.app.ai.retrieval.reranker import (
    RERANK_FAILED,
    RERANK_PROVIDER_NOT_READY,
    BaseReranker,
    CrossEncoderRerankAdapter,
    HybridRerankRetriever,
    LLMRerankAdapter,
    LLMRerankItem,
    LLMRerankResponse,
    RerankFailedError,
    RerankInputError,
    RerankProviderNotReadyError,
    build_reranker,
    sentence_transformers_available,
)

QUERY_VECTOR = (0.1, 0.2, 0.3)


def _candidate(chunk_id: str, *, content: str | None = None) -> RetrievedChunk:
    """构造带来源元数据的候选替身。"""

    return RetrievedChunk(
        chunk_id=chunk_id,
        course_id="course-1",
        document_id="document-1",
        content=content or f"{chunk_id} 的课程片段内容。",
        metadata={"chunk_index": 0, "location": "第 1 段"},
        semantic_score=0.5,
        fusion_score=0.4,
    )


class StubLLMProvider(BaseLLMProvider):
    """可控的 LLM 替身：返回预设结构化结果、抛错或延迟。"""

    provider_name = "stub-llm"

    def __init__(
        self,
        *,
        response: BaseModel | None = None,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.calls: list[dict[str, object]] = []
        self._response = response
        self._error = error
        self._delay = delay

    async def generate_structured(
        self,
        messages: Sequence[LLMMessage],
        schema: type[BaseModel],
        model: str | None = None,
    ) -> BaseModel:
        self.calls.append({"messages": list(messages), "schema": schema, "model": model})
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response

    @property
    def prompt(self) -> str:
        """返回最后一次调用的提示文本，便于断言候选截断。"""

        messages = self.calls[-1]["messages"]
        assert isinstance(messages, list)
        return "\n".join(str(message.get("content", "")) for message in messages)


class StubCrossEncoder:
    """假 Cross Encoder：按候选顺序返回预设 logit。"""

    def __init__(self, logits: Sequence[float]) -> None:
        self.logits = list(logits)
        self.pairs: list[list[tuple[str, str]]] = []

    def predict(self, pairs: Sequence[tuple[str, str]]) -> list[float]:
        self.pairs.append(list(pairs))
        return self.logits


class StubReranker(BaseReranker):
    """按给定顺序重排的替身 Rerank 实现。"""

    provider_name = "stub"
    model_name = "stub-rerank-v1"

    def __init__(self, *, reverse: bool = False, error: Exception | None = None) -> None:
        self.calls: list[str] = []
        self._reverse = reverse
        self._error = error

    def rerank(
        self,
        query: str,
        candidates: Sequence[RetrievedChunk],
        top_k: int = DEFAULT_TOP_K,
    ) -> list[RetrievedChunk]:
        self.calls.append(query)
        if self._error is not None:
            raise self._error
        ordered = list(reversed(candidates)) if self._reverse else list(candidates)
        rescored = [
            replace(candidate, rerank_score=1.0 - index / 10)
            for index, candidate in enumerate(ordered)
        ]
        return [
            replace(candidate, rank=rank)
            for rank, candidate in enumerate(rescored[:top_k])
        ]


def _llm_response(scores: dict[str, float]) -> LLMRerankResponse:
    """构造 LLM 重排响应。"""

    return LLMRerankResponse(
        rankings=[
            LLMRerankItem(chunk_id=chunk_id, score=score)
            for chunk_id, score in scores.items()
        ]
    )


def test_llm_rerank_orders_candidates_and_keeps_sources() -> None:
    """重排按分数降序写入 rerank_score，且不修改内容与来源。"""

    provider = StubLLMProvider(
        response=_llm_response({"chunk-a": 0.2, "chunk-b": 0.9, "chunk-c": 0.5})
    )
    adapter = LLMRerankAdapter(provider=provider, model="stub-model")

    results = adapter.rerank(
        "余弦距离",
        [_candidate("chunk-a"), _candidate("chunk-b"), _candidate("chunk-c")],
        top_k=3,
    )

    assert [item.chunk_id for item in results] == ["chunk-b", "chunk-c", "chunk-a"]
    assert [item.rank for item in results] == [0, 1, 2]
    assert results[0].rerank_score == pytest.approx(0.9)
    assert results[0].content == "chunk-b 的课程片段内容。"
    assert results[0].metadata["location"] == "第 1 段"
    assert results[0].course_id == "course-1"
    # 融合分数保持不变，供 Benchmark 追踪两阶段分数。
    assert results[0].fusion_score == pytest.approx(0.4)
    assert provider.calls[0]["model"] == "stub-model"
    assert provider.calls[0]["schema"] is LLMRerankResponse


def test_llm_rerank_caps_candidates_for_cost_control() -> None:
    """单次重排候选数量受上限约束，提示中只包含被选中的候选。"""

    candidates = [_candidate(f"chunk-{index}") for index in range(30)]
    scores = {candidate.chunk_id: 0.5 for candidate in candidates[:5]}
    provider = StubLLMProvider(response=_llm_response(scores))
    adapter = LLMRerankAdapter(provider=provider, max_candidates=5)

    results = adapter.rerank("查询", candidates, top_k=3)

    assert provider.prompt.count("chunk_id=") == 5
    assert len(results) == 3
    assert {item.chunk_id for item in results} <= {
        candidate.chunk_id for candidate in candidates[:5]
    }
    assert adapter.describe()["max_candidates"] == 5


def test_llm_rerank_rejects_unknown_chunk_id() -> None:
    """LLM 返回候选之外的 chunk_id 视为重排失败。"""

    provider = StubLLMProvider(
        response=_llm_response({"chunk-a": 0.5, "chunk-unknown": 0.9})
    )
    adapter = LLMRerankAdapter(provider=provider)

    with pytest.raises(RerankFailedError) as error:
        adapter.rerank("查询", [_candidate("chunk-a")], top_k=1)

    assert error.value.error_code == RERANK_FAILED
    assert "chunk-unknown" in str(error.value)


def test_llm_rerank_keeps_unreturned_candidates_with_zero_score() -> None:
    """未被 LLM 返回的候选记 0 分并排在末尾，而不是丢弃来源。"""

    provider = StubLLMProvider(response=_llm_response({"chunk-b": 0.8}))
    adapter = LLMRerankAdapter(provider=provider)

    results = adapter.rerank(
        "查询",
        [_candidate("chunk-a"), _candidate("chunk-b")],
        top_k=2,
    )

    assert [item.chunk_id for item in results] == ["chunk-b", "chunk-a"]
    assert results[1].rerank_score == pytest.approx(0.0)


def test_llm_rerank_reports_timeout() -> None:
    """LLM 超时收敛为可识别的重排失败，并保留可重试语义。"""

    provider = StubLLMProvider(
        response=_llm_response({"chunk-a": 0.5}),
        delay=0.5,
    )
    adapter = LLMRerankAdapter(provider=provider, timeout=0.01)

    with pytest.raises(RerankFailedError) as error:
        adapter.rerank("查询", [_candidate("chunk-a")], top_k=1)

    assert error.value.error_code == RERANK_FAILED
    assert error.value.retryable is True
    assert "超时" in str(error.value)


def test_llm_rerank_maps_provider_error() -> None:
    """Provider 错误统一收敛为 RerankFailedError，不泄露底层细节。"""

    provider = StubLLMProvider(error=LLMProviderError("provider 不可用"))
    adapter = LLMRerankAdapter(provider=provider)

    with pytest.raises(RerankFailedError):
        adapter.rerank("查询", [_candidate("chunk-a")], top_k=1)


def test_llm_rerank_async_and_sync_entrypoint_inside_running_event_loop() -> None:
    """已有事件循环时异步入口可用，同步兼容入口给出明确错误。"""

    async def exercise() -> None:
        provider = StubLLMProvider(response=_llm_response({"chunk-a": 0.8}))
        adapter = LLMRerankAdapter(provider=provider)

        results = await adapter.rerank_async("查询", [_candidate("chunk-a")], top_k=1)

        assert [item.chunk_id for item in results] == ["chunk-a"]
        assert results[0].rerank_score == pytest.approx(0.8)
        with pytest.raises(RerankInputError, match="事件循环"):
            adapter.rerank("查询", [_candidate("chunk-a")], top_k=1)

    asyncio.run(exercise())


def test_llm_rerank_requires_ready_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM Provider 未就绪时抛 RERANK_PROVIDER_NOT_READY，不静默降级。"""

    def _raise() -> BaseLLMProvider:
        raise RuntimeError("provider 未配置")

    monkeypatch.setattr(reranker_module, "create_llm_provider", _raise)

    with pytest.raises(RerankProviderNotReadyError) as error:
        LLMRerankAdapter()

    assert error.value.error_code == RERANK_PROVIDER_NOT_READY
    assert error.value.retryable is False
    assert error.value.user_message


def test_cross_encoder_requires_optional_dependency() -> None:
    """未安装 sentence-transformers 时必须在初始化阶段明确报未就绪。"""

    if sentence_transformers_available():
        pytest.skip("本地已安装 sentence-transformers，跳过缺依赖断言。")

    with pytest.raises(RerankProviderNotReadyError) as error:
        CrossEncoderRerankAdapter()

    assert error.value.error_code == RERANK_PROVIDER_NOT_READY
    assert "eduagent[rerank]" in str(error.value)


def test_cross_encoder_scores_with_injected_model_loader() -> None:
    """注入假加载器时 Cross Encoder 路线可验证打分与排序。"""

    model = StubCrossEncoder([0.0, 2.0])
    adapter = CrossEncoderRerankAdapter(
        model="stub-cross-encoder",
        model_loader=lambda _name: model,
        max_candidates=10,
    )

    results = adapter.rerank(
        "查询",
        [_candidate("chunk-a"), _candidate("chunk-b")],
        top_k=2,
    )

    assert [item.chunk_id for item in results] == ["chunk-b", "chunk-a"]
    assert 0.0 < (results[0].rerank_score or 0.0) <= 1.0
    assert results[0].rerank_score == pytest.approx(0.880797, abs=1e-5)
    assert model.pairs[0] == [
        ("查询", "chunk-a 的课程片段内容。"),
        ("查询", "chunk-b 的课程片段内容。"),
    ]
    assert adapter.describe()["provider"] == "cross_encoder"


def test_cross_encoder_maps_prediction_failure() -> None:
    """Cross Encoder 计算异常收敛为 RerankFailedError。"""

    class _BrokenModel:
        def predict(self, pairs: Sequence[tuple[str, str]]) -> list[float]:
            raise RuntimeError("模型推理失败")

    adapter = CrossEncoderRerankAdapter(
        model="stub",
        model_loader=lambda _name: _BrokenModel(),
    )

    with pytest.raises(RerankFailedError):
        adapter.rerank("查询", [_candidate("chunk-a")], top_k=1)


def test_build_reranker_selects_route_and_rejects_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工厂按 RERANK_PROVIDER 选择路线；none 与未知取值明确失败。"""

    adapter = build_reranker("llm", provider=StubLLMProvider(response=_llm_response({})))
    assert isinstance(adapter, LLMRerankAdapter)

    cross_encoder = build_reranker(
        "cross_encoder",
        model_loader=lambda _name: StubCrossEncoder([0.5]),
    )
    assert isinstance(cross_encoder, CrossEncoderRerankAdapter)

    with pytest.raises(RerankProviderNotReadyError):
        build_reranker("none")
    with pytest.raises(RerankInputError):
        build_reranker("mystery")

    class _StubSettings:
        rerank_provider = "mystery"

    monkeypatch.setattr(reranker_module, "get_settings", lambda: _StubSettings())
    with pytest.raises(RerankInputError):
        build_reranker()


def test_hybrid_rerank_returns_empty_context_without_calling_reranker() -> None:
    """Hybrid 融合为空时直接返回空列表，不调用 Rerank。"""

    class _EmptyHybrid:
        def search(self, session: object, query: object, **kwargs: object) -> list[RetrievedChunk]:
            return []

    reranker = StubReranker()
    retriever = HybridRerankRetriever(reranker=reranker, hybrid_retriever=_EmptyHybrid())  # type: ignore[arg-type]

    assert retriever.search(None, RetrievalQuery("查询", QUERY_VECTOR), top_k=3) == []
    assert reranker.calls == []
    assert retriever.mode is RetrievalMode.HYBRID_RERANK


def test_hybrid_rerank_applies_reranker_to_fused_candidates() -> None:
    """重排作用在融合结果上，并保留融合分数用于追踪。"""

    fused = [_candidate("chunk-a"), _candidate("chunk-b")]

    class _FixedHybrid:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] = {}

        def search(self, session: object, query: object, **kwargs: object) -> list[RetrievedChunk]:
            self.kwargs = dict(kwargs)
            return fused

    hybrid = _FixedHybrid()
    reranker = StubReranker(reverse=True)
    retriever = HybridRerankRetriever(
        reranker=reranker,
        hybrid_retriever=hybrid,  # type: ignore[arg-type]
        fusion_top_k=20,
    )

    results = retriever.search(None, RetrievalQuery("余弦距离", QUERY_VECTOR), top_k=2)

    assert [item.chunk_id for item in results] == ["chunk-b", "chunk-a"]
    assert reranker.calls == ["余弦距离"]
    assert hybrid.kwargs["top_k"] == 20
    assert all(item.rerank_score is not None for item in results)
    assert all(item.fusion_score == pytest.approx(0.4) for item in results)


def test_hybrid_rerank_requires_retrieval_query() -> None:
    """缺少查询文本或向量时明确失败。"""

    retriever = HybridRerankRetriever(reranker=StubReranker())

    with pytest.raises(RetrievalInputError):
        retriever.search(None, QUERY_VECTOR)


def test_hybrid_rerank_reports_not_ready_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未配置可用 reranker 时 Hybrid+Rerank 明确失败，不返回未重排结果。"""

    def _raise(**kwargs: object) -> BaseReranker:
        raise RerankProviderNotReadyError("RERANK_PROVIDER=none 表示未启用重排。")

    monkeypatch.setattr(reranker_module, "build_reranker", _raise)

    class _FixedHybrid:
        def search(self, session: object, query: object, **kwargs: object) -> list[RetrievedChunk]:
            return [_candidate("chunk-a")]

    retriever = HybridRerankRetriever(hybrid_retriever=_FixedHybrid())  # type: ignore[arg-type]

    with pytest.raises(RerankProviderNotReadyError) as error:
        retriever.search(None, RetrievalQuery("查询", QUERY_VECTOR), top_k=1)

    assert error.value.error_code == RERANK_PROVIDER_NOT_READY


def test_rerank_inputs_are_validated() -> None:
    """空查询、空候选与非法候选类型必须明确失败。"""

    adapter = LLMRerankAdapter(
        provider=StubLLMProvider(response=_llm_response({"chunk-a": 0.5}))
    )

    assert adapter.rerank("查询", [], top_k=3) == []
    with pytest.raises(RetrievalInputError):
        adapter.rerank("   ", [_candidate("chunk-a")], top_k=1)
    with pytest.raises(RerankInputError):
        adapter.rerank("查询", "不是候选序列", top_k=1)  # type: ignore[arg-type]


def test_llm_rerank_async_keeps_timeout_and_provider_error_codes() -> None:
    """异步入口保持超时与 Provider 失败的既有错误码。"""

    async def exercise() -> None:
        for provider in (
            StubLLMProvider(response=_llm_response({}), delay=1.0),
            StubLLMProvider(error=LLMProviderError("调用失败")),
        ):
            adapter = LLMRerankAdapter(provider=provider, timeout=0.01)
            with pytest.raises(RerankFailedError) as error:
                await adapter.rerank_async("查询", [_candidate("chunk-a")])
            assert error.value.error_code == RERANK_FAILED
            assert error.value.retryable is True
            assert error.value.__cause__ is not None

    asyncio.run(exercise())


def test_llm_rerank_async_propagates_cancellation_to_provider() -> None:
    """取消重排会取消同一事件循环中的 Provider，不遗留后台调用。"""

    async def exercise() -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()
        loop = asyncio.get_running_loop()

        class WaitingProvider(StubLLMProvider):
            async def generate_structured(self, messages, schema, model=None):
                assert asyncio.get_running_loop() is loop
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        adapter = LLMRerankAdapter(provider=WaitingProvider())
        task = asyncio.create_task(adapter.rerank_async("查询", [_candidate("chunk-a")]))
        await asyncio.wait_for(started.wait(), timeout=1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()

    asyncio.run(exercise())


def test_sync_reranker_inherits_async_compatibility() -> None:
    """只实现同步契约的既有适配器仍可通过基类异步入口调用。"""

    reranker = StubReranker(reverse=True)
    candidates = [_candidate("chunk-a"), _candidate("chunk-b")]
    results = asyncio.run(reranker.rerank_async("查询", candidates, top_k=1))
    assert [item.chunk_id for item in results] == ["chunk-b"]
