"""学生考试和答卷提交 Gradio 视图。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast
from uuid import UUID

import gradio as gr
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import selectinload

from backend.app.core.database import get_session_factory
from backend.app.domain.enums import QuestionType, SubmissionStatus, UserRole
from backend.app.domain.permissions import PermissionDeniedError, normalize_role
from backend.app.models import Exam
from backend.app.services.submission_service import (
    AnswerContent,
    AnswerSummary,
    AvailableExamSummary,
    SubmissionConflictError,
    SubmissionNotAvailableError,
    SubmissionNotFoundError,
    SubmissionPermissionError,
    SubmissionService,
    SubmissionServiceError,
    SubmissionSummary,
    SubmissionValidationError,
)
from backend.app.ui.layout_view import (
    empty_state,
    feedback,
    question_indicator,
    status_badge,
    status_label,
    table_options,
)

EXAM_TABLE_HEADERS = (
    "考试 ID",
    "考试标题",
    "状态",
    "时长（分钟）",
    "题目数量",
    "总分",
    "开始时间",
    "结束时间",
)
EXAM_TABLE_DATATYPES = cast(
    tuple[Literal["str"], ...],
    ("str",) * len(EXAM_TABLE_HEADERS),
)
QUESTION_TABLE_HEADERS = (
    "题号",
    "题目 ID",
    "题型",
    "题目内容",
    "选项",
    "分值",
    "知识点",
)
QUESTION_TABLE_DATATYPES = cast(
    tuple[Literal["str"], ...],
    ("str",) * len(QUESTION_TABLE_HEADERS),
)
ANSWER_TABLE_HEADERS = ("题目 ID", "答案", "处理状态")
ANSWER_TABLE_DATATYPES = cast(
    tuple[Literal["str"], ...],
    ("str",) * len(ANSWER_TABLE_HEADERS),
)
_GENERIC_ERROR = "考试操作失败，请稍后重试。"


@dataclass(frozen=True)
class StudentExamView:
    """学生考试视图中由主应用控制的组件集合。"""

    panel: gr.Column
    exams_table: gr.Dataframe
    questions_table: gr.Dataframe
    answers_table: gr.Dataframe
    submission_id: gr.Textbox
    answers_input: gr.JSON
    message: gr.Markdown

    @property
    def available_exams_table(self) -> gr.Dataframe:
        """兼容按“可参加考试”命名的调用方。"""

        return self.exams_table

    @property
    def answer_input(self) -> gr.JSON:
        """兼容单数答案输入命名。"""

        return self.answers_input


def _empty_state() -> dict[str, Any]:
    """返回可供独立视图使用的空登录状态。"""

    return {"access_token": "", "roles": [], "user_id": "", "username": ""}


def _ensure_student(state: Mapping[str, Any]) -> None:
    """检查 Gradio 会话是否已登录且拥有学生角色。"""

    if not state.get("access_token"):
        raise PermissionDeniedError("请先登录。")
    try:
        roles = {normalize_role(role) for role in state.get("roles", [])}
    except (TypeError, ValueError) as exc:
        raise PermissionDeniedError("当前账号无权访问学生考试功能。") from exc
    if UserRole.STUDENT not in roles:
        raise PermissionDeniedError("当前账号无权访问学生考试功能。")


def _student_id(state: Mapping[str, Any]) -> str:
    """读取已登录学生标识。"""

    _ensure_student(state)
    value = state.get("user_id")
    if not value:
        raise PermissionDeniedError("登录状态缺少用户标识。")
    return str(value)


def _format_error(error: BaseException) -> str:
    """将内部异常转换成安全且易理解的中文提示。"""

    if isinstance(error, PermissionDeniedError):
        message = str(error) or "当前账号无权执行此操作。"
    elif isinstance(
        error,
        (
            SubmissionConflictError,
            SubmissionNotAvailableError,
            SubmissionNotFoundError,
            SubmissionPermissionError,
            SubmissionValidationError,
            SubmissionServiceError,
        ),
    ):
        message = str(error) or _GENERIC_ERROR
    elif isinstance(error, SQLAlchemyError):
        message = "系统暂时无法连接数据库，请稍后重试。"
    elif isinstance(error, (TypeError, ValueError, json.JSONDecodeError)):
        message = f"输入有误：{error or '请检查输入内容。'}"
    else:
        message = _GENERIC_ERROR
    return feedback(message, "error")


def _exam_rows(exams: Sequence[AvailableExamSummary]) -> list[list[str]]:
    """把可参加考试摘要转换为 Gradio 表格行。"""

    return [
        [
            exam.id,
            exam.title,
            status_badge(exam.status, entity="exam"),
            str(exam.duration_minutes or ""),
            str(exam.question_count),
            str(exam.total_score),
            exam.starts_at.isoformat() if exam.starts_at else "",
            exam.ends_at.isoformat() if exam.ends_at else "",
        ]
        for exam in exams
    ]


def _question_type_text(value: QuestionType | str) -> str:
    """把题型枚举或字符串转换为稳定显示文本。"""

    return status_label(value, entity="question_type")


def _question_rows(exam: Exam) -> list[list[str]]:
    """将考试题目转换为不含标准答案的表格行。"""

    rows: list[list[str]] = []
    for position, question in enumerate(exam.questions or (), start=1):
        options = question.options
        options_text = (
            ""
            if options is None
            else json.dumps(options, ensure_ascii=False, sort_keys=True)
        )
        rows.append(
            [
                str(position),
                str(question.id),
                _question_type_text(question.type),
                question.content,
                options_text,
                str(question.score),
                "、".join(question.knowledge_points or []),
            ]
        )
    return rows


def _answer_text(content: AnswerContent) -> str:
    """将答案 JSON 内容转换为表格可显示文本。"""

    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, sort_keys=True)


def _answer_rows(answers: Sequence[AnswerSummary]) -> list[list[str]]:
    """将答案摘要转换为学生可查看的处理状态表格。"""

    return [
        [
            answer.question_id,
            _answer_text(answer.content),
            status_badge(answer.status, entity="answer"),
        ]
        for answer in answers
    ]


def _parse_uuid(value: Any, field_name: str) -> str:
    """解析 Gradio 输入的 UUID 文本。"""

    if isinstance(value, UUID):
        return str(value)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name}不能为空。")
    try:
        return str(UUID(value.strip()))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name}无效。") from exc


def _normalize_content(value: Any) -> AnswerContent:
    """校验界面答案输入的 JSON 内容。"""

    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    if isinstance(value, dict) and all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        return dict(value)
    raise ValueError("答案必须是字符串、字符串列表、字符串对象或空值。")


def _parse_answers(value: Any) -> list[dict[str, Any]] | None:
    """解析答案编辑框，支持 JSON 对象、答案条目列表和 JSON 文本。"""

    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("答案不是有效的 JSON 数据。") from exc
    if isinstance(parsed, Mapping) and "answers" in parsed:
        parsed = parsed["answers"]
    if isinstance(parsed, Mapping):
        raw_entries = [
            {"question_id": question_id, "content": content}
            for question_id, content in parsed.items()
        ]
    elif isinstance(parsed, Sequence) and not isinstance(
        parsed,
        (str, bytes, bytearray),
    ):
        raw_entries = list(parsed)
    else:
        raise TypeError("答案必须是题目标识到答案的对象或答案条目列表。")

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_entries:
        if isinstance(item, Mapping):
            raw_question_id = item.get("question_id", item.get("question"))
            if raw_question_id is None:
                raise ValueError("答案条目缺少题目标识。")
            if "content" in item:
                raw_content = item["content"]
            elif "answer" in item:
                raw_content = item["answer"]
            elif "value" in item:
                raw_content = item["value"]
            else:
                raise ValueError("答案条目缺少答案内容。")
        elif (
            isinstance(item, Sequence)
            and not isinstance(item, (str, bytes, bytearray))
            and len(item) == 2
        ):
            raw_question_id, raw_content = item
        else:
            raise ValueError("答案条目格式无效。")
        question_id = _parse_uuid(raw_question_id, "题目标识")
        if question_id in seen:
            raise ValueError("答案列表不能包含重复题目。")
        seen.add(question_id)
        entries.append(
            {
                "question_id": question_id,
                "content": _normalize_content(raw_content),
            }
        )
    if not entries:
        raise ValueError("至少需要填写一道题的答案。")
    return entries


def _load_exam(
    service: SubmissionService,
    exam_id: str,
    student_id: str,
) -> tuple[Exam, SubmissionSummary]:
    """校验考试开放状态并加载题目和学生草稿。"""

    service.get_available_exam(exam_id, student_id=student_id)
    try:
        exam = service.session.scalar(
            select(Exam)
            .options(selectinload(Exam.questions))
            .where(Exam.id == UUID(exam_id))
        )
    except SQLAlchemyError as exc:
        raise SubmissionServiceError("无法读取考试题目。") from exc
    if exam is None:
        raise SubmissionNotFoundError("考试不存在。")
    submission = service.get_or_create_submission(
        exam_id,
        student_id,
        initialize_answers=True,
    )
    return exam, submission


def refresh_exams(state: Mapping[str, Any]) -> tuple[list[list[str]], str]:
    """刷新当前学生可参加的考试列表。"""

    try:
        student_id = _student_id(state)
        with get_session_factory()() as session:
            exams = SubmissionService(session).list_available_exams(
                student_id=student_id,
            )
        return _exam_rows(exams), (
            feedback(f"已加载 {len(exams)} 场可参加考试。", "success")
            if exams
            else empty_state("暂无可参加考试。")
        )
    except (
        PermissionDeniedError,
        SubmissionServiceError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return [], _format_error(error)


refresh_available_exams = refresh_exams
refresh_exam_list = refresh_exams


def start_exam(
    exam_id: str,
    state: Mapping[str, Any],
) -> tuple[list[list[str]], str, dict[str, Any], list[list[str]], str]:
    """打开考试并创建或复用当前学生的草稿答卷。"""

    try:
        student_id = _student_id(state)
        normalized_exam_id = _parse_uuid(exam_id, "考试 ID")
        with get_session_factory()() as session:
            service = SubmissionService(session)
            exam, submission = _load_exam(service, normalized_exam_id, student_id)
        answers = {
            answer.question_id: answer.content
            for answer in submission.answers
            if answer.content is not None
        }
        message = f"已打开考试“{exam.title}”，答卷状态为“{status_label(submission.status, entity='submission')}”。"
        if submission.status is not SubmissionStatus.DRAFT:
            message += "该答卷已提交，不能继续修改。"
        return (
            _question_rows(exam),
            submission.id,
            answers,
            _answer_rows(submission.answers),
            feedback(message, "success"),
        )
    except (
        PermissionDeniedError,
        SubmissionServiceError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return [], "", {}, [], _format_error(error)


open_exam = start_exam
load_exam = start_exam


def save_answers(
    submission_id: str,
    answers: Any,
    state: Mapping[str, Any],
) -> tuple[list[list[str]], str]:
    """保存草稿答卷答案并刷新答案状态表格。"""

    try:
        student_id = _student_id(state)
        normalized_submission_id = _parse_uuid(submission_id, "答卷 ID")
        parsed_answers = _parse_answers(answers)
        if parsed_answers is None:
            raise ValueError("请至少填写一道题的答案。")
        with get_session_factory()() as session:
            service = SubmissionService(session)
            service.save_answers(
                normalized_submission_id,
                parsed_answers,
                student_id=student_id,
            )
            submission = service.get_submission(
                normalized_submission_id,
                student_id=student_id,
            )
        return _answer_rows(submission.answers), feedback("答案已保存。", "success")
    except (
        PermissionDeniedError,
        SubmissionServiceError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return [], _format_error(error)


save_submission_answers = save_answers
save_draft_answers = save_answers


def submit_exam(
    submission_id: str,
    answers: Any,
    state: Mapping[str, Any],
) -> tuple[list[list[str]], str]:
    """提交当前学生答卷，并显示冻结后的答案状态。"""

    try:
        student_id = _student_id(state)
        normalized_submission_id = _parse_uuid(submission_id, "答卷 ID")
        parsed_answers = _parse_answers(answers)
        with get_session_factory()() as session:
            service = SubmissionService(session)
            submission = service.submit_submission(
                normalized_submission_id,
                student_id=student_id,
                answers=parsed_answers,
            )
        return _answer_rows(submission.answers), feedback(
            "答卷提交成功，答案已冻结。", "success"
        )
    except (
        PermissionDeniedError,
        SubmissionServiceError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return [], _format_error(error)


submit_submission = submit_exam
complete_exam = submit_exam


def _answer_value(value: Any) -> Any:
    """把控件答案转换为答卷服务支持的内容。"""

    if value is None:
        return None
    return value if isinstance(value, (str, list, dict)) else str(value)


def _workspace_card(
    records: Sequence[Mapping[str, Any]],
    answers: Mapping[str, Any],
    marks: Sequence[int],
    current: int,
) -> str:
    """渲染带文字冗余状态的答题卡。"""

    items = []
    for index, record in enumerate(records):
        answered = bool(answers.get(str(record["id"])))
        mark = index in marks
        items.append(
            question_indicator(
                index + 1,
                answered=answered,
                marked=mark,
                current=index == current,
            )
        )
    return (
        '<div class="edu-question-card" aria-label="答题卡">'
        + "".join(items)
        + f'<div class="edu-question-count">已答：{sum(bool(answers.get(str(item["id"]))) for item in records)}　'
        + f'未答：{sum(not bool(answers.get(str(item["id"]))) for item in records)}</div>'
        + '<div class="edu-question-legend">图例：已答 / 未答 / 标记 / 当前题</div></div>'
    )


def _workspace_detail(
    records: Sequence[Mapping[str, Any]],
    answers: Mapping[str, Any],
    current: int,
    *,
    read_only: bool = False,
) -> tuple[Any, ...]:
    """返回当前题目及单选、判断、简答控件状态。"""

    if not records:
        return (
            empty_state("请先从可参加考试列表开始或继续。"),
            gr.update(choices=[], value=None, visible=False, interactive=False),
            gr.update(choices=[], value=None, visible=False, interactive=False),
            gr.update(value="", visible=False, interactive=False),
        )
    index = max(0, min(current, len(records) - 1))
    record = records[index]
    question_type = record["type"]
    content = (
        f"### 第 {index + 1} 题　{status_label(question_type, entity='question_type')}　"
        f"{record['score']} 分\n\n{record['content']}"
    )
    options = record.get("options")
    choices: list[tuple[str, str]] = []
    if isinstance(options, Mapping):
        choices = [(f"{key}：{value}", str(key)) for key, value in options.items()]
    elif isinstance(options, Sequence) and not isinstance(options, (str, bytes)):
        choices = [(str(value), str(value)) for value in options]
    value = answers.get(str(record["id"]))
    if question_type in {
        QuestionType.SINGLE_CHOICE.value,
        QuestionType.TRUE_FALSE.value,
    }:
        if question_type == QuestionType.TRUE_FALSE.value and not choices:
            choices = [("正确", "True"), ("错误", "False")]
        return (
            content,
            gr.update(
                choices=choices, value=value, visible=True, interactive=not read_only
            ),
            gr.update(choices=[], value=None, visible=False, interactive=False),
            gr.update(value="", visible=False, interactive=False),
        )
    return (
        content,
        gr.update(choices=[], value=None, visible=False, interactive=False),
        gr.update(choices=[], value=None, visible=False, interactive=False),
        gr.update(
            value=value if isinstance(value, str) else "",
            visible=True,
            interactive=not read_only,
        ),
    )


def _workspace_records(exam: Exam) -> list[dict[str, Any]]:
    """只保存当前考试实际返回的题目和分值信息。"""

    return [
        {
            "id": str(question.id),
            "type": question.type.value,
            "content": question.content,
            "options": question.options,
            "score": str(question.score),
        }
        for question in exam.questions or ()
    ]


def open_answer_workspace(
    exam_id: str,
    state: Mapping[str, Any],
) -> tuple[Any, ...]:
    """打开或继续真实答卷，并初始化题号导航。"""

    try:
        student_id = _student_id(state)
        normalized_exam_id = _parse_uuid(exam_id, "考试标识")
        with get_session_factory()() as session:
            service = SubmissionService(session)
            exam, submission = _load_exam(service, normalized_exam_id, student_id)
        answers = {
            answer.question_id: answer.content
            for answer in submission.answers
            if answer.content is not None
        }
        records = _workspace_records(exam)
        read_only = submission.status is not SubmissionStatus.DRAFT
        deadline = (
            f"截止：{exam.ends_at.isoformat()}"
            if exam.ends_at
            else f"考试时长：{exam.duration_minutes or '未设置'} 分钟"
        )
        detail, single, boolean, text = _workspace_detail(
            records, answers, 0, read_only=read_only
        )
        return (
            exam.title,
            f"{deadline}（仅显示服务提供的时间，不自动交卷）",
            status_badge(submission.status, entity="submission"),
            submission.id,
            records,
            answers,
            [],
            0,
            _workspace_card(records, answers, [], 0),
            detail,
            single,
            boolean,
            text,
            gr.update(visible=True),
            gr.update(interactive=not read_only),
            gr.update(interactive=not read_only),
            gr.update(interactive=not read_only),
            gr.update(interactive=not read_only),
            gr.update(interactive=not read_only),
            empty_state(
                "答卷已提交，当前为只读状态。"
                if read_only
                else "答案尚未保存，请按题号作答并保存。"
            ),
        )
    except (
        PermissionDeniedError,
        SubmissionServiceError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return (
            "",
            "",
            "",
            "",
            [],
            {},
            [],
            0,
            _workspace_card([], {}, [], 0),
            empty_state("答题区暂不可用。"),
            gr.update(visible=False),
            gr.update(visible=False),
            gr.update(visible=False),
            gr.update(visible=False),
            gr.update(interactive=False),
            gr.update(interactive=False),
            gr.update(interactive=False),
            gr.update(interactive=False),
            gr.update(interactive=False),
            _format_error(error),
        )


def update_current_answer(
    value: Any,
    records: Sequence[Mapping[str, Any]],
    answers: Mapping[str, Any],
    current: int,
    marks: Sequence[int],
) -> tuple[dict[str, Any], str]:
    """仅更新当前会话答案，已答状态不等同于已保存状态。"""

    if not records:
        return dict(answers), _workspace_card(records, answers, marks, current)
    updated = dict(answers)
    question_id = str(records[current]["id"])
    normalized = _answer_value(value)
    if normalized is None or (isinstance(normalized, str) and not normalized.strip()):
        updated.pop(question_id, None)
    else:
        updated[question_id] = normalized
    return updated, _workspace_card(records, updated, marks, current)


def toggle_mark(
    marks: Sequence[int],
    current: int,
    records: Sequence[Mapping[str, Any]],
    answers: Mapping[str, Any],
) -> tuple[list[int], str]:
    """切换当前题标记并保留已答状态。"""

    result = {int(item) for item in marks}
    if current in result:
        result.remove(current)
    else:
        result.add(current)
    values = sorted(result)
    return values, _workspace_card(records, answers, values, current)


def navigate_answer(
    target: int,
    records: Sequence[Mapping[str, Any]],
    answers: Mapping[str, Any],
    marks: Sequence[int],
) -> tuple[int, str, Any, Any, Any, Any]:
    """只切换当前题，不丢失当前会话答案。"""

    if not records:
        return (
            0,
            _workspace_card([], {}, marks, 0),
            empty_state("暂无题目。"),
            gr.update(),
            gr.update(),
            gr.update(),
        )
    current = max(0, min(int(target), len(records) - 1))
    detail, single, boolean, text = _workspace_detail(records, answers, current)
    return (
        current,
        _workspace_card(records, answers, marks, current),
        detail,
        single,
        boolean,
        text,
    )


def save_current_answer(
    submission_id: str,
    records: Sequence[Mapping[str, Any]],
    answers: Mapping[str, Any],
    state: Mapping[str, Any],
) -> tuple[str, str]:
    """保存当前会话中的答案，成功消息以服务回执为准。"""

    try:
        student_id = _student_id(state)
        if not submission_id:
            raise ValueError("请先开始或继续一场考试。")
        parsed = _parse_answers(answers)
        if parsed is None:
            raise ValueError("请至少填写一道题的答案。")
        with get_session_factory()() as session:
            service = SubmissionService(session)
            service.save_answers(
                _parse_uuid(submission_id, "答卷标识"), parsed, student_id=student_id
            )
        return "已保存当前答卷答案。", feedback("答案已保存。", "success")
    except (
        PermissionDeniedError,
        SubmissionServiceError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return "", _format_error(error)


def prepare_submit(
    records: Sequence[Mapping[str, Any]],
    answers: Mapping[str, Any],
) -> tuple[Any, ...]:
    """显示内联交卷确认，未答题时只允许返回补答。"""

    missing = [
        str(index + 1)
        for index, record in enumerate(records)
        if not answers.get(str(record["id"]))
    ]
    text = f"交卷前确认：已答 {len(records) - len(missing)} / {len(records)} 题。" + (
        f"未答题号：{'、'.join(missing)}。" if missing else "所有题目均已填写。"
    )
    return (
        text,
        gr.update(visible=True),
        gr.update(interactive=not missing),
        gr.update(visible=bool(missing)),
        gr.update(visible=not missing),
    )


def confirm_submit(
    submission_id: str,
    answers: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    confirmed: bool,
    state: Mapping[str, Any],
) -> tuple[Any, ...]:
    """确认后调用服务提交，提交成功后冻结当前控件。"""

    try:
        if not confirmed:
            raise ValueError("请先确认交卷。")
        missing = [record["id"] for record in records if not answers.get(record["id"])]
        if missing:
            raise ValueError("仍有未答题，请返回补答。")
        with get_session_factory()() as session:
            submission = SubmissionService(session).submit_submission(
                _parse_uuid(submission_id, "答卷标识"),
                student_id=_student_id(state),
                answers=answers,
            )
        return (
            status_badge(submission.status, entity="submission"),
            feedback("答卷提交成功，答案已冻结。", "success"),
            gr.update(interactive=False),
            gr.update(interactive=False),
            gr.update(interactive=False),
            gr.update(interactive=False),
            gr.update(interactive=False),
            gr.update(interactive=False),
            gr.update(interactive=False),
            gr.update(interactive=False),
            gr.update(visible=False),
        )
    except (
        PermissionDeniedError,
        SubmissionServiceError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return (
            "",
            _format_error(error),
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
        )


def create_student_exam_view(session_state: Any | None = None) -> StudentExamView:
    """创建可参加考试、答题卡和逐题作答工作区。"""

    state = session_state or gr.State(_empty_state())
    with gr.Column(visible=False, elem_classes="edu-student-exam") as panel:
        gr.HTML(
            "<style>.edu-student-exam .edu-answer-text textarea "
            "{min-height:240px !important;}</style>"
        )
        gr.Markdown("## 我的考试")
        selected_exam = gr.Textbox(visible=False, container=False)
        exam_ids = gr.State([])
        submission_id = gr.Textbox(visible=False, container=False)
        records = gr.State([])
        answers = gr.State({})
        marks = gr.State([])
        current = gr.State(0)

        with gr.Row():
            refresh_button = gr.Button("刷新考试", variant="secondary")
            start_button = gr.Button("开始或继续", variant="primary")
        exams_table = gr.Dataframe(
            headers=list(EXAM_TABLE_HEADERS),
            datatype=EXAM_TABLE_DATATYPES,
            value=[],
            interactive=False,
            label="可参加考试",
            **table_options(EXAM_TABLE_HEADERS),
        )
        entry_message = gr.Markdown(empty_state("暂无可参加考试。"))

        with gr.Column(
            visible=False, elem_classes="edu-answer-workspace"
        ) as answer_area:
            with gr.Row():
                exam_title = gr.Markdown("### 尚未开始考试")
                submission_status = gr.Markdown()
                deadline = gr.Markdown()
                submit_button = gr.Button("交卷", variant="stop")
            with gr.Row():
                with gr.Column(scale=0, min_width=200, elem_classes="edu-answer-card"):
                    card = gr.HTML()
                    mark_button = gr.Button("标记本题")
                with gr.Column(scale=1):
                    question_detail = gr.Markdown(empty_state("请选择考试。"))
                    single = gr.Radio(label="单选答案", choices=[], visible=False)
                    boolean = gr.Radio(
                        label="判断答案",
                        choices=[("正确", "True"), ("错误", "False")],
                        visible=False,
                    )
                    answer_text = gr.Textbox(
                        label="简答答案（纯文本）",
                        lines=8,
                        elem_classes=["edu-answer-text"],
                        visible=False,
                    )
                    with gr.Row():
                        previous_button = gr.Button("上一题")
                        save_button = gr.Button("保存当前答案")
                        next_button = gr.Button("下一题")
            with gr.Column(
                visible=False, elem_classes="edu-submit-confirmation"
            ) as confirmation:
                confirmation_text = gr.Markdown()
                confirm_checkbox = gr.Checkbox(
                    label="我确认检查了当前答卷",
                    value=False,
                )
                with gr.Row():
                    return_button = gr.Button("返回补答")
                    confirm_submit_button = gr.Button("确认交卷", variant="stop")

            workspace_message = gr.Markdown(empty_state("答案尚未保存。"))

        def refresh_entry(
            current_state: Mapping[str, Any],
        ) -> tuple[list[list[str]], list[str], str]:
            try:
                student_id = _student_id(current_state)
                with get_session_factory()() as session:
                    exams = SubmissionService(session).list_available_exams(
                        student_id=student_id
                    )
                return (
                    _exam_rows(exams),
                    [exam.id for exam in exams],
                    (
                        feedback(f"已加载 {len(exams)} 场可参加考试。", "success")
                        if exams
                        else empty_state("暂无可参加考试。")
                    ),
                )
            except (
                PermissionDeniedError,
                SubmissionServiceError,
                SQLAlchemyError,
                TypeError,
                ValueError,
            ) as error:
                return [], [], _format_error(error)

        def select_exam(event: gr.SelectData, ids: Sequence[str]) -> str:
            index = (
                event.index[0]
                if isinstance(event.index, (list, tuple))
                else event.index
            )
            if (
                not event.selected
                or not isinstance(index, int)
                or not 0 <= index < len(ids)
            ):
                raise ValueError("考试选择已失效，请重新选择。")
            return ids[index]

        def move_previous(index: int, *values: Any) -> tuple[Any, ...]:
            return navigate_answer(index - 1, *values)

        def move_next(index: int, *values: Any) -> tuple[Any, ...]:
            return navigate_answer(index + 1, *values)

        refresh_button.click(
            refresh_entry,
            inputs=[state],
            outputs=[exams_table, exam_ids, entry_message],
            show_progress="hidden",
        )
        exams_table.select(select_exam, inputs=[exam_ids], outputs=[selected_exam])
        start_button.click(
            open_answer_workspace,
            inputs=[selected_exam, state],
            outputs=[
                exam_title,
                deadline,
                submission_status,
                submission_id,
                records,
                answers,
                marks,
                current,
                card,
                question_detail,
                single,
                boolean,
                answer_text,
                answer_area,
                save_button,
                previous_button,
                mark_button,
                next_button,
                submit_button,
                workspace_message,
            ],
            show_progress="minimal",
        )
        answer_value_inputs = [records, answers, current, marks]
        single.input(
            update_current_answer,
            inputs=[single, *answer_value_inputs],
            outputs=[answers, card],
            show_progress="hidden",
        )
        boolean.input(
            update_current_answer,
            inputs=[boolean, *answer_value_inputs],
            outputs=[answers, card],
            show_progress="hidden",
        )
        answer_text.input(
            update_current_answer,
            inputs=[answer_text, *answer_value_inputs],
            outputs=[answers, card],
            show_progress="hidden",
        )
        previous_button.click(
            move_previous,
            inputs=[current, records, answers, marks],
            outputs=[current, card, question_detail, single, boolean, answer_text],
            show_progress="hidden",
        )
        next_button.click(
            move_next,
            inputs=[current, records, answers, marks],
            outputs=[current, card, question_detail, single, boolean, answer_text],
            show_progress="hidden",
        )
        mark_button.click(
            toggle_mark,
            inputs=[marks, current, records, answers],
            outputs=[marks, card],
            show_progress="hidden",
        )
        save_button.click(
            save_current_answer,
            inputs=[submission_id, records, answers, state],
            outputs=[submission_status, workspace_message],
            show_progress="minimal",
        )
        submit_button.click(
            prepare_submit,
            inputs=[records, answers],
            outputs=[
                confirmation_text,
                confirmation,
                confirm_submit_button,
                return_button,
                confirm_checkbox,
            ],
            show_progress="hidden",
        )
        return_button.click(
            lambda: {
                confirmation: gr.update(visible=False),
                confirm_checkbox: gr.update(value=False),
            },
            outputs=[confirmation, confirm_checkbox],
            show_progress="hidden",
        )
        confirm_submit_button.click(
            confirm_submit,
            inputs=[submission_id, answers, records, confirm_checkbox, state],
            outputs=[
                submission_status,
                workspace_message,
                single,
                boolean,
                answer_text,
                save_button,
                submit_button,
                confirmation,
            ],
            show_progress="minimal",
        )

    return StudentExamView(
        panel=panel,
        exams_table=exams_table,
        questions_table=gr.Dataframe(visible=False),
        answers_table=gr.Dataframe(visible=False),
        submission_id=submission_id,
        answers_input=gr.JSON(visible=False),
        message=workspace_message,
    )


build_student_exam_view = create_student_exam_view
create_student_view = create_student_exam_view


__all__ = [
    "ANSWER_TABLE_HEADERS",
    "EXAM_TABLE_HEADERS",
    "QUESTION_TABLE_HEADERS",
    "StudentExamView",
    "build_student_exam_view",
    "complete_exam",
    "create_student_exam_view",
    "create_student_view",
    "load_exam",
    "open_exam",
    "refresh_available_exams",
    "refresh_exam_list",
    "refresh_exams",
    "save_answers",
    "save_draft_answers",
    "save_submission_answers",
    "start_exam",
    "submit_exam",
    "submit_submission",
]
