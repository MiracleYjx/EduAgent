"""Explicit, offline, approved Test retirement with durable non-atomic progress.

The plan contains identities, counts and hashes, never an archive of old rows or
file contents. Unknown/mixed ownership stops disposal; a crash never enables the
space or silently replaces the confirmed plan.
"""

from __future__ import annotations

import hashlib
import json
import stat
from importlib import import_module
from pathlib import Path
from typing import Any, NoReturn
from uuid import UUID

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from backend.app.core.environment import environment_path, load_test_resource_profile
from backend.app.core.maintenance import (
    MARKER,
    MaintenanceWindow,
    authorize_local_maintenance,
    durable_json,
)
from backend.app.core.settings_security import SetupSecurityService
from backend.app.schemas.file_storage import OperationReceipt
from backend.app.schemas.settings import (
    DatabaseIdentity,
    EnvironmentName,
    EnvironmentProfile,
)
from backend.app.services.file_storage_service import (
    FileStorageError,
    FileStorageService,
)
from backend.app.services.storage_migration_service import (
    fingerprint,
    storage_references,
)

_migration = import_module("migrations.versions.personal_learning_cutover")
LEGACY_TABLES: frozenset[str] = _migration.LEGACY_TABLES
JOURNAL = "personal_cutovers"
SHARED_OWNERS = {"chapters", "knowledge_bases", "documents", "document_chunks", "questions", "paper_imports"}
SHARED_DEPENDENTS = {
    "source_pages": ("paper_import_id", "paper_imports"),
    "extracted_questions": ("paper_import_id", "paper_imports"),
    "question_assets": ("question_id", "questions"),
    "question_generation_metadata": ("question_id", "questions"),
    "question_source_chunks": ("question_id", "questions"),
    "question_validation_results": ("question_id", "questions"),
}
DELETE_ORDER = (
    "question_assets", "extracted_questions", "source_pages", "paper_imports",
    "question_generation_metadata", "question_source_chunks", "question_validation_results",
    "document_chunks", "documents", "questions", "chapters", "knowledge_bases",
    "agent_runs", "workflow_runs", "audit_logs",
)
LEGACY_REFERENCES = {
    "chapters": {("course_id", "courses"), ("confirmed_by", "users")},
    "knowledge_bases": {("course_id", "courses")},
    "documents": {("course_id", "courses"), ("uploaded_by", "users")},
    "document_chunks": {("course_id", "courses")},
    "questions": {("course_id", "courses"), ("created_by", "users")},
    "paper_imports": {("course_id", "courses"), ("uploaded_by", "users")},
    "workflow_runs": {("submission_id", "submissions"), ("current_answer_id", "answers"), ("exam_result_id", "exam_results")},
    "agent_runs": {("user_id", "users")},
    "question_validation_results": {("requested_by", "users")},
}


def _checkpoint(_stage: str) -> None:
    """Phase boundary for deterministic process-interruption verification."""


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _old_identities(selected: dict) -> set[str]:
    identities = {str(row["id"]) for values in selected.values() for row in values if "id" in row}
    for row in selected.get("extracted_questions", []):
        identities.update(str(UUID(asset["id"])) for asset in row.get("assets") or [])
    return identities


def _fail(code: str) -> NoReturn:
    raise FileStorageError(code, "退役条件未通过；保留停写与原计划，请核对实际对象。")


