"""T148 owned file GET contract; real JWT, isolated SQL and bytes.

TCR: docs/test-change-record-v2.md §8.
"""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.core.database import Base, get_db
from backend.app.domain.enums import DocumentStatus, UserRole
from backend.app.models import (
    Course,
    Document,
    KnowledgeBase,
    Role,
    User,
)
from backend.app.services.auth_service import AuthService
from backend.app.services.file_storage_service import (
    FileStorageService,
)
from tests.support.retained_component_app import component_app as create_app
from tests.unit.settings_helpers import build_test_settings


@pytest.fixture
def files_api(tmp_path):
    from tests.postgres_helpers import isolated_test_resource_profile
    with isolated_test_resource_profile((tmp_path / "file-profile").resolve()) as profile:
        yield from _files_api(profile.storage_root)

def _files_api(root):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    settings = build_test_settings(STORAGE_ROOT=root)
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
        app = create_app(settings=settings, routers=('file_storage', 'knowledge_bases', 'questions', 'question_assets', 'content_validation', 'image_understanding', 'paper_import'))

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
