"""EduAgent Gradio 应用外壳、登录状态和角色导航。"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from functools import partial, wraps
from html import escape
from typing import Any, TypedDict

import gradio as gr
from sqlalchemy.exc import SQLAlchemyError

from backend.app.core.database import get_session_factory
from backend.app.domain.enums import ExamStatus, UserRole
from backend.app.domain.permissions import (
    ROLE_DISPLAY_NAMES,
    PermissionDeniedError,
    normalize_role,
)
from backend.app.models import User
from backend.app.services.auth_service import AuthenticationError, AuthService
from backend.app.services.course_service import (
    CourseService,
    CourseServiceError,
    CourseSummary,
)
from backend.app.services.exam_service import ExamService, ExamSummary
from backend.app.ui.admin_view import AdminView, create_admin_view
from backend.app.ui.exam_view import ExamView, create_exam_view
from backend.app.ui.knowledge_base_view import (
    KnowledgeBaseView,
    create_knowledge_base_view,
)
from backend.app.ui.layout_view import (
    ROLE_NAVIGATION,
    WORKSPACE_CSS,
    empty_state,
    feedback,
    is_authorized_navigation,
    navigation_item,
    placeholder_page,
    resettable_components,
    status_badge,
    table_options,
)
from backend.app.ui.layout_view import (
    LayoutNavigationItem as NavigationItem,
)
from backend.app.ui.question_generation_view import (
    QuestionGenerationView,
    create_question_generation_view,
)
from backend.app.ui.question_view import QuestionView, create_question_view
from backend.app.ui.results_view import (
    ResultsView,
    TeacherResultsView,
    create_results_view,
    create_teacher_results_view,
    review_context_is_complete,
)
from backend.app.ui.review_view import ReviewView, create_review_view
from backend.app.ui.student_exam_view import (
    StudentExamView,
    create_student_exam_view,
)


class LoginState(TypedDict):
    """Gradio 会话中保存的最小登录状态。"""

    access_token: str
    user_id: str
    username: str
    email: str
    roles: list[str]


NAVIGATION_ITEMS: tuple[NavigationItem, ...] = ROLE_NAVIGATION

_EMPTY_LOGIN_STATE: LoginState = {
    "access_token": "",
    "user_id": "",
    "username": "",
    "email": "",
    "roles": [],
}
_UNAUTHENTICATED_MESSAGE = "请先登录。"
_GENERIC_ERROR_MESSAGE = "操作失败，请稍后重试。"

TEACHER_DASHBOARD_UNAVAILABLE_MESSAGE = "教师概览数据暂不可用，请稍后重试。"
TEACHER_DASHBOARD_SCOPE_UNAVAILABLE = "当前课程：暂不可用"
TEACHER_DASHBOARD_SUMMARY_UNAVAILABLE = "暂不可用"
TEACHER_DASHBOARD_COURSE_EMPTY_MESSAGE = "暂无可展示的课程"
TEACHER_DASHBOARD_TODO_EMPTY_MESSAGE = "暂无待办"
TEACHER_DASHBOARD_EXAM_EMPTY_MESSAGE = "暂无最近考试"
TEACHER_DASHBOARD_GRADE_EMPTY_MESSAGE = "暂无最终成绩概览"
TEACHER_DASHBOARD_COURSE_SLOT_COUNT = 4
TEACHER_DASHBOARD_TODO_HEADERS = ("类别", "所属课程/考试", "状态", "处理入口")
TEACHER_DASHBOARD_EXAM_HEADERS = (
    "考试名称",
    "所属课程",
    "开放时间",
    "状态",
    "题目数",
    "总分",
)


@dataclass(frozen=True)
class TeacherDashboardView:
    """教师概览由主应用控制的组件集合。"""

    panel: gr.Column
    current_scope: gr.Markdown
    course_filter: gr.Dropdown
    refresh_button: gr.Button
    summary_values: tuple[gr.Textbox, ...]
    course_records: gr.State
    course_cards: tuple[gr.HTML, ...]
    course_slots: tuple[gr.Column, ...]
    course_entry_buttons: tuple[gr.Button, ...]
    course_empty: gr.Markdown
    todo_table: gr.Dataframe
    todo_empty: gr.Markdown
    recent_exams_table: gr.Dataframe
    recent_exams_empty: gr.Markdown
    grade_overview: gr.Markdown
    message: gr.Markdown


@dataclass(frozen=True)
class TeacherDashboardPayload:
    """教师概览一次刷新所需的已授权数据和显示状态。"""

    scope: str
    refresh_interactive: bool
    course_choices: list[tuple[str, str]]
    selected_course_id: str | None
    course_records: list[dict[str, Any]]
    summary_values: tuple[str, str, str, str]
    course_cards: tuple[str, ...]
    course_entry_interactive: tuple[bool, ...]
    course_empty: str
    todo_rows: list[list[str]]
    todo_empty: str
    recent_exam_rows: list[list[str]]
    recent_exams_empty: str
    grade_overview: str
    message: str


def empty_login_state() -> LoginState:
    """返回新的空登录状态，避免不同浏览器会话共享可变对象。"""

    return {
        "access_token": _EMPTY_LOGIN_STATE["access_token"],
        "user_id": _EMPTY_LOGIN_STATE["user_id"],
        "username": _EMPTY_LOGIN_STATE["username"],
        "email": _EMPTY_LOGIN_STATE["email"],
        "roles": [],
    }


def _ensure_teacher_dashboard(state: Mapping[str, Any]) -> str:
    """确认当前会话是教师，并返回用于服务查询的用户标识。"""

    if not state.get("access_token"):
        raise PermissionDeniedError("请先登录。")
    try:
        roles = {normalize_role(role) for role in state.get("roles", [])}
    except (TypeError, ValueError) as error:
        raise PermissionDeniedError("当前账号无权访问教师概览。") from error
    if UserRole.TEACHER not in roles:
        raise PermissionDeniedError("当前账号无权访问教师概览。")
    teacher_id = str(state.get("user_id") or "").strip()
    if not teacher_id:
        raise PermissionDeniedError("登录状态缺少教师标识。")
    return teacher_id


def _dashboard_error_message(error: BaseException) -> str:
    """将概览查询异常转换为不泄露内部细节的中文提示。"""

    if isinstance(error, PermissionDeniedError):
        return str(error) or "当前账号无权访问教师概览。"
    if isinstance(error, SQLAlchemyError):
        return "系统暂时无法连接数据库，请稍后重试。"
    if isinstance(error, (TypeError, ValueError)):
        return f"输入有误：{str(error) or '请检查当前筛选条件。'}"
    return TEACHER_DASHBOARD_UNAVAILABLE_MESSAGE


def _dashboard_unavailable_payload(
    message: str = TEACHER_DASHBOARD_UNAVAILABLE_MESSAGE,
    *,
    kind: str = "warning",
    refresh_interactive: bool = False,
) -> TeacherDashboardPayload:
    """构造登录前或服务缺失时使用的完整不可用态。"""

    cards = [""] * TEACHER_DASHBOARD_COURSE_SLOT_COUNT
    return TeacherDashboardPayload(
        scope=TEACHER_DASHBOARD_SCOPE_UNAVAILABLE,
        refresh_interactive=refresh_interactive,
        course_choices=[],
        selected_course_id=None,
        course_records=[],
        summary_values=(
            TEACHER_DASHBOARD_SUMMARY_UNAVAILABLE,
            TEACHER_DASHBOARD_SUMMARY_UNAVAILABLE,
            TEACHER_DASHBOARD_SUMMARY_UNAVAILABLE,
            TEACHER_DASHBOARD_SUMMARY_UNAVAILABLE,
        ),
        course_cards=tuple(cards),
        course_entry_interactive=(False,) * TEACHER_DASHBOARD_COURSE_SLOT_COUNT,
        course_empty=empty_state(TEACHER_DASHBOARD_COURSE_EMPTY_MESSAGE),
        todo_rows=[],
        todo_empty=empty_state(TEACHER_DASHBOARD_TODO_EMPTY_MESSAGE),
        recent_exam_rows=[],
        recent_exams_empty=empty_state(TEACHER_DASHBOARD_EXAM_EMPTY_MESSAGE),
        grade_overview=empty_state(TEACHER_DASHBOARD_GRADE_EMPTY_MESSAGE),
        message=feedback(message, kind),
    )


def _dashboard_course_record(course: CourseSummary) -> dict[str, Any]:
    """保存服务返回的课程摘要，入口只使用已授权记录中的内部标识。"""

    return {
        "id": course.id,
        "name": course.name,
        "description": course.description or "",
        "knowledge_base_count": course.knowledge_base_count,
    }


def _dashboard_course_card(course: CourseSummary) -> str:
    """把真实课程摘要渲染为紧凑卡片，动态文本全部转义。"""

    description = " ".join((course.description or "").split()) or "暂无课程简介"
    if len(description) > 120:
        description = f"{description[:117]}..."
    return (
        '<div class="teacher-dashboard-course-card-content">'
        f"<h4>{escape(course.name)}</h4>"
        f'<p class="teacher-dashboard-course-description">{escape(description)}</p>'
        f'<p class="teacher-dashboard-course-meta">知识库：<strong>{course.knowledge_base_count}</strong> 个</p>'
        "</div>"
    )


def _dashboard_time_label(value: Any) -> str:
    """格式化服务返回的时间；缺少权威时间时明确显示未提供。"""

    return value.strftime("%Y-%m-%d %H:%M") if hasattr(value, "strftime") else "未提供"


def _dashboard_exam_opening_label(exam: ExamSummary) -> str:
    """只显示考试服务提供的开放时间，不推断个人计时或截止规则。"""

    start = _dashboard_time_label(exam.starts_at)
    end = _dashboard_time_label(exam.ends_at)
    if start == "未提供" and end == "未提供":
        return "未提供"
    if start == "未提供":
        return f"未提供 至 {end}"
    if end == "未提供":
        return f"{start} 至 未提供"
    return f"{start} 至 {end}"


def _dashboard_exam_rows(
    exams: Iterable[ExamSummary],
    courses: Iterable[CourseSummary],
) -> list[list[str]]:
    """把授权考试摘要转换为最近考试表格行。"""

    course_names = {course.id: course.name for course in courses}
    # ExamService 按创建时间升序返回，取末尾记录即可保持最近考试顺序。
    recent_exams = list(exams)[-5:][::-1]
    return [
        [
            exam.title,
            course_names.get(exam.course_id, "课程暂不可用"),
            _dashboard_exam_opening_label(exam),
            status_badge(exam.status, entity="exam"),
            str(exam.question_count),
            str(exam.total_score),
        ]
        for exam in recent_exams
    ]


def _dashboard_payload_for_courses(
    courses: Iterable[CourseSummary],
    course_id: str | None,
    exams: Iterable[ExamSummary] | None,
    *,
    message: str,
    message_kind: str = "warning",
) -> TeacherDashboardPayload:
    """根据已授权课程和考试摘要组装概览，不触碰成绩或诊断数据。"""

    all_courses = list(courses)
    choices = [(course.name, course.id) for course in all_courses]
    normalized_course_id = str(course_id or "").strip() or None
    if normalized_course_id is None:
        selected_courses = all_courses
    else:
        selected_courses = [
            course for course in all_courses if course.id == normalized_course_id
        ]
        if not selected_courses:
            raise ValueError("当前课程不可用，请刷新课程后重试。")

    selected_ids = {course.id for course in selected_courses}
    scoped_exams = (
        [exam for exam in exams if exam.course_id in selected_ids]
        if exams is not None
        else None
    )
    displayed_courses = selected_courses[:TEACHER_DASHBOARD_COURSE_SLOT_COUNT]
    cards = [_dashboard_course_card(course) for course in displayed_courses]
    cards.extend([""] * (TEACHER_DASHBOARD_COURSE_SLOT_COUNT - len(cards)))
    if not displayed_courses:
        cards[0] = empty_state(TEACHER_DASHBOARD_COURSE_EMPTY_MESSAGE)
    records = [_dashboard_course_record(course) for course in displayed_courses]

    if scoped_exams is None:
        published_count = TEACHER_DASHBOARD_SUMMARY_UNAVAILABLE
        recent_rows: list[list[str]] = []
        recent_empty = empty_state(TEACHER_DASHBOARD_EXAM_EMPTY_MESSAGE)
    else:
        published_count = str(
            sum(exam.status == ExamStatus.PUBLISHED for exam in scoped_exams)
        )
        recent_rows = _dashboard_exam_rows(scoped_exams, selected_courses)
        recent_empty = empty_state(TEACHER_DASHBOARD_EXAM_EMPTY_MESSAGE)

    scope = (
        f"当前课程：{selected_courses[0].name}"
        if normalized_course_id and selected_courses
        else "当前课程：全部课程"
    )
    detail = message
    if len(selected_courses) > len(displayed_courses):
        detail += " 当前仅展示前四门课程，进入课程管理可查看全部。"
    return TeacherDashboardPayload(
        scope=scope,
        refresh_interactive=True,
        course_choices=choices,
        selected_course_id=normalized_course_id,
        course_records=records,
        summary_values=(
            str(len(selected_courses)),
            published_count,
            TEACHER_DASHBOARD_SUMMARY_UNAVAILABLE,
            TEACHER_DASHBOARD_SUMMARY_UNAVAILABLE,
        ),
        course_cards=tuple(cards),
        course_entry_interactive=tuple(
            index < len(records) for index in range(TEACHER_DASHBOARD_COURSE_SLOT_COUNT)
        ),
        course_empty=empty_state(TEACHER_DASHBOARD_COURSE_EMPTY_MESSAGE),
        todo_rows=[],
        todo_empty=empty_state(TEACHER_DASHBOARD_TODO_EMPTY_MESSAGE),
        recent_exam_rows=recent_rows,
        recent_exams_empty=recent_empty,
        grade_overview=empty_state(TEACHER_DASHBOARD_GRADE_EMPTY_MESSAGE),
        message=feedback(detail, message_kind),
    )


def refresh_teacher_dashboard(
    course_id: str | None = None,
    state: Mapping[str, Any] | None = None,
) -> TeacherDashboardPayload:
    """读取教师授权课程和考试摘要，并保留未就绪业务的空态。"""

    current_state = state or empty_login_state()
    try:
        teacher_id = _ensure_teacher_dashboard(current_state)
        with get_session_factory()() as session:
            courses = CourseService(session).list_courses(teacher_id=teacher_id)
            try:
                exams = ExamService(session).list_exams(teacher_id=teacher_id)
            except (
                PermissionDeniedError,
                SQLAlchemyError,
                TypeError,
                ValueError,
                RuntimeError,
            ):
                return _dashboard_payload_for_courses(
                    courses,
                    course_id,
                    None,
                    message=(
                        "课程数据已加载；最近考试数据暂不可用，"
                        "待审核题、待复核评分和最终成绩仍显示暂不可用。"
                    ),
                )
    except (
        PermissionDeniedError,
        SQLAlchemyError,
        TypeError,
        ValueError,
        RuntimeError,
    ) as error:
        return _dashboard_unavailable_payload(
            _dashboard_error_message(error),
            kind="error" if isinstance(error, PermissionDeniedError) else "warning",
            refresh_interactive=bool(current_state.get("access_token")),
        )

    try:
        return _dashboard_payload_for_courses(
            courses,
            course_id,
            exams,
            message=(
                "课程与考试数据已加载；待审核题、待复核评分和最终成绩"
                "依赖的业务服务暂未就绪。"
            ),
        )
    except (TypeError, ValueError) as error:
        return _dashboard_unavailable_payload(
            _dashboard_error_message(error),
            kind="error",
            refresh_interactive=bool(current_state.get("access_token")),
        )


def _teacher_dashboard_component_updates(
    view: TeacherDashboardView,
    payload: TeacherDashboardPayload,
) -> dict[Any, Any]:
    """把概览载荷映射到组件，供登录、刷新和退出流程复用。"""

    result: dict[Any, Any] = {
        view.current_scope: payload.scope,
        view.refresh_button: gr.update(interactive=payload.refresh_interactive),
        view.course_filter: gr.update(
            choices=payload.course_choices,
            value=payload.selected_course_id,
            interactive=bool(payload.course_choices),
        ),
        view.course_records: payload.course_records,
        view.course_empty: gr.update(
            value=payload.course_empty,
            visible=not bool(payload.course_records),
        ),
        view.todo_table: payload.todo_rows,
        view.todo_empty: gr.update(
            value=payload.todo_empty,
            visible=not bool(payload.todo_rows),
        ),
        view.recent_exams_table: payload.recent_exam_rows,
        view.recent_exams_empty: gr.update(
            value=payload.recent_exams_empty,
            visible=not bool(payload.recent_exam_rows),
        ),
        view.grade_overview: payload.grade_overview,
        view.message: payload.message,
    }
    for summary_component, value in zip(view.summary_values, payload.summary_values):
        result[summary_component] = value
    for card_component, value in zip(view.course_cards, payload.course_cards):
        result[card_component] = value
    for slot, enabled in zip(view.course_slots, payload.course_entry_interactive):
        result[slot] = gr.update(visible=enabled)
    for entry_button, enabled in zip(
        view.course_entry_buttons, payload.course_entry_interactive
    ):
        result[entry_button] = gr.update(interactive=enabled)
    return result


def _create_teacher_dashboard_view(
    session_state: Any | None = None,
) -> TeacherDashboardView:
    """创建教师工作台布局和课程/考试摘要刷新事件。"""

    state = session_state or gr.State(empty_login_state())
    initial_payload = _dashboard_unavailable_payload()
    with gr.Column(visible=False, elem_classes="edu-teacher-dashboard") as panel:
        gr.HTML(
            "<style>"
            ".edu-teacher-dashboard .dashboard-title-row {align-items:center;}"
            ".edu-teacher-dashboard .dashboard-title {min-width:0;}"
            ".edu-teacher-dashboard .dashboard-summary-row {gap:12px;}"
            ".edu-teacher-dashboard .dashboard-summary {min-height:74px;}"
            ".edu-teacher-dashboard .dashboard-main {align-items:stretch;gap:16px;}"
            ".edu-teacher-dashboard .dashboard-courses,"
            ".edu-teacher-dashboard .dashboard-todos,"
            ".edu-teacher-dashboard .dashboard-recent,"
            ".edu-teacher-dashboard .dashboard-grades {min-width:0;}"
            ".edu-teacher-dashboard .dashboard-course-grid {align-items:stretch;gap:12px;}"
            ".edu-teacher-dashboard .dashboard-course-slot {min-width:0;min-height:188px;"
            "padding:12px;border:1px solid #e1e5eb;border-radius:6px;background:#fff;}"
            ".edu-teacher-dashboard .dashboard-course-card {min-height:116px;}"
            ".edu-teacher-dashboard .dashboard-course-card-content h4 {margin:0 0 8px;"
            "font-size:16px;overflow-wrap:anywhere;}"
            ".edu-teacher-dashboard .dashboard-course-description {min-height:48px;"
            "margin:0;color:#68717e;overflow-wrap:anywhere;}"
            ".edu-teacher-dashboard .dashboard-course-meta {margin:10px 0 0;color:#485160;}"
            ".edu-teacher-dashboard .dashboard-course-entry {min-height:44px;width:100%;}"
            ".edu-teacher-dashboard .dashboard-empty {min-height:120px;}"
            "@media(max-width:1023px){"
            ".edu-teacher-dashboard .dashboard-main {flex-wrap:wrap;}"
            ".edu-teacher-dashboard .dashboard-courses,"
            ".edu-teacher-dashboard .dashboard-todos {flex:1 1 100% !important;}"
            "}"
            "@media(max-width:767px){"
            ".edu-teacher-dashboard .dashboard-title-row {flex-wrap:wrap;}"
            ".edu-teacher-dashboard .dashboard-summary-row {flex-wrap:wrap;}"
            ".edu-teacher-dashboard .dashboard-summary {flex:1 1 45%;}"
            ".edu-teacher-dashboard .dashboard-course-grid {flex-wrap:wrap;}"
            ".edu-teacher-dashboard .dashboard-course-slot {flex:1 1 100%;}"
            "}"
            "</style>"
        )
        with gr.Row(equal_height=False, elem_classes="dashboard-title-row"):
            with gr.Column(scale=2, min_width=0, elem_classes="dashboard-title"):
                gr.Markdown("## 教师概览")
                current_scope = gr.Markdown(
                    initial_payload.scope,
                    elem_classes="dashboard-current-scope",
                )
            course_filter = gr.Dropdown(
                label="当前课程",
                choices=[],
                value=None,
                interactive=False,
                scale=1,
                min_width=220,
            )
            refresh_button = gr.Button(
                "刷新概览",
                variant="primary",
                scale=0,
                interactive=False,
            )

        with gr.Row(elem_classes="dashboard-summary-row"):
            summary_values = [
                gr.Textbox(
                    label=label,
                    value=initial_payload.summary_values[index],
                    interactive=False,
                    elem_classes="dashboard-summary",
                )
                for index, label in enumerate(
                    ("课程数", "已发布考试", "待审核题", "待复核评分")
                )
            ]
        gr.Markdown(
            "统计仅展示当前教师已授权且完整的数据；缺失业务数据显示‘暂不可用’。"
        )
        message = gr.Markdown(initial_payload.message)
        course_records = gr.State([])

        with gr.Row(equal_height=False, elem_classes="dashboard-main"):
            with gr.Column(
                scale=2,
                min_width=0,
                elem_classes="dashboard-courses",
            ):
                gr.Markdown("### 我的课程")
                course_empty = gr.Markdown(
                    initial_payload.course_empty,
                    elem_classes="dashboard-empty",
                )
                with gr.Row(
                    equal_height=False,
                    elem_classes="dashboard-course-grid",
                ):
                    course_cards: list[gr.HTML] = []
                    course_slots: list[gr.Column] = []
                    course_entry_buttons: list[gr.Button] = []
                    for index in range(TEACHER_DASHBOARD_COURSE_SLOT_COUNT):
                        with gr.Column(
                            scale=1,
                            min_width=170,
                            elem_classes="dashboard-course-slot",
                            visible=False,
                        ) as course_slot:
                            course_slots.append(course_slot)
                            course_card = gr.HTML(
                                initial_payload.course_cards[index],
                                elem_classes="dashboard-course-card",
                            )
                            course_entry_button = gr.Button(
                                "进入课程",
                                variant="secondary",
                                interactive=False,
                                elem_classes="dashboard-course-entry",
                            )
                            course_cards.append(course_card)
                            course_entry_buttons.append(course_entry_button)
            with gr.Column(
                scale=1,
                min_width=0,
                elem_classes="dashboard-todos",
            ):
                gr.Markdown("### 待办列表")
                todo_table = gr.Dataframe(
                    headers=list(TEACHER_DASHBOARD_TODO_HEADERS),
                    datatype=["str", "str", "markdown", "str"],
                    value=initial_payload.todo_rows,
                    interactive=False,
                    label="候选题审核与待复核评分",
                    **table_options(TEACHER_DASHBOARD_TODO_HEADERS),
                )
                todo_empty = gr.Markdown(
                    initial_payload.todo_empty,
                    elem_classes="dashboard-empty",
                )

        with gr.Row(equal_height=False, elem_classes="dashboard-main"):
            with gr.Column(
                scale=2,
                min_width=0,
                elem_classes="dashboard-recent",
            ):
                gr.Markdown("### 最近考试")
                recent_exams_table = gr.Dataframe(
                    headers=list(TEACHER_DASHBOARD_EXAM_HEADERS),
                    datatype=["str", "str", "str", "markdown", "str", "str"],
                    value=initial_payload.recent_exam_rows,
                    interactive=False,
                    label="最近考试（仅当前教师授权课程）",
                    **table_options(TEACHER_DASHBOARD_EXAM_HEADERS),
                )
                recent_exams_empty = gr.Markdown(
                    initial_payload.recent_exams_empty,
                    elem_classes="dashboard-empty",
                )
            with gr.Column(
                scale=1,
                min_width=0,
                elem_classes="dashboard-grades",
            ):
                gr.Markdown("### 最终成绩概览")
                grade_overview = gr.Markdown(initial_payload.grade_overview)
                gr.Markdown("平均分仅基于服务返回的最终成绩；待复核评分不计入。")

        def refresh_panel(
            selected_course: str | None,
            current_state: Mapping[str, Any],
        ) -> dict[Any, Any]:
            """刷新概览；未就绪的审核、复核和成绩服务继续显示空态。"""

            payload = refresh_teacher_dashboard(selected_course, current_state)
            return _teacher_dashboard_component_updates(view, payload)

        view = TeacherDashboardView(
            panel=panel,
            current_scope=current_scope,
            course_filter=course_filter,
            refresh_button=refresh_button,
            summary_values=tuple(summary_values),
            course_records=course_records,
            course_cards=tuple(course_cards),
            course_slots=tuple(course_slots),
            course_entry_buttons=tuple(course_entry_buttons),
            course_empty=course_empty,
            todo_table=todo_table,
            todo_empty=todo_empty,
            recent_exams_table=recent_exams_table,
            recent_exams_empty=recent_exams_empty,
            grade_overview=grade_overview,
            message=message,
        )
        refresh_button.click(
            refresh_panel,
            inputs=[course_filter, state],
            outputs=[
                view.current_scope,
                view.refresh_button,
                view.course_filter,
                *view.summary_values,
                view.course_records,
                view.course_empty,
                *view.course_cards,
                *view.course_slots,
                *view.course_entry_buttons,
                view.todo_table,
                view.todo_empty,
                view.recent_exams_table,
                view.recent_exams_empty,
                view.grade_overview,
                view.message,
            ],
            show_progress="hidden",
        )
        course_filter.change(
            refresh_panel,
            inputs=[course_filter, state],
            outputs=[
                view.current_scope,
                view.refresh_button,
                view.course_filter,
                *view.summary_values,
                view.course_records,
                view.course_empty,
                *view.course_cards,
                *view.course_slots,
                *view.course_entry_buttons,
                view.todo_table,
                view.todo_empty,
                view.recent_exams_table,
                view.recent_exams_empty,
                view.grade_overview,
                view.message,
            ],
            show_progress="hidden",
        )
    return view


create_teacher_dashboard_view = _create_teacher_dashboard_view


def normalize_ui_roles(
    roles: Iterable[UserRole | str | Any] | UserRole | str,
) -> tuple[UserRole, ...]:
    """将数据库角色、字符串和枚举统一成稳定排序的角色元组。"""

    if isinstance(roles, (str, UserRole)):
        roles = (roles,)

    normalized: set[UserRole] = set()
    for role in roles:
        candidate = getattr(role, "name", role)
        try:
            normalized.add(normalize_role(candidate))
        except (TypeError, ValueError):
            # 异常角色只影响导航显示，不能意外放开任何入口。
            continue
    return tuple(sorted(normalized, key=lambda item: item.value))


def navigation_for_roles(
    roles: Iterable[UserRole | str | Any] | UserRole | str,
) -> tuple[NavigationItem, ...]:
    """返回当前角色能够看到的导航项。"""

    role_set = set(normalize_ui_roles(roles))
    return tuple(
        item for item in NAVIGATION_ITEMS if item.allowed_roles.intersection(role_set)
    )


def format_ui_error(error: BaseException | None) -> str:
    """将内部异常转换成统一且不泄露敏感信息的中文提示。"""

    if error is None:
        return ""

    message = str(error).strip()
    if isinstance(error, PermissionDeniedError):
        return message or "当前账号无权执行此操作。"
    if isinstance(error, AuthenticationError):
        if "JWT 密钥未配置" in message:
            return "认证服务暂时不可用，请联系管理员。"
        return "登录失败：用户名、邮箱或密码错误。"
    if isinstance(error, SQLAlchemyError):
        return "系统暂时无法连接数据库，请稍后重试。"
    if isinstance(error, ValueError):
        return f"输入有误：{message or '请检查输入内容。'}"
    return _GENERIC_ERROR_MESSAGE


def _jwt_secret_key() -> str | None:
    """只从环境变量读取 JWT 密钥，不把密钥写入 UI 或日志。"""

    value = os.getenv("JWT_SECRET_KEY", "").strip()
    return value or None


def _user_state(user: User, access_token: str) -> LoginState:
    """构造不含密码等敏感字段的 Gradio 登录状态。"""

    roles = normalize_ui_roles(user.roles)
    return {
        "access_token": access_token,
        "user_id": str(user.id),
        "username": user.username,
        "email": user.email,
        "roles": [role.value for role in roles],
    }


def _navigation_choices(
    state: Mapping[str, Any],
) -> list[tuple[str, str]]:
    """把登录状态转换为 Gradio 单选导航的显示值和内部键。"""

    return [
        (item.label, item.key) for item in navigation_for_roles(state.get("roles", []))
    ]


def _role_summary(state: Mapping[str, Any]) -> str:
    """构造登录后的用户摘要，只显示公开身份和角色。"""

    roles = normalize_ui_roles(state.get("roles", []))
    role_names = "、".join(ROLE_DISPLAY_NAMES[role] for role in roles) or "未分配角色"
    username = escape(str(state.get("username") or "用户"))
    return f'<span title="{username}（{role_names}）">{username}</span>'


def _page_for_navigation(
    selection: str | None,
    state: Mapping[str, Any],
) -> str:
    """渲染已登录用户当前导航项对应的页面占位内容。"""

    if not state.get("access_token"):
        return _UNAUTHENTICATED_MESSAGE

    allowed_items = navigation_for_roles(state.get("roles", []))
    item = next(
        (candidate for candidate in allowed_items if candidate.key == selection), None
    )
    if item is None:
        return "当前账号没有可访问的导航项。"

    return placeholder_page(item)


def login_user(
    identifier: str,
    password: str,
    *,
    session_factory: Callable[[], Any] | None = None,
    secret_key: str | None = None,
) -> tuple[LoginState, str, dict[str, Any], dict[str, Any], str, dict[str, Any], str]:
    """登录并返回 Gradio 需要更新的状态、面板和导航结果。"""

    try:
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError("用户名或邮箱不能为空。")
        if not isinstance(password, str) or not password:
            raise ValueError("密码不能为空。")

        factory = session_factory or get_session_factory()
        with factory() as session:
            service = AuthService(
                session,
                secret_key=secret_key if secret_key is not None else _jwt_secret_key(),
            )
            user = service.authenticate(identifier.strip(), password)
            access_token = service.issue_access_token(user)
            state = _user_state(user, access_token)

        choices = _navigation_choices(state)
        first_key = choices[0][1] if choices else None
        return (
            state,
            f"登录成功，欢迎 {state['username']}。",
            gr.update(visible=False),
            gr.update(visible=True),
            _role_summary(state),
            gr.update(choices=choices, value=first_key, visible=bool(choices)),
            _page_for_navigation(first_key, state),
        )
    except (
        AuthenticationError,
        PermissionDeniedError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return (
            empty_login_state(),
            format_ui_error(error),
            gr.update(visible=True),
            gr.update(visible=False),
            "",
            gr.update(choices=[], value=None, visible=False),
            _UNAUTHENTICATED_MESSAGE,
        )


def logout_user() -> (
    tuple[LoginState, str, dict[str, Any], dict[str, Any], str, dict[str, Any], str]
):
    """清理当前 Gradio 会话并回到登录面板。"""

    return (
        empty_login_state(),
        "已退出登录。",
        gr.update(visible=True),
        gr.update(visible=False),
        "",
        gr.update(choices=[], value=None, visible=False),
        _UNAUTHENTICATED_MESSAGE,
    )


def select_navigation(
    selection: str | None,
    state: Mapping[str, Any],
) -> str:
    """校验角色边界后渲染导航内容。"""

    try:
        return _page_for_navigation(selection, state)
    except (
        PermissionDeniedError,
        SQLAlchemyError,
        TypeError,
        ValueError,
        AttributeError,
    ) as error:
        return format_ui_error(error)


def _is_admin_navigation(selection: str | None) -> bool:
    """判断当前导航选择是否应该展示管理员视图。"""

    return selection in {"admin.home", "admin.users", "admin.roles", "admin.status"}


def _is_question_navigation(selection: str | None) -> bool:
    """判断当前导航选择是否应该展示教师题库视图。"""

    return selection == "teacher.questions"


def _is_exam_navigation(selection: str | None) -> bool:
    """判断当前导航选择是否应该展示教师考试视图。"""

    return selection == "teacher.exams"


def _is_student_exam_navigation(selection: str | None) -> bool:
    """判断当前导航选择是否应该展示学生考试视图。"""

    return selection == "student.exams"


def login_user_for_app(
    identifier: str,
    password: str,
    *,
    session_factory: Callable[[], Any] | None = None,
    secret_key: str | None = None,
) -> tuple[Any, ...]:
    """登录并同步设置管理员视图的显示状态。"""

    result: list[Any] = list(
        login_user(
            identifier,
            password,
            session_factory=session_factory,
            secret_key=secret_key,
        )
    )
    state = result[0]
    first_selection = _navigation_choices(state)
    is_admin = bool(first_selection) and _is_admin_navigation(first_selection[0][1])
    result[-1] = gr.update(value=result[-1], visible=not is_admin)
    return (*result, gr.update(visible=is_admin))


def login_user_for_app_with_questions(
    identifier: str,
    password: str,
    *,
    session_factory: Callable[[], Any] | None = None,
    secret_key: str | None = None,
) -> tuple[Any, ...]:
    """登录并同步设置管理员和教师题库视图的显示状态。"""

    result = list(
        login_user_for_app(
            identifier,
            password,
            session_factory=session_factory,
            secret_key=secret_key,
        )
    )
    state = result[0]
    choices = _navigation_choices(state)
    first_selection = choices[0][1] if choices else None
    is_admin = bool(state.get("access_token")) and _is_admin_navigation(first_selection)
    is_question = bool(state.get("access_token")) and _is_question_navigation(
        first_selection,
    )
    page_update = result[6]
    page_value = (
        page_update.get("value", "") if isinstance(page_update, dict) else page_update
    )
    result[6] = gr.update(
        value=page_value,
        visible=not is_admin and not is_question,
    )
    return (*result, gr.update(visible=is_question))


def login_user_for_app_with_questions_and_exams(
    identifier: str,
    password: str,
    *,
    session_factory: Callable[[], Any] | None = None,
    secret_key: str | None = None,
) -> tuple[Any, ...]:
    """登录并同步设置管理员、题库和考试视图的显示状态。"""

    result = list(
        login_user_for_app_with_questions(
            identifier,
            password,
            session_factory=session_factory,
            secret_key=secret_key,
        )
    )
    state = result[0]
    choices = _navigation_choices(state)
    first_selection = choices[0][1] if choices else None
    is_admin = bool(state.get("access_token")) and _is_admin_navigation(first_selection)
    is_question = bool(state.get("access_token")) and _is_question_navigation(
        first_selection,
    )
    is_exam = bool(state.get("access_token")) and _is_exam_navigation(first_selection)
    page_update = result[6]
    page_value = (
        page_update.get("value", "") if isinstance(page_update, dict) else page_update
    )
    result[6] = gr.update(
        value=page_value,
        visible=not is_admin and not is_question and not is_exam,
    )
    return (*result, gr.update(visible=is_exam))


def logout_user_for_app() -> tuple[Any, ...]:
    """退出登录并隐藏管理员视图。"""

    result = list(logout_user())
    result[-1] = gr.update(value=result[-1], visible=True)
    return (*result, gr.update(visible=False))


def logout_user_for_app_with_questions() -> tuple[Any, ...]:
    """退出登录并隐藏管理员和教师题库视图。"""

    result = list(logout_user_for_app())
    return (*result, gr.update(visible=False))


def logout_user_for_app_with_questions_and_exams() -> tuple[Any, ...]:
    """退出登录并隐藏管理员、题库和考试视图。"""

    result = list(logout_user_for_app_with_questions())
    return (*result, gr.update(visible=False))


def login_user_for_app_with_questions_and_exams_and_student(
    identifier: str,
    password: str,
    *,
    session_factory: Callable[[], Any] | None = None,
    secret_key: str | None = None,
) -> tuple[Any, ...]:
    """登录并同步设置管理员、教师和学生视图的显示状态。"""

    result = list(
        login_user_for_app_with_questions_and_exams(
            identifier,
            password,
            session_factory=session_factory,
            secret_key=secret_key,
        )
    )
    state = result[0]
    choices = _navigation_choices(state)
    first_selection = choices[0][1] if choices else None
    is_student_exam = bool(state.get("access_token")) and _is_student_exam_navigation(
        first_selection,
    )
    page_update = result[6]
    page_value = (
        page_update.get("value", "") if isinstance(page_update, dict) else page_update
    )
    result[6] = gr.update(
        value=page_value,
        visible=not _is_admin_navigation(first_selection)
        and not _is_question_navigation(first_selection)
        and not _is_exam_navigation(first_selection)
        and not is_student_exam,
    )
    return (*result, gr.update(visible=is_student_exam))


def logout_user_for_app_with_questions_and_exams_and_student() -> tuple[Any, ...]:
    """退出登录并隐藏管理员、教师和学生视图。"""

    result = list(logout_user_for_app_with_questions_and_exams())
    return (*result, gr.update(visible=False))


def select_navigation_for_app(
    selection: str | None,
    state: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """切换导航时同步显示普通页面或管理员视图。"""

    content = select_navigation(selection, state)
    is_admin = _can_navigate(selection, state) and _is_admin_navigation(selection)
    return (
        gr.update(value=content, visible=not is_admin),
        gr.update(visible=is_admin),
    )


def select_navigation_for_app_with_questions(
    selection: str | None,
    state: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """切换导航时同步显示普通页面、管理员或教师题库视图。"""

    content = select_navigation(selection, state)
    is_admin = _can_navigate(selection, state) and _is_admin_navigation(selection)
    is_question = _can_navigate(selection, state) and _is_question_navigation(
        selection,
    )
    return (
        gr.update(value=content, visible=not is_admin and not is_question),
        gr.update(visible=is_admin),
        gr.update(visible=is_question),
    )


def select_navigation_for_app_with_questions_and_exams(
    selection: str | None,
    state: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """切换导航时同步显示普通页面、管理员、题库或考试视图。"""

    page_update, admin_update, question_update = (
        select_navigation_for_app_with_questions(selection, state)
    )
    is_exam = _can_navigate(selection, state) and _is_exam_navigation(selection)
    page_value = (
        page_update.get("value", "") if isinstance(page_update, dict) else page_update
    )
    return (
        gr.update(
            value=page_value,
            visible=not is_exam
            and not admin_update["visible"]
            and not question_update["visible"],
        ),
        admin_update,
        question_update,
        gr.update(visible=is_exam),
    )


def select_navigation_for_app_with_questions_and_exams_and_student(
    selection: str | None,
    state: Mapping[str, Any],
) -> tuple[Any, ...]:
    """切换导航时同步显示普通页面、管理员、教师或学生考试视图。"""

    result = list(select_navigation_for_app_with_questions_and_exams(selection, state))
    is_student_exam = _can_navigate(selection, state) and _is_student_exam_navigation(
        selection,
    )
    page_update = result[0]
    page_value = (
        page_update.get("value", "") if isinstance(page_update, dict) else page_update
    )
    result[0] = gr.update(
        value=page_value,
        visible=not any(update["visible"] for update in result[1:])
        and not is_student_exam,
    )
    return (*result, gr.update(visible=is_student_exam))


def _can_navigate(selection: str | None, state: Mapping[str, Any]) -> bool:
    """同时检查登录状态和角色权限，不能只依赖组件隐藏。"""

    return bool(state.get("access_token")) and is_authorized_navigation(
        selection, state.get("roles", [])
    )


def _authenticated_state(state: Mapping[str, Any]) -> LoginState:
    """复用认证服务核验有效期、启用状态及最新角色。"""

    token = state.get("access_token", "")
    if not token:
        raise AuthenticationError("请先登录。")
    with get_session_factory()() as session:
        user = AuthService(session, secret_key=_jwt_secret_key()).get_current_user(
            token
        )
        return _user_state(user, token)


def _guard_view_callback(
    fn: Callable[..., Any], state_index: int
) -> Callable[..., Any]:
    """业务视图每次操作前核验真实会话，再交回原有角色和资源守卫。"""

    @wraps(fn)
    def guarded(*args: Any, **kwargs: Any) -> Any:
        values = list(args)
        try:
            values[state_index] = _authenticated_state(values[state_index])
        except AuthenticationError:
            raise gr.Error("登录状态已失效，请退出后重新登录。") from None
        except SQLAlchemyError:
            raise gr.Error("系统暂时无法连接数据库，请稍后重试。") from None
        return fn(*values, **kwargs)

    return guarded


def create_gradio_app() -> gr.Blocks:
    """创建共享外壳；会话与各页面输入在当前浏览器会话内保持。"""

    with gr.Blocks(title="EduAgent 教学评测平台", fill_width=True) as demo:
        session_state = gr.State(empty_login_state())
        navigation_state = gr.State({"role": "", "pages": {}})
        with gr.Column(elem_id="edu-root"):
            # 样式随视图挂载，兼容 Gradio 5/6 的挂载参数差异。
            gr.HTML(f"<style>{WORKSPACE_CSS}</style>", elem_id="edu-style")
            with gr.Column(elem_id="edu-login") as login_panel:
                gr.Markdown("# EduAgent\n\n### 登录教学评测平台")
                identifier = gr.Textbox(
                    label="用户名或邮箱", placeholder="请输入用户名或邮箱"
                )
                password = gr.Textbox(
                    label="密码", placeholder="请输入密码", type="password"
                )
                login_button = gr.Button("登录", variant="primary")
                login_message = gr.HTML(elem_id="edu-login-message")

            with gr.Column(visible=False, elem_id="edu-workspace") as workspace:
                with gr.Row(elem_id="edu-topbar"):
                    gr.HTML("<strong>EduAgent</strong>", elem_id="edu-brand")
                    context = gr.HTML(elem_id="edu-context")
                    user_summary = gr.HTML(elem_id="edu-user")
                    role_selector = gr.Dropdown(
                        choices=[],
                        value=None,
                        label="当前角色",
                        show_label=False,
                        container=False,
                        min_width=0,
                        elem_id="edu-role",
                        interactive=True,
                    )
                    logout_button = gr.Button("退出登录", elem_id="edu-logout")
                with gr.Row(elem_id="edu-shell-row"):
                    with (
                        gr.Column(scale=0, min_width=0, elem_id="edu-sidebar"),
                        gr.Accordion(
                            "功能导航", open=False, elem_id="edu-menu"
                        ) as menu,
                    ):
                        groups: dict[tuple[UserRole, str], gr.Column] = {}
                        buttons: dict[str, gr.Button] = {}
                        for role in UserRole:
                            items = navigation_for_roles([role])
                            for group in dict.fromkeys(item.group for item in items):
                                with gr.Column(
                                    visible=False, elem_classes="edu-nav-group"
                                ) as group_panel:
                                    groups[role, group] = group_panel
                                    if group in {"教学准备", "AI 教学"}:
                                        gr.Markdown(
                                            group, elem_classes="edu-group-title"
                                        )
                                    for item in items:
                                        if item.group == group:
                                            buttons[item.key] = gr.Button(
                                                item.label,
                                                elem_classes=["edu-nav"],
                                                min_width=0,
                                                elem_id="edu-nav-"
                                                + item.key.replace(".", "-"),
                                            )
                    with gr.Column(min_width=0, elem_id="edu-content"):
                        workspace_message = gr.HTML(elem_id="edu-feedback")
                        page_content = gr.Markdown()
                        admin_view: AdminView = create_admin_view(session_state)
                        question_view: QuestionView = create_question_view(
                            session_state
                        )
                        exam_view: ExamView = create_exam_view(session_state)
                        student_exam_view: StudentExamView = create_student_exam_view(
                            session_state
                        )
                        knowledge_base_view: KnowledgeBaseView = (
                            create_knowledge_base_view(session_state)
                        )
                        question_generation_view: QuestionGenerationView = (
                            create_question_generation_view(session_state)
                        )
                        review_view: ReviewView = create_review_view(session_state)
                        results_view: ResultsView = create_results_view(session_state)
                        teacher_results_view: TeacherResultsView = (
                            create_teacher_results_view(session_state)
                        )
                        teacher_dashboard_view: TeacherDashboardView = (
                            create_teacher_dashboard_view(session_state)
                        )

        panels = {
            "teacher.home": teacher_dashboard_view.panel,
            "teacher.courses": knowledge_base_view.panel,
            "teacher.questions": question_view.panel,
            "teacher.exams": exam_view.panel,
            "teacher.generate": question_generation_view.panel,
            "teacher.review": review_view.panel,
            "student.exams": student_exam_view.panel,
            "student.results": results_view.panel,
            "teacher.analytics": teacher_results_view.panel,
        }
        knowledge_navigation_keys = {"teacher.courses", "teacher.knowledge"}
        admin_sections = {
            "admin.home": admin_view.overview_section,
            "admin.users": admin_view.users_section,
            "admin.roles": admin_view.roles_section,
            "admin.status": admin_view.status_section,
        }
        resets = [
            entry
            for panel in [admin_view.panel, *panels.values()]
            for entry in resettable_components(panel)
        ]
        view_functions = list(demo.fns.values())
        for block_fn in view_functions:
            if block_fn.fn is not None and session_state in block_fn.inputs:
                block_fn.fn = _guard_view_callback(
                    block_fn.fn, block_fn.inputs.index(session_state)
                )

        def render_workspace(
            state: LoginState, nav: dict[str, Any], selected: str | None
        ) -> dict[Any, Any]:
            """单次更新所有导航和面板，保持当前角色只展示一页。"""

            role = nav.get("role", "")
            allowed = _can_navigate(selected, state) and is_authorized_navigation(
                selected, [role]
            )
            item = navigation_item(selected) if allowed else None
            result: dict[Any, Any] = {
                session_state: state,
                navigation_state: nav,
                login_panel: gr.update(visible=False),
                workspace: gr.update(visible=True),
                user_summary: _role_summary(state),
                context: f"<span>{item.label if item else '工作台'}</span>",
                role_selector: gr.update(
                    choices=[
                        (ROLE_DISPLAY_NAMES[r], r.value)
                        for r in normalize_ui_roles(state["roles"])
                    ],
                    value=role or None,
                    interactive=len(state["roles"]) > 1,
                ),
                page_content: gr.update(
                    value=placeholder_page(item),
                    visible=(
                        selected
                        not in {*panels, *admin_sections, *knowledge_navigation_keys}
                        or not allowed
                    ),
                ),
                admin_view.panel: gr.update(
                    visible=allowed and selected in admin_sections
                ),
            }
            for key, panel in {**panels, **admin_sections}.items():
                result[panel] = gr.update(visible=allowed and key == selected)
            result[knowledge_base_view.panel] = gr.update(
                visible=allowed and selected in knowledge_navigation_keys
            )
            for (group_role, _), panel in groups.items():
                result[panel] = gr.update(
                    visible=group_role.value == role and role in state["roles"]
                )
            for key, button in buttons.items():
                result[button] = gr.update(
                    elem_classes=(
                        ["edu-nav", "edu-nav-active"]
                        if allowed and key == selected
                        else ["edu-nav"]
                    )
                )
            if not state["roles"]:
                result[page_content] = gr.update(
                    value="当前账号未分配角色，请联系管理员。", visible=True
                )
            return result

        def clear_workspace(
            message: str = "已退出登录。", kind: str = "success"
        ) -> dict[Any, Any]:
            """恢复所有页面初值，防止同一浏览器切换账号后残留数据。"""

            result = render_workspace(
                empty_login_state(), {"role": "", "pages": {}}, None
            )
            result.update(
                {
                    component: gr.update(value=deepcopy(value))
                    for component, value in resets
                }
            )
            result.update(
                {
                    login_panel: gr.update(visible=True),
                    workspace: gr.update(visible=False),
                    identifier: "",
                    password: "",
                    login_message: feedback(message, kind),
                    workspace_message: "",
                    context: "",
                    user_summary: "",
                    page_content: gr.update(value="", visible=True),
                    menu: gr.update(open=False),
                }
            )
            result.update(
                _teacher_dashboard_component_updates(
                    teacher_dashboard_view,
                    _dashboard_unavailable_payload(),
                )
            )
            return result

        login_progress = gr.Progress()

        def sign_in(
            username: str, credential: str, progress: gr.Progress = login_progress
        ) -> dict[Any, Any]:
            """完成真实登录，清空旧页面并默认进入首个授权角色的概览。"""

            progress(0, desc="正在登录，请稍候。")
            response = login_user(username, credential)
            state = response[0]
            if not state["access_token"]:
                result = clear_workspace(response[1], "error")
                result[identifier] = username
                return result
            role = state["roles"][0] if state["roles"] else ""
            choices = navigation_for_roles([role])
            selected = choices[0].key if choices else None
            result = clear_workspace("")
            result.update(
                render_workspace(
                    state, {"role": role, "pages": {role: selected}}, selected
                )
            )
            if UserRole.TEACHER.value in state["roles"] and selected == "teacher.home":
                result.update(
                    _teacher_dashboard_component_updates(
                        teacher_dashboard_view,
                        refresh_teacher_dashboard(state=state),
                    )
                )
            result[workspace_message] = feedback(response[1], "success")
            return result

        def navigate(
            state: LoginState,
            nav: dict[str, Any],
            *,
            selected: str | None = None,
            role: str | None = None,
        ) -> dict[Any, Any]:
            """角色切换恢复上次页面；普通导航不重建视图或丢失草稿。"""

            try:
                current = _authenticated_state(state)
            except AuthenticationError:
                return clear_workspace("登录状态已失效，请重新登录。", "error")
            except SQLAlchemyError as error:
                return {
                    workspace_message: feedback(format_ui_error(error), "error"),
                    role_selector: gr.update(value=nav.get("role") or None),
                }
            if current["roles"] != state["roles"]:
                return clear_workspace("账号角色已变更，请重新登录。", "info")
            active_role = role if role is not None else nav.get("role", "")
            if active_role not in current["roles"]:
                return {
                    workspace_message: feedback("当前账号无权切换到该角色。", "error"),
                    role_selector: gr.update(value=nav.get("role") or None),
                }
            next_nav = deepcopy(nav)
            if role is not None:
                selected = (
                    next_nav["pages"].get(role) or navigation_for_roles([role])[0].key
                )
            if not is_authorized_navigation(selected, [active_role]):
                return {
                    workspace_message: feedback("当前账号无权访问该页面。", "error")
                }
            next_nav["role"] = active_role
            next_nav["pages"][active_role] = selected
            result = render_workspace(current, next_nav, selected)
            if active_role == UserRole.TEACHER.value and selected == "teacher.home":
                result.update(
                    _teacher_dashboard_component_updates(
                        teacher_dashboard_view,
                        refresh_teacher_dashboard(state=current),
                    )
                )
            result[workspace_message] = ""
            result[menu] = gr.update(open=False)
            return result

        outputs = list(
            dict.fromkeys(
                [
                    session_state,
                    navigation_state,
                    login_panel,
                    workspace,
                    identifier,
                    password,
                    login_message,
                    workspace_message,
                    context,
                    user_summary,
                    role_selector,
                    page_content,
                    menu,
                    admin_view.panel,
                    *panels.values(),
                    *admin_sections.values(),
                    *groups.values(),
                    *buttons.values(),
                    teacher_dashboard_view.refresh_button,
                    *teacher_dashboard_view.course_slots,
                    *teacher_dashboard_view.course_entry_buttons,
                    *(component for component, _ in resets),
                ]
            )
        )
        # 同一队列串行处理数据操作和退出，避免慢请求在退出清空后重新填入旧数据。
        event_options: dict[str, Any] = {
            "outputs": outputs,
            "show_progress": "minimal",
            "concurrency_id": "eduagent-ui",
            "concurrency_limit": 1,
        }

        def open_review_from_results(
            context_value: Mapping[str, Any] | None,
            current_state: LoginState,
            nav: dict[str, Any],
        ) -> dict[Any, Any]:
            """从教师成绩页带着完整实体上下文切换到 T110。"""

            try:
                current = _authenticated_state(current_state)
            except AuthenticationError:
                return {
                    workspace_message: feedback(
                        "登录状态已失效，请退出后重新登录。", "error"
                    )
                }
            except SQLAlchemyError:
                return {
                    workspace_message: feedback(
                        "系统暂时无法连接数据库，请稍后重试。", "error"
                    )
                }
            if UserRole.TEACHER.value not in current["roles"]:
                return {
                    workspace_message: feedback(
                        "当前账号无权访问阅卷复核功能。", "error"
                    )
                }
            if not review_context_is_complete(context_value):
                return {
                    teacher_results_view.message: feedback(
                        "暂无可展示的完整待复核结果。", "info"
                    )
                }
            assert context_value is not None

            next_nav = deepcopy(nav)
            next_nav["role"] = UserRole.TEACHER.value
            next_nav.setdefault("pages", {})[UserRole.TEACHER.value] = "teacher.review"
            result = render_workspace(current, next_nav, "teacher.review")
            result.update(
                {
                    review_view.review_context: deepcopy(context_value),
                    review_view.exam_filter: str(context_value.get("exam_id", "")),
                    review_view.student_filter: str(
                        context_value.get("student_id")
                        or context_value.get("student_name", "")
                    ),
                    review_view.message: feedback(
                        "已从成绩与学情进入阅卷复核，考试、答卷和题目上下文已绑定。",
                        "info",
                    ),
                }
            )
            return result

        def open_course_from_dashboard(
            course_records: Sequence[Mapping[str, Any]] | None,
            current_state: LoginState,
            nav: dict[str, Any],
            *,
            index: int,
        ) -> dict[Any, Any]:
            """从课程卡片进入课程管理，并再次通过课程服务校验归属。"""

            try:
                current = _authenticated_state(current_state)
            except AuthenticationError:
                return {
                    workspace_message: feedback(
                        "登录状态已失效，请退出后重新登录。", "error"
                    )
                }
            except SQLAlchemyError:
                return {
                    workspace_message: feedback(
                        "系统暂时无法连接数据库，请稍后重试。", "error"
                    )
                }
            if UserRole.TEACHER not in normalize_ui_roles(current["roles"]):
                return {
                    teacher_dashboard_view.message: feedback(
                        "当前账号无权访问课程管理。", "error"
                    )
                }
            if (
                not isinstance(course_records, Sequence)
                or isinstance(course_records, (str, bytes))
                or not isinstance(index, int)
                or not 0 <= index < len(course_records)
                or not isinstance(course_records[index], Mapping)
            ):
                return {
                    teacher_dashboard_view.message: feedback(
                        "当前课程入口已失效，请刷新概览。", "info"
                    )
                }
            course_id = str(course_records[index].get("id") or "").strip()
            if not course_id:
                return {
                    teacher_dashboard_view.message: feedback(
                        "当前课程入口已失效，请刷新概览。", "info"
                    )
                }
            try:
                with get_session_factory()() as session:
                    course = CourseService(session).get_course(
                        course_id,
                        teacher_id=current["user_id"],
                    )
            except (
                PermissionDeniedError,
                CourseServiceError,
                SQLAlchemyError,
                TypeError,
                ValueError,
                RuntimeError,
            ) as error:
                return {
                    teacher_dashboard_view.message: feedback(
                        _dashboard_error_message(error), "error"
                    )
                }

            next_nav = deepcopy(nav)
            next_nav["role"] = UserRole.TEACHER.value
            next_nav.setdefault("pages", {})[UserRole.TEACHER.value] = "teacher.courses"
            result = render_workspace(current, next_nav, "teacher.courses")
            result[knowledge_base_view.message] = feedback(
                f"已进入课程“{course.name}”。", "info"
            )
            return result

        login_button.click(sign_in, inputs=[identifier, password], **event_options)
        password.submit(sign_in, inputs=[identifier, password], **event_options)
        logout_button.click(clear_workspace, inputs=[], **event_options)
        for key, button in buttons.items():
            button.click(
                partial(navigate, selected=key),
                inputs=[session_state, navigation_state],
                **event_options,
            )

        def switch_role(
            role: str, state: LoginState, nav: dict[str, Any]
        ) -> dict[Any, Any]:
            return navigate(state, nav, role=role)

        role_selector.input(
            switch_role,
            inputs=[role_selector, session_state, navigation_state],
            **event_options,
        )
        teacher_results_view.review_button.click(
            open_review_from_results,
            inputs=[
                teacher_results_view.review_context,
                session_state,
                navigation_state,
            ],
            outputs=outputs,
            show_progress="minimal",
            concurrency_id="eduagent-ui",
            concurrency_limit=1,
        )
        for index, course_entry_button in enumerate(
            teacher_dashboard_view.course_entry_buttons
        ):
            course_entry_button.click(
                partial(open_course_from_dashboard, index=index),
                inputs=[
                    teacher_dashboard_view.course_records,
                    session_state,
                    navigation_state,
                ],
                outputs=outputs,
                show_progress="minimal",
                concurrency_id="eduagent-ui",
                concurrency_limit=1,
            )
        for block_fn in view_functions:
            block_fn.concurrency_id = "eduagent-ui"
            block_fn.concurrency_limit = 1
    return demo


__all__ = [
    "NAVIGATION_ITEMS",
    "TEACHER_DASHBOARD_COURSE_EMPTY_MESSAGE",
    "TEACHER_DASHBOARD_EXAM_EMPTY_MESSAGE",
    "TEACHER_DASHBOARD_GRADE_EMPTY_MESSAGE",
    "TEACHER_DASHBOARD_SUMMARY_UNAVAILABLE",
    "TEACHER_DASHBOARD_TODO_EMPTY_MESSAGE",
    "TEACHER_DASHBOARD_UNAVAILABLE_MESSAGE",
    "LoginState",
    "NavigationItem",
    "TeacherDashboardPayload",
    "TeacherDashboardView",
    "create_gradio_app",
    "create_teacher_dashboard_view",
    "empty_login_state",
    "format_ui_error",
    "login_user",
    "login_user_for_app",
    "login_user_for_app_with_questions",
    "login_user_for_app_with_questions_and_exams",
    "login_user_for_app_with_questions_and_exams_and_student",
    "logout_user",
    "logout_user_for_app",
    "logout_user_for_app_with_questions",
    "logout_user_for_app_with_questions_and_exams",
    "logout_user_for_app_with_questions_and_exams_and_student",
    "navigation_for_roles",
    "normalize_ui_roles",
    "refresh_teacher_dashboard",
    "select_navigation",
    "select_navigation_for_app",
    "select_navigation_for_app_with_questions",
    "select_navigation_for_app_with_questions_and_exams",
    "select_navigation_for_app_with_questions_and_exams_and_student",
]
