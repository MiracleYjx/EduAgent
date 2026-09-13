"""T037 资料摄取编排单元测试。

覆盖成功路径与解析失败、分块失败、Embedding 失败（含 Provider 未就绪与未注册 Provider）。
测试只使用替身：假解析器注册表、假清洗器/分块器和假 Embedding Provider，不访问数据库、
网络或真实模型。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from backend.app.ai.embedding.base import (
    EMBEDDING_DIMENSION_MISMATCH,
    EMBEDDING_FAILED,
    EMBEDDING_INVALID_INPUT,
    EMBEDDING_PROVIDER_NOT_READY,
    BaseEmbeddingProvider,
    EmbeddingDimensionError,
    EmbeddingInputError,
    EmbeddingProviderError,
)
from backend.app.ai.embedding.factory import UnsupportedEmbeddingProviderError
from backend.app.ai.ingestion import service as service_module
from backend.app.ai.ingestion.chunking import chunk_document
from backend.app.ai.ingestion.cleaning import CleanedDocument
from backend.app.ai.ingestion.parsers import (
    DOCUMENT_CORRUPTED,
    DOCUMENT_EMPTY,
    DOCUMENT_PARSE_FAILED,
    DOCUMENT_UNSUPPORTED_FORMAT,
    DocumentParseError,
    DocumentParserRegistry,
    ParsedDocument,
    ParsedSection,
)
from backend.app.ai.ingestion.service import (
    INGESTION_ERROR_MESSAGES,
    KNOWLEDGE_BASE_EMPTY,
    IngestionResult,
    IngestionService,
)
from backend.app.domain.enums import DocumentStatus

DOCUMENT_ID = UUID("11111111-1111-4111-8111-111111111111")
COURSE_ID = UUID("22222222-2222-4222-8222-222222222222")
KNOWLEDGE_BASE_ID = UUID("33333333-3333-4333-8333-333333333333")
FILENAME = "第一章讲义.txt"
PAYLOAD = "第一节内容。".encode()

READY_SEQUENCE = [
    DocumentStatus.UPLOADED,
    DocumentStatus.PARSING,
    DocumentStatus.CHUNKING,
    DocumentStatus.EMBEDDING,
    DocumentStatus.READY,
]


def _run(awaitable: Any) -> Any:
    """同步执行协程，保持与仓库既有异步测试一致的写法。"""

    return asyncio.run(awaitable)


def _parsed_document() -> ParsedDocument:
    """构造解析结果替身：两个来源片段，便于验证分块数量。"""

    sections = (
        ParsedSection(index=1, location="第 1 段", text="第一节内容。"),
        ParsedSection(index=2, location="第 2 段", text="第二节内容。"),
    )
    return ParsedDocument(
        file_format="txt",
        text="第一节内容。\n\n第二节内容。",
        sections=sections,
    )


def _registry(
    document: ParsedDocument | None = None, error: Exception | None = None
) -> MagicMock:
    """构造假的解析器注册表；只替换解析行为，不引入真实文件解析。"""

    registry = MagicMock(spec=DocumentParserRegistry)
    if error is not None:
        registry.parse.side_effect = error
    else:
        registry.parse.return_value = document
    return registry


class StubEmbeddingProvider(BaseEmbeddingProvider):
    """可控的 Embedding 替身：返回确定向量、错误或故意的数量违约。"""

    provider_name = "stub"
    model_name = "stub-v1"

    def __init__(
        self,
        *,
        dimension: int = 4,
        error: EmbeddingProviderError | None = None,
        vector_count: int | None = None,
        ready: bool = True,
        readiness_detail: str | None = None,
    ) -> None:
        self.dimension = dimension
        self.calls: list[list[str]] = []
        self._error = error
        self._vector_count = vector_count
        self._ready = ready
        self._readiness_detail = readiness_detail

    def is_ready(self) -> bool:
        return self._ready

    def readiness_detail(self) -> str | None:
        return self._readiness_detail

    async def embed_documents(self, documents: Sequence[str]) -> list[list[float]]:
        normalized = self.ensure_documents(documents)
        if self._error is not None:
            raise self._error
        self.calls.append(normalized)
        if self._vector_count is not None:
            # 故意违约：返回与输入数量不等的向量，用于验证编排层的兜底校验。
            return [self._vector(text) for text in normalized[: self._vector_count]]
        return self.validate_document_vectors(
            normalized, [self._vector(text) for text in normalized]
        )

    async def embed_query(self, query: str) -> list[float]:
        return self._vector(self.ensure_query(query))

    def _vector(self, text: str) -> list[float]:
        return [float((index + len(text)) % 7) for index in range(self.dimension)]


def _assert_failed(
    result: IngestionResult,
    *,
    error_code: str,
    retryable: bool,
    sequence: list[DocumentStatus],
) -> None:
    """断言失败结果的公共契约：终态 Failed、可读原因且不产出任何 chunk。"""

    assert result.status is DocumentStatus.FAILED
    assert result.succeeded is False
    assert result.error_code == error_code
    assert result.error_message == INGESTION_ERROR_MESSAGES[error_code]
    assert result.retryable is retryable
    assert result.chunks == ()
    assert [transition.status for transition in result.transitions] == sequence
    assert result.transitions[-1].error_code == error_code


def test_ingestion_service_success_flow_builds_ready_chunks() -> None:
    """成功路径：状态按序推进到 Ready，并产出带来源元数据的向量片段。"""

    registry = _registry(_parsed_document())
    provider = StubEmbeddingProvider()
    transitions: list[Any] = []
    service = IngestionService(
        parser_registry=registry,
        embedding_provider=provider,
        on_transition=transitions.append,
    )

    result = _run(
        service.ingest(
            filename=FILENAME,
            data=PAYLOAD,
            document_id=DOCUMENT_ID,
            course_id=COURSE_ID,
            knowledge_base_id=KNOWLEDGE_BASE_ID,
        )
    )

    assert result.succeeded is True
    assert result.status is DocumentStatus.READY
    assert result.error_code is None
    assert result.error_message is None
    assert result.detail is None
    assert result.retryable is False
    assert result.file_format == "txt"
    assert [transition.status for transition in result.transitions] == READY_SEQUENCE
    assert [transition.status for transition in transitions] == READY_SEQUENCE
    registry.parse.assert_called_once_with(FILENAME, PAYLOAD)

    assert result.chunks
    assert [chunk.chunk_index for chunk in result.chunks] == list(
        range(len(result.chunks))
    )
    for chunk in result.chunks:
        assert chunk.content.strip()
        assert len(chunk.embedding) == 4
        assert chunk.metadata["document_id"] == str(DOCUMENT_ID)
        assert chunk.metadata["course_id"] == str(COURSE_ID)
        assert chunk.metadata["knowledge_base_id"] == str(KNOWLEDGE_BASE_ID)
        assert chunk.metadata["chunk_index"] == chunk.chunk_index
        assert chunk.metadata["original_filename"] == FILENAME
        assert chunk.metadata["file_format"] == "txt"
        assert isinstance(chunk.metadata["content_sha256"], str)

    embedded = [chunk.content for chunk in result.chunks]
    assert provider.calls == [embedded]


@pytest.mark.parametrize(
    ("error_code", "retryable"),
    [
        (DOCUMENT_PARSE_FAILED, True),
        (DOCUMENT_CORRUPTED, True),
        (DOCUMENT_EMPTY, False),
        (DOCUMENT_UNSUPPORTED_FORMAT, False),
    ],
)
def test_ingestion_service_parse_failure_stops_pipeline(
    error_code: str, retryable: bool
) -> None:
    """解析失败：复用解析器失败码与 retryable，且不再进入分块和 Embedding。"""

    registry = _registry(error=DocumentParseError(error_code, detail="替身解析失败"))
    provider = StubEmbeddingProvider()
    chunker = MagicMock(side_effect=AssertionError("解析失败时不应分块"))
    service = IngestionService(
        parser_registry=registry,
        chunker=chunker,
        embedding_provider=provider,
    )

    result = _run(service.ingest(filename=FILENAME, data=PAYLOAD))

    _assert_failed(
        result,
        error_code=error_code,
        retryable=retryable,
        sequence=[
            DocumentStatus.UPLOADED,
            DocumentStatus.PARSING,
            DocumentStatus.FAILED,
        ],
    )
    assert result.detail == "替身解析失败"
    chunker.assert_not_called()
    assert provider.calls == []


def test_ingestion_service_parse_failure_without_extension_reports_format() -> None:
    """解析失败时文件格式回退到扩展名，便于界面展示。"""

    registry = _registry(error=DocumentParseError(DOCUMENT_EMPTY))
    service = IngestionService(
        parser_registry=registry, embedding_provider=StubEmbeddingProvider()
    )

    result = _run(service.ingest(filename="资料.TXT", data=b""))

    assert result.file_format == "txt"


def test_ingestion_service_chunking_failure_maps_to_failed() -> None:
    """分块失败：记为 Failed、给出可读原因，且不调用 Embedding Provider。"""

    provider = StubEmbeddingProvider()
    service = IngestionService(
        parser_registry=_registry(_parsed_document()),
        chunker=MagicMock(side_effect=RuntimeError("分块器内部错误")),
        embedding_provider=provider,
    )

    result = _run(service.ingest(filename=FILENAME, data=PAYLOAD))

    _assert_failed(
        result,
        error_code=DOCUMENT_PARSE_FAILED,
        retryable=True,
        sequence=[
            DocumentStatus.UPLOADED,
            DocumentStatus.PARSING,
            DocumentStatus.CHUNKING,
            DocumentStatus.FAILED,
        ],
    )
    assert result.detail is not None
    assert "分块" in result.detail
    assert provider.calls == []


def test_ingestion_service_empty_knowledge_maps_to_knowledge_base_empty() -> None:
    """清洗后无内容：判定为 KNOWLEDGE_BASE_EMPTY 终态，不重试也不生成向量。"""

    provider = StubEmbeddingProvider()
    empty = CleanedDocument(file_format="txt", text="", sections=())
    service = IngestionService(
        parser_registry=_registry(_parsed_document()),
        cleaner=MagicMock(return_value=empty),
        embedding_provider=provider,
    )

    result = _run(service.ingest(filename=FILENAME, data=PAYLOAD))

    _assert_failed(
        result,
        error_code=KNOWLEDGE_BASE_EMPTY,
        retryable=False,
        sequence=[
            DocumentStatus.UPLOADED,
            DocumentStatus.PARSING,
            DocumentStatus.CHUNKING,
            DocumentStatus.FAILED,
        ],
    )
    assert result.error_message == "资料未形成有效知识片段，暂不能用于出题或阅卷。"
    assert provider.calls == []


@pytest.mark.parametrize(
    ("error", "error_code", "retryable"),
    [
        (EmbeddingProviderError("Provider 超时"), EMBEDDING_FAILED, True),
        (EmbeddingInputError("输入非法"), EMBEDDING_INVALID_INPUT, False),
        (EmbeddingDimensionError("维度不一致"), EMBEDDING_DIMENSION_MISMATCH, False),
    ],
)
def test_ingestion_service_embedding_failure_maps_provider_error(
    error: EmbeddingProviderError, error_code: str, retryable: bool
) -> None:
    """Embedding 失败：复用 Provider 失败码与 retryable，且不产出半成品知识。"""

    service = IngestionService(
        parser_registry=_registry(_parsed_document()),
        embedding_provider=StubEmbeddingProvider(error=error),
    )

    result = _run(service.ingest(filename=FILENAME, data=PAYLOAD))

    _assert_failed(
        result,
        error_code=error_code,
        retryable=retryable,
        sequence=[
            DocumentStatus.UPLOADED,
            DocumentStatus.PARSING,
            DocumentStatus.CHUNKING,
            DocumentStatus.EMBEDDING,
            DocumentStatus.FAILED,
        ],
    )
    assert result.detail == error.detail


def test_ingestion_service_not_ready_provider_is_not_faked() -> None:
    """Provider 未就绪：返回明确的未就绪错误，不伪造实例、不调用向量化。"""

    provider = StubEmbeddingProvider(
        ready=False, readiness_detail="缺少 EMBEDDING_MODEL 配置"
    )
    service = IngestionService(
        parser_registry=_registry(_parsed_document()), embedding_provider=provider
    )

    result = _run(service.ingest(filename=FILENAME, data=PAYLOAD))

    _assert_failed(
        result,
        error_code=EMBEDDING_PROVIDER_NOT_READY,
        retryable=False,
        sequence=[
            DocumentStatus.UPLOADED,
            DocumentStatus.PARSING,
            DocumentStatus.CHUNKING,
            DocumentStatus.EMBEDDING,
            DocumentStatus.FAILED,
        ],
    )
    assert result.detail == "缺少 EMBEDDING_MODEL 配置"
    assert provider.calls == []


def test_ingestion_service_maps_unsupported_provider_to_not_ready() -> None:
    """未注册 Provider：映射为 EMBEDDING_PROVIDER_NOT_READY，属于配置问题不重试。"""

    builder = MagicMock(
        side_effect=UnsupportedEmbeddingProviderError(
            "未注册的 Embedding Provider：foo。"
        )
    )
    service = IngestionService(
        parser_registry=_registry(_parsed_document()), provider_builder=builder
    )

    result = _run(service.ingest(filename=FILENAME, data=PAYLOAD))

    _assert_failed(
        result,
        error_code=EMBEDDING_PROVIDER_NOT_READY,
        retryable=False,
        sequence=[
            DocumentStatus.UPLOADED,
            DocumentStatus.PARSING,
            DocumentStatus.CHUNKING,
            DocumentStatus.EMBEDDING,
            DocumentStatus.FAILED,
        ],
    )
    assert result.detail is not None
    assert "未注册" in result.detail
    builder.assert_called_once_with()


def test_ingestion_service_rejects_vector_count_mismatch() -> None:
    """Provider 返回的向量数量与片段数量不一致时按维度不一致失败。"""

    service = IngestionService(
        parser_registry=_registry(_parsed_document()),
        embedding_provider=StubEmbeddingProvider(vector_count=1),
    )

    result = _run(service.ingest(filename=FILENAME, data=PAYLOAD))

    _assert_failed(
        result,
        error_code=EMBEDDING_DIMENSION_MISMATCH,
        retryable=False,
        sequence=[
            DocumentStatus.UPLOADED,
            DocumentStatus.PARSING,
            DocumentStatus.CHUNKING,
            DocumentStatus.EMBEDDING,
            DocumentStatus.FAILED,
        ],
    )
    assert result.detail is not None
    assert "数量" in result.detail


def test_ingestion_service_supports_async_provider_builder() -> None:
    """异步 Provider 构造器同样可用，保证与工厂异步扩展兼容。"""

    provider = StubEmbeddingProvider()

    async def build() -> BaseEmbeddingProvider:
        return provider

    service = IngestionService(
        parser_registry=_registry(_parsed_document()), provider_builder=build
    )

    result = _run(service.ingest(filename=FILENAME, data=PAYLOAD))

    assert result.succeeded is True
    assert provider.calls != []


def test_ingestion_service_passes_source_ids_to_chunker() -> None:
    """编排层必须把 document_id/course_id/knowledge_base_id 传给分块器。"""

    chunker = MagicMock(side_effect=chunk_document)
    service = IngestionService(
        parser_registry=_registry(_parsed_document()),
        chunker=chunker,
        embedding_provider=StubEmbeddingProvider(),
    )

    result = _run(
        service.ingest(
            filename=FILENAME,
            data=PAYLOAD,
            document_id=DOCUMENT_ID,
            course_id=COURSE_ID,
            knowledge_base_id=KNOWLEDGE_BASE_ID,
        )
    )

    assert result.succeeded is True
    kwargs = chunker.call_args.kwargs
    assert kwargs["document_id"] == DOCUMENT_ID
    assert kwargs["course_id"] == COURSE_ID
    assert kwargs["knowledge_base_id"] == KNOWLEDGE_BASE_ID
    assert kwargs["original_filename"] == FILENAME


def test_ingestion_service_module_does_not_touch_database() -> None:
    """编排模块不得引入数据库依赖，持久化由 Service 层负责。"""

    source = Path(service_module.__file__).read_text(encoding="utf-8")

    assert "sqlalchemy" not in source
    assert "Session" not in source
    assert "session" not in source
