"""教师题库管理和题目审核 Gradio 视图。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from functools import partial
from html import escape
from typing import Any, Literal, cast

import gradio as gr
from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from backend.app.core.database import get_session_factory
from backend.app.domain.enums import QuestionStatus, QuestionType, UserRole
from backend.app.domain.permissions import PermissionDeniedError, normalize_role
from backend.app.services.auth_service import AuthenticationError
from backend.app.services.course_service import CourseService, CourseServiceError
from backend.app.services.file_storage_service import FileStorageError
from backend.app.services.question_service import (
    QuestionService,
    QuestionServiceError,
    QuestionSummary,
)
from backend.app.ui import question_review_loaders
from backend.app.ui.layout_view import (
    UiStatus,
    bind_confirmation,
    empty_state,
    feedback,
    status_badge,
    status_banner,
    status_choices,
    status_label,
    table_options,
)
from backend.app.ui.question_review_view import (
    approval_enabled,
    create_question_review_panel,
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
    "题干摘要",
    "题型",
    "分值",
    "知识点",
    "审核状态",
)
QUESTION_TABLE_DATATYPES = cast(
    tuple[Literal["str", "markdown"], ...],
    ("str", "str", "str", "str", "markdown"),
)
_GENERIC_ERROR = "题目操作失败，请稍后重试。"


@dataclass(frozen=True)
class QuestionView:
    """题目视图中由主应用控制的组件集合。"""

    panel: gr.Column
    questions_table: gr.Dataframe
    message: gr.Markdown
    list_panel: gr.Column
    detail_panel: gr.Column
    editor: gr.Column


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
    elif isinstance(error, (AuthenticationError, FileStorageError)):
        message = str(error)
    elif isinstance(error, HTTPException):
        message = str(error.detail)
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
    """把题目摘要转换为不显示内部 ID 的 Gradio 表格行。"""

    return [
        [
            question.content.replace("\n", " ")[:120],
            status_label(question.type, entity="question_type"),
            str(question.score),
            "、".join(question.knowledge_points),
            status_badge(question.status, entity="question"),
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


@dataclass(frozen=True)
class QuestionSelection:
    """题库与组卷共用的摘要表和内部选行标识。"""

    table: gr.Dataframe
    ids: gr.State


def create_question_selection(label: str = "题目摘要") -> QuestionSelection:
    """创建不显示内部 ID 的题目选择组件。"""

    settings = table_options(QUESTION_TABLE_HEADERS)
    settings.update(
        column_widths=[420, 100, 80, 180, 140],
        elem_classes=["edu-status-table", "edu-question-table"],
    )
    table = gr.Dataframe(
        headers=list(QUESTION_TABLE_HEADERS),
        datatype=QUESTION_TABLE_DATATYPES,
        value=[],
        interactive=False,
        label=label,
        **settings,
    )
    return QuestionSelection(table, gr.State([]))


def question_selection_data(
    questions: Sequence[QuestionSummary],
) -> tuple[list[list[str]], list[str]]:
    """确保表格与选行 ID 始终按同一顺序更新。"""

    return _question_rows(questions), [question.id for question in questions]


def selected_question_id(event: gr.SelectData, ids: Sequence[str]) -> str:
    """解析表格选行，拒绝过期或非法选择。"""

    index = event.index[0] if isinstance(event.index, (tuple, list)) else event.index
    if not event.selected or not isinstance(index, int) or not 0 <= index < len(ids):
        raise ValueError("题目选择已失效，请重新选择。")
    return ids[index]


def teacher_id_from_state(state: Mapping[str, Any]) -> str:
    """在服务查询前确保教师标识非空，避免查询范围意外扩大。"""

    _ensure_teacher(state)
    teacher_id = str(state.get("user_id") or "")
    if not teacher_id:
        raise PermissionDeniedError("登录状态缺少用户标识。")
    return teacher_id


def teacher_course_choices(state: Mapping[str, Any]) -> list[tuple[str, str]]:
    """返回当前教师拥有的课程，名称用于展示，ID 仅用于绑定。"""

    teacher_id = teacher_id_from_state(state)
    with get_session_factory()() as session:
        courses = CourseService(session).list_courses(teacher_id=teacher_id)
    return [(course.name, course.id) for course in courses]


def load_question_choices(
    course_id: str | None,
    question_type: str | None,
    knowledge_point: str | None,
    question_status: str | None,
    state: Mapping[str, Any],
) -> list[QuestionSummary]:
    """在服务授权的数据集上应用题型和知识点筛选。"""

    teacher_id = teacher_id_from_state(state)
    with get_session_factory()() as session:
        questions = QuestionService(session).list_questions(
            course_id=_course_filter(course_id),
            status=_status_filter(question_status),
            teacher_id=teacher_id,
        )
    if question_type:
        kind = QuestionType(question_type)
        questions = [question for question in questions if question.type == kind]
    if knowledge_point and knowledge_point.strip():
        keyword = knowledge_point.strip().casefold()
        questions = [
            question
            for question in questions
            if any(keyword in point.casefold() for point in question.knowledge_points)
        ]
    return questions


def option_text_rows(options: Any) -> list[list[str]]:
    """兼容已有文本选项；无法无损编辑的复杂结构明确报错。"""

    if options is None:
        return []
    if isinstance(options, dict) and all(
        isinstance(value, str) for value in options.values()
    ):
        return [[str(key), value] for key, value in options.items()]
    if isinstance(options, list) and all(isinstance(value, str) for value in options):
        return [[str(index + 1), value] for index, value in enumerate(options)]
    raise ValueError("该题选项结构暂不支持编辑，原始题目数据已保留。")


def _option_choices(rows: Sequence[Sequence[Any]]) -> list[tuple[str, str]]:
    return [
        (f"{row[0]}：{row[1]}", str(row[0]))
        for row in rows
        if len(row) >= 2 and str(row[0]).strip() and str(row[1]).strip()
    ]


def _answer_keys(answer: str | None, rows: Sequence[Sequence[str]]) -> list[str]:
    """把已有答案文字或选项标识映射为选择控件的值。"""

    if not answer:
        return []
    if answer in {cell for row in rows for cell in row}:
        parts = [answer]
    else:
        try:
            value = json.loads(answer)
        except (ValueError, TypeError):
            value = re.split(r"[,，、;；\s]+", answer)
        parts = value if isinstance(value, list) else [answer]
    return [key for key, text in rows if key in parts or text in parts]


def question_editor_payload(
    kind: str,
    rows: Sequence[Sequence[Any]],
    single: str | None,
    multiple: Sequence[str] | None,
    boolean: str | None,
    answer: str,
    original: Mapping[str, Any] | None,
) -> tuple[Any, str | None]:
    """把自然编辑控件转换为选项与参考答案，不引入新的存储格式。"""

    question_type = QuestionType(kind)
    if question_type == QuestionType.TRUE_FALSE:
        return None, boolean
    if question_type not in {QuestionType.SINGLE_CHOICE, QuestionType.MULTIPLE_CHOICE}:
        return None, answer or None
    options: dict[str, str] = {}
    for row in rows:
        key, text = (
            str(cell or "").strip()
            for cell in list(row[:2]) + [""] * max(0, 2 - len(row))
        )
        if not key and not text:
            continue
        if not key or not text:
            raise ValueError("每个选项都需要填写标识和内容。")
        if key in options:
            raise ValueError("选项标识不能重复。")
        options[key] = text
    chosen = (
        [single]
        if question_type == QuestionType.SINGLE_CHOICE and single
        else list(multiple or [])
    )
    if len(options) < 2:
        raise ValueError("选择题至少需要两个完整选项。")
    if not chosen or any(key not in options for key in chosen):
        raise ValueError("请选择有效的正确答案。")
    result: Any = options
    reference: str | None = (
        chosen[0]
        if question_type == QuestionType.SINGLE_CHOICE
        else json.dumps(chosen, ensure_ascii=False)
    )
    if original and kind == original.get("type"):
        previous_rows = option_text_rows(original.get("options"))
        if previous_rows == [[key, text] for key, text in options.items()]:
            result = original.get("options")
            previous_answer = original.get("reference_answer")
            if set(_answer_keys(previous_answer, previous_rows)) == set(chosen):
                reference = previous_answer
    return result, reference


def question_preview_html(
    question: QuestionSummary, courses: Sequence[tuple[str, str]]
) -> str:
    """展示完整持久化题目；所有题目内容按文本转义，不猜测复杂选项。"""

    def text(value: str | None) -> str:
        return escape(value) if value else "尚未填写"

    course_name = next(
        (label for label, identifier in courses if identifier == question.course_id),
        "所属课程暂不可用",
    )
    tags = [
        course_name,
        status_label(question.type, entity="question_type"),
        f"{question.score} 分",
    ]
    if question.difficulty:
        tags.append(f"难度：{question.difficulty}")
    tags.extend(question.knowledge_points)
    meta = "".join(
        f'<span class="edu-question-tag">{escape(tag)}</span>' for tag in tags
    )
    options_html = ""
    if question.options is not None:
        try:
            rows = option_text_rows(question.options)
            options_html = "".join(
                '<div class="edu-question-option">'
                f'<b>{escape(key)}</b><span class="edu-question-body">{escape(value)}</span></div>'
                for key, value in rows
            )
        except ValueError:
            options_html = (
                '<div class="edu-question-body">'
                + escape(json.dumps(question.options, ensure_ascii=False, indent=2))
                + "</div>"
            )
    missing_basis = [
        label
        for label, value in [
            ("参考答案", question.reference_answer),
            ("评分标准", question.scoring_rubric),
        ]
        if not value
    ]
    notice = (
        status_banner(
            UiStatus.PENDING_COMPLETION,
            entity="ui",
            detail="、".join(missing_basis) + "尚未填写，补全后再送审。",
        )
        if missing_basis
        else ""
    )
    return (
        notice + '<div class="edu-question-preview-grid">'
        '<section class="edu-question-preview-section"><h3>题目内容</h3>'
        f'<div class="edu-question-meta">{meta}</div>'
        f'<div class="edu-question-body">{escape(question.content)}</div>{options_html}</section>'
        '<section class="edu-question-preview-section"><h3>答案与评分</h3>'
        f'<h4>参考答案</h4><div class="edu-question-body">{text(question.reference_answer)}</div>'
        f'<h4>评分标准</h4><div class="edu-question-body">{text(question.scoring_rubric)}</div>'
        "</section></div>"
    )


def question_list_summary(questions: Sequence[QuestionSummary]) -> str:
    """统计只对应当前查询结果，不冒充全库统计。"""

    pending = sum(
        question.status == QuestionStatus.PENDING_REVIEW for question in questions
    )
    approved = sum(question.status == QuestionStatus.APPROVED for question in questions)
    return (
        '<div class="edu-question-summary">'
        f"<span>当前筛选<strong>{len(questions)}</strong> 道</span>"
        f'<span class="edu-summary-pending">待审核<strong>{pending}</strong></span>'
        f'<span class="edu-summary-approved">已审核<strong>{approved}</strong></span>'
        "</div>"
    )


def create_question_view(session_state: Any | None = None) -> QuestionView:
    """创建宽列表、只读详情和按需编辑的题库审核工作区。"""

    state = session_state or gr.State(_empty_state())
    errors = (
        AuthenticationError,
        FileStorageError,
        HTTPException,
        PermissionDeniedError,
        CourseServiceError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    )
    with gr.Column(
        visible=False, elem_id="edu-question-bank", elem_classes="edu-business-page"
    ) as panel:
        snapshot = gr.State(None)
        editing_mode = gr.State(False)
        question_id = gr.Textbox(visible=False, container=False)
        with gr.Row(elem_classes=["edu-question-heading"]):
            gr.HTML(
                '<div class="edu-question-title"><h2>题库管理</h2>'
                "<p>管理课程题目、核对答案与评分标准，审核后用于组卷。</p></div>"
            )
            new_button = gr.Button(
                "新建题目",
                variant="primary",
                scale=0,
                elem_id="edu-question-new",
                elem_classes=["edu-icon"],
            )
        message = gr.Markdown("", elem_id="edu-question-message")
        with gr.Column(elem_id="edu-question-list") as list_panel:
            with (
                gr.Column(elem_classes=["edu-surface"]),
                gr.Row(elem_classes=["edu-filter-row"]),
            ):
                filter_course = gr.Dropdown(
                    label="所属课程",
                    choices=[],
                    value=None,
                    min_width=140,
                )
                filter_kind = gr.Dropdown(
                    label="题型",
                    choices=status_choices(
                        QUESTION_TYPE_CHOICES, entity="question_type", include_all=True
                    ),
                    value="",
                    min_width=120,
                )
                filter_point = gr.Textbox(
                    label="知识点",
                    placeholder="输入知识点",
                    min_width=140,
                )
                filter_status = gr.Dropdown(
                    label="审核状态",
                    choices=status_choices(
                        QUESTION_STATUS_CHOICES, entity="question", include_all=True
                    ),
                    value="",
                    min_width=120,
                )
                with gr.Row(elem_classes=["edu-filter-actions"]):
                    refresh_button = gr.Button(
                        "查询",
                        variant="primary",
                        scale=0,
                        elem_id="edu-question-refresh",
                        elem_classes=["edu-icon"],
                    )
                    reset_button = gr.Button(
                        "重置",
                        scale=0,
                        elem_id="edu-question-reset",
                        elem_classes=["edu-icon"],
                    )
            with gr.Column(elem_classes=["edu-surface"]):
                gr.HTML(
                    '<div class="edu-question-list-title"><h3>题目列表</h3>'
                    "<span>点击题目查看完整内容与审核详情</span></div>"
                )
                list_summary = gr.HTML(
                    '<div class="edu-question-summary">选择课程或查询，加载你的题目。</div>',
                    elem_id="edu-question-summary",
                )
                picker = create_question_selection()
        with gr.Column(visible=False, elem_id="edu-question-detail") as detail_panel:
            with gr.Row(elem_classes=["edu-detail-toolbar"]):
                close_button = gr.Button(
                    "返回题库",
                    scale=0,
                    elem_id="edu-question-back",
                    elem_classes=["edu-icon"],
                )
                detail_status = gr.Markdown("", elem_id="edu-question-detail-status")
                edit_button = gr.Button(
                    "编辑题目",
                    scale=0,
                    elem_id="edu-question-edit",
                    elem_classes=["edu-icon"],
                )
                submit_button = gr.Button("提交审核", scale=0, interactive=False)
                validation_button = gr.Button(
                    "执行当前语义核验", scale=0, interactive=False
                )
                approve_button = gr.Button(
                    "审核通过", variant="primary", scale=0, interactive=False
                )
                revision_button = gr.Button("退回修订", scale=0, interactive=False)
            revision_comment = gr.Textbox(label="退回修订意见", lines=2)
            preview = gr.HTML("", elem_id="edu-question-preview")
            with gr.Column(
                visible=False,
                elem_id="edu-question-editor",
                elem_classes=["edu-surface"],
            ) as editor:
                gr.Markdown("### 编辑题目\n保存后返回详情，再提交审核。")
                edit_course = gr.Dropdown(label="所属课程", choices=[], value=None)
                with gr.Row():
                    kind = gr.Dropdown(
                        label="题型",
                        choices=status_choices(
                            QUESTION_TYPE_CHOICES, entity="question_type"
                        ),
                        value=QuestionType.SHORT_ANSWER.value,
                    )
                    score = gr.Number(label="分值", value=10, minimum=0.01)
                content = gr.Textbox(label="完整题干", lines=5)
                options = gr.Dataframe(
                    label="逐项选项",
                    headers=["选项标识", "选项内容"],
                    datatype=["str", "str"],
                    type="array",
                    value=[],
                    row_count=4,
                    column_count=2,
                    interactive=True,
                    visible=False,
                )
                single = gr.Radio(label="参考答案", choices=[], visible=False)
                multiple = gr.CheckboxGroup(label="参考答案", choices=[], visible=False)
                boolean = gr.Radio(
                    label="参考答案",
                    choices=[("正确", "True"), ("错误", "False")],
                    visible=False,
                )
                answer = gr.Textbox(label="参考答案", lines=3)
                rubric = gr.Textbox(label="评分标准", lines=3)
                analysis = gr.Textbox(label="解析（未提供时留空）", lines=3)
                with gr.Row():
                    difficulty = gr.Textbox(label="难度")
                    points = gr.Textbox(
                        label="知识点", placeholder="多个知识点用逗号分隔"
                    )
                with gr.Row(elem_classes=["edu-editor-actions"]):
                    save_button = gr.Button(
                        "保存题目",
                        variant="primary",
                        interactive=False,
                        elem_id="edu-question-save",
                        elem_classes=["edu-icon"],
                    )
                    cancel_button = gr.Button(
                        "取消编辑（放弃未保存修改）", visible=False
                    )
            review_panel = create_question_review_panel(
                question_id,
                state,
                approval_button=approve_button,
                editing_state=editing_mode,
                validation_button=validation_button,
                revision_button=revision_button,
                submit_button=submit_button,
                question_snapshot=snapshot,
                status_display=detail_status,
            )
            with gr.Accordion(
                "删除题目", open=False, elem_id="edu-question-delete"
            ) as delete_section:
                delete_target = gr.Textbox(label="待删除题目", interactive=False)
                delete_button = gr.Button("删除题目", variant="stop", interactive=False)

        detail_outputs = [
            list_panel,
            detail_panel,
            editor,
            preview,
            edit_button,
            cancel_button,
            delete_section,
            snapshot,
            editing_mode,
            question_id,
            detail_status,
            edit_course,
            kind,
            score,
            content,
            options,
            single,
            multiple,
            boolean,
            answer,
            rubric,
            analysis,
            difficulty,
            points,
            save_button,
            approve_button,
            revision_button,
            submit_button,
            validation_button,
            revision_comment,
            delete_target,
            delete_button,
            *review_panel.outputs,
        ]
        detail_outputs = list(dict.fromkeys(detail_outputs))
        outputs = [
            *detail_outputs,
            filter_course,
            filter_kind,
            filter_point,
            filter_status,
            list_summary,
            picker.table,
            picker.ids,
            message,
        ]

        def visibility(value: str) -> dict[Any, Any]:
            choice = value in {
                QuestionType.SINGLE_CHOICE.value,
                QuestionType.MULTIPLE_CHOICE.value,
            }
            return {
                options: gr.update(visible=choice),
                single: gr.update(visible=value == QuestionType.SINGLE_CHOICE.value),
                multiple: gr.update(
                    visible=value == QuestionType.MULTIPLE_CHOICE.value
                ),
                boolean: gr.update(visible=value == QuestionType.TRUE_FALSE.value),
                answer: gr.update(
                    visible=not choice and value != QuestionType.TRUE_FALSE.value
                ),
            }

        def form(
            question: QuestionSummary | None,
            courses: list[tuple[str, str]],
            course: str | None = None,
            *,
            new: bool = False,
            editing: bool = False,
        ) -> dict[Any, Any]:
            current = question.model_dump(mode="json") if question else None
            editable = (new or question is not None) and (
                question is None
                or question.status
                in {
                    QuestionStatus.DRAFT,
                    QuestionStatus.PENDING_REVIEW,
                    QuestionStatus.NEEDS_REVISION,
                }
            )
            row_values: list[list[str]] = []
            warning = ""
            try:
                row_values = option_text_rows(question.options if question else None)
            except ValueError as error:
                editable = False
                warning = feedback(str(error), "warning")
            question_kind = (
                question.type.value if question else QuestionType.SHORT_ANSWER.value
            )
            keys = _answer_keys(
                question.reference_answer if question else None, row_values
            )
            choices = _option_choices(row_values)
            editing = new or (editing and editable)
            result: dict[Any, Any] = {
                list_panel: gr.update(visible=not (new or question is not None)),
                detail_panel: gr.update(visible=new or question is not None),
                editor: gr.update(visible=editing),
                preview: gr.update(
                    value=question_preview_html(question, courses) if question else "",
                    visible=question is not None and not editing,
                ),
                edit_button: gr.update(visible=not editing, interactive=editable),
                cancel_button: gr.update(visible=editing and question is not None),
                delete_section: gr.update(visible=question is not None and not editing),
                snapshot: current,
                editing_mode: editing,
                question_id: question.id if question else "",
                detail_status: warning
                or (
                    status_badge(question.status, entity="question")
                    if question
                    else empty_state("新建题目" if new else "尚未选择题目。")
                ),
                edit_course: gr.update(
                    choices=courses,
                    value=question.course_id if question else course,
                    interactive=new,
                ),
                kind: gr.update(value=question_kind, interactive=editable),
                score: gr.update(
                    value=float(question.score) if question else 10,
                    interactive=editable,
                ),
                content: gr.update(
                    value=question.content if question else "", interactive=editable
                ),
                options: gr.update(value=row_values, interactive=editable),
                single: gr.update(
                    choices=choices,
                    value=keys[0] if keys else None,
                    interactive=editable,
                ),
                multiple: gr.update(choices=choices, value=keys, interactive=editable),
                boolean: gr.update(
                    value=(
                        {
                            "true": "True",
                            "正确": "True",
                            "false": "False",
                            "错误": "False",
                        }.get((question.reference_answer or "").casefold())
                        if question
                        else None
                    ),
                    interactive=editable,
                ),
                answer: gr.update(
                    value=(question.reference_answer or "") if question else "",
                    interactive=editable,
                ),
                rubric: gr.update(
                    value=(question.scoring_rubric or "") if question else "",
                    interactive=editable,
                ),
                analysis: gr.update(
                    value=(question.analysis or "") if question else "",
                    interactive=editable,
                ),
                difficulty: gr.update(
                    value=(question.difficulty or "") if question else "",
                    interactive=editable,
                ),
                points: gr.update(
                    value="、".join(question.knowledge_points) if question else "",
                    interactive=editable,
                ),
                save_button: gr.update(interactive=editable and editing),
                approve_button: gr.update(
                    visible=not editing,
                    interactive=bool(
                        question
                        and editable
                        and not editing
                        and approval_enabled(question)
                    ),
                ),
                revision_button: gr.update(
                    visible=not editing,
                    interactive=bool(
                        question
                        and not editing
                        and question.status
                        in {QuestionStatus.PENDING_REVIEW, QuestionStatus.APPROVED}
                    ),
                ),
                submit_button: gr.update(
                    visible=not editing,
                    interactive=bool(
                        question
                        and editable
                        and not editing
                        and question.status
                        in {QuestionStatus.DRAFT, QuestionStatus.NEEDS_REVISION}
                    ),
                ),
                validation_button: gr.update(
                    visible=not editing,
                    interactive=bool(
                        question
                        and not editing
                        and question.status == QuestionStatus.PENDING_REVIEW
                    ),
                ),
                revision_comment: gr.update(
                    value="", visible=not editing and question is not None
                ),
                delete_target: question.content[:100] if question else "",
                delete_button: gr.update(interactive=question is not None),
            }
            review_updates = (
                review_panel.render(question, editing=editing)
                if question is not None
                else review_panel.clear()
            )
            for button in (
                approve_button,
                validation_button,
                revision_button,
                submit_button,
            ):
                if button in review_updates:
                    result[button].update(review_updates.pop(button))
            result.update(review_updates)
            if editing:
                result[approve_button] = gr.update(visible=False, interactive=False)
            for component, update in visibility(question_kind).items():
                result[component].update(update)
            return result

        def refresh(
            course: str | None,
            qtype: str,
            point: str,
            status: str,
            current_state: Mapping[str, Any],
        ) -> dict[Any, Any]:
            try:
                courses = teacher_course_choices(current_state)
                questions = load_question_choices(
                    course, qtype, point, status, current_state
                )
                rows, ids = question_selection_data(questions)
                result = form(None, courses)
                result.update(
                    {
                        filter_course: gr.update(choices=courses, value=course),
                        picker.table: rows,
                        picker.ids: ids,
                        list_summary: question_list_summary(questions),
                        message: ("" if rows else empty_state("暂无符合条件的题目。")),
                    }
                )
                return result
            except errors as error:
                return {
                    **form(None, []),
                    picker.table: [],
                    picker.ids: [],
                    list_summary: '<div class="edu-question-summary">题目统计暂不可用</div>',
                    message: _format_error(error),
                }

        def new(course: str | None, current_state: Mapping[str, Any]) -> dict[Any, Any]:
            try:
                courses = teacher_course_choices(current_state)
                selected_course = (
                    course if course in {value for _, value in courses} else None
                )
                return {
                    **form(None, courses, selected_course, new=True),
                    message: "" if courses else empty_state("暂无课程，请先创建课程。"),
                }
            except errors as error:
                return {message: _format_error(error)}

        def select_row(
            ids: list[str], current_state: Mapping[str, Any], event: gr.SelectData
        ) -> dict[Any, Any]:
            try:
                teacher_id_from_state(current_state)
                question = question_review_loaders.detail(
                    selected_question_id(event, ids), current_state
                )
                return {
                    **form(question, teacher_course_choices(current_state)),
                    message: "",
                }
            except errors as error:
                return {**form(None, []), message: _format_error(error)}

        def reset_filters(current_state: Mapping[str, Any]) -> dict[Any, Any]:
            result = refresh(None, "", "", "", current_state)
            result.update(
                {
                    filter_kind: gr.update(value=""),
                    filter_point: gr.update(value=""),
                    filter_status: gr.update(value=""),
                }
            )
            return result

        def close_detail() -> dict[Any, Any]:
            return {**form(None, []), message: ""}

        def edit_selected(
            original: dict[str, Any] | None,
            current_state: Mapping[str, Any],
            *,
            editing: bool = True,
        ) -> dict[Any, Any]:
            try:
                teacher_id_from_state(current_state)
                if not original:
                    raise ValueError("请先选择题目。")
                question = question_review_loaders.detail(original["id"], current_state)
                return {
                    **form(
                        question, teacher_course_choices(current_state), editing=editing
                    ),
                    message: "",
                }
            except errors as error:
                return {message: _format_error(error)}

        def refresh_after(
            question: QuestionSummary, current_state: Mapping[str, Any], text: str
        ) -> dict[Any, Any]:
            result = refresh(question.course_id, "", "", "", current_state)
            actual = question_review_loaders.detail(question.id, current_state)
            result.update(form(actual, teacher_course_choices(current_state)))
            result.update(
                {
                    filter_kind: gr.update(value=""),
                    filter_point: gr.update(value=""),
                    filter_status: gr.update(value=""),
                }
            )
            result[message] = feedback(text, "success")
            return result

        def save(
            original: dict[str, Any] | None,
            course: str | None,
            qtype: str,
            text: str,
            rows: list[list[Any]],
            one: str | None,
            many: list[str],
            truth: str | None,
            reference: str,
            criteria: str,
            explanation: str,
            level: str,
            knowledge: str,
            value: float,
            current_state: Mapping[str, Any],
        ) -> dict[Any, Any]:
            try:
                teacher_id = teacher_id_from_state(current_state)
                if not course:
                    raise ValueError("请选择所属课程。")
                option_value, reference_value = question_editor_payload(
                    qtype, rows, one, many, truth, reference, original
                )
                with get_session_factory()() as session:
                    service = QuestionService(session)
                    payload = {
                        "question_type": qtype,
                        "content": text,
                        "options": option_value,
                        "reference_answer": reference_value,
                        "scoring_rubric": criteria or None,
                        "analysis": explanation or None,
                        "difficulty": level or None,
                        "knowledge_points": _parse_knowledge_points(
                            knowledge.replace("、", ",")
                        ),
                        "score": value,
                    }
                    if original:
                        latest = service.get_question(
                            original["id"], teacher_id=teacher_id
                        )
                        if latest.status == QuestionStatus.APPROVED:
                            raise ValueError("已审核题目为只读，请新建题目。")
                        saved = service.update_question(
                            original["id"], teacher_id=teacher_id, **payload
                        )
                    else:
                        saved = service.create_question(
                            course_id=course, created_by=teacher_id, **payload
                        )
                return refresh_after(saved, current_state, "题目已保存。")
            except errors as error:
                return {message: _format_error(error)}

        def review(
            original: dict[str, Any] | None,
            current_state: Mapping[str, Any],
            comment: str,
            target: QuestionStatus,
        ) -> dict[Any, Any]:
            try:
                teacher_id = teacher_id_from_state(current_state)
                if not original:
                    raise ValueError("请先保存或选择题目。")
                if target == QuestionStatus.NEEDS_REVISION and not comment.strip():
                    raise ValueError("退回修订必须填写真实教师意见。")
                with get_session_factory()() as session:
                    extra = (
                        {"revision_comment": comment.strip()}
                        if target == QuestionStatus.NEEDS_REVISION
                        else {}
                    )
                    updated = QuestionService(session).update_question_status(
                        original["id"], target, teacher_id=teacher_id, **extra
                    )
                return refresh_after(
                    updated,
                    current_state,
                    f"题目已{status_label(target, entity='question')}。",
                )
            except errors as error:
                return {message: _format_error(error)}

        def remove(
            label: str, identifier: str, current_state: Mapping[str, Any]
        ) -> dict[Any, Any]:
            try:
                teacher_id = teacher_id_from_state(current_state)
                with get_session_factory()() as session:
                    service = QuestionService(session)
                    question = service.get_question(identifier, teacher_id=teacher_id)
                    service.delete_question(identifier, teacher_id=teacher_id)
                result = refresh(question.course_id, "", "", "", current_state)
                result.update(
                    {
                        filter_kind: gr.update(value=""),
                        filter_point: gr.update(value=""),
                        filter_status: gr.update(value=""),
                    }
                )
                result[message] = feedback("题目已删除。", "success")
                return result
            except errors as error:
                return {message: _format_error(error)}

        def option_changed(
            rows: list[list[Any]], one: str | None, many: list[str]
        ) -> tuple[Any, Any]:
            choices = _option_choices(rows)
            valid = {value for _, value in choices}
            return (
                gr.update(choices=choices, value=one if one in valid else None),
                gr.update(
                    choices=choices,
                    value=[value for value in many or [] if value in valid],
                ),
            )

        filters = [filter_course, filter_kind, filter_point, filter_status, state]
        event_options: dict[str, Any] = {
            "outputs": outputs,
            "show_progress": "minimal",
            "concurrency_id": "eduagent-ui",
            "concurrency_limit": 1,
        }
        refresh_button.click(refresh, inputs=filters, **event_options)
        reset_button.click(reset_filters, inputs=[state], **event_options)
        close_button.click(close_detail, **event_options)
        edit_button.click(edit_selected, inputs=[snapshot, state], **event_options)
        cancel_button.click(
            partial(edit_selected, editing=False),
            inputs=[snapshot, state],
            **event_options,
        )
        for component in (filter_course, filter_kind, filter_status):
            component.input(refresh, inputs=filters, **event_options)
        filter_point.submit(refresh, inputs=filters, **event_options)
        new_button.click(new, inputs=[filter_course, state], **event_options)
        picker.table.select(select_row, inputs=[picker.ids, state], **event_options)
        save_button.click(
            save,
            inputs=[
                snapshot,
                edit_course,
                kind,
                content,
                options,
                single,
                multiple,
                boolean,
                answer,
                rubric,
                analysis,
                difficulty,
                points,
                score,
                state,
            ],
            **event_options,
        )
        for button, target in (
            (submit_button, QuestionStatus.PENDING_REVIEW),
            (approve_button, QuestionStatus.APPROVED),
            (revision_button, QuestionStatus.NEEDS_REVISION),
        ):
            button.click(
                partial(review, target=target),
                inputs=[snapshot, state, revision_comment],
                **event_options,
            )

        async def validate_current(original, current_state, teaching_chunk_ids=None):
            try:
                teacher_id_from_state(current_state)
                if not original:
                    raise ValueError("请先保存或选择题目。")
                await question_review_loaders.run_validation(
                    original["id"], current_state, teaching_chunk_ids
                )
                actual = question_review_loaders.detail(original["id"], current_state)
                return refresh_after(
                    actual, current_state, "已执行核验，请按真实分项结果处置。"
                )
            except errors as error:
                return {
                    message: _format_error(error),
                    approve_button: gr.update(interactive=False),
                }

        validation_button.click(
            validate_current,
            inputs=[snapshot, state, review_panel.teaching_chunk_ids],
            **event_options,
        )
        kind.input(
            visibility,
            inputs=[kind],
            outputs=[options, single, multiple, boolean, answer],
            show_progress="hidden",
        )
        options.input(
            option_changed,
            inputs=[options, single, multiple],
            outputs=[single, multiple],
            show_progress="hidden",
        )

        # 修改表单后先保存，审核始终针对已持久化的题目。
        def dirty() -> tuple[Any, ...]:
            return tuple(gr.update(interactive=False) for _ in range(4))

        editable_components: tuple[Any, ...] = (
            kind,
            content,
            options,
            single,
            multiple,
            boolean,
            answer,
            rubric,
            analysis,
            difficulty,
            points,
            score,
        )
        for component in editable_components:
            component.input(
                dirty,
                outputs=[
                    submit_button,
                    approve_button,
                    revision_button,
                    validation_button,
                ],
                show_progress="hidden",
            )
        bind_confirmation(
            delete_button,
            action="删除题目",
            target=delete_target,
            callback=remove,
            inputs=[delete_target, question_id, state],
            outputs=outputs,
        )
    return QuestionView(panel, picker.table, message, list_panel, detail_panel, editor)


build_question_view = create_question_view


__all__ = [
    "QUESTION_STATUS_CHOICES",
    "QUESTION_TABLE_HEADERS",
    "QUESTION_TYPE_CHOICES",
    "QuestionSelection",
    "QuestionView",
    "build_question_view",
    "create_question",
    "create_question_selection",
    "create_question_view",
    "delete_question",
    "load_question_choices",
    "question_selection_data",
    "refresh_question_list",
    "refresh_questions",
    "selected_question_id",
    "set_question_status",
    "teacher_course_choices",
    "teacher_id_from_state",
    "update_question",
]
