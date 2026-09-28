"""T080 站内 JWT 工具边界：身份、权限、注册与结构化结果。"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.core.database import Base
from backend.app.domain.enums import UserRole
from backend.app.domain.permissions import Permission
from backend.app.mcp.registry import ToolContext, ToolRegistry, ToolSpec
from backend.app.mcp.server import MCPToolServer
from backend.app.models import Role, User
from backend.app.services.auth_service import AuthService, create_access_token

SECRET = "test-mcp-jwt-secret-that-is-not-for-production"


class EchoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as active:
        yield active
    engine.dispose()


def _user(session: Session, role: UserRole, *, active: bool = True) -> User:
    identifier = uuid4().hex[:8]
    user = User(
        username=f"mcp-{identifier}",
        email=f"mcp-{identifier}@example.com",
        password_hash="unused-test-hash",
        is_active=active,
    )
    user.roles.append(Role(name=role))
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _server(handler: Any | None = None) -> MCPToolServer:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="echo_teacher",
            description="返回当前教师身份与查询文本。",
            permission=Permission.VIEW_COURSES,
            arguments_model=EchoArguments,
            handler=handler
            or (
                lambda context, args: {
                    "actor_id": context.actor_id,
                    "query": args.query,
                }
            ),
        )
    )
    return MCPToolServer(secret_key=SECRET, registry=registry)


def test_registry_rejects_duplicate_and_lists_only_authorized_tools(
    session: Session,
) -> None:
    teacher = _user(session, UserRole.TEACHER)
    student = _user(session, UserRole.STUDENT)
    server = _server()
    definition = server.registry.get("echo_teacher")
    assert definition is not None
    with pytest.raises(ValueError, match="已注册"):
        server.registry.register(definition)

    teacher_token = AuthService(session, secret_key=SECRET).issue_access_token(teacher)
    student_token = AuthService(session, secret_key=SECRET).issue_access_token(student)
    listed = server.list_tools(token=teacher_token, session=session)
    assert listed.ok and listed.data is not None
    assert [item["name"] for item in listed.data["tools"]] == ["echo_teacher"]
    assert "query" in listed.data["tools"][0]["arguments_schema"]["properties"]
    assert server.list_tools(token=student_token, session=session).data == {"tools": []}


def test_real_jwt_identity_reaches_tool_context_not_token_claims(
    session: Session,
) -> None:
    teacher = _user(session, UserRole.TEACHER)
    seen: list[ToolContext] = []

    def handler(context: ToolContext, args: EchoArguments) -> dict[str, Any]:
        seen.append(context)
        return {"actor_id": context.actor_id, "query": args.query}

    server = _server(handler)
    token = create_access_token(
        teacher.id,
        secret_key=SECRET,
        roles=[UserRole.STUDENT],  # 过期的角色声明不能替代数据库中的教师角色。
    )
    result = server.call_tool(
        "echo_teacher", {"query": "课程资料"}, token=token, session=session
    )

    assert result.ok and result.error is None
    assert result.data == {"actor_id": str(teacher.id), "query": "课程资料"}
    assert len(seen) == 1
    assert seen[0].actor_id == teacher.id
    assert seen[0].roles == frozenset({UserRole.TEACHER})
    assert seen[0].session is session


def test_missing_invalid_and_disabled_jwt_never_execute_tool(session: Session) -> None:
    disabled = _user(session, UserRole.TEACHER, active=False)
    token = create_access_token(disabled.id, secret_key=SECRET)
    calls: list[str] = []
    server = _server(lambda _context, _args: calls.append("ran") or {"ok": True})

    for credential in (None, "bad.jwt.value", token):
        result = server.call_tool(
            "echo_teacher", {"query": "x"}, token=credential, session=session
        )
        assert not result.ok
        assert result.error is not None and result.error.code == "TOOL_UNAUTHENTICATED"
        assert not server.list_tools(token=credential, session=session).ok
    assert calls == []


def test_permission_and_identity_spoofing_are_rejected_before_handler(
    session: Session,
) -> None:
    teacher = _user(session, UserRole.TEACHER)
    student = _user(session, UserRole.STUDENT)
    calls: list[str] = []
    server = _server(lambda _context, _args: calls.append("ran") or {"ok": True})
    teacher_token = AuthService(session, secret_key=SECRET).issue_access_token(teacher)
    student_token = AuthService(session, secret_key=SECRET).issue_access_token(student)

    denied = server.call_tool(
        "echo_teacher", {"query": "x"}, token=student_token, session=session
    )
    spoofed = server.call_tool(
        "echo_teacher",
        {"query": "x", "actor_id": str(student.id)},
        token=teacher_token,
        session=session,
    )
    malformed = server.call_tool(
        "echo_teacher", {}, token=teacher_token, session=session
    )
    missing = server.call_tool("missing", {}, token=teacher_token, session=session)

    assert denied.error is not None and denied.error.code == "TOOL_FORBIDDEN"
    assert spoofed.error is not None and spoofed.error.code == "TOOL_INVALID_ARGUMENTS"
    assert (
        malformed.error is not None and malformed.error.code == "TOOL_INVALID_ARGUMENTS"
    )
    assert missing.error is not None and missing.error.code == "TOOL_NOT_FOUND"
    assert calls == []


def test_unexpected_handler_error_is_structured_and_redacted(session: Session) -> None:
    teacher = _user(session, UserRole.TEACHER)

    def handler(_context: ToolContext, _args: EchoArguments) -> dict[str, Any]:
        raise RuntimeError("private database connection string")

    server = _server(handler)
    token = AuthService(session, secret_key=SECRET).issue_access_token(teacher)
    result = server.call_tool(
        "echo_teacher", {"query": "x"}, token=token, session=session
    )

    assert not result.ok and result.data is None
    assert result.error is not None and result.error.code == "TOOL_EXECUTION_FAILED"
    assert "private" not in result.model_dump_json()
