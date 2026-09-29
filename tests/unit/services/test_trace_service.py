"""T085 AgentRun trace privacy, propagation and retention tests."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.models import AgentRun
from backend.app.services.trace_service import (
    TraceService,
    bind_trace,
    record_trace,
    safe_trace_error_code,
    trace_prompt_version,
)


@pytest.fixture
def engine():
    database = create_engine("sqlite:///:memory:", poolclass=StaticPool)
    AgentRun.__table__.create(database)
    yield database
    database.dispose()


def test_trace_records_only_allowlisted_metadata_and_propagates_ids(engine) -> None:
    actor = uuid4()
    service = TraceService(lambda: Session(engine))
    with (
        bind_trace(request_id="request-1", user_id=actor, workflow_id=None, service=service),
        trace_prompt_version("grading-v1"),
    ):
        assert record_trace(
            agent_type="llm", status="success", model="deepseek-chat",
            tokens={"input": 12, "output": 8, "total": 20},
            input_summary="Authorization:Bearer secret-key",
            output_summary="student answer is private",
        )
    with Session(engine) as session:
        row = session.scalar(select(AgentRun))
        assert row is not None
        assert row.request_id == "request-1"
        assert row.user_id == actor
        assert row.workflow_id is None
        assert row.model == "deepseek-chat"
        assert row.prompt_version == "grading-v1"
        assert (row.input_tokens, row.output_tokens, row.total_tokens) == (12, 8, 20)
        assert row.input_summary is None and row.output_summary is None
        assert "secret-key" not in repr(row.__dict__)


def test_trace_failure_never_persists_raw_error_message(engine) -> None:
    service = TraceService(lambda: Session(engine))
    with bind_trace(request_id="request-2", user_id=None, workflow_id=None, service=service):
        assert record_trace(
            agent_type="llm", status="failure", error_code="ProviderFailed",
            error_retryable=True, model="secret prompt containing spaces",
            tokens={"input": None, "output": None, "total": None},
        )
    with Session(engine) as session:
        row = session.scalar(select(AgentRun))
        assert row is not None
        assert row.error_code == "ProviderFailed"
        assert row.error_message == "调用失败"
        assert row.error_retryable is True
        assert row.model is None
        assert (row.input_tokens, row.output_tokens, row.total_tokens) == (None, None, None)
    assert safe_trace_error_code("secretcredential123") == "TraceFailure"


def test_trace_retention_is_30_days_and_does_not_delete_new_rows(engine) -> None:
    now = datetime.now(UTC)
    with Session(engine) as session:
        for age in (31, 29):
            session.add(AgentRun(
                agent_type="llm", request_id=f"request-{age}", status="success",
                created_at=now - timedelta(days=age),
            ))
        session.commit()
    service = TraceService(lambda: Session(engine))
    assert service.purge_expired(now=now) == 1
    with Session(engine) as session:
        assert [row.request_id for row in session.scalars(select(AgentRun))] == ["request-29"]


def test_trace_write_failure_does_not_escape_to_business_caller() -> None:
    def unavailable_session() -> Session:
        raise RuntimeError("private database connection detail")

    service = TraceService(unavailable_session)
    with bind_trace(request_id="request-3", user_id=None, workflow_id=None, service=service):
        assert not record_trace(agent_type="llm", status="failure")
