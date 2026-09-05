"""DeepSeek Chat Completions 的 JSON 结构化输出适配器。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any, cast

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, ValidationError

from backend.app.core.config import AppSettings
from backend.app.core.retry_policy import (
    AsyncSleep,
    ProviderCallError,
    RetryPolicy,
)

from .base import BaseLLMProvider, LLMMessages

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
        self._settings = settings
        self._model = settings.deepseek_model
        self._retry_policy = retry_policy or RetryPolicy()
        self._fallback_provider = fallback_provider
        self._sleep = sleep
        self._client = client or AsyncOpenAI(
            api_key=settings.deepseek_api_key.get_secret_value(),
            base_url=str(settings.deepseek_base_url),
            timeout=timeout,
            max_retries=0,
        )

    async def generate_structured(
        self,
        messages: LLMMessages,
        schema: type[BaseModel],
        model: str | None = None,
    ) -> BaseModel:
        """请求 JSON 对象，解析后通过 Pydantic Schema 校验。"""

        if not isinstance(schema, type) or not issubclass(schema, BaseModel):
            raise TypeError("结构化输出 Schema 必须继承 BaseModel。")

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
        response = await self._client.chat.completions.create(
            messages=messages,
            model=model or self._model,
            response_format={"type": "json_object"},
        )
        content = self._extract_content(response)
        if content is None or not content.strip():
            raise ProviderCallError(
                "ProviderEmptyResponse",
                "LLM Provider 返回为空。",
                retryable=True,
            )

        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            raise ProviderCallError(
                "StructuredOutputFailed",
                "LLM Provider 返回的 JSON 无法解析。",
                retryable=True,
                retry_limit=1,
            ) from None

        if not isinstance(payload, Mapping):
            raise ProviderCallError(
                "StructuredOutputFailed",
                "LLM Provider 返回的 JSON 必须是对象。",
                retryable=True,
                retry_limit=1,
            )

        try:
            return schema.model_validate(payload)
        except ValidationError:
            raise ProviderCallError(
                "StructuredOutputFailed",
                "LLM Provider 返回未通过结构化校验。",
                retryable=True,
                retry_limit=1,
            ) from None

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
        prepared = [
            cast(ChatCompletionMessageParam, dict(message)) for message in messages
        ]
        has_json_instruction = any(
            "json" in str(message.get("content", "")).lower() for message in prepared
        )
        if not has_json_instruction:
            prepared.insert(
                0,
                cast(
                    ChatCompletionMessageParam,
                    {"role": "system", "content": _JSON_INSTRUCTION},
                ),
            )
        return prepared


from .factory import register_llm_provider

register_llm_provider("deepseek", DeepSeekProvider, replace=True)


__all__ = ["DeepSeekProvider"]
