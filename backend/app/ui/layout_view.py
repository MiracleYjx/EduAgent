"""EduAgent 共享工作台布局、角色菜单和响应式样式。"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum, StrEnum
from html import escape
from math import isfinite
from types import MappingProxyType
from typing import Any, Literal

import gradio as gr

from backend.app.domain.enums import (
    AnswerStatus,
    DocumentStatus,
    ExamStatus,
    GradingMode,
    GradingStatus,
    QuestionStatus,
    QuestionType,
    ReviewStatus,
    SubmissionStatus,
    UserRole,
    ValidationStatus,
    WorkflowStatus,
)

StatusTone = Literal["neutral", "info", "success", "warning", "danger"]
StatusEntity = Literal[
    "ui",
    "question",
    "question_type",
    "document",
    "exam",
    "submission",
    "answer",
    "grading",
    "grading_mode",
    "review",
    "validation",
    "workflow",
    "role",
]


class UiStatus(StrEnum):
    """仅用于显示的状态，不参与业务状态流转。"""

    UNANSWERED = "unanswered"
    ANSWERED = "answered"
    MARKED = "marked"
    CURRENT = "current"
    PENDING_GRADING = "pending_grading"
    LOW_CONFIDENCE = "low_confidence"
    PENDING_REVIEW = "pending_review"
    COMPLETED = "completed"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    ACTIVE = "active"
    INACTIVE = "inactive"
    LOADING = "loading"
    EMPTY = "empty"
    INFO = "info"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"


# 前景、背景、边框均为固定显示规范；调用方不能原地修改全局颜色。
STATUS_COLORS: Mapping[StatusTone, tuple[str, str, str]] = MappingProxyType(
    {
        "neutral": ("#64748B", "#F8FAFC", "#64748B"),
        "info": ("#2563EB", "#EFF6FF", "#2563EB"),
        "success": ("#15803D", "#F0FDF4", "#15803D"),
        "warning": ("#B45309", "#FFFBEB", "#B45309"),
        "danger": ("#B91C1C", "#FEF2F2", "#B91C1C"),
    }
)


@dataclass(frozen=True)
class StatusPresentation:
    """稳定的只读状态描述，可供 HTML、表格或原生按钮共用。"""

    value: str
    label: str
    tone: StatusTone
    icon: str

    @property
    def color(self) -> str:
        """返回规范前景色。"""

        return STATUS_COLORS[self.tone][0]


_ENUM_TYPES: Mapping[str, type[StrEnum]] = MappingProxyType(
    {
        "ui": UiStatus,
        "question": QuestionStatus,
        "question_type": QuestionType,
        "document": DocumentStatus,
        "exam": ExamStatus,
        "submission": SubmissionStatus,
        "answer": AnswerStatus,
        "grading": GradingStatus,
        "grading_mode": GradingMode,
        "review": ReviewStatus,
        "validation": ValidationStatus,
        "workflow": WorkflowStatus,
        "role": UserRole,
    }
)


def _status_map(
    *rows: tuple[str, str, StatusTone, str]
) -> Mapping[str, StatusPresentation]:
    return MappingProxyType(
        {
            value: StatusPresentation(value, label, tone, icon)
            for value, label, tone, icon in rows
        }
    )


# 必须先按实体取表，再读取枚举值，避免相同的 Pending Review 被误译。
_STATUS_MAPS: Mapping[str, Mapping[str, StatusPresentation]] = MappingProxyType(
    {
        "ui": _status_map(
            ("unanswered", "未答", "neutral", "○"),
            ("answered", "已答", "success", "✓"),
            ("marked", "已标记", "warning", "⚑"),
            ("current", "当前题", "info", "◎"),
            ("pending_grading", "待批阅", "warning", "◷"),
            ("low_confidence", "低置信度，待人工复核", "warning", "⚠"),
            ("pending_review", "待人工复核", "warning", "⚠"),
            ("completed", "已完成", "success", "✓"),
            ("confirmed", "已确认", "success", "✓"),
            ("failed", "处理失败", "danger", "✕"),
            ("loading", "正在加载", "info", "◷"),
            ("empty", "暂无数据", "neutral", "○"),
            ("info", "提示", "info", "i"),
            ("active", "启用", "success", "✓"),
            ("inactive", "停用", "neutral", "○"),
            ("healthy", "正常", "success", "✓"),
            ("unhealthy", "异常", "danger", "✕"),
        ),
        "question": _status_map(
            ("Draft", "草稿", "neutral", "▤"),
            ("Candidate Generation", "候选题", "warning", "▤"),
            ("Pending Review", "待教师审核", "warning", "◷"),
            ("Approved", "审核通过", "success", "✓"),
            ("Needs Revision", "待修订", "warning", "✎"),
            ("Published", "已发布", "success", "✓"),
        ),
        "question_type": _status_map(
            ("SINGLE_CHOICE", "单选题", "neutral", "○"),
            ("MULTIPLE_CHOICE", "多选题", "neutral", "▤"),
            ("TRUE_FALSE", "判断题", "neutral", "○"),
            ("FILL_BLANK", "填空题", "neutral", "▤"),
            ("SHORT_ANSWER", "简答题", "neutral", "▤"),
            ("ESSAY", "论述题", "neutral", "▤"),
        ),
        "document": _status_map(
            ("Uploaded", "已上传，待处理", "info", "↑"),
            ("Parsing", "正在解析", "info", "◷"),
            ("Chunking", "正在分块", "info", "◷"),
            ("Embedding", "正在向量化", "info", "◷"),
            ("Ready", "资料已就绪", "success", "✓"),
            ("Failed", "处理失败", "danger", "✕"),
        ),
        "exam": _status_map(
            ("Draft", "草稿", "neutral", "▤"),
            ("Published", "已发布", "success", "✓"),
            ("Closed", "已关闭", "neutral", "○"),
            ("Archived", "已归档", "neutral", "▤"),
        ),
        "submission": _status_map(
            ("Draft", "草稿", "neutral", "▤"),
            ("Submitted", "已提交，待批阅", "warning", "◷"),
            ("Graded", "已评分，尚未最终确认", "info", "✓"),
            ("Reviewed", "已复核", "success", "✓"),
        ),
        "answer": _status_map(
            ("Draft", "草稿", "neutral", "▤"),
            ("Submitted", "待批阅", "warning", "◷"),
            ("Grading", "正在批阅", "info", "◷"),
            ("Graded", "已评分，尚未最终确认", "info", "✓"),
            ("Failed", "批阅失败", "danger", "✕"),
        ),
        "grading": _status_map(
            ("Pending", "待批阅", "warning", "◷"),
            ("Validated", "校验通过", "info", "✓"),
            ("Accepted", "评分已接受", "info", "✓"),
            ("Pending Review", "待人工复核", "warning", "⚠"),
            ("Final", "最终评分", "success", "✓"),
            ("Failed", "评分失败", "danger", "✕"),
        ),
        "review": _status_map(
            ("Not Required", "无需复核", "neutral", "○"),
            ("Pending Review", "待人工复核", "warning", "⚠"),
            ("Confirmed", "已确认", "success", "✓"),
            ("Modified", "已修改", "info", "✎"),
            ("Re-grade", "待重新评分", "warning", "◷"),
            ("Final", "复核已完成", "success", "✓"),
        ),
        "validation": _status_map(
            ("Pending", "待校验", "warning", "◷"),
            ("Validated", "校验通过", "success", "✓"),
            ("Failed", "校验失败", "danger", "✕"),
        ),
        "workflow": _status_map(
            ("Queued", "排队中", "warning", "◷"),
            ("Running", "处理中", "info", "◷"),
            ("Paused", "已暂停", "warning", "‖"),
            ("Failed", "处理失败", "danger", "✕"),
            ("Completed", "已完成", "success", "✓"),
        ),
        "role": _status_map(
            ("Teacher", "教师", "neutral", "○"),
            ("Student", "学生", "neutral", "○"),
            ("Admin", "管理员", "neutral", "○"),
        ),
        "grading_mode": _status_map(
            ("Objective", "客观题评分", "info", "▤"),
            ("Subjective", "主观题评分", "info", "▤"),
        ),
    }
)
_UNKNOWN_STATUS = StatusPresentation("unknown", "状态未知", "neutral", "?")


def get_status(value: Enum | str | None, *, entity: StatusEntity) -> StatusPresentation:
    """按实体查询显示规范；接受枚举、枚举名称或原值，未知值一律回退。

    例如 get_status(QuestionStatus.PENDING_REVIEW, entity="question")。
    不读取答案，不判断保存成功，也不根据置信度推导业务状态。
    """

    enum_type = _ENUM_TYPES.get(entity)
    if enum_type is None or value is None:
        return _UNKNOWN_STATUS
    if isinstance(value, Enum):
        if not isinstance(value, enum_type):
            return _UNKNOWN_STATUS
        code = str(value.value)
    elif isinstance(value, str):
        raw = value.strip()
        member = enum_type.__members__.get(raw)
        code = str(member.value) if member is not None else raw
    else:
        return _UNKNOWN_STATUS
    return _STATUS_MAPS[entity].get(code, _UNKNOWN_STATUS)


def status_label(value: Enum | str | None, *, entity: StatusEntity) -> str:
    """返回中文纯文本，供表格、下拉框和业务回执使用。"""

    return get_status(value, entity=entity).label


def status_text(value: Enum | str | None, *, entity: StatusEntity) -> str:
    """返回图标加中文文本，适用于不支持 HTML 的表格单元格。"""

    item = get_status(value, entity=entity)
    return f"{item.icon} {item.label}"


def status_choices(
    values: Iterable[Enum | str], *, entity: StatusEntity, include_all: bool = False
) -> list[tuple[str, str]]:
    """显示中文但保留原始提交值；不会扩大调用方给定的选项集合。"""

    choices = [
        (
            status_label(value, entity=entity),
            str(value.value if isinstance(value, Enum) else value),
        )
        for value in values
    ]
    return [("全部", ""), *choices] if include_all else choices


def status_badge(value: Enum | str | None, *, entity: StatusEntity) -> str:
    """渲染只读徽标；原生操作按钮由调用视图独立绑定。"""

    item = get_status(value, entity=entity)
    return (
        f'<span class="edu-status edu-tone-{item.tone}" data-status="{escape(item.value)}">'
        f'<span aria-hidden="true">{item.icon}</span><span>{item.label}</span></span>'
    )


def status_banner(
    value: Enum | str | None, *, entity: StatusEntity, detail: str = ""
) -> str:
    """渲染状态横幅，动态说明按文本转义，失败状态使用警告语义。"""

    item = get_status(value, entity=entity)
    role = "alert" if item.tone == "danger" else "status"
    body = f'<div class="edu-status-detail">{escape(detail)}</div>' if detail else ""
    return (
        f'<div class="edu-state-banner edu-tone-{item.tone}" role="{role}">'
        f"{status_badge(value, entity=entity)}{body}</div>"
    )


def empty_state(message: str = "暂无数据。") -> str:
    """渲染空数据状态，不以零条记录推断操作失败。"""

    return status_banner(UiStatus.EMPTY, entity="ui", detail=message)


def loading_state(message: str = "正在加载，请稍候。") -> str:
    """渲染带忙碌语义的中文加载状态。"""

    return (
        '<div aria-busy="true">'
        + status_banner(UiStatus.LOADING, entity="ui", detail=message)
        + "</div>"
    )


def confidence_banner(
    confidence: float | None, *, low_confidence: bool, threshold: float | None = None
) -> str:
    """展示服务给定的分流结论及实际数值；不设默认阈值、不比较大小。"""

    def number(value: float | None) -> str:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "暂不可用"
        return str(value) if isfinite(value) and 0 <= value <= 1 else "暂不可用"

    detail = f"置信度：{number(confidence)}"
    if threshold is not None:
        detail += f"；服务阈值：{number(threshold)}"
    return status_banner(
        UiStatus.LOW_CONFIDENCE if low_confidence else UiStatus.INFO,
        entity="ui",
        detail=detail,
    )


def question_indicator(
    number: int, *, answered: bool = False, marked: bool = False, current: bool = False
) -> str:
    """渲染答题卡标识，三种状态独立叠加；answered 由调用方显式给定。

    判断题答案 False 也可以传 answered=True。此标识不代表答对或已保存。
    返回只读 HTML，不包含导航回调，避免把展示组件当作可点击按钮。
    """

    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise ValueError("题号必须为正整数。")
    state = UiStatus.ANSWERED if answered else UiStatus.UNANSWERED
    tone = get_status(state, entity="ui").tone
    current_class = " edu-question-current" if current else ""
    current_attr = ' aria-current="step"' if current else ""
    return (
        f'<span class="edu-question-indicator edu-tone-{tone}{current_class}"{current_attr}>'
        f'<span class="edu-question-number">{number}</span>'
        f'<span>{status_text(state, entity="ui")}</span>'
        f'<span class="edu-question-mark">{status_text(UiStatus.MARKED, entity="ui") if marked else ""}</span>'
        f'<span class="edu-question-position">{"当前题" if current else ""}</span></span>'
    )


@dataclass(frozen=True)
class LayoutNavigationItem:
    """共享外壳使用的角色导航项。"""

    key: str
    label: str
    group: str
    allowed_roles: frozenset[UserRole]
    description: str


ROLE_NAVIGATION: tuple[LayoutNavigationItem, ...] = (
    LayoutNavigationItem(
        "teacher.home",
        "工作台",
        "工作台",
        frozenset({UserRole.TEACHER}),
        "教师工作台暂不可用。",
    ),
    LayoutNavigationItem(
        "teacher.courses",
        "课程管理",
        "教学准备",
        frozenset({UserRole.TEACHER}),
        "课程与知识库元数据管理。",
    ),
    LayoutNavigationItem(
        "teacher.knowledge",
        "知识库",
        "教学准备",
        frozenset({UserRole.TEACHER}),
        "课程资料与处理状态。",
    ),
    LayoutNavigationItem(
        "teacher.questions",
        "题库",
        "教学准备",
        frozenset({UserRole.TEACHER}),
        "教师题库与审核。",
    ),
    LayoutNavigationItem(
        "teacher.exams",
        "考试",
        "教学准备",
        frozenset({UserRole.TEACHER}),
        "教师考试与组卷。",
    ),
    LayoutNavigationItem(
        "teacher.generate",
        "AI 出题",
        "AI 教学",
        frozenset({UserRole.TEACHER}),
        "AI 出题功能暂不可用。",
    ),
    LayoutNavigationItem(
        "teacher.review",
        "AI 阅卷",
        "AI 教学",
        frozenset({UserRole.TEACHER}),
        "AI 阅卷功能暂不可用。",
    ),
    LayoutNavigationItem(
        "teacher.analytics",
        "学情分析",
        "学情分析",
        frozenset({UserRole.TEACHER}),
        "学情分析功能暂不可用。",
    ),
    LayoutNavigationItem(
        "student.home",
        "学习概览",
        "学习概览",
        frozenset({UserRole.STUDENT}),
        "学习概览暂不可用。",
    ),
    LayoutNavigationItem(
        "student.exams",
        "我的考试",
        "我的考试",
        frozenset({UserRole.STUDENT}),
        "学生考试入口。",
    ),
    LayoutNavigationItem(
        "student.results",
        "成绩与诊断",
        "成绩与诊断",
        frozenset({UserRole.STUDENT}),
        "成绩与诊断功能暂不可用。",
    ),
    LayoutNavigationItem(
        "admin.home",
        "系统概览",
        "系统概览",
        frozenset({UserRole.ADMIN}),
        "系统概览暂不可用。",
    ),
    LayoutNavigationItem(
        "admin.users",
        "用户管理",
        "用户管理",
        frozenset({UserRole.ADMIN}),
        "管理员用户管理。",
    ),
    LayoutNavigationItem(
        "admin.roles",
        "角色管理",
        "角色管理",
        frozenset({UserRole.ADMIN}),
        "管理员角色管理。",
    ),
    LayoutNavigationItem(
        "admin.status",
        "运行状态",
        "运行状态",
        frozenset({UserRole.ADMIN}),
        "管理员运行状态。",
    ),
)


STATUS_CSS = (
    "\n".join(
        f".edu-tone-{tone} {{ --edu-status-fg: {colors[0]}; --edu-status-bg: {colors[1]}; --edu-status-border: {colors[2]}; }}"
        for tone, colors in STATUS_COLORS.items()
    )
    + """
