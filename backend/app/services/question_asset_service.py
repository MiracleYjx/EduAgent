"""教师题图关联及可靠落盘；解析/整批确认/Vision 另由后续任务接入。"""
from __future__ import annotations

import hashlib
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.domain.enums import (
    ExamStatus,
    ExtractedQuestionStatus,
    PaperImportStatus,
    QuestionStatus,
)
from backend.app.models import (
    ExtractedQuestion,
    PaperImport,
    Question,
    QuestionAsset,
    SourcePage,
    Submission,
)
from backend.app.schemas.image_assessment import advance_image_context
from backend.app.schemas.paper_import import PixelRegion, StagedAsset
from backend.app.schemas.question_assets import (
    AssetLinkRequest,
    QuestionAssetView,
    StoredStagedAsset,
)
from backend.app.services.file_resources import (
    FileResourceType,
    StagedAssetResource,
    image_owner,
)
from backend.app.services.file_storage_service import (
    FileStorageError,
    FileStorageService,
    StoredFile,
)


def actual_image(content: bytes) -> Image.Image:
    try:
        with Image.open(BytesIO(content)) as image:
            if image.format not in {"PNG", "JPEG"}:
                raise ValueError("仅支持可靠 PNG/JPEG 原图。")
            image.load()
            return image.copy()
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        raise FileStorageError("IMAGE_INVALID", "题图无法解码为可靠 PNG/JPEG。", http_status=422) from exc


