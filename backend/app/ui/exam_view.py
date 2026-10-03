"""教师考试组卷和发布 Gradio 视图。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any, Literal, cast
from uuid import UUID

import gradio as gr
from sqlalchemy.exc import SQLAlchemyError

from backend.app.core.database import get_session_factory
from backend.app.domain.enums import (
    OBJECTIVE_QUESTION_TYPES,
    ExamStatus,
    QuestionStatus,
    UserRole,
)
from backend.app.domain.permissions import PermissionDeniedError, normalize_role
from backend.app.schemas.exam_scoring import (
    ScoringBasisView,
    ScoringConfirmRequest,
    ScoringPrepareRequest,
    money_text,
)
from backend.app.services.course_service import CourseService, CourseServiceError
from backend.app.services.exam_assembly_service import AssemblyError
from backend.app.services.exam_scoring_service import ExamScoringService
from backend.app.services.exam_service import (
    ExamPermissionError,
    ExamService,
    ExamServiceError,
    ExamSummary,
)
from backend.app.services.question_service import QuestionService, QuestionSummary
from backend.app.ui.layout_view import (
    bind_confirmation,
    empty_state,
    feedback,
    status_badge,
    status_choices,
    status_label,
    table_options,
)
from backend.app.ui.question_view import (
    create_question_selection,
    load_question_choices,
    question_selection_data,
    selected_question_id,
    teacher_course_choices,
)

EXAM_STATUS_CHOICES = [exam_status.value for exam_status in ExamStatus]
EXAM_TABLE_HEADERS = (
    "考试名称",
    "课程",
    "开放时间",
    "时长（分钟）",
    "题目数量",
    "总分",
    "状态",
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
        message = str(error) or "当前账号无权执行此操作。"
    elif isinstance(error, ExamPermissionError):
        message = str(error) or "当前账号无权访问该考试。"
    elif isinstance(error, (ExamServiceError, AssemblyError)):
        message = str(error) or _GENERIC_ERROR
    elif isinstance(error, SQLAlchemyError):
        message = "系统暂时无法连接数据库，请稍后重试。"
    elif isinstance(error, (TypeError, ValueError)):
        message = f"输入有误：{error or '请检查输入内容。'}"
    else:
        message = _GENERIC_ERROR
    return feedback(message, "error")


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
            exam.title,
            exam.course_id,
            (exam.starts_at.isoformat() if exam.starts_at else "未设置"),
            str(exam.duration_minutes or "未设置"),
            str(exam.question_count),
            str(exam.total_score) if exam.total_score is not None else "未知（待核对）",
            status_badge(exam.status, entity="exam"),
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
    courses = CourseService(service.session).list_courses(teacher_id=teacher_id)
    rows, _ = exam_table_data(exams, [(course.name, course.id) for course in courses])
    return rows


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
        return rows, (
            feedback(f"已加载 {len(rows)} 场考试。", "success")
            if rows
            else empty_state("暂无考试。")
        )
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
            feedback(
                f"考试“{created.title}”创建成功，当前状态为“{status_label(created.status, entity='exam')}”。",
                "success",
            ),
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
        return rows, feedback(
            f"已为考试“{updated.title}”关联 {updated.question_count} 道题目。",
            "success",
        )
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
        return rows, feedback(
            f"考试“{published.title}”发布成功，学生现在可以参加。", "success"
        )
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
        return rows, feedback(
            f"考试“{updated.title}”已更新为“{status_label(updated.status, entity='exam')}”。",
            "success",
        )
    except (
        PermissionDeniedError,
        ExamServiceError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return [], _format_error(error)


UI_TIMEZONE = timezone(timedelta(hours=8))
UI_TIMEZONE_LABEL = "北京时间 UTC+08:00"


def exam_datetime(value: datetime | str | None) -> datetime | None:
    """把日期时间控件的值统一为北京时间，保留明确的时区语义。"""

    parsed = _parse_datetime(value, "日期时间")
    if parsed is None:
        return None
    return (
        parsed.replace(tzinfo=UI_TIMEZONE)
        if parsed.tzinfo is None
        else parsed.astimezone(UI_TIMEZONE)
    )


def exam_opening_label(exam: ExamSummary) -> str:
    start = exam_datetime(exam.starts_at)
    end = exam_datetime(exam.ends_at)
    return (
        (f"{start:%Y-%m-%d %H:%M}" if start else "不限开始时间")
        + " 至 "
        + (f"{end:%Y-%m-%d %H:%M}" if end else "不限结束时间")
    )


def exam_table_data(
    exams: Sequence[ExamSummary],
    courses: Sequence[tuple[str, str]],
) -> tuple[list[list[str]], list[str]]:
    """展示课程名称，内部考试标识单独保存。"""

    names = {identifier: name for name, identifier in courses}
    return [
        [
            exam.title,
            names.get(exam.course_id, "课程暂不可用"),
            exam_opening_label(exam),
            str(exam.duration_minutes) if exam.duration_minutes else "不限时",
            str(exam.question_count),
            str(exam.total_score) if exam.total_score is not None else "未知（待核对）",
            status_badge(exam.status, entity="exam"),
        ]
        for exam in exams
    ], [exam.id for exam in exams]


def exam_publication_issues(
    exam: ExamSummary,
    questions: Sequence[QuestionSummary],
) -> list[str]:
    """提供发布前反馈，最终发布仍调用考试服务校验。"""

    issues: list[str] = []
    if exam.status != ExamStatus.DRAFT:
        issues.append("当前考试不是草稿。")
    if not exam.title.strip():
        issues.append("考试名称不能为空。")
    if not questions:
        issues.append("至少需要一道已审核题目。")
    if set(exam.question_ids) != {question.id for question in questions}:
        issues.append("已选题目发生变化，请重新加载。")
    if any(question.course_id != exam.course_id for question in questions):
        issues.append("考试题目必须属于同一课程。")
    if any(question.status != QuestionStatus.APPROVED for question in questions):
        issues.append("存在尚未审核通过的题目。")
    start, end = exam_datetime(exam.starts_at), exam_datetime(exam.ends_at)
    if start and end and end <= start:
        issues.append("开放结束时间必须晚于开始时间。")
    if end and end <= datetime.now(UI_TIMEZONE):
        issues.append("开放结束时间已过。")
    return issues


def create_exam_view(session_state: Any | None = None) -> ExamView:
    """创建考试列表和基本信息、选择题目、发布检查三个步骤。"""

    state = session_state or gr.State(_empty_state())
    errors = (
        PermissionDeniedError,
        ExamServiceError,
        AssemblyError,
        CourseServiceError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    )
    with gr.Column(visible=False) as panel:
        gr.Markdown("## 考试与组卷")
        exam_ids = gr.State([])
        selected_exam = gr.State(None)
        publication_snapshot = gr.State(None)
        scoring_snapshot = gr.State(None)
        candidate_id = gr.State(None)
        remove_id = gr.Textbox(visible=False, container=False)
        with gr.Row():
            filter_course = gr.Dropdown(label="课程", choices=[])
            filter_status = gr.Dropdown(
                label="考试状态",
                choices=status_choices(
                    EXAM_STATUS_CHOICES, entity="exam", include_all=True
                ),
                value="",
            )
            refresh_button = gr.Button("刷新考试", scale=0)
            new_button = gr.Button("新建考试", variant="primary", scale=0)
        message = gr.Markdown(empty_state("暂无考试。"))
        exams_table = gr.Dataframe(
            headers=list(EXAM_TABLE_HEADERS),
            datatype=["str", "str", "str", "str", "str", "str", "markdown"],
            value=[],
            interactive=False,
            label=f"考试列表（{UI_TIMEZONE_LABEL}）",
            **table_options(EXAM_TABLE_HEADERS),
        )
        with gr.Column(visible=False) as editor:
            exam_status = gr.Markdown()
            with gr.Tabs(selected="basic") as steps:
                with gr.Tab("基本信息", id="basic"):
                    with gr.Row():
                        edit_course = gr.Dropdown(label="所属课程", choices=[])
                        title = gr.Textbox(label="考试名称")
                        duration = gr.Number(
                            label="时长（分钟，可选）", value=60, minimum=1, precision=0
                        )
                    description = gr.Textbox(label="考试说明", lines=2)
                    with gr.Row():
                        starts_at = gr.DateTime(
                            label=f"开放开始（{UI_TIMEZONE_LABEL}，可选）",
                            type="datetime",
                            timezone="Asia/Shanghai",
                        )
                        ends_at = gr.DateTime(
                            label=f"开放结束（{UI_TIMEZONE_LABEL}，可选）",
                            type="datetime",
                            timezone="Asia/Shanghai",
                        )
                    save_button = gr.Button("保存并选择题目", variant="primary")
                with gr.Tab("选择题目", id="questions"):
                    question_message = gr.Markdown(
                        empty_state("请先保存考试基本信息。")
                    )
                    with gr.Row():
                        with gr.Column(scale=60, min_width=360):
                            available = create_question_selection("当前课程已审核题目")
                            selected_candidate = gr.Textbox(
                                label="当前选中题目", interactive=False
                            )
                            add_button = gr.Button("加入考试", interactive=False)
                        with gr.Column(scale=40, min_width=300):
                            chosen = create_question_selection("已选题清单")
                            summary = gr.Markdown(empty_state("尚未选择题目。"))
                            remove_target = gr.Textbox(
                                label="待移除题目", interactive=False
                            )
                            remove_button = gr.Button("移除题目", interactive=False)
                    with gr.Row():
                        reload_questions = gr.Button("刷新题目")
                        check_button = gr.Button("进入发布检查", variant="primary")
                with gr.Tab("发布检查", id="publish"):
                    with gr.Accordion("本场评分标准", open=True):
                        scoring_question = gr.Dropdown(
                            label="核对评分标准的题目",
                            choices=[],
                            value=None,
                            elem_id="edu-exam-scoring-question",
                        )
                        scoring_reload = gr.Button(
                            "加载本场标准", elem_id="edu-exam-scoring-load"
                        )
                        scoring_source = gr.Textbox(
                            label="题库原始评分标准",
                            lines=3,
                            interactive=False,
                            elem_id="edu-exam-scoring-source",
                        )
                        scoring_context = gr.Markdown(
                            empty_state("请选择本场题目，读取基准与本场分值。"),
                            elem_id="edu-exam-scoring-context",
                        )
                        scoring_mode = gr.Radio(
                            label="评分结构",
                            choices=[
                                "可加总数值要点",
                                "非加总数值要点",
                                "定性文字标准",
                            ],
                            value="定性文字标准",
                            interactive=False,
                            elem_id="edu-exam-scoring-mode",
                        )
                        scoring_points = gr.Dataframe(
                            headers=["要点编号", "评分说明", "基准分值"],
                            datatype=["str", "str", "str"],
                            type="array",
                            value=[],
                            row_count=0,
                            row_limits=(0, None),
                            column_count=3,
                            interactive=False,
                            label="教师明确核对的基准要点",
                            elem_id="edu-exam-scoring-points",
                        )
                        scoring_prepare = gr.Button(
                            "准备本场标准",
                            interactive=False,
                            elem_id="edu-exam-scoring-prepare",
                        )
                        scoring_defaults = gr.Dataframe(
                            headers=[
                                "要点编号",
                                "评分说明",
                                "基准分值",
                                "默认本场分值",
                            ],
                            datatype=["str", "str", "str", "str"],
                            type="array",
                            value=[],
                            interactive=False,
                            label="逐项换算结果",
                            elem_id="edu-exam-scoring-defaults",
                        )
                        scoring_delta = gr.Markdown(elem_id="edu-exam-scoring-delta")
                        scoring_final = gr.Dataframe(
                            headers=["要点编号", "最终本场分值"],
                            datatype=["str", "str"],
                            type="array",
                            value=[],
                            column_count=2,
                            interactive=False,
                            label="教师最终采用的要点分值",
                            elem_id="edu-exam-scoring-final",
                        )
                        scoring_reason = gr.Textbox(
                            label="核对说明与尾差处置理由",
                            lines=2,
                            interactive=False,
                            elem_id="edu-exam-scoring-reason",
                        )
                        scoring_confirm = gr.Button(
                            "确认本场标准",
                            interactive=False,
                            elem_id="edu-exam-scoring-confirm",
                        )
                        scoring_status = gr.Markdown(elem_id="edu-exam-scoring-status")
                    publication_details = gr.Markdown(empty_state("尚未执行发布检查。"))
                    recheck_button = gr.Button("重新检查")
                    confirmed = gr.Checkbox(
                        label="我已核对考试名称、开放时间和已审核题目",
                        value=False,
                        interactive=False,
                    )
                    publish_button = gr.Button(
                        "发布考试", variant="primary", interactive=False
                    )
            with gr.Accordion("考试状态操作", open=False):
                lifecycle_target = gr.Textbox(label="当前考试", interactive=False)
                lifecycle = gr.Dropdown(label="目标状态", choices=[], value=None)
                lifecycle_button = gr.Button("更新考试状态", interactive=False)

        fields = [edit_course, title, duration, description, starts_at, ends_at]
        outputs = [
            exam_ids,
            selected_exam,
            publication_snapshot,
            scoring_snapshot,
            scoring_question,
            scoring_reload,
            scoring_source,
            scoring_context,
            scoring_mode,
            scoring_points,
            scoring_prepare,
            scoring_defaults,
            scoring_delta,
            scoring_final,
            scoring_reason,
            scoring_confirm,
            scoring_status,
            candidate_id,
            remove_id,
            filter_course,
            filter_status,
            message,
            exams_table,
            editor,
            exam_status,
            steps,
            *fields,
            save_button,
            question_message,
            available.table,
            available.ids,
            selected_candidate,
            add_button,
            chosen.table,
            chosen.ids,
            summary,
            remove_target,
            remove_button,
            reload_questions,
            check_button,
            publication_details,
            recheck_button,
            confirmed,
            publish_button,
            lifecycle_target,
            lifecycle,
            lifecycle_button,
        ]

        def invalidate() -> dict[Any, Any]:
            return {
                publication_snapshot: None,
                confirmed: gr.update(value=False, interactive=False),
                publish_button: gr.update(interactive=False),
                publication_details: empty_state(
                    "考试内容已更新，请重新执行发布检查。"
                ),
            }

        def clear_scoring() -> dict[Any, Any]:
            return {
                scoring_snapshot: None,
                scoring_source: "",
                scoring_context: empty_state("请选择本场题目，读取基准与本场分值。"),
                scoring_mode: gr.update(value="定性文字标准", interactive=False),
                scoring_points: gr.update(value=[], interactive=False),
                scoring_prepare: gr.update(interactive=False),
                scoring_defaults: [],
                scoring_delta: "",
                scoring_final: gr.update(value=[], interactive=False),
                scoring_reason: gr.update(value="", interactive=False),
                scoring_confirm: gr.update(interactive=False),
                scoring_status: "",
            }

        def clear() -> dict[Any, Any]:
            result = {**invalidate(), **clear_scoring()}
            result[scoring_question] = gr.update(choices=[], value=None)
            result.update(
                {
                    selected_exam: None,
                    candidate_id: None,
                    remove_id: "",
                    editor: gr.update(visible=False),
                    available.table: [],
                    available.ids: [],
                    chosen.table: [],
                    chosen.ids: [],
                    selected_candidate: "",
                    remove_target: "",
                    add_button: gr.update(interactive=False),
                    remove_button: gr.update(interactive=False),
                }
            )
            return result

        def list_updates(
            course: str | None, status: str | None, current_state: Mapping[str, Any]
        ) -> dict[Any, Any]:
            teacher_id = _teacher_id(current_state)
            courses = teacher_course_choices(current_state)
            with get_session_factory()() as session:
                exams = ExamService(session).list_exams(
                    course_id=_course_filter(course),
                    status=_status_filter(status),
                    teacher_id=teacher_id,
                )
            rows, ids = exam_table_data(exams, courses)
            return {
                filter_course: gr.update(choices=courses, value=course),
                filter_status: gr.update(value=status or ""),
                exams_table: rows,
                exam_ids: ids,
                message: (
                    feedback(f"已加载 {len(exams)} 场考试。", "success")
                    if exams
                    else empty_state("暂无符合条件的考试。")
                ),
            }

        def refresh(
            course: str | None, status: str | None, current_state: Mapping[str, Any]
        ) -> dict[Any, Any]:
            try:
                return {**clear(), **list_updates(course, status, current_state)}
            except errors as error:
                return {
                    **clear(),
                    exams_table: [],
                    exam_ids: [],
                    message: _format_error(error),
                }

        def read_exam(
            identifier: str, current_state: Mapping[str, Any]
        ) -> tuple[ExamSummary, list[QuestionSummary]]:
            teacher_id = _teacher_id(current_state)
            with get_session_factory()() as session:
                exam = ExamService(session).get_exam(identifier, teacher_id=teacher_id)
                service = QuestionService(session)
                questions = [
                    service.get_question(qid, teacher_id=teacher_id)
                    for qid in exam.question_ids
                ]
            return exam, questions

        def form(
            exam: ExamSummary | None,
            current_state: Mapping[str, Any],
            course: str | None = None,
            step: str = "basic",
        ) -> dict[Any, Any]:
            result = clear()
            editable = exam is None or exam.status == ExamStatus.DRAFT
            courses = teacher_course_choices(current_state)
            selected_course = exam.course_id if exam else course
            result.update(
                {
                    editor: gr.update(visible=True),
                    steps: gr.update(selected=step),
                    selected_exam: exam.id if exam else None,
                    exam_status: (
                        status_badge(exam.status, entity="exam")
                        if exam
                        else status_badge(ExamStatus.DRAFT, entity="exam")
                    ),
                    edit_course: gr.update(
                        choices=courses, value=selected_course, interactive=exam is None
                    ),
                    title: gr.update(
                        value=exam.title if exam else "", interactive=editable
                    ),
                    description: gr.update(
                        value=(exam.description or "") if exam else "",
                        interactive=editable,
                    ),
                    duration: gr.update(
                        value=exam.duration_minutes if exam else 60,
                        interactive=editable,
                    ),
                    starts_at: gr.update(
                        value=exam_datetime(exam.starts_at) if exam else None,
                        interactive=editable,
                    ),
                    ends_at: gr.update(
                        value=exam_datetime(exam.ends_at) if exam else None,
                        interactive=editable,
                    ),
                    save_button: gr.update(interactive=editable),
                    reload_questions: gr.update(interactive=exam is not None),
                    check_button: gr.update(interactive=bool(exam and editable)),
                    recheck_button: gr.update(interactive=bool(exam and editable)),
                    lifecycle_target: exam.title if exam else "",
                    lifecycle: gr.update(
                        choices=status_choices(
                            (
                                [ExamStatus.CLOSED, ExamStatus.ARCHIVED]
                                if exam and exam.status == ExamStatus.PUBLISHED
                                else (
                                    [ExamStatus.ARCHIVED]
                                    if exam and exam.status == ExamStatus.CLOSED
                                    else []
                                )
                            ),
                            entity="exam",
                        ),
                        value=None,
                    ),
                    lifecycle_button: gr.update(
                        interactive=bool(
                            exam
                            and exam.status in {ExamStatus.PUBLISHED, ExamStatus.CLOSED}
                        )
                    ),
                }
            )
            if exam:
                latest, questions = read_exam(exam.id, current_state)
                candidates = load_question_choices(
                    exam.course_id,
                    None,
                    None,
                    QuestionStatus.APPROVED.value,
                    current_state,
                )
                candidates = [
                    question
                    for question in candidates
                    if question.id not in latest.question_ids
                ]
                available_rows, available_ids = question_selection_data(candidates)
                chosen_rows, chosen_ids = question_selection_data(questions)
                result.update(
                    {
                        available.table: available_rows,
                        available.ids: available_ids,
                        chosen.table: chosen_rows,
                        chosen.ids: chosen_ids,
                        scoring_question: gr.update(
                            choices=[
                                (
                                    f"第 {index} 题 · {question.content[:55]}",
                                    question.id,
                                )
                                for index, question in enumerate(questions, start=1)
                            ],
                            value=None,
                        ),
                        summary: f"**题数：{latest.question_count}**　**总分：{latest.total_score if latest.total_score is not None else "未知（待核对）"} 分**",
                        question_message: (
                            ""
                            if candidates
                            else empty_state("暂无可加入的已审核题目。")
                        ),
                    }
                )
            else:
                result.update(
                    {
                        summary: empty_state("尚未选择题目。"),
                        question_message: empty_state("请先保存考试基本信息。"),
                    }
                )
            return result

        def new(course: str | None, current_state: Mapping[str, Any]) -> dict[Any, Any]:
            try:
                courses = teacher_course_choices(current_state)
                selected_course = (
                    course if course in {value for _, value in courses} else None
                )
                return {
                    **form(None, current_state, selected_course),
                    message: "" if courses else empty_state("暂无课程，请先创建课程。"),
                }
            except errors as error:
                return {message: _format_error(error)}

        def select_row(
            ids: list[str], current_state: Mapping[str, Any], event: gr.SelectData
        ) -> dict[Any, Any]:
            try:
                identifier = selected_question_id(event, ids)
                exam, _ = read_exam(identifier, current_state)
                return {**form(exam, current_state), message: ""}
            except errors as error:
                return {**clear(), message: _format_error(error)}

        def save(
            identifier: str | None,
            course: str | None,
            name: str,
            minutes: Any,
            text: str,
            start: datetime | None,
            end: datetime | None,
            current_state: Mapping[str, Any],
        ) -> dict[Any, Any]:
            try:
                teacher_id = _teacher_id(current_state)
                if not course:
                    raise ValueError("请选择所属课程。")
                payload: dict[str, Any] = {
                    "title": name,
                    "description": text or None,
                    "duration_minutes": _parse_duration(minutes),
                    "starts_at": exam_datetime(start),
                    "ends_at": exam_datetime(end),
                }
                with get_session_factory()() as session:
                    service = ExamService(session)
                    exam = (
                        service.update_exam(
                            identifier, teacher_id=teacher_id, **payload
                        )
                        if identifier
                        else service.create_exam(
                            course_id=course, created_by=teacher_id, **payload
                        )
                    )
                return {
                    **list_updates(exam.course_id, "", current_state),
                    **form(exam, current_state, step="questions"),
                    message: feedback("考试草稿已保存。", "success"),
                }
            except errors as error:
                return {**invalidate(), message: _format_error(error)}

        def choose_candidate(
            ids: list[str],
            identifier: str | None,
            current_state: Mapping[str, Any],
            event: gr.SelectData,
        ) -> dict[Any, Any]:
            try:
                if not identifier:
                    raise ValueError("请先保存考试。")
                exam, _ = read_exam(identifier, current_state)
                qid = selected_question_id(event, ids)
                with get_session_factory()() as session:
                    question = QuestionService(session).get_question(
                        qid, teacher_id=_teacher_id(current_state)
                    )
                valid = (
                    exam.status == ExamStatus.DRAFT
                    and question.status == QuestionStatus.APPROVED
                    and question.course_id == exam.course_id
                    and qid not in exam.question_ids
                )
                return {
                    candidate_id: qid if valid else None,
                    selected_candidate: question.content,
                    add_button: gr.update(interactive=valid),
                }
            except errors as error:
                return {
                    candidate_id: None,
                    add_button: gr.update(interactive=False),
                    message: _format_error(error),
                }

        def choose_remove(
            ids: list[str],
            identifier: str | None,
            current_state: Mapping[str, Any],
            event: gr.SelectData,
        ) -> dict[Any, Any]:
            try:
                if not identifier:
                    raise ValueError("请先选择考试。")
                exam, questions = read_exam(identifier, current_state)
                qid = selected_question_id(event, ids)
                question = next((q for q in questions if q.id == qid), None)
                valid = exam.status == ExamStatus.DRAFT and question is not None
                return {
                    remove_id: qid if valid else "",
                    remove_target: question.content if question else "",
                    remove_button: gr.update(interactive=valid),
                }
            except errors as error:
                return {
                    remove_id: "",
                    remove_button: gr.update(interactive=False),
                    message: _format_error(error),
                }

        def change_questions(
            identifier: str | None,
            qid: str | None,
            current_state: Mapping[str, Any],
            *,
            remove: bool = False,
        ) -> dict[Any, Any]:
            try:
                if not identifier or not qid:
                    raise ValueError("请先选择考试和题目。")
                with get_session_factory()() as session:
                    service = ExamService(session)
                    action = (
                        service.remove_questions if remove else service.add_questions
                    )
                    exam = action(
                        identifier, [qid], teacher_id=_teacher_id(current_state)
                    )
                return {
                    **list_updates(exam.course_id, "", current_state),
                    **form(exam, current_state, step="questions"),
                    message: feedback("已选题清单已更新。", "success"),
                }
            except errors as error:
                return {**invalidate(), message: _format_error(error)}

        def remove_question(
            label: str,
            qid: str,
            identifier: str | None,
            current_state: Mapping[str, Any],
        ) -> dict[Any, Any]:
            return change_questions(identifier, qid, current_state, remove=True)

        def reload(
            identifier: str | None, current_state: Mapping[str, Any]
        ) -> dict[Any, Any]:
            try:
                if not identifier:
                    raise ValueError("请先保存考试。")
                exam, _ = read_exam(identifier, current_state)
                return form(exam, current_state, step="questions")
            except errors as error:
                return {**invalidate(), message: _format_error(error)}

        def scoring_data(view: ScoringBasisView) -> dict[Any, Any]:
            basis = view.basis
            objective = view.question_type in OBJECTIVE_QUESTION_TYPES
            editable = view.editable and view.effective_score is not None
            mode = (
                "可加总数值要点"
                if basis and basis.additive
                else (
                    "非加总数值要点"
                    if basis and basis.points
                    else "可加总数值要点" if objective else "定性文字标准"
                )
            )
            points = (
                [
                    [point.key, point.label, money_text(point.base_points)]
                    for point in basis.points
                ]
                if basis
                else (
                    [
                        [
                            "correct",
                            "正确作答按本场满分给分",
                            money_text(view.question_score),
                        ]
                    ]
                    if objective
                    else []
                )
            )
            context = (
                f"**题库满分**：{money_text(view.question_score)} 分　"
                f"**本场有效满分**：{money_text(view.effective_score) if view.effective_score is not None else '未知'} 分\n\n"
                f"**已准备的基准满分**：{money_text(view.base_score) if view.base_score is not None else '尚未准备'}"
            )
            if objective:
                context += "\n\n客观题沿用确定性整题给分规则：正确给本场满分，错误或合法空答按既有规则处理。"
            else:
                context += "\n\n请按原始标准明确填写数值要点；定性或重叠要点须核对本场对应语义。"
            if basis is None:
                delta = "尚未准备本场评分标准。"
                status = ""
            else:
                delta = (
                    f"**独立舍入尾差**：{basis.rounding_delta:+.2f} 分。请明确核对最终要点分值。"
                    if basis.rounding_delta is not None
                    else "尾差不适用：定性或非加总标准，请说明本场对应方式。"
                )
                confirmation = basis.confirmation
                status = (
                    feedback(
                        f"已确认：{escape(confirmation.reason)}；教师 {confirmation.teacher_id}；"
                        f"UTC {confirmation.confirmed_at.isoformat()}",
                        "success",
                    )
                    if confirmation
                    else feedback("准备结果已保存，尚无教师确认记录。", "warning")
                )
            return {
                scoring_snapshot: view.model_dump(mode="json"),
                scoring_source: view.source_rubric or "未提供原始评分标准",
                scoring_context: context,
                scoring_mode: gr.update(
                    value=mode, interactive=editable and not objective
                ),
                scoring_points: gr.update(
                    value=points,
                    interactive=editable and not objective and mode != "定性文字标准",
                ),
                scoring_prepare: gr.update(interactive=editable),
                scoring_defaults: (
                    [
                        [
                            point.key,
                            point.label,
                            money_text(point.base_points),
                            money_text(point.default_points),
                        ]
                        for point in basis.points
                    ]
                    if basis
                    else []
                ),
                scoring_delta: delta,
                scoring_final: gr.update(
                    value=(
                        [
                            [point.key, money_text(point.confirmed_points)]
                            for point in basis.points
                        ]
                        if basis
                        else []
                    ),
                    interactive=bool(
                        editable and basis and basis.points and not objective
                    ),
                ),
                scoring_reason: gr.update(
                    value=(
                        basis.confirmation.reason
                        if basis and basis.confirmation
                        else ""
                    ),
                    interactive=editable,
                ),
                scoring_confirm: gr.update(
                    interactive=bool(editable and basis and basis.preparation_id)
                ),
                scoring_status: status,
            }

        def loaded_scoring(
            identifier: str | None, qid: str | None, loaded: Any
        ) -> ScoringBasisView:
            if not identifier or not qid or not isinstance(loaded, dict):
                raise ValueError("请重新加载当前考试题目的评分标准。")
            view = ScoringBasisView.model_validate(loaded)
            if str(view.exam_id) != identifier or str(view.question_id) != qid:
                raise ValueError("考试或题目已切换，请重新加载评分标准。")
            if not view.editable:
                raise ValueError("当前考试评分标准只允许查看。")
            return view

        def point_rows(value: Any, width: int) -> list[list[str]]:
            if value is None:
                return []
            if not isinstance(value, (list, tuple)):
                raise TypeError("请使用评分要点表格填写文本和十进制分值。")
            rows: list[list[str]] = []
            for row in value:
                if not isinstance(row, (list, tuple)) or len(row) != width:
                    raise ValueError("评分要点表格列数不正确。")
                if any(item is not None and not isinstance(item, str) for item in row):
                    raise ValueError("分值请填写十进制文本，不接受浮点数。")
                cells = [(item or "").strip() for item in row]
                if any(cells):
                    rows.append(cells)
            return rows

        def load_scoring(
            identifier: str | None, qid: str | None, current_state: Mapping[str, Any]
        ) -> dict[Any, Any]:
            try:
                teacher = UUID(_teacher_id(current_state))
                if not identifier or not qid:
                    raise ValueError("请选择当前考试中的题目。")
                with get_session_factory()() as session:
                    view = ExamScoringService(session).get_scoring_basis(
                        UUID(identifier), UUID(qid), teacher_id=teacher
                    )
                return {**clear_scoring(), **scoring_data(view)}
            except errors as error:
                return {**clear_scoring(), scoring_status: _format_error(error)}

        def prepare_scoring(
            identifier: str | None,
            qid: str | None,
            mode: str,
            rows: Any,
            loaded: Any,
            current_state: Mapping[str, Any],
        ) -> dict[Any, Any]:
            try:
                teacher = UUID(_teacher_id(current_state))
                view = loaded_scoring(identifier, qid, loaded)
                if mode not in {"可加总数值要点", "非加总数值要点", "定性文字标准"}:
                    raise ValueError("请选择评分结构。")
                supplied = [] if mode == "定性文字标准" else point_rows(rows, 3)
                payload = ScoringPrepareRequest.model_validate(
                    {
                        "expected_question_validation_revision": view.question_validation_revision,
                        "expected_effective_score": view.effective_score,
                        "expected_base_score": view.base_score,
                        "expected_basis": view.basis,
                        "additive": mode == "可加总数值要点",
                        "points": [
                            {"key": row[0], "label": row[1], "base_points": row[2]}
                            for row in supplied
                        ],
                    }
                )
                with get_session_factory()() as session:
                    prepared = ExamScoringService(session).prepare_scoring_basis(
                        view.exam_id, view.question_id, payload, teacher_id=teacher
                    )
                return {**invalidate(), **scoring_data(prepared)}
            except errors as error:
                return {
                    **invalidate(),
                    scoring_confirm: gr.update(interactive=False),
                    scoring_status: _format_error(error),
                }

        def confirm_scoring(
            identifier: str | None,
            qid: str | None,
            rows: Any,
            reason: str,
            loaded: Any,
            current_state: Mapping[str, Any],
        ) -> dict[Any, Any]:
            try:
                teacher = UUID(_teacher_id(current_state))
                view = loaded_scoring(identifier, qid, loaded)
                if view.basis is None or view.basis.preparation_id is None:
                    raise ValueError("请先重新准备本场评分标准。")
                payload = ScoringConfirmRequest.model_validate(
                    {
                        "preparation_id": view.basis.preparation_id,
                        "expected_basis": view.basis,
                        "expected_question_validation_revision": view.question_validation_revision,
                        "expected_effective_score": view.effective_score,
                        "expected_base_score": view.base_score,
                        "confirmed_points": [
                            {"key": row[0], "points": row[1]}
                            for row in point_rows(rows, 2)
                        ],
                        "reason": reason,
                    }
                )
                with get_session_factory()() as session:
                    confirmed_view = ExamScoringService(session).confirm_scoring_basis(
                        view.exam_id, view.question_id, payload, teacher_id=teacher
                    )
                return {**invalidate(), **scoring_data(confirmed_view)}
            except errors as error:
                return {
                    **invalidate(),
                    scoring_confirm: gr.update(interactive=False),
                    scoring_status: _format_error(error),
                }

        def scoring_inputs_changed() -> dict[Any, Any]:
            return {
                **invalidate(),
                scoring_confirm: gr.update(interactive=False),
                scoring_status: feedback(
                    "基准要点已编辑，请重新准备本场标准后确认。", "warning"
                ),
            }

        def scoring_mode_changed(
            mode: str, loaded: Any, current_state: Mapping[str, Any]
        ) -> dict[Any, Any]:
            try:
                _teacher_id(current_state)
                view = ScoringBasisView.model_validate(loaded)
                result = scoring_inputs_changed()
                result[scoring_points] = gr.update(
                    interactive=view.editable
                    and view.question_type not in OBJECTIVE_QUESTION_TYPES
                    and mode != "定性文字标准",
                )
                if mode == "定性文字标准":
                    result[scoring_points]["value"] = []
                return result
            except errors as error:
                return {**invalidate(), scoring_status: _format_error(error)}

        def checked_data(
            identifier: str, current_state: Mapping[str, Any]
        ) -> tuple[ExamSummary, list[QuestionSummary], dict[str, Any]]:
            exam, questions = read_exam(identifier, current_state)
            with get_session_factory()() as session:
                preview = ExamService(session).preview_assembly(
                    identifier, teacher_id=_teacher_id(current_state)
                )
            snapshot = {
                "assembly": preview.model_dump(mode="json"),
                "exam": exam.model_dump(mode="json"),
                "questions": [
                    question.model_dump(mode="json") for question in questions
                ],
            }
            return exam, questions, snapshot

        def check(
            identifier: str | None, current_state: Mapping[str, Any]
        ) -> dict[Any, Any]:
            try:
                if not identifier:
                    raise ValueError("请先保存考试。")
                exam, questions, snapshot = checked_data(identifier, current_state)
                issues = exam_publication_issues(exam, questions)
                issues.extend(
                    item["message"]
                    for item in snapshot["assembly"]["publication_checks"]
                )
                approved_count = sum(
                    question.status == QuestionStatus.APPROVED for question in questions
                )
                details = (
                    f"**考试名称**：{escape(exam.title)}\n\n"
                    f"**开放时间**：{exam_opening_label(exam)}（{UI_TIMEZONE_LABEL}）\n\n"
                    f"**已审核题目**：{approved_count} / {exam.question_count} 道　**总分**：{exam.total_score if exam.total_score is not None else "未知（待核对）"} 分\n\n"
                )
                details += (
                    feedback("；".join(issues), "warning")
                    if issues
                    else feedback("发布条件已满足，请核对并确认。", "success")
                )
                return {
                    steps: gr.update(selected="publish"),
                    publication_snapshot: None if issues else snapshot,
                    publication_details: details,
                    confirmed: gr.update(value=False, interactive=not issues),
                    publish_button: gr.update(interactive=False),
                    message: "",
                }
            except errors as error:
                return {**invalidate(), message: _format_error(error)}

        def confirm(value: bool, snapshot: dict[str, Any] | None) -> dict[Any, Any]:
            return {
                publish_button: gr.update(interactive=value and snapshot is not None)
            }

        def publish(
            identifier: str | None,
            checked: bool,
            snapshot: dict[str, Any] | None,
            current_state: Mapping[str, Any],
        ) -> dict[Any, Any]:
            try:
                if not identifier or not checked or snapshot is None:
                    raise ValueError("请先完成发布检查并确认。")
                exam, questions, current = checked_data(identifier, current_state)
                if snapshot != current:
                    raise ValueError("考试或题目已发生变化，请重新执行发布检查。")
                issues = exam_publication_issues(exam, questions)
                issues.extend(
                    item["message"]
                    for item in current["assembly"]["publication_checks"]
                )
                if issues:
                    raise ValueError("；".join(issues))
                with get_session_factory()() as session:
                    published = ExamService(session).publish_exam(
                        identifier, teacher_id=_teacher_id(current_state)
                    )
                result = {
                    **list_updates(published.course_id, "", current_state),
                    **form(published, current_state, step="publish"),
                }
                result[publication_details] = feedback(
                    f"考试“{published.title}”已发布。", "success"
                )
                result[message] = feedback(
                    "考试已发布，学生参加资格由开放时间和考试状态决定。", "success"
                )
                return result
            except errors as error:
                return {**invalidate(), message: _format_error(error)}

        def change_status(
            label: str,
            identifier: str | None,
            target: str | None,
            current_state: Mapping[str, Any],
        ) -> dict[Any, Any]:
            try:
                if not identifier or target not in {
                    ExamStatus.CLOSED.value,
                    ExamStatus.ARCHIVED.value,
                }:
                    raise ValueError("请选择关闭或归档状态。")
                with get_session_factory()() as session:
                    exam = ExamService(session).update_exam_status(
                        identifier, target, teacher_id=_teacher_id(current_state)
                    )
                return {
                    **list_updates(exam.course_id, "", current_state),
                    **form(exam, current_state),
                    message: feedback(
                        f"考试已{status_label(exam.status, entity='exam')}。", "success"
                    ),
                }
            except errors as error:
                return {message: _format_error(error)}

        event_options: dict[str, Any] = {
            "outputs": outputs,
            "show_progress": "minimal",
            "concurrency_id": "eduagent-ui",
            "concurrency_limit": 1,
        }
        scoring_question.input(
            load_scoring,
            inputs=[selected_exam, scoring_question, state],
            **event_options,
        )
        scoring_reload.click(
            load_scoring,
            inputs=[selected_exam, scoring_question, state],
            **event_options,
        )
        scoring_prepare.click(
            prepare_scoring,
            inputs=[
                selected_exam,
                scoring_question,
                scoring_mode,
                scoring_points,
                scoring_snapshot,
                state,
            ],
            **event_options,
        )
        scoring_confirm.click(
            confirm_scoring,
            inputs=[
                selected_exam,
                scoring_question,
                scoring_final,
                scoring_reason,
                scoring_snapshot,
                state,
            ],
            **event_options,
        )
        scoring_mode.input(
            scoring_mode_changed,
            inputs=[scoring_mode, scoring_snapshot, state],
            **event_options,
        )
        scoring_points.input(scoring_inputs_changed, **event_options)
        scoring_final.input(invalidate, **event_options)
        scoring_reason.input(invalidate, **event_options)
        refresh_button.click(
            refresh, inputs=[filter_course, filter_status, state], **event_options
        )
        filter_course.input(
            refresh, inputs=[filter_course, filter_status, state], **event_options
        )
        filter_status.input(
            refresh, inputs=[filter_course, filter_status, state], **event_options
        )
        new_button.click(new, inputs=[filter_course, state], **event_options)
        exams_table.select(select_row, inputs=[exam_ids, state], **event_options)
        save_button.click(save, inputs=[selected_exam, *fields, state], **event_options)
        available.table.select(
            choose_candidate,
            inputs=[available.ids, selected_exam, state],
            **event_options,
        )
        chosen.table.select(
            choose_remove, inputs=[chosen.ids, selected_exam, state], **event_options
        )
        add_button.click(
            change_questions,
            inputs=[selected_exam, candidate_id, state],
            **event_options,
        )
        reload_questions.click(reload, inputs=[selected_exam, state], **event_options)
        check_button.click(check, inputs=[selected_exam, state], **event_options)
        recheck_button.click(check, inputs=[selected_exam, state], **event_options)
        confirmed.input(
            confirm, inputs=[confirmed, publication_snapshot], **event_options
        )
        publish_button.click(
            publish,
            inputs=[selected_exam, confirmed, publication_snapshot, state],
            **event_options,
        )
        for field in fields:
            field_component: Any = field
            field_event: Any = (
                getattr(field_component, "input", None) or field_component.change
            )
            field_event(
                invalidate,
                outputs=[
                    publication_snapshot,
                    confirmed,
                    publish_button,
                    publication_details,
                ],
                show_progress="hidden",
            )
        bind_confirmation(
            remove_button,
            action="移除题目",
            target=remove_target,
            callback=remove_question,
            inputs=[remove_target, remove_id, selected_exam, state],
            outputs=outputs,
        )
        bind_confirmation(
            lifecycle_button,
            action="更新考试状态",
            target=lifecycle_target,
            callback=change_status,
            inputs=[lifecycle_target, selected_exam, lifecycle, state],
            outputs=outputs,
        )
    return ExamView(panel, exams_table, message)


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
