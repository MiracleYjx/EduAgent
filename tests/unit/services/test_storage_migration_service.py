"""T150 failure-first cases. TCR: docs/test-change-record-v2.md §9."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError

from backend.app.domain.enums import DocumentStatus, UserRole
from backend.app.models import Document, Role, User
from backend.app.services.file_storage_service import (
    FileStorageError,
    FileStorageService,
)
from tests.unit.services.test_file_storage_service import (
    owned_document as _owned_document,
)

owned_document = _owned_document


@pytest.fixture
def migration_case(owned_document):
    session, doc, teacher, root = owned_document
    admin = User(username="maintenance_admin", email="maintenance@example.com", password_hash="unused")
    admin.roles.append(Role(name=UserRole.ADMIN))
    session.add(admin)
    source = root.parent / "legacy.txt"
    source.write_bytes(b"original material")
    doc.storage_path = str(source)
    doc.status = DocumentStatus.READY
    session.commit()
    return session, doc, teacher, admin, root, source


def migrate(session, root, actor_id):
    from backend.app.services.storage_migration_service import StorageMigrationService
    return StorageMigrationService(session, root=root).run(actor_id=actor_id)


def test_shared_references_migrate_once_keep_identity_and_original(migration_case):
    session, doc, teacher, admin, root, source = migration_case
    other = Document(id=uuid4(), course_id=doc.course_id, knowledge_base_id=doc.knowledge_base_id,
                     uploaded_by=teacher.id, original_filename="another.txt", file_format="txt",
                     storage_path=str(source), status=DocumentStatus.READY)
    session.add(other)
    session.commit()
    ids = {doc.id, other.id}
    report = migrate(session, root, admin.id)
    assert report.counts["migrated"] == 2
    assert doc.storage_path == other.storage_path
    assert not Path(doc.storage_path).is_absolute()
    assert source.read_bytes() == b"original material"
    assert (root / doc.storage_path).read_bytes() == source.read_bytes()
    assert {doc.id, other.id} == ids
    assert doc.status == other.status == DocumentStatus.READY
    for row in (doc, other):
        metadata = row.file_metadata
        assert metadata["migration"]["status"] == "migrated"
        assert metadata["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
        assert metadata["migration"]["latest_attempt"]["source_locator"] == str(source)
        assert metadata["migration"]["latest_attempt"]["committed_at"] is not None
    receipts = [json.loads(p.read_text(encoding="utf-8")) for p in root.rglob("*.receipt.json")]
    assert {r["resource_id"] for r in receipts} == {str(i) for i in ids}
    assert all(r["actor_id"] == str(admin.id) and r["stage"] == "committed" for r in receipts)
    before = set(root.rglob("*"))
    again = migrate(session, root, admin.id)
    assert again.counts["already_migrated"] == 2
    assert set(root.rglob("*")) == before
    (root / doc.storage_path).write_bytes(b"changed")
    changed = migrate(session, root, admin.id)
    assert changed.counts["failed"] == 2
    assert source.read_bytes() == b"original material"


def test_missing_unknown_are_distinct_and_do_not_change_ready(migration_case):
    session, doc, _teacher, admin, root, source = migration_case
    source.unlink()
    missing = migrate(session, root, admin.id)
    assert missing.counts["missing"] == 1
    assert doc.storage_path == str(source) and doc.status == DocumentStatus.READY
    doc.storage_path = None
    doc.file_metadata = None
    session.commit()
    unknown = migrate(session, root, admin.id)
    assert unknown.counts["history_unknown"] == 1
    assert doc.storage_path is None and doc.status == DocumentStatus.READY


def test_copy_failure_preserves_source_and_failure_receipt(migration_case, monkeypatch):
    session, doc, _teacher, admin, root, source = migration_case
    from backend.app.services import storage_migration_service as module
    def interrupted(source_path, target_path):
        target_path.write_bytes(b"partial")
        raise OSError("copy interrupted")
    monkeypatch.setattr(module, "copy_verified", interrupted)
    report = migrate(session, root, admin.id)
    assert report.counts["failed"] == 1
    assert doc.storage_path == str(source)
    assert source.read_bytes() == b"original material"
    assert doc.file_metadata["migration"]["status"] == "failed"
    assert doc.file_metadata["migration"]["latest_attempt"]["committed_at"] is None
    receipt = next(root.rglob("*.receipt.json"))
    assert json.loads(receipt.read_text(encoding="utf-8"))["stage"] == "failed"


def test_transaction_failure_preserves_source_and_old_locator(migration_case, monkeypatch):
    session, doc, _teacher, admin, root, source = migration_case
    original_commit = session.commit
    attempts = 0
    def fail_once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OperationalError("commit", {}, RuntimeError("actual injected DB failure"))
        return original_commit()
    monkeypatch.setattr(session, "commit", fail_once)
    report = migrate(session, root, admin.id)
    assert report.counts["failed"] == 1
    assert doc.storage_path == str(source)
    assert source.read_bytes() == b"original material"
    assert doc.file_metadata["migration"]["latest_attempt"]["committed_at"] is None
    assert json.loads(next(root.rglob("*.receipt.json")).read_text(encoding="utf-8"))["stage"] == "failed"
    assert FileStorageService(session, root=root).read_document(doc) == b"original material"


def test_relative_legacy_failure_remains_readable(migration_case, monkeypatch):
    session, doc, _teacher, admin, root, source = migration_case
    monkeypatch.chdir(source.parent)
    doc.storage_path = source.name
    session.commit()
    from backend.app.services import storage_migration_service as module
    monkeypatch.setattr(module, "copy_verified", lambda *_: (_ for _ in ()).throw(OSError("copy failed")))
    assert migrate(session, root, admin.id).counts["failed"] == 1
    assert FileStorageService(session, root=root).read_document(doc) == b"original material"


def test_maintenance_requires_current_active_admin(migration_case):
    session, doc, teacher, admin, root, source = migration_case
    with pytest.raises(FileStorageError) as err:
        migrate(session, root, teacher.id)
    assert err.value.code == "MAINTENANCE_FORBIDDEN"
    admin.is_active = False
    session.commit()
    with pytest.raises(FileStorageError):
        migrate(session, root, admin.id)
    assert doc.storage_path == str(source) and not root.exists()


def test_write_error_is_not_replaced_by_maintenance_receipt_error(migration_case, monkeypatch):
    session, doc, teacher, _admin, root, _source = migration_case
    import os
    doc.storage_path = None
    doc.file_metadata = None
    session.commit()
    original_fsync = os.fsync
    calls = 0
    def fail_bytes(descriptor):
        nonlocal calls
        calls += 1
        if calls == 2:
            (root / ".maintenance.json").write_text("active maintenance", encoding="utf-8")
            raise OSError("original file fsync failed")
        return original_fsync(descriptor)
    monkeypatch.setattr(os, "fsync", fail_bytes)
    with pytest.raises(FileStorageError) as error:
        FileStorageService(session, root=root).store_document(doc, b"actual bytes", actor_id=teacher.id)
    assert error.value.code == "FILE_WRITE_FAILED"
    assert isinstance(error.value.__cause__, OSError)
    assert "original file fsync failed" in str(error.value.__cause__)
    assert json.loads(next(root.rglob("*.receipt.json")).read_text(encoding="utf-8"))["stage"] == "prepared"
    assert any(path.read_bytes() == b"actual bytes" for path in root.rglob("*") if path.is_file())


def test_document_export_shared_original_migrates_to_exports(migration_case):
    session, doc, teacher, admin, root, source = migration_case
    from backend.app.models import ExportFile
    files = FileStorageService(session, root=root)
    view = files.create_export(filename="result.csv", content=source.read_bytes(), actor_id=teacher.id,
                               audience="teacher_only", course_id=doc.course_id)
    export = session.get(ExportFile, view.resource_id)
    export.storage_path = str(source)
    metadata = export.file_metadata.copy()
    metadata["migration"] = {"status": "not_migrated", "latest_attempt": None}
    export.file_metadata = metadata
    session.commit()
    report = migrate(session, root, admin.id)
    assert report.counts["migrated"] == 2
    assert doc.storage_path == export.storage_path
    assert export.storage_path.startswith("exports/")
    assert export.id == view.resource_id and doc.status == DocumentStatus.READY
    assert files.resolve_path(export.storage_path).read_bytes() == source.read_bytes()
    assert source.read_bytes() == b"original material"
