"""TCR-023：业务-only AppSettings 的显式 none 禁用态（T220-Fix）。"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from backend.app.ai.errors import AIConfigurationError
from backend.app.ai.llm.factory import create_llm_provider
from backend.app.core.config import AppSettings


def payload(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "database_url": "postgresql+psycopg://synthetic:secret@127.0.0.1:1/eduagent",
        "redis_url": "redis://127.0.0.1:1/0",
        "confidence_threshold": 0.8,
        "JWT_SECRET_KEY": "synthetic-jwt-secret-that-is-at-least-32-chars",
        "llm_provider": "none",
        "deepseek_api_key": SecretStr(""),
        "deepseek_base_url": None,
        "deepseek_model": "",
        "embedding_provider": "none",
        "embedding_model": None,
        "embedding_base_url": None,
        "embedding_api_key": None,
        "embedding_dimension": 1024,
        "rerank_provider": "none",
        "rerank_model": None,
        "vision_model": None,
        "ocr_enabled": False,
        "ocr_provider": None,
        "ocr_model": None,
        "ocr_model_dir": None,
    }
    values.update(overrides)
    return values


def test_business_only_settings_are_explicitly_disabled() -> None:
    settings = AppSettings(_env_file=None, **payload())

    assert settings.llm_provider == "none"
    assert settings.embedding_provider == "none"
    assert settings.rerank_provider == "none"
    assert settings.llm_configured is False
    assert settings.embedding_configured is False
    assert settings.deepseek_api_key.get_secret_value() == ""
    assert settings.deepseek_base_url is None
    assert settings.vision_model is None


def test_business_only_llm_factory_gate_is_explicit() -> None:
    settings = AppSettings(_env_file=None, **payload())

    with pytest.raises(AIConfigurationError) as error:
        create_llm_provider(settings)

    assert error.value.code == "AI_NOT_CONFIGURED"
    assert error.value.status_code == 503
    assert "sk-" not in str(error.value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"llm_provider": "deepseek", "deepseek_api_key": SecretStr("")},
        {
            "llm_provider": "deepseek",
            "deepseek_api_key": SecretStr("synthetic-key"),
            "deepseek_base_url": None,
        },
        {
            "llm_provider": "deepseek",
            "deepseek_api_key": SecretStr("synthetic-key"),
            "deepseek_base_url": "https://api.deepseek.com",
            "deepseek_model": "",
        },
    ],
)
def test_configured_llm_still_requires_key_url_and_model(
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        AppSettings(_env_file=None, **payload(**overrides))


def test_none_cannot_smuggle_ai_fields() -> None:
    with pytest.raises(ValidationError):
        AppSettings(
            _env_file=None,
            **payload(deepseek_api_key=SecretStr("synthetic-key")),
        )
    with pytest.raises(ValidationError):
        AppSettings(
            _env_file=None,
            **payload(deepseek_base_url="https://example.invalid/v1"),
        )
    with pytest.raises(ValidationError):
        AppSettings(_env_file=None, **payload(deepseek_model="synthetic-model"))
    with pytest.raises(ValidationError):
        AppSettings(
            _env_file=None,
            **payload(embedding_model="synthetic-embedding"),
        )
