"""T033 文本清洗单元测试：确定性、幂等和教学内容保真。"""

from __future__ import annotations

from backend.app.ai.ingestion.cleaning import (
    CleanedSection,
    clean_document,
    clean_text,
    is_blank,
)
from backend.app.ai.ingestion.parsers import ParsedDocument, ParsedSection

MESSY_TEXT = (
    "\ufeff标题一\r\n"
    "正文一。  \r\n"
    "\r\n"
    "\r\n"
    "正文二\t继续  说明。\u200b  终。"
)
CLEANED_TEXT = "标题一\n正文一。\n\n正文二 继续 说明。 终。"


def test_clean_text_contract_normalizes_noise_and_removes_invisibles() -> None:
    """清洗必须统一换行、去掉不可见字符并规范空白。"""

    assert clean_text(MESSY_TEXT) == CLEANED_TEXT


def test_clean_text_contract_is_idempotent() -> None:
    """同一文本重复清洗必须得到完全相同的结果。"""

    once = clean_text(MESSY_TEXT)
    assert clean_text(once) == once
    assert clean_text("\r\n\r\n   ") == ""


def test_clean_text_contract_preserves_teaching_content() -> None:
    """代码缩进、中文标点和公式符号属于教学内容，不得被清洗破坏。"""

    code = "示例代码：\n    print(1)\n        print(2)"
    assert clean_text(code) == code

    formula = "知识点：f(x)=x²，当 a≠b 时取最大值为 100%。"
    assert clean_text(formula) == formula


def test_is_blank_contract_matches_clean_text_emptiness() -> None:
    """空白判定必须与清洗结果一致，避免产生无内容片段。"""

    for text in ("", "   \n\t\u3000 ", "\ufeff\u200b", "\x00\x01\x02"):
        assert is_blank(text) is True
        assert clean_text(text) == ""
    for text in ("内容", "。", "0"):
        assert is_blank(text) is False
        assert clean_text(text) != ""


def test_clean_document_contract_keeps_locations_and_drops_blank_sections() -> None:
    """清洗解析结果时必须保留来源定位，并丢弃清洗后为空的片段。"""

    parsed = ParsedDocument(
        file_format="txt",
        text="有效内容。\n\n   \n",
        sections=(
            ParsedSection(index=1, location="第 1 段", text="  有效内容。  "),
            ParsedSection(index=2, location="第 2 段", text="   \n\t "),
        ),
    )

    cleaned = clean_document(parsed)

    assert cleaned.file_format == "txt"
    assert cleaned.text == "有效内容。"
    assert cleaned.page_count is None
    assert cleaned.sections == (
        CleanedSection(index=1, location="第 1 段", text="有效内容。"),
    )
