from __future__ import annotations

from typing import Any

from backend.app.core.config import AppSettings


def build_test_settings(**overrides: Any) -> AppSettings:
    """构造测试配置；H03 TCR：维度使用应用默认值，避免测试绕过启动门禁。"""

    values: dict[str, Any] = {
        "database_url": "postgresql+psycopg://user:password@localhost:5432/eduagent",
        "redis_url": "redis://localhost:6379/0",
        "llm_provider": "deepseek",
        "deepseek_api_key": "test-key",
        "deepseek_base_url": "https://api.deepseek.com",
        "deepseek_model": "deepseek-chat",
        "embedding_provider": "local",
        "embedding_model": "test-embedding-model",
        "embedding_base_url": "https://embedding.example.com/v1",
        "embedding_api_key": "test-embedding-api-key",
        "rerank_provider": "none",
        "confidence_threshold": 0.8,
        "JWT_SECRET_KEY": "test-jwt-secret-that-is-not-a-production-secret",
    }
    values.update(overrides)
    return AppSettings.model_validate(values)
