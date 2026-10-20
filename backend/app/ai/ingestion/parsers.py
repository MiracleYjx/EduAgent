"""课程资料解析器注册表与适配器。

摄取链路中的第一步是“文件字节 -> 带来源定位的文本”。本模块只负责解析，不负责清洗、
分块和 Embedding，但必须保证：空文件、损坏文件、无文本内容和不支持格式都不会产生
任何可用于知识库的内容，而是抛出携带机器可读失败码的 :class:`DocumentParseError`。

失败码与重试语义与 ``.specify/plan.md`` 的资料摄取失败表保持一致：解析失败与损坏文件
属于可重试问题，空文件、不支持格式和无文本内容属于输入问题，不得无意义重试。

T027 在同一模块内加入已批准首版格式：DOCX（有限 OOXML，保留原对象与真实锚点）与
PNG/JPEG 单页图片（保留原图、标记需要 OCR，不伪造正文字）。PDF/TXT/Markdown 适配器
保持原行为。限额取自 research §7 已批准的 L01/L02/L03：超限必须明确拒绝，不得静默
截断或降采样。
"""

from __future__ import annotations

import hashlib
import io
import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from html import escape
from pathlib import PureWindowsPath
from types import MappingProxyType
from typing import TYPE_CHECKING, ClassVar, Final
from zipfile import BadZipFile, ZipFile

from pypdf import PdfReader
from pypdf.errors import FileNotDecryptedError, PdfReadError, PdfStreamError

if TYPE_CHECKING:
    from xml.etree.ElementTree import Element

DOCUMENT_EMPTY: Final[str] = "DOCUMENT_EMPTY"
DOCUMENT_UNSUPPORTED_FORMAT: Final[str] = "DOCUMENT_UNSUPPORTED_FORMAT"
DOCUMENT_CORRUPTED: Final[str] = "DOCUMENT_CORRUPTED"
DOCUMENT_NO_TEXT: Final[str] = "DOCUMENT_NO_TEXT"
DOCUMENT_PARSE_FAILED: Final[str] = "DOCUMENT_PARSE_FAILED"
#: T027 已批准限额（research §7 L01/L02/L03）；超限拒绝，不静默截断。
DOCUMENT_SIZE_LIMIT: Final[str] = "DOCUMENT_SIZE_LIMIT"
DOCUMENT_EXPANDED_LIMIT: Final[str] = "DOCUMENT_EXPANDED_LIMIT"
DOCUMENT_IMAGE_LIMIT: Final[str] = "DOCUMENT_IMAGE_LIMIT"
DOCUMENT_DECODED_LIMIT: Final[str] = "DOCUMENT_DECODED_LIMIT"

DOCUMENT_ERROR_MESSAGES: Final[Mapping[str, str]] = MappingProxyType(
    {
        DOCUMENT_EMPTY: "上传的文件为空，请选择包含教学内容的文件。",
        DOCUMENT_UNSUPPORTED_FORMAT: (
            "当前仅支持 PDF、DOCX、TXT、Markdown 与 PNG/JPEG 图片。"
        ),
        DOCUMENT_CORRUPTED: "文件无法读取，可能已损坏，请重新导出后上传。",
        DOCUMENT_NO_TEXT: "资料中没有可提取的文本内容，请检查扫描或文件内容。",
        DOCUMENT_PARSE_FAILED: "资料解析失败，请检查文件内容后重新上传。",
        DOCUMENT_SIZE_LIMIT: (
            "文件超过单次上传限制，请自行拆分或缩小后重试；系统不会静默截断。"
        ),
        DOCUMENT_EXPANDED_LIMIT: (
            "DOCX 实际解包内容超过限制，请拆分文档后重试；系统不会截断内容。"
        ),
        DOCUMENT_IMAGE_LIMIT: (
            "文档中的图片数量或解码尺寸超过限制，请拆分文档后重试。"
        ),
        DOCUMENT_DECODED_LIMIT: (
            "图片解码尺寸超过限制，请缩小图片后重试；系统不会静默降采样。"
        ),
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
_HEADING_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$"
)
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

MiB: Final[int] = 1024 * 1024

_DOCX_NS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
        "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
        "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    }
)
_DOCX_SKIP_TAGS: Final[frozenset[str]] = frozenset(
    {
        "{w}pPr",
        "{w}rPr",
        "{w}sectPr",
        "{w}proofErr",
        "{w}bookmarkStart",
        "{w}bookmarkEnd",
        "{w}lastRenderedPageBreak",
    }
)


