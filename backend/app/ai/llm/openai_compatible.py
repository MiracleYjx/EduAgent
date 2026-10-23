"""独立的 OpenAI-compatible 文本 JSON Adapter；不推定 Vision 或 JSON Schema。"""

from __future__ import annotations

import inspect
import ipaddress
import json
import logging
import math
from collections.abc import Mapping
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel, SecretStr, ValidationError

from backend.app.core.retry_policy import (
    ProviderCallError,
    RetryPolicy,
    classify_provider_exception,
)

from .base import BaseLLMProvider, LLMMessages, LLMProviderMetadata

if TYPE_CHECKING:
    from backend.app.core.config import AppSettings

_JSON_INSTRUCTION = "请仅返回合法的 JSON 对象，不要输出 Markdown、解释或其他文本。"
_REASONING_EFFORTS = frozenset({"low", "high", "max"})
_SDK_CALL: ContextVar[bool] = ContextVar("openai_compatible_private_sdk_call", default=False)


class _PrivateSDKLogs(logging.Filter):
    """SDK DEBUG 包含正文/原始异常；仅阻止当前 Adapter 调用上下文的记录。"""

    def filter(self, record: logging.LogRecord) -> bool:
        return not _SDK_CALL.get()


# 不改变 logger level/disabled，也不抑制其他并发任务的 SDK 记录。
# ContextVar 会随异步任务隔离；每次调用均在 finally 恢复原 token。
_sdk_log_filter = _PrivateSDKLogs()
for _logger_name in ("openai._base_client", "openai._response", "openai._legacy_response"):
    logging.getLogger(_logger_name).addFilter(_sdk_log_filter)


class _BorrowedTransport(httpx.AsyncBaseTransport):
    """只借用连接池；关闭自有 HTTP/SDK 视图不会关闭外部客户端。"""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # 禁用外部池的默认 auth 和 redirect；请求头已由本 Adapter 的 SDK 构造。
        return await self._client.send(request, stream=True, auth=None, follow_redirects=False)


def _base_url(value: str, *, auth_mode: str) -> str:
    try:
        url = httpx.URL(value)
        if url.scheme not in {"http", "https"} or not url.host or url.userinfo or url.query or url.fragment:
            raise ValueError
        if auth_mode == "api_key" and url.scheme == "http":
            local = url.host.lower() == "localhost"
            try:
                local = local or ipaddress.ip_address(url.host).is_loopback
            except ValueError:
                pass
            if not local:
                raise ValueError
    except (TypeError, ValueError, httpx.InvalidURL):
        raise ValueError("LLM Provider 地址无效，须使用无凭据和查询参数的安全 HTTP(S) 地址。") from None
    return str(url)


