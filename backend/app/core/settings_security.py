"""本机 Setup 授权与请求来源校验（T217）。

设置管理权属于启动本应用的 OS 用户/宿主管理者，**独立于业务 JWT/RBAC**：本模块不
导入业务数据库、业务 JWT 密钥、业务模型或业务初始化状态，因此业务未装配、数据库
不可达或 JWT 缺失时仍能完成授权判定。

安全不变量：

- 一次性码只由本机管理通道发行（`backend.app.deployment.local_management`），公开
  HTTP 没有发行、续期或列举端点；32 随机字节、5 分钟、单次消费，新码作废未兑换码。
- 交换在一次加锁内完成，并发请求只有一个成功；重放返回 `CODE_USED`。
- 管理令牌 32 随机字节，闲置 10 分钟/绝对 30 分钟过期，绑定 environment/instance，
  进程重启即全部失效；服务端只保存 SHA-256 摘要，令牌只在交换响应中出现一次。
- Host 严格解析；`Origin` 必须与请求自身 scheme+Host 精确同源，或属于显式登记的
  development 来源；缺失/null/多值/畸形/跳站一律拒绝，安全 GET/HEAD 允许缺 `Origin`。
- 不读取 `X-Forwarded-*`；Docker 的 NAT 对端不被当作 loopback，Windows/Python 的
  真实 TCP 对端必须是 loopback。
- 交换失败限流（每码 5 次/分钟、每对端 30 次/分钟）；所有设置响应 `no-store`。
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import secrets
import threading
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import Depends, Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from backend.app.core.environment import (
    EnvironmentSelectionError,
    resolve_environment_profile,
)
from backend.app.schemas.settings import EnvironmentName, RuntimeKind

SETTINGS_PATH_PREFIX = "/api/settings"
SESSION_EXCHANGE_PATH = "/api/settings/session/exchange"
SETUP_SCHEME = "Setup"
SETUP_CHALLENGE = f'{SETUP_SCHEME} realm="EduAgent Settings"'

ONE_TIME_CODE_BYTES = 32
MANAGEMENT_TOKEN_BYTES = 32
ONE_TIME_CODE_LIFETIME = timedelta(minutes=5)
SESSION_IDLE_TIMEOUT = timedelta(minutes=10)
SESSION_ABSOLUTE_TIMEOUT = timedelta(minutes=30)

EXCHANGE_FAILURE_LIMIT = 5
EXCHANGE_PEER_FAILURE_LIMIT = 30
EXCHANGE_FAILURE_WINDOW = timedelta(minutes=1)

SAFE_METHODS = frozenset({"GET", "HEAD"})
LOOPBACK_RUNTIMES = frozenset({RuntimeKind.WINDOWS, RuntimeKind.PYTHON})
_UNKNOWN_PEER = "unknown-peer"

_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_FORBIDDEN_AUTHORITY_CHARS = frozenset(" \t\r\n,;@/\\?#")


class PrincipalKind(StrEnum):
    """管理主体来自哪条本机通道；网页不得自报。"""

    LOCAL_OS = "local_os"
    CONTAINER_OPERATOR = "container_operator"


@dataclass(frozen=True, slots=True)
class OperatorIdentity:
    """可信操作者身份：由本机通道确立，不是请求体字段。"""

    kind: PrincipalKind
    principal_id: str

    def __post_init__(self) -> None:
        identifier = self.principal_id.strip()
        if not identifier or len(identifier) > 128:
            raise ValueError("OPERATOR_IDENTITY_INVALID")
        object.__setattr__(self, "principal_id", identifier)


class SettingsSecurityError(Exception):
    """带安全错误码的设置授权失败；消息不回显令牌或授权码。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 401,
        retryable: bool = False,
        stage: str = "settings_authorization",
        headers: Iterable[tuple[str, str]] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.stage = stage
        self.headers = tuple(headers)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _unauthorized(code: str, message: str, *, retryable: bool = False) -> SettingsSecurityError:
    return SettingsSecurityError(
        code,
        message,
        status_code=401,
        retryable=retryable,
        headers=(("WWW-Authenticate", SETUP_CHALLENGE),),
    )


