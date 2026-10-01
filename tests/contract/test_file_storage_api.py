"""T148 owned file GET contract; real JWT, isolated SQL and bytes.

TCR: docs/test-change-record-v2.md §8.
"""
from datetime import UTC, datetime
from uuid import UUID, uuid4

import gradio as gr
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.core.app import create_app
from backend.app.core.database import Base, get_db
from backend.app.domain.enums import DocumentStatus, UserRole
from backend.app.models import (
    Course,
    Document,
    Exam,
    ExportFile,
    KnowledgeBase,
    Role,
    Submission,
    User,
)
from backend.app.services.auth_service import AuthService
from backend.app.services.file_storage_service import (
    FileStorageError,
    FileStorageService,
)
from tests.unit.settings_helpers import build_test_settings


@pytest.fixture
def files_api(tmp_path):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    settings = build_test_settings(STORAGE_ROOT=tmp_path / "data")
    with Session(engine, expire_on_commit=False) as session:
        teacher_role, student_role = Role(name=UserRole.TEACHER), Role(name=UserRole.STUDENT)
        users = []
        for name, role in [("owner", teacher_role), ("other", teacher_role), ("student", student_role), ("peer", student_role)]:
            user = User(username=name, email=name + "@example.com", password_hash="unused")
            user.roles.append(role)
            session.add(user)
            users.append(user)
        session.flush()
        owner = users[0]
        course = Course(name="files", created_by=owner.id)
        session.add(course)
        session.flush()
        kb = KnowledgeBase(name="materials", course_id=course.id)
        session.add(kb)
        session.flush()
        doc = Document(
            id=uuid4(), course_id=course.id, knowledge_base_id=kb.id, uploaded_by=owner.id,
            original_filename="原稿.txt", file_format="txt", status=DocumentStatus.UPLOADED,
        )
        session.add(doc)
        service = FileStorageService(session, root=settings.storage_root)
        stored = service.store_document(doc, b"owned original", actor_id=owner.id)
        doc.storage_path = stored.storage_path
        doc.file_metadata = stored.metadata.model_dump(mode="json")
        session.commit()
        service.commit_receipt(stored, current_status=doc.status.value)
        auth = AuthService(session, secret_key=settings.JWT_SECRET_KEY.get_secret_value())
        headers = [{ "Authorization": "Bearer " + auth.issue_access_token(u)} for u in users]
        with gr.Blocks() as view:
            gr.Markdown("file contract")
        app = create_app(settings=settings, gradio_app=view)

        def db():
            with Session(engine, expire_on_commit=False) as request_session:
                try:
                    yield request_session
                except Exception:
                    request_session.rollback()
                    raise

        app.dependency_overrides[get_db] = db
        # Do not run production recovery handlers; this contract targets the HTTP boundary.
        client = TestClient(app)
        yield client, session, service, doc, users, headers
        client.close()
    engine.dispose()


def test_owned_document_file_get_returns_real_bytes(files_api):
    client, _session, _service, doc, _users, headers = files_api
    detail = client.get(
        f"/api/knowledge-bases/{doc.knowledge_base_id}/documents/{doc.id}", headers=headers[0],
    )
    assert detail.status_code == 200
    assert detail.json()["file_id"] == "d_" + doc.id.hex
    response = client.get("/api/files/" + detail.json()["file_id"], headers=headers[0])
    assert response.status_code == 200, response.text
    assert response.content == b"owned original"
    assert response.headers["content-type"].startswith("text/plain")
    assert "filename" in response.headers["content-disposition"]
    assert "storage_path" not in response.headers


def test_file_authentication_and_resource_ownership(files_api):
    client, _session, _service, doc, _users, headers = files_api
    path = "/api/files/d_" + doc.id.hex
    assert client.get(path).status_code == 401
    for denied in headers[1:]:
        response = client.get(path, headers=denied)
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "FILE_FORBIDDEN"


def test_missing_unknown_and_nonexistent_are_distinct(files_api):
    client, session, service, doc, _users, headers = files_api
    service.resolve_path(doc.storage_path).unlink()
    response = client.get("/api/files/d_" + doc.id.hex, headers=headers[0])
    assert response.status_code == 404 and response.json()["detail"]["code"] == "FILE_MISSING"
    assert doc.status == DocumentStatus.UPLOADED
    doc.storage_path = None
    session.commit()
    response = client.get("/api/files/d_" + doc.id.hex, headers=headers[0])
    assert response.status_code == 404 and response.json()["detail"]["code"] == "FILE_HISTORY_UNKNOWN"
    response = client.get("/api/files/d_" + uuid4().hex, headers=headers[0])
    assert response.status_code == 404 and response.json()["detail"]["code"] == "FILE_NOT_FOUND"


@pytest.mark.parametrize("state", ["writing", "failed"])
def test_unfinished_export_is_not_a_missing_successful_file(files_api, state):
    client, session, _service, doc, users, headers = files_api
    export = ExportFile(
        id=uuid4(), course_id=doc.course_id, created_by=users[0].id,
        audience="teacher_only", original_filename="report.csv", status=state,
        completed_at=datetime.now(UTC) if state == "failed" else None,
        created_at=datetime.now(UTC),
        error={"code": "EXPORT_FAILED", "message": "fixture", "stage": "write"} if state == "failed" else None,
    )
    # Fixed equal time satisfies the real lifecycle constraint.
    if state == "failed":
        export.completed_at = export.created_at
    session.add(export)
    session.commit()
    response = client.get("/api/files/e_" + export.id.hex, headers=headers[0])
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "FILE_NOT_READY"
    assert response.json()["detail"]["current_status"] == state
    assert client.get("/api/files/e_" + export.id.hex, headers=headers[2]).status_code == 403


