"""T043 Hybrid 检索单元测试：归一化、加权融合、去重与来源标记。

两路召回使用注入的替身实现，因此测试不需要 PostgreSQL、Embedding 或真实模型，只验证
融合公式与去重/排序/来源标记等纯逻辑。
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from backend.app.ai.retrieval import hybrid_search as hybrid_module
from backend.app.ai.retrieval.base import (
    DEFAULT_TOP_K,
    SOURCE_MODE_BOTH,
    SOURCE_MODE_KEYWORD,
    SOURCE_MODE_VECTOR,
    BaseRetriever,
    RetrievalFilters,
    RetrievalInputError,
    RetrievalMode,
    RetrievalQuery,
    RetrievalUnsupportedDialectError,
    RetrievedChunk,
)
from backend.app.ai.retrieval.hybrid_search import (
    DEFAULT_CANDIDATE_K,
    HybridSearchRetriever,
    min_max_normalize,
    resolve_vector_weight,
)

QUERY_VECTOR = (0.1, 0.2, 0.3)


def _candidate(
    chunk_id: str,
    *,
    semantic_score: float | None = None,
    keyword_score: float | None = None,
    content: str | None = None,
) -> RetrievedChunk:
    """构造带来源信息的检索候选替身。"""

    return RetrievedChunk(
        chunk_id=chunk_id,
        course_id="course-1",
        document_id="document-1",
        content=content or f"{chunk_id} 的课程片段内容。",
        metadata={"chunk_index": 0, "location": "第 1 段"},
        semantic_score=semantic_score,
        keyword_score=keyword_score,
    )


class StubRetriever(BaseRetriever):
    """记录调用参数的检索替身；可返回候选或抛出指定异常。"""

    def __init__(
        self,
        mode: RetrievalMode,
        hits: Sequence[RetrievedChunk] = (),
        error: Exception | None = None,
    ) -> None:
        self.mode = mode
        self._hits = list(hits)
        self._error = error
        self.calls: list[dict[str, object]] = []

    def search(
        self,
        session: object,
        query: str | Sequence[float],
        *,
        top_k: int = DEFAULT_TOP_K,
        filters: RetrievalFilters | None = None,
    ) -> list[RetrievedChunk]:
        self.calls.append({"query": query, "top_k": top_k, "filters": filters})
        if self._error is not None:
            raise self._error
        return self._hits[:top_k]


def _build_hybrid(
    *,
    vector_hits: Sequence[RetrievedChunk] = (),
    keyword_hits: Sequence[RetrievedChunk] = (),
    keyword_error: Exception | None = None,
    vector_weight: float | None = 0.5,
    candidate_k: int = DEFAULT_CANDIDATE_K,
) -> tuple[HybridSearchRetriever, StubRetriever, StubRetriever]:
    """构造注入替身两路召回的 Hybrid 检索器。"""

    vector = StubRetriever(RetrievalMode.VECTOR_ONLY, vector_hits)
    keyword = StubRetriever(RetrievalMode.KEYWORD_ONLY, keyword_hits, error=keyword_error)
    hybrid = HybridSearchRetriever(
        vector_weight=vector_weight,
        candidate_k=candidate_k,
        vector_retriever=vector,
        keyword_retriever=keyword,
    )
    return hybrid, vector, keyword


def test_min_max_normalize_maps_scores_to_unit_interval() -> None:
    """min-max 归一化覆盖常规、单条与全等分三种情况。"""

    assert min_max_normalize({}) == {}
    assert min_max_normalize({"only": 3.0}) == {"only": 1.0}
    assert min_max_normalize({"a": 2.0, "b": 2.0}) == {"a": 1.0, "b": 1.0}

    normalized = min_max_normalize({"a": 1.0, "b": 3.0, "c": 5.0})
    assert normalized == {"a": 0.0, "b": 0.5, "c": 1.0}


def test_hybrid_fusion_uses_weighted_sum_of_normalized_scores() -> None:
    """融合分数等于 α * 归一化向量分 + (1-α) * 归一化关键词分。"""

    hybrid, _, _ = _build_hybrid(
        vector_hits=[
            _candidate("chunk-a", semantic_score=0.9),
            _candidate("chunk-b", semantic_score=0.5),
        ],
        keyword_hits=[
            _candidate("chunk-b", keyword_score=0.8),
            _candidate("chunk-c", keyword_score=0.2),
        ],
        vector_weight=0.5,
    )

    results = hybrid.search(None, RetrievalQuery("余弦距离", QUERY_VECTOR), top_k=5)

    fused = {item.chunk_id: item for item in results}
    assert fused["chunk-a"].fusion_score == pytest.approx(0.5)
    assert fused["chunk-b"].fusion_score == pytest.approx(0.5)
    assert fused["chunk-c"].fusion_score == pytest.approx(0.0)

    assert [item.rank for item in results] == [0, 1, 2]
    # 同分时按 chunk_id 升序，保证结果确定。
    assert [item.chunk_id for item in results] == ["chunk-a", "chunk-b", "chunk-c"]
    # 原始分与来源标记不被融合覆盖。
    assert fused["chunk-a"].semantic_score == pytest.approx(0.9)
    assert fused["chunk-a"].source_mode == SOURCE_MODE_VECTOR
    assert fused["chunk-b"].source_mode == SOURCE_MODE_BOTH
    assert fused["chunk-c"].source_mode == SOURCE_MODE_KEYWORD
    assert fused["chunk-c"].semantic_score is None


@pytest.mark.parametrize("weight", [0.0, 1.0])
def test_hybrid_weight_boundaries_delegate_to_single_route(weight: float) -> None:
    """α 取边界时融合分数退化为单路归一化分数。"""

    hybrid, _, _ = _build_hybrid(
        vector_hits=[
            _candidate("chunk-a", semantic_score=0.9),
            _candidate("chunk-b", semantic_score=0.1),
        ],
        keyword_hits=[
            _candidate("chunk-c", keyword_score=1.0),
            _candidate("chunk-b", keyword_score=0.0),
        ],
        vector_weight=weight,
    )

    results = {item.chunk_id: item.fusion_score for item in hybrid.search(
        None, RetrievalQuery("查询", QUERY_VECTOR), top_k=5
    )}

    # α=1.0 时只看向量路：chunk-c 未被向量路召回，因此记 0 分；
    # α=0.0 时只看关键词路：chunk-a 未被关键词路召回，同样记 0 分。
    expected = (
        {"chunk-a": 1.0, "chunk-b": 0.0, "chunk-c": 0.0}
        if weight == 1.0
        else {"chunk-a": 0.0, "chunk-b": 0.0, "chunk-c": 1.0}
    )
    for chunk_id, score in expected.items():
        assert results[chunk_id] == pytest.approx(score)


def test_hybrid_deduplicates_candidates_and_merges_source_mode() -> None:
    """同一 chunk_id 只保留一条候选，并标注 both。"""

    duplicated = _candidate("chunk-a", semantic_score=1.0, content="向量路内容")
    hybrid, _, _ = _build_hybrid(
        vector_hits=[duplicated, duplicated],
        keyword_hits=[
            _candidate("chunk-a", keyword_score=0.5, content="关键词路内容"),
        ],
    )

    results = hybrid.search(None, RetrievalQuery("查询", QUERY_VECTOR), top_k=5)

    assert len(results) == 1
    assert results[0].chunk_id == "chunk-a"
    assert results[0].source_mode == SOURCE_MODE_BOTH
    assert results[0].semantic_score == pytest.approx(1.0)
    assert results[0].keyword_score == pytest.approx(0.5)
    # 内容与来源元数据保持第一路候选的真实值，不被融合改写。
    assert results[0].metadata["location"] == "第 1 段"


def test_hybrid_respects_top_k_and_candidate_k() -> None:
    """融合后截取 top_k，并把候选数量透传给两路召回。"""

    vector_hits = [_candidate(f"chunk-{index}", semantic_score=index / 10) for index in range(5)]
    hybrid, vector, keyword = _build_hybrid(
        vector_hits=vector_hits,
        keyword_hits=[_candidate("chunk-k", keyword_score=0.4)],
        candidate_k=20,
    )

    results = hybrid.search(None, RetrievalQuery("查询", QUERY_VECTOR), top_k=2)

    assert len(results) == 2
    assert [item.rank for item in results] == [0, 1]
    assert vector.calls[0]["top_k"] == 20
    assert keyword.calls[0]["top_k"] == 20
    assert vector.calls[0]["query"] == QUERY_VECTOR
    assert keyword.calls[0]["query"] == "查询"


def test_hybrid_requires_text_and_embedding_together() -> None:
    """缺少查询文本或向量时明确失败，不猜测缺失的一路。"""

    hybrid, vector, keyword = _build_hybrid()

    with pytest.raises(RetrievalInputError):
        hybrid.search(None, QUERY_VECTOR)
    with pytest.raises(RetrievalInputError):
        hybrid.search(None, "只有文本")
    assert vector.calls == []
    assert keyword.calls == []


def test_hybrid_propagates_keyword_dialect_error() -> None:
    """关键词路不可用时传播方言错误，不静默退化为向量单路。"""

    hybrid, vector, _ = _build_hybrid(
        vector_hits=[_candidate("chunk-a", semantic_score=1.0)],
        keyword_error=RetrievalUnsupportedDialectError("当前方言不支持 tsvector。"),
    )

    with pytest.raises(RetrievalUnsupportedDialectError):
        hybrid.search(None, RetrievalQuery("查询", QUERY_VECTOR), top_k=5)
    assert len(vector.calls) == 1


def test_resolve_vector_weight_reads_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """未显式提供 α 时读取 HYBRID_VECTOR_WEIGHT 配置并校验范围。"""

    class _StubSettings:
        hybrid_vector_weight = 0.25

    monkeypatch.setattr(hybrid_module, "get_settings", lambda: _StubSettings())

    assert resolve_vector_weight() == pytest.approx(0.25)
    assert resolve_vector_weight(0.7) == pytest.approx(0.7)
    for invalid in (1.5, -0.1, "0.5", True):
        with pytest.raises(RetrievalInputError):
            resolve_vector_weight(invalid)  # type: ignore[arg-type]


def test_hybrid_uses_configured_weight_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认构造时使用配置的 α，不需要调用方显式传入。"""

    class _StubSettings:
        hybrid_vector_weight = 1.0

    monkeypatch.setattr(hybrid_module, "get_settings", lambda: _StubSettings())
    hybrid, _, _ = _build_hybrid(
        vector_hits=[
            _candidate("chunk-a", semantic_score=0.9),
            _candidate("chunk-b", semantic_score=0.1),
        ],
        keyword_hits=[_candidate("chunk-c", keyword_score=1.0)],
        vector_weight=None,
    )

    results = {item.chunk_id: item.fusion_score for item in hybrid.search(
        None, RetrievalQuery("查询", QUERY_VECTOR), top_k=5
    )}

    assert results["chunk-a"] == pytest.approx(1.0)
    assert results["chunk-c"] == pytest.approx(0.0)


def test_hybrid_is_available_through_mode_factory() -> None:
    """工厂可返回 Hybrid 实现，且声明 HYBRID 模式。"""

    from backend.app.ai.retrieval.base import get_retriever

    retriever = get_retriever(RetrievalMode.HYBRID)

    assert isinstance(retriever, HybridSearchRetriever)
    assert retriever.mode is RetrievalMode.HYBRID