class QuestionAssetService:
    def __init__(self, session: Session, *, root: Path | None = None):
        self.session = session
        self.files = FileStorageService(session, root=root)

    def _teacher(self, course_id: UUID, actor_id: UUID) -> None:
        actor = self.files._actor(actor_id)
        if not self.files._manages(actor, self.files._course(course_id)):
            raise FileStorageError("FILE_FORBIDDEN", "仅所属课程教师可校正题图。", http_status=403)

    def _staged(self, identity: UUID, actor_id: UUID, *, writing: bool = False) -> ExtractedQuestion:
        record = self.session.get(ExtractedQuestion, identity)
        if record is None:
            raise FileStorageError("FILE_NOT_FOUND", "暂存题不存在。", http_status=404)
        self._teacher(record.paper_import.course_id, actor_id)
        self.session.scalar(select(PaperImport).where(PaperImport.id == record.paper_import_id).with_for_update())
        record = self.session.scalars(select(ExtractedQuestion).where(ExtractedQuestion.id == identity).with_for_update().execution_options(populate_existing=True)).one()
        if writing and (record.status != ExtractedQuestionStatus.PENDING_CORRECTION or record.paper_import.status != PaperImportStatus.PENDING_REVIEW):
            raise FileStorageError("PAPER_STATE_CONFLICT", "仅待校正题接受题图修改。", current_status=record.status.value)
        return record

    def _question(self, identity: UUID, actor_id: UUID, *, writing: bool = False) -> Question:
        record = self.session.get(Question, identity)
        if record is None:
            raise FileStorageError("FILE_NOT_FOUND", "正式题不存在。", http_status=404)
        self._teacher(record.course_id, actor_id)
        record = self.session.scalars(select(Question).where(Question.id == identity).with_for_update().execution_options(populate_existing=True)).one()
        if writing:
            if any(exam.status in {ExamStatus.PUBLISHED, ExamStatus.CLOSED, ExamStatus.ARCHIVED} or self.session.scalar(select(Submission.id).where(Submission.exam_id == exam.id).limit(1)) is not None for exam in record.exams):
                raise FileStorageError("QUESTION_PUBLISHED_IMMUTABLE", "发布或历史引用保护期间不能修改题图。", current_status=record.status.value)
            if record.status in {QuestionStatus.APPROVED, QuestionStatus.PUBLISHED}:
                raise FileStorageError("QUESTION_APPROVED_IMMUTABLE", "已审核题须先合法退回修订再修改题图。", current_status=record.status.value)
        return record

    def _source(self, payload: AssetLinkRequest, actor_id: UUID) -> tuple[bytes, Image.Image, str]:
        path, _view = self.files.download(payload.file_id, actor_id=actor_id)
        self.files.lock_path(path)
        data = path.read_bytes()
        resource = self.files._resource(payload.file_id)
        metadata = resource.file_metadata
        if metadata and metadata.get("sha256") is not None and hashlib.sha256(data).hexdigest() != metadata["sha256"]:
            raise FileStorageError("FILE_CONTENT_CHANGED", "原图与登记内容不一致。")
        image = actual_image(data)
        if isinstance(resource, SourcePage):
            if resource.id != payload.source_page_id or image.size != (resource.width, resource.height):
                raise FileStorageError("FILE_REFERENCE_CONFLICT", "原页身份或实际尺寸不一致。")
            if payload.region:
                try:
                    payload.region.within(resource.width, resource.height)
                    if any(int(value) != value for value in payload.region.bbox):
                        raise ValueError("裁图边界须为完整像素。")
                except ValueError as exc:
                    raise FileStorageError("IMAGE_REGION_INVALID", str(exc), http_status=422) from exc
                x0, y0, x1, y1 = payload.region.bbox
                cropped = image.crop((int(x0), int(y0), int(x1), int(y1)))
                stream = BytesIO()
                cropped.save(stream, format="PNG")
                return stream.getvalue(), cropped, "crop"
            return data, image, "link"
        page_id: UUID | None
        if isinstance(resource, StagedAssetResource):
            origin = resource.data
            page_id, region = origin.source_page_id, origin.region
        elif isinstance(resource, QuestionAsset):
            page_id, region = resource.source_page_id, PixelRegion.model_validate(resource.region) if resource.region else None
        else:
            raise FileStorageError("FILE_REFERENCE_CONFLICT", "仅允许登记的原页或题图作为图像来源。")
        if page_id != payload.source_page_id or region != payload.region:
            raise FileStorageError("FILE_REFERENCE_CONFLICT", "复用原图不得伪改原页或像素定位；新裁图须从原页建立。")
        return data, image, "link"

    def _store_or_link(self, *, identity: UUID, kind: FileResourceType, owner: dict[str, str], payload: AssetLinkRequest, actor_id: UUID, content: bytes, operation: str) -> StoredFile:
        if operation == "crop":
            return self.files._store_bytes(resource_type=kind, resource_id=identity, owner=owner, actor_id=actor_id, filename=f"{identity.hex}.png", content=content, bucket="assets")
        resource = self.files._resource(payload.file_id)
        locator = resource.storage_path
        if locator is None or resource.file_metadata is None or self.files._legacy_metadata(resource.file_metadata):
            raise FileStorageError("FILE_NOT_MIGRATED", "新图关联须使用已登记持久原图。")
        from datetime import UTC, datetime

        from backend.app.schemas.file_storage import FileMetadata, OperationReceipt
        path = self.files.resolve_path(locator)
        operation_id = uuid4()
        now = datetime.now(UTC)
        receipt = OperationReceipt(operation_id=operation_id, resource_type=kind, resource_id=identity, owner=owner, actor_id=actor_id, candidate_locator=locator, stage="written", started_at=now, updated_at=now)
        receipt_path = path.with_name(f"{operation_id.hex}_{identity.hex}.receipt.json")
        self.files._write_receipt(receipt_path, receipt, create=True)
        return StoredFile(locator, FileMetadata.model_validate(resource.file_metadata), receipt_path)

    def _commit(self, stored: StoredFile | None = None) -> None:
        try:
            self.session.commit()
        except SQLAlchemyError as exc:
            self.session.rollback()
            if stored is not None:
                self.files.fail_receipt(stored, code="ASSET_PERSISTENCE_FAILED", message="题图引用提交失败，原材料保留。")
            raise FileStorageError("ASSET_PERSISTENCE_FAILED", "题图引用提交失败，原材料保留。", http_status=503) from exc
        if stored is not None:
            self.files.commit_receipt(stored, current_status="stored")

    def create_source_page(self, paper_import_id: UUID, *, page_number: int, content: bytes, actor_id: UUID) -> SourcePage:
        imported = self.session.get(PaperImport, paper_import_id)
        if imported is None:
            raise FileStorageError("FILE_NOT_FOUND", "导入不存在。", http_status=404)
        self._teacher(imported.course_id, actor_id)
        imported = self.session.scalars(select(PaperImport).where(PaperImport.id == paper_import_id).with_for_update()).one()
        if imported.status != PaperImportStatus.PARSING:
            raise FileStorageError("PAPER_STATE_CONFLICT", "原页只能在本次解析阶段建立。")
        if isinstance(page_number, bool) or page_number < 1 or imported.page_count is None or page_number > imported.page_count:
            raise FileStorageError("IMAGE_PAGE_INVALID", "页号须在已确认的原页范围内。", http_status=422)
        image = actual_image(content)
        page = SourcePage(id=uuid4(), paper_import_id=imported.id, page_number=page_number, width=image.width, height=image.height)
        stored = self.files._store_bytes(resource_type="source_page", resource_id=page.id, owner={"course_id": str(imported.course_id), "paper_import_id": str(imported.id)}, actor_id=actor_id, filename=f"page-{page_number}.png", content=content, bucket="papers")
        page.image_path, page.file_metadata = stored.storage_path, stored.metadata.model_dump(mode="json")
        self.session.add(page)
        self._commit(stored)
        return page

    def list_staged(self, extracted_id: UUID, *, actor_id: UUID) -> list[StagedAsset]:
        record = self._staged(extracted_id, actor_id)
        return [StagedAsset.model_validate({key: value for key, value in entry.items() if key != "file_meta"}) for entry in record.assets or []]

    def create_staged(self, extracted_id: UUID, payload: AssetLinkRequest, *, actor_id: UUID) -> StagedAsset:
        content, _image, operation = self._source(payload, actor_id)
        record = self._staged(extracted_id, actor_id, writing=True)
        page = self.session.get(SourcePage, payload.source_page_id) if payload.source_page_id else None
        if page is None or page.paper_import_id != record.paper_import_id or str(page.id) not in record.source_page_ids:
            raise FileStorageError("FILE_REFERENCE_CONFLICT", "原题题图须来自同导入的真实已关联原页。")
        entries = deepcopy(record.assets) or []
        if len(entries) >= 5:
            raise FileStorageError("QUESTION_ASSET_LIMIT", "每题最多关联五图。", http_status=422)
        identity = uuid4()
        owner = {"course_id": str(record.paper_import.course_id), "paper_import_id": str(record.paper_import_id), "extracted_question_id": str(record.id), "source_page_id": str(page.id)}
        next_assessment = advance_image_context(record.image_assessment)
        stored = self._store_or_link(identity=identity, kind="staged_asset", owner=owner, payload=payload, actor_id=actor_id, content=content, operation=operation)
        public = StagedAsset(id=identity, file_id="a_" + identity.hex, asset_type=payload.asset_type, source_page_id=page.id, region=payload.region, caption=payload.caption)
        entry = public.model_dump(mode="json") | {"file_meta": stored.metadata.model_dump(mode="json") | {"storage_path": stored.storage_path}}
        StoredStagedAsset.model_validate(entry)
        record.assets = [*entries, entry]
        record.image_assessment = next_assessment
        self._commit(stored)
        return public

    def replace_staged(self, extracted_id: UUID, assets: list[StagedAsset], *, actor_id: UUID) -> list[StagedAsset]:
        record = self._staged(extracted_id, actor_id, writing=True)
        if len(assets) > 5 or len({asset.id for asset in assets}) != len(assets):
            raise FileStorageError("QUESTION_ASSET_LIMIT", "资产身份須唯一且最多五图。", http_status=422)
        existing = {UUID(entry["id"]): entry for entry in record.assets or []}
        entries = []
        for asset in assets:
            original = existing.get(asset.id) if asset.id else None
            if original is None or asset.id is None:
                raise FileStorageError("FILE_REFERENCE_CONFLICT", "不能借用其他题的暂存身份。")
            for field in ("file_id", "source_page_id", "region"):
                if asset.model_dump(mode="json")[field] != original[field]:
                    raise FileStorageError("FILE_IDENTITY_IN_USE", "原图/定位变更须建立新资产身份。")
            image_owner(self.session, StagedAssetResource(record, asset.id))
            entries.append(asset.model_dump(mode="json") | {"file_meta": original["file_meta"]})
        if record.assets != entries:
            record.image_assessment = advance_image_context(record.image_assessment)
            record.assets = entries
        self._commit()
        return self.list_staged(extracted_id, actor_id=actor_id)

    def remove_staged(self, extracted_id: UUID, asset_id: UUID, *, actor_id: UUID) -> None:
        current = self.list_staged(extracted_id, actor_id=actor_id)
        if not any(asset.id == asset_id for asset in current):
            raise FileStorageError("FILE_NOT_FOUND", "题图关联不存在。", http_status=404)
        self.replace_staged(extracted_id, [asset for asset in current if asset.id != asset_id], actor_id=actor_id)

    def list_question(self, question_id: UUID | str, *, actor_id: UUID) -> list[QuestionAssetView]:
        question = self._question(UUID(str(question_id)), actor_id)
        return [QuestionAssetView.model_validate(asset, from_attributes=True).model_copy(update={"file_id": "a_" + asset.id.hex}) for asset in question.assets]

    def link_question(self, question_id: UUID | str, payload: AssetLinkRequest, *, actor_id: UUID) -> QuestionAssetView:
        content, image, operation = self._source(payload, actor_id)
        question = self._question(UUID(str(question_id)), actor_id, writing=True)
        if len(question.assets) >= 5:
            raise FileStorageError("QUESTION_ASSET_LIMIT", "每题最多关联五图。", http_status=422)
        if payload.source_page_id is not None:
            page = self.session.get(SourcePage, payload.source_page_id)
            if page is None or page.paper_import.course_id != question.course_id:
                raise FileStorageError("FILE_REFERENCE_CONFLICT", "原页与题目课程不一致。")
        if payload.source_page_id is not None and question.imported_extracted_question is not None:
            original = question.imported_extracted_question
            if page is None or page.paper_import_id != original.paper_import_id or str(page.id) not in original.source_page_ids:
                raise FileStorageError("FILE_REFERENCE_CONFLICT", "原题题图须保持同导入页来源。")
        identity = uuid4()
        next_assessment = advance_image_context(question.image_assessment)
        asset = QuestionAsset(id=identity, question=question, asset_type=payload.asset_type, width=image.width, height=image.height, source_page_id=payload.source_page_id, region=payload.region.model_dump(mode="json") if payload.region else None, caption=payload.caption, order_index=len(question.assets) + 1)
        owner = {"course_id": str(question.course_id), "question_id": str(question.id)}
        if payload.source_page_id is not None:
            assert page is not None
            owner |= {"paper_import_id": str(page.paper_import_id), "source_page_id": str(page.id)}
        stored = self._store_or_link(identity=identity, kind="question_asset", owner=owner, payload=payload, actor_id=actor_id, content=content, operation=operation)
        asset._file_path, asset._file_metadata = stored.storage_path, stored.metadata.model_dump(mode="json")
        self.session.add(asset)
        question.image_assessment = next_assessment
        self._commit(stored)
        return QuestionAssetView(id=asset.id, question_id=question.id, file_id="a_" + asset.id.hex, asset_type=payload.asset_type, width=asset.width, height=asset.height, caption=asset.caption, source_page_id=asset.source_page_id, region=payload.region, order_index=asset.order_index)

    def remove_question(self, question_id: UUID | str, asset_id: UUID, *, actor_id: UUID) -> None:
        question = self._question(UUID(str(question_id)), actor_id, writing=True)
        asset = next((asset for asset in question.assets if asset.id == asset_id), None)
        if asset is None:
            raise FileStorageError("FILE_NOT_FOUND", "题图关联不存在。", http_status=404)
        remaining = [entry for entry in question.assets if entry.id != asset.id]
        question.image_assessment = advance_image_context(question.image_assessment)
        self.session.delete(asset)
        self.session.flush()
        # Release old unique slots before assigning continuous new order.
        for entry in remaining:
            entry.order_index = None
        self.session.flush()
        for index, entry in enumerate(remaining, 1):
            entry.order_index = index
        self._commit()


    def upload_question(self, question_id: UUID | str, *, content: bytes, asset_type: str, caption: str | None, actor_id: UUID) -> QuestionAssetView:
        question = self._question(UUID(str(question_id)), actor_id, writing=True)
        if len(question.assets) >= 5:
            raise FileStorageError("QUESTION_ASSET_LIMIT", "每题最多关联五图。", http_status=422)
        if asset_type not in {"figure", "table", "diagram"}:
            raise FileStorageError("IMAGE_TYPE_INVALID", "题图类型无效。", http_status=422)
        image = actual_image(content)
        identity = uuid4()
        next_assessment = advance_image_context(question.image_assessment)
        stored = self.files._store_bytes(resource_type="question_asset", resource_id=identity, owner={"course_id": str(question.course_id), "question_id": str(question.id)}, actor_id=actor_id, filename=f"{identity.hex}.png", content=content, bucket="assets")
        asset = QuestionAsset(id=identity, question=question, asset_type=asset_type, width=image.width, height=image.height, caption=caption, order_index=len(question.assets) + 1, _file_path=stored.storage_path, _file_metadata=stored.metadata.model_dump(mode="json"))
        self.session.add(asset)
        question.image_assessment = next_assessment
        self._commit(stored)
        return QuestionAssetView.model_validate(asset)

    def reorder_question(self, question_id: UUID | str, asset_ids: list[UUID], *, actor_id: UUID) -> list[QuestionAssetView]:
        question = self._question(UUID(str(question_id)), actor_id, writing=True)
        existing = {asset.id: asset for asset in question.assets}
        if len(asset_ids) != len(set(asset_ids)) or set(existing) != set(asset_ids):
            raise FileStorageError("FILE_REFERENCE_CONFLICT", "排序须恰好包含本题全部资产一次。")
        if [asset.id for asset in question.assets] != asset_ids:
            question.image_assessment = advance_image_context(question.image_assessment)
            for asset in existing.values():
                asset.order_index = None
            self.session.flush()
            for index, identity in enumerate(asset_ids, 1):
                existing[identity].order_index = index
        self._commit()
        return self.list_question(question.id, actor_id=actor_id)