def _docx_tag(prefix: str, name: str) -> str:
    return "{" + _DOCX_NS[prefix] + "}" + name


_DOCX_MAIN: Final[str] = "word/document.xml"
_DOCX_ROOT_PATH: Final[str] = "/w:document"
_DOCX_RELS: Final[str] = "word/_rels/document.xml.rels"
_DOCX_PREFIX_BY_NAMESPACE: Final[Mapping[str, str]] = MappingProxyType(
    {namespace: prefix for prefix, namespace in _DOCX_NS.items()}
)
_DOCX_NAMESPACE_BY_PREFIX: Final[Mapping[str, str]] = MappingProxyType(dict(_DOCX_NS))


@dataclass(frozen=True, slots=True)
class MaterialParsingLimits:
    """已批准的解析限额；数值与``services.learning_operations.LearningLimits``一致。

    本模块不导入服务层，避免解析器依赖 Provider/HTTP 栈；合同测试
    ``tests/contract/test_learning_materials.py`` 校验两处取值一致。
    """

    upload_bytes: int = 20 * MiB
    docx_expanded_bytes: int = 20 * MiB
    decoded_rgba_bytes: int = 10 * MiB
    docx_image_occurrences: int = 8
    pdf_batch_pages: int = 10


DEFAULT_MATERIAL_LIMITS: Final[MaterialParsingLimits] = MaterialParsingLimits()


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
class ParsedBlock:
    """解析出的有序内容块；顺序即真实阅读顺序，定位必须可回溯到原文件。

    ``kind`` 与 ``schemas.content_blocks.ContentBlock`` 的判别值同义，便于后续统一展示；
    本模块只输出事实，不决定受管资产身份。
    """

    kind: str
    anchor: str
    page: int | None = None
    text: str | None = None
    rows: tuple[tuple[str, ...], ...] = ()
    mathml: str | None = None
    expression: str | None = None
    image: bytes | None = None
    media_type: str | None = None
    sha256: str | None = None
    width: int | None = None
    height: int | None = None
    placement: str | None = None
    reason: str | None = None
    original: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedSection:
    """解析结果的来源片段，为分块提供可追溯定位。"""

    index: int
    location: str
    text: str
    heading_level: int | None = None
    heading_path: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """一次解析的完整结果：规范化全文、来源片段和页数。"""

    file_format: str
    text: str
    sections: tuple[ParsedSection, ...]
    page_count: int | None = None
    #: 有序内容块与疑点；图文资料必须以此保留真实顺序，不能靠文件名重建。
    blocks: tuple[ParsedBlock, ...] = ()
    issues: tuple[str, ...] = ()
    #: 扫描页/图片资料没有可靠文本时置真；内容保留，不伪造原文。
    requires_ocr: bool = False
    #: 单次批次未处理、但仍保留在原件中的页码；不谎报整份已完成。
    unprocessed_pages: tuple[int, ...] = ()


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
    raise DocumentParseError(
        DOCUMENT_CORRUPTED, detail="文本编码无法识别或包含二进制内容"
    )


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
            raise DocumentParseError(
                DOCUMENT_NO_TEXT, detail="TXT 中没有可提取的文本内容"
            )
        paragraphs = [
            block.strip() for block in _BLANK_LINE_PATTERN.split(text) if block.strip()
        ]
        if not paragraphs:
            raise DocumentParseError(
                DOCUMENT_NO_TEXT, detail="TXT 中没有可提取的文本内容"
            )
        sections = tuple(
            ParsedSection(index=index, location=f"第 {index} 段", text=paragraph)
            for index, paragraph in enumerate(paragraphs, start=1)
        )
        return ParsedDocument(
            file_format=self.format_name,
            text=text.strip(),
            sections=sections,
            blocks=tuple(
                ParsedBlock(kind="text", anchor=f"第 {index} 段", text=paragraph)
                for index, paragraph in enumerate(paragraphs, start=1)
            ),
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
            raise DocumentParseError(
                DOCUMENT_NO_TEXT, detail="Markdown 中没有可提取的文本内容"
            )
        sections = _markdown_sections(lines)
        return ParsedDocument(
            file_format=self.format_name,
            text=text,
            sections=sections,
            blocks=tuple(
                ParsedBlock(kind="text", anchor=section.location, text=section.text)
                for section in sections
            ),
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
        blocks: list[ParsedBlock] = []
        page_texts: list[str] = []
        requires_ocr = False
        for page_number, page in enumerate(pages, start=1):
            page_text = self._extract_page_text(page, page_number).strip()
            if not page_text:
                # 没有文本层的页面按扫描页保留：不编造正文，由后续 OCR 步骤处理。
                requires_ocr = True
                blocks.append(
                    ParsedBlock(
                        kind="image",
                        anchor=f"第 {page_number} 页",
                        page=page_number,
                        media_type="application/pdf-page",
                        reason="PDF_PAGE_WITHOUT_TEXT_LAYER",
                    )
                )
                continue
            page_texts.append(page_text)
            blocks.append(
                ParsedBlock(
                    kind="text",
                    anchor=f"第 {page_number} 页",
                    page=page_number,
                    text=page_text,
                )
            )
            sections.append(
                ParsedSection(
                    index=page_number,
                    location=f"第 {page_number} 页",
                    text=page_text,
                )
            )

        text = "\n\n".join(page_texts)
        if not has_meaningful_text(text):
            if requires_ocr:
                # 扫描型 PDF 没有可抽取文本：保留页面事实并标记需要 OCR。
                return ParsedDocument(
                    file_format=self.format_name,
                    text="",
                    sections=(),
                    page_count=len(pages),
                    blocks=tuple(blocks),
                    issues=("PDF_REQUIRES_OCR",),
                    requires_ocr=True,
                )
            raise DocumentParseError(
                DOCUMENT_NO_TEXT, detail="PDF 中没有可提取的文本内容"
            )
        return ParsedDocument(
            file_format=self.format_name,
            text=text,
            sections=tuple(sections),
            page_count=len(pages),
            blocks=tuple(blocks),
            requires_ocr=requires_ocr,
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
            raise DocumentParseError(
                DOCUMENT_PARSE_FAILED, detail="PDF 解析失败"
            ) from exc

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
            raise DocumentParseError(
                DOCUMENT_PARSE_FAILED, detail="PDF 文本提取失败"
            ) from exc
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


def _markdown_lines(text: str) -> list[tuple[int | None, str]]:
    """把 Markdown 正文规范化为“真实标题层级或空值 + 内容”的行序列。"""

    lines: list[tuple[int | None, str]] = []
    inside_fence = False
    for raw_line in text.split("\n"):
        if _FENCE_PATTERN.match(raw_line):
            # 代码围栏标记本身不是教学内容，内部代码内容必须保留。
            inside_fence = not inside_fence
            continue
        if inside_fence:
            # 代码内容必须保留原有缩进，避免破坏代码类教学内容。
            if raw_line.strip():
                lines.append((None, raw_line.rstrip()))
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
                lines.append((len(heading.group(1)), title))
            continue
        lines.append((None, _LIST_MARKER_PATTERN.sub("", candidate).strip()))
    return lines


def _markdown_sections(
    lines: list[tuple[int | None, str]],
) -> tuple[ParsedSection, ...]:
    """Keep actual heading hierarchy as source evidence, never as course identity."""
    sections: list[ParsedSection] = []
    location = "前言"
    buffer: list[str] = []
    heading_level: int | None = None
    headings: list[tuple[int, str]] = []

    def flush() -> None:
        if buffer:
            sections.append(
                ParsedSection(
                    index=len(sections) + 1,
                    location=location,
                    text="\n".join(buffer),
                    heading_level=heading_level,
                    heading_path=tuple(title for _level, title in headings),
                )
            )

    for level, content in lines:
        if level is not None:
            flush()
            buffer = [content]
            headings = [(depth, title) for depth, title in headings if depth < level]
            headings.append((level, content))
            heading_level = level
            location = f"标题：{content}"
        else:
            buffer.append(content)
    flush()
    return tuple(sections)


def _expanded_zip_bytes(data: bytes, limit: int) -> int:
    """流式累计实际解包字节；不信任 ZIP 目录声明的未压缩大小。"""

    total = 0
    try:
        with ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                with archive.open(info) as stream:
                    while True:
                        block = stream.read(65536)
                        if not block:
                            break
                        total += len(block)
                        if total > limit:
                            raise DocumentParseError(
                                DOCUMENT_EXPANDED_LIMIT,
                                detail=f"DOCX 实际解包超过 {limit} 字节",
                            )
    except BadZipFile as exc:
        raise DocumentParseError(
            DOCUMENT_CORRUPTED, detail="DOCX 不是有效的 OOXML 包"
        ) from exc
    return total


class DocxDocumentParser(DocumentParser):
    """DOCX 适配器：有限 OOXML 遍历，保留顺序、锚点、原对象与图片事实。

    只覆盖 T002 Spike 已验证的范围：段落文本、内联/浮动图、OMML 公式、表格。VML/OLE/
    SmartArt、合并单元格版式、EMF/WMF 图形和原始页面几何不在本版支持范围；这些对象保留
    原 XML 并记疑点，不能静默丢弃，也不能宣称复杂 DOCX 已全面支持。

    实现只使用标准库（``zipfile`` + ``xml.etree``），不引入试点期的第三方 OOXML 写入库，
    因为本项目未声明该依赖，也没有获得新增依赖授权。
    """

    format_name: ClassVar[str] = "docx"
    file_extensions: ClassVar[frozenset[str]] = frozenset({"docx"})

    def parse(
        self,
        data: bytes,
        *,
        filename: str,
        limits: MaterialParsingLimits | None = None,
        max_expanded_bytes: int | None = None,
    ) -> ParsedDocument:
        from xml.etree import ElementTree

        active = limits or DEFAULT_MATERIAL_LIMITS
        expanded_limit = (
            max_expanded_bytes
            if max_expanded_bytes is not None
            else active.docx_expanded_bytes
        )
        if not data.startswith(b"PK\x03\x04"):
            raise DocumentParseError(DOCUMENT_CORRUPTED, detail="文件缺少 OOXML 头标识")
        _expanded_zip_bytes(data, expanded_limit)
        try:
            with ZipFile(io.BytesIO(data)) as archive:
                names = set(archive.namelist())
                if _DOCX_MAIN not in names:
                    raise DocumentParseError(
                        DOCUMENT_CORRUPTED, detail="DOCX 缺少 word/document.xml"
                    )
                root = ElementTree.fromstring(archive.read(_DOCX_MAIN))
                relationships = _docx_relationships(archive, names)
                media = {
                    rel.target: archive.read(rel.target)
                    for rel in relationships.values()
                    if rel.target in names
                }
        except DocumentParseError:
            raise
        except (BadZipFile, ElementTree.ParseError, KeyError, OSError) as exc:
            raise DocumentParseError(
                DOCUMENT_CORRUPTED, detail="DOCX 结构损坏，无法读取正文"
            ) from exc
        except Exception as exc:  # 统一收敛为损坏文件。
            raise DocumentParseError(
                DOCUMENT_CORRUPTED, detail="DOCX 结构损坏，无法读取正文"
            ) from exc

        blocks: list[ParsedBlock] = []
        issues: list[str] = []
        counter = {"images": 0}

        def emit(kind: str, path: str, **values: object) -> None:
            blocks.append(ParsedBlock(kind=kind, anchor=path, **values))  # type: ignore[arg-type]

        def walk(node: Element, path: str) -> None:
            element = node
            tag = str(element.tag)
            if tag in _DOCX_SKIP_TAGS:
                return
            if tag == _docx_tag("w", "t"):
                text = (element.text or "").strip()
                if text:
                    emit("text", path, text=text)
                return
            if tag == _docx_tag("m", "oMath"):
                projection = "".join(element.itertext()).strip()
                emit(
                    "formula",
                    path,
                    expression=projection,
                    mathml=(
                        '<math xmlns="http://www.w3.org/1998/Math/MathML">'
                        f"<mtext>{escape(projection)}</mtext></math>"
                    ),
                )
                return
            if tag == _docx_tag("w", "drawing"):
                blips = [
                    child
                    for child in element.iter()
                    if str(child.tag) == _docx_tag("a", "blip")
                ]
                if not blips:
                    emit(
                        "original",
                        path,
                        reason="DRAWING_UNSUPPORTED",
                        original=_xml(element),
                    )
                    issues.append("DRAWING_UNSUPPORTED")
                    return
                placement = (
                    "floating"
                    if any(
                        str(child.tag) == _docx_tag("wp", "anchor")
                        for child in element.iter()
                    )
                    else "inline"
                )
                for blip in blips:
                    relationship = _docx_relationship_id(blip)
                    rel = relationships.get(relationship) if relationship else None
                    if rel is None or rel.target not in media:
                        emit(
                            "original",
                            path,
                            reason="DRAWING_RELATIONSHIP_UNRESOLVED",
                            original=_xml(element),
                        )
                        issues.append("DRAWING_RELATIONSHIP_UNRESOLVED")
                        continue
                    media_type = _docx_media_type(rel.target)
                    blob = media[rel.target]
                    counter["images"] += 1
                    if counter["images"] > active.docx_image_occurrences:
                        raise DocumentParseError(
                            DOCUMENT_IMAGE_LIMIT,
                            detail=(
                                "DOCX 图片出现次数超过 "
                                f"{active.docx_image_occurrences}"
                            ),
                        )
                    size = (
                        _image_size(blob)
                        if media_type in {"image/png", "image/jpeg"}
                        else None
                    )
                    if size is not None and size[0] * size[1] * 4 > (
                        active.decoded_rgba_bytes
                    ):
                        raise DocumentParseError(
                            DOCUMENT_DECODED_LIMIT,
                            detail="DOCX 图片解码预算超过已批准上限",
                        )
                    if size is None:
                        issues.append("DOCX_IMAGE_FORMAT_UNSUPPORTED")
                    emit(
                        "image",
                        path,
                        image=blob,
                        media_type=media_type,
                        sha256=hashlib.sha256(blob).hexdigest(),
                        width=size[0] if size else None,
                        height=size[1] if size else None,
                        placement=placement,
                        reason=None if size else "DOCX_IMAGE_FORMAT_UNSUPPORTED",
                    )
                return
            if tag == _docx_tag("w", "tbl"):
                rows: list[tuple[str, ...]] = []
                for row in [
                    child
                    for child in element
                    if str(child.tag) == _docx_tag("w", "tr")
                ]:
                    cells = [
                        "".join(
                            t.text or ""
                            for t in cell.iter()
                            if str(t.tag) == _docx_tag("w", "t")
                        )
                        for cell in row
                        if str(cell.tag) == _docx_tag("w", "tc")
                    ]
                    if cells:
                        rows.append(tuple(cells))
                emit("table", path, rows=tuple(rows))
                return
            if tag in {
                _docx_tag("w", "pict"),
                _docx_tag("w", "object"),
                _docx_tag("w", "altChunk"),
            }:
                reason = "OOXML_UNSUPPORTED_" + tag.rsplit("}", 1)[-1].upper()
                emit("original", path, reason=reason, original=_xml(element))
                issues.append(reason)
                return
            for child_path, child in _docx_children(element, path):
                walk(child, child_path)

        for child_path, child in _docx_children(root, _DOCX_ROOT_PATH):
            walk(child, child_path)

        sections = _docx_sections(root)
        text = "\n".join(section.text for section in sections)
        has_image = any(block.kind == "image" for block in blocks)
        if not has_meaningful_text(text) and not has_image:
            raise DocumentParseError(
                DOCUMENT_NO_TEXT, detail="DOCX 中没有可提取的文本或图片内容"
            )
        requires_ocr = not has_meaningful_text(text) and has_image
        if requires_ocr:
            issues = [*issues, "DOCX_IMAGE_ONLY_REQUIRES_OCR"]
        return ParsedDocument(
            file_format=self.format_name,
            text=text,
            sections=sections,
            blocks=tuple(blocks),
            issues=tuple(dict.fromkeys(issues)),
            requires_ocr=requires_ocr,
        )


@dataclass(frozen=True, slots=True)
class _DocxRelationship:
    """OOXML 关系：只保留解析图片所需的实际部件目标。"""

    relationship_id: str
    target: str


def _docx_relationships(
    archive: ZipFile, names: set[str]
) -> dict[str, _DocxRelationship]:
    """读取文档部件关系；缺失或损坏时返回空映射，不编造图片。"""

    if _DOCX_RELS not in names:
        return {}
    from xml.etree import ElementTree

    try:
        root = ElementTree.fromstring(archive.read(_DOCX_RELS))
    except ElementTree.ParseError:
        return {}
    relationships: dict[str, _DocxRelationship] = {}
    for node in root:
        if not str(node.tag).endswith("Relationship"):
            continue
        identifier = node.get("Id")
        target = node.get("Target")
        if not identifier or not target:
            continue
        normalized = target.lstrip("/")
        if not normalized.startswith("word/"):
            normalized = "word/" + normalized
        relationships[identifier] = _DocxRelationship(identifier, normalized)
    return relationships


def _docx_relationship_id(blip: Element) -> str | None:
    """返回图片引用关系 ID；未声明关系时返回 ``None``。"""

    for name in (_docx_tag("r", "embed"), _docx_tag("r", "link")):
        value = blip.get(name)
        if value:
            return str(value)
    return None


def _docx_media_type(partname: str) -> str:
    """按部件扩展名给出媒体类型；未知类型不猜测为可渲染图片。"""

    extension = partname.rsplit(".", 1)[-1].lower()
    return {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "bmp": "image/bmp",
        "tif": "image/tiff",
        "tiff": "image/tiff",
        "emf": "image/x-emf",
        "wmf": "image/x-wmf",
    }.get(extension, "application/octet-stream")


def _docx_children(element: Element, path: str) -> list[tuple[str, Element]]:
    """生成可回放的子节点路径：``/w:document/w:body/w:p[2]``。"""

    children = list(element)
    counts: dict[str, int] = {}
    result: list[tuple[str, Element]] = []
    for child in children:
        tag = str(child.tag)
        counts[tag] = counts.get(tag, 0) + 1
        segment = _docx_path_segment(tag, counts[tag])
        result.append((path + "/" + segment, child))
    return result


def _docx_path_segment(tag: str, index: int) -> str:
    """把 Clark 记法标签转成稳定路径片段；未知命名空间使用局部名。"""

    if tag.startswith("{"):
        namespace, _, local = tag[1:].partition("}")
        prefix = _DOCX_PREFIX_BY_NAMESPACE.get(namespace)
        if prefix is None:
            raise DocumentParseError(
                DOCUMENT_PARSE_FAILED, detail="DOCX 命名空间不在支持范围"
            )
        return f"{prefix}:{local}[{index}]"
    return f"{tag}[{index}]"


def resolve_docx_anchor(root: Element, anchor: str) -> Element | None:
    """按解析器给出的路径锚点定位原 XML 节点；无法定位时返回 ``None``。

    用于核对“内容块确实对应原始位置”，不把路径锚点当作文件名或推测。
    """

    node = root
    for raw in anchor.strip("/").split("/"):
        if not raw:
            continue
        prefix, _, remainder = raw.partition(":")
        local, _, index_text = remainder.partition("[")
        namespace = _DOCX_NAMESPACE_BY_PREFIX.get(prefix)
        if namespace is None:
            return None
        tag = "{" + namespace + "}" + local
        if str(node.tag) == tag:
            # 首段就是文档根节点；路径从这里开始，不需要再向下查找。
            continue
        index = int(index_text.rstrip("]") or "1")
        matches = [child for child in node if str(child.tag) == tag]
        if len(matches) < index:
            return None
        node = matches[index - 1]
    return node


def _xml(node: Element) -> str:
    """保留原始 OOXML 片段；无法渲染的对象不能被静默丢弃。"""

    from xml.etree import ElementTree

    return ElementTree.tostring(node, encoding="unicode")


def _image_size(blob: bytes) -> tuple[int, int]:
    """读取真实像素尺寸，用于解码预算检查；不完整图像按损坏处理。"""

    from PIL import Image

    try:
        with Image.open(io.BytesIO(blob)) as image:
            return int(image.width), int(image.height)
    except Exception as exc:  # 图像不可解码时明确失败。
        raise DocumentParseError(
            DOCUMENT_CORRUPTED, detail="文档内的图片无法解码"
        ) from exc


def _docx_sections(root: Element) -> tuple[ParsedSection, ...]:
    """按正文段落给出真实段落定位与真实标题层级（不编造页码）。"""

    body: Element | None = None
    for child in root:
        if str(child.tag) == _docx_tag("w", "body"):
            body = child
            break
    if body is None:
        return ()
    headings: list[tuple[int, str]] = []
    sections: list[ParsedSection] = []
    for paragraph in body:
        if str(paragraph.tag) != _docx_tag("w", "p"):
            continue
        text = "".join(
            t.text or ""
            for t in paragraph.iter()
            if str(t.tag) == _docx_tag("w", "t")
        ).strip()
        if not has_meaningful_text(text):
            continue
        level = _docx_heading_level(paragraph)
        if level is not None:
            headings = [(depth, title) for depth, title in headings if depth < level]
            headings.append((level, text))
        sections.append(
            ParsedSection(
                index=len(sections) + 1,
                location=f"第 {len(sections) + 1} 段",
                text=text,
                heading_level=level,
                heading_path=tuple(title for _depth, title in headings),
            )
        )
    return tuple(sections)


def _docx_heading_level(paragraph: Element) -> int | None:
    """识别内置标题样式；样式名不可靠时返回 ``None``，不猜层级。"""

    for child in paragraph:
        if str(child.tag) != _docx_tag("w", "pPr"):
            continue
        for style in child:
            if str(style.tag) != _docx_tag("w", "pStyle"):
                continue
            value = str(style.get(_docx_tag("w", "val")) or "")
            match = re.fullmatch(r"(?:heading|Heading|\u6807\u9898)\s*([1-6])", value)
            return int(match.group(1)) if match else None
    return None


class _ImageDocumentParser(DocumentParser):
    """单页 PNG/JPEG 图片资料：保留原图与位置，正文留给 OCR，不编造文字。"""

    image_format: ClassVar[str] = "PNG"
    limits: MaterialParsingLimits

    def parse(
        self,
        data: bytes,
        *,
        filename: str,
        limits: MaterialParsingLimits | None = None,
    ) -> ParsedDocument:
        from PIL import Image

        active = limits or DEFAULT_MATERIAL_LIMITS
        try:
            with Image.open(io.BytesIO(data)) as source:
                if source.format != self.image_format:
                    raise DocumentParseError(
                        DOCUMENT_CORRUPTED,
                        detail=f"文件内容不是 {self.image_format} 图片",
                    )
                if getattr(source, "n_frames", 1) != 1:
                    raise DocumentParseError(
                        DOCUMENT_CORRUPTED, detail="只支持单帧图片资料"
                    )
                width, height = source.size
        except DocumentParseError:
            raise
        except Exception as exc:  # 统一收敛为损坏文件。
            raise DocumentParseError(
                DOCUMENT_CORRUPTED, detail="图片文件无法解码"
            ) from exc
        if width * height * 4 > active.decoded_rgba_bytes:
            raise DocumentParseError(
                DOCUMENT_DECODED_LIMIT,
                detail=f"图片解码预算 {width}x{height}x4 超过已批准上限",
            )
        return ParsedDocument(
            file_format=self.format_name,
            text="",
            sections=(),
            page_count=1,
            blocks=(
                ParsedBlock(
                    kind="image",
                    anchor="第 1 页",
                    page=1,
                    image=data,
                    media_type=f"image/{self.image_format.lower()}",
                    sha256=hashlib.sha256(data).hexdigest(),
                    width=width,
                    height=height,
                    placement="page",
                ),
            ),
            requires_ocr=True,
        )


class PngDocumentParser(_ImageDocumentParser):
    """PNG 单页资料适配器。"""

    format_name: ClassVar[str] = "png"
    file_extensions: ClassVar[frozenset[str]] = frozenset({"png"})
    image_format: ClassVar[str] = "PNG"


class JpegDocumentParser(_ImageDocumentParser):
    """JPEG 单页资料适配器。"""

    format_name: ClassVar[str] = "jpeg"
    file_extensions: ClassVar[frozenset[str]] = frozenset({"jpg", "jpeg"})
    image_format: ClassVar[str] = "JPEG"


class DocumentParserRegistry:
    """受支持格式的解析器注册表。"""

    def __init__(self, *, limits: MaterialParsingLimits | None = None) -> None:
        self._parsers: dict[str, DocumentParser] = {}
        self._extensions: dict[str, str] = {}
        self.limits = limits or DEFAULT_MATERIAL_LIMITS

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

    def parse(
        self,
        filename: str,
        data: bytes,
        *,
        max_expanded_bytes: int | None = None,
    ) -> ParsedDocument:
        """校验格式、文件非空与上传字节上限后执行解析。"""

        parser = self.parser_for(filename)
        if not data:
            raise DocumentParseError(DOCUMENT_EMPTY, detail="上传的文件为空")
        if len(data) > self.limits.upload_bytes:
            raise DocumentParseError(
                DOCUMENT_SIZE_LIMIT,
                detail=f"上传字节 {len(data)} 超过 {self.limits.upload_bytes}",
            )
        if isinstance(parser, DocxDocumentParser):
            return parser.parse(
                data,
                filename=filename,
                limits=self.limits,
                max_expanded_bytes=max_expanded_bytes,
            )
        if isinstance(parser, _ImageDocumentParser):
            return parser.parse(data, filename=filename, limits=self.limits)
        return parser.parse(data, filename=filename)


def build_default_registry(
    *, limits: MaterialParsingLimits | None = None
) -> DocumentParserRegistry:
    """构建首版注册表：PDF、DOCX、TXT、Markdown 与 PNG/JPEG 图片。"""

    registry = DocumentParserRegistry(limits=limits)
    registry.register(TextDocumentParser())
    registry.register(MarkdownDocumentParser())
    registry.register(PdfDocumentParser())
    registry.register(DocxDocumentParser())
    registry.register(PngDocumentParser())
    registry.register(JpegDocumentParser())
    return registry


#: 默认注册表实例；解析器本身无状态，可安全复用。
DEFAULT_REGISTRY: Final[DocumentParserRegistry] = build_default_registry()


def parse_document(
    filename: str,
    data: bytes,
    *,
    registry: DocumentParserRegistry | None = None,
    max_expanded_bytes: int | None = None,
) -> ParsedDocument:
    """使用默认或指定注册表解析资料字节。"""

    active_registry = registry if registry is not None else DEFAULT_REGISTRY
    if max_expanded_bytes is None:
        # 保持既有调用形状：旧消费者与它们的测试不因新增可选限额而改变。
        return active_registry.parse(filename, data)
    return active_registry.parse(
        filename, data, max_expanded_bytes=max_expanded_bytes
    )


__all__ = [
    "DEFAULT_MATERIAL_LIMITS",
    "DEFAULT_REGISTRY",
    "DOCUMENT_CORRUPTED",
    "DOCUMENT_DECODED_LIMIT",
    "DOCUMENT_EMPTY",
    "DOCUMENT_ERROR_MESSAGES",
    "DOCUMENT_EXPANDED_LIMIT",
    "DOCUMENT_IMAGE_LIMIT",
    "DOCUMENT_NO_TEXT",
    "DOCUMENT_PARSE_FAILED",
    "DOCUMENT_SIZE_LIMIT",
    "DOCUMENT_UNSUPPORTED_FORMAT",
    "RETRYABLE_ERROR_CODES",
    "DocumentParseError",
    "DocumentParser",
    "DocumentParserRegistry",
    "DocxDocumentParser",
    "JpegDocumentParser",
    "MarkdownDocumentParser",
    "MaterialParsingLimits",
    "ParsedBlock",
    "ParsedDocument",
    "ParsedSection",
    "PdfDocumentParser",
    "PngDocumentParser",
    "TextDocumentParser",
    "build_default_registry",
    "decode_text_bytes",
    "detect_file_extension",
    "has_meaningful_text",
    "normalize_newlines",
    "parse_document",
    "resolve_docx_anchor",
]
