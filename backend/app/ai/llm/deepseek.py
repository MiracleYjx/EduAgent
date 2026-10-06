"""DeepSeek Chat Completions 的 JSON 结构化输出适配器。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from collections.abc import Mapping, Sequence
from time import perf_counter
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, ValidationError

from backend.app.ai.vision.base import ProviderImage
from backend.app.core.config import AppSettings
from backend.app.core.retry_policy import (
    AsyncSleep,
    ProviderCallError,
    RetryPolicy,
)

from .base import BaseLLMProvider, LLMMessages, LLMProviderMetadata

_JSON_INSTRUCTION = "请仅返回合法的 JSON 对象，不要输出 Markdown、解释或其他文本。"


class DeepSeekProvider(BaseLLMProvider):
    """通过 OpenAI-compatible SDK 调用 DeepSeek 并返回 Pydantic DTO。"""

    provider_name = "deepseek"

    def __init__(
        self,
        settings: AppSettings,
        *,
        client: Any | None = None,
        retry_policy: RetryPolicy | None = None,
        fallback_provider: BaseLLMProvider | None = None,
        sleep: AsyncSleep = asyncio.sleep,
        timeout: float = 30.0,
    ) -> None:
        self._owns_client = client is None
        self._settings = settings
        self._model = settings.deepseek_model
        self._retry_policy = retry_policy or RetryPolicy()
        self._fallback_provider = fallback_provider
        self._sleep = sleep
        self._client = client
        self._api_key = settings.deepseek_api_key
        self._base_url = str(settings.deepseek_base_url)
        self._timeout = timeout
        self._closed = False

    def _request_client(self) -> Any:
        if self._closed:
            raise RuntimeError("LLM Provider is closed.")
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                api_key=self._api_key.get_secret_value(),
                base_url=self._base_url,
                timeout=self._timeout,
                max_retries=0,
            )
        return self._client

    def supports_vision(self) -> bool:
        """仅声明当前实例模型与官方适配器明确支持的真实图像能力。"""
        return (
            self._model in {"deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"}
            and self._settings.deepseek_base_url.host == "api.deepseek.com"
            and self._fallback_provider is None
        )

    async def aclose(self) -> None:
        self._closed = True
        if self._owns_client and self._client is not None:
            await self._client.close()

    def describe(self, *, prompt_version: str | None = None) -> LLMProviderMetadata:
        """默认模型来自构造后的实例，不再读取配置或猜测 fallback 最终去向。

        非 SDK 客户端身份不明，不将其输出归因给 DeepSeek；存在 fallback 时当前
        适配器没有逐次路由回执，保守记 unknown，保留可证明的方法入口。
        """

        metadata = super().describe(prompt_version=prompt_version)
        identified = self._client is None and self._owns_client
        if self._client is not None:
            from openai import AsyncOpenAI

            identified = isinstance(self._client, AsyncOpenAI)
        if self._fallback_provider is not None or not identified:
            metadata.update({"provider": "unknown", "model": "unknown"})
        else:
            metadata["model"] = self._model.strip() or "unknown"
        return metadata

    async def generate_structured(
        self,
        messages: LLMMessages,
        schema: type[BaseModel],
        model: str | None = None,
    ) -> BaseModel:
        """请求 JSON 对象，解析后通过 Pydantic Schema 校验。"""

        if not isinstance(schema, type) or not issubclass(schema, BaseModel):
            raise TypeError("结构化输出 Schema 必须继承 BaseModel。")

        image_input = any(
            isinstance(message.get("content"), Sequence)
            and not isinstance(message.get("content"), str)
            and any(isinstance(part, Mapping) and part.get("type") in {"image", "image_url", "input_image"}
                    for part in message["content"])
            for message in messages
        )
        if image_input and (not self.supports_vision() or (model is not None and model != self._model)):
            raise ProviderCallError("VISION_NOT_SUPPORTED", "当前实际配置模型不支持本次图像输入。")
        prepared_messages = self._prepare_messages(messages)

        async def request() -> BaseModel:
            return await self._request_json(prepared_messages, schema, model)

        fallback = None
        fallback_provider = self._fallback_provider
        if fallback_provider is not None:

            async def use_fallback() -> BaseModel:
                return await fallback_provider.generate_structured(
                    messages,
                    schema,
                    model=model,
                )

            fallback = use_fallback

        return await self._retry_policy.execute(
            request,
            fallback=fallback,
            sleep=self._sleep,
        )

    async def _request_json(
        self,
        messages: list[ChatCompletionMessageParam],
        schema: type[BaseModel],
        model: str | None,
    ) -> BaseModel:
        from backend.app.services.trace_service import record_trace

        started_at = perf_counter()
        try:
            response = await self._request_client().chat.completions.create(
                messages=messages,
                model=model or self._model,
                response_format={"type": "json_object"},
            )
        except Exception as error:
            record_trace(
                agent_type="llm", status="failure", started_at=started_at,
                model=self.describe()["model"],
                error_code=getattr(error, "code", "ProviderFailed"),
                error_retryable=getattr(error, "retryable", False),
            )
            raise
        usage = getattr(response, "usage", None)
        tokens = {
            "input": getattr(usage, "prompt_tokens", None),
            "output": getattr(usage, "completion_tokens", None),
            "total": getattr(usage, "total_tokens", None),
        }

        def trace_invalid(code: str = "StructuredOutputFailed") -> None:
            record_trace(
                agent_type="llm", status="failure", started_at=started_at,
                model=self.describe()["model"], tokens=tokens,
                error_code=code, error_retryable=True,
            )

        content = self._extract_content(response)
        if content is None or not content.strip():
            trace_invalid("ProviderEmptyResponse")
            raise ProviderCallError(
                "ProviderEmptyResponse",
                "LLM Provider 返回为空。",
                retryable=True,
            )

        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            trace_invalid()
            raise ProviderCallError(
                "StructuredOutputFailed",
                "LLM Provider 返回的 JSON 无法解析。",
                retryable=True,
                retry_limit=1,
            ) from None

        if not isinstance(payload, Mapping):
            trace_invalid()
            raise ProviderCallError(
                "StructuredOutputFailed",
                "LLM Provider 返回的 JSON 必须是对象。",
                retryable=True,
                retry_limit=1,
            )

        try:
            parsed = schema.model_validate(payload)
        except ValidationError:
            trace_invalid()
            raise ProviderCallError(
                "StructuredOutputFailed",
                "LLM Provider 返回未通过结构化校验。",
                retryable=True,
                retry_limit=1,
            ) from None
        record_trace(
            agent_type="llm", status="success", started_at=started_at,
            model=self.describe()["model"], tokens=tokens,
        )
        return parsed

    @staticmethod
    def _extract_content(response: Any) -> str | None:
        try:
            choices = response.choices
            if not choices:
                return None
            content = choices[0].message.content
        except (AttributeError, IndexError, TypeError):
            return None
        return content if isinstance(content, str) else None

    @staticmethod
    def _prepare_messages(
        messages: LLMMessages,
    ) -> list[ChatCompletionMessageParam]:
        prepared: list[ChatCompletionMessageParam] = []
        for message in messages:
            converted = dict(message)
            content = message.get("content")
            if isinstance(content, Sequence) and not isinstance(content, str):
                blocks: list[dict[str, Any]] = []
                for block in content:
                    if not isinstance(block, Mapping):
                        raise ProviderCallError("VISION_IMAGE_TRANSPORT_UNAVAILABLE", "多模态内容块结构无效。")
                    if block.get("type") == "image":
                        if message.get("role") != "user":
                            raise ProviderCallError("VISION_IMAGE_TRANSPORT_UNAVAILABLE", "图像仅允许在 user 消息中传输。")
                        try:
                            raw = block.get("image")
                            image = raw if isinstance(raw, ProviderImage) else ProviderImage.model_validate(raw)
                            if image.encoding != "base64" or image.mime_type not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
                                raise ValueError("unsupported transport")
                            base64.b64decode(image.value, validate=True)
                        except (ValidationError, ValueError, binascii.Error):
                            raise ProviderCallError("VISION_IMAGE_TRANSPORT_UNAVAILABLE", "适配器没有可用的授权 Base64 图像传输。") from None
                        blocks.append({"type": "image_url", "image_url": {"url": f"data:{image.mime_type};base64,{image.value}"}})
                    elif block.get("type") in {"image_url", "input_image"}:
                        raise ProviderCallError("VISION_IMAGE_TRANSPORT_UNAVAILABLE", "图像须使用应用边界的 ProviderImage，不接受外部地址。")
                    else:
                        blocks.append(dict(block))
                converted["content"] = blocks
            prepared.append(cast("ChatCompletionMessageParam", converted))
        has_json_instruction = any(
            "json" in str(message.get("content", "")).lower() for message in prepared
        )
        if not has_json_instruction:
            prepared.insert(
                0,
                cast(
                    "ChatCompletionMessageParam",
                    {"role": "system", "content": _JSON_INSTRUCTION},
                ),
            )
        return prepared


from .factory import register_llm_provider

register_llm_provider("deepseek", DeepSeekProvider, replace=True)


__all__ = ["DeepSeekProvider"]
