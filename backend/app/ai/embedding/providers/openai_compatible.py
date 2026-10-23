"""云端 OpenAI 兼容 Embedding Provider。

通过 OpenAI 兼容的 ``/embeddings`` 接口生成向量，可接入云端 Embedding API 或任何保持
该协议的服务。缺少 ``EMBEDDING_MODEL`` 或 ``EMBEDDING_API_KEY`` 时，``from_settings``
直接抛出 :class:`EmbeddingProviderNotReadyError`，不会返回无法调用的实例。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from contextvars import ContextVar
from typing import Any, Final, Literal

import httpx
from openai import DEFAULT_TIMEOUT, AsyncOpenAI, Omit, OpenAIError
from openai.types import CreateEmbeddingResponse

from backend.app.ai.embedding.base import (
    BaseEmbeddingProvider,
    EmbeddingProviderError,
    EmbeddingProviderNotReadyError,
)
from backend.app.core.config import AppSettings

DEFAULT_BATCH_SIZE: Final[int] = 64
_SDK_CALL: ContextVar[bool] = ContextVar("embedding_private_sdk_call", default=False)


class _PrivateSDKLogs(logging.Filter):
    """只阻止本 Provider 调用上下文的 SDK 正文及原始异常日志。"""

    def filter(self, record: logging.LogRecord) -> bool:
        return not _SDK_CALL.get()


_sdk_log_filter = _PrivateSDKLogs()
for _logger_name in ("openai._base_client", "openai._response", "openai._legacy_response"):
    logging.getLogger(_logger_name).addFilter(_sdk_log_filter)


class _BorrowedTransport(httpx.AsyncBaseTransport):
    """仅借用传输；自有 HTTP 视图构造请求，不继承外部池的默认头部。"""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # 默认 aclose 不关闭外部池；认证和重定向策略也不能从外部池继承。
        return await self._client.send(request, stream=True, auth=None, follow_redirects=False)


class OpenAICompatibleEmbeddingProvider(BaseEmbeddingProvider):
    """基于 OpenAI 兼容协议的云端 Embedding Provider。"""

    provider_name = "openai_compatible"

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str | None = None,
        dimension: int | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        client: AsyncOpenAI | None = None,
        auth_mode: Literal["api_key", "none"] = "api_key",
        timeout: float | httpx.Timeout | None = DEFAULT_TIMEOUT,
        max_retries: int = 2,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if auth_mode not in {"api_key", "none"}:
            raise ValueError("不支持的 Embedding 认证模式。")
        if auth_mode == "none" and api_key:
            raise ValueError("无认证 Embedding 不接受 API Key。")
        if client is not None and http_client is not None:
            raise ValueError("Embedding SDK 客户端与 HTTP 连接池不能同时注入。")
        from backend.app.services.settings.provider_registry import (
            validate_provider_endpoint,
        )

        endpoint = validate_provider_endpoint(base_url or "https://api.openai.com/v1")
        self._endpoint_identity = endpoint
        self.model_name = model
        self.dimension = dimension
        self._batch_size = batch_size
        self._auth_mode = auth_mode
        self._closed = False
        self._owns_client = client is None
        self._client: AsyncOpenAI
        self._owned_http: httpx.AsyncClient | None = None
        if client is not None and isinstance(client, AsyncOpenAI):
            # 真实 SDK 只出借 HTTP 池；端点、Key、重试与超时均由本实例决定。
            http_client = client._client
            client = None
            self._owns_client = True
        if client is not None:
            self._client = client
        else:
            if http_client is None:
                transport = httpx.AsyncClient(
                    timeout=timeout, follow_redirects=False, trust_env=False
                )
            else:
                transport = httpx.AsyncClient(
                    transport=_BorrowedTransport(http_client), timeout=timeout,
                    follow_redirects=False, trust_env=False,
                )
            self._owned_http = transport
            self._client = AsyncOpenAI(
                api_key=api_key if auth_mode == "api_key" else "",
                admin_api_key="",
                organization="",
                project="",
                webhook_secret="",
                base_url=endpoint,
                timeout=timeout,
                max_retries=max_retries,
                http_client=transport,
                _enforce_credentials=auth_mode != "none",
            )
            # SDK 2.54 会合并 OPENAI_CUSTOM_HEADERS；从不使用进程的隐式头部。
            self._client._custom_headers = {}

    @classmethod
    def from_settings(cls, settings: AppSettings) -> OpenAICompatibleEmbeddingProvider:
        """按运行配置创建 Provider；缺少必要配置时明确失败。"""

        model = (settings.embedding_model or "").strip()
        api_key = (
            settings.embedding_api_key.get_secret_value().strip()
            if settings.embedding_api_key is not None
            else ""
        )
        missing = [
            name
            for name, value in (("EMBEDDING_MODEL", model), ("EMBEDDING_API_KEY", api_key))
            if not value
        ]
        if missing:
            raise EmbeddingProviderNotReadyError(
                f"缺少 {', '.join(missing)} 配置，无法创建 {cls.provider_name} Provider。",
                provider_name=cls.provider_name,
            )
        return cls(
            model=model,
            api_key=api_key,
            base_url=str(settings.embedding_base_url) if settings.embedding_base_url else None,
            dimension=settings.embedding_dimension,
        )

    def endpoint_identity(self) -> str:
        """返回已验证、无 userinfo/query/fragment 的端点身份。"""

        return self._endpoint_identity

    def semantic_parameters(self) -> dict[str, Any]:
        """云端协议未声明文档归一化或前缀处理，保持明确事实。"""

        return {
            "normalize": False,
            "document_prefix": "",
            "query_prefix": "",
        }

    def is_ready(self) -> bool:
        """存在模型标识与客户端即视为可调用；连接错误在调用时统一收敛。"""

        return not self._closed and bool(self.model_name.strip()) and self._client is not None

    async def aclose(self) -> None:
        """幂等关闭自有 SDK；注入的 SDK 或底层 HTTP 池仍由调用方关闭。"""

        if self._closed:
            return
        self._closed = True
        token = _SDK_CALL.set(True)
        try:
            try:
                if self._owns_client:
                    await self._client.close()
            finally:
                if self._owned_http is not None and not self._owned_http.is_closed:
                    await self._owned_http.aclose()
        except Exception:  # noqa: BLE001 - 释放边界须收敛任意第三方错误，避免秘密正文外泄。
            raise EmbeddingProviderError(
                "云端 Embedding 客户端释放失败。", provider_name=self.provider_name
            ) from None
        finally:
            _SDK_CALL.reset(token)

    async def embed_documents(self, documents: Sequence[str]) -> list[list[float]]:
        normalized = self.ensure_documents(documents)
        vectors: list[list[float]] = []
        for start in range(0, len(normalized), self._batch_size):
            batch = normalized[start : start + self._batch_size]
            response = await self._create_embeddings(batch)
            vectors.extend(list(item.embedding) for item in response.data)
        return self.validate_document_vectors(normalized, vectors)

    async def embed_query(self, query: str) -> list[float]:
        validated = self.ensure_query(query)
        response = await self._create_embeddings([validated])
        if not response.data:
            raise EmbeddingProviderError(
                "云端 Embedding 未返回查询向量。", provider_name=self.provider_name
            )
        return self.validate_vector(list(response.data[0].embedding))

    async def _create_embeddings(self, inputs: list[str]) -> CreateEmbeddingResponse:
        """调用云端接口，并把 SDK 异常映射为统一的 Embedding 失败状态。"""

        if self._closed:
            raise EmbeddingProviderNotReadyError(
                "云端 Embedding Provider 已关闭，请重新创建实例。",
                provider_name=self.provider_name,
            )
        token = _SDK_CALL.set(True)
        try:
            if self._auth_mode == "none":
                # 每次请求显式 omit 才同时满足 SDK 校验与无 Authorization 传输。
                return await self._client.embeddings.create(
                    model=self.model_name,
                    input=list(inputs),
                    encoding_format="float",
                    extra_headers={"Authorization": Omit()},
                )
            return await self._client.embeddings.create(
                model=self.model_name,
                input=list(inputs),
                encoding_format="float",
            )
        except (OpenAIError, TimeoutError, OSError) as exc:
            # 只保留异常类型，避免把响应正文或密钥写入错误信息。
            raise EmbeddingProviderError(
                f"云端 Embedding 调用失败：{type(exc).__name__}。",
                provider_name=self.provider_name,
            ) from None
        finally:
            _SDK_CALL.reset(token)


__all__ = ["DEFAULT_BATCH_SIZE", "OpenAICompatibleEmbeddingProvider"]
