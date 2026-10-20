"""Typed runtime configuration for EduAgent.

The module validates required runtime values and exposes redacted views so
secrets do not leak into logs or error responses.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import (
    AliasChoices,
    AnyUrl,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.networks import PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.app.core.environment import resolve_environment_profile

if TYPE_CHECKING:
    from backend.app.schemas.settings import ResolvedConfiguration

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


def default_storage_root() -> Path:
    """Use the selected profile, retaining unadopted Development storage."""

    return resolve_environment_profile().storage_root


class _ProviderNames:
    """Compatibility attribute backed by executable Registry records, not a list."""

    def __init__(self, kind: str) -> None:
        self.kind = kind

    def __get__(self, instance: object, owner: type | None = None) -> frozenset[str]:
        from backend.app.services.settings.provider_registry import (
            get_provider_registry,
        )

        names = {entry.adapter_id for entry in get_provider_registry().entries(kind=self.kind)}
        if self.kind == "rerank":
            names.add("none")  # Disabled state, never a fabricated runnable entry.
        return frozenset(names)


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

    SUPPORTED_LLM_PROVIDERS: ClassVar[_ProviderNames] = _ProviderNames("llm")
    SUPPORTED_EMBEDDING_PROVIDERS: ClassVar[_ProviderNames] = _ProviderNames("embedding")
    SUPPORTED_RERANK_PROVIDERS: ClassVar[_ProviderNames] = _ProviderNames("rerank")

    storage_root: Path = Field(
        default_factory=default_storage_root,
        validation_alias=AliasChoices("storage_root", "STORAGE_ROOT"),
    )

    # OCR 按需启用；选型/资源校验在扫描处理边界，不阻塞旧文字服务。
    ocr_enabled: bool = False
    ocr_provider: str | None = None
    ocr_model: str | None = None
    ocr_model_dir: Path | None = None

    @field_validator("ocr_provider", "ocr_model", "ocr_model_dir", mode="before")
    @classmethod
    def _normalize_optional_ocr_text(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    database_url: PostgresDsn
    redis_url: RedisDsn
    #: 显式 `none` 表示本环境未配置 LLM；不是默认付费模型，也不填假 Key。
    llm_provider: str = "none"
    llm_auth_mode: Literal["api_key", "none"] = "api_key"
    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_base_url: AnyUrl | None = None
    deepseek_model: str = ""
    # 图像使用显式独立模型；缺省不自动切换旧文字模型。
    vision_model: str | None = None
    embedding_provider: str = "none"
    embedding_model: str | None = None
    embedding_dimension: int = Field(default=EMBEDDING_DIMENSION_DEFAULT, gt=0)
    embedding_base_url: AnyUrl | None = None
    embedding_api_key: SecretStr | None = None
    rerank_provider: str = "none"
    #: 未配置时，LLM 沿用 DEEPSEEK_MODEL，Cross Encoder 使用自身默认模型。
    rerank_model: str | None = None
    confidence_threshold: float = Field(ge=0.0, le=1.0)
    #: Hybrid 检索中向量路的权重 α；关键词路占 1-α。
    hybrid_vector_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    #: 单次 Rerank 的候选上限，用于控制 LLM token 消耗。
    rerank_max_candidates: int = Field(default=5, gt=0)
    #: 该上限同时决定主观题评分链路（backend/app/services/grading/grading_context.py）
    #: Hybrid 的每路召回数与融合候选数，调小会同步收缩召回、融合与重排规模。
    #: LLM Rerank 单次调用超时（秒）。
    rerank_timeout_seconds: float = Field(default=30.0, gt=0)
    JWT_SECRET_KEY: SecretStr
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = Field(default=60, gt=0)
    DEV_MODE: bool = False

    @field_validator("storage_root", mode="before")
    @classmethod
    def _validate_storage_root(cls, value: Any) -> Path:
        if not str(value).strip():
            raise ValueError("storage root must not be empty")
        path = Path(value).expanduser()
        # Normalize on the execution host; Compose uses its container path syntax.
        if any(name in os.environ for name in ("EDUAGENT_ENVIRONMENT", "EDUAGENT_DATA_ROOT", "EDUAGENT_RUNTIME_KIND")):
            return resolve_environment_profile(storage_root=path).storage_root
        return path.resolve()

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
        # 显式 none 下允许空模型；已配置 LLM 时由模型级校验拒绝空值。
        if value is None or not str(value).strip():
            return ""
        return _normalize_text(value)

    @field_validator("deepseek_api_key")
    @classmethod
    def _validate_api_key(cls, value: SecretStr, info: Any) -> SecretStr:
        # 只有已选择 LLM 时才要求非空；显式 none 下空值就是“未配置”。
        if (info.data.get("llm_provider", "none") != "none"
                and info.data.get("llm_auth_mode", "api_key") == "api_key"
                and not value.get_secret_value().strip()):
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

    @field_validator("embedding_model", "embedding_base_url", "rerank_model", "vision_model", mode="before")
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
    def _validate_base_url(cls, value: AnyUrl | None) -> AnyUrl | None:
        if value is not None and value.scheme not in {"http", "https"}:
            raise ValueError("must use http or https")
        return value

    @property
    def llm_configured(self) -> bool:
        """LLM 是否被显式启用（`none` 是未配置，不是可运行 Provider）。"""

        return self.llm_provider != "none"

    @property
    def embedding_configured(self) -> bool:
        return self.embedding_provider != "none"

    @model_validator(mode="after")
    def _validate_supported_providers(self) -> AppSettings:
        # `none` 是显式禁用态（与 rerank 一致），不计算成伪造 Provider。
        checks = (
            ("LLM_PROVIDER", self.llm_provider, self.SUPPORTED_LLM_PROVIDERS | {"none"}),
            (
                "EMBEDDING_PROVIDER",
                self.embedding_provider,
                self.SUPPORTED_EMBEDDING_PROVIDERS | {"none"},
            ),
            ("RERANK_PROVIDER", self.rerank_provider, self.SUPPORTED_RERANK_PROVIDERS),
        )
        # Historical settings may select DeepSeek Embedding so its existing
        # factory can emit the explicit unsupported-API error. This is not a
        # supported preset and never appears in Registry/UI enumerations.
        unsupported = [name for name, value, allowed in checks if value not in allowed
                       and not (name == "EMBEDDING_PROVIDER" and value == "deepseek")]
        if unsupported:
            raise ValueError(
                f"unsupported provider selection: {', '.join(unsupported)}"
            )
        if self.llm_configured:
            if self.llm_auth_mode == "none" and self.llm_provider != "openai_compatible":
                raise ValueError("only openai_compatible supports explicit LLM auth_mode=none")
            missing = [
                name
                for name, satisfied in (
                    ("DEEPSEEK_API_KEY", self.llm_auth_mode == "none" or self.deepseek_api_key.get_secret_value().strip()),
                    ("DEEPSEEK_BASE_URL", self.deepseek_base_url is not None),
                    ("DEEPSEEK_MODEL", bool(self.deepseek_model.strip())),
                )
                if not satisfied
            ]
            if missing:
                raise ValueError(
                    f"configured LLM provider requires: {', '.join(missing)}"
                )
        elif (
            self.deepseek_api_key.get_secret_value().strip()
            or self.deepseek_base_url is not None
            or self.deepseek_model.strip()
        ):
            raise ValueError(
                "LLM_PROVIDER=none must not carry a key, base URL or model"
            )
        if not self.embedding_configured and (
            self.embedding_model
            or self.embedding_base_url is not None
            or self.embedding_api_key is not None
        ):
            raise ValueError(
                "EMBEDDING_PROVIDER=none must not carry a model, base URL or key"
            )
        return self

    def public_dict(self) -> dict[str, Any]:
        """Return a log-safe view of the active configuration."""

        return {
            "storage_root": str(self.storage_root),
            "ocr_enabled": self.ocr_enabled,
            "ocr_provider": self.ocr_provider,
            "ocr_model": self.ocr_model,
            # 模型目录仅供内部部署；不向日志公开本机路径。
            "database_url": _redact_url(self.database_url),
            "redis_url": _redact_url(self.redis_url),
            "llm_provider": self.llm_provider,
            "deepseek_api_key": REDACTED_VALUE,
            "deepseek_base_url": (
                _redact_url(self.deepseek_base_url)
                if self.deepseek_base_url is not None
                else None
            ),
            "deepseek_model": self.deepseek_model,
            "vision_model": self.vision_model,
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


#: 已激活配置的进程内投影；只在设置启动协调里安装，不进进程环境/文件。
_runtime_settings: AppSettings | None = None


def install_runtime_settings(settings: AppSettings | None) -> None:
    """把已激活配置交给业务消费者；不写 env、不读仓库 `.env`。

    业务模块都通过同一个 ``get_settings`` 函数取得设置，因此这里不需要改
    各消费者；调用方必须先完成激活与前置检查。
    """

    global _runtime_settings
    _runtime_settings = settings
    get_settings.cache_clear()


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    """Load runtime configuration once and cache the validated result."""

    if _runtime_settings is not None:
        return _runtime_settings
    try:
        return AppSettings()  # type: ignore[call-arg]
    except (
        ValidationError
    ) as exc:  # pragma: no cover - exercised by startup validation later
        raise ConfigurationError.from_validation_error(exc) from None


def reset_settings_cache() -> None:
    """Clear cached settings and any installed runtime projection."""

    global _runtime_settings
    _runtime_settings = None
    get_settings.cache_clear()


__all__ = [
    "EMBEDDING_DIMENSION_DEFAULT",
    "REDACTED_VALUE",
    "AppSettings",
    "ConfigurationError",
    "get_settings",
    "install_runtime_settings",
    "reset_settings_cache",
]

# New v3 entry points explicitly select non-secret inputs. Legacy AppSettings
# remains the compatibility boundary until bootstrap/credential wiring (T220).
def resolve_configuration(
    *,
    defaults: dict[str, Any] | None = None,
    saved: dict[str, Any] | None = None,
    dotenv_path: Path | None = None,
    process_env: dict[str, str] | None = None,
    saved_source: str = "saved",
) -> ResolvedConfiguration:
    """Resolve selected inputs without opening a DB or parsing process secrets."""
    import json

    from dotenv import dotenv_values
    from pydantic import TypeAdapter

    from backend.app.schemas.settings import (
        BootstrapOptions,
        BusinessSettings,
        ConfigurationValues,
        FieldSource,
        Policies,
        ProviderProfile,
        ResolvedConfiguration,
        SegmentError,
        TaskBinding,
    )

    if saved_source not in {"saved", "mounted_config"}:
        raise ValueError("INVALID_SAVED_SOURCE")
    merged: dict[str, Any] = {}
    sources: dict[str, FieldSource] = {}
    errors: list[SegmentError] = []

    def merge(target: dict, values: dict, source: str, prefix: str = "") -> None:
        for key, value in values.items():
            field = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict) and value:
                # An object overlay supersedes the earlier scalar/null source;
                # provenance now belongs to its individual leaf fields.
                sources.pop(field, None)
                if not isinstance(target.get(key), dict):
                    target[key] = {}
                merge(target[key], value, source, field)
            else:
                target[key] = value
                # A scalar overlay also supersedes any descendant sources.
                for old in tuple(sources):
                    if old.startswith(field + "."):
                        del sources[old]
                sources[field] = FieldSource.model_validate({"source": source})

    merge(merged, ConfigurationValues().model_dump(mode="json"), "default")
    merge(merged, defaults or {}, "default")
    merge(merged, saved or {}, saved_source)
    aliases = {
        "DEV_MODE": "business.dev_mode",
        "JWT_ALGORITHM": "business.jwt_algorithm",
        "JWT_EXPIRE_MINUTES": "business.jwt_expire_minutes",
        "STORAGE_ROOT": "business.storage_root",
    }
    secret_keys = {
        "DEEPSEEK_API_KEY", "EMBEDDING_API_KEY", "JWT_SECRET_KEY",
        "DATABASE_URL", "REDIS_URL",
    }

    def named_source(values: dict, source: str) -> dict:
        result: dict = {}
        for key, value in values.items():
            upper = key.upper()
            field = aliases.get(upper)
            if upper.startswith("EDUAGENT__"):
                field = upper[len("EDUAGENT__"):].lower().replace("__", ".")
            if source == "dotenv" and upper in secret_keys:
                errors.append(SegmentError(segment="source", fields=(upper,), code="SECRET_IN_DOTENV"))
                continue
            if not field:
                continue
            if any(part in {"api_key", "password", "jwt_secret_key", "secret"} for part in field.split(".")):
                errors.append(SegmentError(segment="source", fields=(field,), code="PLAINTEXT_SECRET_REJECTED"))
                continue
            if value is None:
                # A selected empty value is invalid rather than absent.
                value = ""
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except (ValueError, TypeError):
                    pass
            cursor = result
            parts = field.split(".")
            for part in parts[:-1]:
                child = cursor.get(part)
                if not isinstance(child, dict):
                    child = {}
                    cursor[part] = child
                cursor = child
            cursor[parts[-1]] = value
        return result

    if dotenv_path is not None:
        try:
            if not dotenv_path.is_file():
                raise OSError("missing explicit file")
            selected = dict(dotenv_values(dotenv_path, interpolate=False))
        except OSError:
            errors.append(SegmentError(segment="source", fields=("dotenv",), code="DOTENV_UNAVAILABLE"))
        else:
            merge(merged, named_source(selected, "dotenv"), "dotenv")
    if process_env is None:
        # Inspect only declared non-secret names; never materialize a copy of all
        # process values (which may contain inherited real credentials).
        process_env = {
            key: os.environ[key] for key in os.environ
            if key.upper() in aliases or (
                key.upper().startswith("EDUAGENT__")
                and not any(part in {"API_KEY", "PASSWORD", "JWT_SECRET_KEY", "SECRET"}
                            for part in key.upper().split("__"))
            )
        }
    merge(merged, named_source(process_env, "process_env"), "process_env")
    types = {
        "bootstrap": BootstrapOptions, "business": BusinessSettings, "policies": Policies,
        "providers": dict[str, ProviderProfile], "bindings": dict[str, TaskBinding],
    }
    segments: dict[str, Any] = {}
    for segment, value in merged.items():
        if segment not in types:
            errors.append(SegmentError(segment=segment, fields=(segment,)))
            continue
        try:
            segments[segment] = TypeAdapter(types[segment]).validate_python(value)
        except ValidationError as exc:
            issues = exc.errors(include_input=False)
            for issue in issues:
                if issue["type"] == "extra_forbidden":
                    bad_field = ".".join((segment, *(str(part) for part in issue["loc"])))
                    for field in tuple(sources):
                        if field == bad_field or field.startswith(bad_field + "."):
                            del sources[field]
            fields: tuple[str, ...]
            if segment in {"providers", "bindings"}:
                # Dynamic map keys are untrusted identifiers, not safe error fields.
                for field in tuple(sources):
                    if field.startswith(segment + "."):
                        del sources[field]
                fields = (f"{segment}.item",)
            else:
                fields = tuple(sorted({
                    f"{segment}.UNKNOWN_FIELD" if issue["type"] == "extra_forbidden" else
                    ".".join([segment, *(str(part) for part in issue["loc"])])
                    for issue in issues
                }))
            errors.append(SegmentError(segment=segment, fields=fields))
    return ResolvedConfiguration(segments=segments, sources=sources, errors=tuple(errors))
