"""Hybrid 检索：候选合并、去重与 Weighted Score Fusion。

按 ``.specify/plan.md`` §2 的顺序执行：

1. 并行/顺序执行语义检索与关键词检索（各取 ``candidate_k`` 条候选，默认 20）。
2. 按 ``chunk_id`` 合并两路候选并去重。
3. 两路分数不同量纲，先各自 min-max 归一化到 ``[0, 1]``，再加权融合：
   ``score = α * vector_score + (1 - α) * keyword_score``，``α`` 来自配置项
   ``HYBRID_VECTOR_WEIGHT``（默认 0.5）。
4. 按融合分数降序截取 Top-K，并保留 ``source_mode``（``vector`` / ``keyword`` / ``both``）。

约定与边界：

- 缺失的那一路贡献 0 分（表示该路未召回该候选），不做任何补偿性猜测。
- 某一路只有单条候选或全部分数相同时，归一化统一记 1.0。
- 关键词路不可用（非 PostgreSQL 方言）时直接传播方言错误，**不静默退化为向量单路**。
- 本模块不调用 Embedding Provider：调用方需同时提供查询文本与 query embedding。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Final

from sqlalchemy.orm import Session

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
    RetrievedChunk,
    get_retriever,
    normalize_top_k,
    resolve_filters,
)
from backend.app.core.config import get_settings

#: 每路召回的候选数量；默认 20，兼顾召回与融合成本。
DEFAULT_CANDIDATE_K: Final[int] = 20


def min_max_normalize(scores: Mapping[str, float]) -> dict[str, float]:
    """把一路候选分数 min-max 归一化到 ``[0, 1]``。"""

    if not scores:
        return {}
    values = [float(value) for value in scores.values()]
    lowest = min(values)
    highest = max(values)
    if highest - lowest <= 0.0:
        # 单条候选或全部分数相同：视为同等最优，统一记 1.0。
        return {key: 1.0 for key in scores}
    return {
        key: (float(value) - lowest) / (highest - lowest)
        for key, value in scores.items()
    }


def resolve_vector_weight(value: float | None = None) -> float:
    """解析向量路权重 α；未显式提供时读取 ``HYBRID_VECTOR_WEIGHT`` 配置。"""

    raw: object = value
    if raw is None:
        raw = get_settings().hybrid_vector_weight
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise RetrievalInputError("HYBRID_VECTOR_WEIGHT 必须是 0 到 1 之间的数值。")
    weight = float(raw)
    if not 0.0 <= weight <= 1.0:
        raise RetrievalInputError("HYBRID_VECTOR_WEIGHT 必须落在 [0, 1] 区间。")
    return weight


class HybridSearchRetriever(BaseRetriever):
    """合并语义与关键词候选并执行加权分数融合的检索实现。"""

    mode = RetrievalMode.HYBRID

    def __init__(
        self,
        *,
        vector_weight: float | None = None,
        candidate_k: int = DEFAULT_CANDIDATE_K,
        vector_retriever: BaseRetriever | None = None,
        keyword_retriever: BaseRetriever | None = None,
    ) -> None:
        self.vector_weight = resolve_vector_weight(vector_weight)
        self.candidate_k = normalize_top_k(candidate_k)
        # 允许注入替身，便于在无 PostgreSQL 环境下验证纯融合逻辑。
        self._vector = vector_retriever or get_retriever(RetrievalMode.VECTOR_ONLY)
        self._keyword = keyword_retriever or get_retriever(RetrievalMode.KEYWORD_ONLY)

    def search(
        self,
        session: Session,
        query: str | Sequence[float] | RetrievalQuery,
        *,
        top_k: int = DEFAULT_TOP_K,
        filters: RetrievalFilters | None = None,
    ) -> list[RetrievedChunk]:
        """执行两路候选召回、去重与加权融合，返回 Top-K。"""

        if not isinstance(query, RetrievalQuery):
            raise RetrievalInputError(
                "混合检索需要同时提供查询文本与 query embedding，请使用 RetrievalQuery。"
            )
        limit = normalize_top_k(top_k)
        scope = resolve_filters(filters)

        vector_hits = self._vector.search(
            session,
            query.embedding,
            top_k=self.candidate_k,
            filters=scope,
        )
        # 关键词路不可用时抛出方言错误，由调用方明确处理，不做单路静默降级。
        keyword_hits = self._keyword.search(
            session,
            query.text,
            top_k=self.candidate_k,
            filters=scope,
        )

        vector_scores = {
            candidate.chunk_id: float(candidate.semantic_score or 0.0)
            for candidate in vector_hits
        }
        keyword_scores = {
            candidate.chunk_id: float(candidate.keyword_score or 0.0)
            for candidate in keyword_hits
        }
        normalized_vector = min_max_normalize(vector_scores)
        normalized_keyword = min_max_normalize(keyword_scores)

        deduplicated: dict[str, RetrievedChunk] = {}
        for candidate in (*vector_hits, *keyword_hits):
            deduplicated.setdefault(candidate.chunk_id, candidate)

        fused: list[RetrievedChunk] = []
        for chunk_id, candidate in deduplicated.items():
            in_vector = chunk_id in vector_scores
            in_keyword = chunk_id in keyword_scores
            if in_vector and in_keyword:
                source_mode = SOURCE_MODE_BOTH
            elif in_vector:
                source_mode = SOURCE_MODE_VECTOR
            else:
                source_mode = SOURCE_MODE_KEYWORD
            fusion_score = (
                self.vector_weight * normalized_vector.get(chunk_id, 0.0)
                + (1.0 - self.vector_weight) * normalized_keyword.get(chunk_id, 0.0)
            )
            fused.append(
                replace(
                    candidate,
                    # 原始分按各自召回路补齐，便于诊断两路真实贡献。
                    semantic_score=(
                        vector_scores.get(chunk_id) if in_vector else None
                    ),
                    keyword_score=(
                        keyword_scores.get(chunk_id) if in_keyword else None
                    ),
                    fusion_score=fusion_score,
                    source_mode=source_mode,
                    rank=0,
                )
            )

        # 融合分数降序；同分按 chunk_id 排序，保证结果确定。
        fused.sort(key=lambda item: (-float(item.fusion_score or 0.0), item.chunk_id))
        return [
            replace(candidate, rank=rank)
            for rank, candidate in enumerate(fused[:limit])
        ]


__all__ = [
    "DEFAULT_CANDIDATE_K",
    "HybridSearchRetriever",
    "min_max_normalize",
    "resolve_vector_weight",
]
