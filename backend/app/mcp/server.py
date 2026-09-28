"""当前为站内 JWT 认证的工具边界；外部 MCP 传输另定合同。

调用方传入站内访问令牌和数据库会话；未来传输适配层只需负责提取令牌与会话，
不需改写注册、权限或领域工具逻辑。本模块不实现 MCP 协议消息格式。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import SecretStr
from sqlalchemy.orm import Session

from backend.app.core.security import get_user_roles
from backend.app.mcp.registry import ToolContext, ToolRegistry, ToolResult
from backend.app.services.auth_service import AuthenticationError, AuthService


class MCPToolServer:
    """先验证 JWT 并从数据库重载用户，再分派站内工具调用。"""

    def __init__(self, *, secret_key: str | SecretStr, registry: ToolRegistry) -> None:
        self._secret_key = secret_key
        self.registry = registry

    def _context(self, token: str | None, session: Session) -> ToolContext | None:
        if not token:
            return None
        try:
            user = AuthService(session, secret_key=self._secret_key).get_current_user(
                token
            )
        except AuthenticationError:
            return None
        return ToolContext(
            actor_id=user.id,
            roles=get_user_roles(user),
            session=session,
        )

    def list_tools(self, *, token: str | None, session: Session) -> ToolResult:
        """匿名或失效令牌不能发现工具；授权列表随数据库角色变化。"""

        context = self._context(token, session)
        if context is None:
            return ToolResult.failure("TOOL_UNAUTHENTICATED", "请提供有效的登录凭证。")
        return ToolResult.success({"tools": self.registry.available(context.roles)})

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        token: str | None,
        session: Session,
    ) -> ToolResult:
        """认证信息不从 arguments 读取，服务调用者只由 JWT 和数据库用户确定。"""

        context = self._context(token, session)
        if context is None:
            return ToolResult.failure("TOOL_UNAUTHENTICATED", "请提供有效的登录凭证。")
        return self.registry._invoke(name, arguments, context)


__all__ = ["MCPToolServer"]
