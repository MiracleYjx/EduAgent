"""Provider ownership is closed by the new per-import executor; injected clients stay caller-owned."""

import asyncio
from types import SimpleNamespace

import openai
from pydantic import BaseModel

from backend.app.ai.llm import deepseek
from tests.unit.settings_helpers import build_test_settings


def test_close_only_provider_owned_client(monkeypatch):
    closed = []

    class Result(BaseModel):
        answer: int

    class Client:
        def __init__(self, **kwargs):
            self.chat = self
            self.completions = self

        async def create(self, **kwargs):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(message=SimpleNamespace(content='{"answer": 7}'))
                ],
                usage=None,
            )

        async def close(self):
            closed.append(self)

    monkeypatch.setattr(openai, "AsyncOpenAI", Client)
    owned = deepseek.DeepSeekProvider(build_test_settings())
    external = Client()
    injected = deepseek.DeepSeekProvider(build_test_settings(), client=external)

    async def use_and_close():
        result = await owned.generate_structured(
            [{"role": "user", "content": "Compute 3+4."}], Result
        )
        assert result.answer == 7
        await owned.aclose()

    asyncio.run(use_and_close())
    asyncio.run(injected.aclose())
    assert closed == [owned._client]
