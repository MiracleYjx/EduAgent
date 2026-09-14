"""知识库和课程资料元数据 API。"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.security import require_permission
from backend.app.domain.enums import DocumentStatus
from backend.app.domain.permissions import Permission
from backend.app.models import User
from backend.app.services.course_service import CourseNotFoundError, CourseServiceError
from backend.app.services.knowledge_base_service import (
    DocumentIngestionResult,
    DocumentNotFoundError,
    DocumentSummary,
    DocumentValidationError,
    KnowledgeBaseConflictError,
    KnowledgeBaseNotFoundError,
    KnowledgeBasePermissionError,
    KnowledgeBaseService,
    KnowledgeBaseServiceError,
    KnowledgeBaseSummary,
    KnowledgeBaseValidationError,
)

router = APIRouter(prefix="/api/knowledge-bases", tags=["知识库"])

#: 上传资料落盘目录；MVP 使用本地临时目录，后续可替换为配置化存储或对象存储。
UPLOAD_STORAGE_DIR = Path(tempfile.gettempdir()) / "eduagent_uploads"


def _store_upload_bytes(filename: str, data: bytes) -> Path:
    """把上传的文件内容写入本地存储目录，保留原始扩展名。"""

    UPLOAD_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(filename).suffix.lower() or ".bin"
    target = UPLOAD_STORAGE_DIR / f"{uuid4().hex}{suffix}"
    target.write_bytes(data)
    return target


class KnowledgeBaseCreateRequest(BaseModel):
    """创建知识库时使用的请求体。"""

    model_config = ConfigDict(extra="forbid")

    course_id: UUID = Field(description="所属课程标识。")
    name: str = Field(min_length=1, max_length=160, description="知识库名称。")
    description: str | None = Field(
        default=None,
        max_length=65535,
        description="知识库描述。",
    )

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: Any) -> str:
        """清理知识库名称并拒绝空白输入。"""

        if not isinstance(value, str) or not value.strip():
            raise ValueError("知识库名称不能为空。")
        return value.strip()

    @field_validator("description", mode="before")
    @classmethod
    def normalize_description(cls, value: Any) -> str | None:
        """清理可选知识库描述。"""

        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("知识库描述输入无效。")
        return value.strip() or None


class KnowledgeBaseUpdateRequest(BaseModel):
    """修改知识库时使用的请求体。"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=65535)

    @field_validator("name", mode="before")
    @classmethod
    def normalize_optional_name(cls, value: Any) -> str | None:
        """清理可选知识库名称。"""

        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("知识库名称不能为空。")
        return value.strip()

    @field_validator("description", mode="before")
    @classmethod
    def normalize_optional_description(cls, value: Any) -> str | None:
        """清理可选知识库描述，显式空值可清除描述。"""

        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("知识库描述输入无效。")
        return value.strip() or None


class KnowledgeBaseBindRequest(BaseModel):
    """修改知识库所属课程时使用的请求体。"""

    model_config = ConfigDict(extra="forbid")

    course_id: UUID = Field(description="目标课程标识。")


class DocumentCreateRequest(BaseModel):
    """登记课程资料元数据时使用的请求体。"""

    model_config = ConfigDict(extra="forbid")

    original_filename: str = Field(
        min_length=1,
        max_length=255,
        description="原始文件名。",
    )
    file_format: str | None = Field(
        default=None,
        max_length=32,
        description="文件格式；不提供时从文件名后缀推导。",
    )
    storage_path: str | None = Field(
        default=None,
        max_length=1024,
        description="文件存储路径。",
    )

    @field_validator("original_filename", mode="before")
    @classmethod
    def normalize_filename(cls, value: Any) -> str:
        """清理原始文件名并拒绝空白输入。"""

        if not isinstance(value, str) or not value.strip():
            raise ValueError("原始文件名不能为空。")
        return value.strip()

    @field_validator("file_format", "storage_path", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: Any) -> str | None:
        """清理文档可选元数据字段。"""

        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("文档元数据输入无效。")
        return value.strip() or None


