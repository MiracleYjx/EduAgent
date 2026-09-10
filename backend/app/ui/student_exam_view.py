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
    bind_confirmation,
    empty_state,
    feedback,
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


def create_student_exam_view(session_state: Any | None = None) -> StudentExamView:
    """创建学生考试面板。"""

    state = session_state or gr.State(_empty_state())
    with gr.Column(visible=False) as panel:
        gr.Markdown("## 参加考试")
        with gr.Row():
            refresh_button = gr.Button("刷新考试", variant="secondary")
            exam_id_input = gr.Textbox(label="考试 ID")
            open_button = gr.Button("开始或继续", variant="primary")
        exams_table = gr.Dataframe(
            headers=list(EXAM_TABLE_HEADERS),
            datatype=EXAM_TABLE_DATATYPES,
            value=[],
            interactive=False,
            label="可参加考试",
            **table_options(EXAM_TABLE_HEADERS),
        )

        questions_table = gr.Dataframe(
            headers=list(QUESTION_TABLE_HEADERS),
            datatype=QUESTION_TABLE_DATATYPES,
            value=[],
            interactive=False,
            label="考试题目",
            **table_options(QUESTION_TABLE_HEADERS),
        )
        with gr.Row():
            submission_id = gr.Textbox(label="答卷 ID", interactive=False)
            answers_input = gr.JSON(
                label="答案（题目 ID 到答案的 JSON）",
                value={},
            )
        with gr.Row():
            save_button = gr.Button("保存答案")
            submit_button = gr.Button("提交答卷", variant="primary")
        answers_table = gr.Dataframe(
            headers=list(ANSWER_TABLE_HEADERS),
            datatype=ANSWER_TABLE_DATATYPES,
            value=[],
            interactive=False,
            label="答案状态",
            **table_options(ANSWER_TABLE_HEADERS),
        )
        message = gr.Markdown(empty_state("尚未加载考试。"))

        refresh_button.click(
            fn=refresh_exams,
            inputs=[state],
            outputs=[exams_table, message],
            show_progress="hidden",
        )
        open_button.click(
            fn=start_exam,
            inputs=[exam_id_input, state],
            outputs=[
                questions_table,
                submission_id,
                answers_input,
                answers_table,
                message,
            ],
            show_progress="hidden",
        )
        save_button.click(
            fn=save_answers,
            inputs=[submission_id, answers_input, state],
            outputs=[answers_table, message],
            show_progress="hidden",
        )
        bind_confirmation(
            submit_button,
            action="提交答卷",
            target=submission_id,
            callback=submit_exam,
            inputs=[submission_id, answers_input, state],
            outputs=[answers_table, message],
        )

    return StudentExamView(
        panel=panel,
        exams_table=exams_table,
        questions_table=questions_table,
        answers_table=answers_table,
        submission_id=submission_id,
        answers_input=answers_input,
        message=message,
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
