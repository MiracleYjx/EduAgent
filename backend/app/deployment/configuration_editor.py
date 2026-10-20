"""Local configuration editor, available before database/model startup."""

from __future__ import annotations

import hashlib
import io
import os
import re
import secrets
import stat
import sys
import tempfile
import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid4

from dotenv import dotenv_values
from dotenv.parser import parse_stream
from pydantic import SecretStr, ValidationError
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from backend.app.core.config import (
    AppSettings,
    ConfigurationError,
)
from backend.app.deployment.configuration_validation import validate_configuration
from backend.app.schemas.settings import DatabaseLocator, RedisLocator, SafeModel
from backend.app.services.settings.config_service import ConfigServiceError
from backend.app.services.settings.provider_registry import (
    RegistryValidationError,
    validate_provider_endpoint,
)


@dataclass(frozen=True)
class ConfigField:
    name: str
    label: str
    default: str = ""
    choices: tuple[str, ...] = ()
    secret: bool = False
    directory: bool = False


FIELD_GROUPS: tuple[tuple[str, tuple[ConfigField, ...]], ...] = (
    (
        "连接与存储",
        (
            ConfigField(
                "DATABASE_URL",
                "PostgreSQL 地址 *",
                "postgresql+psycopg://USER:PASSWORD@127.0.0.1:5432/eduagent",
                secret=True,
            ),
            ConfigField(
                "REDIS_URL", "Redis 地址 *", "redis://127.0.0.1:6379/0", secret=True
            ),
            ConfigField("STORAGE_ROOT", "业务数据目录（空为缺省）", directory=True),
        ),
    ),
    (
        "文字与图像模型",
        (
            ConfigField(
                "LLM_PROVIDER",
                "文字 Provider *",
                "deepseek",
                ("deepseek", "openai_compatible"),
            ),
            ConfigField("DEEPSEEK_BASE_URL", "API 地址 *", "https://api.deepseek.com"),
            ConfigField("DEEPSEEK_MODEL", "文字模型 *", "deepseek-chat"),
            ConfigField("DEEPSEEK_API_KEY", "API Key *", secret=True),
            ConfigField("VISION_MODEL", "图像模型（空为关闭）"),
        ),
    ),
    (
        "Embedding",
        (
            ConfigField(
                "EMBEDDING_PROVIDER",
                "Embedding Provider *",
                "openai_compatible",
                ("openai_compatible", "local", "bge", "huggingface"),
            ),
            ConfigField("EMBEDDING_MODEL", "模型名／本地完整目录 *", directory=True),
            ConfigField("EMBEDDING_BASE_URL", "云 Embedding 地址"),
            ConfigField(
                "EMBEDDING_API_KEY", "云 Embedding Key（云模式必填）", secret=True
            ),
            ConfigField(
                "EMBEDDING_DIMENSION", "向量维度（固定 1024）", "1024", ("1024",)
            ),
        ),
    ),
    (
        "检索与评分",
        (
            ConfigField(
                "RERANK_PROVIDER",
                "重排 Provider *",
                "llm",
                ("llm", "cross_encoder", "none", "openai_compatible"),
            ),
            ConfigField("RERANK_MODEL", "重排模型／本地目录", directory=True),
            ConfigField("RERANK_MAX_CANDIDATES", "重排候选上限", "5"),
            ConfigField("RERANK_TIMEOUT_SECONDS", "重排超时（秒）", "30"),
            ConfigField("HYBRID_VECTOR_WEIGHT", "向量检索权重（0–1）", "0.5"),
            ConfigField("CONFIDENCE_THRESHOLD", "评分置信阈值（0–1）*", "0.7"),
        ),
    ),
    (
        "OCR",
        (
            ConfigField("OCR_ENABLED", "启用扫描 OCR", "false", ("false", "true")),
            ConfigField("OCR_PROVIDER", "OCR Provider", "rapidocr", ("rapidocr",)),
            ConfigField(
                "OCR_MODEL", "OCR 模型", "PP-OCRv5-mobile", ("PP-OCRv5-mobile",)
            ),
            ConfigField("OCR_MODEL_DIR", "三份外置权重所在目录", directory=True),
        ),
    ),
    (
        "账号与运行",
        (
            ConfigField("JWT_SECRET_KEY", "JWT 签名密钥（≥32字符）*", secret=True),
            ConfigField("JWT_ALGORITHM", "签名算法", "HS256", ("HS256",)),
            ConfigField("JWT_EXPIRE_MINUTES", "登录有效期（分钟）", "60"),
            ConfigField("DEV_MODE", "演示快速登录", "false", ("false", "true")),
        ),
    ),
)
FIELDS = {field.name: field for _, group in FIELD_GROUPS for field in group}


