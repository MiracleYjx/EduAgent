"""T040 检索输入校验单元测试：只在契约层校验，不依赖数据库。

覆盖 Top-K、查询向量/文本与过滤条件的规范化，以及可选输入的错误码与可读提示。
"""

from __future__ import annotations

from uuid import UUID

import pytest

from backend.app.ai.retrieval.base import (
    DEFAULT_TOP_K,
    RETRIEVAL_INVALID_INPUT,
    RetrievalError,
    RetrievalFilters,
    RetrievalInputError,
    RetrievalMode,
    RetrievalModeNotImplementedError,
    normalize_mode,
    normalize_top_k,
    resolve_filters,
)

COURSE_ID = UUID("22222222-2222-4222-8222-222222222222")


def test_normalize_mode_accepts_value_and_name() -> None:
    """模式既可以按取值也可以按枚举名解析。"""

    assert normalize_mode("vector_only") is RetrievalMode.VECTOR_ONLY
    assert normalize_mode("HYBRID_RERANK") is RetrievalMode.HYBRID_RERANK
    assert normalize_mode(RetrievalMode.KEYWORD_ONLY) is RetrievalMode.KEYWORD_ONLY


def test_retrieval_errors_carry_code_and_message() -> None:
    """检索错误必须带机器可读错误码、可读提示且默认不可重试。"""

    error = RetrievalInputError("top_k 必须大于 0。")
    assert error.error_code == RETRIEVAL_INVALID_INPUT
    assert error.retryable is False
    assert "top_k" in str(error)
    assert error.user_message

    not_implemented = RetrievalModeNotImplementedError("尚未实现。")
    assert not_implemented.error_code != error.error_code
    assert isinstance(not_implemented, RetrievalError)


def test_filters_default_to_no_restriction() -> None:
    """未提供过滤条件时不限制课程范围。"""

    assert resolve_filters(None).is_empty is True
    assert RetrievalFilters().describe().startswith("课程范围")


def test_top_k_default_and_boundaries() -> None:
    """默认 Top-K 稳定，边界值通过，越界值失败。"""

    assert normalize_top_k(DEFAULT_TOP_K) == DEFAULT_TOP_K
    with pytest.raises(RetrievalInputError):
        normalize_top_k(0)
    with pytest.raises(RetrievalInputError):
        normalize_top_k(999)


def test_repeated_uuid_filter_values_keep_order() -> None:
    """过滤条件保留调用方给定的顺序，便于稳定诊断。"""

    filters = RetrievalFilters(course_ids=(COURSE_ID, COURSE_ID))
    assert filters.course_ids == (COURSE_ID, COURSE_ID)