# ---------------------------------------------------------------------------
# Host / Origin 严格解析
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HostAuthority:
    """归一化的 `host[:port]`；IPv6 使用压缩形式与本项目统一的括号写法。"""

    hostname: str
    port: int | None

    def __str__(self) -> str:
        hostname = f"[{self.hostname}]" if ":" in self.hostname else self.hostname
        return hostname if self.port is None else f"{hostname}:{self.port}"


@dataclass(frozen=True, slots=True)
class RequestSource:
    """一次请求的可信来源事实；来源头本身不授予管理身份。"""

    host: HostAuthority
    peer: str | None
    scheme: str


def _invalid_host(message: str = "Host 头格式无效。") -> SettingsSecurityError:
    return SettingsSecurityError("HOST_INVALID", message, status_code=403)


def _parse_hostname(raw: str) -> str:
    candidate = raw
    if candidate.startswith("[") or ":" in candidate:
        try:
            return ipaddress.IPv6Address(candidate.strip("[]")).compressed
        except ValueError:
            raise _invalid_host() from None
    try:
        return str(ipaddress.IPv4Address(candidate))
    except ValueError:
        pass
    lowered = candidate.lower()
    if not lowered or lowered.endswith(".") or len(lowered) > 253:
        raise _invalid_host()
    labels = lowered.split(".")
    if not all(_DNS_LABEL.match(label) for label in labels):
        raise _invalid_host()
    return lowered


def parse_authority(value: str) -> HostAuthority:
    """解析单个 `host[:port]`；多值、userinfo、路径或畸形端口一律拒绝。"""

    raw = value
    if not raw or raw != raw.strip() or any(char in raw for char in _FORBIDDEN_AUTHORITY_CHARS):
        raise _invalid_host()
    if raw.startswith("["):
        closing = raw.find("]")
        if closing < 0:
            raise _invalid_host()
        hostname = _parse_hostname(raw[: closing + 1])
        remainder = raw[closing + 1 :]
        if remainder and not remainder.startswith(":"):
            raise _invalid_host()
        port_text = remainder[1:] if remainder else ""
    elif raw.count(":") == 0:
        hostname = _parse_hostname(raw)
        port_text = ""
    elif raw.count(":") == 1:
        hostname_text, _, port_text = raw.partition(":")
        hostname = _parse_hostname(hostname_text)
    else:
        # 裸 IPv6 必须加括号，否则无法与端口区分。
        raise _invalid_host()

    port: int | None = None
    if port_text:
        if not port_text.isdigit():
            raise _invalid_host("Host 端口无效。")
        port = int(port_text)
        if not 1 <= port <= 65535:
            raise _invalid_host("Host 端口无效。")
    return HostAuthority(hostname=hostname, port=port)


def _invalid_origin(message: str = "Origin 头格式无效。") -> SettingsSecurityError:
    return SettingsSecurityError("ORIGIN_INVALID", message, status_code=403)


def parse_origin(value: str) -> tuple[str, HostAuthority]:
    """解析严格的 `scheme://host[:port]`；只接受 http/https 且无路径/查询/凭据。"""

    raw = value
    if (
        not raw
        or raw != raw.strip()
        or raw.lower() == "null"
        or any(char in raw for char in " \t\r\n,;")
        or "://" not in raw
    ):
        raise _invalid_origin()
    scheme, _, remainder = raw.partition("://")
    scheme = scheme.lower()
    if scheme not in {"http", "https"} or not remainder:
        raise _invalid_origin()
    try:
        authority = parse_authority(remainder)
    except SettingsSecurityError:
        raise _invalid_origin() from None
    return scheme, authority


