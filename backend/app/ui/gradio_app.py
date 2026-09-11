"""EduAgent Gradio 应用外壳、登录状态和角色导航。"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from functools import partial, wraps
from html import escape
from typing import Any, TypedDict
from uuid import UUID

import gradio as gr
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from backend.app.core.database import get_session_factory
from backend.app.domain.enums import ExamStatus, SubmissionStatus, UserRole
from backend.app.domain.permissions import (
    ROLE_DISPLAY_NAMES,
    PermissionDeniedError,
    normalize_role,
)
from backend.app.models import Course, User
from backend.app.services.auth_service import AuthenticationError, AuthService
from backend.app.services.course_service import (
    CourseService,
    CourseServiceError,
    CourseSummary,
)
from backend.app.services.exam_service import ExamService, ExamSummary
from backend.app.services.submission_service import (
    AvailableExamSummary,
    SubmissionService,
    SubmissionServiceError,
)
from backend.app.ui.admin_view import AdminView, create_admin_view
from backend.app.ui.exam_view import ExamView, create_exam_view
from backend.app.ui.knowledge_base_view import (
    KnowledgeBaseView,
    create_knowledge_base_view,
)
from backend.app.ui.layout_view import (
    ROLE_NAVIGATION,
    WORKSPACE_CSS,
    breadcrumb_html,
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
    refresh_student_results,
    result_status_text,
    review_context_is_complete,
)
from backend.app.ui.review_view import ReviewView, create_review_view
from backend.app.ui.student_exam_view import (
    StudentExamView,
    create_student_exam_view,
)
from backend.app.ui.student_exam_view import (
    refresh_exams as refresh_student_exams,
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

STUDENT_DASHBOARD_UNAVAILABLE_MESSAGE = "学生概览数据暂不可用，请稍后重试。"
STUDENT_DASHBOARD_SUMMARY_UNAVAILABLE = "暂不可用"
STUDENT_DASHBOARD_EXAM_EMPTY_MESSAGE = "暂无可参加的考试"
STUDENT_DASHBOARD_RESULT_EMPTY_MESSAGE = "暂无最近结果"
STUDENT_DASHBOARD_DIAGNOSIS_EMPTY_MESSAGE = "暂无已确认诊断摘要"
STUDENT_DASHBOARD_EXAM_HEADERS = (
    "考试名称",
    "课程",
    "开放时间",
    "时长",
    "结果状态",
    "开始/继续",
)
STUDENT_DASHBOARD_RESULT_HEADERS = (
    "考试名称",
    "课程",
    "成绩",
    "结果状态",
    "查看结果",
)

TOPBAR_MESSAGE_HEADERS = ("类型", "关联对象", "状态", "时间", "查看入口")

PAGE_BREADCRUMB_GROUPS: Mapping[str, str] = {
    "teacher.home": "概览",
    "teacher.courses": "课程",
    "teacher.knowledge": "课程",
    "teacher.questions": "题库",
    "teacher.exams": "考试",
    "teacher.generate": "AI 教学",
    "teacher.review": "AI 教学",
    "teacher.analytics": "学情分析",
    "student.home": "学习",
    "student.exams": "学习",
    "student.results": "学习",
    "admin.home": "管理",
    "admin.users": "管理",
    "admin.roles": "管理",
    "admin.status": "管理",
}

PAGE_SEARCH_PLACEHOLDERS: Mapping[str, str] = {
    "teacher.home": "搜索课程",
    "teacher.courses": "搜索课程",
    "teacher.knowledge": "搜索课程",
    "teacher.questions": "搜索题目",
    "teacher.exams": "搜索考试",
    "teacher.generate": "搜索候选题",
    "teacher.review": "搜索待复核结果",
    "teacher.analytics": "搜索学生成绩",
    "student.home": "搜索考试或结果",
    "student.exams": "搜索考试",
    "student.results": "搜索结果",
    "admin.home": "搜索运行状态",
    "admin.users": "搜索用户",
    "admin.roles": "搜索角色",
    "admin.status": "搜索运行状态",
}

# 顶部快捷入口只指向当前角色已有页面，不在外壳中创建新的业务操作。
PAGE_PRIMARY_ACTIONS: Mapping[str, tuple[tuple[str, str], ...]] = {
    "teacher.home": (("创建课程", "teacher.courses"), ("创建考试", "teacher.exams")),
    "student.home": (("继续作答", "student.exams"), ("查看结果", "student.results")),
    "admin.home": (("创建用户", "admin.users"), ("刷新状态", "admin.status")),
    "teacher.courses": (("创建考试", "teacher.exams"),),
    "teacher.exams": (("创建课程", "teacher.courses"),),
    "student.exams": (("查看结果", "student.results"),),
    "student.results": (("继续作答", "student.exams"),),
    "admin.users": (("刷新状态", "admin.status"),),
    "admin.status": (("创建用户", "admin.users"),),
}


def _page_breadcrumb(selected: str | None, item: NavigationItem | None) -> str:
    """返回当前页面的共享面包屑，未知页面不显示虚假的层级。"""

    if not selected or item is None:
        return ""
    return breadcrumb_html(
        ("工作台", PAGE_BREADCRUMB_GROUPS.get(selected, "当前页面"), item.label)
    )


def _message_button_label(messages: Sequence[Mapping[str, Any]]) -> str:
    """仅按当前会话已记录的反馈更新消息数量。"""

    return f"消息（{len(messages)}）" if messages else "消息"


def _record_session_message(
    navigation: dict[str, Any],
    *,
    kind: str,
    related_object: str,
    status: str,
    view_key: str | None = None,
    detail: str = "",
) -> None:
    """记录本次会话反馈，不写入数据库、不跨会话复用。"""

    messages = list(navigation.get("messages", []))
    messages.append(
        {
            "kind": kind,
            "related_object": related_object,
            "status": status,
            "time": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M"),
            "view_key": view_key,
            "detail": detail,
        }
    )
    navigation["messages"] = messages[-20:]


def _message_rows(
    messages: Sequence[Mapping[str, Any]], state: Mapping[str, Any]
) -> list[list[str]]:
    """把当前会话消息转换成只包含授权查看入口的表格行。"""

    rows: list[list[str]] = []
    for message in messages:
        view_key = message.get("view_key")
        can_view = isinstance(view_key, str) and _can_navigate(view_key, state)
        rows.append(
            [
                str(message.get("kind") or "操作反馈"),
                str(message.get("related_object") or "当前页面"),
                str(message.get("status") or "提示"),
                str(message.get("time") or "本次会话"),
                "查看" if can_view else "不可用",
            ]
        )
    return rows


def _searchable_rows(value: Any) -> list[tuple[str, str]]:
    """读取页面组件当前已加载的行，搜索不触发额外服务查询。"""

    if hasattr(value, "values") and hasattr(value.values, "tolist"):
        value = value.values.tolist()
    if isinstance(value, Mapping):
        value = [value]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    rows: list[tuple[str, str]] = []
    for index, row in enumerate(value, start=1):
        if isinstance(row, Mapping):
            text = "；".join(
                f"{key}：{item}" for key, item in row.items() if item not in (None, "")
            )
        elif isinstance(row, Sequence) and not isinstance(row, (str, bytes, bytearray)):
            text = "；".join(str(item) for item in row if item not in (None, ""))
        else:
            text = str(row)
        if text.strip():
            rows.append((str(index), text))
    return rows


def _loaded_todo_messages(
    state: Mapping[str, Any],
    teacher_todo_value: Any = None,
    teacher_result_value: Any = None,
    student_result_value: Any = None,
) -> list[dict[str, Any]]:
    """把已经加载的角色范围内待处理行转换成临时会话消息。"""

    roles = set(normalize_ui_roles(state.get("roles", [])))
    records: list[dict[str, Any]] = []
    if UserRole.TEACHER in roles:
        for _, row_text in _searchable_rows(teacher_todo_value):
            target = (
                "teacher.review"
                if "复核" in row_text
                else "teacher.questions" if "审核" in row_text else "teacher.home"
            )
            records.append(
                {
                    "kind": "待办",
                    "related_object": row_text[:160],
                    "status": "待处理",
                    "time": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M"),
                    "view_key": target if _can_navigate(target, state) else None,
                    "detail": "来自当前教师已加载的待办列表。",
                }
            )
        for _, row_text in _searchable_rows(teacher_result_value):
            if not any(marker in row_text for marker in ("待", "复核", "Pending")):
                continue
            records.append(
                {
                    "kind": "成绩待办",
                    "related_object": row_text[:160],
                    "status": "待处理",
                    "time": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M"),
                    "view_key": (
                        "teacher.review"
                        if _can_navigate("teacher.review", state)
                        else None
                    ),
                    "detail": "来自当前教师已加载的成绩结果。",
                }
            )
    if UserRole.STUDENT in roles:
        for _, row_text in _searchable_rows(student_result_value):
            if not any(marker in row_text for marker in ("待", "复核", "Pending")):
                continue
            records.append(
                {
                    "kind": "结果反馈",
                    "related_object": row_text[:160],
                    "status": "待确认",
                    "time": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M"),
                    "view_key": (
                        "student.results"
                        if _can_navigate("student.results", state)
                        else None
                    ),
                    "detail": "来自当前学生已加载的结果状态。",
                }
            )
    return records


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


@dataclass(frozen=True)
class StudentDashboardView:
    """学生概览由主应用控制的组件集合。"""

    panel: gr.Column
    refresh_button: gr.Button
    summary_values: tuple[gr.Textbox, ...]
    available_exams_table: gr.Dataframe
    exam_records: gr.State
    selected_exam_id: gr.Textbox
    continue_button: gr.Button
    exam_empty: gr.Markdown
    recent_results_table: gr.Dataframe
    result_records: gr.State
    selected_result_id: gr.Textbox
    view_results_button: gr.Button
    result_empty: gr.Markdown
    diagnosis: gr.Markdown
    message: gr.Markdown

    @property
    def exam_table(self) -> gr.Dataframe:
        """兼容按考试表命名的调用方。"""

        return self.available_exams_table

    @property
    def results_table(self) -> gr.Dataframe:
        """兼容按结果表命名的调用方。"""

        return self.recent_results_table


@dataclass(frozen=True)
class StudentDashboardPayload:
    """学生概览一次刷新所需的当前学生授权数据和显示状态。"""

    refresh_interactive: bool
    summary_values: tuple[str, str, str]
    available_exam_rows: list[list[str]]
    available_exam_records: list[dict[str, Any]]
    exam_empty: str
    recent_result_rows: list[list[str]]
    recent_result_records: list[dict[str, Any]]
    result_empty: str
    diagnosis: str
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


def _ensure_student_dashboard(state: Mapping[str, Any]) -> str:
    """确认当前会话是学生，并返回用于服务查询的用户标识。"""

    if not state.get("access_token"):
        raise PermissionDeniedError("请先登录。")
    try:
        roles = {normalize_role(role) for role in state.get("roles", [])}
    except (TypeError, ValueError) as error:
        raise PermissionDeniedError("当前账号无权访问学生概览。") from error
    if UserRole.STUDENT not in roles:
        raise PermissionDeniedError("当前账号无权访问学生概览。")
    student_id = str(state.get("user_id") or "").strip()
    if not student_id:
        raise PermissionDeniedError("登录状态缺少学生标识。")
    return student_id


def _student_dashboard_error_message(error: BaseException) -> str:
    """将学生概览异常转换为不泄露内部细节的中文提示。"""

    if isinstance(error, PermissionDeniedError):
        return str(error) or "当前账号无权访问学生概览。"
    if isinstance(error, SQLAlchemyError):
        return "系统暂时无法连接数据库，请稍后重试。"
    if isinstance(error, SubmissionServiceError):
        return str(error) or STUDENT_DASHBOARD_UNAVAILABLE_MESSAGE
    if isinstance(error, (TypeError, ValueError)):
        return f"输入有误：{str(error) or '请检查当前考试数据。'}"
    return STUDENT_DASHBOARD_UNAVAILABLE_MESSAGE


def _student_dashboard_unavailable_payload(
    message: str = STUDENT_DASHBOARD_UNAVAILABLE_MESSAGE,
    *,
    kind: str = "warning",
    refresh_interactive: bool = False,
) -> StudentDashboardPayload:
    """构造登录前或学生业务服务缺失时使用的完整不可用态。"""

    return StudentDashboardPayload(
        refresh_interactive=refresh_interactive,
        summary_values=(
            STUDENT_DASHBOARD_SUMMARY_UNAVAILABLE,
            STUDENT_DASHBOARD_SUMMARY_UNAVAILABLE,
            STUDENT_DASHBOARD_SUMMARY_UNAVAILABLE,
        ),
        available_exam_rows=[],
        available_exam_records=[],
        exam_empty=empty_state(STUDENT_DASHBOARD_EXAM_EMPTY_MESSAGE),
        recent_result_rows=[],
        recent_result_records=[],
        result_empty=empty_state(STUDENT_DASHBOARD_RESULT_EMPTY_MESSAGE),
        diagnosis=empty_state(STUDENT_DASHBOARD_DIAGNOSIS_EMPTY_MESSAGE),
        message=feedback(message, kind),
    )


def _student_dashboard_value(
    item: Mapping[str, Any] | Any,
    name: str,
    default: Any = None,
) -> Any:
    """从服务 DTO 或映射中读取字段，不改变服务返回值。"""

    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _student_dashboard_first_value(
    item: Mapping[str, Any] | Any,
    names: Sequence[str],
    default: Any = None,
) -> Any:
    """按兼容字段名读取第一个已提供的值。"""

    for name in names:
        value = _student_dashboard_value(item, name, None)
        if value not in (None, ""):
            return value
    return default


def _student_dashboard_status_code(value: Any) -> str:
    """将服务状态转换为比较用文本，不根据分数推导结果状态。"""

    raw = getattr(value, "value", value)
    return str(raw or "").strip().casefold().replace("_", " ")


def _student_dashboard_is_final_status(value: Any) -> bool:
    """只识别服务明确返回的最终结果状态。"""

    return _student_dashboard_status_code(value) in {"final", "reviewed"}


def _student_dashboard_submitted_count(records: Iterable[Any]) -> int:
    """只按答卷服务返回的生命周期状态统计已提交答卷。"""

    submitted_statuses = {
        SubmissionStatus.SUBMITTED.value.casefold(),
        SubmissionStatus.GRADED.value.casefold(),
        SubmissionStatus.REVIEWED.value.casefold(),
    }
    return sum(
        _student_dashboard_status_code(_student_dashboard_value(record, "status"))
        in submitted_statuses
        for record in records
    )


def _student_dashboard_course_names(
    session: Any,
    exams: Iterable[AvailableExamSummary],
) -> dict[str, str]:
    """只读取已由学生考试服务授权返回的考试所属课程名称。"""

    course_ids = {
        str(exam.course_id).strip()
        for exam in exams
        if str(exam.course_id or "").strip()
    }
    if not course_ids:
        return {}
    normalized_course_ids: list[UUID] = []
    for course_id in course_ids:
        try:
            normalized_course_ids.append(UUID(course_id))
        except (AttributeError, TypeError, ValueError):
            continue
    if not normalized_course_ids:
        return {}
    try:
        courses = session.scalars(
            select(Course).where(Course.id.in_(normalized_course_ids))
        ).all()
    except SQLAlchemyError as error:
        raise SubmissionServiceError("无法读取考试所属课程信息。") from error
    return {
        str(course.id): str(course.name)
        for course in courses
        if getattr(course, "id", None) is not None and getattr(course, "name", None)
    }


def _student_dashboard_exam_data(
    exams: Iterable[AvailableExamSummary],
    course_names: Mapping[str, str] | None = None,
) -> tuple[list[list[str]], list[dict[str, Any]]]:
    """把当前学生可参加的考试摘要转换为表格行和安全入口记录。"""

    names = course_names or {}
    rows: list[list[str]] = []
    records: list[dict[str, Any]] = []
    for exam in exams:
        exam_id = str(exam.id)
        course_id = str(exam.course_id)
        course_name = names.get(course_id) or "课程暂不可用"
        duration = (
            f"{exam.duration_minutes} 分钟"
            if isinstance(exam.duration_minutes, int)
            and not isinstance(exam.duration_minutes, bool)
            and exam.duration_minutes > 0
            else "未设置"
        )
        rows.append(
            [
                exam.title,
                course_name,
                _dashboard_exam_opening_label(exam),
                duration,
                status_badge(exam.status, entity="exam"),
                "开始/继续",
            ]
        )
        records.append(
            {
                "id": exam_id,
                "exam_id": exam_id,
                "title": exam.title,
                "course_id": course_id,
                "course_name": course_name,
            }
        )
    return rows, records


def _student_dashboard_result_data(
    records: Iterable[Mapping[str, Any] | Any],
) -> tuple[list[list[str]], list[dict[str, Any]]]:
    """渲染结果服务明确提供的本人结果，待复核项不显示为最终成绩。"""

    rows: list[list[str]] = []
    safe_records: list[dict[str, Any]] = []
    for record in list(records)[-5:][::-1]:
        raw_status = _student_dashboard_first_value(
            record,
            ("result_status", "status"),
            None,
        )
        status_code = _student_dashboard_status_code(raw_status)
        if not status_code or status_code == SubmissionStatus.DRAFT.value.casefold():
            continue
        result_id = _student_dashboard_first_value(
            record,
            ("submission_id", "result_id", "id"),
            None,
        )
        exam_id = _student_dashboard_first_value(record, ("exam_id",), None)
        if result_id in (None, "") and exam_id in (None, ""):
            # 没有可校验的实体标识时不渲染无效的查看入口。
            continue
        result_id_text = str(result_id or exam_id)
        exam_name = _student_dashboard_first_value(
            record,
            ("exam_title", "exam_name", "title"),
            "考试暂不可用",
        )
        course_name = _student_dashboard_first_value(
            record,
            ("course_name", "course_title"),
            "课程暂不可用",
        )
        score = "最终成绩未形成"
        if _student_dashboard_is_final_status(raw_status):
            supplied_score = _student_dashboard_first_value(
                record,
                ("final_score", "total_score", "score"),
                None,
            )
            score = (
                str(supplied_score) if supplied_score not in (None, "") else "未提供"
            )
        rows.append(
            [
                str(exam_name),
                str(course_name),
                score,
                result_status_text(raw_status),
                "查看结果",
            ]
        )
        safe_records.append(
            {
                "id": result_id_text,
                "submission_id": str(result_id) if result_id else "",
                "exam_id": str(exam_id) if exam_id else "",
                "result_status": str(getattr(raw_status, "value", raw_status)),
                "exam_name": str(exam_name),
                "course_name": str(course_name),
                "diagnosis": _student_dashboard_first_value(
                    record,
                    ("diagnosis_summary", "diagnosis"),
                    "",
                ),
            }
        )
    return rows, safe_records


def _student_dashboard_diagnosis(
    records: Iterable[Mapping[str, Any] | Any],
) -> str:
    """只展示服务明确返回且已形成最终结果的诊断摘要。"""

    for record in records:
        status = _student_dashboard_first_value(
            record,
            ("result_status", "status"),
            None,
        )
        if not _student_dashboard_is_final_status(status):
            continue
        diagnosis = _student_dashboard_first_value(
            record,
            ("diagnosis_summary", "diagnosis"),
            None,
        )
        if diagnosis in (None, ""):
            continue
        return (
            '<div class="student-dashboard-diagnosis">'
            f"{escape(str(diagnosis))}</div>"
        )
    return empty_state(STUDENT_DASHBOARD_DIAGNOSIS_EMPTY_MESSAGE)


def _student_dashboard_payload(
    exams: Sequence[AvailableExamSummary] | None,
    submissions: Sequence[Any] | None,
    results: Sequence[Mapping[str, Any] | Any] | None,
    course_names: Mapping[str, str] | None,
    *,
    message: str,
    message_kind: str = "info",
    refresh_interactive: bool = True,
) -> StudentDashboardPayload:
    """组装概览载荷；缺少任何权威数据时保留对应不可用态。"""

    if exams is None:
        exam_rows: list[list[str]] = []
        exam_records: list[dict[str, Any]] = []
    else:
        exam_rows, exam_records = _student_dashboard_exam_data(exams, course_names)

    if submissions is None:
        submitted_count = STUDENT_DASHBOARD_SUMMARY_UNAVAILABLE
    else:
        submitted_count = str(_student_dashboard_submitted_count(submissions))

    if results is None:
        result_rows: list[list[str]] = []
        result_records: list[dict[str, Any]] = []
        result_count = STUDENT_DASHBOARD_SUMMARY_UNAVAILABLE
        diagnosis = empty_state(STUDENT_DASHBOARD_DIAGNOSIS_EMPTY_MESSAGE)
    else:
        result_rows, result_records = _student_dashboard_result_data(results)
        result_count = str(len(result_records))
        diagnosis = _student_dashboard_diagnosis(results)

    unavailable_parts = []
    if exams is None:
        unavailable_parts.append("可参加考试")
    if submissions is None:
        unavailable_parts.append("已提交数量")
    if results is None:
        unavailable_parts.append("成绩与诊断")
    detail = message
    if unavailable_parts:
        detail += " " + "、".join(unavailable_parts) + "数据暂不可用。"

    return StudentDashboardPayload(
        refresh_interactive=refresh_interactive,
        summary_values=(
            (
                str(len(exams))
                if exams is not None
                else STUDENT_DASHBOARD_SUMMARY_UNAVAILABLE
            ),
            submitted_count,
            result_count,
        ),
        available_exam_rows=exam_rows,
        available_exam_records=exam_records,
        exam_empty=empty_state(STUDENT_DASHBOARD_EXAM_EMPTY_MESSAGE),
        recent_result_rows=result_rows,
        recent_result_records=result_records,
        result_empty=empty_state(STUDENT_DASHBOARD_RESULT_EMPTY_MESSAGE),
        diagnosis=diagnosis,
        message=feedback(detail, message_kind),
    )


def refresh_student_dashboard(
    state: Mapping[str, Any] | None = None,
) -> StudentDashboardPayload:
    """读取当前学生的考试和答卷摘要，并保留成绩服务未就绪空态。"""

    current_state = state or empty_login_state()
    try:
        student_id = _ensure_student_dashboard(current_state)
        with get_session_factory()() as session:
            service = SubmissionService(session)
            exams = service.list_available_exams(student_id=student_id)
            try:
                submissions = service.list_submissions(student_id=student_id)
            except (
                SubmissionServiceError,
                SQLAlchemyError,
                TypeError,
                ValueError,
                RuntimeError,
            ):
                submissions = None
            try:
                course_names = _student_dashboard_course_names(session, exams)
            except (
                SubmissionServiceError,
                SQLAlchemyError,
                AttributeError,
                TypeError,
                ValueError,
                RuntimeError,
            ):
                course_names = {}
        return _student_dashboard_payload(
            exams,
            submissions,
            None,
            course_names,
            message="可参加考试和本人答卷数据已加载；成绩与诊断服务暂未就绪。",
        )
    except (
        PermissionDeniedError,
        SubmissionServiceError,
        SQLAlchemyError,
        AttributeError,
        TypeError,
        ValueError,
        RuntimeError,
    ) as error:
        return _student_dashboard_unavailable_payload(
            _student_dashboard_error_message(error),
            kind="error" if isinstance(error, PermissionDeniedError) else "warning",
            refresh_interactive=bool(current_state.get("access_token")),
        )


def _student_dashboard_component_updates(
    view: StudentDashboardView,
    payload: StudentDashboardPayload,
) -> dict[Any, Any]:
    """把学生概览载荷映射到组件，供登录、刷新和退出流程复用。"""

    result: dict[Any, Any] = {
        view.refresh_button: gr.update(interactive=payload.refresh_interactive),
        view.available_exams_table: payload.available_exam_rows,
        view.exam_records: payload.available_exam_records,
        view.selected_exam_id: "",
        view.continue_button: gr.update(
            interactive=bool(payload.available_exam_records)
        ),
        view.exam_empty: gr.update(
            value=payload.exam_empty,
            visible=not bool(payload.available_exam_rows),
        ),
        view.recent_results_table: payload.recent_result_rows,
        view.result_records: payload.recent_result_records,
        view.selected_result_id: "",
        view.view_results_button: gr.update(
            interactive=bool(payload.recent_result_records)
        ),
        view.result_empty: gr.update(
            value=payload.result_empty,
            visible=not bool(payload.recent_result_rows),
        ),
        view.diagnosis: payload.diagnosis,
        view.message: payload.message,
    }
    for summary_component, value in zip(view.summary_values, payload.summary_values):
        result[summary_component] = value
    return result


def _student_dashboard_record_by_id(
    records: Sequence[Mapping[str, Any]] | None,
    selected_id: str | None,
    *,
    id_names: Sequence[str],
) -> Mapping[str, Any] | None:
    """从当前会话保存的授权记录中读取选中对象，拒绝伪造标识。"""

    normalized_id = str(selected_id or "").strip()
    if (
        not normalized_id
        or not isinstance(records, Sequence)
        or isinstance(records, (str, bytes))
    ):
        return None
    for record in records:
        if not isinstance(record, Mapping):
            continue
        candidate = _student_dashboard_first_value(record, id_names, "")
        if str(candidate or "").strip() == normalized_id:
            return record
    return None


def _find_panel_component(
    panel: Any,
    component_type: type[Any],
    *,
    label: str | None = None,
    visible: bool | None = None,
    occurrence: int = 0,
) -> Any | None:
    """按稳定属性读取已有视图组件，供跨页面入口传递选择上下文。"""

    matches: list[Any] = []

    def visit(container: Any) -> None:
        for child in getattr(container, "children", []) or []:
            matches_type = isinstance(child, component_type)
            matches_label = label is None or getattr(child, "label", None) == label
            matches_visibility = (
                visible is None or getattr(child, "visible", None) == visible
            )
            if matches_type and matches_label and matches_visibility:
                matches.append(child)
            visit(child)

    visit(panel)
    return matches[occurrence] if 0 <= occurrence < len(matches) else None


def _create_student_dashboard_view(
    session_state: Any | None = None,
) -> StudentDashboardView:
    """创建学生学习概览布局和当前学生数据刷新事件。"""

    state = session_state or gr.State(empty_login_state())
    initial_payload = _student_dashboard_unavailable_payload()
    with gr.Column(visible=False, elem_classes="edu-student-dashboard") as panel:
        gr.HTML(
            "<style>"
            ".edu-student-dashboard .student-dashboard-title-row {align-items:center;}"
            ".edu-student-dashboard .student-dashboard-title {min-width:0;}"
            ".edu-student-dashboard .student-dashboard-summary-row {gap:12px;}"
            ".edu-student-dashboard .student-dashboard-summary {min-height:74px;}"
            ".edu-student-dashboard .student-dashboard-main {align-items:stretch;gap:16px;}"
            ".edu-student-dashboard .student-dashboard-exams,"
            ".edu-student-dashboard .student-dashboard-results {min-width:0;}"
            ".edu-student-dashboard .student-dashboard-empty {min-height:96px;}"
            ".edu-student-dashboard .student-dashboard-diagnosis {"
            "min-height:100px;padding:12px;border:1px solid #e1e5eb;"
            "border-radius:6px;background:#fff;overflow-wrap:anywhere;}"
            "@media(max-width:767px){"
            ".edu-student-dashboard .student-dashboard-title-row {flex-wrap:wrap;}"
            ".edu-student-dashboard .student-dashboard-summary-row {flex-wrap:wrap;}"
            ".edu-student-dashboard .student-dashboard-summary {flex:1 1 30%;}"
            ".edu-student-dashboard .student-dashboard-main {flex-wrap:wrap;}"
            ".edu-student-dashboard .student-dashboard-exams,"
            ".edu-student-dashboard .student-dashboard-results {flex:1 1 100% !important;}"
            "}"
            "</style>"
        )
        with gr.Row(
            equal_height=False,
            elem_classes="student-dashboard-title-row",
        ):
            with gr.Column(
                scale=2,
                min_width=0,
                elem_classes="student-dashboard-title",
            ):
                gr.Markdown("## 学习概览")
                gr.Markdown("仅显示当前学生已授权的考试、答卷和结果。")
            continue_button = gr.Button(
                "开始/继续",
                variant="primary",
                interactive=False,
                scale=0,
            )
            view_results_button = gr.Button(
                "查看结果",
                variant="secondary",
                interactive=False,
                scale=0,
            )
            refresh_button = gr.Button(
                "刷新概览",
                variant="secondary",
                interactive=False,
                scale=0,
            )

        with gr.Row(elem_classes="student-dashboard-summary-row"):
            summary_values = [
                gr.Textbox(
                    label=label,
                    value=initial_payload.summary_values[index],
                    interactive=False,
                    elem_classes="student-dashboard-summary",
                )
                for index, label in enumerate(
                    ("可参加考试数", "已提交数", "可查看结果数")
                )
            ]
        message = gr.Markdown(initial_payload.message)
        gr.Markdown("成绩状态由结果服务提供；待复核内容不会显示为最终成绩。")

        exam_records = gr.State([])
        selected_exam_id = gr.Textbox(visible=False, container=False)
        result_records = gr.State([])
        selected_result_id = gr.Textbox(visible=False, container=False)

        with gr.Row(
            equal_height=False,
            elem_classes="student-dashboard-main",
        ):
            with gr.Column(
                scale=2,
                min_width=0,
                elem_classes="student-dashboard-exams",
            ):
                gr.Markdown("### 可参加考试")
                available_exams_table = gr.Dataframe(
                    headers=list(STUDENT_DASHBOARD_EXAM_HEADERS),
                    datatype=["str", "str", "str", "str", "markdown", "str"],
                    value=initial_payload.available_exam_rows,
                    interactive=False,
                    label="当前学生可参加的考试",
                    **table_options(STUDENT_DASHBOARD_EXAM_HEADERS),
                )
                exam_empty = gr.Markdown(
                    initial_payload.exam_empty,
                    elem_classes="student-dashboard-empty",
                )
                gr.Markdown("选择一行后使用“开始/继续”进入我的考试。")
            with gr.Column(
                scale=1,
                min_width=0,
                elem_classes="student-dashboard-results",
            ):
                gr.Markdown("### 最近结果")
                recent_results_table = gr.Dataframe(
                    headers=list(STUDENT_DASHBOARD_RESULT_HEADERS),
                    datatype=["str", "str", "str", "markdown", "str"],
                    value=initial_payload.recent_result_rows,
                    interactive=False,
                    label="当前学生最近结果",
                    **table_options(STUDENT_DASHBOARD_RESULT_HEADERS),
                )
                result_empty = gr.Markdown(
                    initial_payload.result_empty,
                    elem_classes="student-dashboard-empty",
                )
                gr.Markdown("选择一项结果后使用“查看结果”进入成绩与诊断。")
                gr.Markdown("### 已确认诊断摘要")
                diagnosis = gr.Markdown(initial_payload.diagnosis)

        def select_exam(
            event: gr.SelectData,
            records: Sequence[Mapping[str, Any]] | None,
        ) -> tuple[str, dict[str, Any]]:
            """把表格选行转换为当前学生考试入口标识。"""

            if not event or not getattr(event, "selected", False):
                return "", gr.update(interactive=False)
            index = getattr(event, "index", None)
            index = index[0] if isinstance(index, (list, tuple)) else index
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or not isinstance(records, Sequence)
                or isinstance(records, (str, bytes))
                or not 0 <= index < len(records)
                or not isinstance(records[index], Mapping)
            ):
                return "", gr.update(interactive=False)
            exam_id = str(
                records[index].get("exam_id") or records[index].get("id") or ""
            ).strip()
            return exam_id, gr.update(interactive=bool(exam_id))

        def select_result(
            event: gr.SelectData,
            records: Sequence[Mapping[str, Any]] | None,
        ) -> tuple[str, dict[str, Any]]:
            """把结果表选行转换为当前学生结果入口标识。"""

            if not event or not getattr(event, "selected", False):
                return "", gr.update(interactive=False)
            index = getattr(event, "index", None)
            index = index[0] if isinstance(index, (list, tuple)) else index
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or not isinstance(records, Sequence)
                or isinstance(records, (str, bytes))
                or not 0 <= index < len(records)
                or not isinstance(records[index], Mapping)
            ):
                return "", gr.update(interactive=False)
            result_id = str(
                records[index].get("id")
                or records[index].get("submission_id")
                or records[index].get("exam_id")
                or ""
            ).strip()
            return result_id, gr.update(interactive=bool(result_id))

        def refresh_panel(
            current_state: Mapping[str, Any],
        ) -> dict[Any, Any]:
            """刷新概览；成绩和诊断服务未就绪时继续显示明确空态。"""

            return _student_dashboard_component_updates(
                view,
                refresh_student_dashboard(current_state),
            )

        view = StudentDashboardView(
            panel=panel,
            refresh_button=refresh_button,
            summary_values=tuple(summary_values),
            available_exams_table=available_exams_table,
            exam_records=exam_records,
            selected_exam_id=selected_exam_id,
            continue_button=continue_button,
            exam_empty=exam_empty,
            recent_results_table=recent_results_table,
            result_records=result_records,
            selected_result_id=selected_result_id,
            view_results_button=view_results_button,
            result_empty=result_empty,
            diagnosis=diagnosis,
            message=message,
        )

        refresh_outputs = [
            view.refresh_button,
            *view.summary_values,
            view.available_exams_table,
            view.exam_records,
            view.selected_exam_id,
            view.continue_button,
            view.exam_empty,
            view.recent_results_table,
            view.result_records,
            view.selected_result_id,
            view.view_results_button,
            view.result_empty,
            view.diagnosis,
            view.message,
        ]
        refresh_button.click(
            refresh_panel,
            inputs=[state],
            outputs=refresh_outputs,
            show_progress="hidden",
        )
        available_exams_table.select(
            select_exam,
            inputs=[exam_records],
            outputs=[selected_exam_id, continue_button],
            show_progress="hidden",
        )
        recent_results_table.select(
            select_result,
            inputs=[result_records],
            outputs=[selected_result_id, view_results_button],
            show_progress="hidden",
        )
    return view


create_student_dashboard_view = _create_student_dashboard_view


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
        navigation_state = gr.State(
            {
                "role": "",
                "pages": {},
                "current_page": None,
                "history": [],
                "messages": [],
            }
        )
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
                    page_search = gr.Textbox(
                        label="当前页搜索",
                        placeholder="搜索当前页",
                        show_label=False,
                        container=False,
                        elem_id="edu-search",
                        interactive=True,
                    )
                    message_button = gr.Button("消息", elem_id="edu-message-button")
                    with gr.Accordion("用户菜单", open=False, elem_id="edu-user-menu"):
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
                        with gr.Row(elem_id="edu-page-header"):
                            page_breadcrumb = gr.HTML(elem_id="edu-page-breadcrumb")
                            with gr.Row(elem_id="edu-page-actions"):
                                back_button = gr.Button(
                                    "返回",
                                    elem_id="edu-page-back",
                                    visible=False,
                                    interactive=False,
                                )
                                primary_action_one = gr.Button(
                                    visible=False,
                                    elem_classes=["edu-primary-action"],
                                )
                                primary_action_two = gr.Button(
                                    visible=False,
                                    elem_classes=["edu-primary-action"],
                                )
                        page_search_feedback = gr.HTML(elem_id="edu-search-feedback")
                        with gr.Accordion(
                            "消息与待办",
                            open=False,
                            elem_id="edu-message-panel",
                        ) as message_panel:
                            message_empty = gr.Markdown(
                                empty_state("暂无本次会话消息。"), visible=True
                            )
                            message_table = gr.Dataframe(
                                headers=list(TOPBAR_MESSAGE_HEADERS),
                                datatype=["str"] * len(TOPBAR_MESSAGE_HEADERS),
                                value=[],
                                interactive=False,
                                label="当前用户可见的消息和待办",
                                **table_options(TOPBAR_MESSAGE_HEADERS),
                            )
                            message_view_button = gr.Button(
                                "查看所选消息",
                                variant="secondary",
                                interactive=False,
                                elem_id="edu-message-view",
                            )
                        message_selected = gr.State(None)
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
                        student_dashboard_view: StudentDashboardView = (
                            create_student_dashboard_view(session_state)
                        )
                        student_exam_selection = _find_panel_component(
                            student_exam_view.panel,
                            gr.Textbox,
                            visible=False,
                        )
                        student_exam_ids = _find_panel_component(
                            student_exam_view.panel,
                            gr.State,
                            occurrence=0,
                        )
                        student_results_exam = _find_panel_component(
                            results_view.panel,
                            gr.Dropdown,
                            label="考试",
                        )

        # 搜索输入只读取当前页面已经加载到组件中的数据，不创建跨页面或跨用户查询。
        search_sources: tuple[tuple[str, Any], ...] = (
            ("teacher.home", teacher_dashboard_view.course_records),
            ("teacher.courses", knowledge_base_view.courses_table),
            ("teacher.knowledge", knowledge_base_view.courses_table),
            ("teacher.questions", question_view.questions_table),
            ("teacher.exams", exam_view.exams_table),
            ("student.home", student_dashboard_view.exam_records),
            ("student.home", student_dashboard_view.result_records),
            ("student.exams", student_exam_view.exams_table),
            ("student.results", results_view.results_table),
            ("teacher.analytics", teacher_results_view.results_table),
            ("admin.users", admin_view.users_table),
        )
        search_source_components = [component for _, component in search_sources]

        panels = {
            "teacher.home": teacher_dashboard_view.panel,
            "teacher.courses": knowledge_base_view.panel,
            "teacher.questions": question_view.panel,
            "teacher.exams": exam_view.panel,
            "teacher.generate": question_generation_view.panel,
            "teacher.review": review_view.panel,
            "student.home": student_dashboard_view.panel,
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

        def transition_navigation(
            navigation: Mapping[str, Any] | None,
            role: str,
            selected: str | None,
            *,
            push_history: bool = True,
        ) -> dict[str, Any]:
            """更新导航并保留会话内返回栈，页面组件本身继续保留筛选值。"""

            next_nav = deepcopy(dict(navigation or {}))
            next_nav.setdefault("pages", {})
            next_nav.setdefault("history", [])
            next_nav.setdefault("messages", [])
            next_nav.setdefault("searches", {})
            previous_role = str(next_nav.get("role") or "")
            previous_page = next_nav.get("current_page") or next_nav["pages"].get(
                previous_role
            )
            if (
                push_history
                and previous_role == role
                and previous_page
                and previous_page != selected
            ):
                next_nav["history"] = [
                    *list(next_nav.get("history", [])),
                    {"role": role, "page": previous_page},
                ][-12:]
            next_nav["role"] = role
            next_nav["current_page"] = selected
            if selected:
                next_nav["pages"][role] = selected
            return next_nav

        def back_target(
            state: Mapping[str, Any], navigation: Mapping[str, Any]
        ) -> str | None:
            """从返回栈中取出当前角色仍有权限访问的页面。"""

            role = str(navigation.get("role") or "")
            for entry in reversed(list(navigation.get("history", []))):
                if not isinstance(entry, Mapping) or str(entry.get("role")) != role:
                    continue
                candidate = entry.get("page")
                if (
                    isinstance(candidate, str)
                    and is_authorized_navigation(candidate, [role])
                    and _can_navigate(candidate, state)
                ):
                    return candidate
            return None

        def render_workspace(
            state: LoginState, nav: dict[str, Any], selected: str | None
        ) -> dict[Any, Any]:
            """单次更新所有导航和面板，保持当前角色只展示一页。"""

            role = nav.get("role", "")
            allowed = _can_navigate(selected, state) and is_authorized_navigation(
                selected, [role]
            )
            item = navigation_item(selected) if allowed else None
            messages = list(nav.get("messages", []))
            search_value = str(nav.get("searches", {}).get(selected or "", ""))
            actions = PAGE_PRIMARY_ACTIONS.get(selected or "", ()) if allowed else ()
            previous_page = back_target(state, nav) if allowed else None
            result: dict[Any, Any] = {
                session_state: state,
                navigation_state: nav,
                login_panel: gr.update(visible=False),
                workspace: gr.update(visible=True),
                user_summary: _role_summary(state),
                context: f"<span>{item.label if item else '工作台'}</span>",
                page_search: gr.update(
                    value=search_value,
                    placeholder=PAGE_SEARCH_PLACEHOLDERS.get(
                        selected or "", "搜索当前页"
                    ),
                    interactive=bool(allowed),
                ),
                page_breadcrumb: _page_breadcrumb(selected, item),
                back_button: gr.update(
                    visible=previous_page is not None,
                    interactive=previous_page is not None,
                ),
                primary_action_one: gr.update(
                    value=actions[0][0] if len(actions) > 0 else "",
                    visible=len(actions) > 0,
                    interactive=(
                        len(actions) > 0
                        and is_authorized_navigation(actions[0][1], [role])
                    ),
                ),
                primary_action_two: gr.update(
                    value=actions[1][0] if len(actions) > 1 else "",
                    visible=len(actions) > 1,
                    interactive=(
                        len(actions) > 1
                        and is_authorized_navigation(actions[1][1], [role])
                    ),
                ),
                page_search_feedback: "",
                message_button: _message_button_label(messages),
                message_table: _message_rows(messages, state),
                message_empty: gr.update(
                    value=empty_state("暂无本次会话消息。"), visible=not messages
                ),
                message_view_button: gr.update(interactive=False),
                message_selected: None,
                message_panel: gr.update(open=False),
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
                result[page_breadcrumb] = ""
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
                    page_search: gr.update(
                        value="", placeholder="搜索当前页", interactive=False
                    ),
                    page_search_feedback: "",
                    page_breadcrumb: "",
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
            result.update(
                _student_dashboard_component_updates(
                    student_dashboard_view,
                    _student_dashboard_unavailable_payload(),
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
            initial_navigation: dict[str, Any] = {
                "role": role,
                "pages": {role: selected} if role else {},
                "current_page": selected,
                "history": [],
                "messages": [],
                "searches": {},
            }
            _record_session_message(
                initial_navigation,
                kind="登录反馈",
                related_object=state.get("username") or "当前账号",
                status="已登录",
                view_key=selected,
                detail="本次会话已建立。",
            )
            result.update(render_workspace(state, initial_navigation, selected))
            if UserRole.TEACHER.value in state["roles"] and selected == "teacher.home":
                result.update(
                    _teacher_dashboard_component_updates(
                        teacher_dashboard_view,
                        refresh_teacher_dashboard(state=state),
                    )
                )
            if UserRole.STUDENT.value in state["roles"] and selected == "student.home":
                result.update(
                    _student_dashboard_component_updates(
                        student_dashboard_view,
                        refresh_student_dashboard(state=state),
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
            push_history: bool = True,
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
            if role is not None:
                selected = (
                    nav.get("pages", {}).get(role)
                    or navigation_for_roles([role])[0].key
                )
            if not is_authorized_navigation(selected, [active_role]):
                return {
                    workspace_message: feedback("当前账号无权访问该页面。", "error")
                }
            next_nav = transition_navigation(
                nav,
                active_role,
                selected,
                push_history=push_history and role is None,
            )
            if selected != nav.get("current_page") or role is not None:
                item = navigation_item(selected)
                _record_session_message(
                    next_nav,
                    kind="页面反馈",
                    related_object=item.label if item else "当前页面",
                    status="已打开",
                    view_key=selected,
                    detail="已恢复当前页面的筛选和选中状态。",
                )
            result = render_workspace(current, next_nav, selected)
            if active_role == UserRole.TEACHER.value and selected == "teacher.home":
                result.update(
                    _teacher_dashboard_component_updates(
                        teacher_dashboard_view,
                        refresh_teacher_dashboard(state=current),
                    )
                )
            if active_role == UserRole.STUDENT.value and selected == "student.home":
                result.update(
                    _student_dashboard_component_updates(
                        student_dashboard_view,
                        refresh_student_dashboard(state=current),
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
                    page_search,
                    page_search_feedback,
                    page_breadcrumb,
                    back_button,
                    primary_action_one,
                    primary_action_two,
                    message_button,
                    message_panel,
                    message_empty,
                    message_table,
                    message_view_button,
                    message_selected,
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
                    student_dashboard_view.refresh_button,
                    student_dashboard_view.continue_button,
                    student_dashboard_view.view_results_button,
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

        def search_current_page(
            query: str | None,
            current_state: LoginState,
            nav: dict[str, Any],
            *loaded_values: Any,
        ) -> dict[Any, Any]:
            """只在当前页面已加载的行中搜索，不绕过页面服务重新查库。"""

            selected = nav.get("current_page")
            if not _can_navigate(selected, current_state):
                return {
                    page_search_feedback: "",
                    page_search: gr.update(interactive=False),
                }
            normalized = str(query or "").strip()
            next_nav = deepcopy(nav)
            next_nav.setdefault("searches", {})[selected] = normalized
            if not normalized:
                return {
                    navigation_state: next_nav,
                    page_search_feedback: "",
                }

            matches: list[tuple[str, str]] = []
            for (page_key, _), value in zip(
                search_sources, loaded_values, strict=False
            ):
                if page_key != selected:
                    continue
                for index, row_text in _searchable_rows(value):
                    if normalized.casefold() in row_text.casefold():
                        matches.append((index, row_text))
            if not matches:
                detail = "当前页面暂无匹配的已加载内容。"
            else:
                lines = [
                    f"- 第 {index} 行：{escape(row_text[:240])}"
                    for index, row_text in matches[:20]
                ]
                suffix = "" if len(matches) <= 20 else "\n- 其余匹配项未展开。"
                detail = (
                    f"当前页面已加载内容中匹配 {len(matches)} 项：\n"
                    + "\n".join(lines)
                    + suffix
                )
            return {
                navigation_state: next_nav,
                page_search_feedback: feedback(
                    f"搜索词：{escape(normalized)}\n\n{detail}",
                    "info" if matches else "empty",
                ),
            }

        def open_message_panel(
            current_state: LoginState,
            nav: dict[str, Any],
            teacher_todo_value: Any,
            teacher_result_value: Any,
            student_result_value: Any,
        ) -> dict[Any, Any]:
            """展开当前会话消息，行内容只来自已加载的会话反馈。"""

            next_nav = deepcopy(nav)
            messages = list(next_nav.get("messages", []))
            loaded_messages = _loaded_todo_messages(
                current_state,
                teacher_todo_value,
                teacher_result_value,
                student_result_value,
            )
            existing_keys = {
                (
                    item.get("kind"),
                    item.get("related_object"),
                    item.get("view_key"),
                )
                for item in messages
                if isinstance(item, Mapping)
            }
            for message in loaded_messages:
                key = (
                    message.get("kind"),
                    message.get("related_object"),
                    message.get("view_key"),
                )
                if key not in existing_keys:
                    messages.append(message)
                    existing_keys.add(key)
            next_nav["messages"] = messages[-20:]
            return {
                navigation_state: next_nav,
                message_panel: gr.update(open=True),
                message_button: _message_button_label(next_nav["messages"]),
                message_table: _message_rows(next_nav["messages"], current_state),
                message_empty: gr.update(
                    value=empty_state("暂无本次会话消息。"),
                    visible=not next_nav["messages"],
                ),
            }

        def select_message(
            event: gr.SelectData,
            messages_or_navigation: (
                Sequence[Mapping[str, Any]] | Mapping[str, Any] | None
            ),
        ) -> tuple[Any, Any]:
            """选中一条会话消息，仅启用经过索引校验的查看按钮。"""

            messages: Any = messages_or_navigation
            if isinstance(messages_or_navigation, Mapping):
                messages = messages_or_navigation.get("messages", [])
            index_value = (
                event.index[0] if isinstance(event.index, tuple) else event.index
            )
            if not isinstance(index_value, int) or not isinstance(messages, Sequence):
                return None, gr.update(interactive=False)
            valid = 0 <= index_value < len(messages)
            return (
                index_value if valid else None,
                gr.update(interactive=valid),
            )

        def view_message(
            selected_index: int | None,
            nav: dict[str, Any],
            current_state: LoginState,
        ) -> dict[Any, Any]:
            """查看消息关联页面，重新执行角色和页面权限校验。"""

            messages = list(nav.get("messages", []))
            if (
                not isinstance(selected_index, int)
                or not 0 <= selected_index < len(messages)
                or not isinstance(messages[selected_index], Mapping)
            ):
                return {
                    workspace_message: feedback("暂无可查看的消息。", "info"),
                    message_view_button: gr.update(interactive=False),
                }
            target = messages[selected_index].get("view_key")
            if not isinstance(target, str) or not _can_navigate(target, current_state):
                return {
                    workspace_message: feedback(
                        "该消息没有当前账号可访问的查看入口。", "warning"
                    ),
                    message_view_button: gr.update(interactive=False),
                }
            return navigate(current_state, nav, selected=target)

        def run_primary_action(
            current_state: LoginState,
            nav: dict[str, Any],
            *,
            slot: int,
        ) -> dict[Any, Any]:
            """执行标题行快捷入口，只转到已有授权页面。"""

            selected = nav.get("current_page")
            actions = PAGE_PRIMARY_ACTIONS.get(selected or "", ())
            if not isinstance(slot, int) or not 0 <= slot < len(actions):
                return {workspace_message: feedback("当前页面暂无可用主操作。", "info")}
            target = actions[slot][1]
            if not is_authorized_navigation(target, current_state.get("roles", [])):
                return {
                    workspace_message: feedback(
                        "当前账号无权访问该操作入口。", "warning"
                    )
                }
            return navigate(current_state, nav, selected=target)

        def go_back(current_state: LoginState, nav: dict[str, Any]) -> dict[Any, Any]:
            """返回上一个仍可访问的页面，页面组件状态由 Gradio 会话保留。"""

            next_nav = deepcopy(nav)
            target = None
            history = list(next_nav.get("history", []))
            role = str(next_nav.get("role") or "")
            while history:
                entry = history.pop()
                if not isinstance(entry, Mapping) or str(entry.get("role")) != role:
                    continue
                candidate = entry.get("page")
                if (
                    isinstance(candidate, str)
                    and is_authorized_navigation(candidate, [role])
                    and _can_navigate(candidate, current_state)
                ):
                    target = candidate
                    break
            if target is None:
                return {
                    back_button: gr.update(visible=False, interactive=False),
                    workspace_message: feedback("暂无可返回的页面。", "info"),
                }
            next_nav["history"] = history
            return navigate(
                current_state,
                next_nav,
                selected=target,
                push_history=False,
            )

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

            next_nav = transition_navigation(
                nav, UserRole.TEACHER.value, "teacher.review"
            )
            _record_session_message(
                next_nav,
                kind="复核入口",
                related_object="考试 / 答卷 / 题目",
                status="已打开",
                view_key="teacher.review",
                detail="已携带考试、答卷和题目上下文。",
            )
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

            next_nav = transition_navigation(
                nav, UserRole.TEACHER.value, "teacher.courses"
            )
            _record_session_message(
                next_nav,
                kind="快捷入口",
                related_object=course.name,
                status="已打开",
                view_key="teacher.courses",
                detail="已恢复课程管理页面。",
            )
            result = render_workspace(current, next_nav, "teacher.courses")
            result[knowledge_base_view.message] = feedback(
                f"已进入课程“{course.name}”。", "info"
            )
            return result

        def open_student_exam_from_dashboard(
            exam_records: Sequence[Mapping[str, Any]] | None,
            selected_exam_id: str | None,
            current_state: LoginState,
            nav: dict[str, Any],
        ) -> dict[Any, Any]:
            """从学生概览进入 T108，并再次校验考试对当前学生开放。"""

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
            if UserRole.STUDENT not in normalize_ui_roles(current["roles"]):
                return {
                    student_dashboard_view.message: feedback(
                        "当前账号无权访问学生考试功能。", "error"
                    )
                }
            record = _student_dashboard_record_by_id(
                exam_records,
                selected_exam_id,
                id_names=("exam_id", "id"),
            )
            if record is None:
                return {
                    student_dashboard_view.message: feedback(
                        "当前考试入口已失效，请刷新概览后重新选择。", "info"
                    )
                }
            exam_id = str(
                _student_dashboard_first_value(record, ("exam_id", "id"), "")
            ).strip()
            try:
                with get_session_factory()() as session:
                    exam = SubmissionService(session).get_available_exam(
                        exam_id,
                        student_id=current["user_id"],
                    )
            except (
                SubmissionServiceError,
                PermissionDeniedError,
                SQLAlchemyError,
                TypeError,
                ValueError,
                RuntimeError,
            ) as error:
                return {
                    student_dashboard_view.message: feedback(
                        _student_dashboard_error_message(error),
                        "error",
                    )
                }

            next_nav = transition_navigation(
                nav, UserRole.STUDENT.value, "student.exams"
            )
            _record_session_message(
                next_nav,
                kind="考试入口",
                related_object=exam.title,
                status="已打开",
                view_key="student.exams",
                detail="已定位当前学生可参加的考试。",
            )
            result = render_workspace(current, next_nav, "student.exams")
            exam_rows, _ = refresh_student_exams(current)
            result[student_exam_view.exams_table] = exam_rows
            if student_exam_ids is not None:
                result[student_exam_ids] = [
                    str(item.get("exam_id") or item.get("id") or "")
                    for item in exam_records or ()
                    if isinstance(item, Mapping)
                    and str(item.get("exam_id") or item.get("id") or "").strip()
                ]
            if student_exam_selection is not None:
                result[student_exam_selection] = exam_id
            result[student_exam_view.message] = feedback(
                f"已定位考试“{exam.title}”，请在列表中选中后点击“开始或继续”。",
                "info",
            )
            return result

        def open_student_result_from_dashboard(
            result_records: Sequence[Mapping[str, Any]] | None,
            selected_result_id: str | None,
            current_state: LoginState,
            nav: dict[str, Any],
        ) -> dict[Any, Any]:
            """从学生概览进入 T111，并按当前学生校验答卷归属。"""

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
            if UserRole.STUDENT not in normalize_ui_roles(current["roles"]):
                return {
                    student_dashboard_view.message: feedback(
                        "当前账号无权访问成绩与诊断功能。", "error"
                    )
                }
            record = _student_dashboard_record_by_id(
                result_records,
                selected_result_id,
                id_names=("id", "submission_id", "exam_id"),
            )
            if record is None:
                return {
                    student_dashboard_view.message: feedback(
                        "当前结果入口已失效，请刷新概览后重新选择。", "info"
                    )
                }
            submission_id = str(
                _student_dashboard_first_value(
                    record,
                    ("submission_id",),
                    "",
                )
                or ""
            ).strip()
            exam_id = str(
                _student_dashboard_first_value(record, ("exam_id",), "") or ""
            ).strip()
            if not submission_id and not exam_id:
                return {
                    student_dashboard_view.message: feedback(
                        "当前结果缺少可校验的答卷信息，暂不能打开。", "warning"
                    )
                }
            try:
                with get_session_factory()() as session:
                    service = SubmissionService(session)
                    if submission_id:
                        service.get_submission(
                            submission_id,
                            student_id=current["user_id"],
                        )
                    else:
                        submission = service.find_submission(
                            exam_id,
                            current["user_id"],
                        )
                        if submission is None:
                            raise SubmissionServiceError(
                                "当前学生暂无该考试的答卷结果。"
                            )
            except (
                SubmissionServiceError,
                PermissionDeniedError,
                SQLAlchemyError,
                TypeError,
                ValueError,
                RuntimeError,
            ) as error:
                return {
                    student_dashboard_view.message: feedback(
                        _student_dashboard_error_message(error),
                        "error",
                    )
                }

            next_nav = transition_navigation(
                nav, UserRole.STUDENT.value, "student.results"
            )
            _record_session_message(
                next_nav,
                kind="成绩入口",
                related_object="当前学生成绩与诊断",
                status="已打开",
                view_key="student.results",
                detail="已按当前学生答卷归属打开结果页。",
            )
            result = render_workspace(current, next_nav, "student.results")
            result_rows, result_message = refresh_student_results(
                exam_id or None,
                current,
            )
            result[results_view.results_table] = result_rows
            result[results_view.message] = result_message
            if student_results_exam is not None and exam_id:
                result[student_results_exam] = gr.update(
                    value=exam_id,
                    choices=[
                        (
                            str(
                                _student_dashboard_first_value(
                                    record,
                                    ("exam_name", "exam_title", "title"),
                                    "当前考试",
                                )
                            ),
                            exam_id,
                        )
                    ],
                )
            if not _student_dashboard_is_final_status(
                _student_dashboard_first_value(
                    record,
                    ("result_status", "status"),
                    None,
                )
            ):
                result[results_view.message] = feedback(
                    "已进入成绩与诊断；当前结果尚未形成最终成绩。",
                    "warning",
                )
            return result

        login_button.click(sign_in, inputs=[identifier, password], **event_options)
        password.submit(sign_in, inputs=[identifier, password], **event_options)
        logout_button.click(clear_workspace, inputs=[], **event_options)
        message_button.click(
            open_message_panel,
            inputs=[
                session_state,
                navigation_state,
                teacher_dashboard_view.todo_table,
                teacher_results_view.results_table,
                student_dashboard_view.result_records,
            ],
            outputs=[
                navigation_state,
                message_panel,
                message_button,
                message_table,
                message_empty,
            ],
            show_progress="hidden",
            queue=False,
        )
        message_table.select(
            select_message,
            inputs=[navigation_state],
            outputs=[message_selected, message_view_button],
            show_progress="hidden",
        )
        message_view_button.click(
            view_message,
            inputs=[message_selected, navigation_state, session_state],
            outputs=outputs,
            show_progress="minimal",
            concurrency_id="eduagent-ui",
            concurrency_limit=1,
        )
        back_button.click(
            go_back,
            inputs=[session_state, navigation_state],
            **event_options,
        )
        primary_action_one.click(
            partial(run_primary_action, slot=0),
            inputs=[session_state, navigation_state],
            **event_options,
        )
        primary_action_two.click(
            partial(run_primary_action, slot=1),
            inputs=[session_state, navigation_state],
            **event_options,
        )
        search_inputs = [
            page_search,
            session_state,
            navigation_state,
            *search_source_components,
        ]
        page_search.input(search_current_page, inputs=search_inputs, **event_options)
        page_search.submit(search_current_page, inputs=search_inputs, **event_options)
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
        student_dashboard_view.continue_button.click(
            open_student_exam_from_dashboard,
            inputs=[
                student_dashboard_view.exam_records,
                student_dashboard_view.selected_exam_id,
                session_state,
                navigation_state,
            ],
            outputs=outputs,
            show_progress="minimal",
            concurrency_id="eduagent-ui",
            concurrency_limit=1,
        )
        student_dashboard_view.view_results_button.click(
            open_student_result_from_dashboard,
            inputs=[
                student_dashboard_view.result_records,
                student_dashboard_view.selected_result_id,
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
    "STUDENT_DASHBOARD_DIAGNOSIS_EMPTY_MESSAGE",
    "STUDENT_DASHBOARD_EXAM_EMPTY_MESSAGE",
    "STUDENT_DASHBOARD_EXAM_HEADERS",
    "STUDENT_DASHBOARD_RESULT_EMPTY_MESSAGE",
    "STUDENT_DASHBOARD_RESULT_HEADERS",
    "STUDENT_DASHBOARD_SUMMARY_UNAVAILABLE",
    "STUDENT_DASHBOARD_UNAVAILABLE_MESSAGE",
    "TEACHER_DASHBOARD_COURSE_EMPTY_MESSAGE",
    "TEACHER_DASHBOARD_EXAM_EMPTY_MESSAGE",
    "TEACHER_DASHBOARD_GRADE_EMPTY_MESSAGE",
    "TEACHER_DASHBOARD_SUMMARY_UNAVAILABLE",
    "TEACHER_DASHBOARD_TODO_EMPTY_MESSAGE",
    "TEACHER_DASHBOARD_UNAVAILABLE_MESSAGE",
    "LoginState",
    "NavigationItem",
    "StudentDashboardPayload",
    "StudentDashboardView",
    "TeacherDashboardPayload",
    "TeacherDashboardView",
    "create_gradio_app",
    "create_student_dashboard_view",
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
    "refresh_student_dashboard",
    "refresh_teacher_dashboard",
    "select_navigation",
    "select_navigation_for_app",
    "select_navigation_for_app_with_questions",
    "select_navigation_for_app_with_questions_and_exams",
    "select_navigation_for_app_with_questions_and_exams_and_student",
]
