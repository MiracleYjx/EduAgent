"""JWT-authorized, sanitized read view over AgentRun and WorkflowRun."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.security import CurrentUser, get_user_roles
from backend.app.models import AgentRun, WorkflowRun
from backend.app.services.trace_service import (
    TracePermissionError,
    TraceService,
    safe_trace_error_code,
    safe_trace_summary,
)

router = APIRouter(prefix="/api/traces", tags=["Agent Trace"])


class TraceErrorDTO(BaseModel):
    code: str
    message: str
    retryable: bool | None


class TraceTokensDTO(BaseModel):
    input: int | None
    output: int | None
    total: int | None


class TraceEventDTO(BaseModel):
    request_id: str
    user_id: UUID | None
    workflow_id: str | None
    layer: str
    model: str | None
    latency_ms: int | None
    prompt_version: str | None
    tokens: TraceTokensDTO
    status: str
    error: TraceErrorDTO | None
    input_summary: str | None
    output_summary: str | None
    created_at: datetime


class WorkflowTraceDTO(BaseModel):
    workflow_id: str
    request_id: str
    status: str
    current_node: str | None
    updated_at: datetime


class TraceQueryResponse(BaseModel):
    workflow_runs: list[WorkflowTraceDTO]
    events: list[TraceEventDTO]


def get_trace_service(session: Annotated[Session, Depends(get_db)]) -> TraceService:
    return TraceService(lambda: Session(bind=session.get_bind()))


TraceServiceDependency = Annotated[TraceService, Depends(get_trace_service)]


def _event_dto(row: AgentRun) -> TraceEventDTO:
    # Never serialize legacy free-text summary/error columns without rechecking.
    return TraceEventDTO(
        request_id=row.request_id,
        user_id=row.user_id,
        workflow_id=row.workflow_id,
        layer=row.agent_type,
        model=row.model,
        latency_ms=row.latency_ms,
        prompt_version=row.prompt_version,
        tokens=TraceTokensDTO(
            input=row.input_tokens, output=row.output_tokens, total=row.total_tokens,
        ),
        status=row.status,
        error=(
            TraceErrorDTO(
                code=safe_trace_error_code(row.error_code), message="调用失败",
                retryable=row.error_retryable,
            ) if row.status == "failure" else None
        ),
        input_summary=safe_trace_summary(row.input_summary),
        output_summary=safe_trace_summary(row.output_summary),
        created_at=row.created_at,
    )


def _workflow_dto(row: WorkflowRun) -> WorkflowTraceDTO:
    return WorkflowTraceDTO(
        workflow_id=row.workflow_id,
        request_id=row.request_id,
        status=row.status.value,
        current_node=row.current_node,
        updated_at=row.updated_at,
    )


@router.get("/", response_model=TraceQueryResponse)
def query_traces(
    user: CurrentUser,
    service: TraceServiceDependency,
    workflow_id: str | None = None,
    request_id: str | None = None,
) -> TraceQueryResponse:
    """Query one workflow or request; never expose checkpoints or input content."""

    if bool(workflow_id) == bool(request_id):
        raise HTTPException(status_code=422, detail="必须且只能提供 workflow_id 或 request_id。")
    try:
        runs, events = service.query(
            workflow_id=workflow_id, request_id=request_id,
            actor_id=user.id, roles=get_user_roles(user),
        )
    except TracePermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from None
    return TraceQueryResponse(
        workflow_runs=[_workflow_dto(row) for row in runs],
        events=[_event_dto(row) for row in events],
    )
