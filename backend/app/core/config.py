"""Typed runtime configuration for EduAgent.

The module validates required runtime values and exposes redacted views so
secrets do not leak into logs or error responses.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, ClassVar
from urllib.parse import urlsplit, urlunsplit

from pydantic import (
    AnyUrl,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.networks import PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict

REDACTED_VALUE = "[REDACTED]"
_PROVIDER_NAME_PATTERN = r"^[a-z][a-z0-9_-]*$"
_PLACEHOLDER_PROVIDER_VALUES = {
    "",
    "change-me",
    "example",
    "placeholder",
    "replace-me",
    "sample",
    "tbd",
    "todo",
}


class ConfigurationError(RuntimeError):
    """Safe, user-facing configuration validation error."""

    def __init__(self, message: str, fields: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.fields = fields

    @classmethod
    def from_validation_error(cls, error: ValidationError) -> "ConfigurationError":
        fields = tuple(
            sorted(
                {
                    ".".join(
                        str(part) for part in issue["loc"] if part not in (None, "")
                    ).upper()
                    or "CONFIG"
                    for issue in error.errors()
                }
            )
        )
        if fields:
            return cls(f"Invalid runtime configuration: {', '.join(fields)}", fields)
        return cls("Invalid runtime configuration", fields)


def _normalize_text(value: Any) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError("must not be empty")
    return text


def _normalize_provider_name(value: Any) -> str:
    text = _normalize_text(value).lower()
    if text in _PLACEHOLDER_PROVIDER_VALUES:
        raise ValueError("must not be a placeholder")
    if not re.match(_PROVIDER_NAME_PATTERN, text):
        raise ValueError("must use lowercase letters, numbers, hyphen, or underscore")
    return text


def _redact_url(url: AnyUrl | PostgresDsn | RedisDsn | str) -> str:
    parsed = urlsplit(str(url))
    hostname = parsed.hostname or ""
    if hostname and ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    if parsed.port:
        hostname = f"{hostname}:{parsed.port}" if hostname else f":{parsed.port}"
    return urlunsplit((parsed.scheme, hostname, parsed.path, "", ""))


class AppSettings(BaseSettings):
    """Typed application settings loaded from the environment or `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        env_prefix="",
    )

    SUPPORTED_LLM_PROVIDERS: ClassVar[frozenset[str]] = frozenset(
        {"deepseek", "openai_compatible"}
    )
    SUPPORTED_EMBEDDING_PROVIDERS: ClassVar[frozenset[str]] = frozenset(
        {"bge", "deepseek", "huggingface", "local", "openai_compatible"}
    )
    SUPPORTED_RERANK_PROVIDERS: ClassVar[frozenset[str]] = frozenset(
        {"cross_encoder", "llm", "none", "openai_compatible"}
    )

    database_url: PostgresDsn
    redis_url: RedisDsn
    llm_provider: str
    deepseek_api_key: SecretStr
    deepseek_base_url: AnyUrl
    deepseek_model: str
    embedding_provider: str
    rerank_provider: str
    confidence_threshold: float = Field(ge=0.0, le=1.0)

    @field_validator(
        "llm_provider", "embedding_provider", "rerank_provider", mode="before"
    )
    @classmethod
    def _validate_provider_name(cls, value: Any) -> str:
        return _normalize_provider_name(value)

    @field_validator("deepseek_model", mode="before")
    @classmethod
    def _validate_model_name(cls, value: Any) -> str:
        return _normalize_text(value)

    @field_validator("deepseek_api_key")
    @classmethod
    def _validate_api_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("deepseek_base_url")
    @classmethod
    def _validate_base_url(cls, value: AnyUrl) -> AnyUrl:
        if value.scheme not in {"http", "https"}:
            raise ValueError("must use http or https")
        return value

    @model_validator(mode="after")
    def _validate_supported_providers(self) -> "AppSettings":
        checks = (
            ("LLM_PROVIDER", self.llm_provider, self.SUPPORTED_LLM_PROVIDERS),
            (
                "EMBEDDING_PROVIDER",
                self.embedding_provider,
                self.SUPPORTED_EMBEDDING_PROVIDERS,
            ),
            ("RERANK_PROVIDER", self.rerank_provider, self.SUPPORTED_RERANK_PROVIDERS),
        )
        unsupported = [name for name, value, allowed in checks if value not in allowed]
        if unsupported:
            raise ValueError(
                f"unsupported provider selection: {', '.join(unsupported)}"
            )
        return self

    def public_dict(self) -> dict[str, Any]:
        """Return a log-safe view of the active configuration."""

        return {
            "database_url": _redact_url(self.database_url),
            "redis_url": _redact_url(self.redis_url),
            "llm_provider": self.llm_provider,
            "deepseek_api_key": REDACTED_VALUE,
            "deepseek_base_url": _redact_url(self.deepseek_base_url),
            "deepseek_model": self.deepseek_model,
            "embedding_provider": self.embedding_provider,
            "rerank_provider": self.rerank_provider,
            "confidence_threshold": self.confidence_threshold,
        }


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    """Load runtime configuration once and cache the validated result."""

    try:
        return AppSettings()
    except (
        ValidationError
    ) as exc:  # pragma: no cover - exercised by startup validation later
        raise ConfigurationError.from_validation_error(exc) from None


def reset_settings_cache() -> None:
    """Clear cached settings for tests or process restarts."""

    get_settings.cache_clear()


__all__ = [
    "AppSettings",
    "ConfigurationError",
    "REDACTED_VALUE",
    "get_settings",
    "reset_settings_cache",
]
