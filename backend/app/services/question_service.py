"""题目服务：人工题目 CRUD 和审核状态管理。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from backend.app.domain.enums import QuestionStatus, QuestionType
from backend.app.models import Course, Question, User
from backend.app.services.course_service import (
    CourseNotFoundError,
    CoursePermissionError,
    CourseServiceError,
)

_UNSET = object()
_ALLOWED_STATUS_TRANSITIONS = {
    QuestionStatus.DRAFT: {QuestionStatus.PENDING_REVIEW},
    QuestionStatus.PENDING_REVIEW: {
        QuestionStatus.APPROVED,
        QuestionStatus.NEEDS_REVISION,
    },
    QuestionStatus.NEEDS_REVISION: {QuestionStatus.PENDING_REVIEW},
    QuestionStatus.APPROVED: set(),
}
_MAX_SCORE = Decimal("999999.99")
_TWO_PLACES = Decimal("0.01")


class QuestionServiceError(CourseServiceError):
    """题目服务的异常基类。"""


class QuestionNotFoundError(CourseNotFoundError, QuestionServiceError):
    """题目或题目所属课程不存在时抛出。"""


class QuestionConflictError(QuestionServiceError):
    """题目变更违反数据约束时抛出。"""


class QuestionValidationError(QuestionServiceError):
    """题目输入或状态不符合业务规则时抛出。"""


class QuestionPermissionError(CoursePermissionError, QuestionServiceError):
    """当前教师无权访问题目所属课程时抛出。"""


class QuestionSummary(BaseModel):
    """面向 API 和 UI 的题目元数据摘要。"""

    model_config = ConfigDict(frozen=True)

    id: str
    course_id: str
    type: QuestionType
    content: str
    options: dict[str, Any] | list[Any] | None = None
    reference_answer: str | None = None
    scoring_rubric: str | None = None
    difficulty: str | None = None
    knowledge_points: list[str]
    score: Decimal
    status: QuestionStatus
    created_by: str
    created_at: datetime
    updated_at: datetime


def _normalize_uuid(value: UUID | str | None, field_name: str) -> UUID:
    """将外部标识规范化为 UUID。"""

    if value is None:
        raise QuestionValidationError(f"{field_name}不能为空。")
    try:
        return value if isinstance(value, UUID) else UUID(str(value).strip())
    except (AttributeError, TypeError, ValueError) as exc:
        raise QuestionValidationError(f"{field_name}无效。") from exc


def _normalize_required_text(
    value: str | None,
    field_name: str,
    *,
    max_length: int,
) -> str:
    """清理必填文本并执行长度校验。"""

    if not isinstance(value, str) or not value.strip():
        raise QuestionValidationError(f"{field_name}不能为空。")
    normalized = value.strip()
    if "\x00" in normalized:
        raise QuestionValidationError(f"{field_name}包含无效字符。")
    if len(normalized) > max_length:
        raise QuestionValidationError(f"{field_name}长度不能超过 {max_length} 个字符。")
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
        raise QuestionValidationError(f"{field_name}输入无效。")
    normalized = value.strip()
    if "\x00" in normalized:
        raise QuestionValidationError(f"{field_name}包含无效字符。")
    if len(normalized) > max_length:
        raise QuestionValidationError(f"{field_name}长度不能超过 {max_length} 个字符。")
    return normalized or None


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
        raise QuestionValidationError("创建者标识与教师标识不一致。")
    return normalized_creator or normalized_teacher


def _resolve_question_type(
    question_type: QuestionType | str | None,
    type_alias: QuestionType | str | None,
) -> QuestionType:
    """兼容 question_type 和 type 两种题型参数名称。"""

    normalized_type = (
        _normalize_question_type(question_type) if question_type is not None else None
    )
    normalized_alias = (
        _normalize_question_type(type_alias) if type_alias is not None else None
    )
    if (
        normalized_type is not None
        and normalized_alias is not None
        and normalized_type != normalized_alias
    ):
        raise QuestionValidationError("题型输入不一致。")
    result = normalized_type or normalized_alias
    if result is None:
        raise QuestionValidationError("题型不能为空。")
    return result


def _normalize_question_type(value: QuestionType | str) -> QuestionType:
    """将题型名称或枚举值规范化为领域枚举。"""

    if isinstance(value, QuestionType):
        return value
    if not isinstance(value, str):
        raise QuestionValidationError("题型无效。")
    candidate = value.strip()
    for supported_type in QuestionType:
        if candidate.lower() in {
            supported_type.name.lower(),
            supported_type.value.lower(),
        }:
            return supported_type
    raise QuestionValidationError("题型无效。")


def _normalize_question_status(value: QuestionStatus | str) -> QuestionStatus:
    """将审核状态名称或枚举值规范化为领域枚举。"""

    if isinstance(value, QuestionStatus):
        return value
    if not isinstance(value, str):
        raise QuestionValidationError("题目审核状态无效。")
    candidate = value.strip()
    for supported_status in QuestionStatus:
        if candidate.lower() in {
            supported_status.name.lower(),
            supported_status.value.lower(),
        }:
            return supported_status
    raise QuestionValidationError("题目审核状态无效。")


def _normalize_options(
    value: dict[str, Any] | list[Any] | None,
) -> dict[str, Any] | list[Any] | None:
    """校验题目选项为可持久化的 JSON 对象或数组。"""

    if value is None:
        return None
    if not isinstance(value, (dict, list)):
        raise QuestionValidationError("题目选项必须是对象或数组。")
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise QuestionValidationError("题目选项必须是有效的 JSON 数据。") from exc
    return deepcopy(value)


def _normalize_knowledge_points(value: Sequence[str] | None) -> list[str]:
    """清理知识点列表、去重并保留输入顺序。"""

    if value is None:
        return []
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise QuestionValidationError("知识点必须是字符串列表。")
    normalized: list[str] = []
    for point in value:
        if not isinstance(point, str) or not point.strip():
            raise QuestionValidationError("知识点必须是非空字符串。")
        item = point.strip()
        if "\x00" in item:
            raise QuestionValidationError("知识点包含无效字符。")
        if len(item) > 160:
            raise QuestionValidationError("单个知识点长度不能超过 160 个字符。")
        if item not in normalized:
            normalized.append(item)
    return normalized


def _normalize_score(value: Decimal | float | str) -> Decimal:
    """将题目分值规范化为两位小数并拒绝非正数。"""

    if isinstance(value, bool):
        raise QuestionValidationError("题目分值输入无效。")
    try:
        score = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise QuestionValidationError("题目分值输入无效。") from exc
    if not score.is_finite():
        raise QuestionValidationError("题目分值输入无效。")
    if score <= 0:
        raise QuestionValidationError("题目分值必须大于 0。")
    try:
        normalized = score.quantize(_TWO_PLACES)
    except InvalidOperation as exc:
        raise QuestionValidationError("题目分值输入无效。") from exc
    if normalized != score:
        raise QuestionValidationError("题目分值最多保留两位小数。")
    if normalized > _MAX_SCORE:
        raise QuestionValidationError("题目分值不能超过 999999.99。")
    return normalized


class QuestionService:
    """封装人工题目元数据和审核状态相关业务。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create_question(
        self,
        course_id: UUID | str,
        question_type: QuestionType | str | None = None,
        content: str | None = None,
        options: dict[str, Any] | list[Any] | None = None,
        reference_answer: str | None = None,
        scoring_rubric: str | None = None,
        difficulty: str | None = None,
        knowledge_points: Sequence[str] | None = None,
        score: Decimal | float | str = 1,
        created_by: UUID | str | None = None,
        *,
        teacher_id: UUID | str | None = None,
        type: QuestionType | str | None = None,
    ) -> QuestionSummary:
        """创建人工题目，初始状态固定为 Draft。"""

        course = self._load_course(course_id)
        creator_id = _resolve_actor_id(created_by, teacher_id)
        if creator_id is None:
            raise QuestionValidationError("创建者标识不能为空。")
        self._ensure_course_access(course, creator_id)
        self._ensure_user_exists(creator_id)

        question = Question(
            course_id=course.id,
            type=_resolve_question_type(question_type, type),
            content=_normalize_required_text(content, "题目内容", max_length=65535),
            options=_normalize_options(options),
            reference_answer=_normalize_optional_text(
                reference_answer,
                "参考答案",
                max_length=65535,
            ),
            scoring_rubric=_normalize_optional_text(
                scoring_rubric,
                "评分标准",
                max_length=65535,
            ),
            difficulty=_normalize_optional_text(
                difficulty,
                "难度",
                max_length=160,
            ),
            knowledge_points=_normalize_knowledge_points(knowledge_points),
            score=_normalize_score(score),
            status=QuestionStatus.DRAFT,
            created_by=creator_id,
        )
        self.session.add(question)
        return self._commit_question(question, "创建题目失败。")

    def list_questions(
        self,
        course_id: UUID | str | None = None,
        status: QuestionStatus | str | None = None,
        teacher_id: UUID | str | None = None,
        *,
        created_by: UUID | str | None = None,
        question_status: QuestionStatus | str | None = None,
    ) -> list[QuestionSummary]:
        """列出题目；传入教师或课程标识时限制查询范围。"""

        normalized_course_id = (
            _normalize_uuid(course_id, "课程标识") if course_id is not None else None
        )
        actor_id = _resolve_actor_id(created_by, teacher_id)
        normalized_status = _resolve_status_filter(status, question_status)
        if normalized_course_id is not None:
            course = self._load_course(normalized_course_id)
            self._ensure_course_access(course, actor_id)

        statement = select(Question).join(Course, Question.course_id == Course.id)
        if normalized_course_id is not None:
            statement = statement.where(Question.course_id == normalized_course_id)
        if actor_id is not None:
            statement = statement.where(Course.created_by == actor_id)
        if normalized_status is not None:
            statement = statement.where(Question.status == normalized_status)
        statement = statement.order_by(Question.created_at, Question.id)
        try:
            questions = self.session.scalars(statement).all()
        except SQLAlchemyError as exc:
            raise QuestionServiceError("无法读取题目列表。") from exc
        return [self._question_summary(question) for question in questions]

    def get_question(
        self,
        question_id: UUID | str,
        teacher_id: UUID | str | None = None,
        *,
        created_by: UUID | str | None = None,
    ) -> QuestionSummary:
        """读取题目元数据并按需检查课程所有权。"""

        question = self._load_question(question_id)
        self._ensure_course_access(
            self._load_course(question.course_id),
            _resolve_actor_id(created_by, teacher_id),
        )
        return self._question_summary(question)

    def update_question(
        self,
        question_id: UUID | str,
        content: str | None = None,
        options: dict[str, Any] | list[Any] | None | object = _UNSET,
        reference_answer: str | None | object = _UNSET,
        scoring_rubric: str | None | object = _UNSET,
        difficulty: str | None | object = _UNSET,
        knowledge_points: Sequence[str] | None | object = _UNSET,
        score: Decimal | float | str | object = _UNSET,
        *,
        question_type: QuestionType | str | None = None,
        type: QuestionType | str | None = None,
        teacher_id: UUID | str | None = None,
        created_by: UUID | str | None = None,
    ) -> QuestionSummary:
        """更新题目元数据，审核状态由独立状态接口管理。"""

        has_type = question_type is not None or type is not None
        has_update = any(
            (
                content is not None,
                options is not _UNSET,
                reference_answer is not _UNSET,
                scoring_rubric is not _UNSET,
                difficulty is not _UNSET,
                knowledge_points is not _UNSET,
                score is not _UNSET,
                has_type,
            ),
        )
        if not has_update:
            raise QuestionValidationError("至少需要提供一个更新字段。")

        question = self._load_question(question_id)
        self._ensure_course_access(
            self._load_course(question.course_id),
            _resolve_actor_id(created_by, teacher_id),
        )
        if has_type:
            question.type = _resolve_question_type(question_type, type)
        if content is not None:
            question.content = _normalize_required_text(
                content,
                "题目内容",
                max_length=65535,
            )
        if options is not _UNSET:
            question.options = _normalize_options(options)  # type: ignore[arg-type]
        if reference_answer is not _UNSET:
            question.reference_answer = _normalize_optional_text(
                reference_answer,  # type: ignore[arg-type]
                "参考答案",
                max_length=65535,
            )
        if scoring_rubric is not _UNSET:
            question.scoring_rubric = _normalize_optional_text(
                scoring_rubric,  # type: ignore[arg-type]
                "评分标准",
                max_length=65535,
            )
        if difficulty is not _UNSET:
            question.difficulty = _normalize_optional_text(
                difficulty,  # type: ignore[arg-type]
                "难度",
                max_length=160,
            )
        if knowledge_points is not _UNSET:
            question.knowledge_points = _normalize_knowledge_points(
                knowledge_points,  # type: ignore[arg-type]
            )
        if score is not _UNSET:
            question.score = _normalize_score(score)  # type: ignore[arg-type]
        return self._commit_question(question, "更新题目失败。")

    def delete_question(
        self,
        question_id: UUID | str,
        teacher_id: UUID | str | None = None,
        *,
        created_by: UUID | str | None = None,
    ) -> None:
        """删除题目；已被考试引用的题目不能删除。"""

        question = self._load_question(question_id)
        self._ensure_course_access(
            self._load_course(question.course_id),
            _resolve_actor_id(created_by, teacher_id),
        )
        try:
            self.session.delete(question)
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise QuestionConflictError("题目已被考试引用，无法删除。") from exc
        except SQLAlchemyError as exc:
            self.session.rollback()
            raise QuestionServiceError("删除题目失败。") from exc

    def update_question_status(
        self,
        question_id: UUID | str,
        status: QuestionStatus | str,
        teacher_id: UUID | str | None = None,
        *,
        created_by: UUID | str | None = None,
    ) -> QuestionSummary:
        """按审核状态机持久化题目状态。"""

        question = self._load_question(question_id)
        self._ensure_course_access(
            self._load_course(question.course_id),
            _resolve_actor_id(created_by, teacher_id),
        )
        next_status = _normalize_question_status(status)
        current_status = question.status
        if next_status == current_status:
            return self._question_summary(question)
        if next_status not in _ALLOWED_STATUS_TRANSITIONS.get(current_status, set()):
            raise QuestionValidationError(
                f"题目状态不能从“{current_status.value}”变更为“{next_status.value}”。"
            )
        question.status = next_status
        return self._commit_question(question, "更新题目审核状态失败。")

    set_question_status = update_question_status

    def _load_course(self, course_id: UUID | str) -> Course:
        """加载课程并统一处理不存在错误。"""

        normalized_id = _normalize_uuid(course_id, "课程标识")
        try:
            course = self.session.scalar(
                select(Course).where(Course.id == normalized_id)
            )
        except SQLAlchemyError as exc:
            raise QuestionServiceError("无法读取课程信息。") from exc
        if course is None:
            raise QuestionNotFoundError("课程不存在。")
        return course

    def _load_question(self, question_id: UUID | str) -> Question:
        """加载题目并统一处理不存在错误。"""

        normalized_id = _normalize_uuid(question_id, "题目标识")
        try:
            question = self.session.scalar(
                select(Question)
                .options(selectinload(Question.course))
                .where(Question.id == normalized_id)
            )
        except SQLAlchemyError as exc:
            raise QuestionServiceError("无法读取题目信息。") from exc
        if question is None:
            raise QuestionNotFoundError("题目不存在。")
        return question

    @staticmethod
    def _ensure_course_access(course: Course, actor_id: UUID | None) -> None:
        """检查当前操作人是否为课程所有者。"""

        if actor_id is not None and course.created_by != actor_id:
            raise QuestionPermissionError("无权访问该题目所属课程。")

    def _ensure_user_exists(self, user_id: UUID) -> None:
        """确认题目创建者存在，保留完整的来源关系。"""

        try:
            user = self.session.get(User, user_id)
        except SQLAlchemyError as exc:
            raise QuestionServiceError("无法读取题目创建者信息。") from exc
        if user is None:
            raise QuestionValidationError("题目创建者不存在。")

    def _commit_question(
        self,
        question: Question,
        fallback_message: str,
    ) -> QuestionSummary:
        """提交题目变更并转换为安全摘要。"""

        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise QuestionConflictError("题目信息与现有数据冲突。") from exc
        except SQLAlchemyError as exc:
            self.session.rollback()
            raise QuestionServiceError(fallback_message) from exc

        self.session.refresh(question)
        return self._question_summary(question)

    @staticmethod
    def _question_summary(question: Question) -> QuestionSummary:
        """将题目实体转换为不暴露 ORM 状态的摘要。"""

        raw_knowledge_points = getattr(question, "knowledge_points", None) or []
        return QuestionSummary(
            id=str(question.id),
            course_id=str(question.course_id),
            type=question.type,
            content=question.content,
            options=deepcopy(question.options),
            reference_answer=question.reference_answer,
            scoring_rubric=question.scoring_rubric,
            difficulty=question.difficulty,
            knowledge_points=list(raw_knowledge_points),
            score=question.score,
            status=question.status,
            created_by=str(question.created_by),
            created_at=question.created_at,
            updated_at=question.updated_at,
        )


def _resolve_status_filter(
    status: QuestionStatus | str | None,
    question_status: QuestionStatus | str | None,
) -> QuestionStatus | None:
    """兼容 status 和 question_status 筛选参数。"""

    normalized_status = (
        _normalize_question_status(status) if status is not None else None
    )
    normalized_alias = (
        _normalize_question_status(question_status)
        if question_status is not None
        else None
    )
    if (
        normalized_status is not None
        and normalized_alias is not None
        and normalized_status != normalized_alias
    ):
        raise QuestionValidationError("题目审核状态筛选条件不一致。")
    return normalized_status or normalized_alias


__all__ = [
    "QuestionConflictError",
    "QuestionNotFoundError",
    "QuestionPermissionError",
    "QuestionService",
    "QuestionServiceError",
    "QuestionSummary",
    "QuestionValidationError",
]
