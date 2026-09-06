from __future__ import annotations

from typing import Any

from backend.app.core.config import AppSettings


def build_test_settings(**overrides: Any) -> AppSettings:
    """构造测试用运行配置，并保留 Pydantic 的运行时校验。"""

    values: dict[str, Any] = {
        "database_url": "postgresql+psycopg://user:password@localhost:5432/eduagent",
        "redis_url": "redis://localhost:6379/0",
        "llm_provider": "deepseek",
        "deepseek_api_key": "test-key",
        "deepseek_base_url": "https://api.deepseek.com",
        "deepseek_model": "deepseek-chat",
        "embedding_provider": "local",
        "rerank_provider": "none",
        "confidence_threshold": 0.8,
    }
    values.update(overrides)
    return AppSettings.model_validate(values)
