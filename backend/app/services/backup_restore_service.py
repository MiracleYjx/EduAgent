"""Consistent offline BackupSet creation and verification-only isolated restoration."""
from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.core.environment import environment_path, resolve_environment_profile
from backend.app.core.maintenance import (
    MARKER,
    MaintenanceWindow,
    authorize_local_maintenance,
    durable_json,
    require_admin_user,
)
from backend.app.core.settings_security import SetupSecurityService
from backend.app.domain.enums import WorkflowStatus
from backend.app.models import (
    Course,
    Exam,
    ExtractedQuestion,
    KnowledgeBase,
    PaperImport,
    Question,
    SourcePage,
    Submission,
    User,
    WorkflowRun,
)
from backend.app.schemas.file_storage import OperationReceipt
from backend.app.schemas.settings import EnvironmentProfile
from backend.app.schemas.storage_maintenance import (
    BackupFile,
    BackupIssue,
    BackupManifest,
    BackupReceipt,
    BackupReference,
    DatabaseDump,
    RestoreReport,
)
from backend.app.services.file_resources import resource_kind
from backend.app.services.file_storage_service import (
    FileStorageError,
    FileStorageService,
)
from backend.app.services.postgres_backup import PostgresTools
from backend.app.services.settings.config_service import ConfigService
from backend.app.services.settings.runtime_snapshot import (
    RuntimeSnapshot,
    RuntimeSnapshotService,
)
from backend.app.services.storage_migration_service import (
    copy_verified,
    fingerprint,
    storage_references,
)
from backend.app.services.workflow_checkpoint import (
    run_kind_criteria,
    snapshot_bindings_from_rows,
)

BUCKETS = ("uploads", "papers", "assets", "exports", "materials")


def error_issue(exc: Exception, stage: str) -> BackupIssue:
    return BackupIssue(code=exc.code if isinstance(exc, FileStorageError) else "STORAGE_OPERATION_FAILED",
                       stage=stage, message=str(exc) or type(exc).__name__)


def verify_foreign_keys(connection: Connection) -> None:
    """Check actual restored FK relationships, including composite nullable keys."""
    inspector = inspect(connection)
    schemas = [s for s in inspector.get_schema_names() if s != "information_schema" and not s.startswith("pg_")]
    quote = connection.dialect.identifier_preparer.quote
    for schema in schemas:
        for table in inspector.get_table_names(schema=schema):
            for fk in inspector.get_foreign_keys(table, schema=schema):
                source_columns = fk["constrained_columns"]
                target_columns = fk["referred_columns"]
                target_schema = fk["referred_schema"] or schema
                joins = " AND ".join(f"a.{quote(a)} = b.{quote(b)}" for a, b in zip(source_columns, target_columns, strict=True))
                present = " AND ".join(f"a.{quote(column)} IS NOT NULL" for column in source_columns)
                statement = (f"SELECT 1 FROM {quote(schema)}.{quote(table)} a WHERE {present} AND NOT EXISTS "
                             f"(SELECT 1 FROM {quote(target_schema)}.{quote(fk['referred_table'])} b WHERE {joins}) LIMIT 1")
                if connection.scalar(text(statement)):
                    raise FileStorageError("RESTORE_REFERENCE_CONFLICT", f"恢复后的外键关系不完整：{schema}.{table}。")


def _runtime_snapshot_ids(connection: Connection) -> set[UUID]:
    """Read every live/history binding; completed personal operations still matter."""
    tables = set(inspect(connection).get_table_names())
    required: set[UUID] = set()
    if "workflow_runs" in tables:
        rows = connection.execute(select(WorkflowRun.workflow_id, WorkflowRun.status, WorkflowRun.checkpoint)
            .where(WorkflowRun.status.in_([WorkflowStatus.QUEUED, WorkflowStatus.RUNNING, WorkflowStatus.PAUSED]))
            .where(run_kind_criteria())).all()
        required.update(UUID(item.snapshot_id) for item in snapshot_bindings_from_rows(
            (str(row[0]), row[1], row[2]) for row in rows) if item.snapshot_id is not None)
    from backend.app.models.conversation import Message
    from backend.app.models.document import Document
    from backend.app.models.operation_run import OperationRun
    from backend.app.models.practice import GradeAttempt
    for model in (OperationRun, GradeAttempt, Message):
        if model.__tablename__ in tables:
            required.update(connection.scalars(select(model.runtime_snapshot_id).where(model.runtime_snapshot_id.is_not(None))))
    if Document.__tablename__ in tables:
        for facts in connection.scalars(select(Document.parse_facts).where(Document.subject_id.is_not(None))):
            local = (facts or {}).get("local_ocr", {})
            if local.get("runtime_snapshot_id"):
                required.add(UUID(local["runtime_snapshot_id"]))
            for page in (facts or {}).get("ocr", {}).values():
                if page.get("snapshot_id"):
                    required.add(UUID(page["snapshot_id"]))
    return required


