"""试卷导入身份与原文件只读关系。"""
from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.domain.enums import PaperImportStatus
from backend.app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, enum_type

if TYPE_CHECKING:
    from backend.app.models.document import Document
    from backend.app.models.extracted_question import ExtractedQuestion
    from backend.app.models.source_page import SourcePage


class PaperImport(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "paper_imports"
    __table_args__ = (
        CheckConstraint("page_count IS NULL OR page_count BETWEEN 1 AND 50", name="ck_paper_import_page_count"),
        CheckConstraint("status NOT IN ('Pending Review', 'Ready') OR page_count IS NOT NULL", name="ck_paper_import_known_pages"),
        CheckConstraint("status <> 'Failed' OR (error_code IS NOT NULL AND error_message IS NOT NULL)", name="ck_paper_import_failure"),
        Index("ix_paper_imports_course_created", "course_id", "created_at"),
        Index("ix_paper_imports_course_status", "course_id", "status"),
    )
    course_id: Mapped[UUID] = mapped_column(ForeignKey("courses.id", ondelete="RESTRICT"), nullable=False)
    uploaded_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False, unique=True)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[PaperImportStatus] = mapped_column(enum_type(PaperImportStatus, "paper_import_status", length=32), default=PaperImportStatus.UPLOADED, server_default="Uploaded", nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    document: Mapped[Document] = relationship("Document", back_populates="paper_import")
    pages: Mapped[list[SourcePage]] = relationship("SourcePage", back_populates="paper_import", order_by="SourcePage.page_number", passive_deletes="all")
    questions: Mapped[list[ExtractedQuestion]] = relationship("ExtractedQuestion", back_populates="paper_import", passive_deletes="all")

    @property
    def original_file_path(self) -> str | None:
        return self.document.storage_path
