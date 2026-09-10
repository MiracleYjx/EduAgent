"""学生成绩与诊断视图。

成绩、复核和诊断服务尚未就绪时，仅展示明确空态，不在前端计算或伪造结果。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import gradio as gr

from backend.app.domain.enums import UserRole
from backend.app.domain.permissions import PermissionDeniedError, normalize_role
from backend.app.ui.layout_view import empty_state, feedback

RESULT_HEADERS = ("题号", "状态", "得分", "反馈")
UNAVAILABLE_MESSAGE = "成绩与诊断功能暂未就绪，请等待 M3 阶段完成"


@dataclass(frozen=True)
class ResultsView:
    """学生成绩视图中由主工作台控制的组件。"""

    panel: gr.Column
    results_table: gr.Dataframe
    message: gr.Markdown


def _ensure_student(state: Mapping[str, Any]) -> None:
    """只允许已登录学生读取自己的结果。"""

    if not state.get("access_token"):
        raise PermissionDeniedError("请先登录。")
    try:
        roles = {normalize_role(role) for role in state.get("roles", [])}
    except (TypeError, ValueError) as error:
        raise PermissionDeniedError("当前账号无权访问成绩与诊断功能。") from error
    if UserRole.STUDENT not in roles:
        raise PermissionDeniedError("当前账号无权访问成绩与诊断功能。")


def refresh_student_results(
    exam_id: str | None = None, state: Mapping[str, Any] | None = None
) -> tuple[list[list[str]], str]:
    """读取学生授权结果；依赖未就绪时返回空态。"""

    if state is not None:
        try:
            _ensure_student(state)
        except PermissionDeniedError as error:
            return [], feedback(str(error), "error")
    return [], empty_state(UNAVAILABLE_MESSAGE)


def create_results_view(session_state: Any | None = None) -> ResultsView:
    """创建考试选择、结果摘要、逐题结果和诊断区域。"""

    state = session_state or gr.State({"access_token": "", "roles": []})
    with gr.Column(visible=False, elem_classes="edu-results") as panel:
        gr.HTML(
            "<style>.edu-results .result-summary {min-height:74px;}"
            ".edu-results .result-tabs {min-height:320px;}"
            "@media(max-width:767px){.edu-results .result-summary-row{flex-wrap:wrap;}}</style>"
        )
        gr.Markdown("## 成绩与诊断")
        with gr.Row():
            exam = gr.Dropdown(label="考试", choices=[], value=None)
            refresh = gr.Button("刷新结果", variant="primary")
        gr.Markdown(empty_state(UNAVAILABLE_MESSAGE))
        with gr.Row(elem_classes="result-summary-row"):
            gr.Textbox(label="总分", value="暂无", interactive=False, elem_classes="result-summary")
            gr.Textbox(label="已评分题数", value="暂无", interactive=False, elem_classes="result-summary")
            gr.Textbox(label="待复核题数", value="暂无", interactive=False, elem_classes="result-summary")
        with gr.Tabs(elem_classes="result-tabs"):
            with gr.Tab("逐题结果"):
                results_table = gr.Dataframe(
                    headers=list(RESULT_HEADERS), datatype=["str"] * 4,
                    value=[], interactive=False, label="逐题结果"
                )
                gr.Markdown(empty_state("暂无逐题结果可展示。"))
            with gr.Tab("错题与诊断"), gr.Row(equal_height=False):
                with gr.Column(scale=1):
                    gr.Markdown("### 薄弱知识点")
                    gr.Markdown(empty_state("暂无可展示的掌握度数据。"))
                with gr.Column(scale=1):
                    gr.Markdown("### 错误原因与学习建议")
                    gr.Markdown(empty_state("诊断报告尚未生成"))
        message = gr.Markdown(empty_state("暂无可展示的诊断"))
        refresh.click(
            refresh_student_results,
            inputs=[exam, state],
            outputs=[results_table, message],
            show_progress="hidden",
        )
    return ResultsView(panel=panel, results_table=results_table, message=message)


build_results_view = create_results_view

__all__ = [
    "RESULT_HEADERS",
    "UNAVAILABLE_MESSAGE",
    "ResultsView",
    "build_results_view",
    "create_results_view",
    "refresh_student_results",
]