class OpenAICompatibleProvider(BaseLLMProvider):
    """文本 JSON object + json.loads + 本地 Pydantic，独立于 DeepSeek。

    client 是借用的 SDK 或测试端口。真实 AsyncOpenAI 只借用其 HTTP 池，
    使用本实例捕获的配置创建隔离 SDK 视图，不重配或关闭调用方 SDK。
    http_client 同样不转移所有权；自有 SDK/HTTP 视图统一由 aclose 释放。
    """

    provider_name = "openai_compatible"

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: SecretStr | str | None = None,
        auth_mode: str = "api_key",
        temperature: float | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
        timeout: float = 30.0,
        client: Any = None,
        http_client: httpx.AsyncClient | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("LLM Provider 必须配置非空模型。")
        if auth_mode not in {"api_key", "none"}:
            raise ValueError("LLM Provider 鉴权模式无效。")
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("LLM Provider 超时必须为有限正数。")
        if reasoning_effort is not None and reasoning_effort not in _REASONING_EFFORTS:
            raise ValueError("LLM Provider reasoning_effort 无效。")
        if client is not None and http_client is not None:
            raise ValueError("LLM Provider 不接受同时注入 SDK 和 HTTP 客户端。")
        secret: SecretStr | None = None
        if auth_mode == "api_key":
            if isinstance(api_key, str):
                api_key = SecretStr(api_key)
            if not isinstance(api_key, SecretStr) or not api_key.get_secret_value().strip():
                raise ProviderCallError("KEY_MISSING", "LLM Provider 缺少 API Key。")
            secret = api_key

        # Registry 是参数白名单、范围和 JSON Schema 的唯一事实源；惰性导入避环。
        from backend.app.services.settings.provider_registry import TextParameters

        try:
            parameters = TextParameters(temperature=temperature, max_tokens=max_tokens)
        except ValidationError:
            raise ValueError("LLM Provider 文本参数无效，请检查 temperature 和 max_tokens。") from None

        self._model = model.strip()
        self._auth_mode = auth_mode
        self._api_key = secret
        self._base_url = _base_url(base_url, auth_mode=auth_mode)
        self._parameters = parameters.model_dump(exclude_none=True)
        # 默认 None 保持 G-P 冻结行为；启用前必须重新确认模型与预算。
        self._reasoning_effort = reasoning_effort
        self._timeout = float(timeout)
        self._retry_policy = retry_policy if retry_policy is not None else RetryPolicy()
        self._client = client
        self._borrowed_http = http_client
        self._sdk_client: Any = None
        self._owned_http: httpx.AsyncClient | None = None
        self._closed = False

    @property
    def model_name(self) -> str:
        return self._model

    @classmethod
    def from_settings(
        cls,
        settings: AppSettings,
        *,
        client: Any = None,
        http_client: httpx.AsyncClient | None = None,
        retry_policy: RetryPolicy | None = None,
        timeout: float = 30.0,
    ) -> OpenAICompatibleProvider:
        """仅捕获原 deepseek_* 字段，不保留或重新读取全局配置。"""
        return cls(
            model=settings.deepseek_model,
            base_url=str(settings.deepseek_base_url),
            api_key=settings.deepseek_api_key,
            auth_mode=settings.llm_auth_mode,
            client=client,
            http_client=http_client,
            retry_policy=retry_policy,
            timeout=timeout,
        )

    def describe(self, *, prompt_version: str | None = None) -> LLMProviderMetadata:
        metadata = super().describe(prompt_version=prompt_version)
        if self._client is not None:
            from openai import AsyncOpenAI

            if not isinstance(self._client, AsyncOpenAI):
                metadata.update({"provider": "unknown", "model": "unknown"})
        return metadata

    async def _request_client(self) -> Any:
        if self._closed:
            raise ProviderCallError("ProviderClosed", "LLM Provider 已关闭。")
        from openai import AsyncOpenAI

        if self._client is not None and not isinstance(self._client, AsyncOpenAI):
            return self._client
        if self._sdk_client is not None:
            return self._sdk_client

        borrowed = self._borrowed_http
        if self._client is not None:
            # 只借池，不调用 with_options/copy，以免其重新回退到 ambient Key。
            borrowed = self._client._client
            if not isinstance(borrowed, httpx.AsyncClient):
                raise ProviderCallError("ProviderFailed", "LLM Provider 注入的 HTTP 客户端类型不受支持。")
        if borrowed is None:
            http = httpx.AsyncClient(timeout=self._timeout, follow_redirects=False, trust_env=False)
        else:
            http = httpx.AsyncClient(
                transport=_BorrowedTransport(borrowed), timeout=self._timeout,
                follow_redirects=False, trust_env=False,
            )
        try:
            options: dict[str, Any] = {
                "api_key": self._api_key.get_secret_value() if self._api_key is not None else "",
                "base_url": self._base_url, "timeout": self._timeout, "max_retries": 0,
                "http_client": http, "organization": "", "project": "", "webhook_secret": "",
            }
            sdk_parameters = inspect.signature(AsyncOpenAI).parameters
            # 2.54 新增 admin Key 的 env 回退与空凭据门禁；旧 SDK 没有这些参数。
            if "admin_api_key" in sdk_parameters:
                options["admin_api_key"] = ""
            if self._auth_mode == "none" and "_enforce_credentials" in sdk_parameters:
                options["_enforce_credentials"] = False
            sdk = AsyncOpenAI(**options)
            # 新 SDK 可读 OPENAI_CUSTOM_HEADERS；不允许 ambient headers 越过白名单。
            # 只清理本 Adapter 拥有的新 SDK，不修改注入实例。
            sdk._custom_headers = {}
        except BaseException:
            await http.aclose()
            raise
        self._sdk_client = sdk
        self._owned_http = http
        return sdk

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            try:
                if self._sdk_client is not None:
                    await self._sdk_client.close()
            finally:
                if self._owned_http is not None and not self._owned_http.is_closed:
                    await self._owned_http.aclose()
        except Exception:  # noqa: BLE001 - 释放边界须捕获任意第三方异常，防止秘密正文外泄。
            raise ProviderCallError("ProviderFailed", "LLM Provider 客户端释放失败。") from None

    async def generate_structured(
        self,
        messages: LLMMessages,
        schema: type[BaseModel],
        model: str | None = None,
    ) -> BaseModel:
        if self._closed:
            raise ProviderCallError("ProviderClosed", "LLM Provider 已关闭。")
        if not isinstance(schema, type) or not issubclass(schema, BaseModel):
            raise TypeError("结构化输出 Schema 必须继承 BaseModel。")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise ProviderCallError("ProviderFailed", "LLM Provider 请求模型无效。")
        prepared = self._prepare_messages(messages)
        requested_model = self._model if model is None else model.strip()

        async def request() -> BaseModel:
            return await self._request_json(prepared, schema, requested_model)

        return await self._retry_policy.execute(request)

    @staticmethod
    def _prepare_messages(messages: LLMMessages) -> list[dict[str, str]]:
        prepared: list[dict[str, str]] = []
        for message in messages:
            if not isinstance(message, Mapping):
                raise ProviderCallError("ProviderFailed", "LLM Provider 文本消息结构无效。")
            content = message.get("content")
            if not isinstance(content, str) or set(message) - {"role", "content", "name"}:
                raise ProviderCallError("VISION_NOT_SUPPORTED", "当前 Adapter 仅支持文本消息，不支持多模态或工具输入。")
            role = message.get("role")
            if not isinstance(role, str) or role not in {"system", "developer", "user", "assistant"}:
                raise ProviderCallError("ProviderFailed", "LLM Provider 文本消息角色无效。")
            converted = {"role": role, "content": content}
            if "name" in message:
                name = message["name"]
                if not isinstance(name, str):
                    raise ProviderCallError("ProviderFailed", "LLM Provider 文本消息名称无效。")
                converted["name"] = name
            prepared.append(converted)
        if not any("json" in item["content"].lower() for item in prepared):
            prepared.insert(0, {"role": "system", "content": _JSON_INSTRUCTION})
        return prepared

    async def _request_json(
        self, messages: list[dict[str, str]], schema: type[BaseModel], model: str,
    ) -> BaseModel:
        from openai import Omit

        token = _SDK_CALL.set(True)
        try:
            try:
                client = await self._request_client()
                options: dict[str, Any] = {
                    "messages": messages, "model": model,
                    "response_format": {"type": "json_object"}, **self._parameters,
                }
                if self._reasoning_effort is not None:
                    # 仅显式配置时发送；默认请求体和冻结预算保持不变。
                    options["extra_body"] = {
                        "reasoning_effort": self._reasoning_effort
                    }
                if self._auth_mode == "none":
                    # SDK 校验逐请求的显式 omit；没有假 Key，也不发送 Bearer 空串。
                    options["extra_headers"] = {"Authorization": Omit()}
                response = await client.chat.completions.create(**options)
            except Exception as error:  # noqa: BLE001 - SDK 边界须捕获任意第三方异常，防止秘密正文外泄。
                raise classify_provider_exception(error) from None
        finally:
            _SDK_CALL.reset(token)

        content, reasoning_content, finish_reason = self._extract_message_fields(response)
        if content is None or not content.strip():
            detail = "LLM Provider 返回空正文。"
            if reasoning_content:
                detail += " reasoning_content 已分离，未作为正文。"
            if finish_reason:
                detail += f" finish_reason={finish_reason}。"
            raise ProviderCallError(
                "ProviderEmptyResponse", detail, retryable=False
            )
        if finish_reason == "length":
            raise ProviderCallError(
                "ProviderOutputTruncated",
                "LLM Provider 输出因长度限制被截断。",
                retryable=False,
            )
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            raise ProviderCallError(
                "StructuredOutputFailed", "LLM Provider 返回的 JSON 无法解析。",
                retryable=True, retry_limit=1,
            ) from None
        if not isinstance(payload, dict):
            raise ProviderCallError(
                "StructuredOutputFailed", "LLM Provider 返回的 JSON 必须是对象。",
                retryable=True, retry_limit=1,
            )
        try:
            return schema.model_validate(payload)
        except ValidationError:
            raise ProviderCallError(
                "StructuredOutputFailed", "LLM Provider 返回未通过结构化校验。",
                retryable=True, retry_limit=1,
            ) from None

    @staticmethod
    def _extract_message_fields(
        response: Any,
    ) -> tuple[str | None, str | None, str | None]:
        """只读取 content；reasoning_content 独立保留，绝不作为正文。"""

        try:
            choices = response.choices
            if not choices:
                return None, None, None
            choice = choices[0]
            message = choice.message
            content = getattr(message, "content", None)
            reasoning = getattr(message, "reasoning_content", None)
            finish_reason = getattr(choice, "finish_reason", None)
        except (AttributeError, IndexError, TypeError):
            return None, None, None
        return (
            content if isinstance(content, str) else None,
            reasoning if isinstance(reasoning, str) else None,
            finish_reason if isinstance(finish_reason, str) else None,
        )

    @staticmethod
    def _extract_content(response: Any) -> str | None:
        return OpenAICompatibleProvider._extract_message_fields(response)[0]


__all__ = ["OpenAICompatibleProvider"]
