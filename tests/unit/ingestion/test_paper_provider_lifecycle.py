"""Provider ownership is closed by the new per-import executor; injected clients stay caller-owned."""

import asyncio

from backend.app.ai.llm import deepseek
from tests.unit.settings_helpers import build_test_settings


def test_close_only_provider_owned_client(monkeypatch):
    closed = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def close(self):
            closed.append(self)

    monkeypatch.setattr(deepseek, "AsyncOpenAI", Client)
    owned = deepseek.DeepSeekProvider(build_test_settings())
    external = Client()
    injected = deepseek.DeepSeekProvider(build_test_settings(), client=external)
    asyncio.run(owned.aclose())
    asyncio.run(injected.aclose())
    assert closed == [owned._client]
