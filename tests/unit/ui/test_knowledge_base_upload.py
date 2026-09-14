"""T039 知识库资料区单元测试：真实上传组件、真实阶段与失败原因展示。

只测试视图层的纯函数与组件构建，不访问数据库或网络；摄取成功/失败的真实语义由
``tests/unit/services/test_knowledge_base_ingestion.py`` 与 API 契约测试覆盖。
"""

from __future__ import annotations

from typing import Any

import gradio as gr

from backend.app.domain.enums import DocumentStatus
from backend.app.services.knowledge_base_service import DocumentIngestionResult
from backend.app.ui.knowledge_base_view import (
    DOCUMENT_TABLE_HEADERS,
    INGESTION_READY,
    create_knowledge_base_view,
    upload_document,
)


def _result(
    status: DocumentStatus,
    *,
    chunk_count: int = 0,
    error_code: str | None = None,
    error_message: str | None = None,
    retryable: bool = False,
) -> DocumentIngestionResult:
    """构造摄取结果摘要替身。"""

    return DocumentIngestionResult(
        document_id="11111111-1111-4111-8111-111111111111",
        status=status,
        chunk_count=chunk_count,
        error_code=error_code,
        error_message=error_message,
        retryable=retryable,
    )


def _chunks(count: int) -> list[dict[str, Any]]:
    """构造知识片段摘要替身。"""

    return [
        {
            "id": f"chunk-{index}",
            "chunk_index": index,
            "content": f"第 {index} 段课程内容",
            "metadata": {"location": f"第 {index} 段", "document_id": "doc-1"},
            "has_embedding": True,
        }
        for index in range(count)
    ]


def test_document_stage_summary_reports_ready_with_chunk_count() -> None:
    """成功后展示真实阶段与片段数量。"""

    from backend.app.ui.knowledge_base_view import _document_stage_summary

    text = _document_stage_summary(_result(DocumentStatus.READY, chunk_count=3), _chunks(3))

    assert "已生成 3 个知识片段" in text
    assert "失败原因" not in text


def test_document_stage_summary_reports_failure_reason_and_retryability() -> None:
    """失败时展示可读原因与恢复路径，不显示 Ready。"""

    from backend.app.ui.knowledge_base_view import _document_stage_summary

    text = _document_stage_summary(
        _result(
            DocumentStatus.FAILED,
            error_code="DOCUMENT_PARSE_FAILED",
            error_message="资料解析失败，请检查文件内容后重新上传。",
            retryable=True,
        ),
        [],
    )

    assert "失败原因" in text
    assert "资料解析失败，请检查文件内容后重新上传。" in text
    assert "修正后可重新上传" in text


def test_document_stage_summary_terminal_failure_hints_new_upload() -> None:
    """不可重试的失败（如空知识片段）提示重新上传而不是直接重试。"""

    from backend.app.ui.knowledge_base_view import _document_stage_summary

    text = _document_stage_summary(
        _result(
            DocumentStatus.FAILED,
            error_code="KNOWLEDGE_BASE_EMPTY",
            error_message="资料未形成有效知识片段，暂不能用于出题或阅卷。",
            retryable=False,
        ),
        [],
    )

    assert "需补充或修正资料后重新上传" in text


def test_source_details_lists_locations_and_empty_state() -> None:
    """来源详情展示片段定位；没有片段时给出明确空态。"""

    from backend.app.ui.knowledge_base_view import _source_details

    details = _source_details(_chunks(2))
    assert "来源详情" in details
    assert "第 0 段" in details
    assert "第 1 段" in details

    empty = _source_details([])
    assert "暂无知识片段" in empty


def test_upload_document_requires_selected_knowledge_base() -> None:
    """未选择知识库时给出中文提示，不发起摄取。"""

    rows, _status, details, message = upload_document(
        "C:/tmp/lesson.txt",
        "",
        {"user_id": "user-1", "roles": ["Teacher"], "access_token": "token"},
    )

    assert rows == []
    assert "请先选择知识库" in message
    assert details


def test_upload_document_requires_file_selection() -> None:
    """未选择文件时给出中文提示。"""

    _, _, _, message = upload_document(
        None,
        "11111111-1111-4111-8111-111111111111",
        {"user_id": "user-1", "roles": ["Teacher"], "access_token": "token"},
    )

    assert "请选择要上传" in message


def test_upload_document_rejects_non_teacher_state() -> None:
    """非教师状态被拒绝，摄取入口不对外开放。"""

    _, _, _, message = upload_document(
        "C:/tmp/lesson.txt",
        "11111111-1111-4111-8111-111111111111",
        {"user_id": "user-1", "roles": ["Student"], "access_token": "token"},
    )

    assert message


def test_knowledge_base_view_builds_with_real_file_upload() -> None:
    """视图构建成功，且资料区已启用真实文件上传。"""

    with gr.Blocks():
        view = create_knowledge_base_view(gr.State({"user_id": "", "roles": []}))

    assert view.documents_table is not None
    assert view.knowledge_bases_dropdown is not None
    assert INGESTION_READY is True
    assert DOCUMENT_TABLE_HEADERS == ("文件名", "格式", "处理阶段", "更新时间")