class _FileSettings(AppSettings):
    """Validate this file alone; never borrow credentials from another source."""

    model_config = SettingsConfigDict(env_file=None)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


def _values(text: str) -> dict[str, str | None]:
    for binding in parse_stream(io.StringIO(text)):
        if binding.error:
            raise ConfigurationError(
                f"配置文件第 {binding.original.line} 行无法解析，请先修正。"
            )
    return {
        key.upper() if key.upper() in FIELDS else key: value
        for key, value in dotenv_values(
            stream=io.StringIO(text), interpolate=False
        ).items()
    }


def _comment(original: str) -> str:
    """Keep inline comments without confusing quoted # characters for comments."""
    quote = ""
    escaped = False
    for index, char in enumerate(original):
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif quote:
            if char == quote:
                quote = ""
        elif char in ("'", '"'):
            quote = char
        elif char == "#" and index and original[index - 1].isspace():
            return "  " + original[index:].rstrip("\r\n")
    return ""


class ConfigDocument:
    def __init__(self, path: Path, template: Path):
        self.path = path.expanduser().resolve()
        self.original = self.path.read_bytes() if self.path.exists() else None
        if self.original is None:
            self.text = template.read_text(encoding="utf-8-sig")
        else:
            self.text = self.original.decode("utf-8-sig")
        self.raw = _values(self.text)
        self.values = {
            name: self.raw.get(name) or field.default for name, field in FIELDS.items()
        }
        # Explicit optional empties must stay empty rather than inherit a UI default.
        for name in FIELDS:
            if name in self.raw and self.raw[name] is not None:
                self.values[name] = self.raw[name] or ""

    def render(self, values: Mapping[str, str]) -> str:
        unknown = set(values) - FIELDS.keys()
        if unknown:
            raise ConfigurationError("配置表单包含未知字段，请重新打开。")
        changes = {
            name: value
            for name, value in values.items()
            if name not in self.raw or value != self.raw[name]
        }
        newline = "\r\n" if "\r\n" in self.text else "\n"
        emitted: set[str] = set()
        output: list[str] = []
        for binding in parse_stream(io.StringIO(self.text)):
            name = (
                binding.key.upper()
                if binding.key and binding.key.upper() in FIELDS
                else binding.key
            )
            if name is None or name not in changes:
                output.append(binding.original.string)
                continue
            if name in emitted:
                continue
            emitted.add(name)
            original = binding.original.string
            prefix = re.match(r"\s*(?:export\s+)?", original)
            leading = prefix.group() if prefix else ""
            comment = _comment(original)
            if name == "STORAGE_ROOT" and not changes[name]:
                output.append(leading + comment.lstrip() + newline)
            else:
                escaped = changes[name].replace("\\", "\\\\").replace("'", "\\'")
                ending = newline if original.endswith(("\r", "\n")) else ""
                output.append(f"{leading}{name}='{escaped}'{comment}{ending}")
        result = "".join(output)
        for name, value in changes.items():
            if name in emitted or (name == "STORAGE_ROOT" and not value):
                continue
            if result and not result.endswith(("\n", "\r")):
                result += newline
            escaped = value.replace("\\", "\\\\").replace("'", "\\'")
            result += f"{name}='{escaped}'{newline}"
        return result

    def save(self, values: Mapping[str, str]) -> None:
        if any(
            "\n" in value or "\r" in value or "\x00" in value
            for value in values.values()
        ):
            raise ConfigurationError("表单配置必须为单行文本，不能包含换行或空字符。")
        rendered = self.render(values)
        parsed = _values(rendered)
        data: dict[str, Any] = {
            name: parsed[name.upper()]
            for name in AppSettings.model_fields
            if name.upper() in parsed and parsed[name.upper()] is not None
        }
        try:
            settings = _FileSettings(**data)
        except ValidationError as exc:
            raise ConfigurationError.from_validation_error(exc) from None
        validate_configuration(settings)
        if getattr(sys, "frozen", False):
            resources = Path(
                getattr(sys, "_MEIPASS", Path(sys.executable).parent)
            ).resolve()
            if self.path.is_relative_to(resources) or self.path.is_relative_to(
                Path(sys.executable).parent.resolve()
            ):
                raise ConfigurationError(
                    "配置必须保存在安装目录之外，请使用用户数据目录。"
                )
        current = self.path.read_bytes() if self.path.exists() else None
        if current != self.original:
            raise ConfigurationError(
                "配置已被其他程序修改，请关闭窗口并重新打开；未覆盖现有文件。"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                dir=self.path.parent,
                prefix=".config-",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(rendered)
                stream.flush()
                os.fsync(stream.fileno())
            mode = (
                stat.S_IMODE(self.path.stat().st_mode)
                if self.original is not None
                else 0o600
            )
            os.chmod(temporary, mode)
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self.original = self.path.read_bytes()
        self.text = rendered
        self.raw = parsed


class ConfigurationWindow:
    """Tk is imported only when the user actually opens the local editor."""

    def __init__(
        self, root: Any, document: ConfigDocument, *, continue_launch: bool = False
    ):
        import tkinter as tk
        from tkinter import ttk

        self.root = root
        self.document = document
        self.saved = False
        self.variables: dict[str, Any] = {}
        self.secret_entries: list[Any] = []
        self.entries: dict[str, Any] = {}
        root.title("EduAgent · 本机配置")
        root.geometry("900x650")
        root.minsize(780, 570)
        root.protocol("WM_DELETE_WINDOW", self.cancel)
        outer = ttk.Frame(root, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(
            outer, text="EduAgent 运行配置", font=("Microsoft YaHei UI", 16, "bold")
        ).pack(anchor="w")
        ttk.Label(outer, text=f"配置文件：{document.path}", wraplength=840).pack(
            anchor="w", pady=(6, 12)
        )
        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True)
        for title, group in FIELD_GROUPS:
            frame = ttk.Frame(notebook, padding=16)
            frame.columnconfigure(1, weight=1)
            notebook.add(frame, text=title)
            for row, field in enumerate(group):
                ttk.Label(frame, text=field.label).grid(
                    row=row, column=0, sticky="w", padx=(0, 12), pady=8
                )
                variable = tk.StringVar(root, value=document.values[field.name])
                self.variables[field.name] = variable
                entry: ttk.Entry
                if field.choices:
                    entry = ttk.Combobox(
                        frame,
                        textvariable=variable,
                        values=field.choices,
                        state="readonly",
                    )
                else:
                    entry = ttk.Entry(
                        frame, textvariable=variable, show="*" if field.secret else ""
                    )
                self.entries[field.name] = entry
                entry.grid(row=row, column=1, sticky="ew", pady=8)
                if field.secret:
                    self.secret_entries.append(entry)
                if field.directory:
                    ttk.Button(
                        frame,
                        text="选择目录",
                        command=partial(self.browse, field.name),
                    ).grid(row=row, column=2, padx=(8, 0))
                if field.name == "JWT_SECRET_KEY":
                    ttk.Button(
                        frame, text="生成密钥", command=self.generate_secret
                    ).grid(row=row, column=2, padx=(8, 0))
            if title in {"连接与存储", "Embedding"}:
                hint = (
                    "数据库地址中的 USER/PASSWORD 是示例，需替换为真实账号和密码；PostgreSQL 与 Redis 须另外安装并运行。"
                    if title == "连接与存储"
                    else "云模式需独立的 Embedding 模型和 Key；本地模式选择完整权重目录，无需云 Key。文字 API Key 不能补齐此项。"
                )
                ttk.Label(frame, text=hint, wraplength=740).grid(
                    row=len(group), column=0, columnspan=3, sticky="w", pady=(8, 0)
                )
        self.show_secrets = tk.BooleanVar(root, value=False)
        ttk.Checkbutton(
            outer,
            text="显示密码和密钥（包括连接地址）",
            variable=self.show_secrets,
            command=self.toggle_secrets,
        ).pack(anchor="w", pady=(10, 6))
        ttk.Label(
            outer,
            text="* 为必填。保存校验格式与当前模型的必填项，不测试连接；模型权重需自行准备。修改已运行服务的配置后需重启。",
            wraplength=840,
        ).pack(anchor="w")
        self.status = tk.StringVar(root)
        ttk.Label(
            outer, textvariable=self.status, foreground="#b42318", wraplength=840
        ).pack(anchor="w", pady=6)
        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(4, 0))
        ttk.Button(buttons, text="取消", command=self.cancel).pack(side="right")
        self.save_button = ttk.Button(
            buttons,
            text="保存并继续启动" if continue_launch else "保存并关闭",
            command=self.save,
        )
        self.save_button.pack(side="right", padx=10)

    def toggle_secrets(self) -> None:
        for entry in self.secret_entries:
            entry.configure(show="" if self.show_secrets.get() else "*")

    def generate_secret(self) -> None:
        self.variables["JWT_SECRET_KEY"].set(secrets.token_urlsafe(32))

    def browse(self, name: str) -> None:
        from tkinter import filedialog

        directory = filedialog.askdirectory(
            parent=self.root, title="选择完整外置目录", mustexist=True
        )
        if directory:
            self.variables[name].set(directory)

    def save(self) -> None:
        try:
            self.document.save(
                {name: variable.get() for name, variable in self.variables.items()}
            )
        except ConfigurationError as exc:
            self.status.set(str(exc))
            return
        except (OSError, UnicodeError) as exc:
            self.status.set(
                f"配置未保存：{type(exc).__name__}。请检查文件权限、编码和可用磁盘。"
            )
            return
        self.saved = True
        self.root.destroy()

    def cancel(self) -> None:
        self.root.destroy()


