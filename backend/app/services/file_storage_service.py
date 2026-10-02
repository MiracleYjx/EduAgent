"""Persistent owned files; SQL resources remain the identity/authorization source."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal, NoReturn, cast
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.core.config import get_settings
from backend.app.core.maintenance import ensure_storage_writable
from backend.app.core.security import get_user_roles
from backend.app.domain.enums import UserRole
from backend.app.models import (
    Course,
    Document,
    Exam,
    ExportFile,
    QuestionSourceChunk,
    Submission,
    User,
)
from backend.app.schemas.file_storage import (
    FileErrorDetail,
    FileMetadata,
    ManagedFileView,
    MigrationRecord,
    OperationReceipt,
)


class FileStorageError(RuntimeError):
    def __init__(self, code: str, message: str, *, http_status: int = 409, current_status: str | None = None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.current_status = current_status


@dataclass(frozen=True)
class StoredFile:
    storage_path: str
    metadata: FileMetadata
    receipt_path: Path


def _media_type(filename: str, data: bytes) -> str:
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return "application/octet-stream"
    suffix = Path(filename).suffix.lower()
    if suffix == ".json":
        try:
            json.loads(data)
        except ValueError:
            return "text/plain"
        return "application/json"
    return "text/csv" if suffix == ".csv" else "text/plain"


class FileStorageService:
    def __init__(self, session: Session, *, root: Path | None = None):
        self.session = session
        self._root = root.resolve() if root is not None else None

    @property
    def root(self) -> Path:
        if self._root is None:
            self._root = get_settings().storage_root.resolve()
        return self._root

    def resolve_path(self, locator: str) -> Path:
        """Resolve a canonical new relative locator; never search alternate roots."""
        if (
            not locator or len(locator) > 1024 or "\\" in locator
            or PureWindowsPath(locator).drive or PurePosixPath(locator).is_absolute()
            or any(part in {"", ".", ".."} for part in locator.split("/"))
        ):
            raise FileStorageError("FILE_INVALID_PATH", "文件定位必须是持久根内规范相对路径。", http_status=422)
        path = (self.root / locator).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise FileStorageError("FILE_INVALID_PATH", "文件定位越出持久根。", http_status=422)
        return path

    def _existing_path(self, locator: str | None, *, legacy: bool = False) -> Path:
        if not locator:
            raise FileStorageError("FILE_HISTORY_UNKNOWN", "历史文件定位未知，需核对原材料。", http_status=404)
        # Exact historical absolute paths remain readable until explicit T150 migration.
        if Path(locator).is_absolute():
            return Path(locator)
        if PureWindowsPath(locator).drive or PurePosixPath(locator).is_absolute():
            raise FileStorageError("FILE_HISTORY_UNKNOWN", "当前主机无法核对历史文件定位。", http_status=404)
        return Path(locator) if legacy else self.resolve_path(locator)

    @staticmethod
    def _legacy_metadata(metadata: dict | None) -> bool:
        return metadata is None or FileMetadata.model_validate(metadata).migration.status not in {"not_required", "migrated"}

    def lock_locator(self, locator: str) -> None:
        self.lock_path(self.resolve_path(locator))

    def lock_path(self, path: Path) -> None:
        """Use the same physical key for legacy migration and new references."""
        path = path.resolve()
        if self.session.get_bind().dialect.name == "postgresql":
            key = int.from_bytes(hashlib.sha256(os.path.normcase(str(path)).encode()).digest()[:8], "big", signed=True)
            self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})

    def _actor(self, actor_id: UUID) -> User:
        actor = self.session.get(User, actor_id)
        if actor is None or not actor.is_active:
            raise FileStorageError("FILE_FORBIDDEN", "无有效文件访问身份。", http_status=403)
        return actor

    def _course(self, course_id: UUID) -> Course:
        course = self.session.get(Course, course_id)
        if course is None:
            raise FileStorageError("FILE_NOT_FOUND", "文件所属课程不存在。", http_status=404)
        return course

    @staticmethod
    def _manages(actor: User, course: Course) -> bool:
        return UserRole.TEACHER in get_user_roles(actor) and actor.id == course.created_by

    def _authorize(self, resource: Document | ExportFile, actor_id: UUID) -> Course:
        actor = self._actor(actor_id)
        if isinstance(resource, Document):
            course = self._course(resource.course_id)
            # Current knowledge-base permission is teacher management; no student
            # material-opening relation exists yet. E5 must supply the real grant.
            allowed = self._manages(actor, course)
        else:
            if resource.course_id is not None:
                course = self._course(resource.course_id)
                submission = None
            else:
                submission = self.session.get(Submission, resource.submission_id) if resource.submission_id else None
                exam_id = submission.exam_id if submission else resource.exam_id
                exam = self.session.get(Exam, exam_id) if exam_id else None
                if exam is None:
                    raise FileStorageError("FILE_REFERENCE_CONFLICT", "导出归属关系不完整。")
                course = self._course(exam.course_id)
            allowed = self._manages(actor, course) or (
                resource.audience == "submission_owner" and submission is not None
                and UserRole.STUDENT in get_user_roles(actor)
                and actor.id == submission.student_id
            )
        if not allowed:
            raise FileStorageError("FILE_FORBIDDEN", "无权访问该资源的文件。", http_status=403)
        return course

    def _resource(self, file_id: str) -> Document | ExportFile:
        prefix, _, raw_id = file_id.partition("_")
        try:
            resource_id = UUID(hex=raw_id)
        except ValueError:
            raise FileStorageError("FILE_NOT_FOUND", "文件资源不存在。", http_status=404) from None
        if raw_id != resource_id.hex:
            raise FileStorageError("FILE_NOT_FOUND", "文件资源不存在。", http_status=404)
        models: dict[str, type[Document | ExportFile]] = {"d": Document, "e": ExportFile}
        model = models.get(prefix)
        # SourcePage/staged/QuestionAsset are attached in E2 when actual models exist.
        resource = self.session.get(model, resource_id) if model is not None else None
        if resource is None:
            raise FileStorageError("FILE_NOT_FOUND", "文件资源不存在。", http_status=404)
        return cast(Document | ExportFile, resource)

    def get_view(self, file_id: str, *, actor_id: UUID) -> ManagedFileView:
        resource = self._resource(file_id)
        course = self._authorize(resource, actor_id)
        try:
            metadata = FileMetadata.model_validate(resource.file_metadata) if resource.file_metadata is not None else None
        except ValidationError as exc:
            raise FileStorageError("FILE_METADATA_INVALID", "文件元数据无效，需核对持久记录。") from exc
        locator = resource.storage_path
        availability: Literal["available", "missing", "history_unknown"]
        if locator is None:
            availability = "history_unknown"
        else:
            try:
                availability = "available" if self._existing_path(locator, legacy=self._legacy_metadata(resource.file_metadata)).is_file() else "missing"
            except FileStorageError as exc:
                if exc.code != "FILE_HISTORY_UNKNOWN":
                    raise
                availability = "history_unknown"
        migration = metadata.migration.status if metadata else (
            "history_unknown" if availability == "history_unknown" else "missing" if availability == "missing" else "not_migrated"
        )
        return ManagedFileView(
            file_id=file_id, resource_type="document" if isinstance(resource, Document) else "export",
            resource_id=resource.id, course_id=course.id,
            original_filename=resource.original_filename,
            media_type=metadata.media_type if metadata else None,
            size_bytes=metadata.size_bytes if metadata else None,
            availability=availability, migration_status=migration,
        )

    def download(self, file_id: str, *, actor_id: UUID) -> tuple[Path, ManagedFileView]:
        resource = self._resource(file_id)
        self._authorize(resource, actor_id)
        if isinstance(resource, ExportFile) and resource.status != "ready":
            raise FileStorageError("FILE_NOT_READY", "导出尚未完成，保留真实生成状态。", current_status=resource.status)
        view = self.get_view(file_id, actor_id=actor_id)
        path = self._existing_path(resource.storage_path, legacy=self._legacy_metadata(resource.file_metadata))
        if not path.is_file():
            state = resource.status.value if isinstance(resource, Document) else resource.status
            raise FileStorageError("FILE_MISSING", "已登记文件缺失，请核对原材料。", http_status=404, current_status=state)
        return path, view


    def registered_file_metadata(self, locator: str, *, actor_id: UUID) -> FileMetadata:
        """Link existing authorized bytes under the same transaction locator lock."""
        path = self.resolve_path(locator)
        self.lock_locator(locator)
        denied = None
        models: tuple[type[Document | ExportFile], ...] = (Document, ExportFile)
        for model in models:
            for record in self.session.scalars(select(model).where(model.storage_path == locator)):
                resource = cast(Document | ExportFile, record)
                try:
                    self._authorize(resource, actor_id)
                except FileStorageError as exc:
                    if exc.code != "FILE_FORBIDDEN":
                        raise
                    denied = exc
                    continue
                if isinstance(resource, ExportFile) and resource.status != "ready":
                    raise FileStorageError("FILE_NOT_READY", "导出尚未完成。", current_status=resource.status)
                if resource.file_metadata is None:
                    continue
                try:
                    metadata = FileMetadata.model_validate(resource.file_metadata)
                except ValidationError as exc:
                    raise FileStorageError("FILE_METADATA_INVALID", "已登记文件元数据无效。") from exc
                if metadata.migration.status not in {"not_required", "migrated"}:
                    raise FileStorageError("FILE_METADATA_INVALID", "文件尚未登记到持久根，需显式迁移。")
                try:
                    content = path.read_bytes()
                except FileNotFoundError:
                    raise FileStorageError("FILE_MISSING", "已登记原稿缺失。", http_status=404) from None
                except OSError:
                    raise FileStorageError("FILE_UNREADABLE", "已登记原稿不可读。", http_status=503) from None
                if metadata.size_bytes != len(content) or metadata.sha256 != hashlib.sha256(content).hexdigest():
                    raise FileStorageError("FILE_CONTENT_CHANGED", "原稿与登记内容不一致，需核对材料。")
                return metadata
        if denied is not None:
            raise denied
        raise FileStorageError("FILE_NOT_FOUND", "未找到可关联的已登记持久文件；外部材料请上传。", http_status=404)


    def link_registered_document(self, document: Document, locator: str, *, actor_id: UUID) -> StoredFile:
        """Keep each shared identity traceable after its live metadata is removed."""
        course = self._authorize(document, actor_id)
        if document.id is None or document.uploaded_by != actor_id:
            raise FileStorageError("FILE_REFERENCE_CONFLICT", "共享原稿需要真实拟建身份和上传者。")
        if document.storage_path is not None or document.file_metadata is not None:
            raise FileStorageError("FILE_IDENTITY_IN_USE", "共享关联须使用新资源身份。")
        metadata = self.registered_file_metadata(locator, actor_id=actor_id)
        operation_id = uuid4()
        receipt_path = self.resolve_path(locator).with_name(operation_id.hex + ".receipt.json")
        now = datetime.now(UTC)
        receipt = OperationReceipt(
            operation_id=operation_id, resource_type="document", resource_id=document.id,
            owner={"course_id": str(course.id), "knowledge_base_id": str(document.knowledge_base_id)},
            actor_id=actor_id, candidate_locator=locator, stage="prepared",
            started_at=now, updated_at=now,
        )
        try:
            self._write_receipt(receipt_path, receipt, create=True)
            # The existing bytes were verified under the locator lock; no bytes are rewritten.
            receipt.stage = "written"
            receipt.updated_at = datetime.now(UTC)
            self._write_receipt(receipt_path, receipt)
        except OSError as exc:
            self._fail_file_operation(
                receipt_path, receipt, exc, code="FILE_RECEIPT_WRITE_FAILED",
                message="共享关联收据写入失败，原稿保留。", stage="reference",
            )
        return StoredFile(locator, metadata, receipt_path)

    def _fail_file_operation(
        self, receipt_path: Path, receipt: OperationReceipt, error: OSError,
        *, code: str, message: str, stage: str,
    ) -> NoReturn:
        receipt.stage = "failed"
        receipt.updated_at = datetime.now(UTC)
        receipt.error = FileErrorDetail(code=code, message=str(error) or type(error).__name__, stage=stage)
        if receipt_path.exists():
            try:
                self._write_receipt(receipt_path, receipt)
            except (OSError, FileStorageError) as receipt_error:
                message += f"失败收据也未能更新（{type(receipt_error).__name__}），原收据保留。"
        raise FileStorageError(code, message, http_status=503) from error

    def read_document(self, document: Document) -> bytes:
        path = self._existing_path(document.storage_path, legacy=self._legacy_metadata(document.file_metadata))
        try:
            return path.read_bytes()
        except FileNotFoundError:
            raise FileStorageError("FILE_MISSING", "资料文件缺失。", http_status=404, current_status=document.status.value) from None
        except OSError:
            raise FileStorageError("FILE_UNREADABLE", "资料文件不可读。", http_status=503, current_status=document.status.value) from None

    def _write_receipt(self, path: Path, receipt: OperationReceipt, *, create: bool = False) -> None:
        # Also hold a database connection across post-commit receipt acknowledgement.
        # The maintenance drain must observe this filesystem phase, not just SQL commit.
        self.lock_locator(receipt.candidate_locator)
        ensure_storage_writable(self.root)
        data = receipt.model_dump_json(indent=2).encode("utf-8")
        candidate = path if create else path.with_name(uuid4().hex + ".receipt.pending")
        with candidate.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if not create:
            os.replace(candidate, path)

    def _store_bytes(
        self, *, resource_type: Literal["document", "export"], resource_id: UUID, owner: dict[str, str],
        actor_id: UUID, filename: str, content: bytes, bucket: str,
    ) -> StoredFile:
        ensure_storage_writable(self.root)
        if not isinstance(content, bytes):
            raise FileStorageError("FILE_INVALID_INPUT", "文件内容必须是字节。", http_status=422)
        operation_id = uuid4()
        suffix = Path(filename).suffix.lower()
        if not suffix or len(suffix) > 16 or not suffix[1:].isalnum():
            suffix = ".bin"
        locator = f"{bucket}/{resource_id.hex}/{operation_id.hex}{suffix}"
        path = self.resolve_path(locator)
        self.lock_locator(locator)
        receipt_path = path.with_name(operation_id.hex + ".receipt.json")
        now = datetime.now(UTC)
        receipt = OperationReceipt(
            operation_id=operation_id, resource_type=resource_type, resource_id=resource_id,
            owner=owner, actor_id=actor_id, candidate_locator=locator,
            stage="prepared", started_at=now, updated_at=now,
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._write_receipt(receipt_path, receipt, create=True)
            with path.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            actual = path.read_bytes()
            digest = hashlib.sha256(actual).hexdigest()
            if len(actual) != len(content) or digest != hashlib.sha256(content).hexdigest():
                raise OSError("persisted content differs")
            metadata = FileMetadata(
                media_type=_media_type(filename, actual), size_bytes=len(actual), sha256=digest,
                migration=MigrationRecord(status="not_required"),
            )
            receipt.stage = "written"
            receipt.updated_at = datetime.now(UTC)
            self._write_receipt(receipt_path, receipt)
            return StoredFile(locator, metadata, receipt_path)
        except OSError as exc:
            self._fail_file_operation(
                receipt_path, receipt, exc, code="FILE_WRITE_FAILED",
                message="文件或收据写入失败，已产生材料保留供核对。", stage="write",
            )

    def store_document(self, document: Document, content: bytes, *, actor_id: UUID) -> StoredFile:
        course = self._authorize(document, actor_id)
        if document.storage_path is not None or document.file_metadata is not None:
            raise FileStorageError("FILE_IDENTITY_IN_USE", "已登记原稿不能重写；新内容须使用新资源身份。")
        if document.id is None or document.uploaded_by != actor_id:
            raise FileStorageError("FILE_REFERENCE_CONFLICT", "文件需要真实拟建身份和上传者。")
        return self._store_bytes(
            resource_type="document", resource_id=document.id,
            owner={"course_id": str(course.id), "knowledge_base_id": str(document.knowledge_base_id)},
            actor_id=actor_id, filename=document.original_filename, content=content, bucket="uploads",
        )

    def create_export(
        self, *, filename: str, content: bytes, actor_id: UUID,
        audience: str, course_id: UUID | None = None,
        exam_id: UUID | None = None, submission_id: UUID | None = None,
    ) -> ManagedFileView:
        """Register bytes from a real business producer; do not invent a format."""
        ensure_storage_writable(self.root)
        if (
            sum(owner is not None for owner in (course_id, exam_id, submission_id)) != 1
            or audience not in {"teacher_only", "submission_owner"}
            or (audience == "submission_owner" and submission_id is None)
            or not filename.strip() or len(filename) > 255
            or not isinstance(content, bytes)
        ):
            raise FileStorageError("FILE_INVALID_INPUT", "导出须有唯一真实归属、合法访问范围和文件内容。", http_status=422)
        export = ExportFile(
            id=uuid4(), course_id=course_id, exam_id=exam_id, submission_id=submission_id,
            created_by=actor_id, audience=audience, original_filename=filename,
            status="writing", created_at=datetime.now(UTC),
        )
        course = self._authorize(export, actor_id)
        owner = {"course_id": str(course.id)}
        if exam_id is not None:
            owner["exam_id"] = str(exam_id)
        if submission_id is not None:
            owner["submission_id"] = str(submission_id)
        try:
            self.session.add(export)
            self.session.commit()
        except SQLAlchemyError as exc:
            self.session.rollback()
            raise FileStorageError("FILE_REFERENCE_FAILED", "导出归属登记失败，未开始写文件。", http_status=503) from exc
        stored = None
        try:
            stored = self._store_bytes(
                resource_type="export", resource_id=export.id, owner=owner,
                actor_id=actor_id, filename=filename, content=content, bucket="exports",
            )
            export.storage_path = stored.storage_path
            export.file_metadata = stored.metadata.model_dump(mode="json")
            export.status = "ready"
            export.completed_at = datetime.now(UTC)
            self.session.commit()
        except (FileStorageError, SQLAlchemyError) as exc:
            self.session.rollback()
            error = FileErrorDetail(
                code=exc.code if isinstance(exc, FileStorageError) else "FILE_REFERENCE_FAILED",
                message=str(exc) if isinstance(exc, FileStorageError) else type(exc).__name__,
                stage="write" if stored is None else "reference",
            )
            export.status = "failed"
            export.error = error.model_dump()
            export.completed_at = datetime.now(UTC)
            if stored is not None:
                export.storage_path = stored.storage_path
                export.file_metadata = stored.metadata.model_dump(mode="json")
            try:
                self.session.commit()
            except SQLAlchemyError as record_error:
                self.session.rollback()
                if stored is not None:
                    self.fail_receipt(stored, code=error.code, message=error.message)
                raise FileStorageError(
                    "FILE_FAILURE_RECORD_FAILED",
                    f"导出操作失败（{error.code}），失败登记也未提交；保留 writing 记录和材料。",
                    http_status=503, current_status="writing",
                ) from record_error
            if stored is not None:
                self.fail_receipt(stored, code=error.code, message=error.message)
            raise FileStorageError(error.code, error.message, http_status=503, current_status="failed") from exc
        self.commit_receipt(stored, current_status="ready")
        return self.get_view("e_" + export.id.hex, actor_id=actor_id)

    def _update_receipt(self, stored: StoredFile, *, stage: Literal["prepared", "written", "committed", "failed"], error: FileErrorDetail | None = None) -> None:
        receipt = OperationReceipt.model_validate_json(stored.receipt_path.read_bytes())
        receipt.stage = stage
        receipt.error = error
        receipt.updated_at = datetime.now(UTC)
        self._write_receipt(stored.receipt_path, receipt)

    def commit_receipt(self, stored: StoredFile, *, current_status: str) -> None:
        try:
            self._update_receipt(stored, stage="committed")
        except (OSError, ValueError, FileStorageError) as exc:
            receipt_id = stored.receipt_path.name.removesuffix(".receipt.json")
            raise FileStorageError(
                "FILE_RECEIPT_UPDATE_FAILED",
                f"资源已提交（操作 {receipt_id}），收据更新失败；勿重复创建。",
                http_status=503, current_status=current_status,
            ) from exc

    def fail_receipt(self, stored: StoredFile, *, code: str, message: str) -> None:
        try:
            self._update_receipt(
                stored, stage="failed", error=FileErrorDetail(code=code, message=message, stage="reference"),
            )
        except (OSError, ValueError, FileStorageError) as exc:
            raise FileStorageError("FILE_RECEIPT_UPDATE_FAILED", "原操作失败，失败收据更新也失败；材料保留。", http_status=503) from exc

    def delete_unreferenced_bytes(self, locator: str) -> None:
        """Internal explicitly-authorized cleanup; no public deletion endpoint."""
        ensure_storage_writable(self.root)
        path = self.resolve_path(locator)
        self.lock_locator(locator)
        ensure_storage_writable(self.root)
        known: tuple[type[Document | ExportFile], ...] = (Document, ExportFile)
        for model in known:
            for record in self.session.scalars(select(model)):
                resource = cast(Document | ExportFile, record)
                if resource.storage_path is not None and self._existing_path(resource.storage_path, legacy=self._legacy_metadata(resource.file_metadata)).resolve() == path:
                    raise FileStorageError("FILE_IN_USE", "文件仍有有效业务引用。")
        # Refuse to ignore future E2 tables until their real resource mapping is attached.
        connection = self.session.connection()
        if connection.dialect.name == "postgresql":
            schema = self.session.scalar(text("SELECT current_schema()"))
            tables = inspect(connection).get_table_names(schema=schema)
            if {"source_pages", "extracted_questions", "question_assets"}.intersection(tables):
                raise FileStorageError("FILE_IN_USE", "原页/题图引用需完整接入后才能核对清理。")
        matched_receipt = False
        for bucket in ("uploads", "papers", "assets", "exports"):
            for receipt_path in (self.root / bucket).rglob("*.receipt.json"):
                try:
                    receipt = OperationReceipt.model_validate_json(receipt_path.read_bytes())
                except (OSError, ValueError, FileStorageError) as exc:
                    raise FileStorageError("FILE_IN_USE", "存在无法核对的操作收据。") from exc
                if self.resolve_path(receipt.candidate_locator) != path:
                    continue
                matched_receipt = True
                if receipt.stage != "committed":
                    raise FileStorageError("FILE_IN_USE", "文件仍关联未完成操作材料。")
                if receipt.resource_type == "document" and self.session.scalar(
                    select(QuestionSourceChunk.id).where(QuestionSourceChunk.document_id == receipt.resource_id).limit(1)
                ):
                    raise FileStorageError("FILE_IN_USE", "原材料仍有题目来源引用。")
        if not matched_receipt:
            raise FileStorageError("FILE_IN_USE", "材料没有可核对的登记收据，不能判定可清理。")
        try:
            path.unlink()
        except OSError as exc:
            raise FileStorageError("FILE_DELETE_FAILED", "文件清理失败，引用状态未改写。", http_status=503) from exc
