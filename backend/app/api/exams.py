"""考试创建、组卷和发布 API。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.security import require_permission
from backend.app.domain.enums import ExamStatus
from backend.app.domain.permissions import Permission
from backend.app.models import User
from backend.app.services.exam_service import (
    ExamConflictError,
    ExamNotFoundError,
    ExamPermissionError,
    ExamService,
    ExamServiceError,
    ExamSummary,
    ExamValidationError,
)

router = APIRouter(prefix="/api/exams", tags=["考试"])


class ExamCreateRequest(BaseModel):
    """创建考试草稿时使用的请求体。"""

    model_config = ConfigDict(extra="forbid")

    course_id: UUID = Field(description="所属课程标识。")
    title: str = Field(min_length=1, max_length=160, description="考试标题。")
    description: str | None = Field(
        default=None,
        max_length=65535,
        description="考试描述。",
    )
    duration_minutes: int | None = Field(
        default=None,
        gt=0,
        description="考试时长，单位为分钟。",
    )
    starts_at: datetime | None = Field(default=None, description="开始时间。")
    ends_at: datetime | None = Field(default=None, description="结束时间。")
    question_ids: list[UUID] = Field(
        default_factory=list,
        max_length=200,
        description="初始关联的已审核题目标识列表。",
    )

    @field_validator("title", mode="before")
    @classmethod
    def normalize_title(cls, value: Any) -> str:
        """清理考试标题并拒绝空白输入。"""

        if not isinstance(value, str) or not value.strip():
            raise ValueError("考试标题不能为空。")
        return value.strip()

    @field_validator("description", mode="before")
    @classmethod
    def normalize_description(cls, value: Any) -> str | None:
        """清理可选考试描述。"""

        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("考试描述输入无效。")
        return value.strip() or None

    @field_validator("question_ids")
    @classmethod
    def reject_duplicate_questions(cls, values: list[UUID]) -> list[UUID]:
        """拒绝初始组卷中的重复题目。"""

        if len(set(values)) != len(values):
            raise ValueError("题目列表不能包含重复题目。")
        return values


class ExamUpdateRequest(BaseModel):
    """修改草稿考试元数据时使用的请求体。"""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=65535)
    duration_minutes: int | None = Field(default=None, gt=0)
    starts_at: datetime | None = None
    ends_at: datetime | None = None

    @field_validator("title", mode="before")
    @classmethod
    def normalize_optional_title(cls, value: Any) -> str | None:
        """清理可选考试标题。"""

        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("考试标题不能为空。")
        return value.strip()

    @field_validator("description", mode="before")
    @classmethod
    def normalize_optional_description(cls, value: Any) -> str | None:
        """清理可选考试描述，显式空值可清除描述。"""

        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("考试描述输入无效。")
        return value.strip() or None


class ExamQuestionsRequest(BaseModel):
    """增加或移除考试题目时使用的请求体。"""

    model_config = ConfigDict(extra="forbid")

    question_ids: list[UUID] = Field(
        min_length=1,
        max_length=200,
        description="题目标识列表。",
    )

    @field_validator("question_ids")
    @classmethod
    def reject_duplicate_questions(cls, values: list[UUID]) -> list[UUID]:
        """拒绝请求中的重复题目。"""

        if len(set(values)) != len(values):
            raise ValueError("题目列表不能包含重复题目。")
        return values


class ExamStatusRequest(BaseModel):
    """更新考试生命周期状态时使用的请求体。"""

    model_config = ConfigDict(extra="forbid")

    status: ExamStatus = Field(description="目标考试状态。")


# 保留更具体的别名，便于不同调用方表达“组卷题目请求”。
ExamQuestionRequest = ExamQuestionsRequest
ExamAddQuestionsRequest = ExamQuestionsRequest


def get_exam_service(
    session: Annotated[Session, Depends(get_db)],
) -> ExamService:
    """创建使用当前请求数据库会话的考试服务。"""

    return ExamService(session)


ExamServiceDependency = Annotated[ExamService, Depends(get_exam_service)]
ExamViewer = Annotated[
    User,
    Depends(require_permission(Permission.CREATE_EXAMS)),
]
ExamCreator = Annotated[
    User,
    Depends(require_permission(Permission.CREATE_EXAMS)),
]
ExamEditor = Annotated[
    User,
    Depends(require_permission(Permission.CREATE_EXAMS)),
]
ExamPublisher = Annotated[
    User,
    Depends(require_permission(Permission.PUBLISH_EXAMS)),
]


def _exam_http_exception(error: BaseException) -> HTTPException:
    """将考试服务异常转换为统一的中文 HTTP 错误。"""

    if isinstance(error, ExamNotFoundError):
        code = status.HTTP_404_NOT_FOUND
    elif isinstance(error, ExamPermissionError):
        code = status.HTTP_403_FORBIDDEN
    elif isinstance(error, ExamConflictError):
        code = status.HTTP_409_CONFLICT
    elif isinstance(error, (ExamValidationError, ValueError)):
        code = status.HTTP_422_UNPROCESSABLE_ENTITY
    else:
        code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HTTPException(
        status_code=code,
        detail=str(error) or "考试操作失败，请稍后重试。",
    )


def _exam_update_kwargs(payload: ExamUpdateRequest) -> dict[str, Any]:
    """只把请求中实际出现的考试字段传给服务。"""

    fields = payload.model_fields_set
    return {
        field_name: getattr(payload, field_name)
        for field_name in (
            "title",
            "description",
            "duration_minutes",
            "starts_at",
            "ends_at",
        )
        if field_name in fields
    }


@router.get("", response_model=list[ExamSummary])
def list_exams(
    teacher: ExamViewer,
    service: ExamServiceDependency,
    course_id: UUID | None = None,
    exam_status: Annotated[ExamStatus | None, Query(alias="status")] = None,
) -> list[ExamSummary]:
    """列出当前教师课程下的考试。"""

    try:
        return service.list_exams(
            course_id=course_id,
            status=exam_status,
            teacher_id=teacher.id,
        )
    except ExamServiceError as exc:
        raise _exam_http_exception(exc) from None


@router.post(
    "",
    response_model=ExamSummary,
    status_code=status.HTTP_201_CREATED,
)
def create_exam(
    payload: ExamCreateRequest,
    teacher: ExamCreator,
    service: ExamServiceDependency,
) -> ExamSummary:
    """创建考试草稿并按需关联已审核题目。"""

    try:
        return service.create_exam(
            course_id=payload.course_id,
            title=payload.title,
            description=payload.description,
            duration_minutes=payload.duration_minutes,
            starts_at=payload.starts_at,
            ends_at=payload.ends_at,
            question_ids=payload.question_ids,
            created_by=teacher.id,
        )
    except (ExamServiceError, ValueError) as exc:
        raise _exam_http_exception(exc) from None


@router.get("/{exam_id}", response_model=ExamSummary)
def get_exam(
    exam_id: UUID,
    teacher: ExamViewer,
    service: ExamServiceDependency,
) -> ExamSummary:
    """读取当前教师有权访问的考试。"""

    try:
        return service.get_exam(exam_id, teacher_id=teacher.id)
    except ExamServiceError as exc:
        raise _exam_http_exception(exc) from None


@router.patch("/{exam_id}", response_model=ExamSummary)
def update_exam(
    exam_id: UUID,
    payload: ExamUpdateRequest,
    teacher: ExamEditor,
    service: ExamServiceDependency,
) -> ExamSummary:
    """修改当前教师草稿考试的元数据。"""

    try:
        return service.update_exam(
            exam_id,
            teacher_id=teacher.id,
            **_exam_update_kwargs(payload),
        )
    except (ExamServiceError, ValueError) as exc:
        raise _exam_http_exception(exc) from None


@router.post("/{exam_id}/questions", response_model=ExamSummary)
def add_exam_questions(
    exam_id: UUID,
    payload: ExamQuestionsRequest,
    teacher: ExamEditor,
    service: ExamServiceDependency,
) -> ExamSummary:
    """向草稿考试追加已审核题目。"""

    try:
        return service.add_questions(
            exam_id,
            payload.question_ids,
            teacher_id=teacher.id,
        )
    except (ExamServiceError, ValueError) as exc:
        raise _exam_http_exception(exc) from None


@router.delete("/{exam_id}/questions", response_model=ExamSummary)
def remove_exam_questions(
    exam_id: UUID,
    payload: ExamQuestionsRequest,
    teacher: ExamEditor,
    service: ExamServiceDependency,
) -> ExamSummary:
    """从草稿考试移除已关联题目。"""

    try:
        return service.remove_questions(
            exam_id,
            payload.question_ids,
            teacher_id=teacher.id,
        )
    except (ExamServiceError, ValueError) as exc:
        raise _exam_http_exception(exc) from None


@router.post("/{exam_id}/publish", response_model=ExamSummary)
def publish_exam(
    exam_id: UUID,
    teacher: ExamPublisher,
    service: ExamServiceDependency,
) -> ExamSummary:
    """执行发布检查并开放考试。"""

    try:
        return service.publish_exam(exam_id, teacher_id=teacher.id)
    except (ExamServiceError, ValueError) as exc:
        raise _exam_http_exception(exc) from None


@router.patch("/{exam_id}/status", response_model=ExamSummary)
def update_exam_status(
    exam_id: UUID,
    payload: ExamStatusRequest,
    teacher: ExamPublisher,
    service: ExamServiceDependency,
) -> ExamSummary:
    """按考试生命周期更新状态，发布状态仍需通过完整检查。"""

    try:
        return service.update_exam_status(
            exam_id,
            payload.status,
            teacher_id=teacher.id,
        )
    except (ExamServiceError, ValueError) as exc:
        raise _exam_http_exception(exc) from None


__all__ = [
    "ExamAddQuestionsRequest",
    "ExamCreateRequest",
    "ExamQuestionRequest",
    "ExamQuestionsRequest",
    "ExamStatusRequest",
    "ExamUpdateRequest",
    "get_exam_service",
    "router",
]
