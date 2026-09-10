"""教师阅卷复核视图。

评分结果和复核服务尚未接入时，本视图只展示明确的不可用态，不在界面层
构造示例分数、置信度、评分理由或检索依据。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import gradio as gr

from backend.app.domain.enums import UserRole
from backend.app.domain.permissions import PermissionDeniedError, normalize_role
from backend.app.ui.layout_view import empty_state, feedback

REVIEW_QUEUE_HEADERS = ("学生", "考试", "题号", "复核状态")
REVIEW_STATUS_CHOICES = (
    ("待人工复核", "Pending Review"),
    ("已确认", "Confirmed"),
    ("已修改", "Modified"),
    ("复核已完成", "Final"),
)


@dataclass(frozen=True)
class ReviewView:
    """复核视图中可由主应用切换的组件集合。"""

    panel: gr.Column
    queue: gr.Dataframe
    answer: gr.Textbox
    score: gr.Number
    reason: gr.Textbox
    evidence: gr.Markdown
    message: gr.Markdown


def _empty_state() -> dict[str, Any]:
    """返回视图使用的空会话状态。"""

    return {"access_token": "", "roles": [], "username": ""}


def _ensure_teacher(state: Mapping[str, Any]) -> None:
    """复核动作必须由已登录教师调用；具体权限仍由后端服务校验。"""

    if not state.get("access_token"):
        raise PermissionDeniedError("请先登录。")
    try:
        roles = {normalize_role(role) for role in state.get("roles", [])}
    except (TypeError, ValueError) as error:
        raise PermissionDeniedError("当前账号无权访问阅卷复核功能。") from error
    if UserRole.TEACHER not in roles:
        raise PermissionDeniedError("当前账号无权访问阅卷复核功能。")


def _value(item: Mapping[str, Any] | Any, name: str, default: Any = "") -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def review_queue_rows(records: Sequence[Mapping[str, Any] | Any]) -> list[list[str]]:
    """将后端复核记录转换成队列表格，不推导任何业务状态。"""

    rows: list[list[str]] = []
    for record in records:
        rows.append(
            [
                str(_value(record, "student_name", _value(record, "student", ""))),
                str(_value(record, "exam_name", _value(record, "exam", ""))),
                str(_value(record, "question_number", _value(record, "question_no", ""))),
                str(_value(record, "review_status", "")),
            ]
        )
    return rows


def _evidence_markdown(evidence: Sequence[Mapping[str, Any] | Any] | None) -> str:
    """渲染编号检索依据；没有真实依据时显示空态。"""

    if not evidence:
        return empty_state("暂无检索依据可展示")
    lines: list[str] = []
    for index, item in enumerate(evidence, start=1):
        source = str(_value(item, "source_file", _value(item, "source", "")))
        location = str(_value(item, "location", _value(item, "page_or_paragraph", "")))
        snippet = str(_value(item, "snippet", _value(item, "content", "")))
        lines.append(f"{index}. **{source}**（{location}）\n   {snippet}")
    return "\n\n".join(lines)


def _review_detail(record: Mapping[str, Any] | Any | None) -> dict[str, Any]:
    """读取一条真实复核记录用于详情区。"""

    if record is None:
        return {
            "student": "",
            "exam": "",
            "question": "",
            "confidence": "",
            "answer": "",
            "score": None,
            "reason": "",
            "knowledge_points": [],
            "reference_answer": "",
            "scoring_rubric": "",
            "evidence": [],
        }
    return {
        "student": _value(record, "student_name", _value(record, "student", "")),
        "exam": _value(record, "exam_name", _value(record, "exam", "")),
        "question": _value(record, "question_number", _value(record, "question_no", "")),
        "confidence": _value(record, "confidence", ""),
        "answer": _value(record, "student_answer", _value(record, "answer", "")),
        "score": _value(record, "score", None),
        "reason": _value(record, "reason", ""),
        "knowledge_points": _value(record, "knowledge_points", []),
        "reference_answer": _value(record, "reference_answer", ""),
        "scoring_rubric": _value(record, "scoring_rubric", ""),
        "evidence": _value(record, "evidence", _value(record, "retrieved_evidence", [])),
    }


def refresh_review_queue(
    exam_id: str = "",
    student_id: str = "",
    review_status: str = "",
    state: Mapping[str, Any] | None = None,
) -> tuple[list[list[str]], str]:
    """刷新复核队列。

    T050/T060-T062/T074/T077 未就绪时不访问数据库，也不填充假记录。
    """

    if state is not None:
        try:
            _ensure_teacher(state)
        except PermissionDeniedError as error:
            return [], feedback(str(error), "error")
    return [], empty_state("阅卷复核功能暂未就绪，请等待 M3/M4 阶段完成")


def confirm_review(*_: Any) -> str:
    """确认评分的占位回执，保持原服务状态。"""

    return feedback("阅卷复核功能暂未就绪，请等待 M3/M4 阶段完成", "info")


def save_review_changes(*_: Any) -> str:
    """保存修改的占位回执，不伪造保存成功。"""

    return feedback("阅卷复核功能暂未就绪，请等待 M3/M4 阶段完成", "info")


def _confidence_text(confidence: Any) -> str:
    if confidence in (None, ""):
        return "暂无可展示的复核项"
    try:
        return f"实际置信度：{float(confidence):.0%}"
    except (TypeError, ValueError):
        return "暂无可展示的复核项"


def create_review_view(session_state: Any | None = None) -> ReviewView:
    """创建教师阅卷复核工作台。"""

    state = session_state or gr.State(_empty_state())
    with gr.Column(visible=False, elem_classes="edu-review") as panel:
        gr.HTML(
            "<style>.edu-review .review-actions {position:sticky;bottom:0;"
            "z-index:5;background:var(--background-fill-primary);padding:12px 0;}"
            ".edu-review .review-queue-column {flex:0 0 240px !important;}"
            ".edu-review .review-actions button {min-height:44px;}"
            "@media(max-width:767px){.edu-review .review-queue-column "
            "{flex:1 1 100% !important;}}</style>"
        )
        gr.Markdown("## 阅卷复核")
        with gr.Row(elem_classes=["review-filters"]):
            exam_filter = gr.Textbox(label="考试", placeholder="按考试筛选")
            student_filter = gr.Textbox(label="学生", placeholder="按学生筛选")
            status_filter = gr.Dropdown(
                choices=list(REVIEW_STATUS_CHOICES),
                label="复核状态",
                value="Pending Review",
            )
            refresh = gr.Button("刷新队列", variant="primary")
        message = gr.Markdown(empty_state("暂无可复核的评分记录"))
        with gr.Row(equal_height=False):
            with gr.Column(scale=0, min_width=240, elem_classes="review-queue-column"):
                gr.Markdown("### 复核队列")
                queue = gr.Dataframe(
                    headers=list(REVIEW_QUEUE_HEADERS),
                    datatype=["str"] * len(REVIEW_QUEUE_HEADERS),
                    value=[],
                    interactive=False,
                    label="待复核评分",
                    elem_classes=["review-queue"],
                )
            with gr.Column(scale=1, min_width=0):
                with gr.Row():
                    gr.Markdown("**学生：** 暂无")
                    gr.Markdown("**考试：** 暂无")
                    gr.Markdown("**题号：** 暂无")
                    gr.Markdown("**实际置信度：** 暂无")
                gr.Markdown("### 待人工复核")
                gr.Markdown(feedback("暂无可展示的复核项", "warning"))
                with gr.Row(equal_height=False):
                    with gr.Column(scale=45):
                        answer = gr.Textbox(
                            label="学生答案（原文）",
                            lines=14,
                            interactive=False,
                        )
                    with gr.Column(scale=55):
                        score = gr.Number(label="AI 分数", value=None, precision=2, interactive=True)
                        reason = gr.Textbox(label="评分理由", lines=5, interactive=True)
                        gr.Markdown("**知识点：** 暂无")
                        gr.Markdown("**参考答案：** 暂无")
                        gr.Markdown("**评分标准：** 暂无")
                with gr.Accordion("检索依据", open=False):
                    evidence = gr.Markdown(empty_state("暂无检索依据可展示"))
                with gr.Row(elem_classes=["review-actions"]):
                    confirm = gr.Button("确认评分", variant="primary", interactive=False)
                    save = gr.Button("保存修改", interactive=False)
                    next_item = gr.Button("下一条", interactive=False)

        refresh.click(
            fn=refresh_review_queue,
            inputs=[exam_filter, student_filter, status_filter, state],
            outputs=[queue, message],
            show_progress="hidden",
        )
        confirm.click(fn=confirm_review, inputs=[], outputs=[message], show_progress="hidden")
        save.click(fn=save_review_changes, inputs=[], outputs=[message], show_progress="hidden")
        next_item.click(fn=lambda: feedback("暂无可展示的复核项", "info"), inputs=[], outputs=[message])

    return ReviewView(
        panel=panel,
        queue=queue,
        answer=answer,
        score=score,
        reason=reason,
        evidence=evidence,
        message=message,
    )


build_review_view = create_review_view


__all__ = [
    "REVIEW_QUEUE_HEADERS",
    "REVIEW_STATUS_CHOICES",
    "ReviewView",
    "build_review_view",
    "confirm_review",
    "create_review_view",
    "refresh_review_queue",
    "review_queue_rows",
    "save_review_changes",
]
