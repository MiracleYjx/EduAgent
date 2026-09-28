"""T083 email tool contract through real JWT-backed registration and invocation."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.app.core.database import Base
from backend.app.domain.enums import UserRole
from backend.app.mcp.registry import ToolRegistry
from backend.app.mcp.server import MCPToolServer
from backend.app.mcp.tools.email_tool import (
    EmailDeliveryStatus,
    EmailDispatch,
    EmailSendResult,
    EmailTemplate,
    NoopEmailSender,
    register_email_tool,
)
from backend.app.models import Role, User
from backend.app.services.auth_service import AuthService

SECRET = "test-email-tool-jwt-secret-not-for-production"


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as active:
        yield active
    engine.dispose()


def _user(session: Session, role: UserRole) -> User:
    suffix = uuid4().hex[:8]
    user = User(
        username=f"email-{role.value}-{suffix}",
        email=f"email-{suffix}@example.com",
        password_hash="unused-test-hash",
    )
    existing = session.scalar(select(Role).where(Role.name == role))
    user.roles.append(existing or Role(name=role))
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _token(session: Session, user: User) -> str:
    return AuthService(session, secret_key=SECRET).issue_access_token(user)


def _server(sender: object | None = None) -> MCPToolServer:
    registry = ToolRegistry()
    register_email_tool(registry, sender=sender)
    return MCPToolServer(secret_key=SECRET, registry=registry)


def _send(
    server: MCPToolServer,
    session: Session,
    user: User,
    *,
    recipient: str = "recipient@example.org",
    template: str = "exam_result_ready",
):
    return server.call_tool(
        "send_email",
        {"recipient": recipient, "template": template},
        token=_token(session, user),
        session=session,
    )


def test_default_noop_never_claims_delivery(session: Session) -> None:
    teacher = _user(session, UserRole.TEACHER)
    server = _server()
    result = _send(server, session, teacher)
    assert isinstance(
        NoopEmailSender().send(
            EmailDispatch(
                actor_id=teacher.id,
                recipient="recipient@example.org",
                template=EmailTemplate.EXAM_RESULT_READY,
            )
        ),
        EmailSendResult,
    )
    assert result.ok and result.data is not None
    assert result.data["status"] == "not_configured"
    assert result.data["sent"] is False
    assert "smtp" not in str(result.data).lower()


@pytest.mark.parametrize(
    "recipient",
    [
        "not-an-email",
        "nobody@",
        "a@b",
        "a@b.com\r\nBcc:evil@x.com",
        " user@example.org ",
    ],
)
def test_invalid_recipient_has_specific_error_and_never_calls_sender(
    session: Session, recipient: str
) -> None:
    teacher = _user(session, UserRole.TEACHER)
    sender = FakeEmailSender()
    result = _send(_server(sender), session, teacher, recipient=recipient)
    assert not result.ok and result.error is not None
    assert result.error.code == "EMAIL_RECIPIENT_INVALID"
    assert sender.calls == []


def test_unknown_template_has_specific_error_and_never_calls_sender(
    session: Session,
) -> None:
    teacher = _user(session, UserRole.TEACHER)
    sender = FakeEmailSender()
    result = _send(_server(sender), session, teacher, template="not_a_template")
    assert not result.ok and result.error is not None
    assert result.error.code == "EMAIL_TEMPLATE_UNKNOWN"
    assert sender.calls == []


def test_teacher_and_admin_can_discover_but_student_is_denied(
    session: Session,
) -> None:
    teacher = _user(session, UserRole.TEACHER)
    admin = _user(session, UserRole.ADMIN)
    student = _user(session, UserRole.STUDENT)
    server = _server()
    for user in (teacher, admin):
        listing = server.list_tools(token=_token(session, user), session=session)
        assert [item["name"] for item in listing.data["tools"]] == ["send_email"]
        assert _send(server, session, user).data["status"] == "not_configured"
    assert server.list_tools(token=_token(session, student), session=session).data == {
        "tools": []
    }
    denied = _send(server, session, student)
    assert not denied.ok and denied.error.code == "TOOL_FORBIDDEN"
    unauthenticated = server.call_tool(
        "send_email",
        {"recipient": "recipient@example.org", "template": "exam_result_ready"},
        token=None,
        session=session,
    )
    assert unauthenticated.error.code == "TOOL_UNAUTHENTICATED"


class FakeEmailSender:
    def __init__(self, status: EmailDeliveryStatus = EmailDeliveryStatus.SENT) -> None:
        self.status = status
        self.calls: list[EmailDispatch] = []

    def send(self, dispatch: EmailDispatch) -> EmailSendResult:
        self.calls.append(dispatch)
        return EmailSendResult(status=self.status)


def test_injected_sender_receives_trusted_actor_recipient_and_template(
    session: Session,
) -> None:
    admin = _user(session, UserRole.ADMIN)
    sender = FakeEmailSender()
    result = _send(
        _server(sender),
        session,
        admin,
        recipient="learner@example.org",
        template="review_reminder",
    )
    assert result.ok and result.data["status"] == "sent"
    assert result.data["sent"] is True
    assert sender.calls == [
        EmailDispatch(
            actor_id=admin.id,
            recipient="learner@example.org",
            template=EmailTemplate.REVIEW_REMINDER,
        )
    ]


def test_sender_failure_and_exception_never_leak_or_claim_sent(
    session: Session,
) -> None:
    teacher = _user(session, UserRole.TEACHER)
    failed = _send(
        _server(FakeEmailSender(EmailDeliveryStatus.FAILED)), session, teacher
    )
    assert failed.ok and failed.data["status"] == "failed"
    assert failed.data["sent"] is False

    class BrokenEmailSender:
        def send(self, dispatch: EmailDispatch) -> EmailSendResult:
            raise RuntimeError(
                "smtp://mail.internal/key=TOP_SECRET C:\\private\\config"
            )

    broken = _send(_server(BrokenEmailSender()), session, teacher)
    assert not broken.ok and broken.error.code == "EMAIL_SEND_FAILED"
    serialized = broken.model_dump_json()
    assert "TOP_SECRET" not in serialized
    assert "mail.internal" not in serialized
    assert "private" not in serialized
    assert "smtp" not in serialized.lower()
