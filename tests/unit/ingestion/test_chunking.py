"""T033 文本分块单元测试：确定性、偏移自洽和来源元数据完整性。"""

from __future__ import annotations

import hashlib
import json
from itertools import pairwise
from uuid import UUID

import pytest

from backend.app.ai.ingestion.chunking import (
    chunk_document,
    chunk_text,
)
from backend.app.ai.ingestion.cleaning import CleanedDocument, clean_document
from backend.app.ai.ingestion.parsers import ParsedDocument, ParsedSection

DOCUMENT_ID = UUID("11111111-1111-4111-8111-111111111111")
COURSE_ID = UUID("22222222-2222-4222-8222-222222222222")
KNOWLEDGE_BASE_ID = UUID("33333333-3333-4333-8333-333333333333")


def _cleaned_document(*texts: str, file_format: str = "txt") -> CleanedDocument:
    """构造带多个来源片段的清洗后资料。"""

    sections = tuple(
        ParsedSection(index=index, location=f"第 {index} 段", text=text)
        for index, text in enumerate(texts, start=1)
    )
    return clean_document(
        ParsedDocument(
            file_format=file_format,
            text="\n\n".join(texts),
            sections=sections,
        )
    )


def _expected_sha256(content: str) -> str:
    """计算测试期望的内容摘要。"""

    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def test_chunk_text_contract_is_deterministic_and_offset_consistent() -> None:
    """同一输入重复分块结果一致，且每个片段都是来源文本的精确切片。"""

    text = "".join(f"第{index}句内容。" for index in range(1, 41))
    chunks = chunk_text(text, max_chars=100, overlap_chars=20)

    assert chunks == chunk_text(text, max_chars=100, overlap_chars=20)
    assert len(chunks) > 1
    for expected_index, chunk in enumerate(chunks):
        assert chunk.index == expected_index
        assert chunk.content == text[chunk.start_char : chunk.end_char]
        assert chunk.content.strip()
        assert len(chunk.content) <= 100
        assert chunk.source.content_sha256 == _expected_sha256(chunk.content)
        assert len(chunk.source.content_sha256) == 64

    for previous, current in pairwise(chunks):
        assert current.start_char >= previous.start_char
        assert current.start_char < previous.end_char
    assert chunks[-1].end_char == len(text)


def test_chunk_text_contract_prefers_paragraph_boundary() -> None:
    """存在段落边界时必须优先在边界处切片，而不是硬切满字符上限。"""

    first_paragraph = "甲" * 88 + "。"
    text = first_paragraph + "\n\n" + "乙" * 200

    chunks = chunk_text(text, max_chars=100, overlap_chars=20)

    assert chunks[0].content.endswith("\n\n")
    assert chunks[0].end_char == len(first_paragraph) + 2


def test_chunk_text_contract_applies_deterministic_overlap() -> None:
    """无自然边界时按固定上限硬切，并在相邻片段间保留固定重叠。"""

    text = "".join(f"{index:04d}." for index in range(125))
    assert len(text) == 625

    chunks = chunk_text(text, max_chars=100, overlap_chars=20)

    assert [(chunk.start_char, chunk.end_char) for chunk in chunks] == [
        (0, 100),
        (80, 180),
        (160, 260),
        (240, 340),
        (320, 420),
        (400, 500),
        (480, 580),
        (560, 625),
    ]
    assert chunks[1].content[:20] == chunks[0].content[-20:]


def test_chunk_text_contract_reports_default_source_metadata() -> None:
    """单段文本分块必须使用显式默认定位，便于缺少来源时排查。"""

    chunks = chunk_text("内容" * 200, max_chars=100, overlap_chars=10)

    assert chunks[0].source.location == "全文"
    assert chunks[0].source.section_index == 0
    assert chunks[0].source.document_id is None
    assert chunks[0].source.file_format == ""


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_chars": 0},
        {"max_chars": -1},
        {"overlap_chars": -1},
        {"max_chars": 100, "overlap_chars": 100},
        {"max_chars": 100, "overlap_chars": 120},
    ],
)
def test_chunk_text_contract_rejects_invalid_limits(kwargs: dict[str, int]) -> None:
    """非法分块参数必须直接失败，避免产生不可终止或重复的切片。"""

    with pytest.raises(ValueError):
        chunk_text("内容内容", **kwargs)


@pytest.mark.parametrize("blank_text", ["", "   \n\t ", "\ufeff\u200b"])
def test_chunk_text_contract_returns_no_chunks_for_blank_text(blank_text: str) -> None:
    """空白文本不得产生任何知识片段。"""

    assert chunk_text(blank_text) == ()
    assert chunk_document(_cleaned_document(blank_text)) == ()


def test_chunk_document_contract_carries_source_metadata() -> None:
    """按来源片段分块时必须保留课程、资料、定位和内容摘要。"""

    document = _cleaned_document("第一段内容。" * 20, "第二段内容。" * 20)

    chunks = chunk_document(
        document,
        document_id=DOCUMENT_ID,
        course_id=COURSE_ID,
        knowledge_base_id=KNOWLEDGE_BASE_ID,
        original_filename="课程资料.txt",
        max_chars=60,
        overlap_chars=10,
    )

    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))
    assert [chunk.source.location for chunk in chunks] == ["第 1 段"] * 3 + ["第 2 段"] * 3
    assert [chunk.source.section_index for chunk in chunks] == [1, 1, 1, 2, 2, 2]

    for chunk in chunks:
        metadata = chunk.as_metadata()
        # 元数据必须可序列化，以便写入 DocumentChunk.metadata。
        json.dumps(metadata, ensure_ascii=False)
        assert metadata["chunk_index"] == chunk.index
        assert metadata["document_id"] == str(DOCUMENT_ID)
        assert metadata["course_id"] == str(COURSE_ID)
        assert metadata["knowledge_base_id"] == str(KNOWLEDGE_BASE_ID)
        assert metadata["original_filename"] == "课程资料.txt"
        assert metadata["file_format"] == "txt"
        assert metadata["location"] == chunk.source.location
        assert metadata["section_index"] == chunk.source.section_index
        assert metadata["start_char"] == chunk.start_char
        assert metadata["end_char"] == chunk.end_char
        assert metadata["content_sha256"] == _expected_sha256(chunk.content)

    # 偏移以所属来源片段为基准，保证引用可回溯到原始资料。
    second_section = document.sections[1].text
    for chunk in chunks[3:]:
        assert second_section[chunk.start_char : chunk.end_char] == chunk.content
