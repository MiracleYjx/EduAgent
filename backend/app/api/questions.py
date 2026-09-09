"""题库题目 CRUD 和审核状态 API。"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.security import require_permission
from backend.app.domain.enums import QuestionStatus, QuestionType
from backend.app.domain.permissions import Permission
from backend.app.models import User
from backend.app.services.question_service import (
    QuestionConflictError,
    QuestionNotFoundError,
    QuestionPermissionError,
    QuestionService,
    QuestionServiceError,
    QuestionSummary,
    QuestionValidationError,
)

router = APIRouter(prefix="/api/questions", tags=["题库"])


class QuestionCreateRequest(BaseModel):
    """人工创建题目时使用的请求体。"""

    model_config = ConfigDict(extra="forbid")

    course_id: UUID = Field(description="所属课程标识。")
    type: QuestionType = Field(description="题型。")
    content: str = Field(min_length=1, max_length=65535, description="题目内容。")
    options: dict[str, Any] | list[Any] | None = Field(
        default=None,
        description="题目选项，必须是 JSON 对象或数组。",
    )
    reference_answer: str | None = Field(
        default=None,
        max_length=65535,
        description="参考答案。",
    )
    scoring_rubric: str | None = Field(
        default=None,
        max_length=65535,
        description="评分标准。",
    )
    difficulty: str | None = Field(default=None, max_length=160, description="难度。")
    knowledge_points: list[str] = Field(
        default_factory=list,
        max_length=64,
        description="知识点列表。",
    )
    score: Decimal = Field(
        gt=0, max_digits=8, decimal_places=2, description="题目分值。"
    )

    @field_validator("content", mode="before")
    @classmethod
    def normalize_content(cls, value: Any) -> str:
        """清理题目内容并拒绝空白输入。"""

        if not isinstance(value, str) or not value.strip():
            raise ValueError("题目内容不能为空。")
        return value.strip()

    @field_validator("reference_answer", "scoring_rubric", "difficulty", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: Any) -> str | None:
        """清理题目可选文本字段。"""

        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("题目文本字段输入无效。")
        return value.strip() or None

    @field_validator("knowledge_points", mode="before")
    @classmethod
    def normalize_knowledge_points(cls, value: Any) -> list[str]:
        """清理知识点并去除重复项。"""

        if value is None:
            return []
        if isinstance(value, str) or not isinstance(value, list):
            raise TypeError("知识点必须是字符串列表。")
        normalized: list[str] = []
        for point in value:
            if not isinstance(point, str) or not point.strip():
                raise ValueError("知识点必须是非空字符串。")
            item = point.strip()
            if item not in normalized:
                normalized.append(item)
        return normalized


class QuestionUpdateRequest(BaseModel):
    """修改题目元数据时使用的请求体。"""

    model_config = ConfigDict(extra="forbid")

    type: QuestionType | None = Field(default=None, description="题型。")
    content: str | None = Field(default=None, min_length=1, max_length=65535)
    options: dict[str, Any] | list[Any] | None = None
    reference_answer: str | None = Field(default=None, max_length=65535)
    scoring_rubric: str | None = Field(default=None, max_length=65535)
    difficulty: str | None = Field(default=None, max_length=160)
    knowledge_points: list[str] | None = Field(default=None, max_length=64)
    score: Decimal | None = Field(
        default=None,
        gt=0,
        max_digits=8,
        decimal_places=2,
    )

    @field_validator("content", mode="before")
    @classmethod
    def normalize_content(cls, value: Any) -> str | None:
        """清理可选题目内容。"""

        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("题目内容不能为空。")
        return value.strip()

    @field_validator("reference_answer", "scoring_rubric", "difficulty", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: Any) -> str | None:
        """清理可选题目文本字段。"""

        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("题目文本字段输入无效。")
        return value.strip() or None

    @field_validator("knowledge_points", mode="before")
    @classmethod
    def normalize_knowledge_points(cls, value: Any) -> list[str] | None:
        """清理可选知识点列表。"""

        if value is None:
            return None
        if isinstance(value, str) or not isinstance(value, list):
            raise TypeError("知识点必须是字符串列表。")
        normalized: list[str] = []
        for point in value:
            if not isinstance(point, str) or not point.strip():
                raise ValueError("知识点必须是非空字符串。")
            item = point.strip()
            if item not in normalized:
                normalized.append(item)
        return normalized


class QuestionStatusRequest(BaseModel):
    """更新题目审核状态时使用的请求体。"""

    model_config = ConfigDict(extra="forbid")

    status: QuestionStatus = Field(description="目标审核状态。")


def get_question_service(
    session: Annotated[Session, Depends(get_db)],
) -> QuestionService:
    """创建使用当前请求数据库会话的题目服务。"""

    return QuestionService(session)


QuestionServiceDependency = Annotated[
    QuestionService,
    Depends(get_question_service),
]
QuestionViewer = Annotated[
    User,
    Depends(require_permission(Permission.MANAGE_QUESTION_BANK)),
]
QuestionCreator = Annotated[
    User,
    Depends(require_permission(Permission.CREATE_MANUAL_QUESTIONS)),
]
QuestionEditor = Annotated[
    User,
    Depends(require_permission(Permission.MANAGE_QUESTION_BANK)),
]
QuestionReviewer = Annotated[
    User,
    Depends(require_permission(Permission.REVIEW_QUESTIONS)),
]


def _question_http_exception(error: BaseException) -> HTTPException:
    """将题目服务异常转换为统一的中文 HTTP 错误。"""

    if isinstance(error, QuestionNotFoundError):
        code = status.HTTP_404_NOT_FOUND
    elif isinstance(error, QuestionPermissionError):
        code = status.HTTP_403_FORBIDDEN
    elif isinstance(error, QuestionConflictError):
        code = status.HTTP_409_CONFLICT
    elif isinstance(error, (QuestionValidationError, ValueError)):
        code = status.HTTP_422_UNPROCESSABLE_ENTITY
    else:
        code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HTTPException(
        status_code=code,
        detail=str(error) or "题目操作失败，请稍后重试。",
    )


def _question_update_kwargs(payload: QuestionUpdateRequest) -> dict[str, Any]:
    """只把请求中实际出现的题目字段传给服务。"""

    fields = payload.model_fields_set
    return {
        field_name: getattr(payload, field_name)
        for field_name in (
            "content",
            "options",
            "reference_answer",
            "scoring_rubric",
            "difficulty",
            "knowledge_points",
            "score",
        )
        if field_name in fields
    } | ({"question_type": payload.type} if "type" in fields else {})


@router.get("", response_model=list[QuestionSummary])
def list_questions(
    teacher: QuestionViewer,
    service: QuestionServiceDependency,
    course_id: UUID | None = None,
    question_status: QuestionStatus | None = None,
) -> list[QuestionSummary]:
    """列出当前教师课程下的题目。"""

    try:
        return service.list_questions(
            course_id=course_id,
            status=question_status,
            teacher_id=teacher.id,
        )
    except QuestionServiceError as exc:
        raise _question_http_exception(exc) from None


@router.post(
    "",
    response_model=QuestionSummary,
    status_code=status.HTTP_201_CREATED,
)
def create_question(
    payload: QuestionCreateRequest,
    teacher: QuestionCreator,
    service: QuestionServiceDependency,
) -> QuestionSummary:
    """创建一道人工作答题目，初始状态为 Draft。"""

    try:
        return service.create_question(
            course_id=payload.course_id,
            question_type=payload.type,
            content=payload.content,
            options=payload.options,
            reference_answer=payload.reference_answer,
            scoring_rubric=payload.scoring_rubric,
            difficulty=payload.difficulty,
            knowledge_points=payload.knowledge_points,
            score=payload.score,
            created_by=teacher.id,
        )
    except (QuestionServiceError, ValueError) as exc:
        raise _question_http_exception(exc) from None


@router.get("/{question_id}", response_model=QuestionSummary)
def get_question(
    question_id: UUID,
    teacher: QuestionViewer,
    service: QuestionServiceDependency,
) -> QuestionSummary:
    """读取当前教师有权访问的题目。"""

    try:
        return service.get_question(question_id, teacher_id=teacher.id)
    except QuestionServiceError as exc:
        raise _question_http_exception(exc) from None


@router.patch("/{question_id}", response_model=QuestionSummary)
def update_question(
    question_id: UUID,
    payload: QuestionUpdateRequest,
    teacher: QuestionEditor,
    service: QuestionServiceDependency,
) -> QuestionSummary:
    """修改当前教师有权访问的题目元数据。"""

    try:
        return service.update_question(
            question_id,
            teacher_id=teacher.id,
            **_question_update_kwargs(payload),
        )
    except (QuestionServiceError, ValueError) as exc:
        raise _question_http_exception(exc) from None


@router.delete("/{question_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_question(
    question_id: UUID,
    teacher: QuestionEditor,
    service: QuestionServiceDependency,
) -> Response:
    """删除当前教师有权访问的题目。"""

    try:
        service.delete_question(question_id, teacher_id=teacher.id)
    except (QuestionServiceError, ValueError) as exc:
        raise _question_http_exception(exc) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/{question_id}/status", response_model=QuestionSummary)
@router.post(
    "/{question_id}/status",
    response_model=QuestionSummary,
    include_in_schema=False,
)
def update_question_status(
    question_id: UUID,
    payload: QuestionStatusRequest,
    teacher: QuestionReviewer,
    service: QuestionServiceDependency,
) -> QuestionSummary:
    """按审核状态机更新题目状态。"""

    try:
        return service.update_question_status(
            question_id,
            payload.status,
            teacher_id=teacher.id,
        )
    except (QuestionServiceError, ValueError) as exc:
        raise _question_http_exception(exc) from None


@router.post(
    "/{question_id}/submit-review",
    response_model=QuestionSummary,
)
def submit_question_for_review(
    question_id: UUID,
    teacher: QuestionReviewer,
    service: QuestionServiceDependency,
) -> QuestionSummary:
    """提交题目进入待审核状态。"""

    try:
        return service.update_question_status(
            question_id,
            QuestionStatus.PENDING_REVIEW,
            teacher_id=teacher.id,
        )
    except (QuestionServiceError, ValueError) as exc:
        raise _question_http_exception(exc) from None


@router.post(
    "/{question_id}/approve",
    response_model=QuestionSummary,
)
def approve_question(
    question_id: UUID,
    teacher: QuestionReviewer,
    service: QuestionServiceDependency,
) -> QuestionSummary:
    """批准处于待审核状态的题目。"""

    try:
        return service.update_question_status(
            question_id,
            QuestionStatus.APPROVED,
            teacher_id=teacher.id,
        )
    except (QuestionServiceError, ValueError) as exc:
        raise _question_http_exception(exc) from None


@router.post(
    "/{question_id}/needs-revision",
    response_model=QuestionSummary,
)
def request_question_revision(
    question_id: UUID,
    teacher: QuestionReviewer,
    service: QuestionServiceDependency,
) -> QuestionSummary:
    """将题目退回修订状态。"""

    try:
        return service.update_question_status(
            question_id,
            QuestionStatus.NEEDS_REVISION,
            teacher_id=teacher.id,
        )
    except (QuestionServiceError, ValueError) as exc:
        raise _question_http_exception(exc) from None


__all__ = [
    "QuestionCreateRequest",
    "QuestionStatusRequest",
    "QuestionUpdateRequest",
    "get_question_service",
    "router",
]
