"""课程服务：课程元数据、课程归属和知识库绑定入口。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from backend.app.models import Course, User

_UNSET = object()


class CourseServiceError(RuntimeError):
    """课程服务的异常基类。"""


class CourseNotFoundError(CourseServiceError):
    """课程或课程创建者不存在时抛出。"""


class CourseConflictError(CourseServiceError):
    """课程变更违反数据约束时抛出。"""


class CourseValidationError(CourseServiceError):
    """课程输入或状态不符合业务规则时抛出。"""


class CoursePermissionError(CourseServiceError):
    """当前教师不是课程所有者时抛出。"""


class CourseSummary(BaseModel):
    """面向 API 和 UI 的课程元数据摘要。"""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    description: str | None = None
    created_by: str
    knowledge_base_count: int = 0
    created_at: datetime
    updated_at: datetime


def _normalize_uuid(value: UUID | str | None, field_name: str) -> UUID:
    """将外部标识规范化为 UUID。"""

    if value is None:
        raise CourseValidationError(f"{field_name}不能为空。")
    try:
        return value if isinstance(value, UUID) else UUID(str(value).strip())
    except (AttributeError, TypeError, ValueError) as exc:
        raise CourseValidationError(f"{field_name}无效。") from exc


def _normalize_required_text(
    value: str | None,
    field_name: str,
    *,
    max_length: int,
) -> str:
    """清理必填文本并执行长度校验。"""

    if not isinstance(value, str) or not value.strip():
        raise CourseValidationError(f"{field_name}不能为空。")
    normalized = value.strip()
    if "\x00" in normalized:
        raise CourseValidationError(f"{field_name}包含无效字符。")
    if len(normalized) > max_length:
        raise CourseValidationError(f"{field_name}长度不能超过 {max_length} 个字符。")
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
        raise CourseValidationError(f"{field_name}输入无效。")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise CourseValidationError(f"{field_name}长度不能超过 {max_length} 个字符。")
    return normalized or None


def _resolve_actor_id(
    actor_id: UUID | str | None,
    teacher_id: UUID | str | None,
    *,
    field_name: str = "教师标识",
) -> UUID | None:
    """兼容教师标识别名，并拒绝互相冲突的输入。"""

    normalized_actor = (
        _normalize_uuid(actor_id, field_name) if actor_id is not None else None
    )
    normalized_teacher = (
        _normalize_uuid(teacher_id, field_name) if teacher_id is not None else None
    )
    if (
        normalized_actor is not None
        and normalized_teacher is not None
        and normalized_actor != normalized_teacher
    ):
        raise CourseValidationError(f"{field_name}不一致。")
    return normalized_actor or normalized_teacher


class CourseService:
    """封装课程元数据和课程所有权相关业务。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create_course(
        self,
        name: str,
        description: str | None = None,
        created_by: UUID | str | None = None,
        *,
        teacher_id: UUID | str | None = None,
    ) -> CourseSummary:
        """创建一门由指定教师负责的课程。"""

        creator_id = _resolve_actor_id(created_by, teacher_id, field_name="创建者标识")
        if creator_id is None:
            raise CourseValidationError("创建者标识不能为空。")
        self._ensure_user_exists(creator_id)

        course = Course(
            name=_normalize_required_text(name, "课程名称", max_length=160),
            description=_normalize_optional_text(
                description,
                "课程描述",
                max_length=65535,
            ),
            created_by=creator_id,
        )
        self.session.add(course)
        return self._commit_course(course, "创建课程失败。")

    def list_courses(
        self,
        teacher_id: UUID | str | None = None,
        *,
        created_by: UUID | str | None = None,
    ) -> list[CourseSummary]:
        """列出课程；传入教师标识时只返回其拥有的课程。"""

        actor_id = _resolve_actor_id(teacher_id, created_by)
        statement = select(Course).options(selectinload(Course.knowledge_bases))
        if actor_id is not None:
            statement = statement.where(Course.created_by == actor_id)
        statement = statement.order_by(Course.created_at, Course.name)
        try:
            courses = self.session.scalars(statement).all()
        except SQLAlchemyError as exc:
            raise CourseServiceError("无法读取课程列表。") from exc
        return [self._course_summary(course) for course in courses]

    def get_course(
        self,
        course_id: UUID | str,
        teacher_id: UUID | str | None = None,
        *,
        created_by: UUID | str | None = None,
    ) -> CourseSummary:
        """读取课程元数据并按需检查课程所有权。"""

        course = self._load_course(course_id)
        self._ensure_course_access(
            course,
            _resolve_actor_id(teacher_id, created_by),
        )
        return self._course_summary(course)

    def update_course(
        self,
        course_id: UUID | str,
        name: str | None = None,
        description: str | None | object = _UNSET,
        *,
        teacher_id: UUID | str | None = None,
        created_by: UUID | str | None = None,
    ) -> CourseSummary:
        """更新课程名称或描述，支持使用空白描述清除原内容。"""

        if name is None and description is _UNSET:
            raise CourseValidationError("至少需要提供一个更新字段。")

        course = self._load_course(course_id)
        self._ensure_course_access(
            course,
            _resolve_actor_id(teacher_id, created_by),
        )
        if name is not None:
            course.name = _normalize_required_text(
                name,
                "课程名称",
                max_length=160,
            )
        if description is not _UNSET:
            if description is not None and not isinstance(description, str):
                raise CourseValidationError("课程描述输入无效。")
            course.description = _normalize_optional_text(
                description,
                "课程描述",
                max_length=65535,
            )
        return self._commit_course(course, "更新课程失败。")

    def delete_course(
        self,
        course_id: UUID | str,
        teacher_id: UUID | str | None = None,
        *,
        created_by: UUID | str | None = None,
    ) -> None:
        """删除课程及其由 ORM 管理的课程资源。"""

        course = self._load_course(course_id)
        self._ensure_course_access(
            course,
            _resolve_actor_id(teacher_id, created_by),
        )
        try:
            self.session.delete(course)
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise CourseConflictError("课程仍被业务数据引用，无法删除。") from exc
        except SQLAlchemyError as exc:
            self.session.rollback()
            raise CourseServiceError("删除课程失败。") from exc

    def bind_knowledge_base(
        self,
        course_id: UUID | str,
        knowledge_base_id: UUID | str,
        teacher_id: UUID | str | None = None,
    ) -> CourseSummary:
        """把知识库绑定到课程，并返回更新后的课程摘要。"""

        from backend.app.services.knowledge_base_service import KnowledgeBaseService

        KnowledgeBaseService(self.session).bind_to_course(
            knowledge_base_id,
            course_id,
            teacher_id=teacher_id,
        )
        return self.get_course(course_id, teacher_id=teacher_id)

    def _load_course(self, course_id: UUID | str) -> Course:
        """加载课程实体并统一处理标识和不存在错误。"""

        normalized_id = _normalize_uuid(course_id, "课程标识")
        try:
            course = self.session.scalar(
                select(Course)
                .options(selectinload(Course.knowledge_bases))
                .where(Course.id == normalized_id)
            )
        except SQLAlchemyError as exc:
            raise CourseServiceError("无法读取课程信息。") from exc
        if course is None:
            raise CourseNotFoundError("课程不存在。")
        return course

    def _ensure_user_exists(self, user_id: UUID) -> None:
        """确认课程创建者存在，避免留下不可追溯的课程记录。"""

        try:
            user = self.session.get(User, user_id)
        except SQLAlchemyError as exc:
            raise CourseServiceError("无法读取课程创建者信息。") from exc
        if user is None:
            raise CourseNotFoundError("课程创建者不存在。")

    @staticmethod
    def _ensure_course_access(course: Course, actor_id: UUID | None) -> None:
        """检查当前操作人是否为课程所有者。"""

        if actor_id is not None and course.created_by != actor_id:
            raise CoursePermissionError("无权访问该课程。")

    def _commit_course(self, course: Course, fallback_message: str) -> CourseSummary:
        """提交课程变更并转换为安全摘要。"""

        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise CourseConflictError("课程信息与现有数据冲突。") from exc
        except SQLAlchemyError as exc:
            self.session.rollback()
            raise CourseServiceError(fallback_message) from exc

        self.session.refresh(course)
        return self._course_summary(course)

    @staticmethod
    def _course_summary(course: Course) -> CourseSummary:
        """将课程实体转换为不暴露 ORM 状态的摘要。"""

        knowledge_bases: Sequence[Any] = getattr(course, "knowledge_bases", ())
        return CourseSummary(
            id=str(course.id),
            name=course.name,
            description=course.description,
            created_by=str(course.created_by),
            knowledge_base_count=len(knowledge_bases),
            created_at=course.created_at,
            updated_at=course.updated_at,
        )


__all__ = [
    "CourseConflictError",
    "CourseNotFoundError",
    "CoursePermissionError",
    "CourseService",
    "CourseServiceError",
    "CourseSummary",
    "CourseValidationError",
]
