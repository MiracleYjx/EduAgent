"""T084 audit persistence, redaction and retention contract."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.models.audit_log import AuditLog
from backend.app.services import audit_service
from backend.app.services.audit_service import AuditService, sanitize_detail


@pytest.fixture
def engine():
    database = create_engine("sqlite:///:memory:", poolclass=StaticPool)
    AuditLog.__table__.create(database)
    yield database
    database.dispose()


def test_audit_model_has_required_columns_and_indexes(engine) -> None:
    inspector = inspect(engine)
    assert {column["name"] for column in inspector.get_columns("audit_logs")} == {
        "id", "actor_id", "actor_role", "action", "resource_type", "resource_id",
        "result", "detail", "request_id", "created_at",
    }
    assert {tuple(index["column_names"]) for index in inspector.get_indexes("audit_logs")} == {
        ("actor_id", "created_at"),
        ("resource_type", "resource_id"),
        ("action", "created_at"),
    }


def test_audit_record_sanitizes_sensitive_detail_and_rejects_updates(engine) -> None:
    actor = uuid4()
    audit = AuditService(lambda: Session(engine))
    assert audit.record(
        actor_id=actor, actor_role="teacher", action="grading.triggered",
        resource_type="submission", resource_id=uuid4(),
        detail={"run_kind": "langgraph-workflow", "api_key": "secret-key",
                "credential": "password", "answer_text": "private answer",
                "student_email": "private@example.com", "prompt": "secret prompt"},
    )
    with Session(engine) as session:
        row = session.scalar(select(AuditLog))
        assert row is not None
        assert row.actor_id == actor
        assert row.detail == {"run_kind": "langgraph-workflow"}
        assert row.created_at is not None
        row.result = "changed"
        with pytest.raises(ValueError, match="append-only"):
            session.commit()
    assert sanitize_detail({"answer_length": 12, "prompt_version": "v1",
                            "answer_sha256": "a" * 64, "free_text": "secret"}) == {
        "answer_length": 12, "prompt_version": "v1", "answer_sha256": "a" * 64,
    }


def test_audit_retention_purges_only_older_than_180_days(engine) -> None:
    now = datetime.now(UTC)
    with Session(engine) as session:
        for age in (181, 179):
            session.add(AuditLog(
                actor_role="system", action="test", resource_type="test",
                resource_id=str(age), result="success", detail={},
                created_at=now - timedelta(days=age),
            ))
        session.commit()
    audit = AuditService(lambda: Session(engine))
    assert audit.purge_expired(now=now) == 1
    with Session(engine) as session:
        assert [row.resource_id for row in session.scalars(select(AuditLog))] == ["179"]


def test_audit_failure_does_not_raise_or_leak_details(monkeypatch) -> None:
    logged: list[str] = []
    monkeypatch.setattr(audit_service.LOGGER, "error", logged.append)
    database = create_engine("sqlite:///:memory:", poolclass=StaticPool)
    try:
        audit = AuditService(lambda: Session(database))
        assert not audit.record(
            actor_id=None, actor_role="system", action="test",
            resource_type="test", resource_id="1", detail={"api_key": "secret-key"},
        )
        assert logged == ["AUDIT_WRITE_FAILED"]
        assert "secret-key" not in "".join(logged)
    finally:
        database.dispose()
