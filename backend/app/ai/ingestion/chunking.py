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
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Final
from uuid import UUID

from backend.app.ai.ingestion.cleaning import CleanedDocument, is_blank
from backend.app.schemas.chapter_scope import SourceSplit

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
    heading_level: int | None = None
    heading_path: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        """返回可写入 ``DocumentChunk.metadata`` 的 JSON 兼容字典。"""

        metadata: dict[str, object] = {
            "document_id": str(self.document_id)
            if self.document_id is not None
            else None,
            "course_id": str(self.course_id) if self.course_id is not None else None,
            "knowledge_base_id": (
                str(self.knowledge_base_id)
                if self.knowledge_base_id is not None
                else None
            ),
            "original_filename": self.original_filename,
            "file_format": self.file_format,
            "location": self.location,
            "section_index": self.section_index,
            "start_char": self.start_char,
            "end_char": self.end_char,
            "content_sha256": self.content_sha256,
        }
        if self.heading_level is not None:
            metadata["heading_level"] = self.heading_level
            metadata["heading_path"] = list(self.heading_path)
        return metadata


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


def _split_spans(
    text: str, max_chars: int, overlap_chars: int
) -> list[tuple[int, int]]:
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
    heading_level: int | None = None,
    heading_path: tuple[str, ...] = (),
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
            heading_level=heading_level,
            heading_path=heading_path,
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


def validate_source_splits(
    document: CleanedDocument,
    source_splits: Sequence[SourceSplit] | None,
) -> dict[int, list[int]]:
    """Validate teacher cuts against the exact preview, before creating any chunks."""
    sections = {section.index: section for section in document.sections}
    cuts: dict[int, list[int]] = {}
    for split in source_splits or ():
        split = SourceSplit.model_validate(split)
        section = sections.get(split.section_index)
        if section is None or split.section_index in cuts:
            raise ValueError("Split source section is unknown or duplicated.")
        if any(point >= len(section.text) for point in split.cut_points):
            raise ValueError("Cut points must be strictly inside the source section.")
        cuts[split.section_index] = split.cut_points
    return cuts


def chunk_document(
    document: CleanedDocument,
    *,
    document_id: UUID | None = None,
    course_id: UUID | None = None,
    knowledge_base_id: UUID | None = None,
    original_filename: str = "",
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
    source_splits: Sequence[SourceSplit] | None = None,
) -> tuple[TextChunk, ...]:
    """Split each real source boundary independently, then apply length and overlap."""
    _validate_limits(max_chars, overlap_chars)
    cuts = validate_source_splits(document, source_splits)
    chunks: list[TextChunk] = []
    for section in document.sections:
        endpoints = [0, *cuts.get(section.index, []), len(section.text)]
        for range_start, range_end in pairwise(endpoints):
            text = section.text[range_start:range_end]
            for start, end in _split_spans(text, max_chars, overlap_chars):
                chunks.append(
                    _build_chunk(
                        len(chunks),
                        section.text,
                        (range_start + start, range_start + end),
                        document_id=document_id,
                        course_id=course_id,
                        knowledge_base_id=knowledge_base_id,
                        original_filename=original_filename,
                        file_format=document.file_format,
                        location=section.location,
                        section_index=section.index,
                        heading_level=section.heading_level,
                        heading_path=section.heading_path,
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
    "validate_source_splits",
]