class BackupRestoreService:
    def __init__(self, engine: Engine, *, root: Path, tools: PostgresTools):
        self.engine = engine
        self.root = root.resolve()
        self.tools = tools
        self.profile: EnvironmentProfile | None = None
        self.security: SetupSecurityService | None = None
        self._token: str | None = None
        self.snapshots: RuntimeSnapshotService | None = None
        if tools.url != engine.url:
            raise FileStorageError("POSTGRES_SERVER_MISMATCH", "客户端与维护引擎配置不一致。")

    @classmethod
    def for_local_management(cls, engine: Engine, *, profile: EnvironmentProfile,
                             security: SetupSecurityService, token: str, tools: PostgresTools,
                             snapshots: RuntimeSnapshotService | None = None) -> BackupRestoreService:
        authorize_local_maintenance(security, token, profile)
        locator = profile.database_locator
        if locator is not None and (engine.url.host != locator.host or (engine.url.port or 5432) != locator.port
                or engine.url.database != locator.database or engine.url.username != locator.user):
            raise FileStorageError("POSTGRES_SERVER_MISMATCH", "维护引擎与所选环境的数据库不一致。")
        service = cls(engine, root=environment_path(profile, "storage"), tools=tools)
        service.profile, service.security, service._token = profile, security, token
        service.snapshots = snapshots
        if snapshots is not None and (snapshots.root.parent != profile.config_root
                or snapshots.environment_id != profile.environment_id):
            raise FileStorageError("BACKUP_SETTINGS_PROFILE_MISMATCH", "快照服务与维护空间不一致。")
        return service

    def _check_actor(self, actor_id: UUID | None) -> None:
        if self.profile is not None:
            if self.security is None or self._token is None or actor_id is not None:
                raise FileStorageError("MAINTENANCE_FORBIDDEN", "维护操作需要本地管理授权。", http_status=403)
            authorize_local_maintenance(self.security, self._token, self.profile)
            return
        if actor_id is None:
            raise FileStorageError("MAINTENANCE_FORBIDDEN", "维护操作需要管理授权。", http_status=403)
        with Session(self.engine) as session:
            require_admin_user(session, actor_id)

    def _collect(self, session: Session, root: Path, *, profile: EnvironmentProfile | None = None) -> tuple[list[BackupReference], list[BackupReceipt], set[str], list[BackupIssue]]:
        files = FileStorageService(session, root=root, profile=profile)
        references = []
        receipts = []
        owned: set[str] = set()
        issues = []
        actual_references = storage_references(session, personal_only=profile is not None)
        resources = {(resource_kind(ref.resource), ref.resource.id): ref for ref in actual_references}
        for ref in actual_references:
            if profile is not None and "subject_id" not in ref.owner:
                raise FileStorageError("BACKUP_LEGACY_DATA_PRESENT", "个人备份不能复制待退役的旧业务材料。")
            relative = None
            availability: Literal["available", "missing", "history_unknown"] = "history_unknown"
            try:
                metadata = ref.metadata
                migration_status = metadata.migration.status if metadata else "not_migrated" if ref.locator else "history_unknown"
                path = files._existing_path(ref.locator, legacy=ref.legacy).resolve()
                availability = "available" if path.is_file() else "missing"
                if ref.legacy or not path.is_relative_to(root):
                    issues.append(BackupIssue(code="FILE_NOT_MIGRATED", file_id=ref.file_id, stage="references",
                                              message="原定位仍为历史定位，需显式迁移；未将外部原稿冒充根内文件。"))
                else:
                    relative = path.relative_to(root).as_posix()
                    files.resolve_path(relative)
                    owned.add(relative)
                if availability == "missing":
                    issues.append(BackupIssue(code="FILE_MISSING", file_id=ref.file_id, relative_path=relative,
                                              stage="references", message="关联原稿缺失。"))
                elif metadata is not None:
                    measured = fingerprint(path)
                    if metadata.size_bytes != measured[0] or metadata.sha256 != measured[1]:
                        issues.append(BackupIssue(code="FILE_CONTENT_CHANGED", file_id=ref.file_id, relative_path=relative,
                                                  stage="references", message="原稿与登记摘要/长度不一致。"))
            except FileStorageError as exc:
                migration_status = "history_unknown"
                issues.append(BackupIssue(code=exc.code, file_id=ref.file_id, stage="references", message=str(exc)))
            except ValueError:
                migration_status = "history_unknown"
                issues.append(BackupIssue(code="FILE_METADATA_INVALID", file_id=ref.file_id, stage="references", message="登记文件元数据无效。"))
            references.append(BackupReference(file_id=ref.file_id,
                resource_type=resource_kind(ref.resource),
                resource_id=ref.resource.id, owner=ref.owner, relative_path=relative,
                availability=availability, migration_status=migration_status))
        for bucket in BUCKETS:
            for path in sorted((root / bucket).rglob("*.receipt.json")):
                relative = path.relative_to(root).as_posix()
                try:
                    files.resolve_path(relative)
                    receipt = OperationReceipt.model_validate_json(path.read_bytes())
                    candidate = files.resolve_path(receipt.candidate_locator)
                    if candidate.relative_to(root).parts[0] not in BUCKETS:
                        raise ValueError("receipt candidate outside managed buckets")
                    resource = resources.get((receipt.resource_type, receipt.resource_id))
                    if resource is not None and "subject_id" in resource.owner:
                        valid_owner = receipt.owner in (resource.owner, {"subject_id": resource.owner["subject_id"]})
                    else:
                        valid_owner = resource.owner == receipt.owner if resource else self._receipt_owner_exists(session, receipt)
                    actor_valid = receipt.actor_id is None if "subject_id" in receipt.owner else receipt.actor_id is not None and session.get(User, receipt.actor_id) is not None
                    if not valid_owner or not actor_valid:
                        raise ValueError("receipt owner or actor not real")
                    if receipt.stage == "failed" and receipt.error is None:
                        raise ValueError("failed receipt without error")
                    owned.add(relative)
                    if candidate.is_file():
                        owned.add(receipt.candidate_locator)
                    receipts.append(BackupReceipt(relative_path=relative, operation_id=receipt.operation_id, stage=receipt.stage))
                    if receipt.stage in {"prepared", "written"}:
                        issues.append(BackupIssue(code="UNFINISHED_OPERATION", relative_path=relative,
                                                  stage="receipts", message="操作收据尚未确认结束，不能作为完整备份。"))
                except (OSError, ValueError, FileStorageError) as exc:
                    issues.append(BackupIssue(code="RECEIPT_INVALID", relative_path=relative,
                                              stage="receipts", message=str(exc) or type(exc).__name__))
        if profile is not None:
            from backend.app.models.document import Document
            from backend.app.services.file_storage_service import (
                personal_material_asset_locator,
            )
            for document in session.scalars(select(Document).where(Document.subject_id.is_not(None))):
                for asset in (document.parse_facts or {}).get("assets", []):
                    if asset.get("stored") is not True:
                        continue
                    digest = str(asset.get("sha256", ""))
                    if not re.fullmatch(r"[a-f0-9]{64}", digest):
                        raise FileStorageError("FILE_METADATA_INVALID", "资料媒体摘要无效。")
                    relative = personal_material_asset_locator(document.id, digest, str(asset.get("media_type")))
                    identity = f"m_{document.id.hex}_{digest}"
                    if any(ref.file_id == identity for ref in references):
                        continue
                    path = files.resolve_path(relative)
                    if not path.is_file() or fingerprint(path)[1] != digest:
                        raise FileStorageError("FILE_CONTENT_CHANGED", "资料媒体缺失或与原始摘要不一致。")
                    owned.add(relative)
                    references.append(BackupReference(file_id=identity, resource_type="material_asset",
                        resource_id=document.id, owner={"subject_id": str(document.subject_id), "document_id": str(document.id)},
                        relative_path=relative, availability="available", migration_status="not_required"))
        return references, receipts, owned, issues

    @staticmethod
    def _receipt_owner_exists(session: Session, receipt: OperationReceipt) -> bool:
        if "subject_id" in receipt.owner:
            from backend.app.models.subject import Subject
            return set(receipt.owner) == {"subject_id"} and session.get(Subject, UUID(receipt.owner["subject_id"])) is not None
        models = {"course_id": Course, "knowledge_base_id": KnowledgeBase, "exam_id": Exam, "submission_id": Submission, "paper_import_id": PaperImport, "extracted_question_id": ExtractedQuestion, "source_page_id": SourcePage, "question_id": Question}
        if "course_id" not in receipt.owner or set(receipt.owner) - set(models):
            return False
        try:
            loaded = {key: session.get(models[key], UUID(value)) for key, value in receipt.owner.items()}
        except ValueError:
            return False
        if not all(loaded.values()):
            return False
        course_id = UUID(receipt.owner["course_id"])
        kb = loaded.get("knowledge_base_id")
        exam = loaded.get("exam_id")
        submission = loaded.get("submission_id")
        if isinstance(kb, KnowledgeBase) and kb.course_id != course_id:
            return False
        if isinstance(exam, Exam) and exam.course_id != course_id:
            return False
        if isinstance(submission, Submission):
            actual_exam = session.get(Exam, submission.exam_id)
            if actual_exam is None or actual_exam.course_id != course_id:
                return False
        imported = loaded.get("paper_import_id")
        extracted = loaded.get("extracted_question_id")
        page = loaded.get("source_page_id")
        question = loaded.get("question_id")
        if isinstance(imported, PaperImport) and imported.course_id != course_id:
            return False
        if isinstance(extracted, ExtractedQuestion) and (extracted.paper_import.course_id != course_id or isinstance(imported, PaperImport) and extracted.paper_import_id != imported.id):
            return False
        if isinstance(page, SourcePage):
            if page.paper_import.course_id != course_id or isinstance(imported, PaperImport) and page.paper_import_id != imported.id:
                return False
            if isinstance(extracted, ExtractedQuestion) and str(page.id) not in extracted.source_page_ids:
                return False
        return not (isinstance(question, Question) and question.course_id != course_id)

    def backup(self, *, backup_root: Path, actor_id: UUID | None = None, writers_stopped: bool,
               runtime_snapshots: Any = None,
               timeout: float = 10) -> tuple[Path, BackupManifest]:
        self._check_actor(actor_id)
        backup_root = backup_root.resolve()
        if backup_root.is_relative_to(self.root) or self.root.is_relative_to(backup_root):
            raise FileStorageError("BACKUP_INVALID_TARGET", "备份根与业务文件根必须相互独立。")
        manifest = BackupManifest(backup_set_id=uuid4(), outcome="creating", started_at=datetime.now(UTC),
                                  manifest_version=2 if self.profile is not None else 1,
                                  environment_id=self.profile.environment_id if self.profile is not None else None,
                                  references=[], files=[], operation_receipts=[], issues=[])
        destination = backup_root / ("backup_set_" + manifest.backup_set_id.hex)
        destination.mkdir(parents=True, exist_ok=False)
        durable_json(destination / "manifest.json", manifest.model_dump_json(indent=2), create=True)
        finalized = False
        stage = "window"
        try:
            if not writers_stopped:
                raise FileStorageError("MAINTENANCE_NOT_CONFIRMED", "请先停止全部后端/工作进程及写入脚本，再使用 --writers-stopped。")
            if self.profile is not None:
                assert self.security is not None and self._token is not None
                window = MaintenanceWindow.for_local_management(self.engine, profile=self.profile,
                    security=self.security, token=self._token, timeout=timeout)
            else:
                window = MaintenanceWindow(self.engine, self.root, actor_id=actor_id, timeout=timeout)
            with window:
                manifest.window_started_at = window.started_at
                assert window.connection is not None
                self.tools.validate_source(window.connection)
                stage = "schema_scope"
                self._assert_database_scope(window.connection)
                stage = "references"
                verify_foreign_keys(window.connection)
                if self.profile is not None:
                    self._assert_no_legacy_data(window.connection)
                with Session(bind=window.connection, join_transaction_mode="create_savepoint") as session:
                    manifest.references, manifest.operation_receipts, owned, issues = self._collect(session, self.root, profile=self.profile)
                    manifest.issues.extend(issues)
                revisions = window.connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all() if inspect(window.connection).has_table("alembic_version") else []
                stage = "dump"
                self.tools.dump(destination / "database.dump")
                size, digest = fingerprint(destination / "database.dump")
                manifest.database = DatabaseDump(size_bytes=size, sha256=digest, schema_revision=",".join(revisions) or None,
                    schema_name=window.connection.scalar(text("SELECT current_schema()")))
                stage = "copy"
                for bucket in BUCKETS:
                    if (self.root / bucket).exists() and not (self.root / bucket).is_dir():
                        raise FileStorageError("FILE_INVALID_LAYOUT", "业务文件分区不是目录。")
                    (destination / "files" / bucket).mkdir(parents=True, exist_ok=True)
                    for source in sorted((self.root / bucket).rglob("*")):
                        if not source.is_file():
                            continue
                        relative = source.relative_to(self.root).as_posix()
                        resolved = source.resolve()
                        if not resolved.is_relative_to(self.root):
                            raise FileStorageError("FILE_INVALID_PATH", "材料路径越出业务根。")
                        if self.profile is not None and relative not in owned:
                            raise FileStorageError("UNOWNED_MATERIAL", "未核验归属的材料不复制到个人备份。")
                        target = destination / "files" / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        size, digest = copy_verified(source, target)
                        manifest.files.append(BackupFile(relative_path=relative, size_bytes=size, sha256=digest))
                        if relative not in owned:
                            manifest.issues.append(BackupIssue(code="UNOWNED_MATERIAL", relative_path=relative,
                                                              stage="copy", message="实际材料未能核对所属资源或真实操作收据。"))
                stage = "runtime_snapshots"
                required_snapshots = _runtime_snapshot_ids(window.connection)
                if self.profile is not None and required_snapshots:
                    if self.snapshots is None:
                        raise FileStorageError("SNAPSHOT_BACKUP_UNAVAILABLE", "个人历史缺少原快照服务。")
                    entries = self.snapshots.copy_backup_entries(destination, tuple(required_snapshots))
                    original_revisions = {}
                    for identifier in required_snapshots:
                        snapshot = self.snapshots.load(identifier)
                        original_revisions[snapshot.settings_revision] = self.snapshots.backup_dependencies(snapshot)
                    for identifier, revision in sorted(original_revisions.items(), key=lambda item: str(item[0])):
                        relative = f"configuration_revisions/{identifier}.json"
                        target = destination / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        durable_json(target, revision.model_dump_json(), create=True)
                        size, digest = fingerprint(target)
                        manifest.configuration_revisions.append(BackupFile(relative_path=relative, size_bytes=size, sha256=digest))
                elif runtime_snapshots is None and required_snapshots:
                    profile = resolve_environment_profile()
                    if environment_path(profile, "storage") != self.root:
                        raise FileStorageError("BACKUP_SETTINGS_PROFILE_MISMATCH", "快照与业务文件根不属于同一选定环境。")
                    with ConfigService(profile.config_root.parent, profile.environment_id, writable=False) as config:
                        entries = RuntimeSnapshotService(config).copy_backup_entries(destination, tuple(required_snapshots))
                else:
                    entries = tuple(runtime_snapshots.copy_entries(destination)) if runtime_snapshots is not None else ()
                for entry in entries:
                    manifest.runtime_snapshots.append(BackupFile(relative_path=entry.relative_path,
                        size_bytes=entry.size_bytes, sha256=entry.sha256))
                if not required_snapshots.issubset({UUID(Path(item.relative_path).stem) for item in manifest.runtime_snapshots}):
                    raise FileStorageError("SNAPSHOT_BACKUP_UNAVAILABLE", "备份缺少非终态运行的原快照，未替换当前配置。")
                stage = "verify"
                self._verify_bytes(destination, manifest)
                window.assert_quiet()
                manifest.window_finished_at = datetime.now(UTC)
                manifest.completed_at = datetime.now(UTC)
                manifest.outcome = "incomplete" if manifest.issues else "complete"
                if self.profile is not None and manifest.issues:
                    window.keep_marker_on_exit = True
                manifest = BackupManifest.model_validate(manifest.model_dump())
                durable_json(destination / "manifest.json", manifest.model_dump_json(indent=2))
                finalized = True
            return destination, manifest
        except (OSError, ValueError, RuntimeError, SQLAlchemyError, FileStorageError, subprocess.SubprocessError) as exc:
            if finalized:
                # Backup was published; do not rewrite it if releasing the window failed.
                raise FileStorageError("MAINTENANCE_RELEASE_FAILED", "备份清单已可靠保存，但维护窗口释放失败；保持进程停写并核对标记。") from exc
            manifest.outcome = "failed"
            if manifest.window_started_at is not None:
                manifest.window_finished_at = datetime.now(UTC)
            manifest.completed_at = datetime.now(UTC)
            manifest.issues.append(error_issue(exc, stage))
            durable_json(destination / "manifest.json", manifest.model_dump_json(indent=2))
            return destination, manifest

    @staticmethod
    def _verify_bytes(destination: Path, manifest: BackupManifest) -> None:
        if manifest.database is None:
            raise FileStorageError("BACKUP_DUMP_MISSING", "清单没有真实数据库 dump。")
        dump = (destination / "database.dump").resolve()
        if not dump.is_relative_to(destination.resolve()):
            raise FileStorageError("BACKUP_INVALID_PATH", "数据库 dump 越出同一备份集。")
        if fingerprint(dump) != (manifest.database.size_bytes, manifest.database.sha256):
            raise FileStorageError("BACKUP_CONTENT_CHANGED", "数据库 dump 与清单不一致。")
        root = (destination / "files").resolve()
        expected = {entry.relative_path for entry in manifest.files}
        actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
        if actual != expected:
            raise FileStorageError("BACKUP_FILE_SET_MISMATCH", "备份文件集合与清单不一致。")
        for entry in manifest.files:
            path = (root / entry.relative_path).resolve()
            if not path.is_relative_to(root) or fingerprint(path) != (entry.size_bytes, entry.sha256):
                raise FileStorageError("BACKUP_CONTENT_CHANGED", f"备份文件不一致：{entry.relative_path}。")
        snapshot_root = (destination / "runtime_snapshots").resolve()
        expected_snapshots = {entry.relative_path.split("/", 1)[1] for entry in manifest.runtime_snapshots}
        actual_snapshots = (
            {path.relative_to(snapshot_root).as_posix() for path in snapshot_root.rglob("*") if path.is_file()}
            if snapshot_root.exists() else set()
        )
        if actual_snapshots != expected_snapshots:
            raise FileStorageError("BACKUP_FILE_SET_MISMATCH", "备份运行快照集合与清单不一致。")
        for entry in manifest.runtime_snapshots:
            path = (snapshot_root / entry.relative_path.split("/", 1)[1]).resolve()
            if not path.is_relative_to(snapshot_root) or fingerprint(path) != (entry.size_bytes, entry.sha256):
                raise FileStorageError("BACKUP_CONTENT_CHANGED", f"备份运行快照不一致：{entry.relative_path}。")
        revision_root = destination / "configuration_revisions"
        actual_revisions = {p.relative_to(destination).as_posix() for p in revision_root.rglob("*") if p.is_file()} if revision_root.exists() else set()
        if actual_revisions != {e.relative_path for e in manifest.configuration_revisions}:
            raise FileStorageError("BACKUP_FILE_SET_MISMATCH", "设置修订集合与清单不一致。")
        for entry in manifest.configuration_revisions:
            path = destination / entry.relative_path
            if not path.resolve().is_relative_to(destination.resolve()) or fingerprint(path) != (entry.size_bytes, entry.sha256):
                raise FileStorageError("BACKUP_CONTENT_CHANGED", "备份设置修订与清单不一致。")

    @staticmethod
    def _assert_database_scope(connection: Connection) -> None:
        """A whole-database dump must not copy unverified business schemas."""
        others = connection.execute(text("""
            SELECT DISTINCT n.nspname FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname<>current_schema() AND n.nspname<>'information_schema'
              AND n.nspname !~ '^pg_' AND c.relkind IN ('r','p','f','v','m','S')
              AND NOT EXISTS (
                SELECT 1 FROM pg_depend d
                WHERE d.classid='pg_class'::regclass AND d.objid=c.oid AND d.deptype='e'
              )
            ORDER BY n.nspname
        """)).scalars().all()
        if others:
            raise FileStorageError("UNMAPPED_SCHEMA", "数据库还有未经核实的其他业务 schema，未执行数据库备份：" + "、".join(others))

    @staticmethod
    def _assert_no_legacy_data(connection: Connection) -> None:
        tables = set(inspect(connection).get_table_names())
        dedicated = ("users", "roles", "user_roles", "courses", "exams", "exam_participants", "exam_questions",
            "submissions", "answers", "grading_results", "review_records", "exam_results", "diagnosis_reports",
            "export_files", "question_revision_comments", "question_source_papers", "workflow_runs")
        for table in dedicated:
            if table in tables and connection.scalar(text(f'SELECT 1 FROM "{table}" LIMIT 1')):
                raise FileStorageError("BACKUP_LEGACY_DATA_PRESENT", "个人备份不能为待退役旧业务创建副本。")
        # These shared tables have no personal-operation owner mapping yet.
        # Preserve their source rows, but do not silently archive them as personal facts.
        for table in ("audit_logs", "agent_runs"):
            if table in tables and connection.scalar(text(f'SELECT 1 FROM "{table}" LIMIT 1')):
                raise FileStorageError("BACKUP_LEGACY_DATA_PRESENT", "仍有旧业务或未核验归属的运行/审计记录，个人备份未复制这些数据。")

    def restore(self, *, backup_set: Path, target_database: str, target_root: Path,
                report_path: Path, actor_id: UUID | None = None,
                target_profile: EnvironmentProfile | None = None,
                target_snapshots: RuntimeSnapshotService | None = None) -> RestoreReport:
        self._check_actor(actor_id)
        backup_set = backup_set.resolve()
        target_root = target_root.resolve()
        report_path = report_path.resolve()
        if report_path.is_relative_to(self.root):
            raise FileStorageError("RESTORE_INVALID_REPORT", "恢复报告须独立保存，不能写入原业务文件根。")
        if report_path.exists():
            raise FileStorageError("RESTORE_REPORT_EXISTS", "恢复报告路径已存在，不能覆盖。")
        try:
            manifest = BackupManifest.model_validate_json((backup_set / "manifest.json").read_bytes())
        except (OSError, ValueError) as exc:
            raise FileStorageError("BACKUP_MANIFEST_INVALID", "备份清单缺失或格式无效，未开始恢复。") from exc
        restore_id = uuid4()
        started = datetime.now(UTC)
        issues = []
        target_engine = None
        stage = "verify_backup"
        try:
            if manifest.outcome != "complete":
                raise FileStorageError("BACKUP_NOT_COMPLETE", "此备份集仅可隔离调查，不能作为完整恢复。")
            self._verify_bytes(backup_set, manifest)
            if self.profile is not None:
                if manifest.manifest_version != 2 or manifest.environment_id != self.profile.environment_id:
                    raise FileStorageError("BACKUP_FORMAT_INCOMPATIBLE", "个人维护不导入旧格式或其他空间备份。")
                if target_profile is None or environment_path(target_profile, "storage") != target_root or target_profile.environment_id != manifest.environment_id:
                    raise FileStorageError("RESTORE_SETTINGS_PROFILE_MISMATCH", "恢复需要同一空间的显式隔离目标。")
                self._verify_restore_dependencies(backup_set, manifest, target_profile, target_snapshots)
            elif manifest.manifest_version != 1:
                raise FileStorageError("BACKUP_FORMAT_INCOMPATIBLE", "旧维护入口不能恢复个人备份。")
            if (not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", target_database)
                    or target_database == self.engine.url.database):
                raise FileStorageError("RESTORE_INVALID_DATABASE", "请指定新的隔离数据库名。")
            if (target_root.exists() or target_root.is_relative_to(self.root)
                    or self.root.is_relative_to(target_root) or target_root.is_relative_to(backup_set)
                    or backup_set.is_relative_to(target_root)):
                raise FileStorageError("RESTORE_INVALID_TARGET", "恢复文件根必须全新且独立，不能覆盖原环境或备份集。")
            snapshot_target = None
            if manifest.runtime_snapshots:
                profile = target_profile or resolve_environment_profile()
                if environment_path(profile, "storage") != target_root:
                    raise FileStorageError("RESTORE_SETTINGS_PROFILE_MISMATCH", "恢复快照要求匹配目标 storage 的显式环境。")
                snapshot_target = environment_path(profile, "config", "snapshots")
                if snapshot_target.exists():
                    raise FileStorageError("RESTORE_SNAPSHOTS_EXIST", "目标快照目录已存在，不能覆盖。")
            stage = "create_target"
            control = create_engine(self.engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT")
            try:
                with control.connect() as connection:
                    self.tools.validate_source(connection)
                    if connection.scalar(text("SELECT 1 FROM pg_database WHERE datname=:name"), {"name": target_database}):
                        raise FileStorageError("RESTORE_DATABASE_EXISTS", "目标数据库已存在，不能覆盖。")
                    quote = connection.dialect.identifier_preparer.quote
                    connection.execute(text(f"CREATE DATABASE {quote(target_database)}"))
            finally:
                control.dispose()
            target_root.mkdir(parents=True, exist_ok=False)
            durable_json(target_root / MARKER, json.dumps({"operation_id": str(restore_id),
                "purpose": "isolated_restore", "backup_set_id": str(manifest.backup_set_id),
                "started_at": started.isoformat(), "writes_enabled": False}), create=True)
            stage = "restore_database"
            self.tools.restore(backup_set / "database.dump", target_database)
            stage = "restore_files"
            for entry in manifest.files:
                target = target_root / entry.relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                copy_verified(backup_set / "files" / entry.relative_path, target)
            if snapshot_target is not None:
                stage = "restore_snapshots"
                snapshot_target.mkdir(parents=True, exist_ok=False)
                for entry in manifest.runtime_snapshots:
                    relative = entry.relative_path.split("/", 1)[1]
                    target = (snapshot_target / relative).resolve()
                    if not target.is_relative_to(snapshot_target):
                        raise FileStorageError("RESTORE_INVALID_TARGET", "运行快照越出目标配置目录。")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if copy_verified(backup_set / entry.relative_path, target) != (entry.size_bytes, entry.sha256):
                        raise FileStorageError("RESTORE_CONTENT_CHANGED", "恢复快照与同集清单不同。")
            target_engine = create_engine(self.engine.url.set(database=target_database), pool_pre_ping=True)
            from sqlalchemy import event
            assert manifest.database is not None
            schema = manifest.database.schema_name
            @event.listens_for(target_engine, "connect")
            def restored_schema(dbapi_connection, _record):
                with dbapi_connection.cursor() as cursor:
                    cursor.execute(f'SET search_path TO "{schema}", public')
                dbapi_connection.commit()
            stage = "verify_resources"
            with target_engine.connect() as connection:
                verify_foreign_keys(connection)
                restored_bindings = _runtime_snapshot_ids(connection)
                if not restored_bindings.issubset({UUID(Path(item.relative_path).stem) for item in manifest.runtime_snapshots}):
                    raise FileStorageError("SNAPSHOT_BACKUP_UNAVAILABLE", "恢复数据库引用的快照未包含在备份内，保留停写目标。")
                with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
                    references, receipts, owned, resource_issues = self._collect(session, target_root, profile=target_profile)
                issues.extend(resource_issues)
                expected_references = sorted([r.model_dump(mode="json") for r in manifest.references], key=lambda r: r["file_id"])
                actual_references = sorted([r.model_dump(mode="json") for r in references], key=lambda r: r["file_id"])
                if actual_references != expected_references:
                    issues.append(BackupIssue(code="RESTORE_REFERENCE_MISMATCH", stage=stage,
                                              message="恢复数据库的文件身份/定位/归属与同集清单不同。"))
                if sorted([r.model_dump(mode="json") for r in receipts], key=lambda r: r["relative_path"]) != sorted(
                        [r.model_dump(mode="json") for r in manifest.operation_receipts], key=lambda r: r["relative_path"]):
                    issues.append(BackupIssue(code="RESTORE_RECEIPT_MISMATCH", stage=stage,
                                              message="恢复的操作收据与同集清单不同。"))
                for entry in manifest.files:
                    if entry.relative_path not in owned:
                        issues.append(BackupIssue(code="UNOWNED_MATERIAL", relative_path=entry.relative_path,
                                                  stage=stage, message="恢复材料的真实归属无法核对。"))
                    if fingerprint(target_root / entry.relative_path) != (entry.size_bytes, entry.sha256):
                        issues.append(BackupIssue(code="RESTORE_CONTENT_CHANGED", relative_path=entry.relative_path,
                                                  stage=stage, message="恢复字节与同集清单不同。"))
                if connection.scalar(text("SELECT 1 FROM pg_constraint WHERE contype='f' AND NOT convalidated LIMIT 1")):
                    issues.append(BackupIssue(code="RESTORE_UNVALIDATED_RELATION", stage=stage, message="恢复数据库存在未验证的外键。"))
        except (OSError, ValueError, RuntimeError, SQLAlchemyError, FileStorageError, subprocess.SubprocessError) as exc:
            issues.append(error_issue(exc, stage))
        finally:
            if target_engine is not None:
                target_engine.dispose()
        report = RestoreReport(restore_id=restore_id, backup_set_id=manifest.backup_set_id, started_at=started,
                               completed_at=datetime.now(UTC), outcome="failed" if issues else "verified", issues=issues)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        # Target marker deliberately remains, even on verified: activation is separate.
        durable_json(report_path, report.model_dump_json(indent=2), create=True)
        return report

    @staticmethod
    def _verify_restore_dependencies(backup_set: Path, manifest: BackupManifest,
            profile: EnvironmentProfile, snapshots: RuntimeSnapshotService | None) -> None:
        if not manifest.runtime_snapshots:
            return
        if snapshots is None or snapshots.root.parent != profile.config_root or snapshots.environment_id != profile.environment_id:
            raise FileStorageError("SNAPSHOT_DEPENDENCY_UNAVAILABLE", "请先恢复同一空间的设置及安全凭据依赖。")
        revisions = {}
        try:
            from backend.app.schemas.settings import SettingsRevision
            for entry in manifest.configuration_revisions:
                saved = SettingsRevision.model_validate_json((backup_set / entry.relative_path).read_bytes())
                if saved.environment_id != profile.environment_id or str(saved.revision_id) != Path(entry.relative_path).stem:
                    raise ValueError("configuration revision identity differs")
                revisions[saved.revision_id] = saved
            for entry in manifest.runtime_snapshots:
                snapshot = RuntimeSnapshot.model_validate_json((backup_set / entry.relative_path).read_bytes())
                if (snapshot.environment_id != profile.environment_id or snapshot.settings_revision not in revisions
                        or str(snapshot.snapshot_id) != Path(entry.relative_path).stem):
                    raise ValueError("original snapshot dependency unavailable")
                if snapshots.backup_dependencies(snapshot) != revisions[snapshot.settings_revision]:
                    raise ValueError("configuration revision differs")
        except (ValueError, RuntimeError) as exc:
            raise FileStorageError("SNAPSHOT_DEPENDENCY_UNAVAILABLE", "原设置或凭据版本缺失，未回落当前配置。") from exc
