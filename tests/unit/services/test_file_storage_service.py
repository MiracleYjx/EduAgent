"""T147: owned persistent bytes, receipts and stable resource identity.

TCR: docs/test-change-record-v2.md §8; SQLite does not prove PostgreSQL locking.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from backend.app.core.database import Base
from backend.app.domain.enums import DocumentStatus, UserRole
from backend.app.models import Course, Document, KnowledgeBase, Role, User
from backend.app.services.file_storage_service import FileStorageError
from tests.unit.settings_helpers import build_test_settings


@pytest.fixture
def owned_document(tmp_path):
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        teacher = User(username="files_teacher", email="files@example.com", password_hash="unused")
        teacher.roles.append(Role(name=UserRole.TEACHER))
        session.add(teacher)
        session.flush()
        course = Course(name="文件课程", created_by=teacher.id)
        session.add(course)
        session.flush()
        kb = KnowledgeBase(name="资料", course_id=course.id)
        session.add(kb)
        session.flush()
        doc = Document(
            id=uuid4(), course_id=course.id, knowledge_base_id=kb.id,
            uploaded_by=teacher.id, original_filename="讲义.txt",
            file_format="txt", status=DocumentStatus.UPLOADED,
        )
        session.add(doc)
        session.commit()
        yield session, doc, teacher, tmp_path / "persistent"
    engine.dispose()


def file_service(session, root):
    from backend.app.services.file_storage_service import FileStorageService
    return FileStorageService(session, root=root)


def test_storage_root_is_typed_and_explicit(tmp_path):
    settings = build_test_settings(STORAGE_ROOT=tmp_path / "data")
    assert settings.storage_root == tmp_path / "data"


def test_storage_root_default_uses_user_data_for_frozen_app(monkeypatch, tmp_path):
    import sys

    from backend.app.core.config import default_storage_root
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert default_storage_root() == tmp_path / "EduAgent" / "storage"


def test_owned_bytes_metadata_receipt_and_restart(owned_document):
    session, doc, teacher, root = owned_document
    service = file_service(session, root)
    data = "课程原稿\n".encode()
    stored = service.store_document(doc, data, actor_id=teacher.id)
    assert not Path(stored.storage_path).is_absolute()
    assert stored.storage_path.startswith("uploads/")
    doc.storage_path = stored.storage_path
    doc.file_metadata = stored.metadata.model_dump(mode="json")
    session.commit()
    service.commit_receipt(stored, current_status=doc.status.value)
    receipt = json.loads(stored.receipt_path.read_text(encoding="utf-8"))
    assert receipt["stage"] == "committed"
    assert receipt["resource_id"] == str(doc.id)
    assert receipt["actor_id"] == str(teacher.id)
    assert receipt["owner"]["course_id"] == str(doc.course_id)
    assert stored.metadata.size_bytes == len(data)
    assert stored.metadata.sha256 == hashlib.sha256(data).hexdigest()
    assert stored.metadata.media_type == "text/plain"
    assert stored.metadata.migration.status == "not_required"
    session.expire_all()
    fresh = file_service(session, root)
    assert fresh.read_document(session.get(Document, doc.id)) == data
    view = fresh.get_view("d_" + doc.id.hex, actor_id=teacher.id)
    assert view.availability == "available"
    assert "storage_path" not in view.model_dump()


def test_same_filename_never_overwrites(owned_document):
    session, doc, teacher, root = owned_document
    service = file_service(session, root)
    first = service.store_document(doc, b"first", actor_id=teacher.id)
    second_doc = Document(
        id=uuid4(), course_id=doc.course_id, knowledge_base_id=doc.knowledge_base_id,
        uploaded_by=teacher.id, original_filename=doc.original_filename,
        file_format="txt", status=DocumentStatus.UPLOADED,
    )
    second = service.store_document(second_doc, b"second", actor_id=teacher.id)
    assert first.storage_path != second.storage_path
    assert service.resolve_path(first.storage_path).read_bytes() == b"first"
    assert service.resolve_path(second.storage_path).read_bytes() == b"second"


@pytest.mark.parametrize("locator", ["../secret", "uploads/../../secret", "/outside", "C:/outside", r"..\secret"])
def test_new_paths_cannot_escape_root(owned_document, locator):
    session, _doc, _teacher, root = owned_document
    from backend.app.services.file_storage_service import FileStorageError
    with pytest.raises(FileStorageError) as err:
        file_service(session, root).resolve_path(locator)
    assert err.value.code == "FILE_INVALID_PATH"


def test_missing_and_unknown_preserve_document_success(owned_document):
    session, doc, teacher, root = owned_document
    service = file_service(session, root)
    doc.status = DocumentStatus.READY
    doc.storage_path = None
    session.commit()
    unknown = service.get_view("d_" + doc.id.hex, actor_id=teacher.id)
    assert unknown.availability == "history_unknown"
    doc.storage_path = "uploads/not-there.txt"
    session.commit()
    missing = service.get_view("d_" + doc.id.hex, actor_id=teacher.id)
    assert missing.availability == "missing"
    assert doc.status == DocumentStatus.READY


def test_failed_db_reference_keeps_bytes_and_owned_receipt(owned_document):
    session, doc, teacher, root = owned_document
    service = file_service(session, root)
    stored = service.store_document(doc, b"uncommitted", actor_id=teacher.id)
    session.rollback()
    service.fail_receipt(stored, code="FILE_REFERENCE_FAILED", message="database commit failed")
    receipt = json.loads(stored.receipt_path.read_text(encoding="utf-8"))
    assert receipt["stage"] == "failed"
    assert receipt["resource_id"] == str(doc.id)
    assert receipt["error"]["code"] == "FILE_REFERENCE_FAILED"
    assert service.resolve_path(stored.storage_path).read_bytes() == b"uncommitted"


def test_write_error_does_not_create_successful_receipt(owned_document, monkeypatch):
    session, doc, teacher, root = owned_document
    service = file_service(session, root)
    import os

    from backend.app.services.file_storage_service import FileStorageError
    actual_fsync = os.fsync
    calls = 0

    def no_fsync(fd):
        nonlocal calls
        calls += 1
        if calls == 2:  # receipt persisted; only content flush fails
            raise OSError("test disk failure")
        return actual_fsync(fd)

    monkeypatch.setattr("backend.app.services.file_storage_service.os.fsync", no_fsync)
    with pytest.raises(FileStorageError):
        service.store_document(doc, b"broken", actor_id=teacher.id)
    receipts = [json.loads(p.read_text(encoding="utf-8")) for p in root.rglob("*.receipt.json")]
    assert receipts and receipts[0]["stage"] == "failed"
    assert receipts[0]["owner"]["course_id"] == str(doc.course_id)
    assert doc.storage_path is None


def test_shared_reference_and_pending_receipt_block_physical_delete(owned_document):
    session, doc, teacher, root = owned_document
    service = file_service(session, root)
    from backend.app.services.file_storage_service import FileStorageError
    stored = service.store_document(doc, b"shared", actor_id=teacher.id)
    doc.storage_path = stored.storage_path
    doc.file_metadata = stored.metadata.model_dump(mode="json")
    session.commit()
    service.commit_receipt(stored, current_status=doc.status.value)
    with pytest.raises(FileStorageError) as err:
        service.delete_unreferenced_bytes(stored.storage_path)
    assert err.value.code == "FILE_IN_USE"
    doc.storage_path = None
    session.commit()
    pending_doc = Document(
        id=uuid4(), course_id=doc.course_id, knowledge_base_id=doc.knowledge_base_id,
        uploaded_by=teacher.id, original_filename="pending.txt", file_format="txt",
        status=DocumentStatus.UPLOADED,
    )
    pending = service.store_document(pending_doc, b"pending", actor_id=teacher.id)
    with pytest.raises(FileStorageError) as err:
        service.delete_unreferenced_bytes(pending.storage_path)
    assert err.value.code == "FILE_IN_USE"
    assert service.resolve_path(stored.storage_path).is_file()


def test_symlink_escape_is_rejected(owned_document, tmp_path):
    session, _doc, _teacher, root = owned_document
    outside = tmp_path / "outside"
    outside.mkdir()
    root.mkdir()
    try:
        (root / "uploads").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Host does not permit creation of this test symlink.")
    from backend.app.services.file_storage_service import FileStorageError
    with pytest.raises(FileStorageError):
        file_service(session, root).resolve_path("uploads/file.txt")


def test_legacy_locator_read_is_exact_without_alternate_search(owned_document, tmp_path):
    session, doc, _teacher, root = owned_document
    legacy = tmp_path / "legacy.txt"
    legacy.write_bytes(b"legacy")
    doc.storage_path = str(legacy)
    session.commit()
    assert file_service(session, root).read_document(doc) == b"legacy"
    legacy.unlink()
    (root / "uploads").mkdir(parents=True)
    (root / "uploads" / "legacy.txt").write_bytes(b"replacement")
    from backend.app.services.file_storage_service import FileStorageError
    with pytest.raises(FileStorageError) as err:
        file_service(session, root).read_document(doc)
    assert err.value.code == "FILE_MISSING"


def test_receipt_failure_preserves_original_and_reports_both_errors(owned_document, monkeypatch):
    session, doc, teacher, root = owned_document
    service = file_service(session, root)
    from backend.app.services.file_storage_service import FileStorageError

    def no_fsync(_fd):
        raise OSError("receipt disk unavailable")

    monkeypatch.setattr("backend.app.services.file_storage_service.os.fsync", no_fsync)
    with pytest.raises(FileStorageError) as err:
        service.store_document(doc, b"never committed", actor_id=teacher.id)
    assert "收据也未能更新" in str(err.value)
    receipt = json.loads(next(root.rglob("*.receipt.json")).read_text(encoding="utf-8"))
    assert receipt["stage"] == "prepared"
    assert doc.storage_path is None


def test_actual_export_is_registered_in_exports_and_readable(owned_document):
    session, doc, teacher, root = owned_document
    service = file_service(session, root)
    export = service.create_export(
        filename="statistics.csv", content=b"count,score\n1,5\n", actor_id=teacher.id,
        course_id=doc.course_id, audience="teacher_only",
    )
    assert export.file_id.startswith("e_")
    path, _view = file_service(session, root).download(export.file_id, actor_id=teacher.id)
    assert path.is_relative_to(root / "exports")
    assert path.read_bytes() == b"count,score\n1,5\n"
    assert export.availability == "available"


def test_export_write_failure_keeps_failed_registration(owned_document, monkeypatch):
    session, doc, teacher, root = owned_document
    service = file_service(session, root)
    from sqlalchemy import select

    from backend.app.models import ExportFile
    from backend.app.services.file_storage_service import FileStorageError

    def broken_store(**_kwargs):
        raise FileStorageError("FILE_WRITE_FAILED", "fixture write failure", http_status=503)

    monkeypatch.setattr(service, "_store_bytes", broken_store)
    with pytest.raises(FileStorageError):
        service.create_export(
            filename="statistics.csv", content=b"fixture", actor_id=teacher.id,
            course_id=doc.course_id, audience="teacher_only",
        )
    exported = session.scalar(select(ExportFile))
    assert exported is not None and exported.status == "failed"
    assert exported.completed_at is not None
    assert exported.error["code"] == "FILE_WRITE_FAILED"
    assert exported.storage_path is None


def test_export_invalid_owner_does_not_write(owned_document):
    session, doc, teacher, root = owned_document
    from backend.app.services.file_storage_service import FileStorageError
    with pytest.raises(FileStorageError) as err:
        file_service(session, root).create_export(
            filename="statistics.csv", content=b"fixture", actor_id=teacher.id,
            course_id=doc.course_id, exam_id=uuid4(), audience="teacher_only",
        )
    assert err.value.code == "FILE_INVALID_INPUT"
    assert not root.exists()

def test_registered_document_identity_cannot_be_rewritten(owned_document):
    session, doc, teacher, root = owned_document
    service = file_service(session, root)
    stored = service.store_document(doc, b"original", actor_id=teacher.id)
    doc.storage_path = stored.storage_path
    doc.file_metadata = stored.metadata.model_dump(mode="json")
    session.commit()
    service.commit_receipt(stored, current_status=doc.status.value)
    with pytest.raises(FileStorageError) as error:
        service.store_document(doc, b"replacement", actor_id=teacher.id)
    assert error.value.code == "FILE_IDENTITY_IN_USE"
    assert service.read_document(doc) == b"original"
    assert len(list(root.rglob("*.receipt.json"))) == 1


def test_legacy_relative_locator_keeps_original_working_directory(owned_document, tmp_path, monkeypatch):
    session, doc, _teacher, root = owned_document
    monkeypatch.chdir(tmp_path)
    Path("legacy.txt").write_bytes(b"exact old locator")
    root.mkdir()
    (root / "legacy.txt").write_bytes(b"must not substitute")
    doc.storage_path = "legacy.txt"
    assert file_service(session, root).read_document(doc) == b"exact old locator"


def test_foreign_host_locator_is_unknown_without_substitution(owned_document):
    import os

    session, doc, teacher, root = owned_document
    doc.storage_path = "/mnt/foreign/original.txt" if os.name == "nt" else "C:/foreign/original.txt"
    session.commit()
    service = file_service(session, root)
    view = service.get_view("d_" + doc.id.hex, actor_id=teacher.id)
    assert view.availability == "history_unknown"
    assert view.migration_status == "history_unknown"
    with pytest.raises(FileStorageError) as error:
        service.download("d_" + doc.id.hex, actor_id=teacher.id)
    assert error.value.code == "FILE_HISTORY_UNKNOWN"


def test_export_reference_failure_preserves_failed_row_and_bytes(owned_document, monkeypatch):
    from sqlalchemy import select
    from sqlalchemy.exc import SQLAlchemyError

    from backend.app.models import ExportFile

    session, _doc, teacher, root = owned_document
    service = file_service(session, root)
    actual_commit = session.commit
    calls = 0

    def commit():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise SQLAlchemyError("ready reference unavailable")
        return actual_commit()

    monkeypatch.setattr(session, "commit", commit)
    with pytest.raises(FileStorageError) as error:
        service.create_export(
            filename="result.csv", content=b"real,data", actor_id=teacher.id,
            audience="teacher_only", course_id=_doc.course_id,
        )
    assert error.value.code == "FILE_REFERENCE_FAILED"
    assert error.value.current_status == "failed"
    export = session.scalars(select(ExportFile)).one()
    assert export.status == "failed" and export.error["stage"] == "reference"
    assert service.resolve_path(export.storage_path).read_bytes() == b"real,data"
    receipt = json.loads(next(root.rglob("*.receipt.json")).read_text())
    assert receipt["stage"] == "failed" and receipt["owner"]["course_id"] == str(_doc.course_id)

def test_unknown_unregistered_material_is_not_assumed_unreferenced(owned_document):
    session, _doc, _teacher, root = owned_document
    service = file_service(session, root)
    path = service.resolve_path("uploads/unknown.txt")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"unclassified original")
    with pytest.raises(FileStorageError) as error:
        service.delete_unreferenced_bytes("uploads/unknown.txt")
    assert error.value.code == "FILE_IN_USE"
    assert path.read_bytes() == b"unclassified original"


def test_committed_original_can_be_cleaned_only_after_actual_refs_removed(owned_document):
    session, doc, teacher, root = owned_document
    service = file_service(session, root)
    stored = service.store_document(doc, b"owned bytes", actor_id=teacher.id)
    doc.storage_path = stored.storage_path
    doc.file_metadata = stored.metadata.model_dump(mode="json")
    session.commit()
    service.commit_receipt(stored, current_status=doc.status.value)
    session.delete(doc)
    session.commit()
    service.delete_unreferenced_bytes(stored.storage_path)
    assert not service.resolve_path(stored.storage_path).exists()
    assert stored.receipt_path.is_file()