.edu-status { display: inline-flex; align-items: center; gap: 6px; padding: 3px 8px;
    max-width: 100%; color: var(--edu-status-fg); background: var(--edu-status-bg);
    border: 1px solid var(--edu-status-border); border-radius: 4px; font-size: 13px;
    line-height: 20px; letter-spacing: 0; overflow-wrap: anywhere; }
.edu-status > [aria-hidden] { flex: 0 0 auto; }
.edu-state-banner { padding: 12px 16px; border-left: 3px solid var(--edu-status-border);
    background: var(--edu-status-bg); color: var(--edu-status-fg); }
.edu-state-banner .edu-status { padding: 0; border: 0; font-weight: 600; }
.edu-status-detail { margin-top: 6px; white-space: pre-wrap; overflow-wrap: anywhere; }
.edu-question-indicator { display: inline-flex; vertical-align: top; flex-direction: column;
    align-items: center; justify-content: center; width: 96px; height: 116px;
    box-sizing: border-box; border: 2px solid transparent; border-radius: 6px;
    color: var(--edu-status-fg); background: var(--edu-status-bg); margin: 4px;
    font-size: 12px; line-height: 18px; letter-spacing: 0; }
.edu-question-number { display: inline-flex; justify-content: center; align-items: center;
    min-width: 44px; height: 44px; border: 1px solid var(--edu-status-border); border-radius: 50%;
    font-size: 16px; font-weight: 600; background: #fff; }
.edu-question-mark { color: #B45309; min-height: 18px; }
.edu-question-position { color: #2563EB; min-height: 18px; }
.edu-question-current { border-color: #2563EB; }
.edu-confirmation { border-left: 3px solid #B91C1C; padding: 12px 16px; background: #FEF2F2; }
.edu-confirmation button { min-height: 44px; }
.edu-status-table { min-width: 0 !important; max-width: 100%; }
"""
)


def create_status_styles() -> gr.HTML:
    """独立 Blocks 中调用一次；共享工作台已包含这些样式，无需重复挂载。"""

    return gr.HTML(f"<style>{STATUS_CSS}</style>", elem_classes="edu-status-styles")


def table_options(headers: Sequence[str]) -> dict[str, Any]:
    """统一只读表格尺寸，长文本换行，横向滚动留在表格内部。"""

    widths = [
        (
            240
            if any(word in header for word in ("内容", "标题", "名称", "说明", "答案"))
            else (
                168
                if "状态" in header
                else 180 if "ID" in header or "时间" in header else 112
            )
        )
        for header in headers
    ]
    return {
        "wrap": True,
        "max_height": 420,
        "column_widths": widths,
        "elem_classes": ["edu-status-table"],
    }


WORKSPACE_CSS = STATUS_CSS + """
.gradio-container:has(#edu-root) { max-width: none !important; padding: 0 !important; overflow: visible !important; }
.gradio-container:has(#edu-root) .app { padding: 0 !important; }
#edu-root { --edu-blue: #2563eb; --edu-ink: #202735; --edu-line: #e1e5eb;
    gap: 0; background: #f5f6f8; color: var(--edu-ink); min-height: 100vh; }
#edu-root, #edu-root * { letter-spacing: 0; box-sizing: border-box; }
#edu-style { height: 0; min-height: 0; padding: 0; }
#edu-workspace { gap: 0; }
#edu-topbar { height: 64px; min-height: 64px; flex-wrap: nowrap; gap: 16px;
    position: sticky; top: 0; z-index: 20; padding: 0 24px; align-items: center;
    border-bottom: 1px solid var(--edu-line); background: #fff; }
#edu-brand { flex: 0 0 168px; min-width: 0; font-size: 20px; font-weight: 700; }
#edu-brand strong { color: #2563eb; }
#edu-context { flex: 1 1 0; min-width: 0; font-size: 14px; }
#edu-search { flex: 1 1 260px; min-width: 180px; }
#edu-search .wrap { min-height: 40px; border-color: var(--edu-line); }
#edu-search input { min-height: 38px; }
#edu-message-button { flex: 0 0 auto; min-width: 88px; height: 40px; }
#edu-user-menu { flex: 0 0 auto; min-width: 0; border: 0; background: transparent; }
#edu-user-menu > .label-wrap { min-height: 40px; padding: 0 10px; border: 1px solid var(--edu-line);
    border-radius: 4px; background: #fff; }
#edu-user-menu > [data-testid="accordion-content"] { min-width: 240px; padding: 12px;
    border: 1px solid var(--edu-line); background: #fff; box-shadow: 0 8px 20px rgba(32,39,53,.12); }
#edu-user { flex: 0 1 180px; min-width: 0; text-align: right; }
#edu-user span, #edu-context span { display: block; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis; }
#edu-role { flex: 0 0 112px; min-width: 0 !important; }
#edu-role input { min-width: 0 !important; }
#edu-logout { flex: 0 0 104px; min-width: 0; height: 44px; white-space: nowrap; }
#edu-shell-row { min-height: calc(100vh - 64px); gap: 0; flex-wrap: nowrap; align-items: stretch; }
#edu-sidebar { flex: 0 0 216px; width: 216px; min-width: 0 !important; padding: 16px 12px;
    border-right: 1px solid var(--edu-line); background: #fff; }
#edu-menu { border: 0; background: transparent; padding: 0 !important; overflow: visible !important; }
#edu-menu > .label-wrap { min-height: 44px; width: 100%; margin: 0; padding: 0 4px; align-items: center; }
#edu-sidebar .edu-nav-group { gap: 4px; min-width: 0 !important; flex-grow: 0 !important; }
#edu-sidebar .edu-group-title { color: #68717e; font-size: 12px; }
#edu-sidebar .edu-group-title.block { padding: 12px 10px 4px; }
#edu-sidebar .edu-nav { justify-content: flex-start; min-height: 44px; height: 44px;
    min-width: 0; width: 100%; border: 0; border-left: 3px solid transparent;
    border-radius: 4px; background: transparent; box-shadow: none; padding: 0 12px;
    color: #485160; font-size: 14px; text-align: left; }
#edu-sidebar .edu-nav:hover { background: #f3f5f8; }
#edu-sidebar .edu-nav-active { color: var(--edu-blue); border-left-color: var(--edu-blue);
    background: #edf3ff; font-weight: 600; }
#edu-root button:focus-visible { outline: 2px solid var(--edu-blue); outline-offset: 2px; }
#edu-root button.primary { background: #2563eb; border-color: #2563eb; color: #fff; }
#edu-root button.primary:hover { background: #1d4ed8; border-color: #1d4ed8; }
#edu-content { flex: 1 1 0; min-width: 0 !important; padding: 24px; gap: 18px; }
#edu-content > .column { min-width: 0 !important; }
#edu-content .prose h2 { font-size: 22px; }
#edu-content .prose h3 { font-size: 17px; }
#edu-content textarea { overflow-wrap: anywhere; }
#edu-page-header { min-height: 48px; align-items: center; gap: 12px; }
#edu-page-breadcrumb { flex: 1 1 auto; min-width: 0; color: #485160; font-size: 14px; }
#edu-page-breadcrumb .edu-breadcrumb { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#edu-page-actions { flex: 0 0 auto; min-width: 0; gap: 8px; justify-content: flex-end; }
#edu-page-actions button { min-height: 40px; white-space: nowrap; }
#edu-page-back { flex: 0 0 auto; }
#edu-message-panel { border: 1px solid var(--edu-line); border-radius: 6px; background: #fff; }
#edu-message-panel > .label-wrap { min-height: 42px; padding: 0 12px; }
#edu-message-panel > [data-testid="accordion-content"] { padding: 12px; }
#edu-message-table { min-width: 0; }
#edu-message-view { min-height: 40px; }
#edu-search-feedback { min-height: 0; }
#edu-search-feedback:empty { display: none; }
#edu-shortcut-note { color: #68717e; font-size: 12px; }
#edu-login { width: min(440px, calc(100% - 32px)); min-width: 0 !important;
    margin: 64px auto; padding: 24px; border: 1px solid var(--edu-line);
    border-radius: 8px; background: #fff; }
#edu-login h1 { font-size: 26px; }
#edu-login button { min-height: 44px; }
#edu-login-message { min-height: 28px; }
#edu-feedback p { margin: 0; }
.edu-feedback { padding: 10px 14px; border-left: 3px solid #2563eb; background: #edf3ff; }
.edu-success { color: #166534; border-color: #15803d; background: #f0fdf4; }
.edu-error { color: #991b1b; border-color: #dc2626; background: #fef2f2; }
.edu-placeholder { padding: 24px 0; color: #68717e; }
@media (min-width: 768px) {
    #edu-menu > .label-wrap { display: none; }
    #edu-menu > [data-testid="accordion-content"] { display: block !important; }
}
@media (max-width: 1199px) and (min-width: 768px) {
    #edu-sidebar { flex-basis: 176px; width: 176px; }
    #edu-brand { flex-basis: 128px; }
}
@media (max-width: 767px) {
    #edu-topbar { height: auto; min-height: 64px; padding: 8px 12px; gap: 8px; flex-wrap: wrap; }
    #edu-brand { flex: 0 0 96px; font-size: 17px; }
    #edu-context { display: none; }
    #edu-search { order: 3; flex: 1 1 100%; min-width: 0; }
    #edu-message-button { flex-basis: 72px; font-size: 12px; }
    #edu-user-menu { flex: 0 0 auto; }
    #edu-user-menu > .label-wrap { padding: 0 8px; }
    #edu-page-header { align-items: flex-start; flex-wrap: wrap; }
    #edu-page-breadcrumb { flex-basis: 100%; }
    #edu-page-actions { flex-basis: 100%; justify-content: flex-start; flex-wrap: wrap; }
    #edu-shell-row { flex-direction: column; min-height: calc(100vh - 64px); }
    #edu-sidebar { flex: 0 0 auto; width: 100%; padding: 4px 12px;
        border-right: 0; border-bottom: 1px solid var(--edu-line); }
    #edu-content { padding: 16px 12px; }
    #edu-login { margin: 32px auto; }
}
"""


def navigation_items_for_roles(
    roles: Iterable[UserRole | str],
) -> tuple[LayoutNavigationItem, ...]:
    """返回当前会话已授权的全部菜单项。"""

    role_values = {
        item.value if isinstance(item, UserRole) else str(item) for item in roles
    }
    return tuple(
        item
        for item in ROLE_NAVIGATION
        if any(
            role.value in role_values or role.name in role_values
            for role in item.allowed_roles
        )
    )


def navigation_item(key: str | None) -> LayoutNavigationItem | None:
    """按键读取菜单项；未知键不会获得任何权限。"""

    return next((item for item in ROLE_NAVIGATION if item.key == key), None)


def is_authorized_navigation(key: str | None, roles: Iterable[UserRole | str]) -> bool:
    """校验导航键是否属于当前角色，防止伪造选择值越权。"""

    item = navigation_item(key)
    if item is None:
        return False
    return item in navigation_items_for_roles(roles)


def placeholder_page(item: LayoutNavigationItem | None) -> str:
    """为尚未接入业务视图的菜单提供诚实的中文空态。"""

    if item is None:
        return "当前账号没有可访问的导航项。"
    return f'## {item.label}\n\n<div class="edu-placeholder">功能暂不可用</div>'


def breadcrumb_html(items: Sequence[str]) -> str:
    """生成共享工作台使用的安全面包屑 HTML。"""

    labels = [escape(str(item).strip()) for item in items if str(item).strip()]
    if not labels:
        return ""
    return '<span class="edu-breadcrumb">' + " / ".join(labels) + "</span>"


def feedback(message: str, kind: str = "info") -> str:
    """兼容外壳既有接口，统一成功、失败、警告、加载与空态的中文反馈。"""

    if not message:
        return ""
    state = {
        "info": UiStatus.INFO,
        "success": UiStatus.COMPLETED,
        "error": UiStatus.FAILED,
        "warning": UiStatus.PENDING_REVIEW,
        "loading": UiStatus.LOADING,
        "empty": UiStatus.EMPTY,
    }.get(kind, UiStatus.INFO)
    return status_banner(state, entity="ui", detail=message)


@dataclass(frozen=True)
class ConfirmationView:
    """内联确认区的稳定句柄，pending 仅保存在当前 Gradio 会话。"""

    panel: gr.Column
    message: gr.HTML
    confirm: gr.Button
    cancel: gr.Button
    pending: gr.State


def bind_confirmation(
    trigger: gr.Button,
    *,
    action: str,
    target: gr.Textbox,
    callback: Callable[..., Any],
    inputs: Sequence[Any],
    outputs: Sequence[Any],
) -> ConfirmationView:
    """把已有操作接入双阶段确认，不实现删除、提交或发布业务。

    inputs 是原回调的参数顺序，包含 target；值应可深拷贝且支持比较。
    首次点击只暂存参数并展示目标；确认仅调用一次原回调。修改参数、取消、
    退出或清空 pending 后须重新确认，防止确认对象与实际提交对象不一致。
    """

    target_index = list(inputs).index(target)
    with gr.Column(visible=False, elem_classes="edu-confirmation") as panel:
        pending = gr.State(None)
        message = gr.HTML()
        with gr.Row():
            confirm = gr.Button(f"确认{action}", variant="stop")
            cancel = gr.Button("取消")

    def close() -> dict[Any, Any]:
        return {panel: gr.update(visible=False), pending: None, message: ""}

    def request(*values: Any) -> dict[Any, Any]:
        label = str(values[target_index] or "").strip()
        if not label:
            raise gr.Error("请先选择操作对象。")
        return {
            pending: deepcopy(list(values)),
            panel: gr.update(visible=True),
            message: status_banner(
                UiStatus.INFO,
                entity="ui",
                detail=f"确认{action}：{label}\n确认后将执行此操作。",
            ),
        }

    def execute(snapshot: list[Any] | None, *values: Any) -> dict[Any, Any]:
        if snapshot is None or snapshot != list(values):
            raise gr.Error("确认已失效，请重新发起操作。")
        response = callback(*values)
        result = close()
        if isinstance(response, dict) and all(key in outputs for key in response):
            result.update(response)
        else:
            returned = response if len(outputs) != 1 else (response,)
            result.update(zip(outputs, returned, strict=True))
        return result

    local_outputs = [panel, pending, message]
    trigger.click(
        request, inputs=list(inputs), outputs=local_outputs, show_progress="hidden"
    )
    confirm.click(
        execute,
        inputs=[pending, *inputs],
        outputs=[*outputs, *local_outputs],
        show_progress="minimal",
        concurrency_id="eduagent-ui",
        concurrency_limit=1,
    )
    cancel.click(close, outputs=local_outputs, show_progress="hidden", queue=False)
    for component in inputs:
        if not isinstance(component, gr.State):
            event = getattr(component, "input", component.change)
            event(close, outputs=local_outputs, show_progress="hidden", queue=False)
    return ConfirmationView(panel, message, confirm, cancel, pending)


def resettable_components(panel: Any) -> list[tuple[Any, Any]]:
    """递归记录视图初值，退出时同时清除隐藏页的数据和输入。"""

    components: list[tuple[Any, Any]] = []
    for child in getattr(panel, "children", []):
        if isinstance(
            child,
            (
                gr.Textbox,
                gr.Number,
                gr.Dropdown,
                gr.Checkbox,
                gr.CheckboxGroup,
                gr.Radio,
                gr.JSON,
                gr.Dataframe,
                gr.Markdown,
                gr.HTML,
                gr.State,
            ),
        ):
            components.append((child, deepcopy(child.value)))
        else:
            components.extend(resettable_components(child))
    return components


__all__ = [
    "ROLE_NAVIGATION",
    "STATUS_COLORS",
    "STATUS_CSS",
    "WORKSPACE_CSS",
    "ConfirmationView",
    "LayoutNavigationItem",
    "StatusEntity",
    "StatusPresentation",
    "StatusTone",
    "UiStatus",
    "bind_confirmation",
    "breadcrumb_html",
    "confidence_banner",
    "create_status_styles",
    "empty_state",
    "feedback",
    "get_status",
    "is_authorized_navigation",
    "loading_state",
    "navigation_item",
    "navigation_items_for_roles",
    "placeholder_page",
    "question_indicator",
    "resettable_components",
    "status_badge",
    "status_banner",
    "status_choices",
    "status_label",
    "status_text",
    "table_options",
]
