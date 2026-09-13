"""资料摄取编排：解析 -> 清洗 -> 分块 -> Embedding -> Ready/Failed。

编排器只负责推进状态机并产出“待持久化的结果对象”，不访问数据库：写入 ``Document``
状态、``DocumentChunk`` 片段和向量由 Service 层（T038/T039）完成。因此本模块可以在
没有数据库、没有真实模型的条件下被完整单元测试覆盖。

失败码约定与 ``.specify/plan.md`` 的摄取失败表一致：

- 解析阶段复用 ``parsers`` 的 ``DOCUMENT_*`` 码、``DocumentParseError.retryable`` 和解析器
  自带的用户提示，不重复维护文案。
- 清洗与分块后没有形成任何有效片段时，使用本模块定义的 ``KNOWLEDGE_BASE_EMPTY``；
  它属于摄取终态失败，不重试，也不得被解释为“可检索的空知识”。
- Embedding 阶段复用 ``embedding.base`` 的 ``EMBEDDING_*`` 码与 ``retryable``；工厂未注册
  的 Provider（``UnsupportedEmbeddingProviderError``）在本模块映射为
  ``EMBEDDING_PROVIDER_NOT_READY``，属于配置问题，不重试。
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final
from uuid import UUID

from backend.app.ai.embedding.base import (
    EMBEDDING_DIMENSION_MISMATCH,
    EMBEDDING_FAILED,
    EMBEDDING_INVALID_INPUT,
    EMBEDDING_PROVIDER_NOT_READY,
    BaseEmbeddingProvider,
    EmbeddingDimensionError,
    EmbeddingProviderError,
    EmbeddingProviderNotReadyError,
)
from backend.app.ai.embedding.factory import (
    EmbeddingProviderFactoryError,
    UnsupportedEmbeddingProviderError,
    create_embedding_provider,
)
from backend.app.ai.ingestion.chunking import (
    DEFAULT_MAX_CHARS,
    DEFAULT_OVERLAP_CHARS,
    TextChunk,
    chunk_document,
)
from backend.app.ai.ingestion.cleaning import CleanedDocument, clean_document
from backend.app.ai.ingestion.parsers import (
    DOCUMENT_ERROR_MESSAGES,
    DOCUMENT_PARSE_FAILED,
    DocumentParseError,
    DocumentParserRegistry,
    ParsedDocument,
    detect_file_extension,
    parse_document,
)
from backend.app.domain.enums import DocumentStatus

#: 资料未形成任何有效知识片段；属于摄取终态失败，不重试。
KNOWLEDGE_BASE_EMPTY: Final[str] = "KNOWLEDGE_BASE_EMPTY"

#: 摄取失败码对应的可读提示；解析类文案直接复用解析器注册表的定义。
INGESTION_ERROR_MESSAGES: Final[Mapping[str, str]] = MappingProxyType(
    {
        **DOCUMENT_ERROR_MESSAGES,
        KNOWLEDGE_BASE_EMPTY: "资料未形成有效知识片段，暂不能用于出题或阅卷。",
        EMBEDDING_FAILED: "知识向量生成失败，请稍后重试或切换 Embedding Provider。",
        EMBEDDING_INVALID_INPUT: "形成的知识片段不符合向量化输入要求，请检查资料内容后重新处理。",
        EMBEDDING_DIMENSION_MISMATCH: "知识向量维度与 Provider 声明不一致，请检查 Embedding 配置。",
        EMBEDDING_PROVIDER_NOT_READY: (
            "Embedding Provider 当前不可用，请检查配置或切换 Provider 后重新处理。"
        ),
    }
)

#: 清洗器契约：与 :func:`backend.app.ai.ingestion.cleaning.clean_document` 一致。
type Cleaner = Callable[[ParsedDocument], CleanedDocument]
#: 分块器契约：与 :func:`backend.app.ai.ingestion.chunking.chunk_document` 一致。
type Chunker = Callable[..., tuple[TextChunk, ...]]
#: Provider 构造器契约：同步或异步返回一个 Embedding Provider。
type ProviderBuilder = Callable[[], BaseEmbeddingProvider | Awaitable[BaseEmbeddingProvider]]
#: 状态流转监听器，供 Service 层把流转落库或推送界面。
type StatusListener = Callable[["IngestionTransition"], None]


@dataclass(frozen=True, slots=True)
class IngestionTransition:
    """一次状态流转记录；``message`` 只包含可安全展示的原因。"""

    status: DocumentStatus
    error_code: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class IngestedChunk:
    """待持久化的知识片段：内容、来源元数据和向量。"""

    chunk_index: int
    content: str
    metadata: dict[str, object]
    embedding: list[float]


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """一次完整摄取的终态结果，交给 Service 层持久化。"""

    status: DocumentStatus
    document_id: UUID | None
    course_id: UUID | None
    knowledge_base_id: UUID | None
    original_filename: str
    file_format: str
    transitions: tuple[IngestionTransition, ...]
    chunks: tuple[IngestedChunk, ...] = ()
    error_code: str | None = None
    error_message: str | None = None
    detail: str | None = None
    retryable: bool = False

    @property
    def succeeded(self) -> bool:
        """返回是否已形成可供出题与阅卷引用的知识。"""

        return self.status is DocumentStatus.READY


def resolve_error_message(error_code: str) -> str:
    """返回失败码对应的可读提示；未知失败码回退到通用解析失败提示。"""

    return INGESTION_ERROR_MESSAGES.get(
        error_code, INGESTION_ERROR_MESSAGES[DOCUMENT_PARSE_FAILED]
    )


class IngestionService:
    """把一份上传资料编排为待持久化的知识片段集合。"""

    def __init__(
        self,
        *,
        parser_registry: DocumentParserRegistry | None = None,
        cleaner: Cleaner = clean_document,
        chunker: Chunker = chunk_document,
        embedding_provider: BaseEmbeddingProvider | None = None,
        provider_builder: ProviderBuilder | None = None,
        max_chars: int = DEFAULT_MAX_CHARS,
        overlap_chars: int = DEFAULT_OVERLAP_CHARS,
        on_transition: StatusListener | None = None,
    ) -> None:
        self._parser_registry = parser_registry
        self._cleaner = cleaner
        self._chunker = chunker
        self._embedding_provider = embedding_provider
        self._provider_builder = provider_builder
        self._max_chars = max_chars
        self._overlap_chars = overlap_chars
        self._on_transition = on_transition

    async def ingest(
        self,
        *,
        filename: str,
        data: bytes,
        document_id: UUID | None = None,
        course_id: UUID | None = None,
        knowledge_base_id: UUID | None = None,
    ) -> IngestionResult:
        """按 Uploaded -> Parsing -> Chunking -> Embedding -> Ready 摄取资料。

        任一步失败都会立即进入 ``Failed`` 并停止后续步骤：失败结果不包含任何 chunk，
        避免上层把它当作可用知识。
        """

        transitions: list[IngestionTransition] = []

        def emit(
            status: DocumentStatus,
            *,
            error_code: str | None = None,
            message: str | None = None,
        ) -> None:
            """记录一次状态流转并通知监听器。"""

            transition = IngestionTransition(
                status=status, error_code=error_code, message=message
            )
            transitions.append(transition)
            if self._on_transition is not None:
                self._on_transition(transition)

        def fail(
            *,
            error_code: str,
            retryable: bool,
            detail: str | None = None,
            file_format: str | None = None,
            message: str | None = None,
        ) -> IngestionResult:
            """进入 Failed 终态并返回不含任何 chunk 的结果。"""

            user_message = message or resolve_error_message(error_code)
            emit(DocumentStatus.FAILED, error_code=error_code, message=user_message)
            return IngestionResult(
                status=DocumentStatus.FAILED,
                document_id=document_id,
                course_id=course_id,
                knowledge_base_id=knowledge_base_id,
                original_filename=filename,
                file_format=(
                    file_format if file_format else detect_file_extension(filename)
                ),
                transitions=tuple(transitions),
                chunks=(),
                error_code=error_code,
                error_message=user_message,
                detail=detail,
                retryable=retryable,
            )

        emit(DocumentStatus.UPLOADED)

        emit(DocumentStatus.PARSING)
        try:
            parsed = parse_document(filename, data, registry=self._parser_registry)
        except DocumentParseError as exc:
            return fail(
                error_code=exc.error_code,
                retryable=exc.retryable,
                detail=exc.detail,
            )
        except Exception as exc:  # noqa: BLE001  # 未知异常按可重试的解析失败处理，不泄露原始内容。
            return fail(
                error_code=DOCUMENT_PARSE_FAILED,
                retryable=True,
                detail=f"解析阶段未预期异常：{type(exc).__name__}",
            )

        emit(DocumentStatus.CHUNKING)
        try:
            cleaned = self._cleaner(parsed)
            chunks = self._chunker(
                cleaned,
                document_id=document_id,
                course_id=course_id,
                knowledge_base_id=knowledge_base_id,
                original_filename=filename,
                max_chars=self._max_chars,
                overlap_chars=self._overlap_chars,
            )
        except DocumentParseError as exc:
            return fail(
                error_code=exc.error_code,
                retryable=exc.retryable,
                detail=exc.detail,
                file_format=parsed.file_format,
            )
        except Exception as exc:  # noqa: BLE001  # 清洗与分块阶段的未知异常同样收敛为 Failed。
            return fail(
                error_code=DOCUMENT_PARSE_FAILED,
                retryable=True,
                detail=f"清洗或分块阶段未预期异常：{type(exc).__name__}",
                file_format=parsed.file_format,
            )

        if not chunks:
            # 解析与清洗成功但没有形成片段：属于终态失败，不重试、不进入 Embedding。
            return fail(
                error_code=KNOWLEDGE_BASE_EMPTY,
                retryable=False,
                detail="清洗与分块后没有形成任何有效知识片段",
                file_format=parsed.file_format,
            )

        emit(DocumentStatus.EMBEDDING)
        documents = [chunk.content for chunk in chunks]
        try:
            provider = await self._resolve_provider()
            vectors = await provider.embed_documents(documents)
            if len(vectors) != len(chunks):
                # Provider 未按契约返回等量向量时按维度不一致处理，不得产出半成品知识。
                raise EmbeddingDimensionError(
                    f"向量数量 {len(vectors)} 与知识片段数量 {len(chunks)} 不一致。",
                    provider_name=provider.provider_name,
                )
        except UnsupportedEmbeddingProviderError as exc:
            # 未注册的 Provider 属于配置问题，统一映射为“Provider 未就绪”。
            return fail(
                error_code=EMBEDDING_PROVIDER_NOT_READY,
                retryable=False,
                detail=str(exc),
                file_format=parsed.file_format,
            )
        except EmbeddingProviderFactoryError as exc:
            return fail(
                error_code=EMBEDDING_PROVIDER_NOT_READY,
                retryable=False,
                detail=str(exc),
                file_format=parsed.file_format,
            )
        except EmbeddingProviderError as exc:
            return fail(
                error_code=exc.error_code,
                retryable=exc.retryable,
                detail=exc.detail,
                file_format=parsed.file_format,
            )
        except Exception as exc:  # noqa: BLE001  # Provider 未映射异常按可重试的 Embedding 失败处理。
            return fail(
                error_code=EMBEDDING_FAILED,
                retryable=True,
                detail=f"Embedding 阶段未预期异常：{type(exc).__name__}",
                file_format=parsed.file_format,
            )

        emit(DocumentStatus.READY)
        return IngestionResult(
            status=DocumentStatus.READY,
            document_id=document_id,
            course_id=course_id,
            knowledge_base_id=knowledge_base_id,
            original_filename=filename,
            file_format=parsed.file_format,
            transitions=tuple(transitions),
            chunks=tuple(
                IngestedChunk(
                    chunk_index=chunk.index,
                    content=chunk.content,
                    metadata=chunk.as_metadata(),
                    embedding=[float(value) for value in vector],
                )
                for chunk, vector in zip(chunks, vectors, strict=True)
            ),
        )

    async def _resolve_provider(self) -> BaseEmbeddingProvider:
        """解析并校验 Embedding Provider；未就绪时抛出明确错误，不伪造实例。"""

        provider = self._embedding_provider
        if provider is None and self._provider_builder is not None:
            candidate: object = self._provider_builder()
            if inspect.isawaitable(candidate):
                candidate = await candidate
            if not isinstance(candidate, BaseEmbeddingProvider):
                raise EmbeddingProviderNotReadyError(
                    "Embedding Provider 构造器必须返回 BaseEmbeddingProvider 实例。",
                    provider_name="unknown",
                )
            provider = candidate
        if provider is None:
            provider = create_embedding_provider()
        if not provider.is_ready():
            raise EmbeddingProviderNotReadyError(
                provider.readiness_detail() or "Provider 未满足运行条件。",
                provider_name=provider.provider_name,
            )
        return provider


__all__ = [
    "INGESTION_ERROR_MESSAGES",
    "KNOWLEDGE_BASE_EMPTY",
    "Chunker",
    "Cleaner",
    "IngestedChunk",
    "IngestionResult",
    "IngestionService",
    "IngestionTransition",
    "ProviderBuilder",
    "StatusListener",
    "resolve_error_message",
]
