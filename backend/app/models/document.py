"""课程资料元数据模型。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import JSON, Boolean, CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.domain.enums import DocumentPurpose, DocumentStatus
from backend.app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, enum_type

if TYPE_CHECKING:
    from backend.app.models.course import Course
    from backend.app.models.document_chunk import DocumentChunk
    from backend.app.models.knowledge_base import KnowledgeBase
    from backend.app.models.paper_import import PaperImport
    from backend.app.models.user import User


class Document(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """记录上传文件来源和摄取处理状态的资料实体。"""

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("(purpose = 'knowledge_base' AND knowledge_base_id IS NOT NULL) OR (purpose = 'paper_source' AND knowledge_base_id IS NULL)", name="ck_document_purpose_knowledge_base"),
        Index("ix_documents_course_purpose", "course_id", "purpose"),
        CheckConstraint(
            "file_metadata IS NULL OR jsonb_typeof(file_metadata) = 'object'",
            name="ck_document_file_metadata_object",
        ).ddl_if(dialect="postgresql"),
    )

    course_id: Mapped[UUID] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    knowledge_base_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=True, index=True
    )
    purpose: Mapped[DocumentPurpose] = mapped_column(enum_type(DocumentPurpose, "document_purpose", length=32), default=DocumentPurpose.KNOWLEDGE_BASE, server_default="knowledge_base", nullable=False)
    uploaded_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_format: Mapped[str] = mapped_column(String(32), nullable=False)
    storage_path: Mapped[str | None] = mapped_column(String(1024))
    file_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")
    )
    status: Mapped[DocumentStatus] = mapped_column(
        enum_type(DocumentStatus, "document_status"),
        default=DocumentStatus.UPLOADED,
        nullable=False,
        index=True,
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    retryable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    course: Mapped[Course] = relationship("Course", back_populates="documents")
    paper_import: Mapped[PaperImport | None] = relationship("PaperImport", back_populates="document", passive_deletes="all")
    knowledge_base: Mapped[KnowledgeBase | None] = relationship(
        "KnowledgeBase", back_populates="documents"
    )
    uploader: Mapped[User] = relationship(
        "User", back_populates="uploaded_documents", foreign_keys=[uploaded_by]
    )
    chunks: Mapped[list[DocumentChunk]] = relationship(
        "DocumentChunk",
        back_populates="document",
        cascade="all, delete-orphan",
    )


__all__ = ["Document"]
