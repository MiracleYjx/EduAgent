"""课程元数据和课程资源绑定 API。"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.security import require_permission
from backend.app.domain.permissions import Permission
from backend.app.models import User
from backend.app.services.course_service import (
    CourseConflictError,
    CourseNotFoundError,
    CoursePermissionError,
    CourseService,
    CourseServiceError,
    CourseSummary,
    CourseValidationError,
)

router = APIRouter(prefix="/api/courses", tags=["课程"])


class CourseCreateRequest(BaseModel):
    """创建课程时使用的请求体。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=160, description="课程名称。")
    description: str | None = Field(
        default=None,
        max_length=65535,
        description="课程描述。",
    )

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: Any) -> str:
        """清理课程名称并拒绝空白输入。"""

        if not isinstance(value, str) or not value.strip():
            raise ValueError("课程名称不能为空。")
        return value.strip()

    @field_validator("description", mode="before")
    @classmethod
    def normalize_description(cls, value: Any) -> str | None:
        """清理可选课程描述，空白描述按未提供处理。"""

        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("课程描述输入无效。")
        return value.strip() or None


class CourseUpdateRequest(BaseModel):
    """修改课程时使用的请求体。"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=65535)

    @field_validator("name", mode="before")
    @classmethod
    def normalize_optional_name(cls, value: Any) -> str | None:
        """清理可选课程名称。"""

        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("课程名称不能为空。")
        return value.strip()

    @field_validator("description", mode="before")
    @classmethod
    def normalize_optional_description(cls, value: Any) -> str | None:
        """清理可选课程描述，显式空值可清除描述。"""

        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("课程描述输入无效。")
        return value.strip() or None


class CourseKnowledgeBaseBindingRequest(BaseModel):
    """通过课程资源路径绑定知识库时使用的请求体。"""

    model_config = ConfigDict(extra="forbid")

    knowledge_base_id: UUID = Field(description="知识库标识。")


def get_course_service(session: Annotated[Session, Depends(get_db)]) -> CourseService:
    """创建使用当前请求数据库会话的课程服务。"""

    return CourseService(session)


CourseServiceDependency = Annotated[CourseService, Depends(get_course_service)]
CourseViewer = Annotated[
    User,
    Depends(require_permission(Permission.VIEW_COURSES)),
]
CourseCreator = Annotated[
    User,
    Depends(require_permission(Permission.CREATE_COURSE)),
]
CourseEditor = Annotated[
    User,
    Depends(require_permission(Permission.UPDATE_COURSE)),
]
CourseDeleter = Annotated[
    User,
    Depends(require_permission(Permission.DELETE_COURSE)),
]


def _course_http_exception(error: BaseException) -> HTTPException:
    """将课程服务异常转换为统一的中文 HTTP 错误。"""

    if isinstance(error, CourseNotFoundError):
        code = status.HTTP_404_NOT_FOUND
    elif isinstance(error, CoursePermissionError):
        code = status.HTTP_403_FORBIDDEN
    elif isinstance(error, CourseConflictError):
        code = status.HTTP_409_CONFLICT
    elif isinstance(error, (CourseValidationError, ValueError)):
        code = status.HTTP_422_UNPROCESSABLE_ENTITY
    else:
        code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HTTPException(
        status_code=code,
        detail=str(error) or "课程操作失败，请稍后重试。",
    )


def _course_update_kwargs(payload: CourseUpdateRequest) -> dict[str, Any]:
    """只把请求中实际出现的课程字段传给服务。"""

    fields = payload.model_fields_set
    return {
        field_name: getattr(payload, field_name)
        for field_name in ("name", "description")
        if field_name in fields
    }


@router.get("", response_model=list[CourseSummary])
def list_courses(
    teacher: CourseViewer,
    service: CourseServiceDependency,
) -> list[CourseSummary]:
    """列出当前教师拥有的课程。"""

    try:
        return service.list_courses(teacher_id=teacher.id)
    except CourseServiceError as exc:
        raise _course_http_exception(exc) from None


@router.post(
    "",
    response_model=CourseSummary,
    status_code=status.HTTP_201_CREATED,
)
def create_course(
    payload: CourseCreateRequest,
    teacher: CourseCreator,
    service: CourseServiceDependency,
) -> CourseSummary:
    """创建一门由当前教师负责的课程。"""

    try:
        return service.create_course(
            name=payload.name,
            description=payload.description,
            created_by=teacher.id,
        )
    except (CourseServiceError, ValueError) as exc:
        raise _course_http_exception(exc) from None


@router.get("/{course_id}", response_model=CourseSummary)
def get_course(
    course_id: UUID,
    teacher: CourseViewer,
    service: CourseServiceDependency,
) -> CourseSummary:
    """读取当前教师拥有的课程。"""

    try:
        return service.get_course(course_id, teacher_id=teacher.id)
    except CourseServiceError as exc:
        raise _course_http_exception(exc) from None


@router.patch("/{course_id}", response_model=CourseSummary)
def update_course(
    course_id: UUID,
    payload: CourseUpdateRequest,
    teacher: CourseEditor,
    service: CourseServiceDependency,
) -> CourseSummary:
    """修改当前教师拥有的课程元数据。"""

    try:
        return service.update_course(
            course_id,
            teacher_id=teacher.id,
            **_course_update_kwargs(payload),
        )
    except (CourseServiceError, ValueError) as exc:
        raise _course_http_exception(exc) from None


@router.delete("/{course_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_course(
    course_id: UUID,
    teacher: CourseDeleter,
    service: CourseServiceDependency,
) -> Response:
    """删除当前教师拥有的课程及其课程资源。"""

    try:
        service.delete_course(course_id, teacher_id=teacher.id)
    except (CourseServiceError, ValueError) as exc:
        raise _course_http_exception(exc) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{course_id}/knowledge-bases/{knowledge_base_id}",
    response_model=CourseSummary,
)
def bind_knowledge_base(
    course_id: UUID,
    knowledge_base_id: UUID,
    teacher: CourseEditor,
    service: CourseServiceDependency,
) -> CourseSummary:
    """将知识库绑定到当前教师拥有的课程。"""

    try:
        return service.bind_knowledge_base(
            course_id,
            knowledge_base_id,
            teacher_id=teacher.id,
        )
    except (CourseServiceError, ValueError) as exc:
        raise _course_http_exception(exc) from None


@router.post(
    "/{course_id}/knowledge-bases",
    response_model=CourseSummary,
    include_in_schema=False,
)
def bind_knowledge_base_from_body(
    course_id: UUID,
    payload: CourseKnowledgeBaseBindingRequest,
    teacher: CourseEditor,
    service: CourseServiceDependency,
) -> CourseSummary:
    """兼容通过请求体传入知识库标识的绑定方式。"""

    return bind_knowledge_base(
        course_id,
        payload.knowledge_base_id,
        teacher,
        service,
    )


__all__ = [
    "CourseCreateRequest",
    "CourseKnowledgeBaseBindingRequest",
    "CourseUpdateRequest",
    "get_course_service",
    "router",
]