class DocumentStatusUpdateRequest(BaseModel):
    """更新文档处理状态时使用的请求体。"""

    model_config = ConfigDict(extra="forbid")

    status: DocumentStatus = Field(description="目标处理状态。")
    error_code: str | None = Field(default=None, max_length=64)
    error_message: str | None = Field(default=None, max_length=2000)
    retryable: bool | None = Field(default=None, description="失败后是否允许重试。")

    @field_validator("status")
    @classmethod
    def validate_external_status(cls, value: DocumentStatus) -> DocumentStatus:
        """外部请求只能报告处理中或失败，不能声明摄取成功。"""

        if value not in {
            DocumentStatus.PARSING, DocumentStatus.EMBEDDING, DocumentStatus.FAILED,
        }:
            raise ValueError("状态更新只允许 Parsing、Embedding、Failed；Ready 由摄取事务设置。")
        return value

    @field_validator("error_code", "error_message", mode="before")
    @classmethod
    def normalize_error_text(cls, value: Any) -> str | None:
        """清理文档失败信息中的可选文本。"""

        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("文档错误信息输入无效。")
        return value.strip() or None


def get_knowledge_base_service(
    session: Annotated[Session, Depends(get_db)],
) -> KnowledgeBaseService:
    """创建使用当前请求数据库会话的知识库服务。"""

    return KnowledgeBaseService(session)


KnowledgeBaseServiceDependency = Annotated[
    KnowledgeBaseService,
    Depends(get_knowledge_base_service),
]
KnowledgeBaseManager = Annotated[
    User,
    Depends(require_permission(Permission.MANAGE_KNOWLEDGE_BASES)),
]


def _knowledge_base_http_exception(error: BaseException) -> HTTPException:
    """将知识库服务异常转换为统一的中文 HTTP 错误。"""

    if isinstance(
        error, (KnowledgeBaseNotFoundError, DocumentNotFoundError, CourseNotFoundError)
    ):
        code = status.HTTP_404_NOT_FOUND
    elif isinstance(error, KnowledgeBasePermissionError):
        code = status.HTTP_403_FORBIDDEN
    elif isinstance(error, KnowledgeBaseConflictError):
        code = status.HTTP_409_CONFLICT
    elif isinstance(
        error,
        (KnowledgeBaseValidationError, DocumentValidationError, ValueError),
    ):
        code = status.HTTP_422_UNPROCESSABLE_ENTITY
    else:
        code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HTTPException(
        status_code=code,
        detail=str(error) or "知识库操作失败，请稍后重试。",
    )


def _knowledge_base_update_kwargs(
    payload: KnowledgeBaseUpdateRequest,
) -> dict[str, Any]:
    """只把请求中实际出现的知识库字段传给服务。"""

    fields = payload.model_fields_set
    return {
        field_name: getattr(payload, field_name)
        for field_name in ("name", "description")
        if field_name in fields
    }


def _ensure_document_belongs_to_knowledge_base(
    document: DocumentSummary,
    knowledge_base_id: UUID,
) -> DocumentSummary:
    """防止通过嵌套路径读取其他知识库的文档。"""

    if document.knowledge_base_id != str(knowledge_base_id):
        raise KnowledgeBaseConflictError("文档不属于指定知识库。")
    return document


@router.get("", response_model=list[KnowledgeBaseSummary])
def list_knowledge_bases(
    teacher: KnowledgeBaseManager,
    service: KnowledgeBaseServiceDependency,
    course_id: UUID | None = None,
) -> list[KnowledgeBaseSummary]:
    """列出当前教师课程下的知识库。"""

    try:
        return service.list_knowledge_bases(
            course_id=course_id,
            teacher_id=teacher.id,
        )
    except (CourseServiceError, KnowledgeBaseServiceError) as exc:
        raise _knowledge_base_http_exception(exc) from None


@router.post(
    "",
    response_model=KnowledgeBaseSummary,
    status_code=status.HTTP_201_CREATED,
)
def create_knowledge_base(
    payload: KnowledgeBaseCreateRequest,
    teacher: KnowledgeBaseManager,
    service: KnowledgeBaseServiceDependency,
) -> KnowledgeBaseSummary:
    """在当前教师拥有的课程下创建知识库。"""

    try:
        return service.create_knowledge_base(
            course_id=payload.course_id,
            name=payload.name,
            description=payload.description,
            teacher_id=teacher.id,
        )
    except (CourseServiceError, KnowledgeBaseServiceError, ValueError) as exc:
        raise _knowledge_base_http_exception(exc) from None


