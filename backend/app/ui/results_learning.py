"""T183 safe display of authorized feedback; no score or scope inference."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from html import escape
from typing import Any

from backend.app.domain.permissions import PermissionDeniedError
from backend.app.ui.results_analysis import answer_text, labels, text, value

LEARNING_HEADERS = (
    "题号",
    "本人答案",
    "处理状态",
    "得分",
    "本场满分",
    "最终失分",
    "评分解释",
    "发布知识点",
)
RECOMMENDATION_HEADERS = ("类型", "关联知识点", "来源文件", "真实来源ID", "推荐理由")


def learning_rows(feedback: Any | None) -> list[list[str]]:
    return [
        [
            text(value(i, "order")),
            answer_text(value(i, "student_answer")),
            text(value(i, "grading_status")),
            text(value(i, "score"), "尚未确认"),
            text(value(i, "max_score"), "未知（待核对）"),
            text(value(i, "lost_score"), "尚无最终失分"),
            text(value(i, "reason"), "暂无评分解释"),
            labels(value(i, "published_knowledge_points")),
        ]
        for i in value(feedback, "items", [])
    ]


def recommendation_rows(
    feedback: Any | None,
) -> tuple[list[list[str]], list[dict[str, str]]]:
    rows = []
    records = []
    for group in value(feedback, "recommendations", []):
        for kind, field, label, key in [
            ("material", "materials", "复习片段", "chunk_id"),
            ("practice", "practices", "已审核练习", "question_id"),
        ]:
            for item in value(group, field, []):
                sources = (
                    [value(item, "source")]
                    if kind == "material"
                    else value(item, "sources", [])
                )
                rows.append(
                    [
                        label,
                        text(value(group, "knowledge_point")),
                        "；".join(text(value(s, "source_file")) for s in sources),
                        "；".join(
                            text(value(s, "source_id", value(s, "chunk_id")))
                            for s in sources
                        ),
                        text(value(item, "reason")),
                    ]
                )
                records.append(
                    {
                        "kind": kind,
                        "resource_id": str(value(item, key)),
                        "submission_id": str(value(feedback, "submission_id")),
                    }
                )
    return rows, records


def learning_message(feedback: Any | None) -> str:
    if feedback is None:
        return "暂无本人学习反馈；选择已有答卷的考试后刷新。"
    state = value(feedback, "processing_status", "unfinished")
    names = {
        "final": "当前最终成绩",
        "pending_review": "待人工复核",
        "failed": "阅卷失败",
        "insufficient_evidence": "评分依据不足",
        "unfinished": "尚未完成",
    }
    parts = [
        names.get(state, state),
        text(value(feedback, "processing_reason"), "只读当前已确认结果。"),
    ]
    code = value(feedback, "processing_error_code")
    if code:
        parts.append(f"真实错误码：{code}")
    if not value(feedback, "is_final", False):
        parts.append("尚无最终失分、薄弱知识点或复习推荐；不把失败解释成零分。")
    if value(feedback, "insufficient_evidence_answer_ids", []):
        parts.append(
            "部分答案缺发布标签或固定归因依据："
            + "、".join(value(feedback, "insufficient_evidence_answer_ids"))
        )
    if value(feedback, "source_exam_result_updated_at"):
        parts.append(
            "来源汇总时间：" + str(value(feedback, "source_exam_result_updated_at"))
        )
    for group in value(feedback, "recommendations", []):
        for field in ["material_not_ready_reason", "practice_not_ready_reason"]:
            reason = value(group, field)
            if reason:
                parts.append(str(value(group, "knowledge_point")) + "：" + str(reason))
    parts.append("多标签归属不能相加成整卷失分；资料原文件仍沿用教师下载权限。")
    return "\n\n".join(escape(str(p)) for p in parts)


def image_html(data: bytes, mime: str, caption: str) -> str:
    return f'<figure><img alt="{escape(caption,quote=True)}" src="data:{escape(mime,quote=True)};base64,{base64.b64encode(data).decode("ascii")}" style="max-width:100%;height:auto;max-height:780px"/><figcaption>{escape(caption)}</figcaption></figure>'


def error_html(error: Exception) -> str:
    code = getattr(error, "code", getattr(error, "error_code", "LEARNING_READ_FAILED"))
    message = (
        getattr(error, "detail", None)
        or getattr(error, "message", None)
        or (str(error) if isinstance(error, PermissionDeniedError) else None)
        or "当前内容读取失败，请刷新后重试。"
    )
    return f'<p role="alert">{escape(str(code))}：{escape(str(message))}</p>'


def question_html(item: Any, images: str) -> str:
    parts = [
        f'第 {text(value(item,"order"))} 题',
        "本人答案：" + answer_text(value(item, "student_answer")),
        "得分："
        + text(value(item, "score"), "尚未确认")
        + "/"
        + text(value(item, "max_score"), "未知"),
        "最终失分：" + text(value(item, "lost_score"), "尚无最终失分"),
        "评分解释：" + text(value(item, "reason"), "暂无"),
        "发布知识点：" + labels(value(item, "published_knowledge_points")),
    ]
    if value(item, "image_unavailable_reason"):
        parts.append(value(item, "image_unavailable_reason"))
    return (
        '<section class="edu-surface"><div style="white-space:pre-wrap">'
        + escape("\n".join(parts))
        + "</div>"
        + images
        + "</section>"
    )


def material_html(detail: Any) -> str:
    source = value(value(detail, "recommendation"), "source")
    header = (
        "来源："
        + text(value(source, "source_file"))
        + "；Chunk "
        + text(value(source, "chunk_id"))
    )
    return (
        '<section class="edu-surface"><h3>复习片段</h3><p>'
        + escape(header)
        + '</p><div style="white-space:pre-wrap">'
        + escape(str(value(detail, "content")))
        + "</div></section>"
    )


def practice_html(detail: Any, images: str) -> str:
    recommendation = value(detail, "recommendation")
    sources = value(recommendation, "sources", [])
    header = "；".join(
        text(value(s, "source_file"))
        + " / "
        + text(value(s, "source_id"))
        + " ("
        + text(value(s, "kind"))
        + ")"
        for s in sources
    )
    options = value(detail, "options")
    option_text = (
        "\n".join(f"{k}：{v}" for k, v in options.items())
        if isinstance(options, Mapping)
        else answer_text(options)
    )
    unknown = (
        "" if value(detail, "order_preserved", False) else "<p>历史选项原序未知。</p>"
    )
    return (
        '<section class="edu-surface"><h3>已审核练习</h3><p>'
        + escape(header)
        + '</p><div style="white-space:pre-wrap">'
        + escape(str(value(detail, "content")))
        + "\n"
        + escape(option_text)
        + "</div>"
        + unknown
        + images
        + "</section>"
    )
