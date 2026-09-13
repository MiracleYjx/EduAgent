"""课程资料解析器注册表与适配器。

摄取链路中的第一步是“文件字节 -> 带来源定位的文本”。本模块只负责解析，不负责清洗、
分块和 Embedding，但必须保证：空文件、损坏文件、无文本内容和不支持格式都不会产生
任何可用于知识库的内容，而是抛出携带机器可读失败码的 :class:`DocumentParseError`。

失败码与重试语义与 ``.specify/plan.md`` 的资料摄取失败表保持一致：解析失败与损坏文件
属于可重试问题，空文件、不支持格式和无文本内容属于输入问题，不得无意义重试。
"""

from __future__ import annotations

import io
import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PureWindowsPath
from types import MappingProxyType
from typing import ClassVar, Final

from pypdf import PdfReader
from pypdf.errors import FileNotDecryptedError, PdfReadError, PdfStreamError

DOCUMENT_EMPTY: Final[str] = "DOCUMENT_EMPTY"
DOCUMENT_UNSUPPORTED_FORMAT: Final[str] = "DOCUMENT_UNSUPPORTED_FORMAT"
DOCUMENT_CORRUPTED: Final[str] = "DOCUMENT_CORRUPTED"
DOCUMENT_NO_TEXT: Final[str] = "DOCUMENT_NO_TEXT"
DOCUMENT_PARSE_FAILED: Final[str] = "DOCUMENT_PARSE_FAILED"

DOCUMENT_ERROR_MESSAGES: Final[Mapping[str, str]] = MappingProxyType(
    {
        DOCUMENT_EMPTY: "上传的文件为空，请选择包含教学内容的文件。",
        DOCUMENT_UNSUPPORTED_FORMAT: "当前仅支持 PDF、TXT 和 Markdown 文件。",
        DOCUMENT_CORRUPTED: "文件无法读取，可能已损坏，请重新导出后上传。",
        DOCUMENT_NO_TEXT: "资料中没有可提取的文本内容，请检查扫描或文件内容。",
        DOCUMENT_PARSE_FAILED: "资料解析失败，请检查文件内容后重新上传。",
    }
)

#: 失败后可安全重试的失败码；其余失败码必须先修正输入。
RETRYABLE_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {DOCUMENT_PARSE_FAILED, DOCUMENT_CORRUPTED}
)

