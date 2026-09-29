"""Sanitized AgentRun traces and request-local propagation without new tables."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backend.app.domain.enums import UserRole
from backend.app.models import AgentRun, Course, Exam, Submission, WorkflowRun

LOGGER = logging.getLogger(__name__)
TRACE_RETENTION_DAYS = 30
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")
_SUMMARY = re.compile(r"^(node|branch|agent|status):[A-Za-z0-9_./-]{1,64}$")
_ERROR_CODE = re.compile(r"^(GRADING|QUESTION)_[A-Z0-9_]{1,54}$")
_PROVIDER_ERRORS = frozenset({
    "ProviderFailed", "ProviderEmptyResponse", "StructuredOutputFailed",
    "WorkflowNodeFailed",
})
_context: ContextVar[TraceContext | None] = ContextVar("eduagent_trace", default=None)


@dataclass(frozen=True, slots=True)
class TraceContext:
    request_id: str
    user_id: UUID | None
    workflow_id: str | None
    service: TraceService
    prompt_version: str | None = None


def current_trace() -> TraceContext | None:
    return _context.get()


@contextmanager
def bind_trace(
    *, request_id: str, user_id: UUID | str | None, workflow_id: str | None,
    service: TraceService,
) -> Iterator[None]:
    """Bind trusted IDs before Agent/Workflow execution; never send them to LLM."""

    context = TraceContext(
        request_id=request_id,
        user_id=UUID(str(user_id)) if user_id is not None else None,
        workflow_id=workflow_id,
        service=service,
    )
    token = _context.set(context)
    try:
        yield
    finally:
        _context.reset(token)


@contextmanager
def trace_prompt_version(version: str) -> Iterator[None]:
    """Provide only a prompt version to the provider-level recorder."""

    context = current_trace()
    if context is None:
        yield
        return
    token = _context.set(replace(context, prompt_version=version))
    try:
        yield
    finally:
        _context.reset(token)


def _safe_identifier(value: object, *, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped if len(stripped) <= maximum and _IDENTIFIER.fullmatch(stripped) else None


def safe_trace_summary(value: object) -> str | None:
    return value if isinstance(value, str) and _SUMMARY.fullmatch(value) else None


def safe_trace_error_code(value: object) -> str:
    """Accept only application-owned error-code namespaces, never upstream text."""

    if isinstance(value, str) and (
        value in _PROVIDER_ERRORS or _ERROR_CODE.fullmatch(value)
    ):
        return value
    return "TraceFailure"


def _safe_count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


class TraceService:
    """Append restricted trace metadata; failure cannot change a business outcome."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def record(
        self, *, context: TraceContext, agent_type: str, status: str,
        latency_ms: int | None = None, model: str | None = None,
        prompt_version: str | None = None, input_tokens: int | None = None,
        output_tokens: int | None = None, total_tokens: int | None = None,
        input_summary: str | None = None, output_summary: str | None = None,
        error_code: str | None = None, error_retryable: bool | None = None,
    ) -> bool:
        if status not in {"success", "failure", "pending_review"}:
            return False
        try:
            with self._session_factory() as session:
                session.add(AgentRun(
                    agent_type=_safe_identifier(agent_type, maximum=64) or "unknown",
                    request_id=context.request_id,
                    workflow_id=context.workflow_id,
                    user_id=context.user_id,
                    status=status,
                    latency_ms=_safe_count(latency_ms),
                    model=_safe_identifier(model, maximum=128),
                    prompt_version=_safe_identifier(prompt_version, maximum=64),
                    input_tokens=_safe_count(input_tokens),
                    output_tokens=_safe_count(output_tokens),
                    total_tokens=_safe_count(total_tokens),
                    input_summary=safe_trace_summary(input_summary),
                    output_summary=safe_trace_summary(output_summary),
                    error_code=safe_trace_error_code(error_code) if status == "failure" else None,
                    error_message="调用失败" if status == "failure" else None,
                    error_retryable=error_retryable if isinstance(error_retryable, bool) else None,
                ))
                session.commit()
            return True
        except Exception:  # noqa: BLE001 - trace failure must not change business result
            LOGGER.error("TRACE_WRITE_FAILED")
            return False

    def purge_expired(self, *, now: datetime | None = None) -> int:
        """Delete AgentRun traces older than 30 days; never delete WorkflowRun state."""

        cutoff = (now or datetime.now(UTC)) - timedelta(days=TRACE_RETENTION_DAYS)
        with self._session_factory() as session:
            result = session.execute(delete(AgentRun).where(AgentRun.created_at < cutoff))
            session.commit()
            return int(getattr(result, "rowcount", 0) or 0)

    def query(
        self, *, workflow_id: str | None, request_id: str | None,
        actor_id: UUID, roles: frozenset[UserRole],
    ) -> tuple[list[WorkflowRun], list[AgentRun]]:
        """Return only traces linked to the caller's own course/submission or user."""

        cutoff = datetime.now(UTC) - timedelta(days=TRACE_RETENTION_DAYS)
        with self._session_factory() as session:
            run_query = (
                select(WorkflowRun, Submission.student_id, Course.created_by)
                .join(Submission, Submission.id == WorkflowRun.submission_id)
                .join(Exam, Exam.id == Submission.exam_id)
                .join(Course, Course.id == Exam.course_id)
                .where(WorkflowRun.created_at >= cutoff)
            )
            event_query = select(AgentRun).where(AgentRun.created_at >= cutoff)
            if workflow_id is not None:
                run_query = run_query.where(WorkflowRun.workflow_id == workflow_id)
                event_query = event_query.where(AgentRun.workflow_id == workflow_id)
            if request_id is not None:
                run_query = run_query.where(WorkflowRun.request_id == request_id)
                event_query = event_query.where(AgentRun.request_id == request_id)
            run_rows = session.execute(run_query.limit(200)).all()
            events = session.scalars(event_query.order_by(AgentRun.created_at, AgentRun.id).limit(500)).all()

            def permitted(student_id: UUID, course_owner_id: UUID) -> bool:
                if UserRole.ADMIN in roles:
                    return True
                return (UserRole.TEACHER in roles and course_owner_id == actor_id) or (
                    UserRole.STUDENT in roles and student_id == actor_id
                )

            runs = [row for row, student_id, owner_id in run_rows if permitted(student_id, owner_id)]
            allowed_ids = {row.workflow_id for row in runs}
            # An event may refer to a run matched only by event ID; look up its
            # ownership without granting access based on the event's user_id.
            event_workflow_ids = {event.workflow_id for event in events if event.workflow_id}
            missing_ids = event_workflow_ids - allowed_ids
            if missing_ids:
                linked = session.execute(
                    select(WorkflowRun.workflow_id, Submission.student_id, Course.created_by)
                    .join(Submission, Submission.id == WorkflowRun.submission_id)
                    .join(Exam, Exam.id == Submission.exam_id)
                    .join(Course, Course.id == Exam.course_id)
                    .where(WorkflowRun.workflow_id.in_(missing_ids))
                    .where(WorkflowRun.created_at >= cutoff)
                ).all()
                allowed_ids.update(
                    run_id for run_id, student_id, owner_id in linked
                    if permitted(student_id, owner_id)
                )
            visible_events = [
                event for event in events
                if (event.workflow_id in allowed_ids if event.workflow_id else
                    UserRole.ADMIN in roles or event.user_id == actor_id)
            ]
            if (run_rows or events) and not (runs or visible_events):
                raise TracePermissionError("无权查看该追踪记录。")
            return runs, visible_events


class TracePermissionError(PermissionError):
    """The requested trace exists but is outside the caller's scope."""


def record_trace(
    *, agent_type: str, status: str, started_at: float | None = None,
    model: str | None = None, prompt_version: str | None = None,
    tokens: Mapping[str, Any] | None = None,
    input_summary: str | None = None, output_summary: str | None = None,
    error_code: str | None = None, error_retryable: bool | None = None,
) -> bool:
    """Record an event only when a trusted request context is bound."""

    context = current_trace()
    if context is None:
        return False
    usage = tokens or {}
    elapsed = max(0, round((perf_counter() - started_at) * 1000)) if started_at is not None else None
    return context.service.record(
        context=context, agent_type=agent_type, status=status, latency_ms=elapsed,
        model=model, prompt_version=prompt_version or context.prompt_version,
        input_tokens=usage.get("input"), output_tokens=usage.get("output"),
        total_tokens=usage.get("total"), input_summary=input_summary,
        output_summary=output_summary, error_code=error_code,
        error_retryable=error_retryable,
    )