class PersonalCutoverService:
    """Not exposed as a business HTTP API or an automatic migration hook."""

    def __init__(
        self, engine: Engine, *, profile: EnvironmentProfile, security: SetupSecurityService,
        token: str, approved_database: DatabaseIdentity, approved_root: Path,
    ) -> None:
        self.engine, self.profile, self.security, self.token = engine, profile, security, token
        self.approved_database, self.approved_root = approved_database, approved_root

    @classmethod
    def for_local_management(
        cls, engine: Engine, *, profile: EnvironmentProfile, security: SetupSecurityService,
        token: str, approved_database: DatabaseIdentity, approved_root: Path,
    ) -> PersonalCutoverService:
        authorize_local_maintenance(security, token, profile)
        return cls(engine, profile=profile, security=security, token=token,
            approved_database=approved_database, approved_root=approved_root)

    def _identity(self, connection: Connection) -> str:
        authorize_local_maintenance(self.security, self.token, self.profile)
        self.profile.validate_paths()
        receipt = load_test_resource_profile()
        expected = self.approved_database
        # This implementation is deliberately limited to the authorized Test
        # execution. Approval for another existing deployment is not inferred.
        if self.profile.environment != EnvironmentName.TEST or self.profile.environment_id != receipt.environment_id:
            _fail("CUTOVER_TEST_APPROVAL_REQUIRED")
        if expected.model_copy(update={"schema_name": "public"}) != receipt.database_identity:
            _fail("CUTOVER_DATABASE_IDENTITY_MISMATCH")
        root = self.profile.config_root.parent
        if (not self.approved_root.is_absolute() or self.approved_root != self.approved_root.resolve()
                or root == self.approved_root or not root.is_relative_to(self.approved_root)):
            _fail("CUTOVER_ROOT_NOT_APPROVED")
        for parent in (root, *root.parents):
            if parent.exists() and (parent.is_symlink() or parent.is_junction()):
                _fail("CUTOVER_ROOT_NOT_APPROVED")
        url = connection.engine.url
        if (url.host, url.port or 5432, url.database, url.username) != (
                expected.host, expected.port, expected.database, expected.user):
            _fail("CUTOVER_DATABASE_IDENTITY_MISMATCH")
        actual = connection.execute(text("SELECT current_database(),current_user,current_schema(),"
            "(SELECT system_identifier::text FROM pg_control_system())")).one()
        if tuple(actual) != (expected.database, expected.user, expected.schema_name, expected.instance_id):
            _fail("CUTOVER_DATABASE_IDENTITY_MISMATCH")
        owner = connection.scalar(text("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname=:schema"),
            {"schema": expected.schema_name})
        if owner != expected.user or not expected.schema_name.startswith("test_"):
            _fail("CUTOVER_SCHEMA_NOT_OWNED")
        return expected.schema_name

    def _window(self, *, operation_id: UUID | None = None) -> MaintenanceWindow:
        window = MaintenanceWindow.for_local_management(self.engine, profile=self.profile,
            security=self.security, token=self.token)
        window.purpose = "personal_cutover"
        window.keep_marker_on_exit = True
        window.session_lock_key = int.from_bytes(hashlib.sha256(
            _json(self.approved_database.model_dump(mode="json")).encode()).digest()[:8], "big", signed=True)
        if operation_id is not None:
            window.operation_id = operation_id
            window.resume_operation_id = operation_id
        return window

    def _rows(self, connection: Connection) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
        schema = self.approved_database.schema_name
        inspector = inspect(connection)
        tables = inspector.get_table_names(schema=schema)
        if JOURNAL not in tables:
            _fail("CUTOVER_MIGRATION_REQUIRED")
        quote = connection.dialect.identifier_preparer.quote
        rows, keys = {}, {}
        for name in tables:
            if name in {JOURNAL, "alembic_version"}:
                continue
            rows[name] = list(connection.scalars(text(
                f"SELECT row_to_json(t) FROM {quote(schema)}.{quote(name)} t")))
            keys[name] = [dict(key) for key in inspector.get_foreign_keys(name, schema=schema)]
        return rows, keys

    @staticmethod
    def _classify(rows: dict[str, list[dict]]) -> dict[str, list[dict]]:
        if not LEGACY_TABLES.issubset(rows):
            _fail("CUTOVER_SCHEMA_CHANGED")
        selected = {name: list(rows[name]) for name in LEGACY_TABLES}
        course_ids = {r["id"] for r in rows["courses"]}
        user_ids = {r["id"] for r in rows["users"]}
        for name in SHARED_OWNERS:
            chosen = []
            for row in rows[name]:
                if row["subject_id"] is not None:
                    if row["course_id"] is not None:
                        _fail("CUTOVER_MIXED_OWNERSHIP")
                    continue
                if row["course_id"] not in course_ids:
                    _fail("CUTOVER_UNKNOWN_OWNERSHIP")
                actor = row.get("uploaded_by", row.get("created_by", row.get("confirmed_by")))
                if actor is not None and actor not in user_ids:
                    _fail("CUTOVER_UNKNOWN_OWNERSHIP")
                chosen.append(row)
            selected[name] = chosen
        for name, (column, parent) in SHARED_DEPENDENTS.items():
            parent_ids = {r["id"] for r in selected[parent]}
            selected[name] = [r for r in rows[name] if r[column] in parent_ids]
        submissions = {r["id"] for r in rows["submissions"]}
        selected["workflow_runs"] = [r for r in rows["workflow_runs"] if r["submission_id"] in submissions]
        workflows = {r["workflow_id"] for r in selected["workflow_runs"]}
        selected["agent_runs"] = [r for r in rows["agent_runs"] if r["workflow_id"] in workflows]
        # Audit has no FK: role alone is not proof of object ownership.
        audit_targets = {name.removesuffix("s"): {r["id"] for r in selected[name] if "id" in r}
            for name in LEGACY_TABLES}
        audit_targets.update({"question": {r["id"] for r in selected["questions"]},
            "document": {r["id"] for r in selected["documents"]}})
        selected["audit_logs"] = [r for r in rows["audit_logs"]
            if r["actor_role"] in {"teacher", "student", "admin"}
            and r["resource_id"] in audit_targets.get(r["resource_type"], set())]
        return selected

    @staticmethod
    def _retained(rows: dict[str, list[dict]], selected: dict[str, list[dict]]) -> dict[str, list[dict]]:
        return {name: [row for row in values if row not in selected.get(name, [])]
            for name, values in rows.items() if name not in LEGACY_TABLES}

    @staticmethod
    def _hashes(retained: dict[str, list[dict]]) -> dict[str, str]:
        return {name: _digest(sorted(_json(row) for row in values)) for name, values in retained.items()}

    def _references(self, connection: Connection, retained: dict, selected: dict, keys: dict) -> list[dict]:
        # Include every retained scalar/JSON value, not only declared foreign
        # keys: frozen grading/retrieval/chat facts contain immutable JSON refs.
        identities = _old_identities(selected)
        needles = identities | {value.replace("-", "") for value in identities}
        for values in retained.values():
            for row in values:
                serialized = _json(row).lower()
                if any(value.lower() in serialized for value in needles):
                    _fail("CUTOVER_RETAINED_REFERENCE")
        cross_schema = connection.scalar(text("""SELECT count(*) FROM pg_constraint k
            JOIN pg_class target ON target.oid=k.confrelid
            JOIN pg_namespace tn ON tn.oid=target.relnamespace
            JOIN pg_class source ON source.oid=k.conrelid
            JOIN pg_namespace sn ON sn.oid=source.relnamespace
            WHERE k.contype='f' AND tn.nspname=:schema AND sn.nspname<>:schema"""),
            {"schema": self.approved_database.schema_name})
        if cross_schema:
            _fail("CUTOVER_EXTERNAL_REFERENCE")
        foreign_keys = []
        for name, entries in keys.items():
            for key in entries:
                target = key["referred_table"]
                # Old children must not consume retained personal parents.
                # A NULL subject on the child does not grant disposal rights.
                if target in retained and selected.get(name):
                    for row in selected[name]:
                        value = tuple(row[column] for column in key["constrained_columns"])
                        if all(part is not None for part in value) and any(
                                value == tuple(parent[column] for column in key["referred_columns"])
                                for parent in retained[target]):
                            _fail("CUTOVER_MIXED_OWNERSHIP")
                if name in LEGACY_TABLES or target in LEGACY_TABLES:
                    if name not in LEGACY_TABLES:
                        columns = key["constrained_columns"]
                        if len(columns) != 1 or (columns[0], target) not in LEGACY_REFERENCES.get(name, set()):
                            _fail("CUTOVER_UNAPPROVED_CONSTRAINT")
                    foreign_keys.append({"table": name, "target": target, "name": key["name"]})
        return sorted(foreign_keys, key=_json)

    def _path(self, relative: str) -> Path:
        path = environment_path(self.profile, "storage", relative)
        root = environment_path(self.profile, "storage")
        if path == root or not path.is_relative_to(root):
            _fail("CUTOVER_FILE_NOT_APPROVED")
        for candidate in (path, *path.parents):
            if candidate.exists() and (candidate.is_symlink() or candidate.is_junction()
                    or getattr(candidate.stat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                _fail("CUTOVER_FILE_NOT_APPROVED")
            if candidate == root:
                break
        return path

    def _files(self, connection: Connection, selected: dict, retained: dict) -> list[dict]:
        old_ids = _old_identities(selected)
        old_courses = {str(r["id"]) for r in selected["courses"]}
        planned: dict[str, dict] = {}
        personal_paths = set()
        with Session(bind=connection) as session:
            files = FileStorageService(session, profile=self.profile)
            for ref in storage_references(session):
                if "subject_id" in ref.owner:
                    if ref.locator:
                        personal_paths.add(files._existing_path(ref.locator, legacy=ref.legacy))
                    continue
                if ref.owner.get("course_id") not in old_courses or str(ref.resource.id) not in old_ids:
                    _fail("CUTOVER_UNKNOWN_FILE_OWNER")
                if ref.locator is None:
                    continue
                path = files._existing_path(ref.locator, legacy=ref.legacy)
                if not path.is_absolute() or not path.is_relative_to(files.root):
                    _fail("CUTOVER_FILE_NOT_APPROVED")
                relative = path.relative_to(files.root).as_posix()
                self._path(relative)
                metadata = ref.metadata
                if metadata is None or not path.is_file() or fingerprint(path) != (metadata.size_bytes, metadata.sha256):
                    _fail("CUTOVER_FILE_IDENTITY_UNKNOWN")
                planned[relative] = {"path": relative, "sha256": metadata.sha256,
                    "size": metadata.size_bytes, "file_id": ref.file_id}
            for path in self.profile.storage_root.rglob("*.receipt.json"):
                relative = path.relative_to(self.profile.storage_root).as_posix()
                self._path(relative)
                receipt = OperationReceipt.model_validate_json(path.read_bytes())
                if str(receipt.resource_id) not in old_ids:
                    if any(self._path(entry["path"]) == files.resolve_path(receipt.candidate_locator)
                            for entry in planned.values()):
                        _fail("CUTOVER_RETAINED_REFERENCE")
                    continue
                if receipt.owner.get("course_id") not in old_courses or receipt.candidate_locator not in planned:
                    _fail("CUTOVER_UNKNOWN_FILE_OWNER")
                size, digest = fingerprint(path)
                planned[relative] = {"path": relative, "sha256": digest, "size": size,
                    "file_id": "receipt:" + str(receipt.operation_id)}
        retained_json = _json(retained)
        for relative, entry in planned.items():
            if self._path(relative) in personal_paths or relative in retained_json or entry["file_id"] in retained_json:
                _fail("CUTOVER_RETAINED_REFERENCE")
        return sorted(planned.values(), key=lambda entry: entry["path"])

    def _check_receipt_references(self, plan: dict) -> None:
        # Receipts can appear without a committed DB row. A new or unclassified
        # alias is still a reference, including on reentry after a partial unlink.
        paths = {self._path(entry["path"]) for entry in plan["files"]}
        for path in self.profile.storage_root.rglob("*.receipt.json"):
            relative = path.relative_to(self.profile.storage_root).as_posix()
            self._path(relative)
            receipt = OperationReceipt.model_validate_json(path.read_bytes())
            candidate = self._path(receipt.candidate_locator)
            if candidate in paths and path not in paths:
                _fail("CUTOVER_PLAN_CHANGED")

    def prepare(self, *, writers_stopped: bool, resume_operation_id: UUID | None = None) -> dict:
        if not writers_stopped:
            _fail("MAINTENANCE_WRITERS_NOT_STOPPED")
        # Verify identity before creating a marker or any durable record.
        with self.engine.connect() as connection:
            self._identity(connection)
            if resume_operation_id is not None and connection.scalar(text(
                    "SELECT 1 FROM personal_cutovers WHERE operation_id=:id"), {"id": resume_operation_id}):
                _fail("CUTOVER_PLAN_ALREADY_PREPARED")
        with self._window(operation_id=resume_operation_id) as window:
            assert window.connection is not None
            connection = window.connection
            schema = self._identity(connection)
            try:
                rows, keys = self._rows(connection)
                selected = self._classify(rows)
                retained = self._retained(rows, selected)
                foreign_keys = self._references(connection, retained, selected, keys)
                files = self._files(connection, selected, retained)
            except BaseException as error:
                marker_path = self.profile.storage_root / MARKER
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
                marker.update(state="failed", phase="preflight", error_code=getattr(error, "code", "CUTOVER_PREFLIGHT_FAILED"))
                durable_json(marker_path, _json(marker))
                raise
            plan = {"schema": schema, "database": self.approved_database.model_dump(mode="json"),
                "root": str(self.profile.storage_root), "tables": sorted(LEGACY_TABLES),
                "selected": {name: [str(r.get("id", r.get("question_id"))) for r in values]
                    for name, values in selected.items() if name not in LEGACY_TABLES},
                "selected_hashes": self._hashes(selected), "preserved_hashes": self._hashes(retained),
                "deleted_rows": {name: len(values) for name, values in selected.items()},
                "foreign_keys": foreign_keys, "files": files}
            digest = _digest(plan)
            connection.execute(text("""INSERT INTO personal_cutovers
                (operation_id,environment_id,state,phase,plan_digest,plan,file_progress)
                VALUES (:id,:env,'prepared','prepared',:digest,CAST(:plan AS jsonb),'{}')"""),
                {"id": window.operation_id, "env": self.profile.environment_id,
                    "digest": digest, "plan": _json(plan)})
            connection.commit()
            return {"operation_id": str(window.operation_id), "digest": digest,
                "tables": plan["tables"], "deleted_rows": plan["deleted_rows"], "files": files}

    def _stored(self, connection: Connection, operation_id: UUID, digest: str) -> dict:
        row = connection.execute(text("SELECT * FROM personal_cutovers WHERE operation_id=:id"), {"id": operation_id}).mappings().one_or_none()
        if (row is None or row["environment_id"] != self.profile.environment_id or row["plan_digest"] != digest
                or _digest(row["plan"]) != digest or row["plan"]["root"] != str(self.profile.storage_root)
                or row["plan"]["database"] != self.approved_database.model_dump(mode="json")):
            _fail("CUTOVER_CONFIRMATION_MISMATCH")
        return dict(row)

    @staticmethod
    def _result(row: dict) -> dict:
        return {"operation_id": str(row["operation_id"]), "state": row["state"],
            "deleted_rows": row["plan"]["deleted_rows"], "deleted_files": len(row["plan"]["files"])}

    @staticmethod
    def _save(connection: Connection, operation_id: UUID, *, state: str, phase: str, progress: dict, error: str | None = None) -> None:
        connection.execute(text("""UPDATE personal_cutovers SET state=:state,phase=:phase,
            file_progress=CAST(:progress AS jsonb),error_code=:error,updated_at=now() WHERE operation_id=:id"""),
            {"id": operation_id, "state": state, "phase": phase, "progress": _json(progress), "error": error})
        connection.commit()

    def execute(self, operation_id: UUID | str, digest: str, *, writers_stopped: bool) -> dict:
        if not writers_stopped:
            _fail("MAINTENANCE_WRITERS_NOT_STOPPED")
        operation_id = UUID(str(operation_id))
        with self.engine.connect() as connection:
            self._identity(connection)
            stored = self._stored(connection, operation_id, digest)
            if stored["state"] == "complete" and not (self.profile.storage_root / MARKER).exists():
                return self._result(stored)
        with self._window(operation_id=operation_id) as window:
            assert window.connection is not None
            connection = window.connection
            self._identity(connection)
            stored = self._stored(connection, operation_id, digest)
            plan, progress, phase = stored["plan"], stored["file_progress"], stored["phase"]
            try:
                if phase not in {"database_retired", "complete"}:
                    rows, keys = self._rows(connection)
                    selected = self._classify(rows)
                    retained = self._retained(rows, selected)
                    foreign_keys = self._references(connection, retained, selected, keys)
                    if (self._hashes(selected) != plan["selected_hashes"]
                            or self._hashes(retained) != plan["preserved_hashes"] or foreign_keys != plan["foreign_keys"]):
                        _fail("CUTOVER_PLAN_CHANGED")
                    self._check_receipt_references(plan)
                    # Check every remaining byte before deleting the first one.
                    for entry in plan["files"]:
                        path = self._path(entry["path"])
                        if path.exists():
                            if progress.get(entry["path"]) == "deleted" or fingerprint(path) != (entry["size"], entry["sha256"]):
                                _fail("CUTOVER_PLAN_CHANGED")
                        elif progress.get(entry["path"]) not in {"deleting", "deleted"}:
                            _fail("CUTOVER_PLAN_CHANGED")
                    phase = "files_disposing"
                    for entry in plan["files"]:
                        path = self._path(entry["path"])
                        progress[entry["path"]] = "deleting"
                        self._save(connection, operation_id, state="running", phase=phase, progress=progress)
                        window.assert_quiet()
                        if path.exists():
                            if fingerprint(path) != (entry["size"], entry["sha256"]):
                                _fail("CUTOVER_PLAN_CHANGED")
                            path.unlink()
                            _checkpoint("after_file_unlink")
                        progress[entry["path"]] = "deleted"
                        self._save(connection, operation_id, state="running", phase=phase, progress=progress)
                    self._identity(connection)
                    window.assert_quiet()
                    # Local DB DDL/data/journal share ONE transaction; files do not.
                    _migration.detach_approved_foreign_keys(connection, plan["schema"], plan["foreign_keys"])
                    quote = connection.dialect.identifier_preparer.quote
                    for name in DELETE_ORDER:
                        values = plan["selected"].get(name, [])
                        column = "question_id" if name == "question_generation_metadata" else "id"
                        for identity in values:
                            connection.execute(text(f'DELETE FROM {quote(plan["schema"])}.{quote(name)} WHERE {quote(column)}=:id'), {"id": UUID(identity)})
                    _migration.retire_approved_tables(connection, plan["schema"])
                    phase = "database_retired"
                    self._save(connection, operation_id, state="running", phase=phase, progress=progress)
                    _checkpoint("after_database_commit")
                self._identity(connection)
                current, _ = self._rows(connection)
                if LEGACY_TABLES.intersection(current) or self._hashes(current) != plan["preserved_hashes"]:
                    _fail("CUTOVER_PRESERVATION_FAILED")
                if any(self._path(entry["path"]).exists() for entry in plan["files"]):
                    _fail("CUTOVER_PLAN_CHANGED")
                self._save(connection, operation_id, state="complete", phase="complete", progress=progress)
                window.keep_marker_on_exit = False
                stored["state"] = "complete"
                return self._result(stored)
            except BaseException as error:
                connection.rollback()
                # Do not store a phase that was never committed.
                actual_phase = connection.scalar(text("SELECT phase FROM personal_cutovers WHERE operation_id=:id"), {"id": operation_id})
                self._save(connection, operation_id, state="failed", phase=actual_phase,
                    progress=progress, error=getattr(error, "code", "CUTOVER_INTERRUPTED"))
                raise
