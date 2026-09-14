"""T040 检索契约测试（失败优先）。

契约以 ``.specify/contracts/rag-retrieval.md`` 为准：四种检索模式、九个必需结果字段、
来源追踪、空上下文与模式选择。本文件先于 T041/T042 实现编写，因此在实现落地前应当
失败；实现完成后同一套断言必须全部通过。

本文件不访问真实模型：语义检索只接收调用方传入的 query embedding，关键词检索只接收
查询文本。
"""

from __future__ import annotations

from dataclasses import fields
from uuid import UUID

import pytest

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
