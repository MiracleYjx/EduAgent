"""与具体模型无关的 LLM Provider 抽象契约。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from pydantic import BaseModel

type LLMMessage = Mapping[str, Any]
type LLMMessages = Sequence[LLMMessage]


class BaseLLMProvider(ABC):
    """所有 LLM Provider 共同遵守的最小接口。"""

    provider_name: ClassVar[str] = "unknown"

    @abstractmethod
    async def generate_structured(
        self,
        messages: LLMMessages,
        schema: type[BaseModel],
        model: str | None = None,
    ) -> BaseModel:
        """根据消息生成并返回经过校验的结构化结果。"""

        raise NotImplementedError


__all__ = ["BaseLLMProvider", "LLMMessage", "LLMMessages"]
