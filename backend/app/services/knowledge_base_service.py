"""知识库服务：知识库元数据、课程绑定和文档处理状态。"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from backend.app.ai.embedding.base import EMBEDDING_DIMENSION_MISMATCH
from backend.app.ai.ingestion.parsers import DOCUMENT_PARSE_FAILED
from backend.app.ai.ingestion.service import (
    KNOWLEDGE_BASE_EMPTY,
    IngestionResult,
    IngestionService,
    IngestionTransition,
    StatusListener,
    resolve_error_message,
)
from backend.app.domain.enums import DocumentStatus
from backend.app.models import Course, Document, DocumentChunk, KnowledgeBase, User
from backend.app.models.document_chunk import EMBEDDING_VECTOR_DIMENSION
from backend.app.services.course_service import (
    CourseNotFoundError,
    CoursePermissionError,
    CourseServiceError,
)

_UNSET = object()
_SUPPORTED_FORMATS = {
    "pdf": "pdf",
    ".pdf": "pdf",
    "txt": "txt",
    ".txt": "txt",
    "text": "txt",
    "md": "md",
    ".md": "md",
    "markdown": "md",
    ".markdown": "md",
}
_ERROR_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_ALLOWED_DOCUMENT_TRANSITIONS = {
    DocumentStatus.UPLOADED: {
        DocumentStatus.PARSING,
        DocumentStatus.FAILED,
    },
    DocumentStatus.PARSING: {
        DocumentStatus.CHUNKING,
        DocumentStatus.EMBEDDING,
        DocumentStatus.FAILED,
    },
    DocumentStatus.CHUNKING: {
        DocumentStatus.EMBEDDING,
        DocumentStatus.FAILED,
    },
    DocumentStatus.EMBEDDING: {
        DocumentStatus.READY,
        DocumentStatus.FAILED,
    },
    DocumentStatus.READY: {
        DocumentStatus.PARSING,
        DocumentStatus.FAILED,
    },
    DocumentStatus.FAILED: {
        DocumentStatus.PARSING,
        DocumentStatus.FAILED,
    },
}


class KnowledgeBaseServiceError(CourseServiceError):
    """知识库服务的异常基类。"""


class KnowledgeBaseNotFoundError(KnowledgeBaseServiceError):
    """知识库或其关联资源不存在时抛出。"""


class KnowledgeBaseConflictError(KnowledgeBaseServiceError):
    """知识库变更违反数据约束或来源关系时抛出。"""


class KnowledgeBaseValidationError(KnowledgeBaseServiceError):
    """知识库输入不合法时抛出。"""


class KnowledgeBasePermissionError(
    CoursePermissionError,
    KnowledgeBaseServiceError,
):
    """当前教师无权访问课程资源时抛出。"""


class DocumentNotFoundError(KnowledgeBaseServiceError):
    """文档不存在时抛出。"""


class DocumentValidationError(KnowledgeBaseServiceError):
    """文档元数据或处理状态不合法时抛出。"""


class KnowledgeBaseSummary(BaseModel):
    """面向 API 和 UI 的知识库元数据摘要。"""

    model_config = ConfigDict(frozen=True)

    id: str
    course_id: str
    name: str
    description: str | None = None
    document_count: int = 0
    created_at: datetime
    updated_at: datetime


class DocumentSummary(BaseModel):
    """面向 API 和 UI 的文档元数据与处理状态摘要。"""

    model_config = ConfigDict(frozen=True)

    id: str
    course_id: str
    knowledge_base_id: str
    uploaded_by: str
    original_filename: str
    file_format: str
    storage_path: str | None = None
    status: DocumentStatus
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    created_at: datetime
    updated_at: datetime


class DocumentIngestionResult(BaseModel):
    """资料摄取结果摘要：真实终态、片段数量和可展示的失败原因。"""

    model_config = ConfigDict(frozen=True)

    document_id: str
    status: DocumentStatus
    chunk_count: int = 0
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    detail: str | None = None


def _safe_failure_detail(error: BaseException) -> str:
    """返回可安全展示的失败技术原因，不包含资料内容或敏感配置。"""

    return f"知识片段写入失败：{type(error).__name__}"


def _default_ingestion_service_factory(listener: StatusListener) -> IngestionService:
    """构造默认摄取编排器，并把阶段回调交给 Service 层落库。"""

    return IngestionService(on_transition=listener)


def _normalize_uuid(value: UUID | str | None, field_name: str) -> UUID:
    """将外部标识规范化为 UUID。"""

    if value is None:
        raise KnowledgeBaseValidationError(f"{field_name}不能为空。")
    try:
        return value if isinstance(value, UUID) else UUID(str(value).strip())
    except (AttributeError, TypeError, ValueError) as exc:
        raise KnowledgeBaseValidationError(f"{field_name}无效。") from exc


def _normalize_required_text(
    value: str | None,
    field_name: str,
    *,
    max_length: int,
) -> str:
    """清理必填文本并执行长度校验。"""

    if not isinstance(value, str) or not value.strip():
        raise KnowledgeBaseValidationError(f"{field_name}不能为空。")
    normalized = value.strip()
    if "\x00" in normalized:
        raise KnowledgeBaseValidationError(f"{field_name}包含无效字符。")
    if len(normalized) > max_length:
        raise KnowledgeBaseValidationError(
            f"{field_name}长度不能超过 {max_length} 个字符。"
        )
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
        raise KnowledgeBaseValidationError(f"{field_name}输入无效。")
    normalized = value.strip()
    if "\x00" in normalized:
        raise KnowledgeBaseValidationError(f"{field_name}包含无效字符。")
    if len(normalized) > max_length:
        raise KnowledgeBaseValidationError(
            f"{field_name}长度不能超过 {max_length} 个字符。"
        )
    return normalized or None


def _resolve_actor_id(
    teacher_id: UUID | str | None,
    actor_id: UUID | str | None = None,
) -> UUID | None:
    """兼容教师和操作人标识，并拒绝互相冲突的输入。"""

    normalized_teacher = (
        _normalize_uuid(teacher_id, "教师标识") if teacher_id is not None else None
    )
    normalized_actor = (
        _normalize_uuid(actor_id, "操作人标识") if actor_id is not None else None
    )
    if (
        normalized_teacher is not None
        and normalized_actor is not None
        and normalized_teacher != normalized_actor
    ):
        raise KnowledgeBaseValidationError("教师标识与操作人标识不一致。")
    return normalized_teacher or normalized_actor


def _normalize_document_status(value: DocumentStatus | str) -> DocumentStatus:
    """将文档状态名称或枚举值规范化为领域枚举。"""

    if isinstance(value, DocumentStatus):
        return value
    if not isinstance(value, str):
        raise DocumentValidationError("文档状态无效。")
    candidate = value.strip()
    for status in DocumentStatus:
        if candidate.lower() in {status.name.lower(), status.value.lower()}:
            return status
    raise DocumentValidationError("文档状态无效。")


def _normalize_file_format(
    original_filename: str,
    file_format: str | None,
) -> str:
    """从显式格式或文件名后缀推导受支持的规范格式。"""

    if file_format is not None and not isinstance(file_format, str):
        raise DocumentValidationError("文档格式输入无效。")
    candidate = file_format.strip().lower() if file_format is not None else ""
    if file_format is None:
        candidate = Path(original_filename).suffix.lower()
    normalized = _SUPPORTED_FORMATS.get(candidate)
    if normalized is None:
        raise DocumentValidationError("当前仅支持 PDF、TXT 和 Markdown 文件。")
    return normalized


def _normalize_error_code(value: str | None) -> str | None:
    """校验失败状态使用的机器可读错误码。"""

    if value is None:
        return None
    if not isinstance(value, str):
        raise DocumentValidationError("文档错误码无效。")
    normalized = value.strip().upper()
    if not _ERROR_CODE_PATTERN.fullmatch(normalized):
        raise DocumentValidationError("文档错误码无效。")
    return normalized


class KnowledgeBaseService:
    """封装知识库、课程绑定和文档元数据相关业务。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create_knowledge_base(
        self,
        course_id: UUID | str,
        name: str,
        description: str | None = None,
        *,
        teacher_id: UUID | str | None = None,
    ) -> KnowledgeBaseSummary:
        """在指定课程下创建知识库元数据。"""

        course = self._load_course(course_id)
        self._ensure_course_access(
            course,
            _resolve_actor_id(teacher_id),
        )
        knowledge_base = KnowledgeBase(
            course_id=course.id,
            name=_normalize_required_text(name, "知识库名称", max_length=160),
            description=_normalize_optional_text(
                description,
                "知识库描述",
                max_length=65535,
            ),
        )
        self.session.add(knowledge_base)
        return self._commit_knowledge_base(knowledge_base, "创建知识库失败。")

    def list_knowledge_bases(
        self,
        course_id: UUID | str | None = None,
        teacher_id: UUID | str | None = None,
    ) -> list[KnowledgeBaseSummary]:
        """列出知识库；传入课程或教师标识时限制查询范围。"""

        normalized_course_id = (
            _normalize_uuid(course_id, "课程标识") if course_id is not None else None
        )
        actor_id = _resolve_actor_id(teacher_id)
        if normalized_course_id is not None:
            course = self._load_course(normalized_course_id)
            self._ensure_course_access(course, actor_id)

        statement = (
            select(KnowledgeBase)
            .options(selectinload(KnowledgeBase.documents))
            .join(Course, KnowledgeBase.course_id == Course.id)
        )
        if normalized_course_id is not None:
            statement = statement.where(KnowledgeBase.course_id == normalized_course_id)
        if actor_id is not None:
            statement = statement.where(Course.created_by == actor_id)
        statement = statement.order_by(KnowledgeBase.created_at, KnowledgeBase.name)
        try:
            knowledge_bases = self.session.scalars(statement).all()
        except SQLAlchemyError as exc:
            raise KnowledgeBaseServiceError("无法读取知识库列表。") from exc
        return [self._knowledge_base_summary(item) for item in knowledge_bases]

    def get_knowledge_base(
        self,
        knowledge_base_id: UUID | str,
        teacher_id: UUID | str | None = None,
    ) -> KnowledgeBaseSummary:
        """读取知识库元数据并按需检查课程所有权。"""

        knowledge_base = self._load_knowledge_base(knowledge_base_id)
        self._ensure_course_access(
            self._load_course(knowledge_base.course_id),
            _resolve_actor_id(teacher_id),
        )
        return self._knowledge_base_summary(knowledge_base)

    def update_knowledge_base(
        self,
        knowledge_base_id: UUID | str,
        name: str | None = None,
        description: str | None | object = _UNSET,
        *,
        teacher_id: UUID | str | None = None,
    ) -> KnowledgeBaseSummary:
        """更新知识库名称或描述。"""

        if name is None and description is _UNSET:
            raise KnowledgeBaseValidationError("至少需要提供一个更新字段。")
        knowledge_base = self._load_knowledge_base(knowledge_base_id)
        self._ensure_course_access(
            self._load_course(knowledge_base.course_id),
            _resolve_actor_id(teacher_id),
        )
        if name is not None:
            knowledge_base.name = _normalize_required_text(
                name,
                "知识库名称",
                max_length=160,
            )
        if description is not _UNSET:
            if description is not None and not isinstance(description, str):
                raise KnowledgeBaseValidationError("知识库描述输入无效。")
            knowledge_base.description = _normalize_optional_text(
                description,
                "知识库描述",
                max_length=65535,
            )
        return self._commit_knowledge_base(knowledge_base, "更新知识库失败。")

    def delete_knowledge_base(
        self,
        knowledge_base_id: UUID | str,
        teacher_id: UUID | str | None = None,
    ) -> None:
        """删除知识库及其由 ORM 管理的文档元数据。"""

        knowledge_base = self._load_knowledge_base(knowledge_base_id)
        self._ensure_course_access(
            self._load_course(knowledge_base.course_id),
            _resolve_actor_id(teacher_id),
        )
        try:
            self.session.delete(knowledge_base)
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise KnowledgeBaseConflictError(
                "知识库仍被业务数据引用，无法删除。"
            ) from exc
        except SQLAlchemyError as exc:
            self.session.rollback()
            raise KnowledgeBaseServiceError("删除知识库失败。") from exc

    def bind_to_course(
        self,
        knowledge_base_id: UUID | str,
        course_id: UUID | str,
        *,
        teacher_id: UUID | str | None = None,
    ) -> KnowledgeBaseSummary:
        """将没有资料的知识库绑定到指定课程。"""

        knowledge_base = self._load_knowledge_base(knowledge_base_id)
        target_course = self._load_course(course_id)
        actor_id = _resolve_actor_id(teacher_id)
        self._ensure_course_access(target_course, actor_id)

        if knowledge_base.course_id != target_course.id:
            source_course = self._load_course(knowledge_base.course_id)
            self._ensure_course_access(source_course, actor_id)
            if knowledge_base.documents:
                raise KnowledgeBaseConflictError("已有资料的知识库不能更换课程。")
            knowledge_base.course = target_course
            knowledge_base.course_id = target_course.id
        return self._commit_knowledge_base(knowledge_base, "绑定知识库失败。")

    bind_knowledge_base = bind_to_course

    def list_documents(
        self,
        knowledge_base_id: UUID | str | None = None,
        course_id: UUID | str | None = None,
        teacher_id: UUID | str | None = None,
    ) -> list[DocumentSummary]:
        """按知识库或课程列出文档元数据。"""

        normalized_knowledge_base_id = (
            _normalize_uuid(knowledge_base_id, "知识库标识")
            if knowledge_base_id is not None
            else None
        )
        normalized_course_id = (
            _normalize_uuid(course_id, "课程标识") if course_id is not None else None
        )
        actor_id = _resolve_actor_id(teacher_id)

        if normalized_knowledge_base_id is not None:
            knowledge_base = self._load_knowledge_base(normalized_knowledge_base_id)
            if (
                normalized_course_id is not None
                and knowledge_base.course_id != normalized_course_id
            ):
                raise KnowledgeBaseConflictError("知识库不属于指定课程。")
            normalized_course_id = knowledge_base.course_id
        if normalized_course_id is not None:
            self._ensure_course_access(
                self._load_course(normalized_course_id),
                actor_id,
            )

        statement = select(Document)
        if normalized_knowledge_base_id is not None:
            statement = statement.where(
                Document.knowledge_base_id == normalized_knowledge_base_id
            )
        if normalized_course_id is not None:
            statement = statement.where(Document.course_id == normalized_course_id)
        if actor_id is not None:
            statement = statement.join(
                Course,
                Document.course_id == Course.id,
            ).where(Course.created_by == actor_id)
        statement = statement.order_by(Document.created_at, Document.original_filename)
        try:
            documents = self.session.scalars(statement).all()
        except SQLAlchemyError as exc:
            raise KnowledgeBaseServiceError("无法读取文档列表。") from exc
        return [self._document_summary(item) for item in documents]

    def get_document(
        self,
        document_id: UUID | str,
        teacher_id: UUID | str | None = None,
    ) -> DocumentSummary:
        """读取文档元数据和处理状态。"""

        document = self._load_document(document_id)
        self._ensure_course_access(
            self._load_course(document.course_id),
            _resolve_actor_id(teacher_id),
        )
        return self._document_summary(document)

    def upload_document(
        self,
        course_id: UUID | str | None = None,
        knowledge_base_id: UUID | str | None = None,
        uploaded_by: UUID | str | None = None,
        original_filename: str | None = None,
        file_format: str | None = None,
        storage_path: str | None = None,
        *,
        teacher_id: UUID | str | None = None,
        uploader_id: UUID | str | None = None,
    ) -> DocumentSummary:
        """登记课程资料元数据，并以 Uploaded 状态进入后续摄取流程。"""

        normalized_knowledge_base_id = _normalize_uuid(
            knowledge_base_id,
            "知识库标识",
        )
        knowledge_base = self._load_knowledge_base(normalized_knowledge_base_id)
        normalized_course_id = (
            _normalize_uuid(course_id, "课程标识")
            if course_id is not None
            else knowledge_base.course_id
        )
        if knowledge_base.course_id != normalized_course_id:
            raise KnowledgeBaseConflictError("知识库不属于指定课程。")

        course = self._load_course(normalized_course_id)
        normalized_uploader_id = self._resolve_uploader_id(uploaded_by, uploader_id)
        actor_id = _resolve_actor_id(teacher_id, normalized_uploader_id)
        self._ensure_course_access(course, actor_id or normalized_uploader_id)
        self._ensure_user_exists(normalized_uploader_id)
        normalized_filename = _normalize_required_text(
            original_filename,
            "原始文件名",
            max_length=255,
        )
        normalized_format = _normalize_file_format(
            normalized_filename,
            file_format,
        )
        normalized_storage_path = _normalize_optional_text(
            storage_path,
            "存储路径",
            max_length=1024,
        )
        document = Document(
            course_id=course.id,
            knowledge_base_id=knowledge_base.id,
            uploaded_by=normalized_uploader_id,
            original_filename=normalized_filename,
            file_format=normalized_format,
            storage_path=normalized_storage_path,
            status=DocumentStatus.UPLOADED,
            retryable=False,
        )
        self.session.add(document)
        return self._commit_document(document, "上传文档元数据失败。")

    create_document = upload_document

    def update_document_status(
        self,
        document_id: UUID | str,
        status: DocumentStatus | str,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
        retryable: bool | None = None,
        teacher_id: UUID | str | None = None,
    ) -> DocumentSummary:
        """按状态机持久化文档处理状态和失败信息。"""

        document = self._load_document(document_id)
        self._ensure_course_access(
            self._load_course(document.course_id),
            _resolve_actor_id(teacher_id),
        )
        next_status = _normalize_document_status(status)
        current_status = document.status
        if (
            next_status != current_status
            and next_status not in _ALLOWED_DOCUMENT_TRANSITIONS[current_status]
        ):
            raise DocumentValidationError(
                f"文档状态不能从“{current_status.value}”变更为“{next_status.value}”。"
            )

        if next_status is DocumentStatus.FAILED:
            normalized_code = _normalize_error_code(error_code)
            normalized_message = _normalize_optional_text(
                error_message,
                "文档错误提示",
                max_length=2000,
            )
            if current_status is DocumentStatus.FAILED:
                normalized_code = normalized_code or document.error_code
                normalized_message = normalized_message or document.error_message
            if not normalized_code or not normalized_message:
                raise DocumentValidationError(
                    "文档进入失败状态时必须提供错误码和错误提示。"
                )
            if retryable is not None and not isinstance(retryable, bool):
                raise DocumentValidationError("文档可重试标记无效。")
            document.error_code = normalized_code
            document.error_message = normalized_message
            document.retryable = (
                retryable if retryable is not None else document.retryable
            )
        else:
            if error_code is not None or error_message is not None:
                raise DocumentValidationError("非失败状态不能保存文档错误信息。")
            if retryable is not None and not isinstance(retryable, bool):
                raise DocumentValidationError("文档可重试标记无效。")
            document.error_code = None
            document.error_message = None
            document.retryable = False

        document.status = next_status
        return self._commit_document(document, "更新文档处理状态失败。")

    set_document_status = update_document_status

    def mark_document_failed(
        self,
        document_id: UUID | str,
        *,
        error_code: str,
        error_message: str,
        retryable: bool = False,
        teacher_id: UUID | str | None = None,
    ) -> DocumentSummary:
        """以失败状态记录文档错误码、提示和可重试标记。"""

        return self.update_document_status(
            document_id,
            DocumentStatus.FAILED,
            error_code=error_code,
            error_message=error_message,
            retryable=retryable,
            teacher_id=teacher_id,
        )

    def retry_document(
        self,
        document_id: UUID | str,
        teacher_id: UUID | str | None = None,
    ) -> DocumentSummary:
        """将可重试的失败文档重新置为 Parsing 状态。"""

        document = self._load_document(document_id)
        self._ensure_course_access(
            self._load_course(document.course_id),
            _resolve_actor_id(teacher_id),
        )
        if document.status is not DocumentStatus.FAILED:
            raise DocumentValidationError("只有失败文档可以重新处理。")
        if not document.retryable:
            raise DocumentValidationError("当前资料不可直接重试，请修正后重新上传。")
        return self.update_document_status(
            document.id,
            DocumentStatus.PARSING,
            teacher_id=teacher_id,
        )

    def ingest_document(
        self,
        document_id: UUID | str,
        *,
        content: bytes | None = None,
        teacher_id: UUID | str | None = None,
        ingestion_service_factory: Callable[
            [StatusListener], IngestionService
        ]
        | None = None,
    ) -> DocumentIngestionResult:
        """读取真实文件内容并执行摄取，返回真实的处理结果。

        编排步骤（Uploaded -> Parsing -> Chunking -> Embedding -> Ready/Failed）由 T037 的
        ``IngestionService`` 负责，本方法只负责：读取文件字节、把真实阶段与结果落库、
        在成功后写入 ``DocumentChunk``（含向量与来源元数据）。失败时不得留下任何可用片段。

        注意：编排器是异步的，而 Service 层保持同步接口（FastAPI 同步路由在线程池中执行），
        因此这里用 ``asyncio.run`` 桥接；若在已有事件循环内调用需改用线程池执行。
        """

        document = self._load_document(document_id)
        self._ensure_course_access(
            self._load_course(document.course_id),
            _resolve_actor_id(teacher_id),
        )
        payload = self._read_document_content(document, content)

        def persist_transition(transition: IngestionTransition) -> None:
            """把编排器的中间阶段真实写入文档状态。"""

            if transition.status in {DocumentStatus.CHUNKING, DocumentStatus.EMBEDDING}:
                self.update_document_status(
                    document.id,
                    transition.status,
                    teacher_id=teacher_id,
                )

        self.update_document_status(
            document.id,
            DocumentStatus.PARSING,
            teacher_id=teacher_id,
        )
        factory = ingestion_service_factory or _default_ingestion_service_factory
        runner = factory(persist_transition)
        result = asyncio.run(
            runner.ingest(
                filename=document.original_filename,
                data=payload,
                document_id=document.id,
                course_id=document.course_id,
                knowledge_base_id=document.knowledge_base_id,
            )
        )
        return self._persist_ingestion_result(
            document,
            result,
            teacher_id=teacher_id,
        )

    def list_document_chunks(
        self,
        document_id: UUID | str,
        teacher_id: UUID | str | None = None,
    ) -> list[dict[str, Any]]:
        """读取文档的知识片段摘要，供界面展示摄取结果。"""

        document = self._load_document(document_id)
        self._ensure_course_access(
            self._load_course(document.course_id),
            _resolve_actor_id(teacher_id),
        )
        statement = (
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document.id)
            .order_by(DocumentChunk.chunk_index)
        )
        try:
            chunks = self.session.scalars(statement).all()
        except SQLAlchemyError as exc:
            raise KnowledgeBaseServiceError("无法读取知识片段。") from exc
        return [
            {
                "id": str(chunk.id),
                "chunk_index": chunk.chunk_index,
                "content": chunk.content,
                "metadata": chunk.chunk_metadata,
                "has_embedding": chunk.embedding is not None,
            }
            for chunk in chunks
        ]

    @staticmethod
    def _read_document_content(document: Document, content: bytes | None) -> bytes:
        """优先使用调用方传入的文件字节，否则从存储路径读取。"""

        if content is not None:
            if not isinstance(content, (bytes, bytearray, memoryview)):
                raise DocumentValidationError("资料内容必须是字节数据。")
            return bytes(content)
        if not document.storage_path:
            raise DocumentValidationError("资料缺少可读取的文件内容，请重新上传。")
        path = Path(document.storage_path)
        try:
            if not path.is_file():
                raise DocumentValidationError("资料文件不存在，请重新上传。")
            return path.read_bytes()
        except DocumentValidationError:
            raise
        except OSError as exc:
            raise DocumentValidationError("资料文件不可读，请重新上传。") from exc

    def _persist_ingestion_result(
        self,
        document: Document,
        result: IngestionResult,
        *,
        teacher_id: UUID | str | None = None,
    ) -> DocumentIngestionResult:
        """把摄取结果写入文档状态与知识片段，失败时清理残留片段。"""

        self._delete_document_chunks(document.id)
        if not result.succeeded:
            return self._mark_ingestion_failed(document, result, teacher_id=teacher_id)

        mismatched = next(
            (
                chunk
                for chunk in result.chunks
                if len(chunk.embedding) != EMBEDDING_VECTOR_DIMENSION
            ),
            None,
        )
        if mismatched is not None:
            return self._mark_ingestion_failed(
                document,
                result,
                error_code=EMBEDDING_DIMENSION_MISMATCH,
                retryable=False,
                teacher_id=teacher_id,
            )

        rows = [
            DocumentChunk(
                document_id=document.id,
                course_id=document.course_id,
                knowledge_base_id=document.knowledge_base_id,
                chunk_index=chunk.chunk_index,
                content=chunk.content,
                embedding=list(chunk.embedding),
                chunk_metadata=dict(chunk.metadata),
            )
            for chunk in result.chunks
        ]
        if self._supports_postgres_features():
            # 只有 PostgreSQL 能维护 tsvector；测试方言保持为空而不是伪造全文内容。
            for row in rows:
                row.search_vector = func.to_tsvector("simple", row.content)
        try:
            self.session.add_all(rows)
            self.session.commit()
        except (IntegrityError, SQLAlchemyError) as exc:
            self.session.rollback()
            self._delete_document_chunks(document.id)
            return self._mark_ingestion_failed(
                document,
                result,
                error_code=result.error_code or DOCUMENT_PARSE_FAILED,
                retryable=True,
                detail=_safe_failure_detail(exc),
                teacher_id=teacher_id,
            )

        summary = self.update_document_status(
            document.id,
            DocumentStatus.READY,
            teacher_id=teacher_id,
        )
        return DocumentIngestionResult(
            document_id=summary.id,
            status=summary.status,
            chunk_count=len(rows),
        )

    def _mark_ingestion_failed(
        self,
        document: Document,
        result: IngestionResult,
        *,
        error_code: str | None = None,
        retryable: bool | None = None,
        detail: str | None = None,
        teacher_id: UUID | str | None = None,
    ) -> DocumentIngestionResult:
        """按真实失败原因标记文档失败；空知识库属于终态，不允许直接重试。"""

        resolved_code = error_code or result.error_code or DOCUMENT_PARSE_FAILED
        resolved_message = (
            result.error_message
            if error_code is None and result.error_message
            else resolve_error_message(resolved_code)
        )
        resolved_retryable = (
            retryable
            if retryable is not None
            else (False if resolved_code == KNOWLEDGE_BASE_EMPTY else bool(result.retryable))
        )
        summary = self.update_document_status(
            document.id,
            DocumentStatus.FAILED,
            error_code=resolved_code,
            error_message=resolved_message,
            retryable=resolved_retryable,
            teacher_id=teacher_id,
        )
        return DocumentIngestionResult(
            document_id=summary.id,
            status=summary.status,
            chunk_count=0,
            error_code=resolved_code,
            error_message=resolved_message,
            retryable=resolved_retryable,
            detail=detail or result.detail,
        )

    def _delete_document_chunks(self, document_id: UUID) -> None:
        """删除文档已有的知识片段，保证重复摄取不会残留旧知识。"""

        try:
            self.session.execute(
                delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
            )
            self.session.flush()
        except SQLAlchemyError as exc:
            self.session.rollback()
            raise KnowledgeBaseServiceError("无法清理已有知识片段。") from exc

    def _supports_postgres_features(self) -> bool:
        """判断当前会话是否使用 PostgreSQL（tsvector 与 pgvector 只在 PG 生效）。"""

        bind = self.session.get_bind()
        return bind is not None and bind.dialect.name == "postgresql"

    def _load_course(self, course_id: UUID | str) -> Course:
        """加载课程实体并统一处理不存在错误。"""

        normalized_id = _normalize_uuid(course_id, "课程标识")
        try:
            course = self.session.scalar(
                select(Course)
                .options(selectinload(Course.knowledge_bases))
                .where(Course.id == normalized_id)
            )
        except SQLAlchemyError as exc:
            raise KnowledgeBaseServiceError("无法读取课程信息。") from exc
        if course is None:
            raise CourseNotFoundError("课程不存在。")
        return course

    def _load_knowledge_base(self, knowledge_base_id: UUID | str) -> KnowledgeBase:
        """加载知识库实体并统一处理不存在错误。"""

        normalized_id = _normalize_uuid(knowledge_base_id, "知识库标识")
        try:
            knowledge_base = self.session.scalar(
                select(KnowledgeBase)
                .options(selectinload(KnowledgeBase.documents))
                .where(KnowledgeBase.id == normalized_id)
            )
        except SQLAlchemyError as exc:
            raise KnowledgeBaseServiceError("无法读取知识库信息。") from exc
        if knowledge_base is None:
            raise KnowledgeBaseNotFoundError("知识库不存在。")
        return knowledge_base

    def _load_document(self, document_id: UUID | str) -> Document:
        """加载文档实体并统一处理不存在错误。"""

        normalized_id = _normalize_uuid(document_id, "文档标识")
        try:
            document = self.session.scalar(
                select(Document).where(Document.id == normalized_id)
            )
        except SQLAlchemyError as exc:
            raise KnowledgeBaseServiceError("无法读取文档信息。") from exc
        if document is None:
            raise DocumentNotFoundError("文档不存在。")
        return document

    @staticmethod
    def _ensure_course_access(course: Course, actor_id: UUID | None) -> None:
        """检查当前操作人是否为课程所有者。"""

        if actor_id is not None and course.created_by != actor_id:
            raise KnowledgeBasePermissionError("无权访问该课程。")

    def _ensure_user_exists(self, user_id: UUID) -> None:
        """确认文档上传者存在，保留完整的来源关系。"""

        try:
            user = self.session.get(User, user_id)
        except SQLAlchemyError as exc:
            raise KnowledgeBaseServiceError("无法读取文档上传者信息。") from exc
        if user is None:
            raise DocumentValidationError("文档上传者不存在。")

    @staticmethod
    def _resolve_uploader_id(
        uploaded_by: UUID | str | None,
        uploader_id: UUID | str | None,
    ) -> UUID:
        """兼容上传者标识别名，并拒绝互相冲突的输入。"""

        normalized_uploaded_by = (
            _normalize_uuid(uploaded_by, "上传者标识")
            if uploaded_by is not None
            else None
        )
        normalized_uploader = (
            _normalize_uuid(uploader_id, "上传者标识")
            if uploader_id is not None
            else None
        )
        if (
            normalized_uploaded_by is not None
            and normalized_uploader is not None
            and normalized_uploaded_by != normalized_uploader
        ):
            raise DocumentValidationError("上传者标识不一致。")
        normalized = normalized_uploaded_by or normalized_uploader
        if normalized is None:
            raise DocumentValidationError("上传者标识不能为空。")
        return normalized

    def _commit_knowledge_base(
        self,
        knowledge_base: KnowledgeBase,
        fallback_message: str,
    ) -> KnowledgeBaseSummary:
        """提交知识库变更并转换为摘要。"""

        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise KnowledgeBaseConflictError(
                "同一课程下的知识库名称不能重复。"
            ) from exc
        except SQLAlchemyError as exc:
            self.session.rollback()
            raise KnowledgeBaseServiceError(fallback_message) from exc

        self.session.refresh(knowledge_base)
        return self._knowledge_base_summary(knowledge_base)

    def _commit_document(
        self,
        document: Document,
        fallback_message: str,
    ) -> DocumentSummary:
        """提交文档变更并转换为摘要。"""

        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise KnowledgeBaseConflictError("文档元数据与现有数据冲突。") from exc
        except SQLAlchemyError as exc:
            self.session.rollback()
            raise KnowledgeBaseServiceError(fallback_message) from exc

        self.session.refresh(document)
        return self._document_summary(document)

    @staticmethod
    def _knowledge_base_summary(
        knowledge_base: KnowledgeBase,
    ) -> KnowledgeBaseSummary:
        """将知识库实体转换为不暴露 ORM 状态的摘要。"""

        documents: Sequence[Any] = getattr(knowledge_base, "documents", ())
        return KnowledgeBaseSummary(
            id=str(knowledge_base.id),
            course_id=str(knowledge_base.course_id),
            name=knowledge_base.name,
            description=knowledge_base.description,
            document_count=len(documents),
            created_at=knowledge_base.created_at,
            updated_at=knowledge_base.updated_at,
        )

    @staticmethod
    def _document_summary(document: Document) -> DocumentSummary:
        """将文档实体转换为不暴露 ORM 状态的摘要。"""

        return DocumentSummary(
            id=str(document.id),
            course_id=str(document.course_id),
            knowledge_base_id=str(document.knowledge_base_id),
            uploaded_by=str(document.uploaded_by),
            original_filename=document.original_filename,
            file_format=document.file_format,
            storage_path=document.storage_path,
            status=document.status,
            error_code=document.error_code,
            error_message=document.error_message,
            retryable=document.retryable,
            created_at=document.created_at,
            updated_at=document.updated_at,
        )


__all__ = [
    "DocumentIngestionResult",
    "DocumentNotFoundError",
    "DocumentSummary",
    "DocumentValidationError",
    "KnowledgeBaseConflictError",
    "KnowledgeBaseNotFoundError",
    "KnowledgeBasePermissionError",
    "KnowledgeBaseService",
    "KnowledgeBaseServiceError",
    "KnowledgeBaseSummary",
    "KnowledgeBaseValidationError",
]
