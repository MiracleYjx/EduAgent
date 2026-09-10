"""教师 AI 出题视图：在生成契约未就绪时提供明确的不可用态。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import gradio as gr
from sqlalchemy.exc import SQLAlchemyError

from backend.app.core.database import get_session_factory
from backend.app.domain.enums import QuestionType, UserRole
from backend.app.domain.permissions import PermissionDeniedError, normalize_role
from backend.app.services.course_service import CourseService, CourseServiceError
from backend.app.ui.layout_view import empty_state, feedback, status_choices

AI_GENERATION_READY = False
AI_UNAVAILABLE_MESSAGE = "AI 出题功能暂未就绪，请等待 M4 阶段完成。"
CONDITION_HEADERS = ("条件", "当前值")
CANDIDATE_HEADERS = ("候选题目", "题型", "状态")


@dataclass(frozen=True)
class QuestionGenerationView:
    """AI 出题面板中由主工作台控制的组件。"""

    panel: gr.Column
    candidates_table: gr.Dataframe
    message: gr.Markdown


def _empty_state() -> dict[str, Any]:
    """返回独立视图使用的空会话状态。"""

    return {"access_token": "", "roles": [], "user_id": "", "username": ""}


def _ensure_teacher(state: Mapping[str, Any]) -> str:
    """确认当前会话是已登录教师，并返回用户标识。"""

    if not state.get("access_token"):
        raise PermissionDeniedError("请先登录。")
    try:
        roles = {normalize_role(role) for role in state.get("roles", [])}
    except (TypeError, ValueError) as exc:
        raise PermissionDeniedError("当前账号无权访问 AI 出题功能。") from exc
    if UserRole.TEACHER not in roles:
        raise PermissionDeniedError("当前账号无权访问 AI 出题功能。")
    user_id = str(state.get("user_id") or "")
    if not user_id:
        raise PermissionDeniedError("登录状态缺少用户标识。")
    return user_id


def _course_choices(state: Mapping[str, Any]) -> list[tuple[str, str]]:
    """读取真实课程上下文，课程为空时不创建占位数据。"""

    teacher_id = _ensure_teacher(state)
    with get_session_factory()() as session:
        courses = CourseService(session).list_courses(teacher_id=teacher_id)
    return [(course.name, course.id) for course in courses]


def refresh_generation_context(state: Mapping[str, Any]) -> tuple[Any, str]:
    """刷新当前课程下拉框，并保留 AI 链路不可用提示。"""

    try:
        choices = _course_choices(state)
        return (
            gr.update(choices=choices, value=choices[0][1] if choices else None),
            feedback(
                AI_UNAVAILABLE_MESSAGE + (" 当前教师暂无课程。" if not choices else ""),
                "warning",
            ),
        )
    except (PermissionDeniedError, CourseServiceError, SQLAlchemyError) as error:
        return gr.update(choices=[], value=None), feedback(str(error), "error")


def show_course_context(course_id: str | None, state: Mapping[str, Any]) -> str:
    """显示当前课程名称；没有课程时保持明确空态。"""

    try:
        choices = _course_choices(state)
        name = next(
            (name for name, identifier in choices if identifier == course_id), None
        )
        return f"### 当前课程：{name}" if name else empty_state("尚未选择课程。")
    except (PermissionDeniedError, CourseServiceError, SQLAlchemyError) as error:
        return feedback(str(error), "error")


def unavailable_generation(*_: Any) -> tuple[list[list[str]], str, Any, Any, Any]:
    """明确拒绝调用未实现的生成链路，不产生伪造候选题。"""

    return (
        [],
        AI_UNAVAILABLE_MESSAGE,
        empty_state("暂无候选题目。生成契约未就绪，当前不展示示例数据。"),
        gr.update(interactive=False),
        gr.update(interactive=False),
    )


def create_question_generation_view(
    session_state: Any | None = None,
) -> QuestionGenerationView:
    """创建左条件、右候选和来源折叠区的 AI 出题面板。"""

    state = session_state or gr.State(_empty_state())
    with gr.Column(visible=False, elem_classes="edu-question-generation") as panel:
        gr.Markdown("## AI 出题")
        current_course = gr.Markdown(empty_state("尚未选择课程。"))
        with gr.Row():
            with gr.Column(scale=30, min_width=260):
                course = gr.Dropdown(label="课程", choices=[])
                knowledge_point = gr.Textbox(label="知识点", placeholder="输入知识点")
                difficulty = gr.Dropdown(
                    label="难度",
                    choices=["简单", "中等", "困难"],
                    value="中等",
                )
                question_type = gr.Dropdown(
                    label="题型",
                    choices=status_choices(
                        [item.value for item in QuestionType],
                        entity="question_type",
                    ),
                    value=QuestionType.SHORT_ANSWER.value,
                )
                amount = gr.Number(label="数量", value=1, minimum=1, precision=0)
                refresh_button = gr.Button("刷新课程", variant="secondary")
                generate_button = gr.Button(
                    "生成题目（暂未就绪）",
                    variant="primary",
                    interactive=False,
                )
                generation_state = gr.Markdown(
                    feedback(AI_UNAVAILABLE_MESSAGE, "warning")
                )
            with gr.Column(scale=70, min_width=420):
                candidates = gr.Dataframe(
                    headers=list(CANDIDATE_HEADERS),
                    datatype=["str", "str", "markdown"],
                    value=[],
                    interactive=False,
                    label="候选题列表（待教师审核）",
                )
                candidate_preview = gr.Markdown(empty_state("暂无候选题目。"))
                with gr.Row():
                    approve_button = gr.Button("审核通过", interactive=False)
                    revision_button = gr.Button("退回修订", interactive=False)
                gr.Textbox(
                    label="修订意见",
                    lines=3,
                    interactive=False,
                )
        with gr.Accordion("检索来源", open=False):
            gr.Markdown(
                empty_state(
                    "检索来源暂不可用。T067/T068/T075 未就绪，当前不展示虚构引用。"
                )
            )
        message = gr.Markdown(empty_state(AI_UNAVAILABLE_MESSAGE))

        refresh_button.click(
            refresh_generation_context,
            inputs=[state],
            outputs=[course, message],
            show_progress="hidden",
        )
        course.change(
            show_course_context,
            inputs=[course, state],
            outputs=[current_course],
            show_progress="hidden",
        )
        # 生成契约未实现，按钮保持禁用；绑定仅用于未来接线时的输出结构稳定性。
        generate_button.click(
            unavailable_generation,
            inputs=[course, knowledge_point, difficulty, question_type, amount, state],
            outputs=[
                candidates,
                generation_state,
                candidate_preview,
                approve_button,
                revision_button,
            ],
            show_progress="hidden",
        )

    return QuestionGenerationView(
        panel=panel, candidates_table=candidates, message=message
    )


build_question_generation_view = create_question_generation_view

__all__ = [
    "AI_GENERATION_READY",
    "AI_UNAVAILABLE_MESSAGE",
    "CANDIDATE_HEADERS",
    "QuestionGenerationView",
    "build_question_generation_view",
    "create_question_generation_view",
    "refresh_generation_context",
    "unavailable_generation",
]