def _peer_address(peer: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if not peer:
        return None
    candidate = peer.split("%", 1)[0].strip().strip("[]")
    if not candidate:
        return None
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        # 非 IP 对端（测试 transport / Unix socket）不冒充网络对端。
        return None


@dataclass(frozen=True, slots=True)
class SetupSourcePolicy:
    """精确 Host/Origin 与对端边界策略。

    `allowed_hosts` 为空时以“Origin 必须与本请求 scheme+Host 精确同源”为准；T218/T221
    取得真实监听绑定后应传入允许集合，从而同时拒绝 DNS rebinding 形式的外部 Host。
    """

    environment: EnvironmentName = EnvironmentName.DEVELOPMENT
    runtime_kind: RuntimeKind = RuntimeKind.PYTHON
    allowed_hosts: frozenset[str] = frozenset()
    development_origins: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        hosts: set[str] = set()
        for entry in self.allowed_hosts:
            try:
                hosts.add(str(parse_authority(entry)))
            except SettingsSecurityError:
                raise ValueError("ALLOWED_HOST_INVALID") from None
        origins: set[str] = set()
        if self.development_origins:
            if self.environment is not EnvironmentName.DEVELOPMENT:
                raise ValueError("DEVELOPMENT_ORIGIN_REQUIRES_DEVELOPMENT")
            for entry in self.development_origins:
                try:
                    scheme, authority = parse_origin(entry)
                except SettingsSecurityError:
                    raise ValueError("DEVELOPMENT_ORIGIN_INVALID") from None
                origins.add(f"{scheme}://{authority}")
        object.__setattr__(self, "allowed_hosts", frozenset(hosts))
        object.__setattr__(self, "development_origins", frozenset(origins))

    def check_request(self, request: Request) -> RequestSource:
        """校验唯一 Host、允许集合与对端；不读取任何转发头。"""

        host_values = request.headers.getlist("host")
        if len(host_values) != 1:
            raise SettingsSecurityError(
                "HOST_REQUIRED", "请求必须且只能携带一个 Host 头。", status_code=403
            )
        authority = parse_authority(host_values[0])
        if self.allowed_hosts and str(authority) not in self.allowed_hosts:
            raise SettingsSecurityError(
                "HOST_NOT_ALLOWED", "请求 Host 不是本机设置服务地址。", status_code=403
            )
        peer = request.client.host if request.client else None
        self.check_peer(peer)
        return RequestSource(host=authority, peer=peer, scheme=request.url.scheme.lower())

    def check_peer(self, peer: str | None) -> None:
        """Windows/Python 的真实 IP 对端必须是 loopback；Docker NAT 不据此放行。"""

        if self.runtime_kind is RuntimeKind.DOCKER:
            return
        address = _peer_address(peer)
        if address is not None and not address.is_loopback:
            raise SettingsSecurityError(
                "PEER_NOT_LOOPBACK", "设置管理入口只接受本机连接。", status_code=403
            )

    def check_origin(
        self,
        request: Request,
        *,
        source: RequestSource,
        required: bool,
    ) -> str | None:
        """敏感操作必须有合法 Origin；安全读取允许缺失，但存在时必须精确匹配。"""

        values = request.headers.getlist("origin")
        if not values:
            if required:
                raise SettingsSecurityError(
                    "ORIGIN_REQUIRED", "敏感设置操作必须携带合法 Origin。", status_code=403
                )
            return None
        if len(values) != 1:
            raise _invalid_origin("请求携带多个 Origin 头。")
        return self.match_origin(values[0], source=source)

    def match_origin(self, value: str, *, source: RequestSource) -> str:
        scheme, authority = parse_origin(value)
        candidate = f"{scheme}://{authority}"
        if scheme == source.scheme and authority == source.host:
            return candidate
        if candidate in self.development_origins:
            return candidate
        raise SettingsSecurityError(
            "ORIGIN_NOT_ALLOWED", "请求来源不是本机设置服务的同源地址。", status_code=403
        )


# ---------------------------------------------------------------------------
# 一次性码 / 管理会话
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IssuedOneTimeCode:
    """只回传给本机管理通道的一次性码；`repr` 不含码本身。"""

    code: str = field(repr=False)
    code_id: UUID
    expires_at: datetime
    environment_id: UUID
    instance_id: UUID
    operator: OperatorIdentity


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """交换得到的管理令牌；只在这一次响应里出现。"""

    token: str = field(repr=False)
    session_id: UUID
    expires_at: datetime
    idle_expires_at: datetime
    environment_id: UUID
    instance_id: UUID
    operator: OperatorIdentity


@dataclass(frozen=True, slots=True)
class ManagementSession:
    """已认证的管理会话元数据；不包含令牌或授权码。"""

    session_id: UUID
    environment_id: UUID
    instance_id: UUID
    operator: OperatorIdentity
    issued_at: datetime
    last_seen_at: datetime
    expires_at: datetime


@dataclass(slots=True)
class _CodeRecord:
    code_id: UUID
    expires_at: datetime
    operator: OperatorIdentity
    used: bool = False


@dataclass(slots=True)
class _SessionRecord:
    session_id: UUID
    operator: OperatorIdentity
    environment_id: UUID
    instance_id: UUID
    issued_at: datetime
    last_seen_at: datetime
    expires_at: datetime


class SetupSecurityService:
    """进程内 Setup 授权事实源；进程重启即撤销全部码与会话。"""

    def __init__(
        self,
        *,
        environment_id: UUID,
        instance_id: UUID | None = None,
        environment: EnvironmentName = EnvironmentName.DEVELOPMENT,
        runtime_kind: RuntimeKind = RuntimeKind.PYTHON,
        policy: SetupSourcePolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.environment_id = environment_id
        self.instance_id = instance_id or uuid4()
        self.environment = environment
        self.runtime_kind = runtime_kind
        self.policy = policy or SetupSourcePolicy(
            environment=environment, runtime_kind=runtime_kind
        )
        self._clock = clock or _utcnow
        self._lock = threading.Lock()
        self._codes: dict[str, _CodeRecord] = {}
        self._sessions: dict[str, _SessionRecord] = {}
        self._code_failures: dict[str, deque[datetime]] = {}
        self._peer_failures: dict[str, deque[datetime]] = {}

    # -- 本机发行 ---------------------------------------------------------

    def issue_code(self, operator: OperatorIdentity) -> IssuedOneTimeCode:
        """仅由本机管理通道调用；新码作废此前未兑换的码。"""

        now = self._now()
        code = secrets.token_urlsafe(ONE_TIME_CODE_BYTES)
        record = _CodeRecord(
            code_id=uuid4(),
            expires_at=now + ONE_TIME_CODE_LIFETIME,
            operator=operator,
        )
        with self._lock:
            self._codes.clear()
            self._codes[_digest(code)] = record
        return IssuedOneTimeCode(
            code=code,
            code_id=record.code_id,
            expires_at=record.expires_at,
            environment_id=self.environment_id,
            instance_id=self.instance_id,
            operator=operator,
        )

    # -- 交换 -------------------------------------------------------------

    def exchange(self, code: str, *, peer: str | None = None) -> IssuedSession:
        """一次性码换短期管理令牌；并发时只有一个请求成功。"""

        now = self._now()
        digest = _digest(code)
        with self._lock:
            self._enforce_limits(digest, peer, now)
            record = self._codes.get(digest)
            if record is None:
                self._register_failure(digest, peer, now)
                raise SettingsSecurityError(
                    "CODE_EXPIRED",
                    "授权码已过期或无效，请从本机启动器重新获取。",
                    retryable=True,
                    stage="settings_session",
                )
            if record.used:
                self._register_failure(digest, peer, now)
                raise SettingsSecurityError(
                    "CODE_USED",
                    "授权码已被使用，请从本机启动器重新获取。",
                    retryable=True,
                    stage="settings_session",
                )
            if now >= record.expires_at:
                self._codes.pop(digest, None)
                self._register_failure(digest, peer, now)
                raise SettingsSecurityError(
                    "CODE_EXPIRED",
                    "授权码已过期或无效，请从本机启动器重新获取。",
                    retryable=True,
                    stage="settings_session",
                )
            record.used = True
            token = secrets.token_urlsafe(MANAGEMENT_TOKEN_BYTES)
            session = _SessionRecord(
                session_id=uuid4(),
                operator=record.operator,
                environment_id=self.environment_id,
                instance_id=self.instance_id,
                issued_at=now,
                last_seen_at=now,
                expires_at=now + SESSION_ABSOLUTE_TIMEOUT,
            )
            self._sessions[_digest(token)] = session
            self._code_failures.pop(digest, None)
        return IssuedSession(
            token=token,
            session_id=session.session_id,
            expires_at=session.expires_at,
            idle_expires_at=now + SESSION_IDLE_TIMEOUT,
            environment_id=session.environment_id,
            instance_id=session.instance_id,
            operator=session.operator,
        )

    # -- 会话校验 ---------------------------------------------------------

    def authorize(
        self,
        token: str,
        *,
        environment_id: UUID | None = None,
        instance_id: UUID | None = None,
    ) -> ManagementSession:
        """校验管理令牌、期限与环境/实例绑定；不校验任何业务权限。"""

        now = self._now()
        digest = _digest(token)
        with self._lock:
            record = self._sessions.get(digest)
            if record is None:
                raise _unauthorized("SESSION_INVALID", "管理会话无效，请重新授权。")
            if now >= record.expires_at or now - record.last_seen_at > SESSION_IDLE_TIMEOUT:
                self._sessions.pop(digest, None)
                raise _unauthorized(
                    "SESSION_EXPIRED", "管理会话已过期，请重新授权。", retryable=True
                )
            if environment_id is not None and environment_id != record.environment_id:
                raise _unauthorized(
                    "SESSION_BINDING_MISMATCH", "管理会话与当前环境不匹配，请重新授权。"
                )
            if instance_id is not None and instance_id != record.instance_id:
                raise _unauthorized(
                    "SESSION_BINDING_MISMATCH", "管理会话与当前实例不匹配，请重新授权。"
                )
            record.last_seen_at = now
            return ManagementSession(
                session_id=record.session_id,
                environment_id=record.environment_id,
                instance_id=record.instance_id,
                operator=record.operator,
                issued_at=record.issued_at,
                last_seen_at=record.last_seen_at,
                expires_at=record.expires_at,
            )

    # -- 内部 -------------------------------------------------------------

    def _now(self) -> datetime:
        moment = self._clock()
        return moment if moment.tzinfo else moment.replace(tzinfo=UTC)

    def _recent(self, bucket: dict[str, deque[datetime]], key: str, now: datetime) -> int:
        window = bucket.get(key)
        if window is None:
            return 0
        while window and now - window[0] > EXCHANGE_FAILURE_WINDOW:
            window.popleft()
        if not window:
            bucket.pop(key, None)
            return 0
        return len(window)

    def _enforce_limits(self, digest: str, peer: str | None, now: datetime) -> None:
        limited = self._recent(self._code_failures, digest, now) >= EXCHANGE_FAILURE_LIMIT
        limited = limited or self._recent(
            self._peer_failures, peer or _UNKNOWN_PEER, now
        ) >= EXCHANGE_PEER_FAILURE_LIMIT
        if limited:
            raise SettingsSecurityError(
                "EXCHANGE_RATE_LIMITED",
                "授权码兑换失败次数过多，请稍后重试。",
                status_code=429,
                retryable=True,
                stage="settings_session",
                headers=(("Retry-After", "60"),),
            )

    def _register_failure(self, digest: str, peer: str | None, now: datetime) -> None:
        for bucket, key in (
            (self._code_failures, digest),
            (self._peer_failures, peer or _UNKNOWN_PEER),
        ):
            self._recent(bucket, key, now)
            bucket.setdefault(key, deque()).append(now)


# ---------------------------------------------------------------------------
# 错误响应与 HTTP 接缝
# ---------------------------------------------------------------------------


def error_response(error: SettingsSecurityError) -> JSONResponse:
    """统一安全错误体；不附带令牌、授权码或内部异常原文。"""

    payload = {
        "code": error.code,
        "message": error.message,
        "stage": error.stage,
        "retryable": error.retryable,
        "correlation_id": uuid4().hex,
    }
    headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
    headers.update(dict(error.headers))
    return JSONResponse(status_code=error.status_code, content=payload, headers=headers)


def parse_setup_authorization(values: Sequence[str]) -> str | None:
    """只接受唯一且严格 `Setup <token>` 形式的授权头；不认业务 Bearer。"""

    if len(values) != 1:
        return None
    parts = values[0].strip().split()
    if len(parts) != 2 or parts[0].lower() != SETUP_SCHEME.lower():
        return None
    token = parts[1]
    if not token or any(char.isspace() for char in token):
        return None
    return token


def _is_settings_path(path: str) -> bool:
    return path == SETTINGS_PATH_PREFIX or path.startswith(SETTINGS_PATH_PREFIX + "/")


def _normalized_path(path: str) -> str:
    return path.rstrip("/") or "/"


def require_json_content(request: Request) -> None:
    """敏感写请求必须是 JSON；表单、text/plain、multipart 一律 415。"""

    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise SettingsSecurityError(
            "JSON_REQUIRED", "设置写操作只接受 application/json。", status_code=415
        )


async def check_personal_source(request: Request) -> SetupSourcePolicy:
    """Host/Origin/space boundary only; never buffers the request body.

    Upload routes need the same source boundary but must stream their own
    multipart body under their own byte cap, so body handling stays out of this
    check. Ordinary personal writes keep ``check_personal_request`` below.
    """

    policy = getattr(request.app.state, "learning_source_policy", None)
    if not isinstance(policy, SetupSourcePolicy) or not policy.allowed_hosts:
        raise SettingsSecurityError(
            "LEARNING_SOURCE_UNAVAILABLE", "个人入口的来源边界尚未配置。", status_code=503
        )
    source = policy.check_request(request)
    writing = request.method not in {"GET", "HEAD"}
    policy.check_origin(request, source=source, required=writing)
    if writing:
        # Offline maintenance applies to every personal writer, including
        # practice/teacher/operation endpoints that do not write a file.
        # This must run before a request can claim a paid operation.
        bootstrap = getattr(request.app.state, "bootstrap", None)
        settings = getattr(bootstrap, "settings", None)
        if settings is not None and (settings.config_root.parent / "storage" / ".maintenance.json").exists():
            raise SettingsSecurityError(
                "STORAGE_MAINTENANCE", "个人空间正在离线维护，暂时停止写入。", status_code=503
            )
    return policy


async def check_personal_request(request: Request) -> None:
    """Personal routes share exact source checks, without acquiring Setup authority."""
    await check_personal_source(request)
    writing = request.method not in {"GET", "HEAD"}
    if writing:
        require_json_content(request)
        data = bytearray()
        async for part in request.stream():
            if len(data) + len(part) > 65536:
                raise SettingsSecurityError(
                    "LIMIT_EXCEEDED", "请求超过允许的大小。", status_code=413
                )
            data.extend(part)
        request._body = bytes(data)


def _harden(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    if "access-control-allow-origin" in response.headers:
        del response.headers["access-control-allow-origin"]
    return response


class SetupAuthorizationMiddleware(BaseHTTPMiddleware):
    """在路由前执行 Settings 管理面授权；不依赖业务装配或数据库。"""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if not _is_settings_path(request.url.path):
            return await call_next(request)

        service = getattr(request.app.state, "setup_security", None)
        if not isinstance(service, SetupSecurityService):
            return _harden(
                error_response(
                    SettingsSecurityError(
                        "SETTINGS_SECURITY_UNAVAILABLE",
                        "设置授权服务不可用。",
                        status_code=503,
                    )
                )
            )
        try:
            source = service.policy.check_request(request)
        except SettingsSecurityError as error:
            return _harden(error_response(error))

        # 预检与交换由后续层处理：预检不获得管理权限，交换改验一次性码。
        if request.method == "OPTIONS" or _normalized_path(request.url.path) == SESSION_EXCHANGE_PATH:
            return _harden(await call_next(request))

        try:
            token = parse_setup_authorization(request.headers.getlist("authorization"))
            if token is None:
                raise _unauthorized("SESSION_REQUIRED", "设置操作需要本机管理授权。")
            session = service.authorize(token)
            sensitive = request.method not in SAFE_METHODS
            service.policy.check_origin(request, source=source, required=sensitive)
            if sensitive:
                require_json_content(request)
        except SettingsSecurityError as error:
            return _harden(error_response(error))

        request.state.management_session = session
        return _harden(await call_next(request))


def require_management_session(request: Request) -> ManagementSession:
    """供后续 Settings 路由复用的会话依赖；缺失时按防御性失败关闭。"""

    session = getattr(request.state, "management_session", None)
    if not isinstance(session, ManagementSession):
        raise _unauthorized("SESSION_REQUIRED", "设置操作需要本机管理授权。")
    return session


ManagementSessionContext = Annotated[ManagementSession, Depends(require_management_session)]


def _fallback_environment_id() -> UUID:
    root = os.environ.get("EDUAGENT_DATA_ROOT") or str(Path.cwd())
    return uuid5(NAMESPACE_URL, "eduagent:unadopted:" + os.path.normcase(root))


def build_process_setup_security(
    *,
    environment: EnvironmentName | None = None,
    runtime_kind: RuntimeKind | None = None,
    environment_id: UUID | None = None,
    allowed_hosts: Iterable[str] = (),
    development_origins: Iterable[str] = (),
    clock: Callable[[], datetime] | None = None,
) -> SetupSecurityService:
    """为本进程建立授权服务；业务未配置时仍可运行（只做身份与来源判定）。"""

    resolved_environment = environment
    resolved_runtime = runtime_kind
    resolved_id = environment_id
    if resolved_environment is None or resolved_runtime is None or resolved_id is None:
        try:
            profile = resolve_environment_profile()
        except (EnvironmentSelectionError, ValueError, OSError):
            profile = None
        if profile is not None:
            resolved_environment = resolved_environment or profile.environment
            resolved_runtime = resolved_runtime or profile.runtime_kind
            resolved_id = resolved_id or profile.environment_id
    policy = SetupSourcePolicy(
        environment=resolved_environment or EnvironmentName.DEVELOPMENT,
        runtime_kind=resolved_runtime or RuntimeKind.PYTHON,
        allowed_hosts=frozenset(allowed_hosts),
        development_origins=frozenset(development_origins),
    )
    return SetupSecurityService(
        environment_id=resolved_id or _fallback_environment_id(),
        environment=policy.environment,
        runtime_kind=policy.runtime_kind,
        policy=policy,
        clock=clock,
    )


__all__ = [
    "EXCHANGE_FAILURE_LIMIT",
    "EXCHANGE_FAILURE_WINDOW",
    "EXCHANGE_PEER_FAILURE_LIMIT",
    "LOOPBACK_RUNTIMES",
    "ONE_TIME_CODE_BYTES",
    "ONE_TIME_CODE_LIFETIME",
    "SAFE_METHODS",
    "SESSION_ABSOLUTE_TIMEOUT",
    "SESSION_EXCHANGE_PATH",
    "SESSION_IDLE_TIMEOUT",
    "SETTINGS_PATH_PREFIX",
    "SETUP_CHALLENGE",
    "SETUP_SCHEME",
    "HostAuthority",
    "IssuedOneTimeCode",
    "IssuedSession",
    "ManagementSession",
    "ManagementSessionContext",
    "OperatorIdentity",
    "PrincipalKind",
    "RequestSource",
    "SettingsSecurityError",
    "SetupAuthorizationMiddleware",
    "SetupSecurityService",
    "SetupSourcePolicy",
    "build_process_setup_security",
    "error_response",
    "parse_authority",
    "parse_origin",
    "parse_setup_authorization",
    "require_json_content",
    "require_management_session",
]