@router.get("/{knowledge_base_id}", response_model=KnowledgeBaseSummary)
def get_knowledge_base(
    knowledge_base_id: UUID,
    teacher: KnowledgeBaseManager,
    service: KnowledgeBaseServiceDependency,
) -> KnowledgeBaseSummary:
    """读取当前教师有权访问的知识库。"""

    try:
        return service.get_knowledge_base(knowledge_base_id, teacher_id=teacher.id)
    except (CourseServiceError, KnowledgeBaseServiceError) as exc:
        raise _knowledge_base_http_exception(exc) from None


@router.patch("/{knowledge_base_id}", response_model=KnowledgeBaseSummary)
def update_knowledge_base(
    knowledge_base_id: UUID,
    payload: KnowledgeBaseUpdateRequest,
    teacher: KnowledgeBaseManager,
    service: KnowledgeBaseServiceDependency,
) -> KnowledgeBaseSummary:
    """修改当前教师有权访问的知识库元数据。"""

    try:
        return service.update_knowledge_base(
            knowledge_base_id,
            teacher_id=teacher.id,
            **_knowledge_base_update_kwargs(payload),
        )
    except (CourseServiceError, KnowledgeBaseServiceError, ValueError) as exc:
        raise _knowledge_base_http_exception(exc) from None


@router.delete("/{knowledge_base_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_knowledge_base(
    knowledge_base_id: UUID,
    teacher: KnowledgeBaseManager,
    service: KnowledgeBaseServiceDependency,
) -> Response:
    """删除当前教师有权访问的知识库及其文档元数据。"""

    try:
        service.delete_knowledge_base(knowledge_base_id, teacher_id=teacher.id)
    except (CourseServiceError, KnowledgeBaseServiceError, ValueError) as exc:
        raise _knowledge_base_http_exception(exc) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{knowledge_base_id}/bind",
    response_model=KnowledgeBaseSummary,
)
def bind_knowledge_base(
    knowledge_base_id: UUID,
    payload: KnowledgeBaseBindRequest,
    teacher: KnowledgeBaseManager,
    service: KnowledgeBaseServiceDependency,
) -> KnowledgeBaseSummary:
    """将没有资料的知识库绑定到目标课程。"""

    try:
        return service.bind_to_course(
            knowledge_base_id,
            payload.course_id,
            teacher_id=teacher.id,
        )
    except (CourseServiceError, KnowledgeBaseServiceError, ValueError) as exc:
        raise _knowledge_base_http_exception(exc) from None


@router.get(
    "/{knowledge_base_id}/documents",
    response_model=list[DocumentSummary],
)
def list_documents(
    knowledge_base_id: UUID,
    teacher: KnowledgeBaseManager,
    service: KnowledgeBaseServiceDependency,
) -> list[DocumentSummary]:
    """列出指定知识库中的文档元数据。"""

    try:
        return service.list_documents(
            knowledge_base_id=knowledge_base_id,
            teacher_id=teacher.id,
        )
    except (CourseServiceError, KnowledgeBaseServiceError) as exc:
        raise _knowledge_base_http_exception(exc) from None


@router.post(
    "/{knowledge_base_id}/documents",
    response_model=DocumentSummary,
    status_code=status.HTTP_201_CREATED,
)
def create_document(
    knowledge_base_id: UUID,
    payload: DocumentCreateRequest,
    teacher: KnowledgeBaseManager,
    service: KnowledgeBaseServiceDependency,
) -> DocumentSummary:
    """登记文档元数据并将其置为 Uploaded 状态。"""

    try:
        return service.upload_document(
            knowledge_base_id=knowledge_base_id,
            uploaded_by=teacher.id,
            original_filename=payload.original_filename,
            file_format=payload.file_format,
            storage_path=payload.storage_path,
            teacher_id=teacher.id,
        )
    except (CourseServiceError, KnowledgeBaseServiceError, ValueError) as exc:
        raise _knowledge_base_http_exception(exc) from None


