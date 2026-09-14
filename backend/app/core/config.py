"""Typed runtime configuration for EduAgent.

The module validates required runtime values and exposes redacted views so
secrets do not leak into logs or error responses.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, ClassVar, Final
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

# DocumentChunk 的 PostgreSQL 迁移固定为 vector(1024)，应用配置必须与之保持一致。
EMBEDDING_DIMENSION_DEFAULT: Final[int] = 1024


class ConfigurationError(RuntimeError):
    """Safe, user-facing configuration validation error."""

    def __init__(self, message: str, fields: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.fields = fields

    @classmethod
    def from_validation_error(cls, error: ValidationError) -> ConfigurationError:
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
            message = f"运行配置无效：{', '.join(fields)}。"
            if "JWT_SECRET_KEY" in fields:
                message += "JWT_SECRET_KEY 必须配置为至少 32 个字符的非空密钥。"
            return cls(message, fields)
        return cls("运行配置无效。", fields)


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
        hide_input_in_errors=True,
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
    embedding_model: str | None = None
    embedding_dimension: int = Field(default=EMBEDDING_DIMENSION_DEFAULT, gt=0)
    embedding_base_url: AnyUrl | None = None
    embedding_api_key: SecretStr | None = None
    rerank_provider: str
    #: 未配置时，LLM 沿用 DEEPSEEK_MODEL，Cross Encoder 使用自身默认模型。
    rerank_model: str | None = None
    confidence_threshold: float = Field(ge=0.0, le=1.0)
    #: Hybrid 检索中向量路的权重 α；关键词路占 1-α。
    hybrid_vector_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    #: 单次 Rerank 的候选上限，用于控制 LLM token 消耗。
    rerank_max_candidates: int = Field(default=20, gt=0)
    #: LLM Rerank 单次调用超时（秒）。
    rerank_timeout_seconds: float = Field(default=30.0, gt=0)
    JWT_SECRET_KEY: SecretStr
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = Field(default=60, gt=0)
    DEV_MODE: bool = False

    @property
    def dev_mode(self) -> bool:
        """返回开发模式开关，兼容 Python 代码中的小写属性访问。"""

        return self.DEV_MODE

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

    @field_validator("JWT_SECRET_KEY")
    @classmethod
    def _validate_jwt_secret_key(cls, value: SecretStr) -> SecretStr:
        """拒绝空白和过短的 JWT 密钥，不在错误中输出输入值。"""

        secret = value.get_secret_value()
        if not secret.strip() or len(secret) < 32:
            raise ValueError("JWT_SECRET_KEY 必须为至少 32 个字符的非空密钥。")
        return value

    @field_validator("JWT_ALGORITHM")
    @classmethod
    def _validate_jwt_algorithm(cls, value: str) -> str:
        """当前签名实现只支持 HS256，禁止配置与实际算法不一致。"""

        if value != "HS256":
            raise ValueError("JWT_ALGORITHM 当前仅支持 HS256。")
        return value

    @field_validator("embedding_model", "embedding_base_url", "rerank_model", mode="before")
    @classmethod
    def _normalize_optional_embedding_text(cls, value: Any) -> Any:
        """将空的或占位形式的模型配置规范化为未配置。"""

        if value is None:
            return None
        text = str(value).strip()
        if text.lower() in _PLACEHOLDER_PROVIDER_VALUES:
            return None
        return text

    @field_validator("embedding_api_key", mode="before")
    @classmethod
    def _normalize_optional_api_key(cls, value: Any) -> Any:
        """空值或占位值一律视为未配置密钥。"""

        if value is None:
            return None
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        if str(value).strip().lower() in _PLACEHOLDER_PROVIDER_VALUES:
            return None
        return value

    @field_validator("embedding_base_url")
    @classmethod
    def _validate_embedding_base_url(cls, value: AnyUrl | None) -> AnyUrl | None:
        if value is not None and value.scheme not in {"http", "https"}:
            raise ValueError("must use http or https")
        return value

    @field_validator("deepseek_base_url")
    @classmethod
    def _validate_base_url(cls, value: AnyUrl) -> AnyUrl:
        if value.scheme not in {"http", "https"}:
            raise ValueError("must use http or https")
        return value

    @model_validator(mode="after")
    def _validate_supported_providers(self) -> AppSettings:
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
            "embedding_model": self.embedding_model,
            "embedding_dimension": self.embedding_dimension,
            "embedding_base_url": (
                _redact_url(self.embedding_base_url)
                if self.embedding_base_url is not None
                else None
            ),
            "embedding_api_key": (
                REDACTED_VALUE if self.embedding_api_key is not None else None
            ),
            "rerank_provider": self.rerank_provider,
            "rerank_model": self.rerank_model,
            "confidence_threshold": self.confidence_threshold,
            "JWT_SECRET_KEY": REDACTED_VALUE,
            "JWT_ALGORITHM": self.JWT_ALGORITHM,
            "JWT_EXPIRE_MINUTES": self.JWT_EXPIRE_MINUTES,
            "DEV_MODE": self.DEV_MODE,
        }


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    """Load runtime configuration once and cache the validated result."""

    try:
        return AppSettings()  # type: ignore[call-arg]
    except (
        ValidationError
    ) as exc:  # pragma: no cover - exercised by startup validation later
        raise ConfigurationError.from_validation_error(exc) from None


def reset_settings_cache() -> None:
    """Clear cached settings for tests or process restarts."""

    get_settings.cache_clear()


__all__ = [
    "EMBEDDING_DIMENSION_DEFAULT",
    "REDACTED_VALUE",
    "AppSettings",
    "ConfigurationError",
    "get_settings",
    "reset_settings_cache",
]
