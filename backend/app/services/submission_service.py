"""学生考试答卷服务：可参加考试、答案保存和答卷状态管理。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.sql.elements import ColumnElement

from backend.app.domain.enums import (
    AnswerStatus,
    ExamStatus,
    QuestionStatus,
    SubmissionStatus,
    UserRole,
)
from backend.app.models import Answer, Exam, ExamParticipant, Submission, User
from backend.app.services.exam_service import ExamSummary

_UNSET = object()
_ZERO_SCORE = Decimal("0.00")

_ALLOWED_SUBMISSION_TRANSITIONS = {
    SubmissionStatus.DRAFT: {SubmissionStatus.SUBMITTED},
    SubmissionStatus.SUBMITTED: {SubmissionStatus.GRADED},
    SubmissionStatus.GRADED: {SubmissionStatus.REVIEWED},
    SubmissionStatus.REVIEWED: set(),
}
_ALLOWED_ANSWER_TRANSITIONS = {
    AnswerStatus.DRAFT: {AnswerStatus.SUBMITTED},
    AnswerStatus.SUBMITTED: {
        AnswerStatus.GRADING,
        AnswerStatus.GRADED,
        AnswerStatus.FAILED,
    },
    AnswerStatus.GRADING: {AnswerStatus.GRADED, AnswerStatus.FAILED},
    AnswerStatus.GRADED: set(),
    AnswerStatus.FAILED: {AnswerStatus.GRADING},
}

AnswerContent = str | list[str] | dict[str, str] | None
StudentInput = UUID | str | User
AnswerCollection = Mapping[Any, Any] | Iterable[Any]


class AnswerSummary(BaseModel):
    """面向 API、UI 和阅卷流程的单题答案摘要。"""

    model_config = ConfigDict(frozen=True)

    id: str
    submission_id: str
    question_id: str
    content: AnswerContent = None
    status: AnswerStatus
    created_at: datetime
    updated_at: datetime


class SubmissionSummary(BaseModel):
    """面向 API、UI 和阅卷流程的答卷摘要。"""

    model_config = ConfigDict(frozen=True)

    id: str
    exam_id: str
    student_id: str
    status: SubmissionStatus
    submitted_at: datetime | None = None
    graded_at: datetime | None = None
    reviewed_at: datetime | None = None
    answers: list[AnswerSummary] = Field(default_factory=list)
    answer_count: int = 0
    question_count: int = 0
    missing_question_ids: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    @property
    def answer_ids(self) -> list[str]:
        """返回当前答卷中已保存的答案标识。"""

        return [answer.id for answer in self.answers]

    @property
    def question_ids(self) -> list[str]:
        """返回当前答卷中已保存答案对应的题目标识。"""

        return [answer.question_id for answer in self.answers]

    @property
    def is_complete(self) -> bool:
        """判断答卷是否已经为每道考试题保存非空答案。"""

        return not self.missing_question_ids


class AvailableExamSummary(ExamSummary):
    """学生当前可以参加的考试摘要。"""


class SubmissionServiceError(RuntimeError):
    """答卷服务的异常基类。"""


class SubmissionNotFoundError(SubmissionServiceError):
    """答卷、考试、题目或学生不存在时抛出。"""


class SubmissionConflictError(SubmissionServiceError):
    """重复提交、冻结答卷或答案唯一约束冲突时抛出。"""

    def __init__(
        self,
        message: str,
        existing_submission: SubmissionSummary | None = None,
    ) -> None:
        super().__init__(message)
        self.existing_submission = existing_submission
        # 保留两个常用名称，便于 API 层直接返回已有答卷状态。
        self.submission = existing_submission
        self.existing_status = (
            existing_submission.status if existing_submission is not None else None
        )


class SubmissionValidationError(SubmissionServiceError):
    """答卷输入或状态规则不符合要求时抛出。"""


class SubmissionPermissionError(SubmissionServiceError):
    """当前学生无权访问或修改目标答卷时抛出。"""


class SubmissionNotAvailableError(SubmissionValidationError):
    """考试尚未开放、已经结束或不满足参加条件时抛出。"""


# 兼容调用方对异常名称的不同约定。
DuplicateSubmissionError = SubmissionConflictError
ExamNotAvailableError = SubmissionNotAvailableError


def _normalize_uuid(value: UUID | str | User | Any, field_name: str) -> UUID:
    """将外部标识统一转换为 UUID。"""

    if isinstance(value, User) or (
        not isinstance(value, (UUID, str)) and hasattr(value, "id")
    ):
        value = value.id
    if value is None:
        raise SubmissionValidationError(f"{field_name}不能为空。")
    try:
        return value if isinstance(value, UUID) else UUID(str(value).strip())
    except (AttributeError, TypeError, ValueError) as exc:
        raise SubmissionValidationError(f"{field_name}无效。") from exc


def _resolve_student_id(
    student_id: StudentInput | None,
    student: StudentInput | None = None,
    user_id: StudentInput | None = None,
) -> UUID | None:
    """兼容学生、用户和学生标识别名，并拒绝互相冲突的输入。"""

    candidates = [
        (_normalize_uuid(value, field_name) if value is not None else None)
        for value, field_name in (
            (student_id, "学生标识"),
            (student, "学生标识"),
            (user_id, "用户标识"),
        )
    ]
    normalized = [candidate for candidate in candidates if candidate is not None]
    if normalized and any(candidate != normalized[0] for candidate in normalized[1:]):
        raise SubmissionValidationError("学生标识与用户标识不一致。")
    return normalized[0] if normalized else None


def _normalize_submission_status(value: SubmissionStatus | str) -> SubmissionStatus:
    """将答卷状态名称或枚举值规范化为领域枚举。"""

    if isinstance(value, SubmissionStatus):
        return value
    if not isinstance(value, str):
        raise SubmissionValidationError("答卷状态无效。")
    candidate = value.strip()
    for status in SubmissionStatus:
        if candidate.lower() in {status.name.lower(), status.value.lower()}:
            return status
    raise SubmissionValidationError("答卷状态无效。")


def _normalize_answer_status(value: AnswerStatus | str) -> AnswerStatus:
    """将答案状态名称或枚举值规范化为领域枚举。"""

    if isinstance(value, AnswerStatus):
        return value
    if not isinstance(value, str):
        raise SubmissionValidationError("答案状态无效。")
    candidate = value.strip()
    for status in AnswerStatus:
        if candidate.lower() in {status.name.lower(), status.value.lower()}:
            return status
    raise SubmissionValidationError("答案状态无效。")


def _as_utc(moment: datetime | None) -> datetime:
    """将时间统一为带时区的 UTC 时间，兼容 SQLite 返回的朴素时间。"""

    if moment is None:
        return datetime.now(UTC)
    if not isinstance(moment, datetime):
        raise SubmissionValidationError("时间输入无效。")
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _resolve_time(
    now: datetime | None,
    current_time: datetime | None,
    as_of: datetime | None,
) -> datetime:
    """兼容 now、current_time 和 as_of 三种测试或调用参数名称。"""

    moments = [moment for moment in (now, current_time, as_of) if moment is not None]
    if len(moments) > 1:
        normalized = [_as_utc(moment) for moment in moments]
        if any(moment != normalized[0] for moment in normalized[1:]):
            raise SubmissionValidationError("时间参数不一致。")
    return _as_utc(moments[0] if moments else None)


def _normalize_answer_content(value: Any) -> AnswerContent:
    """校验答案内容为模型允许保存的 JSON 形状。"""

    if value is None:
        return None
    if isinstance(value, str):
        if "\x00" in value:
            raise SubmissionValidationError("答案包含无效字符。")
        return value
    if isinstance(value, tuple):
        value = list(value)
    if isinstance(value, list):
        if not all(isinstance(item, str) for item in value):
            raise SubmissionValidationError("答案列表必须只包含字符串。")
        if any("\x00" in item for item in value):
            raise SubmissionValidationError("答案包含无效字符。")
        return deepcopy(value)
    if isinstance(value, dict):
        if not all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in value.items()
        ):
            raise SubmissionValidationError("答案对象的键和值必须是字符串。")
        if any("\x00" in key or "\x00" in item for key, item in value.items()):
            raise SubmissionValidationError("答案包含无效字符。")
        return deepcopy(value)
    raise SubmissionValidationError("答案必须是字符串、字符串列表、字符串对象或空值。")


def _is_blank_answer(value: AnswerContent) -> bool:
    """判断答案是否缺失，供最终提交前的完整性检查使用。"""

    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list):
        return not value or not any(item.strip() for item in value)
    return not value or not any(
        key.strip() and item.strip() for key, item in value.items()
    )


def _normalize_answer_entries(
    answers: AnswerCollection | Answer | None,
) -> list[tuple[UUID, AnswerContent]]:
    """把映射、二元组列表或 Answer 对象统一为答案条目。"""

    if answers is None:
        return []
    if isinstance(answers, Answer):
        raw_entries: Iterable[Any] = (answers,)
        mapping_as_entries = False
    elif isinstance(answers, Mapping) and not (
        "question_id" in answers or "question" in answers
    ):
        raw_entries = answers.items()
        mapping_as_entries = True
    elif isinstance(answers, Mapping):
        raw_entries = (answers,)
        mapping_as_entries = False
    elif isinstance(answers, Iterable) and not isinstance(
        answers, (str, bytes, bytearray)
    ):
        raw_entries = answers
        mapping_as_entries = False
    else:
        raise SubmissionValidationError("答案列表格式无效。")

    entries: list[tuple[UUID, AnswerContent]] = []
    seen: set[UUID] = set()
    for item in raw_entries:
        if mapping_as_entries:
            raw_question_id, raw_content = item
        elif isinstance(item, Answer):
            raw_question_id, raw_content = item.question_id, item.content
        elif isinstance(item, Mapping):
            raw_question_id = item.get(
                "question_id",
                item.get("question", item.get("id")),
            )
            if raw_question_id is None:
                raise SubmissionValidationError("答案条目缺少题目标识。")
            if "content" in item:
                raw_content = item["content"]
            elif "answer" in item:
                raw_content = item["answer"]
            elif "value" in item:
                raw_content = item["value"]
            elif "answer_content" in item:
                raw_content = item["answer_content"]
            else:
                raise SubmissionValidationError("答案条目缺少答案内容。")
        elif (
            isinstance(item, Sequence)
            and not isinstance(
                item,
                (str, bytes, bytearray),
            )
            and len(item) == 2
        ):
            raw_question_id, raw_content = item
        else:
            raw_question_id = getattr(item, "question_id", None)
            if raw_question_id is None:
                raise SubmissionValidationError("答案条目缺少题目标识。")
            raw_content = getattr(item, "content", _UNSET)
            if raw_content is _UNSET:
                raw_content = getattr(item, "answer", _UNSET)
            if raw_content is _UNSET:
                raise SubmissionValidationError("答案条目缺少答案内容。")

        question_id = _normalize_uuid(raw_question_id, "题目标识")
        if question_id in seen:
            raise SubmissionValidationError("同一批答案不能重复包含同一道题。")
        seen.add(question_id)
        entries.append((question_id, _normalize_answer_content(raw_content)))
    return entries


class SubmissionService:
    """封装学生考试资格、答卷写入和答卷状态机。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def list_available_exams(
        self,
        student_id: StudentInput | None = None,
        *,
        student: StudentInput | None = None,
        user_id: StudentInput | None = None,
        now: datetime | None = None,
        current_time: datetime | None = None,
        as_of: datetime | None = None,
    ) -> list[AvailableExamSummary]:
        """列出当前学生可以参加的已发布考试。"""

        normalized_student_id = _resolve_student_id(student_id, student, user_id)
        if normalized_student_id is not None:
            self._load_student(normalized_student_id)
        moment = _resolve_time(now, current_time, as_of)

        statement = (
            select(Exam)
            .options(selectinload(Exam.questions))
            .where(Exam.status == ExamStatus.PUBLISHED)
            .where(self._exam_participation_filter(normalized_student_id))
            .order_by(Exam.created_at, Exam.title, Exam.id)
        )
        try:
            exams = self.session.scalars(statement).all()
        except SQLAlchemyError as exc:
            raise SubmissionServiceError("无法读取可参加考试列表。") from exc

        available: list[AvailableExamSummary] = []
        for exam in exams:
            try:
                self._ensure_exam_available(exam, moment)
            except SubmissionNotAvailableError:
                continue
            available.append(self._available_exam_summary(exam))
        return available

    get_available_exams = list_available_exams
    list_student_exams = list_available_exams
    list_exams_for_student = list_available_exams
    get_student_exams = list_available_exams
    available_exams = list_available_exams

    def get_available_exam(
        self,
        exam_id: UUID | str,
        student_id: StudentInput | None = None,
        *,
        student: StudentInput | None = None,
        user_id: StudentInput | None = None,
        now: datetime | None = None,
        current_time: datetime | None = None,
        as_of: datetime | None = None,
    ) -> AvailableExamSummary:
        """读取一场考试，并确认它当前对学生开放。"""

        normalized_student_id = _resolve_student_id(student_id, student, user_id)
        if normalized_student_id is not None:
            self._load_student(normalized_student_id)
        exam = self._load_exam(exam_id)
        self._ensure_exam_participation(exam.id, normalized_student_id)
        self._ensure_exam_available(exam, _resolve_time(now, current_time, as_of))
        return self._available_exam_summary(exam)

    get_exam = get_available_exam

    def create_submission(
        self,
        exam_id: UUID | str,
        student_id: StudentInput | None = None,
        answers: AnswerCollection | Answer | None = None,
        *,
        student: StudentInput | None = None,
        user_id: StudentInput | None = None,
        initialize_answers: bool = False,
        now: datetime | None = None,
        current_time: datetime | None = None,
        as_of: datetime | None = None,
    ) -> SubmissionSummary:
        """创建草稿答卷；已有草稿答卷会被复用，已提交答卷不会被覆盖。"""

        normalized_student_id = _resolve_student_id(student_id, student, user_id)
        if normalized_student_id is None:
            raise SubmissionValidationError("学生标识不能为空。")
        self._load_student(normalized_student_id)
        moment = _resolve_time(now, current_time, as_of)

        # 锁定考试行，避免 PostgreSQL 并发请求同时创建同一学生的草稿答卷。
        exam = self._load_exam(exam_id, for_update=True)
        self._ensure_exam_participation(exam.id, normalized_student_id)
        normalized_entries = (
            _normalize_answer_entries(answers) if answers is not None else []
        )
        self._validate_answer_question_ids(exam, normalized_entries)
        existing = self._find_submission_entity(exam.id, normalized_student_id)
        if existing is not None:
            existing_summary = self._submission_summary(existing)
            if self._submission_status(existing) is not SubmissionStatus.DRAFT:
                raise SubmissionConflictError(
                    "该学生已经提交此考试，不能重复提交。",
                    existing_summary,
                )
            self._ensure_exam_available(exam, moment)
            if answers is not None:
                self._save_answer_entries(
                    existing,
                    normalized_entries,
                    exam,
                )
                if initialize_answers:
                    self._initialize_answer_entities(existing, exam)
                return self._commit_and_reload_submission(
                    existing,
                    "保存答卷失败。",
                )
            if initialize_answers:
                self._initialize_answer_entities(existing, exam)
                return self._commit_and_reload_submission(
                    existing,
                    "初始化答卷失败。",
                )
            return existing_summary

        self._ensure_exam_available(exam, moment)
        submission = Submission(
            exam_id=exam.id,
            student_id=normalized_student_id,
            status=SubmissionStatus.DRAFT,
        )
        self.session.add(submission)
        try:
            self.session.flush()
            if initialize_answers:
                self._initialize_answer_entities(submission, exam)
            if answers is not None:
                self._save_answer_entries(
                    submission,
                    normalized_entries,
                    exam,
                )
            self.session.commit()
        except SubmissionServiceError:
            self.session.rollback()
            raise
        except IntegrityError as exc:
            self.session.rollback()
            raced = self._find_submission_entity(exam.id, normalized_student_id)
            if raced is not None:
                raced_summary = self._submission_summary(raced)
                raise SubmissionConflictError(
                    "该学生已经存在此考试的答卷。",
                    raced_summary,
                ) from exc
            raise SubmissionConflictError("创建答卷时发生数据冲突。") from exc
        except SQLAlchemyError as exc:
            self.session.rollback()
            raise SubmissionServiceError("创建答卷失败。") from exc
        return self._reload_submission_summary(submission.id)

    start_submission = create_submission

    def get_or_create_submission(
        self,
        exam_id: UUID | str,
        student_id: StudentInput | None = None,
        answers: AnswerCollection | Answer | None = None,
        *,
        student: StudentInput | None = None,
        user_id: StudentInput | None = None,
        initialize_answers: bool = False,
        now: datetime | None = None,
        current_time: datetime | None = None,
        as_of: datetime | None = None,
    ) -> SubmissionSummary:
        """获取学生答卷，不存在时创建，已完成答卷也只返回原状态。"""

        try:
            return self.create_submission(
                exam_id,
                student_id,
                student=student,
                user_id=user_id,
                answers=answers,
                initialize_answers=initialize_answers,
                now=now,
                current_time=current_time,
                as_of=as_of,
            )
        except SubmissionConflictError as error:
            if error.existing_submission is None:
                raise
            return error.existing_submission

    def save_answer(
        self,
        submission_id: UUID | str,
        question_id: UUID | str,
        content: AnswerContent | object = _UNSET,
        student_id: StudentInput | None = None,
        *,
        answer: AnswerContent | object = _UNSET,
        answer_content: AnswerContent | object = _UNSET,
        student: StudentInput | None = None,
        user_id: StudentInput | None = None,
    ) -> AnswerSummary:
        """在草稿答卷中新增或更新一道题的答案。"""

        values = [
            value for value in (content, answer, answer_content) if value is not _UNSET
        ]
        if not values:
            raise SubmissionValidationError("答案内容不能为空。")
        if any(value != values[0] for value in values[1:]):
            raise SubmissionValidationError("答案内容参数不一致。")
        normalized_student_id = _resolve_student_id(student_id, student, user_id)
        entries = _normalize_answer_entries(
            {question_id: values[0]},
        )
        saved = self._save_answers_internal(
            submission_id,
            entries,
            normalized_student_id,
        )
        return saved[0]

    persist_answer = save_answer
    upsert_answer = save_answer
    save_submission_answer = save_answer
    add_answer = save_answer
    create_answer = save_answer

    def save_answers(
        self,
        submission_id: UUID | str,
        answers: AnswerCollection | Answer,
        student_id: StudentInput | None = None,
        *,
        student: StudentInput | None = None,
        user_id: StudentInput | None = None,
    ) -> list[AnswerSummary]:
        """在草稿答卷中原子地新增或更新多道题的答案。"""

        normalized_student_id = _resolve_student_id(student_id, student, user_id)
        entries = _normalize_answer_entries(answers)
        return self._save_answers_internal(
            submission_id,
            entries,
            normalized_student_id,
        )

    persist_answers = save_answers
    save_submission_answers = save_answers
    upsert_answers = save_answers
    persist_submission_answers = save_answers

    def submit_submission(
        self,
        submission_id: UUID | str,
        student_id: StudentInput | AnswerCollection | Answer | None = None,
        answers: AnswerCollection | Answer | None = None,
        *,
        student: StudentInput | None = None,
        user_id: StudentInput | None = None,
        now: datetime | None = None,
        current_time: datetime | None = None,
        as_of: datetime | None = None,
    ) -> SubmissionSummary:
        """校验答案完整性并提交答卷，提交后冻结答案内容。"""

        # 兼容 submit_submission(id, answers) 的调用形式。
        is_answer_collection = self._looks_like_answer_collection(student_id)
        if is_answer_collection:
            if answers is not None and not self._looks_like_answer_collection(answers):
                # 兼容 submit_submission(id, answers, student_id) 的调用形式。
                student_id, answers = (
                    answers,
                    cast(AnswerCollection | Answer, student_id),
                )
                student_input = cast(StudentInput | None, student_id)
            elif answers is not None:
                raise SubmissionValidationError(
                    "学生标识和答案集合不能同时使用该调用形式。"
                )
            else:
                answers = cast(AnswerCollection | Answer, student_id)
                student_input = None
        else:
            student_input = cast(StudentInput | None, student_id)
        normalized_student_id = _resolve_student_id(
            student_input,
            student,
            user_id,
        )
        submission = self._load_submission(submission_id)
        self._ensure_submission_owner(submission, normalized_student_id)
        current_status = self._submission_status(submission)
        if current_status is not SubmissionStatus.DRAFT:
            raise SubmissionConflictError(
                "答卷已经提交，不能重复提交或覆盖原答案。",
                self._submission_summary(submission),
            )

        entries = _normalize_answer_entries(answers)
        exam = self._submission_exam(submission)
        self._ensure_exam_participation(exam.id, submission.student_id)
        self._validate_answer_question_ids(exam, entries)
        moment = _resolve_time(now, current_time, as_of)
        self._ensure_exam_available(exam, moment)
        prospective = {
            answer.question_id: answer.content for answer in submission.answers
        }
        prospective.update(dict(entries))
        self._validate_complete_answers(exam, prospective)

        self._save_answer_entries(submission, entries, exam)
        for answer in submission.answers:
            answer.status = AnswerStatus.SUBMITTED
        submission.status = SubmissionStatus.SUBMITTED
        submission.submitted_at = moment
        return self._commit_and_reload_submission(submission, "提交答卷失败。")

    submit = submit_submission
    submit_answers = submit_submission
    complete_submission = submit_submission

    def update_submission_status(
        self,
        submission_id: UUID | str,
        status: SubmissionStatus | str,
        student_id: StudentInput | None = None,
        *,
        student: StudentInput | None = None,
        user_id: StudentInput | None = None,
        now: datetime | None = None,
        current_time: datetime | None = None,
        as_of: datetime | None = None,
        mark_answers: bool = False,
    ) -> SubmissionSummary:
        """按 Draft、Submitted、Graded、Reviewed 状态机更新答卷。"""

        normalized_student_id = _resolve_student_id(student_id, student, user_id)
        submission = self._load_submission(submission_id)
        self._ensure_submission_owner(submission, normalized_student_id)
        next_status = _normalize_submission_status(status)
        current_status = self._submission_status(submission)
        if next_status is current_status:
            return self._submission_summary(submission)
        if next_status not in _ALLOWED_SUBMISSION_TRANSITIONS.get(
            current_status, set()
        ):
            raise SubmissionValidationError(
                f"答卷状态不能从“{current_status.value}”变更为“{next_status.value}”。"
            )

        moment = _resolve_time(now, current_time, as_of)
        if next_status is SubmissionStatus.SUBMITTED:
            exam = self._submission_exam(submission)
            self._ensure_exam_participation(exam.id, submission.student_id)
            prospective = {
                answer.question_id: answer.content for answer in submission.answers
            }
            self._ensure_exam_available(exam, moment)
            self._validate_complete_answers(exam, prospective)
            for answer in submission.answers:
                answer.status = AnswerStatus.SUBMITTED
            submission.submitted_at = moment
        elif next_status is SubmissionStatus.GRADED:
            submission.graded_at = moment
            if mark_answers:
                for answer in submission.answers:
                    answer.status = AnswerStatus.GRADED
        elif next_status is SubmissionStatus.REVIEWED:
            submission.reviewed_at = moment
        submission.status = next_status
        return self._commit_and_reload_submission(submission, "更新答卷状态失败。")

    set_submission_status = update_submission_status
    update_status = update_submission_status
    change_submission_status = update_submission_status
    set_status = update_submission_status

    def mark_graded(
        self,
        submission_id: UUID | str,
        student_id: StudentInput | None = None,
        *,
        now: datetime | None = None,
        mark_answers: bool = False,
    ) -> SubmissionSummary:
        """把已提交答卷推进到 Graded 状态。"""

        return self.update_submission_status(
            submission_id,
            SubmissionStatus.GRADED,
            student_id,
            now=now,
            mark_answers=mark_answers,
        )

    def mark_reviewed(
        self,
        submission_id: UUID | str,
        student_id: StudentInput | None = None,
        *,
        now: datetime | None = None,
    ) -> SubmissionSummary:
        """把已评分答卷推进到 Reviewed 状态。"""

        return self.update_submission_status(
            submission_id,
            SubmissionStatus.REVIEWED,
            student_id,
            now=now,
        )

    def update_answer_status(
        self,
        answer_id: UUID | str,
        status: AnswerStatus | str,
        student_id: StudentInput | None = None,
        *,
        student: StudentInput | None = None,
        user_id: StudentInput | None = None,
    ) -> AnswerSummary:
        """按答案处理状态机更新单题答案状态。"""

        normalized_student_id = _resolve_student_id(student_id, student, user_id)
        answer = self._load_answer(answer_id)
        submission = self._load_submission(answer.submission_id)
        self._ensure_submission_owner(submission, normalized_student_id)
        next_status = _normalize_answer_status(status)
        current_status = self._answer_status(answer)
        if next_status is current_status:
            return self._answer_summary(answer)
        if next_status not in _ALLOWED_ANSWER_TRANSITIONS.get(current_status, set()):
            raise SubmissionValidationError(
                f"答案状态不能从“{current_status.value}”变更为“{next_status.value}”。"
            )
        answer.status = next_status
        try:
            self.session.commit()
        except SQLAlchemyError as exc:
            self.session.rollback()
            raise SubmissionServiceError("更新答案状态失败。") from exc
        return self._reload_answer_summary(answer.id)

    set_answer_status = update_answer_status

    def get_submission(
        self,
        submission_id: UUID | str,
        student_id: StudentInput | None = None,
        *,
        student: StudentInput | None = None,
        user_id: StudentInput | None = None,
    ) -> SubmissionSummary:
        """读取答卷，并按需校验学生所有权。"""

        normalized_student_id = _resolve_student_id(student_id, student, user_id)
        submission = self._load_submission(submission_id)
        self._ensure_submission_owner(submission, normalized_student_id)
        return self._submission_summary(submission)

    get_submission_detail = get_submission

    def get_submission_for_exam(
        self,
        exam_id: UUID | str,
        student_id: StudentInput,
        *,
        student: StudentInput | None = None,
    ) -> SubmissionSummary:
        """读取学生在指定考试中的已有答卷。"""

        normalized_exam_id = _normalize_uuid(exam_id, "考试标识")
        normalized_student_id = _resolve_student_id(student_id, student)
        if normalized_student_id is None:
            raise SubmissionValidationError("学生标识不能为空。")
        self._load_student(normalized_student_id)
        submission = self._find_submission_entity(
            normalized_exam_id,
            normalized_student_id,
        )
        if submission is None:
            raise SubmissionNotFoundError("该学生尚未创建此考试的答卷。")
        return self._submission_summary(submission)

    def find_submission(
        self,
        exam_id: UUID | str,
        student_id: StudentInput,
        *,
        student: StudentInput | None = None,
    ) -> SubmissionSummary | None:
        """查询学生在指定考试中的答卷，不存在时返回空值。"""

        normalized_exam_id = _normalize_uuid(exam_id, "考试标识")
        normalized_student_id = _resolve_student_id(student_id, student)
        if normalized_student_id is None:
            raise SubmissionValidationError("学生标识不能为空。")
        self._load_student(normalized_student_id)
        submission = self._find_submission_entity(
            normalized_exam_id,
            normalized_student_id,
        )
        return self._submission_summary(submission) if submission is not None else None

    def list_submissions(
        self,
        student_id: StudentInput | None = None,
        exam_id: UUID | str | None = None,
        status: SubmissionStatus | str | None = None,
        *,
        student: StudentInput | None = None,
        user_id: StudentInput | None = None,
        submission_status: SubmissionStatus | str | None = None,
    ) -> list[SubmissionSummary]:
        """按学生、考试或状态查询答卷摘要。"""

        normalized_student_id = _resolve_student_id(student_id, student, user_id)
        if normalized_student_id is not None:
            self._load_student(normalized_student_id)
        normalized_exam_id = (
            _normalize_uuid(exam_id, "考试标识") if exam_id is not None else None
        )
        normalized_status = self._resolve_status_filter(status, submission_status)
        statement = select(Submission).options(
            selectinload(Submission.answers),
            selectinload(Submission.exam).selectinload(Exam.questions),
        )
        if normalized_student_id is not None:
            statement = statement.where(Submission.student_id == normalized_student_id)
        if normalized_exam_id is not None:
            statement = statement.where(Submission.exam_id == normalized_exam_id)
        if normalized_status is not None:
            statement = statement.where(Submission.status == normalized_status)
        statement = statement.order_by(Submission.created_at, Submission.id)
        try:
            submissions = self.session.scalars(statement).all()
        except SQLAlchemyError as exc:
            raise SubmissionServiceError("无法读取答卷列表。") from exc
        return [self._submission_summary(item) for item in submissions]

    list_student_submissions = list_submissions

    def list_answers(
        self,
        submission_id: UUID | str,
        student_id: StudentInput | None = None,
        *,
        student: StudentInput | None = None,
        user_id: StudentInput | None = None,
    ) -> list[AnswerSummary]:
        """读取答卷内的全部答案。"""

        summary = self.get_submission(
            submission_id,
            student_id,
            student=student,
            user_id=user_id,
        )
        return summary.answers

    get_answers = list_answers
    get_submission_answers = list_answers

    def get_answer(
        self,
        answer_id: UUID | str,
        student_id: StudentInput | None = None,
        *,
        student: StudentInput | None = None,
        user_id: StudentInput | None = None,
    ) -> AnswerSummary:
        """读取单题答案，并按需校验答卷所有权。"""

        normalized_student_id = _resolve_student_id(student_id, student, user_id)
        answer = self._load_answer(answer_id)
        submission = self._load_submission(answer.submission_id)
        self._ensure_submission_owner(submission, normalized_student_id)
        return self._answer_summary(answer)

    def _save_answers_internal(
        self,
        submission_id: UUID | str,
        entries: list[tuple[UUID, AnswerContent]],
        student_id: UUID | None,
    ) -> list[AnswerSummary]:
        """执行答案批量校验、写入和一次提交。"""

        if not entries:
            raise SubmissionValidationError("至少需要保存一道题的答案。")
        submission = self._load_submission(submission_id)
        self._ensure_submission_owner(submission, student_id)
        if self._submission_status(submission) is not SubmissionStatus.DRAFT:
            raise SubmissionConflictError(
                "答卷已经提交，不能修改答案。",
                self._submission_summary(submission),
            )
        exam = self._submission_exam(submission)
        self._ensure_exam_participation(exam.id, submission.student_id)
        self._validate_answer_question_ids(exam, entries)
        self._save_answer_entries(submission, entries, exam)
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise SubmissionConflictError("保存答案时发生重复或数据冲突。") from exc
        except SQLAlchemyError as exc:
            self.session.rollback()
            raise SubmissionServiceError("保存答案失败。") from exc

        refreshed = self._load_submission(submission.id)
        answers_by_question = {
            answer.question_id: answer for answer in refreshed.answers
        }
        return [
            self._answer_summary(answers_by_question[question_id])
            for question_id, _ in entries
        ]

    def _save_answer_entries(
        self,
        submission: Submission,
        entries: list[tuple[UUID, AnswerContent]],
        exam: Exam,
    ) -> None:
        """在已完成输入校验后将答案写入 ORM 实体。"""

        self._validate_answer_question_ids(exam, entries)
        existing_by_question = {
            answer.question_id: answer for answer in submission.answers
        }
        # 先完整检查可更新答案，避免批量写入中途留下半套内存变更。
        for question_id, _ in entries:
            existing = existing_by_question.get(question_id)
            if (
                existing is not None
                and self._answer_status(existing) is not AnswerStatus.DRAFT
            ):
                raise SubmissionConflictError("已处理的答案不能被覆盖。")
        for question_id, content in entries:
            existing = existing_by_question.get(question_id)
            if existing is not None:
                existing.content = content
                continue
            answer = Answer(
                submission_id=submission.id,
                question_id=question_id,
                content=content,
                status=AnswerStatus.DRAFT,
            )
            submission.answers.append(answer)
            existing_by_question[question_id] = answer

    @staticmethod
    def _initialize_answer_entities(submission: Submission, exam: Exam) -> None:
        """按考试题目创建空的草稿答案占位记录。"""

        existing_ids = {answer.question_id for answer in submission.answers}
        for question in exam.questions:
            if question.id in existing_ids:
                continue
            submission.answers.append(
                Answer(
                    submission_id=submission.id,
                    question_id=question.id,
                    content=None,
                    status=AnswerStatus.DRAFT,
                )
            )

    @staticmethod
    def _looks_like_answer_collection(value: Any) -> bool:
        """判断参数是否更像答案集合而不是学生标识。"""

        return isinstance(value, (Mapping, Answer)) or (
            isinstance(value, Iterable)
            and not isinstance(value, (str, bytes, bytearray, UUID, User))
        )

    @staticmethod
    def _submission_status(submission: Submission) -> SubmissionStatus:
        """读取并规范化答卷实体状态。"""

        return _normalize_submission_status(submission.status)

    @staticmethod
    def _answer_status(answer: Answer) -> AnswerStatus:
        """读取并规范化答案实体状态。"""

        return _normalize_answer_status(answer.status)

    @staticmethod
    def _submission_exam(submission: Submission) -> Exam:
        """返回已由查询预加载的考试实体。"""

        exam = getattr(submission, "exam", None)
        if not isinstance(exam, Exam):
            raise SubmissionServiceError("答卷关联的考试信息不可用。")
        return exam

    @staticmethod
    def _validate_answer_question_ids(
        exam: Exam,
        entries: Iterable[tuple[UUID, AnswerContent]],
    ) -> None:
        """拒绝不属于当前考试的答案题目标识。"""

        question_ids = {question.id for question in exam.questions}
        unknown_ids = [
            question_id for question_id, _ in entries if question_id not in question_ids
        ]
        if unknown_ids:
            formatted = "、".join(str(question_id) for question_id in unknown_ids)
            raise SubmissionValidationError(f"题目不属于当前考试：{formatted}。")

    @staticmethod
    def _validate_complete_answers(
        exam: Exam,
        answers: Mapping[UUID, AnswerContent],
    ) -> None:
        """确认提交时每道考试题都有非空答案。"""

        if not exam.questions:
            raise SubmissionValidationError("考试没有可提交的题目。")
        expected_ids = {question.id for question in exam.questions}
        extra_ids = [
            question_id for question_id in answers if question_id not in expected_ids
        ]
        if extra_ids:
            formatted = "、".join(str(question_id) for question_id in extra_ids)
            raise SubmissionValidationError(f"题目不属于当前考试：{formatted}。")
        missing_ids = [
            question.id
            for question in exam.questions
            if question.id not in answers or _is_blank_answer(answers[question.id])
        ]
        if missing_ids:
            formatted = "、".join(str(question_id) for question_id in missing_ids)
            raise SubmissionValidationError(
                f"提交答卷前必须完成全部题目：{formatted}。"
            )

    @staticmethod
    def _exam_participation_filter(student_id: UUID | None) -> ColumnElement[bool]:
        """仅允许已分配学生；没有分配记录的考试兼容历史全体开放行为。"""

        assignments = select(ExamParticipant.id).where(ExamParticipant.exam_id == Exam.id)
        return or_(
            ~assignments.exists(),
            assignments.where(ExamParticipant.student_id == student_id).exists(),
        )

    def _ensure_exam_participation(self, exam_id: UUID, student_id: UUID | None) -> None:
        """按当前数据库分配记录检查资格，避免已有草稿绕过范围变化。"""

        try:
            allowed_exam = self.session.scalar(
                select(Exam.id).where(
                    Exam.id == exam_id,
                    self._exam_participation_filter(student_id),
                )
            )
        except SQLAlchemyError as exc:
            raise SubmissionServiceError("无法检查考试分配信息。") from exc
        if allowed_exam is None:
            raise SubmissionPermissionError("当前学生未被分配参加此考试。")

    def _ensure_exam_available(self, exam: Exam, moment: datetime) -> None:
        """校验考试状态、时间窗和题目关系是否允许学生参加。"""

        status = _normalize_exam_status(exam.status)
        if status is not ExamStatus.PUBLISHED:
            raise SubmissionNotAvailableError("考试当前未开放参加。")
        starts_at = exam.starts_at
        if starts_at is not None and moment < _as_utc(starts_at):
            raise SubmissionNotAvailableError("考试尚未到开放时间。")
        ends_at = exam.ends_at
        if ends_at is not None and moment >= _as_utc(ends_at):
            raise SubmissionNotAvailableError("考试参加时间已经结束。")
        if not exam.questions:
            raise SubmissionNotAvailableError("考试尚未配置题目，暂不能参加。")
        for question in exam.questions:
            if question.course_id != exam.course_id:
                raise SubmissionNotAvailableError("考试包含不属于当前课程的题目。")
            if not _is_approved_question(question):
                raise SubmissionNotAvailableError("考试包含未审核题目，暂不能参加。")

    def _load_student(self, student_id: UUID | str | User) -> User:
        """加载启用的学生账号并执行角色边界检查。"""

        normalized_id = _normalize_uuid(student_id, "学生标识")
        try:
            user = self.session.scalar(
                select(User)
                .options(selectinload(User.roles))
                .where(User.id == normalized_id)
            )
        except SQLAlchemyError as exc:
            raise SubmissionServiceError("无法读取学生账号信息。") from exc
        if user is None:
            raise SubmissionNotFoundError("学生账号不存在。")
        if not user.is_active:
            raise SubmissionPermissionError("学生账号已停用。")

        roles: set[UserRole] = set()
        for role in getattr(user, "roles", ()) or ():
            raw_role = getattr(role, "name", role)
            try:
                roles.add(_normalize_user_role(raw_role))
            except (TypeError, ValueError):
                continue
        # 历史数据可能尚未初始化角色；有明确角色时则必须包含 Student。
        if roles and UserRole.STUDENT not in roles:
            raise SubmissionPermissionError("只有学生账号可以参加考试。")
        return user

    def _load_exam(self, exam_id: UUID | str, *, for_update: bool = False) -> Exam:
        """加载考试及其题目关系。"""

        normalized_id = _normalize_uuid(exam_id, "考试标识")
        statement = (
            select(Exam)
            .options(selectinload(Exam.questions))
            .where(Exam.id == normalized_id)
        )
        if for_update:
            statement = statement.with_for_update()
        try:
            exam = self.session.scalar(statement)
        except SQLAlchemyError as exc:
            raise SubmissionServiceError("无法读取考试信息。") from exc
        if exam is None:
            raise SubmissionNotFoundError("考试不存在。")
        return exam

    def _load_submission(self, submission_id: UUID | str) -> Submission:
        """加载答卷、答案、考试和考试题目关系。"""

        normalized_id = _normalize_uuid(submission_id, "答卷标识")
        statement = (
            select(Submission)
            .options(
                selectinload(Submission.answers),
                selectinload(Submission.exam).selectinload(Exam.questions),
            )
            .where(Submission.id == normalized_id)
        )
        try:
            submission = self.session.scalar(statement)
        except SQLAlchemyError as exc:
            raise SubmissionServiceError("无法读取答卷信息。") from exc
        if submission is None:
            raise SubmissionNotFoundError("答卷不存在。")
        return submission

    def _load_answer(self, answer_id: UUID | str) -> Answer:
        """加载单题答案实体。"""

        normalized_id = _normalize_uuid(answer_id, "答案标识")
        try:
            answer = self.session.scalar(
                select(Answer).where(Answer.id == normalized_id)
            )
        except SQLAlchemyError as exc:
            raise SubmissionServiceError("无法读取答案信息。") from exc
        if answer is None:
            raise SubmissionNotFoundError("答案不存在。")
        return answer

    def _find_submission_entity(
        self,
        exam_id: UUID,
        student_id: UUID,
    ) -> Submission | None:
        """按考试和学生查找已有答卷。"""

        statement = (
            select(Submission)
            .options(
                selectinload(Submission.answers),
                selectinload(Submission.exam).selectinload(Exam.questions),
            )
            .where(
                Submission.exam_id == exam_id,
                Submission.student_id == student_id,
            )
            .order_by(Submission.created_at, Submission.id)
        )
        try:
            return self.session.scalars(statement).first()
        except SQLAlchemyError as exc:
            raise SubmissionServiceError("无法查询已有答卷。") from exc

    def _ensure_submission_owner(
        self,
        submission: Submission,
        student_id: UUID | None,
    ) -> None:
        """确认提供的学生只能访问自己的答卷。"""

        if student_id is not None and submission.student_id != student_id:
            raise SubmissionPermissionError("无权访问其他学生的答卷。")

    def _commit_and_reload_submission(
        self,
        submission: Submission,
        fallback_message: str,
    ) -> SubmissionSummary:
        """提交答卷变更并重新加载完整关系。"""

        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise SubmissionConflictError("答卷变更与现有数据冲突。") from exc
        except SQLAlchemyError as exc:
            self.session.rollback()
            raise SubmissionServiceError(fallback_message) from exc
        return self._reload_submission_summary(submission.id)

    def _reload_submission_summary(
        self, submission_id: UUID | None
    ) -> SubmissionSummary:
        """按主键重新加载答卷摘要，避免关系缓存不完整。"""

        if submission_id is None:
            raise SubmissionServiceError("答卷标识未生成。")
        return self._submission_summary(self._load_submission(submission_id))

    def _reload_answer_summary(self, answer_id: UUID | None) -> AnswerSummary:
        """按主键重新加载答案摘要。"""

        if answer_id is None:
            raise SubmissionServiceError("答案标识未生成。")
        answer = self._load_answer(answer_id)
        return self._answer_summary(answer)

    @staticmethod
    def _answer_summary(answer: Answer) -> AnswerSummary:
        """将答案实体转换为不可变摘要。"""

        return AnswerSummary(
            id=str(answer.id),
            submission_id=str(answer.submission_id),
            question_id=str(answer.question_id),
            content=_normalize_answer_content(answer.content),
            status=_normalize_answer_status(answer.status),
            created_at=answer.created_at,
            updated_at=answer.updated_at,
        )

    @classmethod
    def _submission_summary(cls, submission: Submission) -> SubmissionSummary:
        """将答卷实体及答案关系转换为摘要。"""

        exam = cls._submission_exam(submission)
        questions = list(exam.questions or ())
        question_order = {
            question.id: index for index, question in enumerate(questions)
        }
        answer_entities = sorted(
            submission.answers or (),
            key=lambda answer: (
                question_order.get(answer.question_id, len(question_order)),
                str(answer.id),
            ),
        )
        answers = [cls._answer_summary(answer) for answer in answer_entities]
        answers_by_question = {answer.question_id: answer for answer in answer_entities}
        missing_question_ids = [
            str(question.id)
            for question in questions
            if question.id not in answers_by_question
            or _is_blank_answer(
                _normalize_answer_content(answers_by_question[question.id].content)
            )
        ]
        return SubmissionSummary(
            id=str(submission.id),
            exam_id=str(submission.exam_id),
            student_id=str(submission.student_id),
            status=_normalize_submission_status(submission.status),
            submitted_at=submission.submitted_at,
            graded_at=submission.graded_at,
            reviewed_at=submission.reviewed_at,
            answers=answers,
            answer_count=len(answers),
            question_count=len(questions),
            missing_question_ids=missing_question_ids,
            created_at=submission.created_at,
            updated_at=submission.updated_at,
        )

    @staticmethod
    def _available_exam_summary(exam: Exam) -> AvailableExamSummary:
        """将已开放考试转换为学生可读取的摘要。"""

        questions = list(exam.questions or ())
        total_score = sum(
            (Decimal(str(question.score)) for question in questions),
            _ZERO_SCORE,
        )
        return AvailableExamSummary(
            id=str(exam.id),
            course_id=str(exam.course_id),
            created_by=str(exam.created_by),
            title=exam.title,
            description=exam.description,
            status=_normalize_exam_status(exam.status),
            duration_minutes=exam.duration_minutes,
            starts_at=exam.starts_at,
            ends_at=exam.ends_at,
            question_ids=[str(question.id) for question in questions],
            question_count=len(questions),
            total_score=total_score,
            created_at=exam.created_at,
            updated_at=exam.updated_at,
        )

    @staticmethod
    def _resolve_status_filter(
        status: SubmissionStatus | str | None,
        submission_status: SubmissionStatus | str | None,
    ) -> SubmissionStatus | None:
        """兼容 status 和 submission_status 两种筛选参数名称。"""

        normalized_status = (
            _normalize_submission_status(status) if status is not None else None
        )
        normalized_alias = (
            _normalize_submission_status(submission_status)
            if submission_status is not None
            else None
        )
        if (
            normalized_status is not None
            and normalized_alias is not None
            and normalized_status != normalized_alias
        ):
            raise SubmissionValidationError("答卷状态筛选条件不一致。")
        return normalized_status or normalized_alias


