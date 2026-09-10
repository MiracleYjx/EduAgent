"""教师课程、知识库和资料状态视图。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html import escape
from typing import Any

import gradio as gr
from sqlalchemy.exc import SQLAlchemyError

from backend.app.core.database import get_session_factory
from backend.app.domain.enums import DocumentStatus, UserRole
from backend.app.domain.permissions import PermissionDeniedError, normalize_role
from backend.app.services.course_service import (
    CourseService,
    CourseServiceError,
    CourseSummary,
)
from backend.app.services.knowledge_base_service import (
    DocumentSummary,
    KnowledgeBaseService,
    KnowledgeBaseServiceError,
    KnowledgeBaseSummary,
)
from backend.app.ui.layout_view import (
    bind_confirmation,
    empty_state,
    feedback,
    status_badge,
    status_label,
    table_options,
)

# T038/T039 尚未完成，资料区只展示明确的不可用状态，不创建或伪造资料记录。
INGESTION_READY = False
COURSE_TABLE_HEADERS = ("课程名称", "简介", "知识库数", "更新时间")
DOCUMENT_TABLE_HEADERS = ("文件名", "格式", "处理阶段", "更新时间")
DOCUMENT_STATUS_LABELS = tuple(
    status_label(status, entity="document") for status in DocumentStatus
)
_GENERIC_ERROR = "课程与知识库操作失败，请稍后重试。"


@dataclass(frozen=True)
class KnowledgeBaseView:
    """主应用需要控制的课程与知识库组件集合。"""

    panel: gr.Column
    courses_table: gr.Dataframe
    knowledge_bases_dropdown: gr.Dropdown
    documents_table: gr.Dataframe
    message: gr.Markdown


def _empty_state() -> dict[str, Any]:
    """返回独立视图可用的未登录状态。"""

    return {"access_token": "", "roles": [], "user_id": "", "username": ""}


def _ensure_teacher(state: Mapping[str, Any]) -> None:
    """检查当前会话是否已经登录并拥有教师角色。"""

    if not state.get("access_token"):
        raise PermissionDeniedError("请先登录。")
    try:
        roles = {normalize_role(role) for role in state.get("roles", [])}
    except (TypeError, ValueError) as exc:
        raise PermissionDeniedError("当前账号无权访问课程与知识库。") from exc
    if UserRole.TEACHER not in roles:
        raise PermissionDeniedError("当前账号无权访问课程与知识库。")


def _format_error(error: BaseException) -> str:
    """把服务异常转换成不泄露内部细节的中文提示。"""

    if isinstance(error, PermissionDeniedError):
        message = str(error) or "当前账号无权执行此操作。"
    elif isinstance(error, (CourseServiceError, KnowledgeBaseServiceError)):
        message = str(error) or _GENERIC_ERROR
    elif isinstance(error, SQLAlchemyError):
        message = "系统暂时无法连接数据库，请稍后重试。"
    elif isinstance(error, (TypeError, ValueError)):
        message = f"输入有误：{str(error) or '请检查输入内容。'}"
    else:
        message = _GENERIC_ERROR
    return feedback(message, "error")


def _time_label(value: Any) -> str:
    """把服务时间转换为稳定的中文界面格式。"""

    return value.strftime("%Y-%m-%d %H:%M") if hasattr(value, "strftime") else "-"


def _description_label(value: str | None) -> str:
    """压缩课程简介，避免表格行被超长文本撑开。"""

    text = " ".join((value or "").split())
    return text if len(text) <= 80 else f"{text[:77]}..."


def _course_rows(courses: Sequence[CourseSummary]) -> list[list[str]]:
    """把课程摘要转换为左侧课程表格行。"""

    return [
        [
            course.name,
            _description_label(course.description),
            str(course.knowledge_base_count),
            _time_label(course.updated_at),
        ]
        for course in courses
    ]


def _course_records(courses: Sequence[CourseSummary]) -> list[dict[str, Any]]:
    """保存课程内部标识，避免要求用户在表单中输入课程 ID。"""

    return [
        {
            "id": course.id,
            "name": course.name,
            "description": course.description or "",
            "knowledge_base_count": course.knowledge_base_count,
        }
        for course in courses
    ]


def _course_picker_update(
    records: Sequence[Mapping[str, Any]], selected_id: str | None = None
) -> dict[str, Any]:
    """更新课程下拉框，同时只把内部 ID 放在组件值中。"""

    choices = [
        (str(record.get("name", "")), str(record.get("id", "")))
        for record in records
        if record.get("id") and record.get("name")
    ]
    return gr.update(
        choices=choices,
        value=selected_id if selected_id in {value for _, value in choices} else None,
        interactive=bool(choices),
    )


def _document_rows(documents: Sequence[DocumentSummary]) -> list[list[str]]:
    """把真实文档元数据映射成表格行，状态使用共享规范。"""

    return [
        [
            document.original_filename,
            document.file_format.upper(),
            status_badge(document.status, entity="document"),
            _time_label(document.updated_at),
        ]
        for document in documents
    ]


def _documents_unavailable() -> str:
    """返回摄取链路未就绪时的资料空态。"""

    return feedback(
        "文件摄取功能暂未就绪，暂无可展示的资料。上传按钮已禁用。",
        "warning",
    )


def _source_unavailable() -> str:
    """返回片段追溯未就绪时的来源和失败原因空态。"""

    return feedback(
        "片段追溯功能暂未就绪，暂无可用来源详情或失败原因。",
        "warning",
    )


def _document_status_legend() -> str:
    """展示真实处理阶段的中文规范，不伪造任何资料行。"""

    return "处理阶段规范：" + " · ".join(DOCUMENT_STATUS_LABELS)


def _list_courses(
    search: str | None, state: Mapping[str, Any]
) -> tuple[list[list[str]], list[dict[str, Any]], str]:
    """按当前教师和搜索文本读取课程元数据。"""

    with get_session_factory()() as session:
        courses = CourseService(session).list_courses(teacher_id=state.get("user_id"))
    query = (search or "").strip().casefold()
    if query:
        courses = [
            course
            for course in courses
            if query in course.name.casefold()
            or query in (course.description or "").casefold()
        ]
    rows = _course_rows(courses)
    records = _course_records(courses)
    message = (
        feedback(f"已加载 {len(courses)} 门课程。", "success")
        if courses
        else empty_state("暂无课程。")
    )
    return rows, records, message


def refresh_courses(
    search: str | None, state: Mapping[str, Any]
) -> tuple[list[list[str]], list[dict[str, Any]], dict[str, Any], str]:
    """刷新课程表格，并同步顶部课程下拉框。"""

    try:
        _ensure_teacher(state)
        rows, records, message = _list_courses(search, state)
        return rows, records, _course_picker_update(records), message
    except (
        PermissionDeniedError,
        CourseServiceError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return [], [], _course_picker_update([]), _format_error(error)


def create_course(
    name: str, description: str | None, search: str | None, state: Mapping[str, Any]
) -> tuple[list[list[str]], list[dict[str, Any]], dict[str, Any], str]:
    """创建课程后重新读取当前教师的课程列表。"""

    try:
        _ensure_teacher(state)
        with get_session_factory()() as session:
            CourseService(session).create_course(
                name,
                description or None,
                created_by=state.get("user_id"),
            )
        rows, records, _ = _list_courses(search, state)
        return (
            rows,
            records,
            _course_picker_update(records),
            feedback("课程已创建。", "success"),
        )
    except (
        PermissionDeniedError,
        CourseServiceError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return [], [], _course_picker_update([]), _format_error(error)


def update_course(
    course_id: str,
    name: str,
    description: str | None,
    search: str | None,
    state: Mapping[str, Any],
) -> tuple[list[list[str]], list[dict[str, Any]], dict[str, Any], str]:
    """更新选中课程，课程标识来自选中行的隐藏绑定。"""

    try:
        _ensure_teacher(state)
        if not course_id.strip():
            raise ValueError("请先从课程表格或课程下拉框选择课程。")
        with get_session_factory()() as session:
            CourseService(session).update_course(
                course_id,
                name=name,
                description=description,
                teacher_id=state.get("user_id"),
            )
        rows, records, _ = _list_courses(search, state)
        return (
            rows,
            records,
            _course_picker_update(records, course_id),
            feedback("课程已保存。", "success"),
        )
    except (
        PermissionDeniedError,
        CourseServiceError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return [], [], _course_picker_update([]), _format_error(error)


def _empty_knowledge_base_context() -> tuple[Any, ...]:
    """返回空的知识库、资料和来源区状态。"""

    return (
        gr.update(choices=[], value=None, interactive=False),
        "",
        "",
        "",
        empty_state("请先选择知识库。"),
        [],
    )


def _knowledge_base_context(
    knowledge_bases: Sequence[KnowledgeBaseSummary], selected_id: str | None = None
) -> tuple[Any, ...]:
    """根据真实知识库元数据构建下拉框和资料区状态。"""

    choices = [(item.name, item.id) for item in knowledge_bases]
    selected = next(
        (item for item in knowledge_bases if item.id == selected_id),
        knowledge_bases[0] if knowledge_bases else None,
    )
    if selected is None:
        return _empty_knowledge_base_context()
    return (
        gr.update(choices=choices, value=selected.id, interactive=True),
        selected.id,
        selected.name,
        selected.description or "",
        f"已选知识库：{escape(selected.name)}\n\n资料数量由文件摄取链路提供。",
        [],
    )


def _empty_course_context(message: str | None = None) -> tuple[Any, ...]:
    """返回课程尚未选择时的完整右侧状态。"""

    return (
        "",
        gr.update(value=None),
        "",
        "",
        empty_state("请先选择课程。"),
        *_empty_knowledge_base_context(),
        message or empty_state("请先选择课程。"),
    )


def select_course_by_id(
    course_id: str | None,
    records: Sequence[Mapping[str, Any]],
    state: Mapping[str, Any],
) -> tuple[Any, ...]:
    """按表格或下拉框选择课程，并自动读取其知识库。"""

    try:
        _ensure_teacher(state)
        normalized_id = (course_id or "").strip()
        record = next(
            (item for item in records if str(item.get("id", "")) == normalized_id),
            None,
        )
        if not normalized_id or record is None:
            return _empty_course_context()
        with get_session_factory()() as session:
            knowledge_bases = KnowledgeBaseService(session).list_knowledge_bases(
                course_id=normalized_id,
                teacher_id=state.get("user_id"),
            )
        detail = (
            f"已选课程：{escape(str(record.get('name', '')))}\n\n"
            f"已绑定知识库：{record.get('knowledge_base_count', 0)} 个"
        )
        return (
            normalized_id,
            gr.update(value=normalized_id),
            str(record.get("name", "")),
            str(record.get("description", "")),
            detail,
            *_knowledge_base_context(knowledge_bases),
            feedback("已绑定课程，知识库导航已同步。", "success"),
        )
    except (
        PermissionDeniedError,
        CourseServiceError,
        KnowledgeBaseServiceError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return _empty_course_context(_format_error(error))


def select_course_row(
    event: gr.SelectData,
    records: Sequence[Mapping[str, Any]],
    state: Mapping[str, Any],
) -> tuple[Any, ...]:
    """把课程表格选行转换成内部课程标识。"""

    index = getattr(event, "index", None)
    if isinstance(index, (tuple, list)):
        index = index[0] if index else None
    if isinstance(index, bool) or not isinstance(index, int):
        return _empty_course_context()
    if index < 0 or index >= len(records):
        return _empty_course_context()
    return select_course_by_id(str(records[index].get("id", "")), records, state)


def start_new_course() -> tuple[Any, ...]:
    """清空编辑上下文，准备创建课程。"""

    return (
        "",
        gr.update(value=None),
        "",
        "",
        empty_state("请填写右侧表单创建课程。"),
        *_empty_knowledge_base_context(),
        feedback("已准备新课程表单。", "info"),
    )


def select_knowledge_base(
    knowledge_base_id: str | None,
    course_id: str | None,
    state: Mapping[str, Any],
) -> tuple[Any, ...]:
    """选择知识库并同步资料和来源详情空态。"""

    try:
        _ensure_teacher(state)
        normalized_kb_id = (knowledge_base_id or "").strip()
        normalized_course_id = (course_id or "").strip()
        if not normalized_kb_id or not normalized_course_id:
            return (
                *_empty_knowledge_base_context(),
                empty_state("请先选择课程和知识库。"),
            )
        with get_session_factory()() as session:
            service = KnowledgeBaseService(session)
            selected = service.get_knowledge_base(
                normalized_kb_id, teacher_id=state.get("user_id")
            )
            if selected.course_id != normalized_course_id:
                raise KnowledgeBaseServiceError("知识库不属于当前选中课程。")
            knowledge_bases = service.list_knowledge_bases(
                course_id=normalized_course_id,
                teacher_id=state.get("user_id"),
            )
        return (
            *_knowledge_base_context(knowledge_bases, normalized_kb_id),
            feedback("已切换知识库。", "success"),
        )
    except (
        PermissionDeniedError,
        CourseServiceError,
        KnowledgeBaseServiceError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return (*_empty_knowledge_base_context(), _format_error(error))


def _knowledge_base_action_context(
    course_id: str, selected_id: str | None, state: Mapping[str, Any]
) -> tuple[Any, ...]:
    """读取课程下的知识库上下文，不触碰未就绪的摄取链路。"""

    with get_session_factory()() as session:
        knowledge_bases = KnowledgeBaseService(session).list_knowledge_bases(
            course_id=course_id,
            teacher_id=state.get("user_id"),
        )
    return _knowledge_base_context(knowledge_bases, selected_id)


def create_knowledge_base(
    name: str,
    description: str | None,
    course_id: str,
    state: Mapping[str, Any],
) -> tuple[Any, ...]:
    """在当前选中课程下创建知识库元数据。"""

    try:
        _ensure_teacher(state)
        if not course_id.strip():
            raise ValueError("请先选择课程。")
        with get_session_factory()() as session:
            created = KnowledgeBaseService(session).create_knowledge_base(
                course_id,
                name,
                description or None,
                teacher_id=state.get("user_id"),
            )
        return (
            *_knowledge_base_action_context(course_id, created.id, state),
            feedback("知识库已创建。", "success"),
        )
    except (
        PermissionDeniedError,
        CourseServiceError,
        KnowledgeBaseServiceError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return (*_empty_knowledge_base_context(), _format_error(error))


def update_knowledge_base(
    knowledge_base_id: str,
    name: str,
    description: str | None,
    course_id: str,
    state: Mapping[str, Any],
) -> tuple[Any, ...]:
    """更新当前选中知识库的元数据。"""

    try:
        _ensure_teacher(state)
        if not knowledge_base_id.strip():
            raise ValueError("请先选择知识库。")
        with get_session_factory()() as session:
            updated = KnowledgeBaseService(session).update_knowledge_base(
                knowledge_base_id,
                name=name,
                description=description,
                teacher_id=state.get("user_id"),
            )
        return (
            *_knowledge_base_action_context(course_id, updated.id, state),
            feedback("知识库已保存。", "success"),
        )
    except (
        PermissionDeniedError,
        CourseServiceError,
        KnowledgeBaseServiceError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return (*_empty_knowledge_base_context(), _format_error(error))


def delete_knowledge_base(
    knowledge_base_id: str, course_id: str, state: Mapping[str, Any]
) -> tuple[Any, ...]:
    """删除知识库元数据，调用前由共享确认组件二次确认。"""

    try:
        _ensure_teacher(state)
        if not knowledge_base_id.strip():
            raise ValueError("请先选择知识库。")
        with get_session_factory()() as session:
            KnowledgeBaseService(session).delete_knowledge_base(
                knowledge_base_id,
                teacher_id=state.get("user_id"),
            )
        return (
            *_knowledge_base_action_context(course_id, None, state),
            feedback("知识库已删除。", "success"),
        )
    except (
        PermissionDeniedError,
        CourseServiceError,
        KnowledgeBaseServiceError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return (*_empty_knowledge_base_context(), _format_error(error))


def delete_course(
    course_id: str, search: str | None, state: Mapping[str, Any]
) -> tuple[Any, ...]:
    """删除课程元数据，调用前由共享确认组件二次确认。"""

    try:
        _ensure_teacher(state)
        if not course_id.strip():
            raise ValueError("请先选择课程。")
        with get_session_factory()() as session:
            CourseService(session).delete_course(
                course_id,
                teacher_id=state.get("user_id"),
            )
        rows, records, _ = _list_courses(search, state)
        return (
            rows,
            records,
            _course_picker_update(records),
            "",
            "",
            "",
            empty_state("请先选择课程。"),
            *_empty_knowledge_base_context(),
            feedback("课程已删除。", "success"),
        )
    except (
        PermissionDeniedError,
        CourseServiceError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return (
            [],
            [],
            _course_picker_update([]),
            "",
            "",
            "",
            empty_state("请先选择课程。"),
            *_empty_knowledge_base_context(),
            _format_error(error),
        )


def create_knowledge_base_view(session_state: Any | None = None) -> KnowledgeBaseView:
    """创建课程管理和知识库工作区。"""

    state = session_state or gr.State(_empty_state())
    with gr.Column(visible=False) as panel:
        gr.Markdown("## 课程与知识库")
        course_records = gr.State([])
        selected_course_id = gr.Textbox(visible=False, container=False)
        selected_knowledge_base_id = gr.Textbox(visible=False, container=False)
        with gr.Row():
            course_search = gr.Textbox(
                label="搜索课程",
                placeholder="按课程名称或简介搜索",
                scale=2,
            )
            refresh_button = gr.Button("刷新课程", variant="secondary", scale=0)
            new_course_button = gr.Button("新建课程", variant="primary", scale=0)
            course_picker = gr.Dropdown(
                choices=[],
                value=None,
                label="当前课程",
                scale=1,
                interactive=False,
            )
            knowledge_bases_dropdown = gr.Dropdown(
                choices=[],
                value=None,
                label="当前知识库",
                scale=1,
                interactive=False,
            )

        with gr.Row():
            with gr.Column(scale=65, min_width=0):
                courses_table = gr.Dataframe(
                    headers=list(COURSE_TABLE_HEADERS),
                    datatype=["str", "str", "str", "str"],
                    value=[],
                    interactive=False,
                    label="课程列表",
                    **table_options(COURSE_TABLE_HEADERS),
                )
            with gr.Column(scale=35, min_width=0):
                gr.Markdown("### 课程详情")
                course_name = gr.Textbox(label="课程名称")
                course_description = gr.Textbox(label="课程简介", lines=4)
                course_detail = gr.Markdown(empty_state("请先选择课程。"))
                with gr.Row():
                    create_course_button = gr.Button("保存为新课程", variant="primary")
                    update_course_button = gr.Button("保存课程")
                    delete_course_button = gr.Button("删除课程", variant="stop")

                gr.Markdown("### 知识库详情")
                knowledge_base_name = gr.Textbox(label="知识库名称")
                knowledge_base_description = gr.Textbox(label="知识库简介", lines=3)
                knowledge_base_detail = gr.Markdown(empty_state("请先选择知识库。"))
                with gr.Row():
                    create_knowledge_base_button = gr.Button(
                        "新建知识库", variant="primary"
                    )
                    update_knowledge_base_button = gr.Button("保存知识库")
                    delete_knowledge_base_button = gr.Button(
                        "删除知识库", variant="stop"
                    )

        gr.Markdown("### 课程资料")
        with gr.Row():
            gr.File(
                label="上传资料（功能暂未就绪）",
                file_types=[".pdf", ".txt", ".md"],
                interactive=False,
                scale=2,
            )
            gr.Button(
                "上传资料（暂未就绪）",
                variant="secondary",
                interactive=False,
                scale=0,
            )
        gr.Markdown(_documents_unavailable())
        documents_table = gr.Dataframe(
            headers=list(DOCUMENT_TABLE_HEADERS),
            datatype=["str", "str", "markdown", "str"],
            value=[],
            interactive=False,
            label="资料处理状态",
            **table_options(DOCUMENT_TABLE_HEADERS),
        )
        gr.Markdown(_document_status_legend())
        with gr.Accordion("来源详情与失败原因", open=False):
            gr.Markdown(_source_unavailable())
        message = gr.Markdown(empty_state("请刷新课程列表。"))

        refresh_button.click(
            refresh_courses,
            inputs=[course_search, state],
            outputs=[courses_table, course_records, course_picker, message],
            show_progress="hidden",
        )
        new_course_button.click(
            start_new_course,
            outputs=[
                selected_course_id,
                course_picker,
                course_name,
                course_description,
                course_detail,
                knowledge_bases_dropdown,
                selected_knowledge_base_id,
                knowledge_base_name,
                knowledge_base_description,
                knowledge_base_detail,
                documents_table,
                message,
            ],
            show_progress="hidden",
        )
        create_course_button.click(
            create_course,
            inputs=[course_name, course_description, course_search, state],
            outputs=[courses_table, course_records, course_picker, message],
            show_progress="hidden",
        )
        update_course_button.click(
            update_course,
            inputs=[
                selected_course_id,
                course_name,
                course_description,
                course_search,
                state,
            ],
            outputs=[courses_table, course_records, course_picker, message],
            show_progress="hidden",
        )
        courses_table.select(
            select_course_row,
            inputs=[course_records, state],
            outputs=[
                selected_course_id,
                course_picker,
                course_name,
                course_description,
                course_detail,
                knowledge_bases_dropdown,
                selected_knowledge_base_id,
                knowledge_base_name,
                knowledge_base_description,
                knowledge_base_detail,
                documents_table,
                message,
            ],
            show_progress="hidden",
        )
        course_picker.change(
            select_course_by_id,
            inputs=[course_picker, course_records, state],
            outputs=[
                selected_course_id,
                course_picker,
                course_name,
                course_description,
                course_detail,
                knowledge_bases_dropdown,
                selected_knowledge_base_id,
                knowledge_base_name,
                knowledge_base_description,
                knowledge_base_detail,
                documents_table,
                message,
            ],
            show_progress="hidden",
        )
        knowledge_bases_dropdown.change(
            select_knowledge_base,
            inputs=[knowledge_bases_dropdown, selected_course_id, state],
            outputs=[
                knowledge_bases_dropdown,
                selected_knowledge_base_id,
                knowledge_base_name,
                knowledge_base_description,
                knowledge_base_detail,
                documents_table,
                message,
            ],
            show_progress="hidden",
        )
        create_knowledge_base_button.click(
            create_knowledge_base,
            inputs=[
                knowledge_base_name,
                knowledge_base_description,
                selected_course_id,
                state,
            ],
            outputs=[
                knowledge_bases_dropdown,
                selected_knowledge_base_id,
                knowledge_base_name,
                knowledge_base_description,
                knowledge_base_detail,
                documents_table,
                message,
            ],
            show_progress="hidden",
        )
        update_knowledge_base_button.click(
            update_knowledge_base,
            inputs=[
                selected_knowledge_base_id,
                knowledge_base_name,
                knowledge_base_description,
                selected_course_id,
                state,
            ],
            outputs=[
                knowledge_bases_dropdown,
                selected_knowledge_base_id,
                knowledge_base_name,
                knowledge_base_description,
                knowledge_base_detail,
                documents_table,
                message,
            ],
            show_progress="hidden",
        )
        bind_confirmation(
            delete_course_button,
            action="删除课程",
            target=selected_course_id,
            callback=delete_course,
            inputs=[selected_course_id, course_search, state],
            outputs=[
                courses_table,
                course_records,
                course_picker,
                selected_course_id,
                course_name,
                course_description,
                course_detail,
                knowledge_bases_dropdown,
                selected_knowledge_base_id,
                knowledge_base_name,
                knowledge_base_description,
                knowledge_base_detail,
                documents_table,
                message,
            ],
        )
        bind_confirmation(
            delete_knowledge_base_button,
            action="删除知识库",
            target=selected_knowledge_base_id,
            callback=delete_knowledge_base,
            inputs=[selected_knowledge_base_id, selected_course_id, state],
            outputs=[
                knowledge_bases_dropdown,
                selected_knowledge_base_id,
                knowledge_base_name,
                knowledge_base_description,
                knowledge_base_detail,
                documents_table,
                message,
            ],
        )

    return KnowledgeBaseView(
        panel=panel,
        courses_table=courses_table,
        knowledge_bases_dropdown=knowledge_bases_dropdown,
        documents_table=documents_table,
        message=message,
    )


build_knowledge_base_view = create_knowledge_base_view


__all__ = [
    "COURSE_TABLE_HEADERS",
    "DOCUMENT_STATUS_LABELS",
    "DOCUMENT_TABLE_HEADERS",
    "INGESTION_READY",
    "KnowledgeBaseView",
    "build_knowledge_base_view",
    "create_course",
    "create_knowledge_base",
    "create_knowledge_base_view",
    "delete_course",
    "delete_knowledge_base",
    "refresh_courses",
    "select_course_by_id",
    "select_course_row",
    "select_knowledge_base",
    "update_course",
    "update_knowledge_base",
]
