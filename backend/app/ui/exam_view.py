"""教师考试组卷和发布 Gradio 视图。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast
from uuid import UUID

import gradio as gr

from backend.app.ui.layout_view import (
    bind_confirmation, empty_state, feedback, status_badge, status_choices,
    status_label, table_options,
)
from sqlalchemy.exc import SQLAlchemyError

from backend.app.core.database import get_session_factory
from backend.app.domain.enums import ExamStatus, UserRole
from backend.app.domain.permissions import PermissionDeniedError, normalize_role
from backend.app.services.exam_service import (
    ExamPermissionError,
    ExamService,
    ExamServiceError,
    ExamSummary,
)

EXAM_STATUS_CHOICES = [exam_status.value for exam_status in ExamStatus]
EXAM_TABLE_HEADERS = (
    "考试 ID",
    "课程 ID",
    "考试标题",
    "状态",
    "时长（分钟）",
    "题目数量",
    "总分",
    "开始时间",
    "结束时间",
    "题目 ID",
)
EXAM_TABLE_DATATYPES = cast(
    tuple[Literal["str"], ...],
    ("str",) * len(EXAM_TABLE_HEADERS),
)
_GENERIC_ERROR = "考试操作失败，请稍后重试。"


@dataclass(frozen=True)
class ExamView:
    """考试视图中由主应用控制的组件集合。"""

    panel: gr.Column
    exams_table: gr.Dataframe
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
    except (TypeError, ValueError):
        raise PermissionDeniedError("当前账号无权访问考试管理功能。") from None
    if UserRole.TEACHER not in roles:
        raise PermissionDeniedError("当前账号无权访问考试管理功能。")


def _teacher_id(state: Mapping[str, Any]) -> str:
    """读取已登录教师标识。"""

    _ensure_teacher(state)
    value = state.get("user_id")
    if not value:
        raise PermissionDeniedError("登录状态缺少用户标识。")
    return str(value)


def _format_error(error: BaseException) -> str:
    """将内部异常转换成安全且易理解的中文提示。"""

    if isinstance(error, PermissionDeniedError):
        return str(error) or "当前账号无权执行此操作。"
    if isinstance(error, ExamPermissionError):
        return str(error) or "当前账号无权访问该考试。"
    if isinstance(error, ExamServiceError):
        return str(error) or _GENERIC_ERROR
    if isinstance(error, SQLAlchemyError):
        return "系统暂时无法连接数据库，请稍后重试。"
    if isinstance(error, (TypeError, ValueError)):
        return f"输入有误：{error or '请检查输入内容。'}"
    return _GENERIC_ERROR


def _course_filter(value: Any) -> str | None:
    """清理课程筛选标识，空值表示不限定课程。"""

    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("课程标识输入无效。")
    normalized = value.strip()
    return normalized or None


def _status_filter(value: Any) -> ExamStatus | None:
    """清理考试状态筛选值。"""

    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, ExamStatus):
        return value
    if not isinstance(value, str):
        raise TypeError("考试状态输入无效。")
    candidate = value.strip()
    for exam_status in ExamStatus:
        if candidate.lower() in {
            exam_status.name.lower(),
            exam_status.value.lower(),
        }:
            return exam_status
    raise ValueError("考试状态输入无效。")


def _parse_question_ids(value: Any) -> list[str]:
    """解析逗号或换行分隔的题目标识，并拒绝重复值。"""

    if value is None or (isinstance(value, str) and not value.strip()):
        return []
    if isinstance(value, str):
        candidates: Sequence[Any] = re.split(r"[,，;；\s]+", value.strip())
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        candidates = value
    else:
        raise TypeError("题目标识必须使用文本或字符串列表。")

    normalized: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, (str, UUID)) or not str(candidate).strip():
            raise ValueError("题目标识输入无效。")
        try:
            question_id = str(UUID(str(candidate).strip()))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("题目标识输入无效。") from exc
        if question_id in normalized:
            raise ValueError("题目列表不能包含重复题目。")
        normalized.append(question_id)
    return normalized


def _parse_duration(value: Any) -> int | None:
    """把 Gradio 数字输入转换为正整数时长。"""

    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise TypeError("考试时长必须是正整数。")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("考试时长必须是正整数。")
        value = int(value)
    elif isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError as exc:
            raise ValueError("考试时长必须是正整数。") from exc
    if not isinstance(value, int) or value <= 0:
        raise ValueError("考试时长必须是正整数。")
    return value


def _parse_datetime(value: Any, field_name: str) -> datetime | None:
    """解析 Gradio 文本框中的 ISO 8601 时间。"""

    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise TypeError(f"{field_name}输入无效。")
    candidate = value.strip()
    if candidate.endswith(("Z", "z")):
        candidate = candidate[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"{field_name}格式无效，请使用 ISO 8601 格式。") from exc


def _exam_rows(exams: Sequence[ExamSummary]) -> list[list[str]]:
    """把考试摘要转换为 Gradio 表格行。"""

    return [
        [
            exam.id,
            exam.course_id,
            exam.title,
            status_badge(exam.status, entity="exam"),
            str(exam.duration_minutes or ""),
            str(exam.question_count),
            str(exam.total_score),
            exam.starts_at.isoformat() if exam.starts_at else "",
            exam.ends_at.isoformat() if exam.ends_at else "",
            "、".join(exam.question_ids),
        ]
        for exam in exams
    ]


def _list_exam_rows(
    service: ExamService,
    teacher_id: str,
    *,
    course_id: str | None = None,
    exam_status: ExamStatus | None = None,
) -> list[list[str]]:
    """读取当前教师可见的考试并转换为表格行。"""

    exams = service.list_exams(
        course_id=course_id,
        status=exam_status,
        teacher_id=teacher_id,
    )
    return _exam_rows(exams)


def refresh_exams(
    course_id: str | None,
    exam_status: str | None,
    state: Mapping[str, Any],
) -> tuple[list[list[str]], str]:
    """按课程和状态刷新教师考试列表。"""

    try:
        teacher_id = _teacher_id(state)
        with get_session_factory()() as session:
            rows = _list_exam_rows(
                ExamService(session),
                teacher_id,
                course_id=_course_filter(course_id),
                exam_status=_status_filter(exam_status),
            )
        return rows, feedback(f"已加载 {len(rows)} 场考试。", "success") if rows else empty_state("暂无考试。")
    except (
        PermissionDeniedError,
        ExamServiceError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return [], _format_error(error)


refresh_exam_list = refresh_exams


def create_exam(
    course_id: str,
    title: str,
    description: str,
    duration_minutes: Any,
    starts_at: str,
    ends_at: str,
    question_ids: str,
    state: Mapping[str, Any],
) -> tuple[list[list[str]], str]:
    """创建考试草稿并刷新当前课程的考试列表。"""

    try:
        teacher_id = _teacher_id(state)
        normalized_course_id = _course_filter(course_id)
        if normalized_course_id is None:
            raise ValueError("课程 ID 不能为空。")
        with get_session_factory()() as session:
            service = ExamService(session)
            created = service.create_exam(
                course_id=normalized_course_id,
                title=title,
                description=description or None,
                duration_minutes=_parse_duration(duration_minutes),
                starts_at=_parse_datetime(starts_at, "开始时间"),
                ends_at=_parse_datetime(ends_at, "结束时间"),
                question_ids=_parse_question_ids(question_ids),
                created_by=teacher_id,
            )
            rows = _list_exam_rows(
                service,
                teacher_id,
                course_id=created.course_id,
            )
        return (
            rows,
            feedback(f"考试“{created.title}”创建成功，当前状态为“{status_label(created.status, entity='exam')}”。", "success"),
        )
    except (
        PermissionDeniedError,
        ExamServiceError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return [], _format_error(error)


def update_exam(
    exam_id: str,
    title: str,
    description: str,
    duration_minutes: Any,
    starts_at: str,
    ends_at: str,
    state: Mapping[str, Any],
) -> tuple[list[list[str]], str]:
    """更新草稿考试元数据并刷新考试列表。"""

    try:
        teacher_id = _teacher_id(state)
        with get_session_factory()() as session:
            service = ExamService(session)
            updated = service.update_exam(
                exam_id,
                title=title or None,
                description=description or None,
                duration_minutes=_parse_duration(duration_minutes),
                starts_at=_parse_datetime(starts_at, "开始时间"),
                ends_at=_parse_datetime(ends_at, "结束时间"),
                teacher_id=teacher_id,
            )
            rows = _list_exam_rows(
                service,
                teacher_id,
                course_id=updated.course_id,
            )
        return rows, feedback(f"考试“{updated.title}”更新成功。", "success")
    except (
        PermissionDeniedError,
        ExamServiceError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return [], _format_error(error)


def add_exam_questions(
    exam_id: str,
    question_ids: str,
    state: Mapping[str, Any],
) -> tuple[list[list[str]], str]:
    """向草稿考试追加已审核题目并刷新列表。"""

    try:
        teacher_id = _teacher_id(state)
        with get_session_factory()() as session:
            service = ExamService(session)
            updated = service.add_questions(
                exam_id,
                _parse_question_ids(question_ids),
                teacher_id=teacher_id,
            )
            rows = _list_exam_rows(
                service,
                teacher_id,
                course_id=updated.course_id,
            )
        return rows, feedback(f"已为考试“{updated.title}”关联 {updated.question_count} 道题目。", "success")
    except (
        PermissionDeniedError,
        ExamServiceError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return [], _format_error(error)


add_questions_to_exam = add_exam_questions


def remove_exam_questions(
    exam_id: str,
    question_ids: str,
    state: Mapping[str, Any],
) -> tuple[list[list[str]], str]:
    """从草稿考试移除题目并刷新列表。"""

    try:
        teacher_id = _teacher_id(state)
        with get_session_factory()() as session:
            service = ExamService(session)
            updated = service.remove_questions(
                exam_id,
                _parse_question_ids(question_ids),
                teacher_id=teacher_id,
            )
            rows = _list_exam_rows(
                service,
                teacher_id,
                course_id=updated.course_id,
            )
        return (
            rows,
            f"已从考试“{updated.title}”移除题目，当前共 {updated.question_count} 道。",
        )
    except (
        PermissionDeniedError,
        ExamServiceError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return [], _format_error(error)


remove_questions_from_exam = remove_exam_questions


def publish_exam(
    exam_id: str,
    state: Mapping[str, Any],
) -> tuple[list[list[str]], str]:
    """发布考试并刷新其所属课程的考试列表。"""

    try:
        teacher_id = _teacher_id(state)
        with get_session_factory()() as session:
            service = ExamService(session)
            published = service.publish_exam(exam_id, teacher_id=teacher_id)
            rows = _list_exam_rows(
                service,
                teacher_id,
                course_id=published.course_id,
            )
        return rows, feedback(f"考试“{published.title}”发布成功，学生现在可以参加。", "success")
    except (
        PermissionDeniedError,
        ExamServiceError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return [], _format_error(error)


def set_exam_status(
    exam_id: str,
    exam_status: str,
    state: Mapping[str, Any],
) -> tuple[list[list[str]], str]:
    """更新考试状态并刷新考试列表。"""

    try:
        teacher_id = _teacher_id(state)
        normalized_status = _status_filter(exam_status)
        if normalized_status is None:
            raise ValueError("请选择目标考试状态。")
        with get_session_factory()() as session:
            service = ExamService(session)
            updated = service.update_exam_status(
                exam_id,
                normalized_status,
                teacher_id=teacher_id,
            )
            rows = _list_exam_rows(
                service,
                teacher_id,
                course_id=updated.course_id,
            )
        return rows, feedback(f"考试“{updated.title}”已更新为“{status_label(updated.status, entity='exam')}”。", "success")
    except (
        PermissionDeniedError,
        ExamServiceError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return [], _format_error(error)


def create_exam_view(session_state: Any | None = None) -> ExamView:
    """创建教师考试组卷和发布面板。"""

    state = session_state or gr.State(_empty_state())
    with gr.Column(visible=False) as panel:
        gr.Markdown("## 考试与组卷")
        with gr.Row():
            filter_course_id = gr.Textbox(label="课程 ID")
            filter_status = gr.Dropdown(
                choices=status_choices(EXAM_STATUS_CHOICES, entity="exam", include_all=True),
                value="",
                label="考试状态",
            )
            refresh_button = gr.Button("刷新考试", variant="secondary")
        exams_table = gr.Dataframe(
            headers=list(EXAM_TABLE_HEADERS),
            datatype=EXAM_TABLE_DATATYPES,
            value=[],
            interactive=False,
            label="考试列表",
            **table_options(EXAM_TABLE_HEADERS),
        )

        gr.Markdown("### 创建或修改考试")
        with gr.Row():
            exam_id = gr.Textbox(label="考试 ID（修改、组卷或发布时填写）")
            course_id = gr.Textbox(label="课程 ID")
            title = gr.Textbox(label="考试标题")
            duration_minutes = gr.Number(
                label="考试时长（分钟）",
                value=60,
                minimum=1,
                precision=0,
            )
        description = gr.Textbox(label="考试描述", lines=2)
        with gr.Row():
            starts_at = gr.Textbox(label="开始时间（ISO 8601，可选）")
            ends_at = gr.Textbox(label="结束时间（ISO 8601，可选）")
        initial_question_ids = gr.Textbox(
            label="初始题目 ID（逗号或换行分隔，可选）",
            lines=3,
        )
        with gr.Row():
            create_button = gr.Button("创建考试", variant="primary")
            update_button = gr.Button("保存考试")

        gr.Markdown("### 草稿组卷与发布")
        add_question_ids = gr.Textbox(
            label="要加入的题目 ID（逗号或换行分隔）",
            lines=2,
        )
        remove_question_ids = gr.Textbox(
            label="要移除的题目 ID（逗号或换行分隔）",
            lines=2,
        )
        with gr.Row():
            add_button = gr.Button("加入题目")
            remove_button = gr.Button("移除题目")
            target_status = gr.Dropdown(
                choices=status_choices(EXAM_STATUS_CHOICES, entity="exam"),
                value=ExamStatus.PUBLISHED.value,
                label="目标状态",
            )
            status_button = gr.Button("更新状态")
            publish_button = gr.Button("发布考试", variant="primary")
        message = gr.Markdown(empty_state("尚未加载考试。"))

        refresh_button.click(
            fn=refresh_exams,
            inputs=[filter_course_id, filter_status, state],
            outputs=[exams_table, message],
            show_progress="hidden",
        )
        create_button.click(
            fn=create_exam,
            inputs=[
                course_id,
                title,
                description,
                duration_minutes,
                starts_at,
                ends_at,
                initial_question_ids,
                state,
            ],
            outputs=[exams_table, message],
            show_progress="hidden",
        )
        update_button.click(
            fn=update_exam,
            inputs=[
                exam_id,
                title,
                description,
                duration_minutes,
                starts_at,
                ends_at,
                state,
            ],
            outputs=[exams_table, message],
            show_progress="hidden",
        )
        add_button.click(
            fn=add_exam_questions,
            inputs=[exam_id, add_question_ids, state],
            outputs=[exams_table, message],
            show_progress="hidden",
        )
        bind_confirmation(
            remove_button, action="移除题目", target=exam_id, callback=remove_exam_questions,
            inputs=[exam_id, remove_question_ids, state],
            outputs=[exams_table, message],
        )
        bind_confirmation(
            status_button, action="更新考试状态", target=exam_id, callback=set_exam_status,
            inputs=[exam_id, target_status, state],
            outputs=[exams_table, message],
        )
        bind_confirmation(
            publish_button, action="发布考试", target=exam_id, callback=publish_exam,
            inputs=[exam_id, state],
            outputs=[exams_table, message],
        )

    return ExamView(
        panel=panel,
        exams_table=exams_table,
        message=message,
    )


build_exam_view = create_exam_view


__all__ = [
    "EXAM_STATUS_CHOICES",
    "EXAM_TABLE_HEADERS",
    "ExamView",
    "add_exam_questions",
    "add_questions_to_exam",
    "build_exam_view",
    "create_exam",
    "create_exam_view",
    "publish_exam",
    "refresh_exam_list",
    "refresh_exams",
    "remove_exam_questions",
    "remove_questions_from_exam",
    "set_exam_status",
    "update_exam",
]