def _normalize_exam_status(value: ExamStatus | str) -> ExamStatus:
    """将考试状态名称或枚举值规范化为领域枚举。"""

    if isinstance(value, ExamStatus):
        return value
    if not isinstance(value, str):
        raise SubmissionValidationError("考试状态无效。")
    candidate = value.strip()
    for status in ExamStatus:
        if candidate.lower() in {status.name.lower(), status.value.lower()}:
            return status
    raise SubmissionValidationError("考试状态无效。")


def _normalize_user_role(value: UserRole | str) -> UserRole:
    """将用户角色名称或枚举值规范化为领域枚举。"""

    if isinstance(value, UserRole):
        return value
    if not isinstance(value, str):
        raise TypeError("角色值无效。")
    candidate = value.strip()
    for role in UserRole:
        if candidate.lower() in {role.name.lower(), role.value.lower()}:
            return role
    raise ValueError("角色值无效。")


def _is_approved_question(question: Any) -> bool:
    """判断考试题目是否处于允许学生作答的审核状态。"""

    value = getattr(question, "status", None)
    if isinstance(value, QuestionStatus):
        return value is QuestionStatus.APPROVED
    if isinstance(value, str):
        return value.strip().lower() in {
            QuestionStatus.APPROVED.name.lower(),
            QuestionStatus.APPROVED.value.lower(),
        }
    return False


__all__ = [
    "AnswerContent",
    "AnswerSummary",
    "AvailableExamSummary",
    "DuplicateSubmissionError",
    "ExamNotAvailableError",
    "ExamSummary",
    "SubmissionConflictError",
    "SubmissionNotAvailableError",
    "SubmissionNotFoundError",
    "SubmissionPermissionError",
    "SubmissionService",
    "SubmissionServiceError",
    "SubmissionSummary",
    "SubmissionValidationError",
]
