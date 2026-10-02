"""持久原页图；OCR 未执行保持 NULL。"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.models.base import Base, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from backend.app.models.paper_import import PaperImport


class SourcePage(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "source_pages"
    __table_args__ = (
        UniqueConstraint("paper_import_id", "page_number", name="uq_source_pages_import_number"),
        CheckConstraint("page_number >= 1 AND width > 0 AND height > 0", name="ck_source_page_dimensions"),
        CheckConstraint("ocr_confidence IS NULL OR ocr_confidence BETWEEN 0 AND 1", name="ck_source_page_confidence"),
        CheckConstraint("file_metadata IS NULL OR jsonb_typeof(file_metadata) = 'object'", name="ck_source_page_metadata").ddl_if(dialect="postgresql"),
    )
    paper_import_id: Mapped[UUID] = mapped_column(ForeignKey("paper_imports.id", ondelete="RESTRICT"), nullable=False)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    image_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    ocr_text: Mapped[str | None] = mapped_column(Text)
    ocr_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    file_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=func.now(), nullable=False)
    paper_import: Mapped[PaperImport] = relationship("PaperImport", back_populates="pages")

    @property
    def storage_path(self) -> str:
        return self.image_path

    @storage_path.setter
    def storage_path(self, value: str) -> None:
        self.image_path = value

    @property
    def original_filename(self) -> str:
        return f"page-{self.page_number}.png"
