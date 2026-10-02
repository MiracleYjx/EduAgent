"""唯一的持久文件资源枚举，供读取、迁移、备份和删除共用。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import cast, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from backend.app.domain.enums import DocumentPurpose, ExtractedQuestionStatus
from backend.app.models import (
    Course,
    Document,
    ExportFile,
    ExtractedQuestion,
    PaperImport,
    QuestionAsset,
    SourcePage,
)
from backend.app.schemas.paper_import import PixelRegion
from backend.app.schemas.question_assets import StoredStagedAsset


@dataclass
class StagedAssetResource:
    extracted: ExtractedQuestion
    id: UUID

    @property
    def data(self) -> StoredStagedAsset:
        matches = [entry for entry in self.extracted.assets or [] if entry.get("id") == str(self.id)]
        if len(matches) != 1:
            raise ValueError("暂存资产身份冲突。")
        value = StoredStagedAsset.model_validate(matches[0])
        if value.file_id != "a_" + self.id.hex:
            raise ValueError("暂存文件身份与资源不一致。")
        return value

    @property
    def storage_path(self) -> str:
        return self.data.file_meta.storage_path

    @storage_path.setter
    def storage_path(self, value: str) -> None:
        self.replace_metadata(self.file_metadata | {"storage_path": value})

    @property
    def file_metadata(self) -> dict:
        return self.data.file_meta.model_dump(mode="json", exclude={"storage_path"})

    @file_metadata.setter
    def file_metadata(self, value: dict) -> None:
        self.replace_metadata(value | {"storage_path": self.storage_path})

    def replace_metadata(self, value: dict) -> None:
        entries = deepcopy(self.extracted.assets)
        assert entries is not None
        for entry in entries:
            if entry["id"] == str(self.id):
                entry["file_meta"] = value
        self.extracted.assets = entries

    @property
    def original_filename(self) -> str:
        return f"asset-{self.id.hex}.png"


def staged_matches(session: Session, asset_id: UUID) -> list[StagedAssetResource]:
    query = select(ExtractedQuestion)
    if session.get_bind().dialect.name == "postgresql":
        query = query.where(cast(ExtractedQuestion.assets, JSONB).contains([{"id": str(asset_id)}]))
    result = []
    for extracted in session.scalars(query):
        for entry in extracted.assets or []:
            if entry.get("id") == str(asset_id):
                result.append(StagedAssetResource(extracted, asset_id))
    return result


def asset_origin(session: Session, asset: QuestionAsset) -> StagedAssetResource | None:
    matches = staged_matches(session, asset.id)
    if len(matches) > 1:
        raise ValueError("多个暂存记录使用同一资产身份。")
    if not matches:
        if asset._file_path is None:
            raise ValueError("正式资产没有可靠文件登记。")
        return None
    origin = matches[0]
    if origin.extracted.question_id != asset.question_id or origin.extracted.status != ExtractedQuestionStatus.CORRECTED:
        raise ValueError("暂存与正式资产没有真实确认关联。")
    if asset._file_path is not None or asset._file_metadata is not None:
        raise ValueError("导入正式资产不得另存可分叉定位。")
    _validated = origin.data
    return origin


FileResourceType = Literal["document", "source_page", "staged_asset", "question_asset", "export"]


def resource_kind(resource: object) -> FileResourceType:
    if isinstance(resource, Document):
        return "document"
    if isinstance(resource, SourcePage):
        return "source_page"
    if isinstance(resource, StagedAssetResource):
        return "staged_asset"
    if isinstance(resource, QuestionAsset):
        return "question_asset"
    return "export"


def paper_owner(session: Session, imported: PaperImport) -> dict[str, str]:
    doc = imported.document
    course = session.get(Course, imported.course_id)
    if course is None or doc.course_id != imported.course_id or doc.purpose != DocumentPurpose.PAPER_SOURCE or doc.knowledge_base_id is not None:
        raise ValueError("导入、原文件及课程关系不一致。")
    return {"course_id": str(course.id), "paper_import_id": str(imported.id)}


def image_owner(session: Session, resource: SourcePage | StagedAssetResource | QuestionAsset) -> dict[str, str]:
    if isinstance(resource, SourcePage):
        return paper_owner(session, resource.paper_import)
    if isinstance(resource, StagedAssetResource):
        extracted = resource.extracted
        data = resource.data
        page = session.get(SourcePage, data.source_page_id)
        if page is None or page.paper_import_id != extracted.paper_import_id or str(page.id) not in extracted.source_page_ids:
            raise ValueError("暂存图不是同导入的真实来源页。")
        if data.region:
            data.region.within(page.width, page.height)
        return paper_owner(session, extracted.paper_import) | {"extracted_question_id": str(extracted.id), "source_page_id": str(page.id)}
    course = session.get(Course, resource.question.course_id)
    if course is None:
        raise ValueError("正式题图课程不存在。")
    owner = {"course_id": str(course.id), "question_id": str(resource.question_id)}
    origin = asset_origin(session, resource)
    if origin:
        owner |= image_owner(session, origin)
    elif resource.source_page_id is not None:
        page = session.get(SourcePage, resource.source_page_id)
        if page is None or page.paper_import.course_id != course.id:
            raise ValueError("正式题图来源页课程不一致。")
        if resource.region is not None:
            PixelRegion.model_validate(resource.region).within(page.width, page.height)
        imported_question = resource.question.imported_extracted_question
        if imported_question is not None and (page.paper_import_id != imported_question.paper_import_id or str(page.id) not in imported_question.source_page_ids):
            raise ValueError("原题正式资产须保持同导入页来源。")
        owner |= paper_owner(session, page.paper_import) | {"source_page_id": str(page.id)}
    return owner


type FileResource = Document | ExportFile | SourcePage | StagedAssetResource | QuestionAsset
