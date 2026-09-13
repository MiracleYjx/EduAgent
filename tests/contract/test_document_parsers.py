"""T031 文档解析器契约测试（失败优先）。

覆盖 PDF、TXT、Markdown、不支持格式、空文件、损坏文件和无文本内容，并固定解析器
注册表、解析结果来源定位和失败码语义。本文件先于 T032 的实现编写，因此在 T031 提交
时预期为红，由 T032 的解析器注册表与适配器转绿。
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from backend.app.ai.ingestion.parsers import (
    DOCUMENT_CORRUPTED,
    DOCUMENT_EMPTY,
    DOCUMENT_ERROR_MESSAGES,
    DOCUMENT_NO_TEXT,
    DOCUMENT_PARSE_FAILED,
    DOCUMENT_UNSUPPORTED_FORMAT,
    RETRYABLE_ERROR_CODES,
    DocumentParseError,
    build_default_registry,
    parse_document,
)

TXT_SOURCE = "第一段内容，用于验证段落定位。\n\n第二段内容，用于验证段落切分。"

MARKDOWN_SOURCE = """---
title: 不应保留的元数据
---

# 第一章 概述

本节介绍 **RAG** 基础。

## 1.1 检索

参考 [设计文档](https://example.com/doc) 与 `代码片段`。

```python
def 示例():
    print("代码内容必须保留")
```
"""

# 仅包含空白字符的资料属于“无文本内容”，而不是“空文件”。
WHITESPACE_ONLY_TXT_BYTES = b"   \n\n\t  \n"

# 无法按 UTF-8 或 GB18030 解码的字节序列，用于模拟损坏的 TXT 资料。
UNDECODABLE_TEXT_BYTES = bytes([255, 254, 0, 0, 1])

# 头部像 PDF 但内容损坏的字节序列，用于模拟损坏的 PDF 资料。
CORRUPTED_PDF_BYTES = b"%PDF-1.4\n" + bytes(range(64))


def _page_content_stream(page_text: str) -> str:
    """生成单页文本内容流；测试资料只使用 ASCII 文本。"""

    stripped = page_text.strip()
    if not stripped:
        return "BT ET"
    return f"BT /F1 24 Tf 72 700 Td ({stripped}) Tj ET"


def _build_pdf(pages: Sequence[str]) -> bytes:
    """构造仅含未压缩文本流的最小 PDF，供解析器契约测试使用。"""

    page_object_ids: list[int] = []
    content_objects: list[tuple[int, str]] = []
    next_object_id = 3  # 1 = Catalog，2 = Pages
    for page_text in pages:
        page_object_ids.append(next_object_id)
        next_object_id += 1
        content_objects.append((next_object_id, page_text))
        next_object_id += 1
    font_object_id = next_object_id

    kids = " ".join(f"{page_id} 0 R" for page_id in page_object_ids)
    objects: list[tuple[int, str]] = [
        (1, "<< /Type /Catalog /Pages 2 0 R >>"),
        (2, f"<< /Type /Pages /Kids [{kids}] /Count {len(page_object_ids)} >>"),
    ]
    for page_id, (content_id, _page_text) in zip(page_object_ids, content_objects):
        objects.append(
            (
                page_id,
                (
                    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Contents {content_id} 0 R "
                    f"/Resources << /Font << /F1 {font_object_id} 0 R >> >> >>"
                ),
            )
        )
    for content_id, page_text in content_objects:
        stream = _page_content_stream(page_text)
        objects.append(
            (content_id, f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        )
    objects.append(
        (font_object_id, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    )
    objects.sort()

    document = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for object_id, payload in objects:
        offsets[object_id] = len(document)
        document += f"{object_id} 0 obj\n".encode("ascii")
        document += payload.encode("ascii") + b"\nendobj\n"

    start_xref = len(document)
    size = max(offsets) + 1
    document += f"xref\n0 {size}\n".encode("ascii")
    document += b"0000000000 65535 f \n"
    for object_id in range(1, size):
        document += f"{offsets[object_id]:010d} 00000 n \n".encode("ascii")
    document += (
        f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{start_xref}\n%%EOF\n".encode(
            "ascii"
        )
    )
    return bytes(document)


PDF_SOURCE_BYTES = _build_pdf(["Chapter One Body", "Chapter Two Body"])


def test_txt_parser_contract_returns_text_with_paragraph_locations() -> None:
    """TXT 解析必须返回规范化文本，并为每个段落提供可追溯定位。"""

    parsed = parse_document("课程资料.txt", TXT_SOURCE.encode("utf-8"))

    assert parsed.file_format == "txt"
    assert parsed.text == TXT_SOURCE
    assert [section.text for section in parsed.sections] == [
        "第一段内容，用于验证段落定位。",
        "第二段内容，用于验证段落切分。",
    ]
    assert [section.location for section in parsed.sections] == ["第 1 段", "第 2 段"]
    assert [section.index for section in parsed.sections] == [1, 2]


def test_markdown_parser_contract_strips_syntax_and_keeps_heading_locations() -> None:
    """Markdown 解析必须去掉语法噪声，并保留标题级来源定位。"""

    parsed = parse_document("课程资料.md", MARKDOWN_SOURCE.encode("utf-8"))

    assert parsed.file_format == "markdown"
    assert "title: 不应保留的元数据" not in parsed.text
    assert "**" not in parsed.text
    assert "https://example.com/doc" not in parsed.text
    assert "第一章 概述" in parsed.text
    assert "本节介绍 RAG 基础。" in parsed.text
    assert "参考 设计文档 与 代码片段。" in parsed.text
    assert 'print("代码内容必须保留")' in parsed.text
    # 代码块缩进属于教学内容，解析时不得被抹平。
    assert '    print("代码内容必须保留")' in parsed.text
    assert [section.location for section in parsed.sections] == [
        "标题：第一章 概述",
        "标题：1.1 检索",
    ]
    assert "RAG" in parsed.sections[0].text


def test_pdf_parser_contract_extracts_text_with_page_locations() -> None:
    """PDF 解析必须逐页提取文本，并保留页码定位与总页数。"""

    parsed = parse_document("课程资料.pdf", PDF_SOURCE_BYTES)

    assert parsed.file_format == "pdf"
    assert parsed.page_count == 2
    assert "Chapter One Body" in parsed.text
    assert "Chapter Two Body" in parsed.text
    assert [section.location for section in parsed.sections] == ["第 1 页", "第 2 页"]


def test_supported_formats_contract_covers_pdf_txt_and_markdown() -> None:
    """注册表必须声明 PDF、TXT 和 Markdown，且扩展名判定不区分大小写。"""

    registry = build_default_registry()

    assert registry.supported_formats == ("markdown", "pdf", "txt")
    assert registry.supported_extensions == ("markdown", "md", "pdf", "txt")
    for filename in ("course.pdf", "课程.TXT", "notes.md", "notes.markdown"):
        assert registry.is_supported(filename), filename
    for filename in ("lesson.docx", "slides.pptx", "archive", "notes.rtf"):
        assert not registry.is_supported(filename), filename


@pytest.mark.parametrize("filename", ["lesson.docx", "slides.pptx", "archive"])
def test_unsupported_format_contract_blocks_knowledge_creation(filename: str) -> None:
    """不支持格式必须直接失败，不得产生任何可入库的知识内容。"""

    with pytest.raises(DocumentParseError) as captured:
        parse_document(filename, b"unsupported-format-payload")

    error = captured.value
    assert error.error_code == DOCUMENT_UNSUPPORTED_FORMAT
    assert error.user_message == "当前仅支持 PDF、TXT 和 Markdown 文件。"
    # 不支持格式属于输入问题，不允许重试，也不得返回任何解析结果。
    assert error.retryable is False
    assert build_default_registry().is_supported(filename) is False


@pytest.mark.parametrize("filename", ["empty.txt", "empty.md", "empty.pdf"])
def test_empty_file_contract_reports_not_retryable_empty_error(filename: str) -> None:
    """空文件必须报告 DOCUMENT_EMPTY，且不得进入可重试流程。"""

    with pytest.raises(DocumentParseError) as captured:
        parse_document(filename, b"")

    error = captured.value
    assert error.error_code == DOCUMENT_EMPTY
    assert error.user_message == "上传的文件为空，请选择包含教学内容的文件。"
    assert error.retryable is False


@pytest.mark.parametrize(
    ("filename", "payload"),
    [
        ("损坏资料.pdf", CORRUPTED_PDF_BYTES),
        ("损坏资料.txt", UNDECODABLE_TEXT_BYTES),
    ],
)
def test_corrupted_file_contract_reports_retryable_corruption(
    filename: str, payload: bytes
) -> None:
    """损坏文件必须报告 DOCUMENT_CORRUPTED，并标记为可重试错误。"""

    with pytest.raises(DocumentParseError) as captured:
        parse_document(filename, payload)

    error = captured.value
    assert error.error_code == DOCUMENT_CORRUPTED
    assert error.user_message == "文件无法读取，可能已损坏，请重新导出后上传。"
    assert error.retryable is True


@pytest.mark.parametrize(
    ("filename", "payload"),
    [
        ("空白资料.txt", WHITESPACE_ONLY_TXT_BYTES),
        ("无标题资料.md", b"#\n\n   \n"),
        ("无正文资料.pdf", _build_pdf([""])),
    ],
    ids=["whitespace-txt", "empty-heading-markdown", "textless-pdf"],
)
def test_no_text_document_contract_reports_not_retryable_no_text(
    filename: str, payload: bytes
) -> None:
    """没有可提取文本的资料必须报告 DOCUMENT_NO_TEXT，且不得重试。"""

    with pytest.raises(DocumentParseError) as captured:
        parse_document(filename, payload)

    error = captured.value
    assert error.error_code == DOCUMENT_NO_TEXT
    assert error.user_message == "资料中没有可提取的文本内容，请检查扫描或文件内容。"
    assert error.retryable is False


def test_error_code_contract_matches_plan_retry_semantics() -> None:
    """失败码提示与重试语义必须与计划中的摄取失败表一致。"""

    assert DOCUMENT_ERROR_MESSAGES == {
        DOCUMENT_EMPTY: "上传的文件为空，请选择包含教学内容的文件。",
        DOCUMENT_UNSUPPORTED_FORMAT: "当前仅支持 PDF、TXT 和 Markdown 文件。",
        DOCUMENT_CORRUPTED: "文件无法读取，可能已损坏，请重新导出后上传。",
        DOCUMENT_NO_TEXT: "资料中没有可提取的文本内容，请检查扫描或文件内容。",
        DOCUMENT_PARSE_FAILED: "资料解析失败，请检查文件内容后重新上传。",
    }
    assert RETRYABLE_ERROR_CODES == frozenset({DOCUMENT_PARSE_FAILED, DOCUMENT_CORRUPTED})

    with pytest.raises(DocumentParseError) as captured:
        parse_document("lesson.docx", b"unsupported-format-payload")
    assert str(captured.value).startswith(DOCUMENT_UNSUPPORTED_FORMAT)


@pytest.mark.parametrize(
    ("filename", "payload"),
    [
        ("课程资料.txt", TXT_SOURCE.encode("utf-8")),
        ("课程资料.md", MARKDOWN_SOURCE.encode("utf-8")),
        ("课程资料.pdf", PDF_SOURCE_BYTES),
    ],
    ids=["txt", "markdown", "pdf"],
)
def test_supported_documents_contract_never_return_blank_text(
    filename: str, payload: bytes
) -> None:
    """受支持资料必须返回非空全文，且每个来源片段都有内容与定位。"""

    parsed = parse_document(filename, payload)

    assert parsed.text.strip()
    assert parsed.sections
    for section in parsed.sections:
        assert section.text.strip(), section.location
        assert section.location.strip()
