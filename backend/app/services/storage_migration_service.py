"""Copy and verify historical originals before atomically updating every alias."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import inspect, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.core.maintenance import ensure_storage_writable, require_admin_user
from backend.app.models import (
    Course,
    Document,
    Exam,
    ExportFile,
    ExtractedQuestion,
    KnowledgeBase,
    PaperImport,
    QuestionAsset,
    SourcePage,
    Submission,
)
from backend.app.schemas.file_storage import (
    FileErrorDetail,
    FileMetadata,
    MigrationAttempt,
    MigrationRecord,
    OperationReceipt,
)
from backend.app.schemas.storage_maintenance import MigrationItem, MigrationReport
from backend.app.services.file_storage_service import (
    FileStorageError,
    FileStorageService,
    StoredFile,
    _media_type,
)


def fingerprint(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            size += len(block)
            digest.update(block)
    return size, digest.hexdigest()


def copy_verified(source: Path, target: Path) -> tuple[int, str]:
    """Exclusive creation, durable copy and two actual reads; never replace a file."""
    expected = fingerprint(source)
    with source.open("rb") as reader, target.open("xb") as writer:
        while block := reader.read(1024 * 1024):
            writer.write(block)
        writer.flush()
        os.fsync(writer.fileno())
    actual = fingerprint(target)
    if actual != expected or fingerprint(source) != expected:
        raise FileStorageError("FILE_CONTENT_CHANGED", "复制期间原稿变化或副本不一致。")
    return actual


from backend.app.domain.enums import DocumentPurpose
from backend.app.services.file_resources import (
    FileResource,
    StagedAssetResource,
    asset_origin,
    image_owner,
    resource_kind,
)


@dataclass(frozen=True)
class StorageReference:
    resource: FileResource
    file_id: str
    owner: dict[str, str]

    @property
    def locator(self) -> str | None:
        return self.resource.storage_path

    @property
    def metadata(self) -> FileMetadata | None:
        return FileMetadata.model_validate(self.resource.file_metadata) if self.resource.file_metadata is not None else None

    @property
    def legacy(self) -> bool:
        metadata = self.metadata
        return metadata is None or metadata.migration.status not in {"not_required", "migrated"}


def storage_references(session: Session) -> list[StorageReference]:
    """Current implemented resource identities only; E2 must attach real mappings."""
    connection = session.connection()
    schema = session.scalar(text("SELECT current_schema()")) if connection.dialect.name == "postgresql" else None
    inspector = inspect(connection)
    tables = set(inspector.get_table_names(schema=schema))
    if "export_files" not in tables or "file_metadata" not in {c["name"] for c in inspector.get_columns("documents", schema=schema)}:
        raise FileStorageError("MAINTENANCE_SCHEMA_NOT_READY", "请先按既有迁移流程升级数据库；工具不会自动修改 schema。")
    if not {"source_pages", "extracted_questions", "question_assets"}.issubset(tables):
        raise FileStorageError("MAINTENANCE_SCHEMA_NOT_READY", "请先升级完整的导入/题图模型，维护工具不会自动修改 schema。")
    result = []
    for document in session.scalars(select(Document).order_by(Document.id)):
        course = session.get(Course, document.course_id)
        if course is None:
            raise FileStorageError("FILE_REFERENCE_CONFLICT", "文件课程不存在。")
        owner = {"course_id": str(course.id)}
        if document.purpose == DocumentPurpose.KNOWLEDGE_BASE:
            kb = session.get(KnowledgeBase, document.knowledge_base_id)
            if kb is None or kb.course_id != course.id:
                raise FileStorageError("FILE_REFERENCE_CONFLICT", "资料与知识库课程不一致。")
            owner["knowledge_base_id"] = str(kb.id)
        elif document.knowledge_base_id is not None:
            raise FileStorageError("FILE_REFERENCE_CONFLICT", "原试卷不能登记到知识库。")
        result.append(StorageReference(document, "d_" + document.id.hex, owner))
    for export in session.scalars(select(ExportFile).order_by(ExportFile.id)):
        owner = {}
        if export.submission_id is not None:
            submission = session.get(Submission, export.submission_id)
            exam = session.get(Exam, submission.exam_id) if submission else None
            owner["submission_id"] = str(export.submission_id)
        elif export.exam_id is not None:
            exam = session.get(Exam, export.exam_id)
            owner["exam_id"] = str(export.exam_id)
        else:
            exam = None
        course_id = exam.course_id if exam else export.course_id
        course = session.get(Course, course_id) if course_id else None
        if course is None:
            raise FileStorageError("FILE_REFERENCE_CONFLICT", "导出与课程/考试/答卷归属不一致。")
        owner["course_id"] = str(course.id)
        result.append(StorageReference(export, "e_" + export.id.hex, owner))
    try:
        for page in session.scalars(select(SourcePage).order_by(SourcePage.id)):
            result.append(StorageReference(page, "p_" + page.id.hex, image_owner(session, page)))
        staged_ids: set[UUID] = set()
        for extracted in session.scalars(select(ExtractedQuestion).order_by(ExtractedQuestion.id)):
            for entry in extracted.assets or []:
                identity = UUID(entry["id"])
                if identity in staged_ids:
                    raise ValueError("暂存资产身份重复。")
                staged_ids.add(identity)
                staged = StagedAssetResource(extracted, identity)
                formal = session.get(QuestionAsset, identity)
                resource = formal if formal is not None else staged
                owner = image_owner(session, resource)
                result.append(StorageReference(resource, "a_" + identity.hex, owner))
        for asset in session.scalars(select(QuestionAsset).order_by(QuestionAsset.id)):
            if asset.id not in staged_ids:
                asset_origin(session, asset)
                result.append(StorageReference(asset, "a_" + asset.id.hex, image_owner(session, asset)))
    except (ValueError, KeyError, TypeError) as exc:
        raise FileStorageError("FILE_REFERENCE_CONFLICT", "原页或题图持久关联不一致。") from exc
    return result


def physical_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


class StorageMigrationService:
    def __init__(self, session: Session, *, root: Path | None = None):
        self.session = session
        self.files = FileStorageService(session, root=root)

    def _path(self, ref: StorageReference) -> Path:
        return self.files._existing_path(ref.locator, legacy=ref.legacy).resolve()

    def run(self, *, actor_id: UUID) -> MigrationReport:
        ensure_storage_writable(self.files.root)
        require_admin_user(self.session, actor_id)
        started = datetime.now(UTC)
        references = storage_references(self.session)
        groups: dict[str, list[StorageReference]] = {}
        items: list[MigrationItem] = []
        for ref in references:
            try:
                key = physical_key(self._path(ref))
            except FileStorageError as exc:
                if exc.code != "FILE_HISTORY_UNKNOWN":
                    raise
                groups["unknown:" + ref.file_id] = [ref]
            else:
                groups.setdefault(key, []).append(ref)
        for group in groups.values():
            try:
                items.append(self._migrate_group(group, actor_id))
            except (OSError, ValueError, SQLAlchemyError, FileStorageError) as exc:
                self.session.rollback()
                items.append(MigrationItem(file_ids=[r.file_id for r in group],
                    source_locator=group[0].locator, outcome="failed",
                    error=FileErrorDetail(code=exc.code if isinstance(exc, FileStorageError) else "FILE_MIGRATION_FAILED",
                                          message=str(exc) if isinstance(exc, FileStorageError) else type(exc).__name__, stage="inventory")))
        return MigrationReport(started_at=started, completed_at=datetime.now(UTC), items=items)

    def _mark_unavailable(self, group: list[StorageReference], outcome: Literal["missing", "history_unknown"]) -> MigrationItem:
        for ref in group:
            old = ref.metadata
            ref.resource.file_metadata = FileMetadata(
                media_type=old.media_type if old else None, size_bytes=old.size_bytes if old else None,
                sha256=old.sha256 if old else None,
                migration=MigrationRecord(status=outcome),
            ).model_dump(mode="json")
        self.session.commit()
        return MigrationItem(file_ids=[r.file_id for r in group], source_locator=group[0].locator, outcome=outcome)

    def _migrate_group(self, group: list[StorageReference], actor_id: UUID) -> MigrationItem:
        first = group[0]
        if not first.locator:
            return self._mark_unavailable(group, "history_unknown")
        try:
            source = self._path(first)
        except FileStorageError as exc:
            if exc.code == "FILE_HISTORY_UNKNOWN":
                return self._mark_unavailable(group, "history_unknown")
            raise
        self.files.lock_path(source)
        # A writer waiting on the same physical key may have added another alias.
        self.session.expire_all()
        group = []
        for ref in storage_references(self.session):
            try:
                matches = ref.locator and physical_key(self._path(ref)) == physical_key(source)
            except FileStorageError as exc:
                if exc.code == "FILE_HISTORY_UNKNOWN":
                    continue
                raise
            if matches:
                group.append(ref)
        if not group:
            raise FileStorageError("FILE_REFERENCE_CONFLICT", "原定位已变化，请重新盘点。")
        imported_ids: set[UUID] = set()
        extracted_ids: set[UUID] = set()
        for ref in group:
            if isinstance(ref.resource, StagedAssetResource):
                extracted_ids.add(ref.resource.extracted.id)
                imported_ids.add(ref.resource.extracted.paper_import_id)
            elif isinstance(ref.resource, QuestionAsset):
                origin = asset_origin(self.session, ref.resource)
                if origin is not None:
                    extracted_ids.add(origin.extracted.id)
                    imported_ids.add(origin.extracted.paper_import_id)
            elif isinstance(ref.resource, SourcePage):
                imported_ids.add(ref.resource.paper_import_id)
        for owner_model, owner_ids in ((PaperImport, imported_ids), (ExtractedQuestion, extracted_ids)):
            if owner_ids:
                list(self.session.scalars(select(owner_model).where(owner_model.id.in_(owner_ids)).order_by(owner_model.id).with_for_update()))
        for model in (Document, ExportFile, SourcePage, QuestionAsset):
            ids = [ref.resource.id for ref in group if isinstance(ref.resource, model)]
            if ids:
                list(self.session.scalars(select(model).where(model.id.in_(ids)).order_by(model.id).with_for_update()))
        if not source.is_file():
            if any(not ref.legacy for ref in group):
                self.session.rollback()
                return MigrationItem(file_ids=[r.file_id for r in group], source_locator=group[0].locator,
                                     outcome="missing", error=FileErrorDetail(code="FILE_MISSING", message="持久原稿缺失。", stage="verify"))
            return self._mark_unavailable(group, "missing")
        measured = fingerprint(source)
        for ref in group:
            metadata = ref.metadata
            if metadata and ((metadata.size_bytes is not None and metadata.size_bytes != measured[0])
                             or (metadata.sha256 is not None and metadata.sha256 != measured[1])):
                raise FileStorageError("FILE_CONTENT_CHANGED", "原稿与已有可信元数据不一致。")
        if all(not ref.legacy for ref in group):
            for ref in group:
                self.files.resolve_path(cast(str, ref.locator))
            self.session.rollback()
            return MigrationItem(file_ids=[r.file_id for r in group], source_locator=group[0].locator,
                                 target_relative_path=group[0].locator,
                                 outcome="already_migrated" if all(ref.metadata and ref.metadata.migration.status == "migrated" for ref in group) else "native")
        operation_id = uuid4()
        suffix = source.suffix.lower()
        if not suffix or len(suffix) > 16 or not suffix[1:].isalnum():
            suffix = ".bin"
        bucket = "exports" if any(isinstance(ref.resource, ExportFile) for ref in group) else "assets" if any(isinstance(ref.resource, (QuestionAsset, StagedAssetResource)) for ref in group) else "papers" if any(isinstance(ref.resource, SourcePage) or isinstance(ref.resource, Document) and ref.resource.purpose == DocumentPurpose.PAPER_SOURCE for ref in group) else "uploads"
        locator = f"{bucket}/{group[0].resource.id.hex}/{operation_id.hex}{suffix}"
        target = self.files.resolve_path(locator)
        self.files.lock_path(target)
        started = datetime.now(UTC)
        receipts: list[tuple[Path, OperationReceipt]] = []
        attempts = {ref.file_id: MigrationAttempt(operation_id=operation_id, source_locator=cast(str, ref.locator),
                    target_relative_path=locator, started_at=started) for ref in group}
        committed = False
        stage = "copy"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            for ref in group:
                receipt = OperationReceipt(operation_id=operation_id, resource_type=resource_kind(ref.resource),
                    resource_id=ref.resource.id, owner=ref.owner, actor_id=actor_id, candidate_locator=locator,
                    stage="prepared", started_at=started, updated_at=started)
                receipt_path = target.with_name(f"{operation_id.hex}_{ref.resource.id.hex}.receipt.json")
                self.files._write_receipt(receipt_path, receipt, create=True)
                receipts.append((receipt_path, receipt))
            size, digest = copy_verified(source, target)
            verified = datetime.now(UTC)
            media_type = _media_type(source.name, target.read_bytes())
            for path, receipt in receipts:
                receipt.stage = "written"
                receipt.updated_at = verified
                self.files._write_receipt(path, receipt)
            stage = "reference"
            for ref in group:
                attempt = attempts[ref.file_id]
                attempt.verified_at = verified
                attempt.committed_at = datetime.now(UTC)
                ref.resource.storage_path = locator
                ref.resource.file_metadata = FileMetadata(media_type=media_type, size_bytes=size, sha256=digest,
                    migration=MigrationRecord(status="migrated", latest_attempt=attempt)).model_dump(mode="json")
            self.session.commit()
            committed = True
            stage = "receipt"
            for path, _receipt in receipts:
                self.files.commit_receipt(StoredFile(locator, FileMetadata.model_validate(group[0].resource.file_metadata), path),
                                          current_status="migrated")
        except (OSError, ValueError, SQLAlchemyError, FileStorageError) as exc:
            error = FileErrorDetail(code=exc.code if isinstance(exc, FileStorageError) else "FILE_MIGRATION_FAILED",
                                    message=str(exc) if isinstance(exc, FileStorageError) else type(exc).__name__, stage=stage)
            if not committed:
                self.session.rollback()
                for ref in group:
                    attempt = attempts[ref.file_id]
                    attempt.committed_at = None
                    attempt.error = error
                    old = ref.metadata
                    ref.resource.file_metadata = FileMetadata(media_type=old.media_type if old else None,
                        size_bytes=old.size_bytes if old else None, sha256=old.sha256 if old else None,
                        migration=MigrationRecord(status="failed", latest_attempt=attempt)).model_dump(mode="json")
                try:
                    self.session.commit()
                except SQLAlchemyError:
                    self.session.rollback()
                    error.message += "；失败状态也未能提交，原引用保留。"
                for path, receipt in receipts:
                    receipt.stage = "failed"
                    receipt.updated_at = datetime.now(UTC)
                    receipt.error = error
                    try:
                        self.files._write_receipt(path, receipt)
                    except (OSError, FileStorageError):
                        error.message += "；失败收据更新失败，原材料保留。"
            return MigrationItem(file_ids=[ref.file_id for ref in group], source_locator=attempts[group[0].file_id].source_locator,
                                 target_relative_path=locator, operation_id=operation_id, outcome="failed", error=error)
        return MigrationItem(file_ids=[ref.file_id for ref in group], source_locator=attempts[group[0].file_id].source_locator,
                             target_relative_path=locator, operation_id=operation_id, outcome="migrated")
