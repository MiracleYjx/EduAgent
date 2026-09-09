"""考试服务：考试创建、题目组卷和发布规则。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from backend.app.domain.enums import ExamStatus, QuestionStatus
from backend.app.models import Course, Exam, Question, User

_UNSET = object()
_ZERO_SCORE = Decimal("0.00")
_ALLOWED_STATUS_TRANSITIONS = {
    ExamStatus.DRAFT: {ExamStatus.PUBLISHED},
    ExamStatus.PUBLISHED: {ExamStatus.CLOSED, ExamStatus.ARCHIVED},
    ExamStatus.CLOSED: {ExamStatus.ARCHIVED},
    ExamStatus.ARCHIVED: set(),
}


class ExamServiceError(RuntimeError):
    """考试服务的异常基类。"""


class ExamNotFoundError(ExamServiceError):
    """考试、课程或题目不存在时抛出。"""


class ExamConflictError(ExamServiceError):
    """考试关联或数据约束发生冲突时抛出。"""


class ExamValidationError(ExamServiceError):
    """考试输入或生命周期规则不满足时抛出。"""


class ExamPermissionError(ExamServiceError):
    """当前教师无权访问考试所属课程时抛出。"""


class ExamSummary(BaseModel):
    """面向 API 和 UI 的考试元数据摘要。"""

    model_config = ConfigDict(frozen=True)

    id: str
    course_id: str
    created_by: str
    title: str
    description: str | None = None
    status: ExamStatus
    duration_minutes: int | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    question_ids: list[str]
    question_count: int
    total_score: Decimal
    created_at: datetime
    updated_at: datetime

    @property
    def total_points(self) -> Decimal:
        """兼容按总分命名的调用方。"""

        return self.total_score


QuestionInput = UUID | str | Question
QuestionInputs = Iterable[QuestionInput] | QuestionInput


def _normalize_uuid(value: UUID | str | Any, field_name: str) -> UUID:
    """将外部标识规范化为 UUID。"""

    if value is None:
        raise ExamValidationError(f"{field_name}不能为空。")
    try:
        return value if isinstance(value, UUID) else UUID(str(value).strip())
    except (AttributeError, TypeError, ValueError) as exc:
        raise ExamValidationError(f"{field_name}无效。") from exc


def _resolve_actor_id(
    created_by: UUID | str | None,
    teacher_id: UUID | str | None,
) -> UUID | None:
    """兼容创建者和教师标识，并拒绝互相冲突的输入。"""

    normalized_creator = (
        _normalize_uuid(created_by, "创建者标识") if created_by is not None else None
    )
    normalized_teacher = (
        _normalize_uuid(teacher_id, "教师标识") if teacher_id is not None else None
    )
    if (
        normalized_creator is not None
        and normalized_teacher is not None
        and normalized_creator != normalized_teacher
    ):
        raise ExamValidationError("创建者标识与教师标识不一致。")
    return normalized_creator or normalized_teacher


def _normalize_required_text(
    value: str | None,
    field_name: str,
    *,
    max_length: int,
) -> str:
    """清理必填文本并执行长度校验。"""

    if not isinstance(value, str) or not value.strip():
        raise ExamValidationError(f"{field_name}不能为空。")
    normalized = value.strip()
    if "\x00" in normalized:
        raise ExamValidationError(f"{field_name}包含无效字符。")
    if len(normalized) > max_length:
        raise ExamValidationError(f"{field_name}长度不能超过 {max_length} 个字符。")
    return normalized


def _normalize_optional_text(
    value: str | None,
    field_name: str,
    *,
    max_length: int,
) -> str | None:
    """清理可选文本，空白文本统一转换为空值。"""

    if value is None:
        return None
    if not isinstance(value, str):
        raise ExamValidationError(f"{field_name}输入无效。")
    normalized = value.strip()
    if "\x00" in normalized:
        raise ExamValidationError(f"{field_name}包含无效字符。")
    if len(normalized) > max_length:
        raise ExamValidationError(f"{field_name}长度不能超过 {max_length} 个字符。")
    return normalized or None


def _normalize_duration(value: int | None) -> int | None:
    """校验考试时长必须是正整数。"""

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExamValidationError("考试时长必须是正整数。")
    if value <= 0:
        raise ExamValidationError("考试时长必须大于 0 分钟。")
    return value


def _validate_schedule(
    starts_at: datetime | None,
    ends_at: datetime | None,
) -> None:
    """校验考试时间范围和时间类型。"""

    for value, field_name in (
        (starts_at, "开始时间"),
        (ends_at, "结束时间"),
    ):
        if value is not None and not isinstance(value, datetime):
            raise ExamValidationError(f"{field_name}输入无效。")
    if starts_at is None or ends_at is None:
        return
    try:
        if ends_at <= starts_at:
            raise ExamValidationError("考试结束时间必须晚于开始时间。")
    except TypeError as exc:
        raise ExamValidationError("开始时间和结束时间的时区信息必须一致。") from exc


def _normalize_exam_status(value: ExamStatus | str) -> ExamStatus:
    """将考试状态名称或枚举值规范化为领域枚举。"""

    if isinstance(value, ExamStatus):
        return value
    if not isinstance(value, str):
        raise ExamValidationError("考试状态无效。")
    candidate = value.strip()
    for supported_status in ExamStatus:
        if candidate.lower() in {
            supported_status.name.lower(),
            supported_status.value.lower(),
        }:
            return supported_status
    raise ExamValidationError("考试状态无效。")


def _normalize_question_ids(values: QuestionInputs | None) -> list[UUID]:
    """规范化题目标识，去除隐式歧义并拒绝重复题目。"""

    if values is None:
        return []
    if isinstance(values, (str, UUID, Question)):
        candidates: Iterable[QuestionInput] = (values,)
    elif isinstance(values, (Mapping, bytes, bytearray)):
        raise ExamValidationError("题目列表必须是题目标识序列。")
    else:
        try:
            candidates = iter(values)
        except TypeError as exc:
            raise ExamValidationError("题目列表必须是题目标识序列。") from exc

    normalized: list[UUID] = []
    for candidate in candidates:
        raw_id = candidate.id if isinstance(candidate, Question) else candidate
        question_id = _normalize_uuid(raw_id, "题目标识")
        if question_id in normalized:
            raise ExamValidationError("题目列表不能包含重复题目。")
        normalized.append(question_id)
    return normalized


def _resolve_question_inputs(
    question_ids: QuestionInputs | None,
    questions: QuestionInputs | None,
) -> QuestionInputs | None:
    """兼容 question_ids 和 questions 两种参数名称。"""

    if question_ids is not None and questions is not None:
        first = _normalize_question_ids(question_ids)
        second = _normalize_question_ids(questions)
        if first != second:
            raise ExamValidationError("题目标识参数不一致。")
        return first
    return question_ids if question_ids is not None else questions


class ExamService:
    """封装考试创建、题目关联和发布前业务校验。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create_exam(
        self,
        course_id: UUID | str,
        title: str,
        description: str | None = None,
        duration_minutes: int | None = None,
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
        question_ids: QuestionInputs | None = None,
        created_by: UUID | str | None = None,
        *,
        teacher_id: UUID | str | None = None,
        questions: QuestionInputs | None = None,
    ) -> ExamSummary:
        """创建草稿考试，并在初始组卷时校验所有题目。"""

        normalized_course_id = _normalize_uuid(course_id, "课程标识")
        course = self._load_course(normalized_course_id)
        actor_id = _resolve_actor_id(created_by, teacher_id)
        if actor_id is None:
            raise ExamValidationError("创建者标识不能为空。")
        self._ensure_course_access(course, actor_id)
        self._ensure_user_exists(actor_id)

        normalized_title = _normalize_required_text(title, "考试标题", max_length=160)
        normalized_description = _normalize_optional_text(
            description,
            "考试描述",
            max_length=65535,
        )
        normalized_duration = _normalize_duration(duration_minutes)
        _validate_schedule(starts_at, ends_at)
        normalized_question_ids = _normalize_question_ids(
            _resolve_question_inputs(question_ids, questions),
        )
        selected_questions = self._load_valid_questions(
            normalized_course_id,
            normalized_question_ids,
        )

        exam = Exam(
            course_id=normalized_course_id,
            created_by=actor_id,
            title=normalized_title,
            description=normalized_description,
            status=ExamStatus.DRAFT,
            duration_minutes=normalized_duration,
            starts_at=starts_at,
            ends_at=ends_at,
        )
        exam.questions.extend(selected_questions)
        self.session.add(exam)
        return self._commit_exam(exam, "创建考试失败。")

    def list_exams(
        self,
        course_id: UUID | str | None = None,
        status: ExamStatus | str | None = None,
        teacher_id: UUID | str | None = None,
        *,
        created_by: UUID | str | None = None,
        exam_status: ExamStatus | str | None = None,
    ) -> list[ExamSummary]:
        """列出考试；传入教师标识时只返回其课程下的考试。"""

        normalized_course_id = (
            _normalize_uuid(course_id, "课程标识") if course_id is not None else None
        )
        actor_id = _resolve_actor_id(created_by, teacher_id)
        normalized_status = self._resolve_status_filter(status, exam_status)
        if normalized_course_id is not None:
            course = self._load_course(normalized_course_id)
            self._ensure_course_access(course, actor_id)

        statement = (
            select(Exam)
            .join(Course, Exam.course_id == Course.id)
            .options(selectinload(Exam.questions))
        )
        if normalized_course_id is not None:
            statement = statement.where(Exam.course_id == normalized_course_id)
        if actor_id is not None:
            statement = statement.where(Course.created_by == actor_id)
        if normalized_status is not None:
            statement = statement.where(Exam.status == normalized_status)
        statement = statement.order_by(Exam.created_at, Exam.title, Exam.id)
        try:
            exams = self.session.scalars(statement).all()
        except SQLAlchemyError as exc:
            raise ExamServiceError("无法读取考试列表。") from exc
        return [self._exam_summary(exam) for exam in exams]

    def get_exam(
        self,
        exam_id: UUID | str,
        teacher_id: UUID | str | None = None,
        *,
        created_by: UUID | str | None = None,
    ) -> ExamSummary:
        """读取考试，并按需检查当前教师的课程归属。"""

        exam = self._load_exam(exam_id)
        self._ensure_course_access(
            self._load_course(exam.course_id),
            _resolve_actor_id(created_by, teacher_id),
        )
        return self._exam_summary(exam)

    def update_exam(
        self,
        exam_id: UUID | str,
        title: str | None = None,
        description: str | None | object = _UNSET,
        duration_minutes: int | None | object = _UNSET,
        starts_at: datetime | None | object = _UNSET,
        ends_at: datetime | None | object = _UNSET,
        *,
        teacher_id: UUID | str | None = None,
        created_by: UUID | str | None = None,
    ) -> ExamSummary:
        """修改草稿考试的元数据，发布后不允许改变组卷配置。"""

        if (
            title is None
            and description is _UNSET
            and duration_minutes is _UNSET
            and starts_at is _UNSET
            and ends_at is _UNSET
        ):
            raise ExamValidationError("至少需要提供一个更新字段。")

        exam = self._load_exam(exam_id)
        actor_id = _resolve_actor_id(created_by, teacher_id)
        self._ensure_course_access(self._load_course(exam.course_id), actor_id)
        self._ensure_draft(exam)

        if title is not None:
            exam.title = _normalize_required_text(title, "考试标题", max_length=160)
        if description is not _UNSET:
            exam.description = _normalize_optional_text(
                description,  # type: ignore[arg-type]
                "考试描述",
                max_length=65535,
            )
        if duration_minutes is not _UNSET:
            exam.duration_minutes = _normalize_duration(
                duration_minutes,  # type: ignore[arg-type]
            )
        next_starts_at = exam.starts_at if starts_at is _UNSET else starts_at
        next_ends_at = exam.ends_at if ends_at is _UNSET else ends_at
        _validate_schedule(next_starts_at, next_ends_at)  # type: ignore[arg-type]
        if starts_at is not _UNSET:
            exam.starts_at = starts_at  # type: ignore[assignment]
        if ends_at is not _UNSET:
            exam.ends_at = ends_at  # type: ignore[assignment]
        return self._commit_exam(exam, "更新考试失败。")

    def add_questions(
        self,
        exam_id: UUID | str,
        question_ids: QuestionInputs | None = None,
        *,
        questions: QuestionInputs | None = None,
        teacher_id: UUID | str | None = None,
        created_by: UUID | str | None = None,
    ) -> ExamSummary:
        """向草稿考试追加题目，并强制要求题目已审核且属于同一课程。"""

        exam = self._load_exam(exam_id)
        actor_id = _resolve_actor_id(created_by, teacher_id)
        self._ensure_course_access(self._load_course(exam.course_id), actor_id)
        self._ensure_draft(exam)

        normalized_ids = _normalize_question_ids(
            _resolve_question_inputs(question_ids, questions),
        )
        if not normalized_ids:
            raise ExamValidationError("至少需要提供一道题目。")
        selected_questions = self._load_valid_questions(
            exam.course_id,
            normalized_ids,
        )
        existing_ids = {question.id for question in exam.questions}
        questions_to_add = [
            question
            for question in selected_questions
            if question.id not in existing_ids
        ]
        exam.questions.extend(questions_to_add)
        if not questions_to_add:
            return self._exam_summary(exam)
        return self._commit_exam(exam, "关联考试题目失败。")

    def add_question(
        self,
        exam_id: UUID | str,
        question_id: QuestionInput,
        *,
        teacher_id: UUID | str | None = None,
        created_by: UUID | str | None = None,
    ) -> ExamSummary:
        """向草稿考试追加一道已审核题目。"""

        return self.add_questions(
            exam_id,
            question_id,
            teacher_id=teacher_id,
            created_by=created_by,
        )

    associate_questions = add_questions
    add_questions_to_exam = add_questions

    def remove_questions(
        self,
        exam_id: UUID | str,
        question_ids: QuestionInputs,
        *,
        teacher_id: UUID | str | None = None,
        created_by: UUID | str | None = None,
    ) -> ExamSummary:
        """从草稿考试移除已关联的题目。"""

        exam = self._load_exam(exam_id)
        actor_id = _resolve_actor_id(created_by, teacher_id)
        self._ensure_course_access(self._load_course(exam.course_id), actor_id)
        self._ensure_draft(exam)
        normalized_ids = _normalize_question_ids(question_ids)
        if not normalized_ids:
            raise ExamValidationError("至少需要提供一道题目。")
        existing_ids = {question.id for question in exam.questions}
        missing_ids = [
            question_id
            for question_id in normalized_ids
            if question_id not in existing_ids
        ]
        if missing_ids:
            raise ExamConflictError("指定题目未关联到该考试。")
        exam.questions[:] = [
            question for question in exam.questions if question.id not in normalized_ids
        ]
        return self._commit_exam(exam, "移除考试题目失败。")

    def remove_question(
        self,
        exam_id: UUID | str,
        question_id: QuestionInput,
        *,
        teacher_id: UUID | str | None = None,
        created_by: UUID | str | None = None,
    ) -> ExamSummary:
        """从草稿考试移除一道题目。"""

        return self.remove_questions(
            exam_id,
            question_id,
            teacher_id=teacher_id,
            created_by=created_by,
        )

    def publish_exam(
        self,
        exam_id: UUID | str,
        teacher_id: UUID | str | None = None,
        *,
        created_by: UUID | str | None = None,
    ) -> ExamSummary:
        """执行发布检查并将考试置为 Published。"""

        exam = self._load_exam(exam_id)
        actor_id = _resolve_actor_id(created_by, teacher_id)
        self._ensure_course_access(self._load_course(exam.course_id), actor_id)
        if exam.status is ExamStatus.PUBLISHED:
            self._validate_publish_requirements(exam)
            return self._exam_summary(exam)
        self._ensure_draft(exam)
        self._validate_publish_requirements(exam)
        exam.status = ExamStatus.PUBLISHED
        return self._commit_exam(exam, "发布考试失败。")

    publish = publish_exam

    def update_exam_status(
        self,
        exam_id: UUID | str,
        status: ExamStatus | str,
        teacher_id: UUID | str | None = None,
        *,
        created_by: UUID | str | None = None,
    ) -> ExamSummary:
        """按考试生命周期更新状态，发布状态必须经过完整检查。"""

        next_status = _normalize_exam_status(status)
        if next_status is ExamStatus.PUBLISHED:
            return self.publish_exam(
                exam_id,
                teacher_id=teacher_id,
                created_by=created_by,
            )

        exam = self._load_exam(exam_id)
        actor_id = _resolve_actor_id(created_by, teacher_id)
        self._ensure_course_access(self._load_course(exam.course_id), actor_id)
        if next_status is exam.status:
            return self._exam_summary(exam)
        if next_status not in _ALLOWED_STATUS_TRANSITIONS.get(exam.status, set()):
            raise ExamValidationError(
                f"考试状态不能从“{exam.status.value}”变更为“{next_status.value}”。"
            )
        exam.status = next_status
        return self._commit_exam(exam, "更新考试状态失败。")

    set_exam_status = update_exam_status

    def _load_course(self, course_id: UUID | str) -> Course:
        """加载课程并统一处理不存在错误。"""

        normalized_id = _normalize_uuid(course_id, "课程标识")
        try:
            course = self.session.scalar(
                select(Course).where(Course.id == normalized_id)
            )
        except SQLAlchemyError as exc:
            raise ExamServiceError("无法读取课程信息。") from exc
        if course is None:
            raise ExamNotFoundError("课程不存在。")
        return course

    def _load_exam(self, exam_id: UUID | str) -> Exam:
        """加载考试及其题目关系。"""

        normalized_id = _normalize_uuid(exam_id, "考试标识")
        try:
            exam = self.session.scalar(
                select(Exam)
                .options(selectinload(Exam.questions))
                .where(Exam.id == normalized_id)
            )
        except SQLAlchemyError as exc:
            raise ExamServiceError("无法读取考试信息。") from exc
        if exam is None:
            raise ExamNotFoundError("考试不存在。")
        return exam

    def _load_valid_questions(
        self,
        course_id: UUID,
        question_ids: list[UUID],
    ) -> list[Question]:
        """批量读取题目并验证课程归属和审核状态。"""

        if not question_ids:
            return []
        try:
            questions = self.session.scalars(
                select(Question).where(Question.id.in_(question_ids))
            ).all()
        except SQLAlchemyError as exc:
            raise ExamServiceError("无法读取组卷题目。") from exc

        by_id = {question.id: question for question in questions}
        missing_ids = [
            question_id for question_id in question_ids if question_id not in by_id
        ]
        if missing_ids:
            missing = "、".join(str(question_id) for question_id in missing_ids)
            raise ExamNotFoundError(f"题目不存在：{missing}。")

        ordered_questions: list[Question] = []
        for question_id in question_ids:
            question = by_id[question_id]
            if question.course_id != course_id:
                raise ExamValidationError("考试题目必须属于同一课程。")
            if question.status is not QuestionStatus.APPROVED:
                raise ExamValidationError(
                    f"只有 Approved 状态的题目才能加入考试，题目 {question_id} 当前为“{question.status.value}”。"
                )
            ordered_questions.append(question)
        return ordered_questions

    @staticmethod
    def _ensure_course_access(course: Course, actor_id: UUID | None) -> None:
        """检查当前操作人是否为课程所有者。"""

        if actor_id is not None and course.created_by != actor_id:
            raise ExamPermissionError("无权访问该考试所属课程。")

    def _ensure_user_exists(self, user_id: UUID) -> None:
        """确认考试创建者存在，避免留下不可追溯的考试记录。"""

        try:
            user = self.session.get(User, user_id)
        except SQLAlchemyError as exc:
            raise ExamServiceError("无法读取考试创建者信息。") from exc
        if user is None:
            raise ExamValidationError("考试创建者不存在。")

    @staticmethod
    def _ensure_draft(exam: Exam) -> None:
        """限制只有草稿考试可以修改组卷和元数据。"""

        if exam.status is not ExamStatus.DRAFT:
            raise ExamValidationError("已发布或已结束的考试不能修改。")

    @staticmethod
    def _validate_publish_requirements(exam: Exam) -> None:
        """再次核验发布考试所需的题目边界。"""

        if not exam.questions:
            raise ExamValidationError("考试至少需要关联一道已审核题目后才能发布。")
        for question in exam.questions:
            if question.course_id != exam.course_id:
                raise ExamValidationError("考试题目必须属于同一课程。")
            if question.status is not QuestionStatus.APPROVED:
                raise ExamValidationError(
                    f"只有 Approved 状态的题目才能发布考试，题目 {question.id} 当前为“{question.status.value}”。"
                )

    def _commit_exam(self, exam: Exam, fallback_message: str) -> ExamSummary:
        """提交考试变更并转换为安全摘要。"""

        try:
            self.session.commit()
            self.session.refresh(exam)
        except IntegrityError as exc:
            self.session.rollback()
            raise ExamConflictError("考试信息与现有数据冲突。") from exc
        except SQLAlchemyError as exc:
            self.session.rollback()
            raise ExamServiceError(fallback_message) from exc
        return self._exam_summary(exam)

    @staticmethod
    def _exam_summary(exam: Exam) -> ExamSummary:
        """将考试实体转换为不暴露 ORM 状态的摘要。"""

        questions = list(getattr(exam, "questions", ()) or ())
        total_score = sum(
            (Decimal(str(question.score)) for question in questions),
            _ZERO_SCORE,
        )
        return ExamSummary(
            id=str(exam.id),
            course_id=str(exam.course_id),
            created_by=str(exam.created_by),
            title=exam.title,
            description=exam.description,
            status=exam.status,
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
        status: ExamStatus | str | None,
        exam_status: ExamStatus | str | None,
    ) -> ExamStatus | None:
        """兼容 status 和 exam_status 两种筛选参数名称。"""

        normalized_status = (
            _normalize_exam_status(status) if status is not None else None
        )
        normalized_alias = (
            _normalize_exam_status(exam_status) if exam_status is not None else None
        )
        if (
            normalized_status is not None
            and normalized_alias is not None
            and normalized_status != normalized_alias
        ):
            raise ExamValidationError("考试状态筛选条件不一致。")
        return normalized_status or normalized_alias


__all__ = [
    "ExamConflictError",
    "ExamNotFoundError",
    "ExamPermissionError",
    "ExamService",
    "ExamServiceError",
    "ExamSummary",
    "ExamValidationError",
]
