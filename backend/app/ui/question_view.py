"""教师题库管理和题目审核 Gradio 视图。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, cast

import gradio as gr
from sqlalchemy.exc import SQLAlchemyError

from backend.app.core.database import get_session_factory
from backend.app.domain.enums import QuestionStatus, QuestionType, UserRole
from backend.app.domain.permissions import PermissionDeniedError, normalize_role
from backend.app.services.question_service import (
    QuestionService,
    QuestionServiceError,
    QuestionSummary,
)
from backend.app.ui.layout_view import (
    bind_confirmation,
    empty_state,
    feedback,
    status_badge,
    status_choices,
    status_label,
    table_options,
)

QUESTION_TYPE_CHOICES = [question_type.value for question_type in QuestionType]
MANAGED_QUESTION_STATUSES = (
    QuestionStatus.DRAFT,
    QuestionStatus.PENDING_REVIEW,
    QuestionStatus.APPROVED,
    QuestionStatus.NEEDS_REVISION,
)
QUESTION_STATUS_CHOICES = [
    question_status.value for question_status in MANAGED_QUESTION_STATUSES
]
QUESTION_TABLE_HEADERS = (
    "题目 ID",
    "课程 ID",
    "题型",
    "题目内容",
    "分值",
    "审核状态",
    "知识点",
    "创建时间",
)
QUESTION_TABLE_DATATYPES = cast(
    tuple[Literal["str", "markdown"], ...],
    ("str", "str", "str", "str", "str", "markdown", "str", "str"),
)
_GENERIC_ERROR = "题目操作失败，请稍后重试。"


@dataclass(frozen=True)
class QuestionView:
    """题目视图中由主应用控制的组件集合。"""

    panel: gr.Column
    questions_table: gr.Dataframe
    message: gr.Markdown


def _empty_state() -> dict[str, Any]:
    """返回可供独立视图使用的空登录状态。"""

    return {"access_token": "", "roles": [], "user_id": "", "username": ""}


def _ensure_teacher(state: Mapping[str, Any]) -> None:
    """检查 Gradio 会话是否已登录且拥有教师角色。"""

    if not state.get("access_token"):
        raise PermissionDeniedError("请先登录。")
    try:
        roles = {normalize_role(role) for role in state.get("roles", [])}
    except (TypeError, ValueError) as exc:
        raise PermissionDeniedError("当前账号无权访问题库功能。") from exc
    if UserRole.TEACHER not in roles:
        raise PermissionDeniedError("当前账号无权访问题库功能。")


def _format_error(error: BaseException) -> str:
    """将内部异常转换成安全且易理解的中文提示。"""

    if isinstance(error, PermissionDeniedError):
        message = str(error) or "当前账号无权执行此操作。"
    elif isinstance(error, QuestionServiceError):
        message = str(error) or _GENERIC_ERROR
    elif isinstance(error, SQLAlchemyError):
        message = "系统暂时无法连接数据库，请稍后重试。"
    elif isinstance(error, (TypeError, ValueError, json.JSONDecodeError)):
        message = f"输入有误：{error or '请检查输入内容。'}"
    else:
        message = _GENERIC_ERROR
    return feedback(message, "error")


def _parse_options(value: Any) -> dict[str, Any] | list[Any] | None:
    """解析题目选项文本并限制为 JSON 对象或数组。"""

    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        raise TypeError("题目选项必须是 JSON 对象或数组。")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("题目选项不是有效的 JSON 数据。") from exc
    if not isinstance(parsed, (dict, list)):
        raise TypeError("题目选项必须是 JSON 对象或数组。")
    return parsed


def _parse_knowledge_points(value: Any) -> list[str]:
    """把逗号或换行分隔的知识点文本转换为去重列表。"""

    if value is None:
        return []
    if isinstance(value, str):
        values: Sequence[Any] = re.split(r"[,，\n]", value)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = value
    else:
        raise TypeError("知识点必须使用文本或字符串列表。")
    result: list[str] = []
    for item in values:
        if not isinstance(item, str) or not item.strip():
            continue
        normalized = item.strip()
        if normalized not in result:
            result.append(normalized)
    return result


def _question_rows(questions: Sequence[QuestionSummary]) -> list[list[str]]:
    """把题目摘要转换为 Gradio 表格行。"""

    return [
        [
            question.id,
            question.course_id,
            status_label(question.type, entity="question_type"),
            question.content,
            str(question.score),
            status_badge(question.status, entity="question"),
            "、".join(question.knowledge_points),
            question.created_at.isoformat(),
        ]
        for question in questions
    ]


def _course_filter(value: Any) -> str | None:
    """清理课程筛选标识，空值表示不限定课程。"""

    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("课程标识输入无效。")
    normalized = value.strip()
    return normalized or None


def _status_filter(value: Any) -> QuestionStatus | None:
    """清理审核状态筛选值。"""

    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, QuestionStatus):
        return value
    if not isinstance(value, str):
        raise TypeError("审核状态输入无效。")
    candidate = value.strip()
    for question_status in QuestionStatus:
        if candidate.lower() in {
            question_status.name.lower(),
            question_status.value.lower(),
        }:
            return question_status
    raise ValueError("审核状态输入无效。")


def refresh_questions(
    course_id: str | None,
    question_status: str | None,
    state: Mapping[str, Any],
) -> tuple[list[list[str]], str]:
    """按课程和审核状态刷新教师题目列表。"""

    try:
        _ensure_teacher(state)
        with get_session_factory()() as session:
            questions = QuestionService(session).list_questions(
                course_id=_course_filter(course_id),
                status=_status_filter(question_status),
                teacher_id=state.get("user_id"),
            )
        return _question_rows(questions), (
            feedback(f"已加载 {len(questions)} 道题目。", "success")
            if questions
            else empty_state("暂无题目。")
        )
    except (
        PermissionDeniedError,
        QuestionServiceError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return [], _format_error(error)


refresh_question_list = refresh_questions


def _list_course_questions(
    service: QuestionService,
    course_id: str | None,
    teacher_id: str,
) -> list[list[str]]:
    """读取操作完成后的当前课程题目表格。"""

    questions = service.list_questions(
        course_id=_course_filter(course_id),
        teacher_id=teacher_id,
    )
    return _question_rows(questions)


def create_question(
    course_id: str,
    question_type: str,
    content: str,
    options: str,
    reference_answer: str,
    scoring_rubric: str,
    difficulty: str,
    knowledge_points: str,
    score: Decimal | float,
    state: Mapping[str, Any],
) -> tuple[list[list[str]], str]:
    """创建人工题目并刷新当前课程题目列表。"""

    try:
        _ensure_teacher(state)
        teacher_id = str(state.get("user_id") or "")
        if not teacher_id:
            raise PermissionDeniedError("登录状态缺少用户标识。")
        with get_session_factory()() as session:
            service = QuestionService(session)
            created = service.create_question(
                course_id=course_id,
                question_type=question_type,
                content=content,
                options=_parse_options(options),
                reference_answer=reference_answer or None,
                scoring_rubric=scoring_rubric or None,
                difficulty=difficulty or None,
                knowledge_points=_parse_knowledge_points(knowledge_points),
                score=score,
                created_by=teacher_id,
            )
            rows = _list_course_questions(service, course_id, teacher_id)
        return rows, feedback(
            f"题目“{created.id}”创建成功，当前状态为{status_label(created.status, entity='question')}。",
            "success",
        )
    except (
        PermissionDeniedError,
        QuestionServiceError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return [], _format_error(error)


def update_question(
    question_id: str,
    question_type: str,
    content: str,
    options: str,
    reference_answer: str,
    scoring_rubric: str,
    difficulty: str,
    knowledge_points: str,
    score: Decimal | float,
    state: Mapping[str, Any],
) -> tuple[list[list[str]], str]:
    """更新题目元数据并刷新题目列表。"""

    try:
        _ensure_teacher(state)
        teacher_id = str(state.get("user_id") or "")
        if not teacher_id:
            raise PermissionDeniedError("登录状态缺少用户标识。")
        with get_session_factory()() as session:
            service = QuestionService(session)
            updated = service.update_question(
                question_id,
                question_type=question_type,
                content=content,
                options=_parse_options(options),
                reference_answer=reference_answer or None,
                scoring_rubric=scoring_rubric or None,
                difficulty=difficulty or None,
                knowledge_points=_parse_knowledge_points(knowledge_points),
                score=score,
                teacher_id=teacher_id,
            )
            rows = _list_course_questions(service, updated.course_id, teacher_id)
        return rows, feedback(f"题目“{updated.id}”更新成功。", "success")
    except (
        PermissionDeniedError,
        QuestionServiceError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return [], _format_error(error)


def set_question_status(
    question_id: str,
    question_status: str,
    state: Mapping[str, Any],
) -> tuple[list[list[str]], str]:
    """更新题目审核状态并刷新题目列表。"""

    try:
        _ensure_teacher(state)
        teacher_id = str(state.get("user_id") or "")
        if not teacher_id:
            raise PermissionDeniedError("登录状态缺少用户标识。")
        status_value = _status_filter(question_status)
        if status_value is None:
            raise ValueError("请选择目标审核状态。")
        with get_session_factory()() as session:
            service = QuestionService(session)
            updated = service.update_question_status(
                question_id,
                status_value,
                teacher_id=teacher_id,
            )
            rows = _list_course_questions(service, updated.course_id, teacher_id)
        return rows, feedback(
            f"题目“{updated.id}”已更新为“{status_label(updated.status, entity='question')}”。",
            "success",
        )
    except (
        PermissionDeniedError,
        QuestionServiceError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return [], _format_error(error)


def delete_question(
    question_id: str,
    state: Mapping[str, Any],
) -> tuple[list[list[str]], str]:
    """删除题目并刷新题目列表。"""

    try:
        _ensure_teacher(state)
        teacher_id = str(state.get("user_id") or "")
        if not teacher_id:
            raise PermissionDeniedError("登录状态缺少用户标识。")
        with get_session_factory()() as session:
            service = QuestionService(session)
            question = service.get_question(question_id, teacher_id=teacher_id)
            service.delete_question(question_id, teacher_id=teacher_id)
            rows = _list_course_questions(service, question.course_id, teacher_id)
        return rows, feedback("题目删除成功。", "success")
    except (
        PermissionDeniedError,
        QuestionServiceError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return [], _format_error(error)


def create_question_view(session_state: Any | None = None) -> QuestionView:
    """创建教师题库管理和审核面板。"""

    state = session_state or gr.State(_empty_state())
    with gr.Column(visible=False) as panel:
        gr.Markdown("## 题库与审核")
        with gr.Row():
            filter_course_id = gr.Textbox(label="课程 ID")
            filter_status = gr.Dropdown(
                choices=status_choices(
                    QUESTION_STATUS_CHOICES, entity="question", include_all=True
                ),
                value="",
                label="审核状态",
            )
            refresh_button = gr.Button("刷新题目", variant="secondary")
        questions_table = gr.Dataframe(
            headers=list(QUESTION_TABLE_HEADERS),
            datatype=QUESTION_TABLE_DATATYPES,
            value=[],
            interactive=False,
            label="题目列表",
            **table_options(QUESTION_TABLE_HEADERS),
        )

        gr.Markdown("### 题目编辑")
        with gr.Row():
            question_id = gr.Textbox(label="题目 ID")
            course_id = gr.Textbox(label="课程 ID")
            question_type = gr.Dropdown(
                choices=status_choices(QUESTION_TYPE_CHOICES, entity="question_type"),
                value=QuestionType.SHORT_ANSWER.value,
                label="题型",
            )
            score = gr.Number(label="分值", value=10, minimum=0.01)
        content = gr.Textbox(label="题目内容", lines=3)
        with gr.Row():
            options = gr.Textbox(label="选项 JSON", lines=3)
            reference_answer = gr.Textbox(label="参考答案", lines=3)
        with gr.Row():
            scoring_rubric = gr.Textbox(label="评分标准", lines=3)
            difficulty = gr.Textbox(label="难度")
            knowledge_points = gr.Textbox(label="知识点")
        with gr.Row():
            create_button = gr.Button("创建题目", variant="primary")
            update_button = gr.Button("保存题目")
            status_value = gr.Dropdown(
                choices=status_choices(QUESTION_STATUS_CHOICES, entity="question"),
                value=QuestionStatus.PENDING_REVIEW.value,
                label="目标状态",
            )
            status_button = gr.Button("更新状态")
            delete_button = gr.Button("删除题目", variant="stop")
        message = gr.Markdown(empty_state("尚未加载题目。"))

        refresh_button.click(
            fn=refresh_questions,
            inputs=[filter_course_id, filter_status, state],
            outputs=[questions_table, message],
            show_progress="hidden",
        )
        create_button.click(
            fn=create_question,
            inputs=[
                course_id,
                question_type,
                content,
                options,
                reference_answer,
                scoring_rubric,
                difficulty,
                knowledge_points,
                score,
                state,
            ],
            outputs=[questions_table, message],
            show_progress="hidden",
        )
        update_button.click(
            fn=update_question,
            inputs=[
                question_id,
                question_type,
                content,
                options,
                reference_answer,
                scoring_rubric,
                difficulty,
                knowledge_points,
                score,
                state,
            ],
            outputs=[questions_table, message],
            show_progress="hidden",
        )
        status_button.click(
            fn=set_question_status,
            inputs=[question_id, status_value, state],
            outputs=[questions_table, message],
            show_progress="hidden",
        )
        bind_confirmation(
            delete_button,
            action="删除题目",
            target=question_id,
            callback=delete_question,
            inputs=[question_id, state],
            outputs=[questions_table, message],
        )

    return QuestionView(
        panel=panel,
        questions_table=questions_table,
        message=message,
    )


build_question_view = create_question_view


__all__ = [
    "QUESTION_STATUS_CHOICES",
    "QUESTION_TABLE_HEADERS",
    "QUESTION_TYPE_CHOICES",
    "QuestionView",
    "build_question_view",
    "create_question",
    "create_question_view",
    "delete_question",
    "refresh_question_list",
    "refresh_questions",
    "set_question_status",
    "update_question",
]
