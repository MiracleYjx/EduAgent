"""学生成绩与教师学情视图。

成绩、复核和诊断服务尚未就绪时，仅展示明确空态，不在前端计算或伪造结果。
教师结果页保留后端查询契约所需的筛选、选中对象和复核上下文边界，待契约接通后
由服务返回最终成绩、诊断和知识点数据。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

import gradio as gr

from backend.app.domain.enums import UserRole
from backend.app.domain.permissions import PermissionDeniedError, normalize_role
from backend.app.ui.layout_view import empty_state, feedback, status_text

RESULT_HEADERS = ("题号", "状态", "得分", "反馈")
TEACHER_RESULT_HEADERS = ("学生", "总分", "结果状态", "待复核数")
KNOWLEDGE_POINT_HEADERS = ("知识点", "掌握度")
TEACHER_RESULT_DATATYPES = cast(
    tuple[Literal["str"], ...], ("str",) * len(TEACHER_RESULT_HEADERS)
)
RESULT_TABLE_DATATYPES = cast(
    tuple[Literal["str"], ...], ("str",) * len(RESULT_HEADERS)
)
UNAVAILABLE_MESSAGE = "成绩与诊断功能暂未就绪，请等待 M3 阶段完成"
TEACHER_UNAVAILABLE_MESSAGE = "教师成绩与学情功能暂未就绪，请等待 M3 阶段完成"
TEACHER_STUDENT_EMPTY_MESSAGE = "暂无可展示的学生成绩"
TEACHER_DIAGNOSIS_EMPTY_MESSAGE = "暂无可展示的诊断摘要"
TEACHER_KNOWLEDGE_EMPTY_MESSAGE = "暂无可展示的知识点分布图"

# 结果状态属于成绩汇总契约，不能直接把 Graded 当作最终成绩。
_RESULT_STATUS_PRESENTATIONS: Mapping[str, tuple[str, str]] = {
    "final": ("✓", "最终成绩"),
    "reviewed": ("✓", "最终成绩"),
    "confirmed": ("✓", "已确认，待形成最终成绩"),
    "pending review": ("⚠", "待人工复核"),
    "pending_review": ("⚠", "待人工复核"),
    "graded": ("✓", "已评分，尚未最终确认"),
    "submitted": ("◷", "已提交，待批阅"),
    "failed": ("✕", "处理失败"),
}

# T110 以考试、答卷和题目定位复核项；答案标识有则随上下文传递，没有也不阻断入口。
_REVIEW_CONTEXT_KEYS = ("exam_id", "submission_id", "question_id")


@dataclass(frozen=True)
class ResultsView:
    """学生成绩视图中由主工作台控制的组件。"""

    panel: gr.Column
    results_table: gr.Dataframe
    message: gr.Markdown


@dataclass(frozen=True)
class TeacherResultsView:
    """教师成绩视图中由主工作台控制的组件。"""

    panel: gr.Column
    course: gr.Dropdown
    exam: gr.Dropdown
    results_table: gr.Dataframe
    result_records: gr.State
    submitted_summary: gr.Textbox
    final_summary: gr.Textbox
    pending_summary: gr.Textbox
    average_summary: gr.Textbox
    student_empty: gr.Markdown
    selected_student: gr.Markdown
    diagnosis: gr.Markdown
    knowledge_plot: gr.Plot
    knowledge_plot_empty: gr.Markdown
    knowledge_table: gr.Dataframe
    knowledge_table_empty: gr.Markdown
    review_context: gr.State
    review_button: gr.Button
    message: gr.Markdown


def _ensure_student(state: Mapping[str, Any]) -> None:
    """只允许已登录学生读取自己的结果。"""

    if not state.get("access_token"):
        raise PermissionDeniedError("请先登录。")
    try:
        roles = {normalize_role(role) for role in state.get("roles", [])}
    except (TypeError, ValueError) as error:
        raise PermissionDeniedError("当前账号无权访问成绩与诊断功能。") from error
    if UserRole.STUDENT not in roles:
        raise PermissionDeniedError("当前账号无权访问成绩与诊断功能。")


def refresh_student_results(
    exam_id: str | None = None, state: Mapping[str, Any] | None = None
) -> tuple[list[list[str]], str]:
    """读取学生授权结果；依赖未就绪时返回空态。"""

    if state is not None:
        try:
            _ensure_student(state)
        except PermissionDeniedError as error:
            return [], feedback(str(error), "error")
    return [], empty_state(UNAVAILABLE_MESSAGE)


def _value(item: Mapping[str, Any] | Any, name: str, default: Any = "") -> Any:
    """从服务 DTO 或映射中读取字段，不改变服务返回值。"""

    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _first_value(
    item: Mapping[str, Any] | Any,
    names: Sequence[str],
    default: Any = "",
) -> Any:
    """按兼容字段名读取第一个已提供的值。"""

    for name in names:
        value = _value(item, name, None)
        if value not in (None, ""):
            return value
    return default


def _result_status_code(value: Any) -> str:
    """将结果状态转换为比较用文本，不根据分数推导状态。"""

    raw = getattr(value, "value", value)
    return str(raw or "").strip()


def result_status_text(value: Any) -> str:
    """使用统一中文状态映射显示结果状态。"""

    code = _result_status_code(value)
    presentation = _RESULT_STATUS_PRESENTATIONS.get(code.casefold())
    if presentation is not None:
        icon, label = presentation
        return f"{icon} {label}"
    return status_text(value, entity="grading")


def _is_final_result(value: Any) -> bool:
    """只识别服务明确返回的最终结果状态。"""

    return _result_status_code(value).casefold() in {
        "final",
        "reviewed",
    }


def _is_pending_review(value: Any) -> bool:
    """只识别服务明确返回的待复核状态。"""

    return _result_status_code(value).casefold() in {
        "pending review",
        "pending_review",
    }


def _display_value(value: Any, fallback: str = "未提供") -> str:
    """把后端已提供的值转成安全的表格文本；缺失值不转换成零。"""

    if value is None or value == "":
        return fallback
    return str(value)


def teacher_result_rows(
    records: Sequence[Mapping[str, Any] | Any],
) -> list[list[str]]:
    """把教师查询契约返回的学生结果转换成表格行。

    总分和待复核数必须由服务提供；此函数不按单题结果重新汇总，也不计算平均分。
    """

    rows: list[list[str]] = []
    for record in records:
        status = _first_value(record, ("result_status", "status"), None)
        final_score = _first_value(record, ("final_score", "total_score"), None)
        pending_count = _first_value(
            record,
            ("pending_review_count", "pending_count", "review_count"),
            None,
        )
        rows.append(
            [
                _display_value(_first_value(record, ("student_name", "student"), None)),
                (
                    _display_value(final_score)
                    if _is_final_result(status)
                    else "最终成绩未形成"
                ),
                result_status_text(status),
                _display_value(pending_count),
            ]
        )
    return rows


def review_context_for_record(
    record: Mapping[str, Any] | Any,
) -> dict[str, Any]:
    """提取进入 T110 所需的考试、答卷、答案和题目上下文。"""

    nested = _first_value(record, ("review_context", "context"), None)

    def context_value(names: Sequence[str]) -> str | None:
        value = _first_value(nested, names, None) if nested is not None else None
        if value in (None, ""):
            value = _first_value(record, names, None)
        if isinstance(value, Mapping):
            value = _first_value(value, ("id", "value"), None)
        else:
            value = getattr(value, "id", value)
        if value in (None, "") or isinstance(value, (Mapping, list, tuple, set)):
            return None
        return str(value)

    context: dict[str, Any] = {}
    aliases = {
        "exam_id": ("exam_id", "exam"),
        "submission_id": ("submission_id", "submission"),
        "answer_id": ("answer_id", "grading_result_id", "result_id"),
        "question_id": ("question_id", "question"),
        "student_id": ("student_id",),
        "student_name": ("student_name", "student"),
        "question_number": ("question_number", "question_no"),
    }
    for key, names in aliases.items():
        value = context_value(names)
        if value is not None:
            context[key] = value
    return context


def _has_review_context(context: Mapping[str, Any] | None) -> bool:
    """确认复核入口携带了全部必要的实体标识。"""

    if not context:
        return False
    return all(str(context.get(key, "")).strip() for key in _REVIEW_CONTEXT_KEYS)


def review_context_is_complete(context: Mapping[str, Any] | None) -> bool:
    """公开复核入口校验，供共享工作台导航使用。"""

    return _has_review_context(context)


def _record_has_pending_review(record: Mapping[str, Any] | Any) -> bool:
    """读取服务提供的待复核标记或数量，不在前端重新判断评分。"""

    status = _first_value(record, ("result_status", "status"), None)
    if _is_pending_review(status):
        return True
    count = _first_value(
        record,
        ("pending_review_count", "pending_count", "review_count"),
        None,
    )
    return isinstance(count, int) and not isinstance(count, bool) and count > 0


def _empty_teacher_panel_values() -> tuple[Any, ...]:
    """返回教师查询未就绪时所有区域的初始值。"""

    return (
        [],
        [],
        "暂无",
        "暂无",
        "暂无",
        "暂无",
        gr.update(visible=True),
        empty_state("尚未选择学生。"),
        empty_state(TEACHER_DIAGNOSIS_EMPTY_MESSAGE),
        None,
        gr.update(visible=True),
        [],
        gr.update(visible=True),
        None,
        gr.update(interactive=False),
        empty_state(TEACHER_UNAVAILABLE_MESSAGE),
    )


def create_results_view(session_state: Any | None = None) -> ResultsView:
    """创建考试选择、结果摘要、逐题结果和诊断区域。"""

    state = session_state or gr.State({"access_token": "", "roles": []})
    with gr.Column(visible=False, elem_classes="edu-results") as panel:
        gr.HTML(
            "<style>.edu-results .result-summary {min-height:74px;}"
            ".edu-results .result-tabs {min-height:320px;}"
            "@media(max-width:767px){.edu-results .result-summary-row{flex-wrap:wrap;}}</style>"
        )
        gr.Markdown("## 成绩与诊断")
        with gr.Row():
            exam = gr.Dropdown(label="考试", choices=[], value=None)
            refresh = gr.Button("刷新结果", variant="primary")
        gr.Markdown(empty_state(UNAVAILABLE_MESSAGE))
        with gr.Row(elem_classes="result-summary-row"):
            gr.Textbox(
                label="总分",
                value="暂无",
                interactive=False,
                elem_classes="result-summary",
            )
            gr.Textbox(
                label="已评分题数",
                value="暂无",
                interactive=False,
                elem_classes="result-summary",
            )
            gr.Textbox(
                label="待复核题数",
                value="暂无",
                interactive=False,
                elem_classes="result-summary",
            )
        with gr.Tabs(elem_classes="result-tabs"):
            with gr.Tab("逐题结果"):
                results_table = gr.Dataframe(
                    headers=list(RESULT_HEADERS),
                    datatype=RESULT_TABLE_DATATYPES,
                    value=[],
                    interactive=False,
                    label="逐题结果",
                )
                gr.Markdown(empty_state("暂无逐题结果可展示。"))
            with gr.Tab("错题与诊断"), gr.Row(equal_height=False):
                with gr.Column(scale=1):
                    gr.Markdown("### 薄弱知识点")
                    gr.Markdown(empty_state("暂无可展示的掌握度数据。"))
                with gr.Column(scale=1):
                    gr.Markdown("### 错误原因与学习建议")
                    gr.Markdown(empty_state("诊断报告尚未生成"))
        message = gr.Markdown(empty_state("暂无可展示的诊断"))
        refresh.click(
            refresh_student_results,
            inputs=[exam, state],
            outputs=[results_table, message],
            show_progress="hidden",
        )
    return ResultsView(panel=panel, results_table=results_table, message=message)


def _ensure_teacher(state: Mapping[str, Any]) -> None:
    """只允许已登录教师读取授权范围内的成绩汇总。"""

    if not state.get("access_token"):
        raise PermissionDeniedError("请先登录。")
    try:
        roles = {normalize_role(role) for role in state.get("roles", [])}
    except (TypeError, ValueError) as error:
        raise PermissionDeniedError("当前账号无权访问教师成绩与学情功能。") from error
    if UserRole.TEACHER not in roles:
        raise PermissionDeniedError("当前账号无权访问教师成绩与学情功能。")


def refresh_teacher_results(
    course_id: str | None = None,
    exam_id: str | None = None,
    state: Mapping[str, Any] | None = None,
) -> tuple[list[list[str]], str]:
    """读取教师授权的学生成绩；查询契约未就绪时只返回空态。"""

    records, availability = _load_teacher_result_records(course_id, exam_id, state)
    return teacher_result_rows(records), availability


def _load_teacher_result_records(
    course_id: str | None,
    exam_id: str | None,
    state: Mapping[str, Any] | None,
) -> tuple[list[Mapping[str, Any] | Any], str]:
    """保留教师查询契约的唯一接线点；契约未就绪时不访问数据库。"""

    if state is not None:
        try:
            _ensure_teacher(state)
        except PermissionDeniedError as error:
            return [], feedback(str(error), "error")
    return [], empty_state(TEACHER_UNAVAILABLE_MESSAGE)


def create_teacher_results_view(
    session_state: Any | None = None,
) -> TeacherResultsView:
    """创建教师成绩、诊断和知识点分析工作台。"""

    state = session_state or gr.State({"access_token": "", "roles": []})
    with gr.Column(visible=False, elem_classes="edu-teacher-results") as panel:
        gr.HTML(
            "<style>"
            ".edu-teacher-results .teacher-summary {min-height:74px;}"
            ".edu-teacher-results .teacher-summary-row {gap:12px;}"
            ".edu-teacher-results .teacher-main {min-height:340px;align-items:stretch;}"
            ".edu-teacher-results .teacher-table-column,"
            ".edu-teacher-results .teacher-diagnosis-column,"
            ".edu-teacher-results .teacher-knowledge-column {min-width:0;}"
            ".edu-teacher-results .teacher-table {min-width:0;}"
            ".edu-teacher-results .teacher-review-entry {min-height:44px;}"
            ".edu-teacher-results .teacher-knowledge-empty {min-height:120px;}"
            "@media(max-width:1023px){"
            ".edu-teacher-results .teacher-main {flex-wrap:wrap;}"
            ".edu-teacher-results .teacher-table-column,"
            ".edu-teacher-results .teacher-diagnosis-column {flex:1 1 100% !important;}"
            "}"
            "@media(max-width:767px){"
            ".edu-teacher-results .teacher-summary-row {flex-wrap:wrap;}"
            ".edu-teacher-results .teacher-summary {flex:1 1 45%;}"
            "}"
            "</style>"
        )
        with gr.Row(equal_height=False):
            with gr.Column(scale=1, min_width=0):
                gr.Markdown("## 教师成绩与学情")
                gr.Markdown("课程 / 考试 / 学生成绩与诊断")
            refresh = gr.Button("刷新成绩", variant="primary", scale=0)
        with gr.Row(elem_classes="teacher-filters"):
            course = gr.Dropdown(
                label="课程",
                choices=[],
                value=None,
                interactive=False,
                info="教师查询契约就绪后提供课程筛选。",
            )
            exam = gr.Dropdown(
                label="考试",
                choices=[],
                value=None,
                interactive=False,
                info="教师查询契约就绪后提供考试筛选。",
            )
        message = gr.Markdown(empty_state(TEACHER_UNAVAILABLE_MESSAGE))
        with gr.Row(elem_classes="teacher-summary-row"):
            submitted_summary = gr.Textbox(
                label="已提交数",
                value="暂无",
                interactive=False,
                elem_classes="teacher-summary",
            )
            final_summary = gr.Textbox(
                label="最终成绩数",
                value="暂无",
                interactive=False,
                elem_classes="teacher-summary",
            )
            pending_summary = gr.Textbox(
                label="待复核数",
                value="暂无",
                interactive=False,
                elem_classes="teacher-summary",
            )
            average_summary = gr.Textbox(
                label="最终成绩平均分",
                value="暂无",
                interactive=False,
                elem_classes="teacher-summary",
            )
        gr.Markdown("平均分仅统计服务返回的最终成绩，待复核分数不计入。")
        result_records = gr.State([])
        review_context = gr.State(None)
        with gr.Row(equal_height=False, elem_classes="teacher-main"):
            with gr.Column(
                scale=65,
                min_width=0,
                elem_classes="teacher-table-column",
            ):
                gr.Markdown("### 学生成绩")
                results_table = gr.Dataframe(
                    headers=list(TEACHER_RESULT_HEADERS),
                    datatype=TEACHER_RESULT_DATATYPES,
                    value=[],
                    interactive=False,
                    label="学生成绩（学生、总分、结果状态、待复核数）",
                    wrap=True,
                    max_height=420,
                    column_widths=[180, 120, 190, 120],
                    elem_classes="teacher-table",
                )
                student_empty = gr.Markdown(empty_state(TEACHER_STUDENT_EMPTY_MESSAGE))
                review_button = gr.Button(
                    "进入阅卷复核（携带考试/答卷/题目）",
                    interactive=False,
                    elem_classes="teacher-review-entry",
                )
                gr.Markdown(
                    "选择含待复核结果的学生后，可携带考试、答卷和题目上下文进入 T110。"
                )
            with gr.Column(
                scale=35,
                min_width=0,
                elem_classes="teacher-diagnosis-column",
            ):
                gr.Markdown("### 所选学生诊断摘要")
                selected_student = gr.Markdown(empty_state("尚未选择学生。"))
                diagnosis = gr.Markdown(empty_state(TEACHER_DIAGNOSIS_EMPTY_MESSAGE))
                gr.Markdown("诊断只展示服务返回的已确认结果；待复核结果不会生成诊断。")
        with gr.Row(equal_height=False):
            with gr.Column(
                scale=55,
                min_width=0,
                elem_classes="teacher-knowledge-column",
            ):
                gr.Markdown("### 知识点分布图")
                knowledge_plot_empty = gr.Markdown(
                    empty_state(TEACHER_KNOWLEDGE_EMPTY_MESSAGE),
                    elem_classes="teacher-knowledge-empty",
                )
                knowledge_plot = gr.Plot(
                    value=None,
                    label="知识点掌握度（仅展示真实服务数据）",
                    visible=False,
                )
            with gr.Column(scale=45, min_width=0):
                gr.Markdown("### 知识点分布数据")
                knowledge_table = gr.Dataframe(
                    headers=list(KNOWLEDGE_POINT_HEADERS),
                    datatype=("str", "number"),
                    value=[],
                    interactive=False,
                    label="知识点分布表",
                )
                knowledge_table_empty = gr.Markdown(
                    empty_state("暂无可展示的知识点分布数据。")
                )

        def refresh_panel(
            course_id: str | None,
            exam_id: str | None,
            current_state: Mapping[str, Any],
        ) -> tuple[Any, ...]:
            """刷新教师结果工作台，并在契约未就绪时恢复所有空态。"""

            records, availability = _load_teacher_result_records(
                course_id, exam_id, current_state
            )
            values = list(_empty_teacher_panel_values())
            values[0] = teacher_result_rows(records)
            values[1] = list(records)
            values[6] = gr.update(visible=not bool(records))
            values[-1] = availability
            return tuple(values)

        def select_result(
            records: Sequence[Mapping[str, Any] | Any],
            current_state: Mapping[str, Any],
            event: gr.SelectData,
        ) -> tuple[Any, ...]:
            """绑定学生行和复核上下文，不把内部标识显示在成绩表中。"""

            try:
                _ensure_teacher(current_state)
                index = (
                    event.index[0]
                    if isinstance(event.index, (tuple, list))
                    else event.index
                )
                if (
                    not event.selected
                    or not isinstance(index, int)
                    or not 0 <= index < len(records)
                ):
                    raise ValueError("学生选择已失效，请重新选择。")
                record = records[index]
                context = review_context_for_record(record)
                is_reviewable = _record_has_pending_review(
                    record
                ) and _has_review_context(context)
                student_name = _first_value(record, ("student_name", "student"), "")
                return (
                    f"**当前学生：** {_display_value(student_name)}",
                    empty_state(TEACHER_DIAGNOSIS_EMPTY_MESSAGE),
                    context if context else None,
                    gr.update(interactive=is_reviewable),
                    (
                        feedback(
                            "已选择学生；待复核项将使用服务返回的考试、答卷和题目上下文。",
                            "info",
                        )
                        if is_reviewable
                        else feedback(
                            "当前学生暂无可进入阅卷复核的完整待复核结果。",
                            "info",
                        )
                    ),
                )
            except (PermissionDeniedError, TypeError, ValueError) as error:
                return (
                    empty_state("尚未选择学生。"),
                    empty_state(TEACHER_DIAGNOSIS_EMPTY_MESSAGE),
                    None,
                    gr.update(interactive=False),
                    feedback(str(error), "error"),
                )

        refresh_outputs = [
            results_table,
            result_records,
            submitted_summary,
            final_summary,
            pending_summary,
            average_summary,
            student_empty,
            selected_student,
            diagnosis,
            knowledge_plot,
            knowledge_plot_empty,
            knowledge_table,
            knowledge_table_empty,
            review_context,
            review_button,
            message,
        ]
        results_table.select(
            select_result,
            inputs=[result_records, state],
            outputs=[
                selected_student,
                diagnosis,
                review_context,
                review_button,
                message,
            ],
            show_progress="hidden",
        )
        refresh.click(
            refresh_panel,
            inputs=[course, exam, state],
            outputs=refresh_outputs,
            show_progress="hidden",
        )
    return TeacherResultsView(
        panel=panel,
        course=course,
        exam=exam,
        results_table=results_table,
        result_records=result_records,
        submitted_summary=submitted_summary,
        final_summary=final_summary,
        pending_summary=pending_summary,
        average_summary=average_summary,
        student_empty=student_empty,
        selected_student=selected_student,
        diagnosis=diagnosis,
        knowledge_plot=knowledge_plot,
        knowledge_plot_empty=knowledge_plot_empty,
        knowledge_table=knowledge_table,
        knowledge_table_empty=knowledge_table_empty,
        review_context=review_context,
        review_button=review_button,
        message=message,
    )


build_results_view = create_results_view
build_teacher_results_view = create_teacher_results_view

__all__ = [
    "KNOWLEDGE_POINT_HEADERS",
    "RESULT_HEADERS",
    "RESULT_TABLE_DATATYPES",
    "TEACHER_DIAGNOSIS_EMPTY_MESSAGE",
    "TEACHER_KNOWLEDGE_EMPTY_MESSAGE",
    "TEACHER_RESULT_DATATYPES",
    "TEACHER_RESULT_HEADERS",
    "TEACHER_STUDENT_EMPTY_MESSAGE",
    "TEACHER_UNAVAILABLE_MESSAGE",
    "UNAVAILABLE_MESSAGE",
    "ResultsView",
    "TeacherResultsView",
    "build_results_view",
    "build_teacher_results_view",
    "create_results_view",
    "create_teacher_results_view",
    "refresh_student_results",
    "refresh_teacher_results",
    "result_status_text",
    "review_context_for_record",
    "review_context_is_complete",
    "teacher_result_rows",
]
