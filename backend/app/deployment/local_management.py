"""本机管理通道：Setup 一次码的唯一发行接缝（T217）。

Windows Launcher、源码启动与 Docker 宿主受限 exec 共用本模块：它们才是“启动本应用的
OS 用户/宿主管理者”，网页不能自报这个身份。因此这里只提供进程内接口：

- 不注册任何 HTTP 路由，公开 API 无法申请、续期、列举或复制授权码；
- 授权码只出现在一次性返回物与设置页 URL 的 fragment 中，绝不进入查询串、argv、
  服务日志或业务备份（受限 exec 的一次性返回值仅交给操作者）；
- 新码作废此前未兑换的码，重复点击“打开设置/重新授权”不会累积可用码。

Windows 控制管道和 Docker 私有 Unix socket 均复用此授权事实源。
"""

from __future__ import annotations

import getpass
from collections.abc import Mapping
from urllib.parse import quote, urlsplit

from backend.app.core.settings_security import (
    IssuedOneTimeCode,
    OperatorIdentity,
    PrincipalKind,
    SetupSecurityService,
)
from backend.app.schemas.settings import RuntimeKind

#: 控制通道允许的消息类型；其他类型一律拒绝，避免把管理通道扩成通用命令入口。
CONTROL_MESSAGES = frozenset({"open_settings", "reauthorize"})
_CONTROL_MESSAGE_KEYS = frozenset({"type", "settings_base_url"})

CONTAINER_OPERATOR_ID = "container-operator"
LOCAL_OPERATOR_FALLBACK = "local-operator"

#: Windows/源码本机入口只向回环地址发行设置页，不因 --port/绑定改为公网地址。
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class LocalManagementError(RuntimeError):
    """本机管理通道的安全错误；消息不含授权码或令牌。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def resolve_operator_identity(*, runtime_kind: RuntimeKind) -> OperatorIdentity:
    """按运行形态标记可信主体；Docker 不猜宿主用户名。"""

    if runtime_kind is RuntimeKind.DOCKER:
        return OperatorIdentity(
            kind=PrincipalKind.CONTAINER_OPERATOR, principal_id=CONTAINER_OPERATOR_ID
        )
    try:
        account = getpass.getuser().strip()
    except (OSError, KeyError):
        account = ""
    return OperatorIdentity(
        kind=PrincipalKind.LOCAL_OS, principal_id=account or LOCAL_OPERATOR_FALLBACK
    )


def authorization_url(base_url: str, code: str) -> str:
    """把授权码放进 fragment（不经服务端、不进查询串与访问日志）。"""

    parts = urlsplit(base_url)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise LocalManagementError(
            "AUTHORIZATION_BASE_URL_INVALID",
            "设置页地址必须是本机 http/https 地址，且不得自带查询或片段。",
        )
    if not code:
        raise LocalManagementError("AUTHORIZATION_CODE_REQUIRED", "授权码不能为空。")
    return f"{base_url.rstrip('/')}/#code={quote(code, safe='')}"


def settings_base_url(host: str, port: int) -> str:
    """由实际监听的 Host/Port 生成本机设置页基址。

    只接受回环主机名：`SetupSourcePolicy` 的精确 Host 允许集合由同一监听地址推得，
    因此这里不引入新的来源范围；非回环绑定的启动器不发行设置页地址。
    """

    name = (host or "").strip().lower()
    if name not in LOOPBACK_HOSTS:
        raise LocalManagementError(
            "SETTINGS_BIND_NOT_LOOPBACK",
            "设置页只允许在回环监听地址上打开。",
        )
    try:
        value = int(port)
    except (TypeError, ValueError):
        raise LocalManagementError("SETTINGS_PORT_INVALID", "监听端口无效。") from None
    if not 1 <= value <= 65535:
        raise LocalManagementError("SETTINGS_PORT_INVALID", "监听端口无效。")
    authority = f"[{name}]" if ":" in name else name
    return f"http://{authority}:{value}"


class LocalManagementAuthority:
    """唯一允许发行 Setup 一次码的进程内对象。"""

    def __init__(
        self,
        security: SetupSecurityService,
        *,
        runtime_kind: RuntimeKind | None = None,
        operator: OperatorIdentity | None = None,
    ) -> None:
        self.security = security
        self.runtime_kind = runtime_kind or security.runtime_kind
        self._operator = operator

    @property
    def operator(self) -> OperatorIdentity:
        if self._operator is None:
            self._operator = resolve_operator_identity(runtime_kind=self.runtime_kind)
        return self._operator

    def request_one_time_code(self) -> IssuedOneTimeCode:
        """发行新码并作废此前未兑换的码；由本机所有者通道调用。"""

        return self.security.issue_code(self.operator)

    def handle_control_message(self, message: Mapping[str, object]) -> dict[str, object]:
        """处理“打开设置/重新授权”控制消息，返回一次性授权载荷。"""

        if not isinstance(message, Mapping):
            raise LocalManagementError("CONTROL_MESSAGE_INVALID", "控制消息必须是对象。")
        if set(message) - _CONTROL_MESSAGE_KEYS:
            raise LocalManagementError(
                "CONTROL_MESSAGE_INVALID", "控制消息包含未定义字段。"
            )
        kind = message.get("type")
        if not isinstance(kind, str) or kind not in CONTROL_MESSAGES:
            raise LocalManagementError(
                "CONTROL_MESSAGE_UNSUPPORTED", "不支持的控制消息类型。"
            )
        base_url = message.get("settings_base_url")
        if base_url is not None and not isinstance(base_url, str):
            raise LocalManagementError(
                "AUTHORIZATION_BASE_URL_INVALID", "设置页地址必须是字符串。"
            )
        grant = self.request_one_time_code()
        return {
            "type": "one_time_code_grant",
            "code": grant.code,
            "expires_at": grant.expires_at.isoformat(),
            "environment_id": str(grant.environment_id),
            "instance_id": str(grant.instance_id),
            "operator": {
                "kind": grant.operator.kind.value,
                "principal_id": grant.operator.principal_id,
            },
            "authorization_url": (
                authorization_url(base_url, grant.code) if base_url else None
            ),
        }


__all__ = [
    "CONTAINER_OPERATOR_ID",
    "CONTROL_MESSAGES",
    "LOCAL_OPERATOR_FALLBACK",
    "LOOPBACK_HOSTS",
    "LocalManagementAuthority",
    "LocalManagementError",
    "authorization_url",
    "resolve_operator_identity",
    "settings_base_url",
]
