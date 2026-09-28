"""T083 email tool contract; the default adapter never sends real mail."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from backend.app.domain.enums import UserRole
from backend.app.domain.permissions import Permission
from backend.app.mcp.registry import (
    ToolContext,
    ToolExecutionError,
    ToolRegistry,
    ToolSpec,
)

_EMAIL_PATTERN = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+"
)
_ALLOWED_ROLES = frozenset({UserRole.TEACHER, UserRole.ADMIN})


class EmailTemplate(StrEnum):
    """Named templates only; no free-form body or generated text is accepted."""

    EXAM_RESULT_READY = "exam_result_ready"
    REVIEW_REMINDER = "review_reminder"


class EmailDeliveryStatus(StrEnum):
    NOT_CONFIGURED = "not_configured"
    SENT = "sent"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class EmailDispatch:
    """Trusted actor is supplied by the JWT tool context, never by arguments."""

    actor_id: UUID
    recipient: str
    template: EmailTemplate


@dataclass(frozen=True, slots=True)
class EmailSendResult:
    """A sender may report SENT only after its own delivery operation succeeds."""

    status: EmailDeliveryStatus


class EmailSender(Protocol):
    """Replaceable delivery adapter; SMTP/cloud implementations are out of scope."""

    def send(self, dispatch: EmailDispatch) -> EmailSendResult: ...


class NoopEmailSender:
    """Explicitly unconfigured default: never claims delivery or does I/O."""

    def send(self, dispatch: EmailDispatch) -> EmailSendResult:
        return EmailSendResult(status=EmailDeliveryStatus.NOT_CONFIGURED)


class SendEmailArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipient: str
    template: str


def _valid_recipient(recipient: str) -> bool:
    if len(recipient) > 254 or _EMAIL_PATTERN.fullmatch(recipient) is None:
        return False
    local_part = recipient.partition("@")[0]
    return not (
        len(local_part) > 64
        or local_part.startswith(".")
        or local_part.endswith(".")
        or ".." in local_part
    )


def _send_email(
    context: ToolContext, args: SendEmailArguments, sender: EmailSender
) -> dict[str, str | bool]:
    if not context.roles.intersection(_ALLOWED_ROLES):
        raise ToolExecutionError("TOOL_FORBIDDEN", "只有教师和管理员可调用邮件工具。")
    if not _valid_recipient(args.recipient):
        raise ToolExecutionError("EMAIL_RECIPIENT_INVALID", "收件人邮箱地址无效。")
    try:
        template = EmailTemplate(args.template)
    except ValueError as error:
        raise ToolExecutionError(
            "EMAIL_TEMPLATE_UNKNOWN", "邮件模板不存在。"
        ) from error

    dispatch = EmailDispatch(
        actor_id=context.actor_id,
        recipient=args.recipient,
        template=template,
    )
    try:
        outcome = sender.send(dispatch)
    except Exception as error:  # Never expose adapter credentials or paths.
        raise ToolExecutionError("EMAIL_SEND_FAILED", "邮件发送失败。") from error
    if not isinstance(outcome, EmailSendResult) or not isinstance(
        outcome.status, EmailDeliveryStatus
    ):
        raise ToolExecutionError("EMAIL_SEND_FAILED", "邮件发送失败。")

    return {
        "status": outcome.status.value,
        "sent": outcome.status is EmailDeliveryStatus.SENT,
        "template": template.value,
        "message": {
            EmailDeliveryStatus.NOT_CONFIGURED: "邮件服务未配置，未发送。",
            EmailDeliveryStatus.SENT: "邮件适配器确认已发送。",
            EmailDeliveryStatus.FAILED: "邮件发送失败。",
        }[outcome.status],
    }


def register_email_tool(
    registry: ToolRegistry, *, sender: EmailSender | None = None
) -> None:
    """Register one tool; registry permissions are coarse, role allowlist is exact."""

    effective_sender = sender if sender is not None else NoopEmailSender()
    registry.register(
        ToolSpec(
            name="send_email",
            description="向明确收件人发送指定模板邮件；默认未配置且不发送。",
            permission=Permission.VIEW_EXAM_RESULTS,
            any_of_permissions=(Permission.VIEW_SYSTEM_STATUS,),
            arguments_model=SendEmailArguments,
            handler=lambda context, args: _send_email(context, args, effective_sender),
        )
    )


__all__ = [
    "EmailDeliveryStatus",
    "EmailDispatch",
    "EmailSendResult",
    "EmailSender",
    "EmailTemplate",
    "NoopEmailSender",
    "SendEmailArguments",
    "register_email_tool",
]
