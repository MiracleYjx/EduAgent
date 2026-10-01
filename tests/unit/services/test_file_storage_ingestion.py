"""T149 original-file persistence shared by API and UI service callers.

TCR: docs/test-change-record-v2.md §8.
"""
import json
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from backend.app.models import Document
from backend.app.services.file_storage_service import (
    FileStorageError,
    FileStorageService,
)
from backend.app.services.knowledge_base_service import (
    KnowledgeBaseService,
    KnowledgeBaseServiceError,
)
from tests.unit.services import test_file_storage_service as file_fixtures

owned_document = file_fixtures.owned_document


def test_byte_upload_has_relative_original_and_fresh_reader(owned_document):
    session, doc, teacher, root = owned_document
    service = KnowledgeBaseService(session, storage_root=root)
    result = service.upload_document(
        knowledge_base_id=doc.knowledge_base_id, uploaded_by=teacher.id,
        teacher_id=teacher.id, original_filename="原稿.md", content=b"# actual original\n",
    )
    assert result.file_id == "d_" + UUID(result.id).hex
    assert result.storage_path.startswith("uploads/")
    session.expire_all()
    saved = session.get(Document, UUID(result.id))
    assert saved.file_metadata["migration"]["status"] == "not_required"
    fresh = KnowledgeBaseService(session, storage_root=root)
    assert fresh._read_document_content(saved, None) == b"# actual original\n"


def test_other_course_upload_is_rejected_before_file_writes(owned_document):
    session, doc, _teacher, root = owned_document
    from backend.app.domain.enums import UserRole
    from backend.app.models import Role, User
    from backend.app.services.knowledge_base_service import KnowledgeBasePermissionError
    outsider = User(username="outsider", email="outsider@example.com", password_hash="unused")
    outsider.roles.append(session.scalar(select(Role).where(Role.name == UserRole.TEACHER)))
    session.add(outsider)
    session.commit()
    with pytest.raises(KnowledgeBasePermissionError):
        KnowledgeBaseService(session, storage_root=root).upload_document(
            knowledge_base_id=doc.knowledge_base_id, uploaded_by=outsider.id,
            teacher_id=outsider.id, original_filename="file.txt", content=b"no write",
        )
    assert not root.exists()


def test_db_failure_leaves_owned_material_and_no_document_success(owned_document, monkeypatch):
    session, doc, teacher, root = owned_document
    service = KnowledgeBaseService(session, storage_root=root)

    def fail_commit():
        raise SQLAlchemyError("fixture database failure")

    monkeypatch.setattr(session, "commit", fail_commit)
    with pytest.raises(KnowledgeBaseServiceError) as error:
        service.upload_document(
            knowledge_base_id=doc.knowledge_base_id, uploaded_by=teacher.id,
            teacher_id=teacher.id, original_filename="file.txt", content=b"keep",
        )
    assert "失败" in str(error.value)
    receipts = [json.loads(p.read_text(encoding="utf-8")) for p in root.rglob("*.receipt.json")]
    assert len(receipts) == 1 and receipts[0]["stage"] == "failed"
    assert receipts[0]["owner"]["course_id"] == str(doc.course_id)
    assert len(list(session.scalars(select(Document)))) == 1  # only original fixture row
    path = FileStorageService(session, root=root).resolve_path(receipts[0]["candidate_locator"])
    assert path.read_bytes() == b"keep"


def test_native_ingestion_does_not_substitute_changed_bytes(owned_document):
    session, doc, teacher, root = owned_document
    service = KnowledgeBaseService(session, storage_root=root)
    result = service.upload_document(
        knowledge_base_id=doc.knowledge_base_id, uploaded_by=teacher.id,
        teacher_id=teacher.id, original_filename="file.txt", content=b"original",
    )
    saved = session.get(Document, UUID(result.id))
    from backend.app.services.knowledge_base_service import DocumentValidationError
    with pytest.raises(DocumentValidationError):
        service._read_document_content(saved, b"substitute")


def test_receipt_failure_after_commit_reports_committed_resource(owned_document, monkeypatch):
    session, doc, teacher, root = owned_document
    service = KnowledgeBaseService(session, storage_root=root)
    original = service.files._update_receipt

    def fail_only_committed(stored, *, stage, error=None):
        if stage == "committed":
            raise OSError("fixture receipt failure")
        return original(stored, stage=stage, error=error)

    monkeypatch.setattr(service.files, "_update_receipt", fail_only_committed)
    with pytest.raises(FileStorageError) as err:
        service.upload_document(
            knowledge_base_id=doc.knowledge_base_id, uploaded_by=teacher.id,
            teacher_id=teacher.id, original_filename="file.txt", content=b"committed",
        )
    assert err.value.code == "FILE_RECEIPT_UPDATE_FAILED"
    assert err.value.current_status == "Uploaded"
    assert len(list(session.scalars(select(Document)))) == 2
    receipt = json.loads(next(root.rglob("*.receipt.json")).read_text(encoding="utf-8"))
    assert receipt["stage"] == "written"
