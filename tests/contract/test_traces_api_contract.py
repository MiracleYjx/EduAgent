"""T085 JWT-scoped trace view contract."""

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.core.app import create_app
from backend.app.core.database import Base, get_db
from backend.app.domain.enums import SubmissionStatus, UserRole, WorkflowStatus
from backend.app.models import (
    AgentRun,
    Course,
    Exam,
    Role,
    Submission,
    User,
    WorkflowRun,
)
from backend.app.services.auth_service import create_access_token
from tests.unit.settings_helpers import build_test_settings

SECRET = "trace-test-secret-with-enough-length"


@pytest.fixture
def environment() -> Generator[tuple[TestClient, dict[str, User]], None, None]:
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    roles = {role: Role(name=role, description=role.value) for role in UserRole}
    users = {
        name: User(
            username=name, email=f"{name}@example.com", password_hash="hashed",
            roles=[roles[role]],
        )
        for name, role in (
            ("teacher", UserRole.TEACHER), ("other_teacher", UserRole.TEACHER),
            ("student", UserRole.STUDENT), ("other_student", UserRole.STUDENT),
            ("admin", UserRole.ADMIN),
        )
    }
    course = Course(name="Trace Course", creator=users["teacher"])
    exam = Exam(title="Trace Exam", course=course, creator=users["teacher"])
    submission = Submission(
        exam=exam, student=users["student"], status=SubmissionStatus.SUBMITTED,
    )
    session.add_all(users.values())
    session.add(submission)
    session.flush()
    run = WorkflowRun(
        workflow_id="wf-trace-1", request_id="request-trace-1",
        submission_id=submission.id, current_node="confidence_check",
        status=WorkflowStatus.PAUSED,
    )
    session.add(run)
    session.add(AgentRun(
        agent_type="llm", workflow_id=run.workflow_id,
        request_id=run.request_id, user_id=users["teacher"].id,
        status="failure", model="deepseek-chat", latency_ms=15,
        prompt_version="grading-v1", input_tokens=10, output_tokens=2,
        total_tokens=12, input_summary="Authorization Bearer private-key",
        output_summary="student answer private", error_code="ProviderFailed",
        error_message="Prompt and private student answer", error_retryable=True,
    ))
    session.commit()
    app = create_app(settings=build_test_settings(JWT_SECRET_KEY=SECRET))
    app.dependency_overrides[get_db] = lambda: (yield session)
    try:
        yield TestClient(app), users
    finally:
        app.dependency_overrides.clear()
        session.close()
        engine.dispose()


def _headers(user: User, role: UserRole) -> dict[str, str]:
    token = create_access_token(user.id, secret_key=SECRET, roles=[role])
    return {"Authorization": f"Bearer {token}"}


def test_trace_view_by_workflow_and_request_is_sanitized(environment) -> None:
    client, users = environment
    headers = _headers(users["teacher"], UserRole.TEACHER)
    for parameter in ("workflow_id=wf-trace-1", "request_id=request-trace-1"):
        response = client.get(f"/api/traces/?{parameter}", headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert body["workflow_runs"][0]["current_node"] == "confidence_check"
        assert body["events"][0]["request_id"] == "request-trace-1"
        assert body["events"][0]["workflow_id"] == "wf-trace-1"
        assert body["events"][0]["tokens"] == {"input": 10, "output": 2, "total": 12}
        assert body["events"][0]["error"] == {
            "code": "ProviderFailed", "message": "调用失败", "retryable": True,
        }
        serialized = response.text
        assert "private-key" not in serialized
        assert "student answer" not in serialized
        assert "Prompt and private" not in serialized


def test_trace_view_teacher_student_admin_ownership(environment) -> None:
    client, users = environment
    query = "/api/traces/?workflow_id=wf-trace-1"
    assert client.get(query).status_code == 401
    assert client.get(query, headers=_headers(users["other_teacher"], UserRole.TEACHER)).status_code == 403
    assert client.get(query, headers=_headers(users["other_student"], UserRole.STUDENT)).status_code == 403
    assert client.get(query, headers=_headers(users["student"], UserRole.STUDENT)).status_code == 200
    assert client.get(query, headers=_headers(users["admin"], UserRole.ADMIN)).status_code == 200
    assert client.get("/api/traces/", headers=_headers(users["teacher"], UserRole.TEACHER)).status_code == 422
