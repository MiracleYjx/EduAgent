"""题图校正请求及服务端文件登记；公开响应不泄露定位。"""
from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from backend.app.schemas.file_storage import FileMetadata
from backend.app.schemas.paper_import import PixelRegion, StagedAsset


class AssetLinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_id: str = Field(min_length=1)
    asset_type: Literal["figure", "table", "diagram"]
    source_page_id: UUID | None = None
    region: PixelRegion | None = None
    caption: str | None = None
    student_visible: StrictBool = False


class AssetVisibilityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    student_visible: StrictBool


class StagedFileMetadata(FileMetadata):
    storage_path: str = Field(min_length=1, max_length=1024)


class StoredStagedAsset(StagedAsset):
    id: UUID
    file_meta: StagedFileMetadata


class QuestionAssetView(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)
    id: UUID
    question_id: UUID
    file_id: str
    asset_type: Literal["figure", "table", "diagram"]
    width: int
    height: int
    caption: str | None
    source_page_id: UUID | None
    region: PixelRegion | None
    order_index: int | None
    student_visible: bool = False