def test_ready_submission_export_is_only_own_or_managed_course(files_api):
    client, session, _service, doc, users, headers = files_api
    exam = Exam(course_id=doc.course_id, created_by=users[0].id, title="fixture")
    session.add(exam)
    session.flush()
    submission = Submission(exam_id=exam.id, student_id=users[2].id)
    session.add(submission)
    session.flush()
    view = _service.create_export(
        filename="own.txt", content=b"real exported result", actor_id=users[0].id,
        audience="submission_owner", submission_id=submission.id,
    )
    path = "/api/files/" + view.file_id
    for allowed in (headers[0], headers[2]):
        response = client.get(path, headers=allowed)
        assert response.status_code == 200
        assert response.content == b"real exported result"
    assert client.get(path, headers=headers[1]).status_code == 403
    assert client.get(path, headers=headers[3]).status_code == 403

def test_metadata_registration_rejects_unregistered_or_external_files(files_api, tmp_path):
    client, _session, service, doc, _users, headers = files_api
    external = tmp_path / "private.txt"
    external.write_bytes(b"must not be served")
    service.root.mkdir(parents=True, exist_ok=True)
    (service.root / "unregistered.txt").write_bytes(b"not owned")
    for locator, code in [(str(external), "FILE_INVALID_PATH"), ("unregistered.txt", "FILE_NOT_FOUND")]:
        response = client.post(
            f"/api/knowledge-bases/{doc.knowledge_base_id}/documents",
            headers=headers[0],
            json={"original_filename": "new.txt", "storage_path": locator},
        )
        assert response.status_code in {404, 422}, response.text
        assert response.json()["detail"]["code"] == code


def test_metadata_registration_links_owned_bytes_and_keeps_empty_metadata(files_api):
    client, session, service, doc, _users, headers = files_api
    response = client.post(
        f"/api/knowledge-bases/{doc.knowledge_base_id}/documents",
        headers=headers[0],
        json={"original_filename": "linked.txt", "storage_path": doc.storage_path},
    )
    assert response.status_code == 201, response.text
    linked = session.get(Document, UUID(response.json()["id"]))
    assert linked is not None and linked.id != doc.id
    assert linked.storage_path == doc.storage_path
    assert linked.file_metadata == doc.file_metadata
    assert client.get("/api/files/" + response.json()["file_id"], headers=headers[0]).content == b"owned original"
    session.delete(doc)
    session.commit()
    with pytest.raises(FileStorageError) as in_use:
        service.delete_unreferenced_bytes(linked.storage_path)
    assert in_use.value.code == "FILE_IN_USE"
    empty = client.post(
        f"/api/knowledge-bases/{linked.knowledge_base_id}/documents",
        headers=headers[0], json={"original_filename": "not_uploaded.txt"},
    )
    assert empty.status_code == 201
    assert empty.json()["storage_path"] is None
    missing = client.get("/api/files/" + empty.json()["file_id"], headers=headers[0])
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "FILE_HISTORY_UNKNOWN"


def test_metadata_registration_cannot_link_another_teachers_file(files_api):
    client, session, service, doc, users, headers = files_api
    course = Course(name="other", created_by=users[1].id)
    session.add(course)
    session.flush()
    kb = KnowledgeBase(name="other", course_id=course.id)
    session.add(kb)
    session.flush()
    foreign = Document(
        id=uuid4(), course_id=course.id, knowledge_base_id=kb.id, uploaded_by=users[1].id,
        original_filename="other.txt", file_format="txt", status=DocumentStatus.UPLOADED,
    )
    stored = service.store_document(foreign, b"other teacher", actor_id=users[1].id)
    foreign.storage_path = stored.storage_path
    foreign.file_metadata = stored.metadata.model_dump(mode="json")
    session.add(foreign)
    session.commit()
    service.commit_receipt(stored, current_status=foreign.status.value)
    response = client.post(
        f"/api/knowledge-bases/{doc.knowledge_base_id}/documents",
        headers=headers[0],
        json={"original_filename": "stolen.txt", "storage_path": foreign.storage_path},
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "FILE_FORBIDDEN"

def test_linked_identity_history_protects_shared_original_after_metadata_deletion(files_api):
    from backend.app.domain.enums import QuestionType
    from backend.app.models import Question, QuestionSourceChunk

    client, session, service, doc, users, headers = files_api
    response = client.post(
        f"/api/knowledge-bases/{doc.knowledge_base_id}/documents",
        headers=headers[0],
        json={"original_filename": "shared.txt", "storage_path": doc.storage_path},
    )
    assert response.status_code == 201, response.text
    linked_id = UUID(response.json()["id"])
    locator = doc.storage_path
    question = Question(
        course_id=doc.course_id, type=QuestionType.SINGLE_CHOICE,
        content="retained source question", reference_answer="A",
        score=1, created_by=users[0].id,
    )
    session.add(question)
    session.flush()
    session.add(QuestionSourceChunk(
        question_id=question.id, chunk_id=uuid4(), document_id=linked_id,
        course_id=doc.course_id, source_order=0, content_snapshot="owned original",
        source_file="shared.txt", chunk_index=0,
    ))
    linked = session.get(Document, linked_id)
    session.delete(linked)
    session.delete(doc)
    session.commit()
    with pytest.raises(FileStorageError) as error:
        service.delete_unreferenced_bytes(locator)
    assert error.value.code == "FILE_IN_USE"
    assert service.resolve_path(locator).read_bytes() == b"owned original"
