"""Local configuration editor, available before database/model startup."""

from __future__ import annotations

import io
import os
import re
import secrets
import stat
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from dotenv.parser import parse_stream
from pydantic import ValidationError
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
