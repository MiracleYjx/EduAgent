"""题图关系；导入资产的文件登记由原暂存资产投影。"""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.models.base import Base, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from backend.app.models.question import Question
    from backend.app.models.source_page import SourcePage


class QuestionAsset(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "question_assets"
    __table_args__ = (
        UniqueConstraint("question_id", "order_index", name="uq_question_assets_order"),
        CheckConstraint("asset_type IN ('figure', 'table', 'diagram')", name="ck_question_asset_type"),
        CheckConstraint("width > 0 AND height > 0", name="ck_question_asset_dimensions"),
        CheckConstraint("order_index IS NULL OR order_index BETWEEN 1 AND 5", name="ck_question_asset_order"),
        CheckConstraint("region IS NULL OR source_page_id IS NOT NULL", name="ck_question_asset_region_page"),
        CheckConstraint("region IS NULL OR jsonb_typeof(region) = 'object'", name="ck_question_asset_region").ddl_if(dialect="postgresql"),
        CheckConstraint("file_metadata IS NULL OR jsonb_typeof(file_metadata) = 'object'", name="ck_question_asset_metadata").ddl_if(dialect="postgresql"),
    )
    question_id: Mapped[UUID] = mapped_column(ForeignKey("questions.id", ondelete="RESTRICT"), nullable=False, index=True)
    asset_type: Mapped[str] = mapped_column(String(16), nullable=False)
    _file_path: Mapped[str | None] = mapped_column("file_path", String(1024))
    _file_metadata: Mapped[dict[str, Any] | None] = mapped_column("file_metadata", JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql"))
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    caption: Mapped[str | None] = mapped_column(Text)
    student_visible: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    source_page_id: Mapped[UUID | None] = mapped_column(ForeignKey("source_pages.id", ondelete="RESTRICT"), index=True)
    region: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql"))
    order_index: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=func.now(), nullable=False)
    question: Mapped[Question] = relationship("Question", back_populates="assets")
    source_page: Mapped[SourcePage | None] = relationship("SourcePage")

    @property
    def imported_asset(self) -> dict[str, Any] | None:
        if self.question is None:
            return None
        extracted = self.question.imported_extracted_question
        if extracted is None:
            return None
        matches = [asset for asset in extracted.assets or [] if asset.get("id") == str(self.id)]
        if len(matches) > 1:
            raise ValueError("导入资产身份冲突。")
        return matches[0] if matches else None

    @property
    def file_path(self) -> str | None:
        origin = self.imported_asset
        if origin is not None:
            return origin["file_meta"]["storage_path"]
        return self._file_path

    @file_path.setter
    def file_path(self, value: str | None) -> None:
        if self.imported_asset is not None:
            raise ValueError("导入文件路径由原暂存记录投影，不能独立修改。")
        self._file_path = value

    @property
    def file_metadata(self) -> dict[str, Any] | None:
        origin = self.imported_asset
        if origin is not None:
            return {key: value for key, value in origin["file_meta"].items() if key != "storage_path"}
        return self._file_metadata

    @file_metadata.setter
    def file_metadata(self, value: dict[str, Any] | None) -> None:
        origin = self.imported_asset
        if origin is None:
            self._file_metadata = value
        else:
            self._replace_origin_meta((value or {}) | {"storage_path": origin["file_meta"]["storage_path"]})

    def _replace_origin_meta(self, value: dict[str, Any]) -> None:
        extracted = self.question.imported_extracted_question
        if extracted is None:
            raise ValueError("导入资产来源不存在。")
        entries = deepcopy(extracted.assets)
        if entries is None:
            raise ValueError("导入资产来源不存在。")
        for entry in entries:
            if entry["id"] == str(self.id):
                entry["file_meta"] = value
        extracted.assets = entries

    @property
    def storage_path(self) -> str | None:
        return self.file_path

    @storage_path.setter
    def storage_path(self, value: str | None) -> None:
        origin = self.imported_asset
        if origin is None:
            self._file_path = value
        else:
            self._replace_origin_meta(origin["file_meta"] | {"storage_path": value})

    @property
    def file_id(self) -> str:
        return "a_" + self.id.hex

    @property
    def original_filename(self) -> str:
        return f"asset-{self.id.hex}.png"