_LEGACY_MAX_BYTES = 1024 * 1024
_LEGACY_PREVIEW_MINUTES = 10
_LEGACY_SOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z", re.ASCII)


class LegacyImportPreview(SafeModel):
    """Safe projection of an explicit legacy preview; never holds secret text."""

    preview_id: UUID
    source_id: str
    expires_at: datetime
    warnings: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
    credential_purposes: tuple[str, ...] = ()
    database_locator: DatabaseLocator | None = None
    redis_locator: RedisLocator | None = None
    storage_root: Path | None = None
    provider_adapters: tuple[str, ...] = ()
    restart_required: bool = True


@dataclass(frozen=True)
class _LegacyPlan:
    source_id: str
    path: Path
    digest: str
    expected_revision: UUID | None
    expires_at: datetime
    document: dict
    writes: dict
    warnings: tuple
    missing: tuple
    purposes: tuple
    adapters: tuple
    database: DatabaseLocator | None
    redis: RedisLocator | None
    storage_root: Path | None


class LegacyConfigImporter:
    """Explicit legacy .env acceptance: safe preview first, then a safe draft only.

    The source file is never modified, moved or deleted, no process environment
    value is borrowed, and no configuration pointer changes in this service.
    """

    def __init__(self, *, service: Any, sources: Mapping[str, Path], clock: Any = None,
                 max_previews: int = 8) -> None:
        if not callable(getattr(service, "create_draft_with_secrets", None)):
            raise self._error("INVALID_LEGACY_IMPORT_SERVICE")
        if not isinstance(sources, Mapping):
            raise self._error("INVALID_LEGACY_IMPORT_SOURCE")
        validated: dict[str, Path] = {}
        for source_id, path in sources.items():
            if (not isinstance(source_id, str) or _LEGACY_SOURCE_ID.fullmatch(source_id) is None
                    or not isinstance(path, Path) or not path.is_absolute()):
                raise self._error("INVALID_LEGACY_IMPORT_SOURCE")
            validated[source_id] = path
        self.service = service
        self.sources = validated
        self._clock = clock if callable(clock) else (lambda: datetime.now(UTC))
        self._max = max(1, int(max_previews))
        self._pending: OrderedDict[UUID, _LegacyPlan] = OrderedDict()
        self._mutex = threading.Lock()

    @staticmethod
    def _error(code: str) -> ConfigServiceError:
        return ConfigServiceError(code, stage="legacy_import")

    def preview(self, *, source_id: str, expected_revision: UUID | None) -> LegacyImportPreview:
        with self._mutex:
            path = self.sources.get(source_id)
            if path is None:
                raise self._error("LEGACY_SOURCE_NOT_ALLOWED")
            text, digest = self._read_source(path)
            plan = self._plan(source_id, path, text, digest, expected_revision)
            preview_id = uuid4()
            self._pending[preview_id] = plan
            while len(self._pending) > self._max:
                self._pending.popitem(last=False)
            return LegacyImportPreview(
                preview_id=preview_id, source_id=source_id, expires_at=plan.expires_at,
                warnings=plan.warnings, missing_fields=plan.missing, credential_purposes=plan.purposes,
                database_locator=plan.database, redis_locator=plan.redis,
                storage_root=plan.storage_root, provider_adapters=plan.adapters)

    def apply(self, *, preview_id: UUID, confirm: Any) -> Any:
        if confirm is not True:
            raise self._error("IMPORT_CONFIRMATION_REQUIRED")
        with self._mutex:
            plan = self._pending.pop(preview_id, None)
            if plan is None:
                raise self._error("PREVIEW_NOT_FOUND")
            if self._clock() > plan.expires_at:
                raise self._error("PREVIEW_EXPIRED")
        # Re-prove the exact source bytes before any new secret is written.
        _, digest = self._read_source(plan.path)
        if digest != plan.digest:
            raise self._error("LEGACY_SOURCE_CHANGED")
        return self.service.create_draft_with_secrets(
            plan.document, plan.writes, expected_revision=plan.expected_revision,
            process_env={}, acknowledge_override=False)

    def _read_source(self, path: Path) -> tuple[str, str]:
        try:
            info = path.lstat()
        except OSError:
            raise self._error("LEGACY_SOURCE_UNAVAILABLE") from None
        if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or not 0 < info.st_size <= _LEGACY_MAX_BYTES):
            raise self._error("LEGACY_SOURCE_UNAVAILABLE")
        try:
            with path.open("rb") as handle:
                value = handle.read(_LEGACY_MAX_BYTES + 1)
                opened = os.fstat(handle.fileno())
        except OSError:
            raise self._error("LEGACY_SOURCE_UNAVAILABLE") from None
        try:
            after = path.lstat()
        except OSError:
            raise self._error("LEGACY_SOURCE_CHANGED") from None
        if (len(value) != info.st_size or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                or (after.st_size, after.st_mtime_ns, after.st_ino)
                != (info.st_size, info.st_mtime_ns, info.st_ino)):
            raise self._error("LEGACY_SOURCE_CHANGED")
        try:
            text = value.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise self._error("LEGACY_CONFIGURATION_INVALID") from None
        return text, hashlib.sha256(value).hexdigest()

    def _plan(self, source_id: str, path: Path, text: str, digest: str,
              expected_revision: UUID | None) -> _LegacyPlan:
        try:
            raw = _values(text)
        except ConfigurationError:
            raise self._error("LEGACY_CONFIGURATION_INVALID") from None
        if any(name not in FIELDS for name in raw):
            # An unrecognized field may carry a secret that a silent import would
            # drop; the operator must resolve it explicitly.
            raise self._error("LEGACY_CONFIGURATION_INVALID")
        warnings = ["LEGACY_PLAINTEXT_REMAINS", "PROCESS_ENV_OVERRIDES_SAVED"]
        missing: list[str] = []
        writes: dict = {}
        purposes: list[str] = []
        adapters: list[str] = []
        providers: dict = {}
        bindings: dict = {}

        def text_of(name: str, *, required: bool = False) -> str | None:
            if name not in raw:
                if required:
                    missing.append(name)
                return None
            item = raw[name]
            if item is None or not str(item).strip():
                # An explicitly empty value is not an instruction to inherit.
                raise self._error("LEGACY_CONFIGURATION_INVALID")
            return str(item).strip()

        def number_of(name: str, low: float, high: float, *, integer: bool = False):
            value = text_of(name)
            if value is None:
                return None
            try:
                number = int(value) if integer else float(value)
            except ValueError:
                raise self._error("LEGACY_CONFIGURATION_INVALID") from None
            if isinstance(number, bool) or not low <= number <= high:
                raise self._error("LEGACY_CONFIGURATION_INVALID")
            return number

        def boolean_of(name: str) -> bool | None:
            value = text_of(name)
            if value is None:
                return None
            lowered = value.lower()
            if lowered not in {"true", "false"}:
                raise self._error("LEGACY_CONFIGURATION_INVALID")
            return lowered == "true"

        dimension = text_of("EMBEDDING_DIMENSION")
        if dimension is not None and dimension != "1024":
            # vector(1024) is the fixed index contract of this batch.
            raise self._error("LEGACY_CONFIGURATION_INVALID")
        algorithm = text_of("JWT_ALGORITHM")
        if algorithm is not None and algorithm != "HS256":
            raise self._error("LEGACY_CONFIGURATION_INVALID")
        minutes = number_of("JWT_EXPIRE_MINUTES", 1, 100000, integer=True)
        dev_mode = boolean_of("DEV_MODE")
        ocred = boolean_of("OCR_ENABLED")

        business_missing = "DATABASE_URL" if "DATABASE_URL" not in raw else (
            "REDIS_URL" if "REDIS_URL" not in raw else (
                "STORAGE_ROOT" if "STORAGE_ROOT" not in raw else (
                    "JWT_SECRET_KEY" if "JWT_SECRET_KEY" not in raw else None)))
        database = text_of("DATABASE_URL", required=True)
        database_locator: DatabaseLocator | None = None
        if database is not None:
            database_locator, password = self._database(database)
            if password is not None:
                writes[("business", "database_password_ref")] = ("database", SecretStr(password))
                purposes.append("database")
        redis = text_of("REDIS_URL", required=True)
        redis_locator: RedisLocator | None = None
        if redis is not None:
            redis_locator, password = self._redis(redis)
            if password is not None:
                writes[("business", "redis_password_ref")] = ("redis", SecretStr(password))
                purposes.append("redis")
        storage = text_of("STORAGE_ROOT")
        storage_path: Path | None = None
        if storage is None:
            missing.append("STORAGE_ROOT")
        else:
            storage_path = Path(storage).expanduser()
            if not storage_path.is_absolute():
                raise self._error("LEGACY_CONFIGURATION_INVALID")
        jwt = text_of("JWT_SECRET_KEY", required=True)
        if jwt is not None:
            if len(jwt) < 32:
                raise self._error("LEGACY_CONFIGURATION_INVALID")
            writes[("business", "jwt_secret_ref")] = ("jwt", SecretStr(jwt))
            purposes.append("jwt")

        llm_provider = text_of("LLM_PROVIDER")
        text_base_url: str | None = None
        text_key: str | None = None
        if llm_provider is not None:
            if llm_provider not in {"deepseek", "openai_compatible"}:
                raise self._error("LEGACY_CONFIGURATION_INVALID")
            model = text_of("DEEPSEEK_MODEL")
            base_url = text_of("DEEPSEEK_BASE_URL")
            key = text_of("DEEPSEEK_API_KEY")
            if model is None:
                missing.append("DEEPSEEK_MODEL")
            if base_url is None:
                missing.append("DEEPSEEK_BASE_URL")
            if key is None or model is None or base_url is None:
                missing.append("DEEPSEEK_API_KEY")
            if model is not None and base_url is not None:
                self._endpoint(base_url, warnings)
                text_base_url = base_url
                if key is not None:
                    text_key = key
                profile_id, profile_revision = uuid4(), uuid4()
                providers[str(profile_id)] = {
                    "profile_id": str(profile_id), "profile_revision": str(profile_revision),
                    "display_name": "Legacy " + llm_provider, "adapter_id": llm_provider,
                    "model_id": model, "base_url": base_url, "credential_ref": None,
                    "auth_mode": "api_key", "declared_capabilities": ["text", "json_object"],
                    "parameters": {}, "enabled": True,
                }
                adapters.append(llm_provider)
                bindings["default_llm"] = {
                    "task_kind": "default_llm", "profile_id": str(profile_id),
                    "profile_revision": providers[str(profile_id)]["profile_revision"],
                }
                if key is None:
                    warnings.append("PROVIDER_CREDENTIAL_MISSING")
                else:
                    writes[("providers", str(profile_id), "credential_ref")] = ("provider", SecretStr(key))
                    purposes.append("provider")

        vision_model = text_of("VISION_MODEL")
        if vision_model is not None:
            if llm_provider == "deepseek" and text_base_url is not None and text_key is not None:
                vision_id, vision_revision = uuid4(), uuid4()
                providers[str(vision_id)] = {
                    "profile_id": str(vision_id), "profile_revision": str(vision_revision),
                    "display_name": "Legacy vision", "adapter_id": "deepseek",
                    "model_id": vision_model, "base_url": text_base_url, "credential_ref": None,
                    "auth_mode": "api_key", "declared_capabilities": ["vision", "text", "json_object"],
                    "parameters": {}, "enabled": True,
                }
                adapters.append("deepseek")
                bindings["vision"] = {
                    "task_kind": "vision", "profile_id": str(vision_id),
                    "profile_revision": providers[str(vision_id)]["profile_revision"],
                }
                writes[("providers", str(vision_id), "credential_ref")] = ("provider", SecretStr(text_key))
                purposes.append("provider")
            else:
                warnings.append("VISION_CONFIGURATION_SKIPPED_UNSUPPORTED")

        embedding_provider = text_of("EMBEDDING_PROVIDER")
        if embedding_provider is not None:
            if embedding_provider not in {"openai_compatible", "local", "bge", "huggingface"}:
                raise self._error("LEGACY_CONFIGURATION_INVALID")
            model = text_of("EMBEDDING_MODEL")
            if model is None:
                missing.append("EMBEDDING_MODEL")
            elif embedding_provider == "openai_compatible":
                url = text_of("EMBEDDING_BASE_URL")
                key = text_of("EMBEDDING_API_KEY")
                if url is None or key is None:
                    missing.extend(name for name, value in (
                        ("EMBEDDING_BASE_URL", url), ("EMBEDDING_API_KEY", key)) if value is None)
                    warnings.append("EMBEDDING_CONFIGURATION_INCOMPLETE")
                else:
                    self._endpoint(url, warnings)
                    cloud_id, cloud_revision = uuid4(), uuid4()
                    providers[str(cloud_id)] = {
                        "profile_id": str(cloud_id), "profile_revision": str(cloud_revision),
                        "display_name": "Legacy cloud embedding", "adapter_id": "openai_compatible",
                        "model_id": model, "base_url": url, "credential_ref": None,
                        "auth_mode": "api_key", "declared_capabilities": ["embedding"],
                        "parameters": {}, "enabled": True,
                    }
                    adapters.append("openai_compatible")
                    bindings["embedding"] = {
                        "task_kind": "embedding", "profile_id": str(cloud_id),
                        "profile_revision": str(cloud_revision),
                    }
                    writes[("providers", str(cloud_id), "credential_ref")] = ("provider", SecretStr(key))
                    purposes.append("provider")
            else:
                directory = Path(model).expanduser()
                if not directory.is_absolute():
                    raise self._error("LEGACY_CONFIGURATION_INVALID")
                local_id, local_revision = uuid4(), uuid4()
                providers[str(local_id)] = {
                    "profile_id": str(local_id), "profile_revision": str(local_revision),
                    "display_name": "Legacy local embedding", "adapter_id": embedding_provider,
                    "model_id": model, "credential_ref": None, "auth_mode": "none",
                    "declared_capabilities": ["embedding"], "parameters": {},
                    "resource_directory": str(directory), "enabled": True,
                }
                adapters.append(embedding_provider)
                bindings["embedding"] = {"task_kind": "embedding", "profile_id": str(local_id),
                                          "profile_revision": str(local_revision)}

        rerank_provider = text_of("RERANK_PROVIDER")
        if rerank_provider is not None:
            if rerank_provider not in {"llm", "openai_compatible", "cross_encoder", "none"}:
                raise self._error("LEGACY_CONFIGURATION_INVALID")
            if rerank_provider == "cross_encoder":
                model = text_of("RERANK_MODEL")
                if model is None:
                    missing.append("RERANK_MODEL")
                else:
                    directory = Path(model).expanduser()
                    if not directory.is_absolute():
                        raise self._error("LEGACY_CONFIGURATION_INVALID")
                    local_rerank_id, local_rerank_revision = uuid4(), uuid4()
                    providers[str(local_rerank_id)] = {
                        "profile_id": str(local_rerank_id), "profile_revision": str(local_rerank_revision),
                        "display_name": "Legacy local rerank", "adapter_id": "cross_encoder",
                        "model_id": model, "credential_ref": None, "auth_mode": "none",
                        "declared_capabilities": ["local_rerank"], "parameters": {},
                        "resource_directory": str(directory), "enabled": True,
                    }
                    adapters.append("cross_encoder")
                    bindings["rerank"] = {"task_kind": "rerank", "profile_id": str(local_rerank_id),
                                          "profile_revision": str(local_rerank_revision)}
            elif rerank_provider != "none":
                model = text_of("RERANK_MODEL") or text_of("DEEPSEEK_MODEL")
                if text_base_url is None or text_key is None or model is None:
                    warnings.append("RERANK_CONFIGURATION_INCOMPLETE")
                else:
                    text_rerank_id, text_rerank_revision = uuid4(), uuid4()
                    providers[str(text_rerank_id)] = {
                        "profile_id": str(text_rerank_id), "profile_revision": str(text_rerank_revision),
                        "display_name": "Legacy text rerank", "adapter_id": "openai_compatible",
                        "model_id": model, "base_url": text_base_url, "credential_ref": None,
                        "auth_mode": "api_key", "declared_capabilities": ["llm_rerank"],
                        "parameters": {}, "enabled": True,
                    }
                    adapters.append("openai_compatible")
                    bindings["rerank"] = {"task_kind": "rerank", "profile_id": str(text_rerank_id),
                                          "profile_revision": str(text_rerank_revision)}
                    writes[("providers", str(text_rerank_id), "credential_ref")] = (
                        "provider", SecretStr(text_key))
                    purposes.append("provider")

        if ocred:
            provider_name = text_of("OCR_PROVIDER") or "rapidocr"
            ocr_model = text_of("OCR_MODEL") or "PP-OCRv5-mobile"
            ocr_directory = text_of("OCR_MODEL_DIR")
            if provider_name != "rapidocr" or ocr_model != "PP-OCRv5-mobile":
                raise self._error("LEGACY_CONFIGURATION_INVALID")
            if ocr_directory is None:
                missing.append("OCR_MODEL_DIR")
                warnings.append("OCR_RESOURCE_MISSING")
            else:
                resource = Path(ocr_directory).expanduser()
                if not resource.is_absolute():
                    raise self._error("LEGACY_CONFIGURATION_INVALID")
                ocr_id, ocr_revision = uuid4(), uuid4()
                providers[str(ocr_id)] = {
                    "profile_id": str(ocr_id), "profile_revision": str(ocr_revision),
                    "display_name": "Legacy OCR", "adapter_id": "rapidocr",
                    "model_id": ocr_model, "credential_ref": None, "auth_mode": "none",
                    "declared_capabilities": ["ocr"], "parameters": {},
                    "resource_directory": str(resource), "enabled": True,
                }
                adapters.append("rapidocr")
                bindings["ocr"] = {"task_kind": "ocr", "profile_id": str(ocr_id),
                                   "profile_revision": str(ocr_revision)}

        policies: dict = {}
        threshold = number_of("CONFIDENCE_THRESHOLD", 0.0, 1.0)
        if threshold is not None:
            policies["confidence_threshold"] = threshold
        weight = number_of("HYBRID_VECTOR_WEIGHT", 0.0, 1.0)
        if weight is not None:
            policies["hybrid_weight"] = weight
        candidate_limit = number_of("RERANK_MAX_CANDIDATES", 1, 100000, integer=True)
        if candidate_limit is not None:
            policies["rerank_max_candidates"] = candidate_limit
        timeout = number_of("RERANK_TIMEOUT_SECONDS", 0.1, 100000)
        if timeout is not None:
            policies["rerank_timeout_seconds"] = timeout

        if business_missing is not None:
            warnings.append("BUSINESS_CONFIGURATION_INCOMPLETE")
        if any(name.startswith(("DEEPSEEK", "EMBEDDING", "RERANK", "OCR_")) for name in missing):
            warnings.append("AI_CONFIGURATION_INCOMPLETE")

        document = {
            "bootstrap": {},
            "business": {
                "database_locator": (database_locator.model_dump(mode="json")
                                     if database_locator is not None else None),
                "database_password_ref": None,
                "redis_locator": (redis_locator.model_dump(mode="json")
                                  if redis_locator is not None else None),
                "redis_password_ref": None,
                "storage_root": str(storage_path) if storage_path is not None else None,
                "jwt_secret_ref": None,
                "jwt_algorithm": algorithm or "HS256",
                "jwt_expire_minutes": minutes if minutes is not None else 60,
                "dev_mode": bool(dev_mode) if dev_mode is not None else False,
            },
            "providers": providers,
            "bindings": bindings,
            "policies": policies,
        }
        return _LegacyPlan(
            source_id=source_id, path=path, digest=digest, expected_revision=expected_revision,
            expires_at=self._clock() + timedelta(minutes=_LEGACY_PREVIEW_MINUTES), document=document,
            writes=writes, warnings=tuple(warnings), missing=tuple(missing), purposes=tuple(purposes),
            adapters=tuple(adapters), database=database_locator, redis=redis_locator,
            storage_root=storage_path)

    @staticmethod
    def _endpoint(value: str, warnings: list[str]) -> None:
        try:
            validate_provider_endpoint(value)
        except RegistryValidationError as error:
            if getattr(error, "code", None) == "HTTPS_REQUIRED":
                # Legacy http endpoints stay importable but are flagged; the
                # Registry still refuses them at real construction time.
                warnings.append("ENDPOINT_HTTPS_REQUIRED")
                return
            raise LegacyConfigImporter._error("LEGACY_CONFIGURATION_INVALID") from None

    @staticmethod
    def _database(value: str) -> tuple[DatabaseLocator, str | None]:
        try:
            parsed = urlsplit(value)
            if not parsed.scheme.startswith("postgres") or not parsed.hostname or not parsed.username:
                raise ValueError
            database = parsed.path.lstrip("/")
            if not database:
                raise ValueError
            locator = DatabaseLocator(host=parsed.hostname, port=parsed.port or 5432, database=database,
                                      user=unquote(parsed.username), schema_name="public")
            password = unquote(parsed.password) if parsed.password else None
        except (ValueError, TypeError, ValidationError):
            raise LegacyConfigImporter._error("LEGACY_CONFIGURATION_INVALID") from None
        return locator, password

    @staticmethod
    def _redis(value: str) -> tuple[RedisLocator, str | None]:
        try:
            parsed = urlsplit(value)
            if not parsed.scheme.startswith("redis") or not parsed.hostname:
                raise ValueError
            index = parsed.path.strip("/")
            if index and not index.isdigit():
                raise ValueError
            locator = RedisLocator(host=parsed.hostname, port=parsed.port or 6379, database=int(index or 0))
            password = unquote(parsed.password) if parsed.password else None
        except (ValueError, TypeError, ValidationError):
            raise LegacyConfigImporter._error("LEGACY_CONFIGURATION_INVALID") from None
        return locator, password

def open_configuration(
    path: Path, template: Path, *, continue_launch: bool = False
) -> bool:
    import tkinter as tk

    document = ConfigDocument(path, template)
    root = tk.Tk()
    try:
        window = ConfigurationWindow(root, document, continue_launch=continue_launch)
        root.mainloop()
        return window.saved
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass
