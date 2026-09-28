"""Best-effort, sanitized audit writes and explicit 180-day retention maintenance."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete
from sqlalchemy.orm import Session

from backend.app.models.audit_log import AuditLog

LOGGER = logging.getLogger(__name__)
RETENTION_DAYS = 180
_SAFE_TEXT = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SAFE_FIELDS = frozenset({
    "run_kind", "review_action", "question_status", "document_status",
    "role_change", "prompt_version", "chunk_count", "answer_length",
    "answer_sha256", "reused",
})


def sanitize_detail(detail: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep only bounded, non-free-text metadata; discard all unknown fields."""

    safe: dict[str, Any] = {}
    for key, value in (detail or {}).items():
        if key not in _SAFE_FIELDS:
            continue
        if key in {"chunk_count", "answer_length"}:
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                safe[key] = value
        elif key == "reused":
            if isinstance(value, bool):
                safe[key] = value
        elif key == "answer_sha256":
            if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
                safe[key] = value
        elif isinstance(value, str) and _SAFE_TEXT.fullmatch(value):
            safe[key] = value
    return safe


class AuditService:
    """Append an event in its own short transaction after business commit."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def record(
        self,
        *,
        actor_id: UUID | str | None,
        actor_role: str,
        action: str,
        resource_type: str,
        resource_id: UUID | str,
        result: str = "success",
        detail: Mapping[str, Any] | None = None,
        request_id: str | None = None,
    ) -> bool:
        """Never propagate an audit failure to the already completed operation."""

        try:
            with self._session_factory() as session:
                session.add(
                    AuditLog(
                        actor_id=UUID(str(actor_id)) if actor_id is not None else None,
                        actor_role=actor_role,
                        action=action,
                        resource_type=resource_type,
                        resource_id=str(resource_id),
                        result=result,
                        detail=sanitize_detail(detail),
                        request_id=request_id,
                    )
                )
                session.commit()
            return True
        except Exception:  # noqa: BLE001 - audit must never fail the business action
            LOGGER.error("AUDIT_WRITE_FAILED")
            return False

    def purge_expired(self, *, now: datetime | None = None) -> int:
        """Delete events older than 180 days; call from controlled maintenance."""

        cutoff = (now or datetime.now(UTC)) - timedelta(days=RETENTION_DAYS)
        with self._session_factory() as session:
            result = session.execute(delete(AuditLog).where(AuditLog.created_at < cutoff))
            session.commit()
            return int(getattr(result, "rowcount", 0) or 0)


def audit_after_commit(
    business_session: Session,
    *,
    actor_id: UUID | str | None,
    actor_role: str,
    action: str,
    resource_type: str,
    resource_id: UUID | str,
    detail: Mapping[str, Any] | None = None,
    request_id: str | None = None,
) -> bool:
    """Use the business session's bind, but never its transaction, for audit."""

    try:
        bind = business_session.get_bind()
        return AuditService(lambda: Session(bind=bind)).record(
            actor_id=actor_id,
            actor_role=actor_role,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            detail=detail,
            request_id=request_id,
        )
    except Exception:  # noqa: BLE001 - audit must never fail the business action
        LOGGER.error("AUDIT_WRITE_FAILED")
        return False