@router.post(
    "/{knowledge_base_id}/documents/upload",
    response_model=DocumentIngestionResult,
    status_code=status.HTTP_201_CREATED,
)
def upload_and_ingest_document(
    knowledge_base_id: UUID,
    file: Annotated[UploadFile, File(description="真实资料文件内容。")],
    teacher: KnowledgeBaseManager,
    service: KnowledgeBaseServiceDependency,
) -> DocumentIngestionResult:
    """上传真实文件内容并触发摄取，返回真实的处理状态。

    与仅登记元数据的 ``POST /{knowledge_base_id}/documents`` 不同，本端点接收 multipart
    文件正文，落盘后立即执行解析、清洗、分块与 Embedding；返回体中的状态是真实阶段
    结果（读取失败时给出具体失败原因），不会因为上传成功就报 Ready。

    本端点保持同步：摄取编排器内部使用 ``asyncio.run`` 驱动异步 Provider，FastAPI 会把
    同步路由放到线程池执行，避免与运行中的事件循环冲突。
    """

    filename = (file.filename or "").strip()
    data = file.file.read()
    try:
        stored_path = _store_upload_bytes(filename or "upload.bin", data)
        document = service.upload_document(
            knowledge_base_id=knowledge_base_id,
            uploaded_by=teacher.id,
            original_filename=filename or stored_path.name,
            storage_path=str(stored_path),
            teacher_id=teacher.id,
        )
        return service.ingest_document(
            document.id,
            content=data,
            teacher_id=teacher.id,
        )
    except (CourseServiceError, KnowledgeBaseServiceError, ValueError, OSError) as exc:
        raise _knowledge_base_http_exception(exc) from None


@router.get(
    "/{knowledge_base_id}/documents/{document_id}",
    response_model=DocumentSummary,
)
def get_document(
    knowledge_base_id: UUID,
    document_id: UUID,
    teacher: KnowledgeBaseManager,
    service: KnowledgeBaseServiceDependency,
) -> DocumentSummary:
    """读取指定知识库中的文档元数据和处理状态。"""

    try:
        document = service.get_document(document_id, teacher_id=teacher.id)
        return _ensure_document_belongs_to_knowledge_base(document, knowledge_base_id)
    except (CourseServiceError, KnowledgeBaseServiceError, ValueError) as exc:
        raise _knowledge_base_http_exception(exc) from None


@router.patch(
    "/{knowledge_base_id}/documents/{document_id}/status",
    response_model=DocumentSummary,
)
def update_document_status(
    knowledge_base_id: UUID,
    document_id: UUID,
    payload: DocumentStatusUpdateRequest,
    teacher: KnowledgeBaseManager,
    service: KnowledgeBaseServiceDependency,
) -> DocumentSummary:
    """更新指定知识库中文档的处理状态和失败信息。"""

    try:
        document = service.get_document(document_id, teacher_id=teacher.id)
        _ensure_document_belongs_to_knowledge_base(document, knowledge_base_id)
        return service.update_document_status(
            document_id,
            payload.status,
            error_code=payload.error_code,
            error_message=payload.error_message,
            retryable=payload.retryable,
            teacher_id=teacher.id,
        )
    except (CourseServiceError, KnowledgeBaseServiceError, ValueError) as exc:
        raise _knowledge_base_http_exception(exc) from None


@router.post(
    "/{knowledge_base_id}/documents/{document_id}/retry",
    response_model=DocumentSummary,
)
def retry_document(
    knowledge_base_id: UUID,
    document_id: UUID,
    teacher: KnowledgeBaseManager,
    service: KnowledgeBaseServiceDependency,
) -> DocumentSummary:
    """重新处理指定知识库中可重试的失败文档。"""

    try:
        document = service.get_document(document_id, teacher_id=teacher.id)
        _ensure_document_belongs_to_knowledge_base(document, knowledge_base_id)
        return service.retry_document(document_id, teacher_id=teacher.id)
    except (CourseServiceError, KnowledgeBaseServiceError, ValueError) as exc:
        raise _knowledge_base_http_exception(exc) from None


__all__ = [
    "DocumentCreateRequest",
    "DocumentIngestionResult",
    "DocumentStatusUpdateRequest",
    "KnowledgeBaseBindRequest",
    "KnowledgeBaseCreateRequest",
    "KnowledgeBaseUpdateRequest",
    "get_knowledge_base_service",
    "router",
]
