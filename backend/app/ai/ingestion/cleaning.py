"""教学资料文本清洗。

清洗是摄取链路的第二步：把解析得到的文本规范化为确定性纯文本，去掉控制字符、零宽
字符和排版噪声，同时保留中文标点、代码缩进等教学内容。清洗必须幂等，并且不依赖任何
模型或外部服务，从而保证同一份资料重复摄取得到完全一致的结果。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from backend.app.ai.ingestion.parsers import ParsedDocument

_CONTROL_PATTERN: Final[re.Pattern[str]] = re.compile(r"[\u0000-\u0008\u000b-\u001f\u007f]")
_ZERO_WIDTH_PATTERN: Final[re.Pattern[str]] = re.compile(r"[\u00ad\u200b-\u200d\u2060\ufeff]")
_SPACE_LIKE_PATTERN: Final[re.Pattern[str]] = re.compile(r"[\t\u3000]+")
_INNER_SPACE_PATTERN: Final[re.Pattern[str]] = re.compile(r" {2,}")
_BLANK_LINE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\n{2,}")
_IGNORABLE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[\u0000-\u0008\u000b-\u001f\u007f\u00ad\u200b-\u200d\u2060\ufeff]"
)


@dataclass(frozen=True, slots=True)
class CleanedSection:
    """清洗后的来源片段，保留解析阶段的定位信息。"""

    index: int
    location: str
    text: str


@dataclass(frozen=True, slots=True)
class CleanedDocument:
    """清洗后的资料文本；``sections`` 只包含清洗后仍有内容的片段。"""

    file_format: str
    text: str
    sections: tuple[CleanedSection, ...]
    page_count: int | None = None


def _collapse_inner_spaces(line: str) -> str:
    """折叠行内连续空格，同时保留行首缩进以保护代码类教学内容。"""

    stripped = line.rstrip()
    indent = stripped[: len(stripped) - len(stripped.lstrip(" "))]
    return indent + _INNER_SPACE_PATTERN.sub(" ", stripped[len(indent) :])


def clean_text(text: str) -> str:
    """把文本清洗为确定性纯文本。

    处理内容：统一换行符、去掉 BOM/零宽字符/控制字符、把制表符和全角空格转为空格、
    去掉行尾空白、折叠多余空行并去掉首尾空白。首个内容行之后的缩进会被保留，避免
    破坏代码类教学内容。函数是幂等的：``clean_text(clean_text(x)) == clean_text(x)``。
    """

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = _ZERO_WIDTH_PATTERN.sub("", normalized)
    normalized = _CONTROL_PATTERN.sub("", normalized)
    normalized = _SPACE_LIKE_PATTERN.sub(" ", normalized)
    lines = [_collapse_inner_spaces(line) for line in normalized.split("\n")]
    return _BLANK_LINE_PATTERN.sub("\n\n", "\n".join(lines)).strip()


def is_blank(text: str) -> bool:
    """判断文本去掉空白、不可见字符和控制字符后是否为空。"""

    return not _IGNORABLE_PATTERN.sub("", text).strip()


def clean_document(document: ParsedDocument) -> CleanedDocument:
    """清洗解析结果，并丢弃清洗后为空的来源片段。

    全部片段清洗后为空时返回空 ``sections``；是否判定为 ``KNOWLEDGE_BASE_EMPTY``
    由摄取编排负责，清洗阶段不创建任何可用于出题或阅卷的知识。
    """

    sections: list[CleanedSection] = []
    for section in document.sections:
        text = clean_text(section.text)
        if is_blank(text):
            continue
        sections.append(
            CleanedSection(index=section.index, location=section.location, text=text)
        )
    return CleanedDocument(
        file_format=document.file_format,
        text=clean_text(document.text),
        sections=tuple(sections),
        page_count=document.page_count,
    )


__all__ = [
    "CleanedDocument",
    "CleanedSection",
    "clean_document",
    "clean_text",
    "is_blank",
]
