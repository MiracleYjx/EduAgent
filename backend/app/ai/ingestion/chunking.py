"""带来源元数据的确定性文本分块。

分块是摄取链路的第三步：把清洗后的来源片段切成可检索的知识片段，并保留能够回溯到
课程、资料和源文件定位的元数据。设计要求：

- 同一输入重复分块必须得到完全一致的结果（无随机、无时间依赖）。
- 每个片段的内容必须是来源文本的精确切片，满足
  ``section_text[start_char:end_char] == content``，便于引用与校验。
- 优先在段落和句子边界切开，找不到自然边界时才按上限硬切。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from backend.app.ai.ingestion.cleaning import CleanedDocument, is_blank

DEFAULT_MAX_CHARS: Final[int] = 500
DEFAULT_OVERLAP_CHARS: Final[int] = 80

#: 边界优先级：先段落分隔，再中文句末，再换行，最后英文句末。
_BOUNDARY_SEPARATORS: Final[tuple[str, ...]] = (
    "\n\n",
    "。",
    "！",
    "？",
    "；",
    "\n",
    ". ",
    "! ",
    "? ",
    "; ",
)


@dataclass(frozen=True, slots=True)
class ChunkSourceMetadata:
    """知识片段的来源元数据，用于出题与阅卷的上下文追溯。"""

    document_id: UUID | None
    course_id: UUID | None
    knowledge_base_id: UUID | None
    original_filename: str
    file_format: str
    location: str
    section_index: int
    start_char: int
    end_char: int
    content_sha256: str

    def as_dict(self) -> dict[str, str | int | None]:
        """返回可写入 ``DocumentChunk.metadata`` 的 JSON 兼容字典。"""

        return {
            "document_id": str(self.document_id) if self.document_id is not None else None,
            "course_id": str(self.course_id) if self.course_id is not None else None,
            "knowledge_base_id": (
                str(self.knowledge_base_id) if self.knowledge_base_id is not None else None
            ),
            "original_filename": self.original_filename,
            "file_format": self.file_format,
            "location": self.location,
            "section_index": self.section_index,
            "start_char": self.start_char,
            "end_char": self.end_char,
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True, slots=True)
class TextChunk:
    """一个可检索的知识片段及其来源元数据。"""

    index: int
    content: str
    source: ChunkSourceMetadata

    @property
    def start_char(self) -> int:
        """返回片段在来源文本中的起始偏移。"""

        return self.source.start_char

    @property
    def end_char(self) -> int:
        """返回片段在来源文本中的结束偏移（不含）。"""

        return self.source.end_char

    def as_metadata(self) -> dict[str, object]:
        """返回包含分块序号与来源信息的元数据字典。"""

        metadata: dict[str, object] = {"chunk_index": self.index}
        metadata.update(self.source.as_dict())
        return metadata


def _validate_limits(max_chars: int, overlap_chars: int) -> None:
    """校验分块参数，保证切片单调前进且不会重复整段内容。"""

    if max_chars <= 0:
        raise ValueError("max_chars 必须大于 0")
    if overlap_chars < 0:
        raise ValueError("overlap_chars 不能为负数")
    if overlap_chars >= max_chars:
        raise ValueError("overlap_chars 必须小于 max_chars")


def _content_sha256(content: str) -> str:
    """计算片段内容摘要，用于校验分块与摄取的可重复性。"""

    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _find_boundary(text: str, start: int, end: int, max_chars: int) -> int:
    """在允许窗口内寻找更自然的切片边界，找不到时按上限硬切。"""

    min_end = start + max(1, max_chars // 2)
    if min_end >= end:
        return end
    window = text[min_end:end]
    for separator in _BOUNDARY_SEPARATORS:
        position = window.rfind(separator)
        if position != -1:
            return min_end + position + len(separator)
    return end


def _split_spans(text: str, max_chars: int, overlap_chars: int) -> list[tuple[int, int]]:
    """按字符上限和重叠长度生成确定性切片区间。"""

    spans: list[tuple[int, int]] = []
    length = len(text)
    start = 0
    while start < length:
        end = min(start + max_chars, length)
        if end < length:
            end = _find_boundary(text, start, end, max_chars)
        if not is_blank(text[start:end]):
            spans.append((start, end))
        if end >= length:
            break
        next_start = end - overlap_chars
        start = next_start if next_start > start else end
    return spans


def _build_chunk(
    index: int,
    text: str,
    span: tuple[int, int],
    *,
    document_id: UUID | None,
    course_id: UUID | None,
    knowledge_base_id: UUID | None,
    original_filename: str,
    file_format: str,
    location: str,
    section_index: int,
) -> TextChunk:
    """按切片区间构造带来源元数据的片段。"""

    start, end = span
    content = text[start:end]
    return TextChunk(
        index=index,
        content=content,
        source=ChunkSourceMetadata(
            document_id=document_id,
            course_id=course_id,
            knowledge_base_id=knowledge_base_id,
            original_filename=original_filename,
            file_format=file_format,
            location=location,
            section_index=section_index,
            start_char=start,
            end_char=end,
            content_sha256=_content_sha256(content),
        ),
    )


def chunk_text(
    text: str,
    *,
    document_id: UUID | None = None,
    course_id: UUID | None = None,
    knowledge_base_id: UUID | None = None,
    original_filename: str = "",
    file_format: str = "",
    location: str = "全文",
    section_index: int = 0,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> tuple[TextChunk, ...]:
    """把一段文本切成带来源元数据的片段；空白文本返回空结果。"""

    _validate_limits(max_chars, overlap_chars)
    if is_blank(text):
        return ()
    return tuple(
        _build_chunk(
            index,
            text,
            span,
            document_id=document_id,
            course_id=course_id,
            knowledge_base_id=knowledge_base_id,
            original_filename=original_filename,
            file_format=file_format,
            location=location,
            section_index=section_index,
        )
        for index, span in enumerate(_split_spans(text, max_chars, overlap_chars))
    )


def chunk_document(
    document: CleanedDocument,
    *,
    document_id: UUID | None = None,
    course_id: UUID | None = None,
    knowledge_base_id: UUID | None = None,
    original_filename: str = "",
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> tuple[TextChunk, ...]:
    """按来源片段切分清洗后的资料，并保持全局递增的分块序号。"""

    _validate_limits(max_chars, overlap_chars)
    if document.file_format == "markdown":
        return _chunk_markdown_document(
            document,
            document_id=document_id,
            course_id=course_id,
            knowledge_base_id=knowledge_base_id,
            original_filename=original_filename,
            max_chars=max_chars,
            overlap_chars=overlap_chars,
        )
    chunks: list[TextChunk] = []
    for section in document.sections:
        if is_blank(section.text):
            continue
        for span in _split_spans(section.text, max_chars, overlap_chars):
            chunks.append(
                _build_chunk(
                    len(chunks),
                    section.text,
                    span,
                    document_id=document_id,
                    course_id=course_id,
                    knowledge_base_id=knowledge_base_id,
                    original_filename=original_filename,
                    file_format=document.file_format,
                    location=section.location,
                    section_index=section.index,
                )
            )
    return tuple(chunks)


def _chunk_markdown_document(
    document: CleanedDocument,
    *,
    document_id: UUID | None,
    course_id: UUID | None,
    knowledge_base_id: UUID | None,
    original_filename: str,
    max_chars: int,
    overlap_chars: int,
) -> tuple[TextChunk, ...]:
    """合并 Markdown 标题与相邻正文后再分块，避免标题或短 section 单独成块。"""

    groups: list[tuple[str, str, int]] = []
    current_text: list[str] = []
    current_locations: list[str] = []
    current_section_index = 0
    pending_heading_text: list[str] = []
    pending_heading_locations: list[str] = []

    def flush() -> None:
        if not current_text:
            return
        groups.append(
            (
                "\n\n".join(current_text),
                "；".join(current_locations),
                current_section_index,
            )
        )

    for section in document.sections:
        # 解析器把没有正文的章节标题作为独立 section；暂存到下一段正文前，避免标题单独成块。
        if "\n" not in section.text:
            pending_heading_text.append(section.text)
            pending_heading_locations.append(section.location)
            continue

        section_text = "\n\n".join((*pending_heading_text, section.text))
        section_locations = [*pending_heading_locations, section.location]
        pending_heading_text.clear()
        pending_heading_locations.clear()
        candidate = "\n\n".join((*current_text, section_text))
        # 小于 100 字符的短 section 即使略超上限也与相邻正文合并。
        short_section = len(section_text) < 100
        if current_text and len(candidate) > max_chars and not short_section:
            flush()
            current_text.clear()
            current_locations.clear()
            current_section_index = 0
        if not current_text:
            current_section_index = section.index
        current_text.append(section_text)
        current_locations.extend(section_locations)
    # 末尾没有正文的标题不单独产出 chunk。
    flush()

    chunks: list[TextChunk] = []
    for merged_text, location, section_index in groups:
        spans = _split_spans(merged_text, max_chars, overlap_chars)
        for span in spans:
            chunks.append(
                _build_chunk(
                    len(chunks),
                    merged_text,
                    span,
                    document_id=document_id,
                    course_id=course_id,
                    knowledge_base_id=knowledge_base_id,
                    original_filename=original_filename,
                    file_format=document.file_format,
                    location=location,
                    section_index=section_index,
                )
            )
    return tuple(chunks)


__all__ = [
    "DEFAULT_MAX_CHARS",
    "DEFAULT_OVERLAP_CHARS",
    "ChunkSourceMetadata",
    "TextChunk",
    "chunk_document",
    "chunk_text",
]