_TEXT_ENCODINGS: Final[tuple[str, ...]] = ("utf-8-sig", "gb18030")
_IGNORABLE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[\u0000-\u0008\u000b-\u001f\u007f\u200b-\u200d\u2060\ufeff]"
)
_BLANK_LINE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\n[ \t]*\n+")
_FRONT_MATTER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\A[ \t]*---[ \t]*\n.*?\n---[ \t]*(?:\n|\Z)", re.DOTALL
)
_HTML_COMMENT_PATTERN: Final[re.Pattern[str]] = re.compile(r"<!--.*?-->", re.DOTALL)
_FENCE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[ \t]*(?:```|~~~)")
_RULE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^([-*_])[ \t]*(?:\1[ \t]*){2,}$")
_EMPTY_HEADING_PATTERN: Final[re.Pattern[str]] = re.compile(r"^#{1,6}[ \t]*$")
_HEADING_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_LIST_MARKER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[ \t]*(?:[-*+]|\d{1,3}[.)])[ \t]+"
)
_IMAGE_PATTERN: Final[re.Pattern[str]] = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_PATTERN: Final[re.Pattern[str]] = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_INLINE_CODE_PATTERN: Final[re.Pattern[str]] = re.compile(r"`([^`]+)`")
_HTML_TAG_PATTERN: Final[re.Pattern[str]] = re.compile(r"<[^>]+>")
_BOLD_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\*\*([^*]+)\*\*"),
    re.compile(r"__([^_]+)__"),
    re.compile(r"(?<![\w*])\*([^*\s][^*]*?)\*(?![\w*])"),
    re.compile(r"(?<![\w_])_([^_\s][^_]*?)_(?![\w_])"),
)


class DocumentParseError(Exception):
    """资料解析失败，携带机器可读失败码、用户提示和重试标识。"""

    def __init__(self, error_code: str, *, detail: str | None = None) -> None:
        message = DOCUMENT_ERROR_MESSAGES.get(
            error_code, DOCUMENT_ERROR_MESSAGES[DOCUMENT_PARSE_FAILED]
        )
        super().__init__(f"{error_code}：{message}")
        self.error_code = error_code
        # detail 只保存可安全展示的技术原因，不得包含文件内容或敏感配置。
        self.detail = detail

    @property
    def user_message(self) -> str:
        """返回可直接展示给教师的失败提示。"""

        return DOCUMENT_ERROR_MESSAGES.get(
            self.error_code, DOCUMENT_ERROR_MESSAGES[DOCUMENT_PARSE_FAILED]
        )

    @property
    def retryable(self) -> bool:
        """返回该失败是否允许按统一错误策略重试。"""

        return self.error_code in RETRYABLE_ERROR_CODES


@dataclass(frozen=True, slots=True)
class ParsedSection:
    """解析结果的来源片段，为分块提供可追溯定位。"""

    index: int
    location: str
    text: str


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """一次解析的完整结果：规范化全文、来源片段和页数。"""

    file_format: str
    text: str
    sections: tuple[ParsedSection, ...]
    page_count: int | None = None


def detect_file_extension(filename: str) -> str:
    """提取规范化扩展名（小写、不含点）；无扩展名时返回空串。"""

    name = PureWindowsPath(filename).name
    stem, dot, suffix = name.rpartition(".")
    if not dot or not stem:
        return ""
    return suffix.strip().lower()


def normalize_newlines(text: str) -> str:
    """统一换行符，避免 CRLF 影响段落与定位的确定性。"""

    return text.replace("\r\n", "\n").replace("\r", "\n")


def has_meaningful_text(text: str) -> bool:
    """判断文本除空白、BOM 和控制字符外是否还有真实内容。"""

    return bool(_IGNORABLE_PATTERN.sub("", text).strip())


def decode_text_bytes(data: bytes) -> str:
    """按 UTF-8（含 BOM）优先、GB18030 兜底解码文本资料。

    两种编码都失败或解码结果包含 NUL 字节时视为损坏文件，不保留任何内容。
    """

    for encoding in _TEXT_ENCODINGS:
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        if "\x00" in text:
            break
        return text
    raise DocumentParseError(DOCUMENT_CORRUPTED, detail="文本编码无法识别或包含二进制内容")


class DocumentParser(ABC):
    """单一资料格式的解析适配器契约。"""

    format_name: ClassVar[str] = "unknown"
    file_extensions: ClassVar[frozenset[str]] = frozenset()

    @abstractmethod
    def parse(self, data: bytes, *, filename: str) -> ParsedDocument:
        """把文件字节解析为带来源定位的文本，失败时抛出 :class:`DocumentParseError`。"""

        raise NotImplementedError


class TextDocumentParser(DocumentParser):
    """TXT 适配器：按空行切分段落，并给出段落级定位。"""

    format_name: ClassVar[str] = "txt"
    file_extensions: ClassVar[frozenset[str]] = frozenset({"txt"})

    def parse(self, data: bytes, *, filename: str) -> ParsedDocument:
        text = normalize_newlines(decode_text_bytes(data))
        if not has_meaningful_text(text):
            raise DocumentParseError(DOCUMENT_NO_TEXT, detail="TXT 中没有可提取的文本内容")
        paragraphs = [
            block.strip() for block in _BLANK_LINE_PATTERN.split(text) if block.strip()
        ]
        if not paragraphs:
            raise DocumentParseError(DOCUMENT_NO_TEXT, detail="TXT 中没有可提取的文本内容")
        sections = tuple(
            ParsedSection(index=index, location=f"第 {index} 段", text=paragraph)
            for index, paragraph in enumerate(paragraphs, start=1)
        )
        return ParsedDocument(
            file_format=self.format_name,
            text=text.strip(),
            sections=sections,
        )


class MarkdownDocumentParser(DocumentParser):
    """Markdown 适配器：去掉语法噪声，并按标题切分来源片段。"""

    format_name: ClassVar[str] = "markdown"
    file_extensions: ClassVar[frozenset[str]] = frozenset({"md", "markdown"})

    def parse(self, data: bytes, *, filename: str) -> ParsedDocument:
        raw = normalize_newlines(decode_text_bytes(data))
        lines = _markdown_lines(_strip_front_matter(raw))
        text = "\n".join(content for _is_heading, content in lines)
        if not has_meaningful_text(text):
            raise DocumentParseError(DOCUMENT_NO_TEXT, detail="Markdown 中没有可提取的文本内容")
        return ParsedDocument(
            file_format=self.format_name,
            text=text,
            sections=_markdown_sections(lines),
        )


class PdfDocumentParser(DocumentParser):
    """PDF 适配器：逐页提取文本，并保留页码定位与总页数。"""

    format_name: ClassVar[str] = "pdf"
    file_extensions: ClassVar[frozenset[str]] = frozenset({"pdf"})

    def parse(self, data: bytes, *, filename: str) -> ParsedDocument:
        if not data.lstrip()[:5] == b"%PDF-":
            raise DocumentParseError(DOCUMENT_CORRUPTED, detail="文件缺少 PDF 头标识")
        pages = self._load_pages(data)

        sections: list[ParsedSection] = []
        page_texts: list[str] = []
        for page_number, page in enumerate(pages, start=1):
            page_text = self._extract_page_text(page, page_number).strip()
            if not page_text:
                continue
            page_texts.append(page_text)
            sections.append(
                ParsedSection(
                    index=page_number,
                    location=f"第 {page_number} 页",
                    text=page_text,
                )
            )

        text = "\n\n".join(page_texts)
        if not has_meaningful_text(text):
            raise DocumentParseError(DOCUMENT_NO_TEXT, detail="PDF 中没有可提取的文本内容")
        return ParsedDocument(
            file_format=self.format_name,
            text=text,
            sections=tuple(sections),
            page_count=len(pages),
        )

    @staticmethod
    def _load_pages(data: bytes) -> list[object]:
        """打开 PDF 并读取全部页面，把 pypdf 异常映射为统一的失败码。"""

        try:
            reader = PdfReader(io.BytesIO(data))
            _ensure_decrypted(reader)
            return list(reader.pages)
        except DocumentParseError:
            raise
        except (PdfReadError, PdfStreamError) as exc:
            raise DocumentParseError(
                DOCUMENT_CORRUPTED, detail="PDF 结构损坏，无法读取页面"
            ) from exc
        except Exception as exc:
            raise DocumentParseError(DOCUMENT_PARSE_FAILED, detail="PDF 解析失败") from exc

    @staticmethod
    def _extract_page_text(page: object, page_number: int) -> str:
        """提取单页文本，损坏页面同样收敛为统一失败码。"""

        try:
            extracted = page.extract_text()  # type: ignore[attr-defined]
        except FileNotDecryptedError as exc:
            raise DocumentParseError(
                DOCUMENT_PARSE_FAILED, detail="PDF 已加密，无法提取文本"
            ) from exc
        except (PdfReadError, PdfStreamError) as exc:
            raise DocumentParseError(
                DOCUMENT_CORRUPTED, detail=f"PDF 第 {page_number} 页结构损坏"
            ) from exc
        except Exception as exc:
            raise DocumentParseError(DOCUMENT_PARSE_FAILED, detail="PDF 文本提取失败") from exc
        return normalize_newlines(extracted or "")


def _ensure_decrypted(reader: PdfReader) -> None:
    """尝试使用空密码解密；需要密码的资料按解析失败处理。"""

    if not reader.is_encrypted:
        return
    try:
        reader.decrypt("")
    except Exception as exc:
        raise DocumentParseError(
            DOCUMENT_PARSE_FAILED, detail="PDF 已加密，无法提取文本"
        ) from exc


def _strip_front_matter(text: str) -> str:
    """去掉 Markdown 文件头部的 YAML 元数据块。"""

    return _FRONT_MATTER_PATTERN.sub("", text, count=1)


def _strip_inline_markup(line: str) -> str:
    """去掉行内 Markdown 标记，同时避免破坏代码标识符中的下划线。"""

    stripped = _HTML_COMMENT_PATTERN.sub("", line)
    stripped = _IMAGE_PATTERN.sub(r"\1", stripped)
    stripped = _LINK_PATTERN.sub(r"\1", stripped)
    stripped = _INLINE_CODE_PATTERN.sub(r"\1", stripped)
    stripped = _HTML_TAG_PATTERN.sub("", stripped)
    for pattern in _BOLD_PATTERNS:
        stripped = pattern.sub(r"\1", stripped)
    return stripped.strip()


def _markdown_lines(text: str) -> list[tuple[bool, str]]:
    """把 Markdown 正文规范化为“是否为标题 + 内容”的行序列。"""

    lines: list[tuple[bool, str]] = []
    inside_fence = False
    for raw_line in text.split("\n"):
        if _FENCE_PATTERN.match(raw_line):
            # 代码围栏标记本身不是教学内容，内部代码内容必须保留。
            inside_fence = not inside_fence
            continue
        if inside_fence:
            # 代码内容必须保留原有缩进，避免破坏代码类教学内容。
            if raw_line.strip():
                lines.append((False, raw_line.rstrip()))
            continue

        candidate = _strip_inline_markup(raw_line.strip())
        if not candidate or _RULE_PATTERN.match(candidate):
            continue
        if _EMPTY_HEADING_PATTERN.match(candidate):
            continue
        heading = _HEADING_PATTERN.match(candidate)
        if heading is not None:
            title = heading.group(2).strip()
            if title:
                lines.append((True, title))
            continue
        lines.append((False, _LIST_MARKER_PATTERN.sub("", candidate).strip()))
    return lines


def _markdown_sections(lines: list[tuple[bool, str]]) -> tuple[ParsedSection, ...]:
    """按标题边界生成来源片段；标题前的内容归入“前言”。"""

    sections: list[ParsedSection] = []
    location = "前言"
    buffer: list[str] = []

    def flush() -> None:
        if not buffer:
            return
        sections.append(
            ParsedSection(
                index=len(sections) + 1,
                location=location,
                text="\n".join(buffer),
            )
        )

    for is_heading, content in lines:
        if is_heading:
            flush()
            buffer = [content]
            location = f"标题：{content}"
            continue
        buffer.append(content)
    flush()
    return tuple(sections)


class DocumentParserRegistry:
    """受支持格式的解析器注册表。"""

    def __init__(self) -> None:
        self._parsers: dict[str, DocumentParser] = {}
        self._extensions: dict[str, str] = {}

    def register(self, parser: DocumentParser) -> None:
        """注册一个格式适配器；重复格式或扩展名冲突直接失败。"""

        if parser.format_name in self._parsers:
            raise ValueError(f"解析器格式重复注册：{parser.format_name}")
        for extension in sorted(parser.file_extensions):
            owner = self._extensions.get(extension)
            if owner is not None and owner != parser.format_name:
                raise ValueError(f"扩展名 {extension} 已注册给格式 {owner}")
            self._extensions[extension] = parser.format_name
        self._parsers[parser.format_name] = parser

    @property
    def supported_formats(self) -> tuple[str, ...]:
        """返回已注册的格式标识。"""

        return tuple(sorted(self._parsers))

    @property
    def supported_extensions(self) -> tuple[str, ...]:
        """返回已注册的扩展名。"""

        return tuple(sorted(self._extensions))

    def format_for(self, filename: str) -> str | None:
        """按文件名返回受支持格式；不受支持时返回 ``None``。"""

        return self._extensions.get(detect_file_extension(filename))

    def is_supported(self, filename: str) -> bool:
        """判断文件名是否属于受支持格式。"""

        return self.format_for(filename) is not None

    def get(self, file_format: str) -> DocumentParser | None:
        """按格式标识获取适配器。"""

        return self._parsers.get(file_format.strip().lower())

    def parser_for(self, filename: str) -> DocumentParser:
        """按文件名获取适配器，不支持格式抛出 ``DOCUMENT_UNSUPPORTED_FORMAT``。"""

        file_format = self.format_for(filename)
        if file_format is None:
            extension = detect_file_extension(filename) or "（无扩展名）"
            raise DocumentParseError(
                DOCUMENT_UNSUPPORTED_FORMAT, detail=f"不支持的资料扩展名：{extension}"
            )
        return self._parsers[file_format]

    def parse(self, filename: str, data: bytes) -> ParsedDocument:
        """校验格式与文件非空后执行解析。"""

        parser = self.parser_for(filename)
        if not data:
            raise DocumentParseError(DOCUMENT_EMPTY, detail="上传的文件为空")
        return parser.parse(data, filename=filename)


def build_default_registry() -> DocumentParserRegistry:
    """构建 MVP 默认注册表：PDF、TXT 和 Markdown。"""

    registry = DocumentParserRegistry()
    registry.register(TextDocumentParser())
    registry.register(MarkdownDocumentParser())
    registry.register(PdfDocumentParser())
    return registry


#: 默认注册表实例；解析器本身无状态，可安全复用。
DEFAULT_REGISTRY: Final[DocumentParserRegistry] = build_default_registry()


def parse_document(
    filename: str,
    data: bytes,
    *,
    registry: DocumentParserRegistry | None = None,
) -> ParsedDocument:
    """使用默认或指定注册表解析资料字节。"""

    active_registry = registry if registry is not None else DEFAULT_REGISTRY
    return active_registry.parse(filename, data)


__all__ = [
    "DEFAULT_REGISTRY",
    "DOCUMENT_CORRUPTED",
    "DOCUMENT_EMPTY",
    "DOCUMENT_ERROR_MESSAGES",
    "DOCUMENT_NO_TEXT",
    "DOCUMENT_PARSE_FAILED",
    "DOCUMENT_UNSUPPORTED_FORMAT",
    "RETRYABLE_ERROR_CODES",
    "DocumentParseError",
    "DocumentParser",
    "DocumentParserRegistry",
    "MarkdownDocumentParser",
    "ParsedDocument",
    "ParsedSection",
    "PdfDocumentParser",
    "TextDocumentParser",
    "build_default_registry",
    "decode_text_bytes",
    "detect_file_extension",
    "has_meaningful_text",
    "normalize_newlines",
    "parse_document",
]
