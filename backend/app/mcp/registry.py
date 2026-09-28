"""当前为站内 JWT 认证的工具边界；外部 MCP 传输另定合同。

本模块只定义传输无关的注册、授权与结构化结果，不模拟 MCP 协议消息。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from backend.app.domain.enums import UserRole
from backend.app.domain.permissions import Permission, any_role_has_permission

_JSON_OBJECT = TypeAdapter(dict[str, Any])
_IDENTITY_ARGUMENTS = frozenset({"actor_id", "user_id", "roles", "principal"})


@dataclass(frozen=True, slots=True)
class ToolContext:
    """从 JWT 对应的数据库用户构造；工具参数不能提供这些身份字段。"""

    actor_id: UUID
    roles: frozenset[UserRole]
    session: Session


ToolHandler = Callable[[ToolContext, Any], Mapping[str, Any] | BaseModel]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """每个工具必须声明输入模型、所需权限与处理器。"""

    name: str
    description: str
    permission: Permission
    arguments_model: type[BaseModel]
    handler: ToolHandler
    any_of_permissions: tuple[Permission, ...] = ()


class ToolErrorDTO(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    message: str


class ToolResult(BaseModel):
    """站内结构化结果；不声明兼容任何 MCP 协议版本。"""

    model_config = ConfigDict(frozen=True)

    ok: bool
    data: dict[str, Any] | None = None
    error: ToolErrorDTO | None = None

    @classmethod
    def success(cls, data: Mapping[str, Any] | BaseModel) -> ToolResult:
        value = (
            data.model_dump(mode="json") if isinstance(data, BaseModel) else dict(data)
        )
        return cls(ok=True, data=_JSON_OBJECT.dump_python(value, mode="json"))

    @classmethod
    def failure(cls, code: str, message: str) -> ToolResult:
        return cls(ok=False, error=ToolErrorDTO(code=code, message=message))


class ToolExecutionError(Exception):
    """工具可安全返回给调用方的业务失败；原始内部异常不透传。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ToolRegistry:
    """注册与查找工具；调用只能由已认证的站内 Server 注入身份上下文。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        name = spec.name.strip()
        if not name or name != spec.name:
            raise ValueError("工具名称必须为非空且无首尾空格的标识。")
        if name in self._tools:
            raise ValueError(f"工具 {name} 已注册。")
        self._tools[name] = spec

    def get(self, name: str) -> ToolSpec | None:
        """只返回定义；不提供绕过认证的公开执行入口。"""

        return self._tools.get(name)

    @staticmethod
    def _permitted(spec: ToolSpec, roles: frozenset[UserRole]) -> bool:
        """Keep the original single permission; optional entries are alternatives."""

        return any(
            any_role_has_permission(roles, permission)
            for permission in (spec.permission, *spec.any_of_permissions)
        )

    def available(self, roles: frozenset[UserRole]) -> list[dict[str, Any]]:
        """只列出当前数据库角色被授权的工具。"""

        return [
            {
                "name": spec.name,
                "description": spec.description,
                "arguments_schema": spec.arguments_model.model_json_schema(),
            }
            for spec in sorted(self._tools.values(), key=lambda item: item.name)
            if self._permitted(spec, roles)
        ]

    def _invoke(
        self,
        name: str,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        """验证权限与参数后调用处理器；只供 Server 在 JWT 校验后使用。"""

        spec = self.get(name)
        if spec is None:
            return ToolResult.failure("TOOL_NOT_FOUND", "工具不存在。")
        if not self._permitted(spec, context.roles):
            return ToolResult.failure("TOOL_FORBIDDEN", "当前用户无权使用该工具。")
        if _IDENTITY_ARGUMENTS.intersection(arguments):
            return ToolResult.failure(
                "TOOL_INVALID_ARGUMENTS", "调用参数不得包含身份字段。"
            )
        try:
            parsed = spec.arguments_model.model_validate(dict(arguments))
        except ValidationError:
            return ToolResult.failure("TOOL_INVALID_ARGUMENTS", "工具参数不符合要求。")
        try:
            return ToolResult.success(spec.handler(context, parsed))
        except ToolExecutionError as error:
            return ToolResult.failure(error.code, error.message)
        except Exception:  # noqa: BLE001 - Do not leak arbitrary handler exceptions to callers.
            return ToolResult.failure("TOOL_EXECUTION_FAILED", "工具执行失败。")


__all__ = [
    "ToolContext",
    "ToolErrorDTO",
    "ToolExecutionError",
    "ToolHandler",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
]
