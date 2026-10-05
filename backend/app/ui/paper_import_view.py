"""Independent teacher paper upload entry; reads persisted progress and corrections."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import gradio as gr

from backend.app.ui import paper_import_loaders as loaders
from backend.app.ui.paper_correction_view import (
    PAPER_STATES,
    create_paper_correction_view,
    ui_errors,
)


@dataclass
class PaperImportView:
    panel: gr.Column
    course: gr.Dropdown
    imports: gr.Dropdown
    message: gr.Markdown
    file: gr.File
    reset: Callable[[], dict[Any, Any]]


def create_paper_import_view(session_state: Any | None = None) -> PaperImportView:
    state = session_state if session_state is not None else gr.State({})
    with gr.Column(
        visible=False,
        elem_id="edu-paper-import",
        elem_classes=["edu-paper-import", "edu-business-page"],
    ) as panel:
        gr.Markdown("## 试卷导入与校正")
        gr.Markdown(
            "上传原卷 → 解析与提取 → 对照原页校正 → 确认草稿。支持 PDF、PNG、JPEG，最多 50 页。"
        )
        with gr.Row():
            course = gr.Dropdown(label="导入课程", choices=[], interactive=True)
            refresh_courses = gr.Button(
                "刷新课程", elem_id="edu-paper-courses", elem_classes="edu-icon"
            )
        with gr.Accordion("导入新试卷", open=False, elem_classes="edu-surface"):
            file = gr.File(
                label="原试卷",
                file_types=[".pdf", ".png", ".jpg", ".jpeg"],
                type="filepath",
            )
            upload_button = gr.Button(
                "上传并提取",
                variant="primary",
                elem_id="edu-paper-upload",
                elem_classes="edu-icon",
            )
        message = gr.Markdown("选择课程并上传原卷，或打开已有导入记录。")
        with gr.Row():
            imports = gr.Dropdown(label="试卷导入记录", choices=[], interactive=True)
            refresh = gr.Button(
                "刷新进度 / 重新读取",
                elem_id="edu-paper-refresh",
                elem_classes="edu-icon",
            )
        editor = create_paper_correction_view(imports, state)

    @ui_errors
    def course_choices(current_state):
        return gr.update(choices=loaders.courses(current_state), value=None)

    @ui_errors
    def list_imports(course_id, current_state):
        records = loaders.list_imports(course_id, current_state) if course_id else []
        return gr.update(
            choices=[
                (
                    f"{p.original_filename} · {PAPER_STATES[p.status.value]} · {p.created_at:%m-%d %H:%M}",
                    str(p.id),
                )
                for p in records
            ],
            value=None,
        )

    def upload_action(course_id, filename, current_state):
        if not course_id or not filename:
            raise gr.Error("请选择课程和原试卷。")

        @ui_errors
        def receive():
            path = Path(filename)
            return loaders.upload(
                course_id, path.name, path.read_bytes(), current_state
            )

        paper = receive()
        choices = list_imports(course_id, current_state)
        choices["value"] = str(paper.id)
        yield choices, "原卷已保存，正在解析；可刷新进度查看已产生的页与题。"
        ui_errors(loaders.run)(str(paper.id), current_state)
        latest = loaders.load(str(paper.id), current_state)
        choices = list_imports(course_id, current_state)
        choices["value"] = str(paper.id)
        yield (
            choices,
            (
                "处理失败：" + (latest.error_message or latest.error_code or "")
                if latest.status.value == "Failed"
                else "提取结束，请对照原页逐题校正。"
            ),
        )

    refresh_courses.click(course_choices, inputs=[state], outputs=course)
    course.change(list_imports, inputs=[course, state], outputs=imports)
    upload_button.click(
        upload_action, inputs=[course, file, state], outputs=[imports, message]
    )
    imports.change(
        lambda identity, s: editor.reload(identity, None, s),
        inputs=[imports, state],
        outputs=editor.outputs,
    )
    refresh.click(
        lambda identity, s: editor.reload(identity, None, s),
        inputs=[imports, state],
        outputs=editor.outputs,
    )

    def reset():
        return {
            **editor.reload(None, None, {}),
            course: gr.update(choices=[], value=None),
            imports: gr.update(choices=[], value=None),
            file: gr.update(value=None),
            message: "选择课程并上传原卷，或打开已有导入记录。",
        }

    return PaperImportView(panel, course, imports, message, file, reset)
