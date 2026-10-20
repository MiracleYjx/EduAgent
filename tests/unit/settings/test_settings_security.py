"""TCR-S3-SETTINGS-SECURITY-019（T217）：独立 Setup 授权单元契约。

只验证 `backend.app.core.settings_security` 的安全语义：本机一次性码、交换、期限、
重放、并发、限流、精确 Host/Origin 与对端边界。本目录的 `conftest.py` 已阻断真实
socket 与 Engine.connect，因此任何“靠数据库/网络完成授权”的实现都会立即暴露。
"""

from __future__ import annotations

import ast
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from starlette.requests import Request

from backend.app.core.settings_security import (
    EXCHANGE_FAILURE_LIMIT,
    EXCHANGE_PEER_FAILURE_LIMIT,
    ONE_TIME_CODE_LIFETIME,
    SESSION_ABSOLUTE_TIMEOUT,
    SESSION_IDLE_TIMEOUT,
    OperatorIdentity,
    PrincipalKind,
    SettingsSecurityError,
    SetupSecurityService,
    SetupSourcePolicy,
    error_response,
    parse_authority,
    parse_origin,
    parse_setup_authorization,
)
from backend.app.schemas.settings import EnvironmentName, RuntimeKind

T217 = "T217_SETTINGS_SECURITY_REQUIRED"

ENVIRONMENT_ID = UUID("11111111-1111-4111-8111-111111111111")
INSTANCE_ID = UUID("22222222-2222-4222-8222-222222222222")
OPERATOR = OperatorIdentity(
    kind=PrincipalKind.LOCAL_OS, principal_id="synthetic-local-operator"
)
LOCAL_PEER = "127.0.0.1"
START = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)

REPO_ROOT = Path(__file__).resolve().parents[3]


class FixedClock:
    """可推进的 UTC 时钟；不 sleep，不依赖真实时间。"""

    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, delta: timedelta) -> None:
        self.moment = self.moment + delta


def _request(
    *,
    method: str = "GET",
    path: str = "/api/settings/",
    headers: tuple[tuple[str, str], ...] = (("host", "127.0.0.1:8000"),),
    client: tuple[str, int] | None = (LOCAL_PEER, 50000),
    scheme: str = "http",
) -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": scheme,
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (name.lower().encode("latin-1"), value.encode("latin-1"))
            for name, value in headers
        ],
        "client": client,
        "server": ("127.0.0.1", 8000),
    }
    return Request(scope)


@pytest.fixture()
def clock() -> FixedClock:
    return FixedClock(START)


@pytest.fixture()
def service(clock: FixedClock) -> SetupSecurityService:
    return SetupSecurityService(
        environment_id=ENVIRONMENT_ID,
        instance_id=INSTANCE_ID,
        environment=EnvironmentName.DEVELOPMENT,
        runtime_kind=RuntimeKind.PYTHON,
        clock=clock,
    )


# ---------------------------------------------------------------------------
# 发行、交换、过期与重放
# ---------------------------------------------------------------------------


def test_issued_code_has_entropy_and_five_minute_lifetime(
    service: SetupSecurityService,
) -> None:
    issued = service.issue_code(OPERATOR)

    assert len(issued.code) >= 43  # 32 随机字节的 urlsafe 编码
    assert issued.environment_id == ENVIRONMENT_ID
    assert issued.instance_id == INSTANCE_ID
    assert issued.operator == OPERATOR
    assert issued.expires_at == START + ONE_TIME_CODE_LIFETIME


def test_exchange_returns_bound_short_lived_token(service: SetupSecurityService) -> None:
    issued = service.issue_code(OPERATOR)

    session = service.exchange(issued.code, peer=LOCAL_PEER)

    assert session.token and session.token != issued.code
    assert session.environment_id == ENVIRONMENT_ID
    assert session.instance_id == INSTANCE_ID
    assert session.expires_at == START + SESSION_ABSOLUTE_TIMEOUT
    assert session.idle_expires_at == START + SESSION_IDLE_TIMEOUT

    authorized = service.authorize(session.token)
    assert authorized.session_id == session.session_id
    assert authorized.environment_id == ENVIRONMENT_ID
    assert authorized.instance_id == INSTANCE_ID
    assert authorized.operator == OPERATOR


def test_code_is_single_use(service: SetupSecurityService) -> None:
    issued = service.issue_code(OPERATOR)
    service.exchange(issued.code, peer=LOCAL_PEER)

    with pytest.raises(SettingsSecurityError) as error:
        service.exchange(issued.code, peer=LOCAL_PEER)

    assert error.value.code == "CODE_USED"
    assert error.value.status_code == 401
    assert issued.code not in str(error.value)


def test_unknown_and_expired_codes_are_indistinguishable(
    service: SetupSecurityService, clock: FixedClock
) -> None:
    with pytest.raises(SettingsSecurityError) as unknown:
        service.exchange("synthetic-never-issued", peer=LOCAL_PEER)
    assert unknown.value.code == "CODE_EXPIRED"

    issued = service.issue_code(OPERATOR)
    clock.advance(ONE_TIME_CODE_LIFETIME + timedelta(seconds=1))
    with pytest.raises(SettingsSecurityError) as expired:
        service.exchange(issued.code, peer=LOCAL_PEER)

    assert expired.value.code == "CODE_EXPIRED"
    assert issued.code not in str(expired.value)


def test_new_code_supersedes_previous_unexchanged_code(
    service: SetupSecurityService,
) -> None:
    first = service.issue_code(OPERATOR)
    second = service.issue_code(OPERATOR)

    with pytest.raises(SettingsSecurityError) as superseded:
        service.exchange(first.code, peer=LOCAL_PEER)
    assert superseded.value.code == "CODE_EXPIRED"

    assert service.exchange(second.code, peer=LOCAL_PEER).token


def test_concurrent_exchange_succeeds_exactly_once(
    service: SetupSecurityService,
) -> None:
    successes: list[str] = []
    failures: list[str] = []
    barrier = threading.Barrier(4)

    def attempt(code: str) -> None:
        barrier.wait()
        try:
            successes.append(service.exchange(code, peer=LOCAL_PEER).token)
        except SettingsSecurityError as error:
            failures.append(error.code)

    for _ in range(3):
        issued = service.issue_code(OPERATOR)
        successes.clear()
        failures.clear()
        threads = [threading.Thread(target=attempt, args=(issued.code,)) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(successes) == 1, "并发交换不得重复成功"
        assert sorted(failures) == ["CODE_USED"] * 3


# ---------------------------------------------------------------------------
# 会话期限与绑定
# ---------------------------------------------------------------------------


def test_idle_timeout_requires_reauthorization(
    service: SetupSecurityService, clock: FixedClock
) -> None:
    token = service.exchange(service.issue_code(OPERATOR).code, peer=LOCAL_PEER).token

    clock.advance(SESSION_IDLE_TIMEOUT - timedelta(seconds=1))
    assert service.authorize(token).session_id

    clock.advance(SESSION_IDLE_TIMEOUT + timedelta(seconds=1))
    with pytest.raises(SettingsSecurityError) as expired:
        service.authorize(token)

    assert expired.value.code == "SESSION_EXPIRED"
    assert expired.value.status_code == 401
    assert dict(expired.value.headers)["WWW-Authenticate"].startswith("Setup")


def test_absolute_timeout_cannot_be_refreshed(
    service: SetupSecurityService, clock: FixedClock
) -> None:
    token = service.exchange(service.issue_code(OPERATOR).code, peer=LOCAL_PEER).token

    for _ in range(5):
        clock.advance(timedelta(minutes=5))
        service.authorize(token)

    clock.advance(SESSION_ABSOLUTE_TIMEOUT)
    with pytest.raises(SettingsSecurityError) as expired:
        service.authorize(token)
    assert expired.value.code == "SESSION_EXPIRED"


def test_session_is_bound_to_environment_and_instance(
    service: SetupSecurityService,
) -> None:
    token = service.exchange(service.issue_code(OPERATOR).code, peer=LOCAL_PEER).token

    with pytest.raises(SettingsSecurityError) as wrong_environment:
        service.authorize(token, environment_id=UUID("33333333-3333-4333-8333-333333333333"))
    assert wrong_environment.value.code == "SESSION_BINDING_MISMATCH"

    with pytest.raises(SettingsSecurityError) as wrong_instance:
        service.authorize(token, instance_id=UUID("44444444-4444-4444-8444-444444444444"))
    assert wrong_instance.value.code == "SESSION_BINDING_MISMATCH"

    assert service.authorize(
        token, environment_id=ENVIRONMENT_ID, instance_id=INSTANCE_ID
    ).session_id


def test_restart_revokes_tokens_and_codes(
    service: SetupSecurityService, clock: FixedClock
) -> None:
    issued = service.issue_code(OPERATOR)
    token = service.exchange(issued.code, peer=LOCAL_PEER).token
    restarted = SetupSecurityService(environment_id=ENVIRONMENT_ID, clock=clock)

    with pytest.raises(SettingsSecurityError) as unknown_session:
        restarted.authorize(token)
    assert unknown_session.value.code == "SESSION_INVALID"

    with pytest.raises(SettingsSecurityError) as unknown_code:
        restarted.exchange(issued.code, peer=LOCAL_PEER)
    assert unknown_code.value.code == "CODE_EXPIRED"


# ---------------------------------------------------------------------------
# 失败限流
# ---------------------------------------------------------------------------


def test_failure_limit_is_per_code(service: SetupSecurityService) -> None:
    for _ in range(EXCHANGE_FAILURE_LIMIT):
        with pytest.raises(SettingsSecurityError) as failure:
            service.exchange("synthetic-rate-limited", peer=LOCAL_PEER)
        assert failure.value.code == "CODE_EXPIRED"

    with pytest.raises(SettingsSecurityError) as limited:
        service.exchange("synthetic-rate-limited", peer=LOCAL_PEER)

    assert limited.value.code == "EXCHANGE_RATE_LIMITED"
    assert limited.value.status_code == 429
    assert dict(limited.value.headers)["Retry-After"] == "60"

    # 另一个码不受影响：限流不能变成跨码的一刀切拒绝。
    with pytest.raises(SettingsSecurityError) as other:
        service.exchange("synthetic-other-code", peer=LOCAL_PEER)
    assert other.value.code == "CODE_EXPIRED"


def test_peer_ceiling_stops_code_spraying(service: SetupSecurityService) -> None:
    for index in range(EXCHANGE_PEER_FAILURE_LIMIT):
        with pytest.raises(SettingsSecurityError) as failure:
            service.exchange(f"synthetic-spray-{index}", peer=LOCAL_PEER)
        assert failure.value.code == "CODE_EXPIRED"

    with pytest.raises(SettingsSecurityError) as limited:
        service.exchange("synthetic-spray-final", peer=LOCAL_PEER)
    assert limited.value.code == "EXCHANGE_RATE_LIMITED"


def test_failure_window_expires(
    service: SetupSecurityService, clock: FixedClock
) -> None:
    for _ in range(EXCHANGE_FAILURE_LIMIT):
        with pytest.raises(SettingsSecurityError):
            service.exchange("synthetic-window", peer=LOCAL_PEER)

    clock.advance(timedelta(minutes=1, seconds=1))
    with pytest.raises(SettingsSecurityError) as allowed_again:
        service.exchange("synthetic-window", peer=LOCAL_PEER)
    assert allowed_again.value.code == "CODE_EXPIRED"


# ---------------------------------------------------------------------------
# Host / Origin / 对端
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("TestServer", "testserver"),
        ("HOST:80", "host:80"),
        ("127.0.0.1:8000", "127.0.0.1:8000"),
        ("[::1]:8000", "[::1]:8000"),
        ("[0:0:0:0:0:0:0:1]", "[::1]"),
    ],
)
def test_host_authority_is_normalized(raw: str, expected: str) -> None:
    assert str(parse_authority(raw)) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "evil.example/path",
        "a.example,b.example",
        "user@host:80",
        "host:99999",
        "host:abc",
        "[::1",
        "space host",
        "trailing.example.",
        "under_score:80",
        "::1",
        "",
    ],
)
def test_malformed_host_is_rejected(raw: str) -> None:
    with pytest.raises(SettingsSecurityError) as error:
        parse_authority(raw)
    assert error.value.code == "HOST_INVALID"
    assert error.value.status_code == 403


def test_multiple_host_headers_are_rejected() -> None:
    policy = SetupSourcePolicy()
    request = _request(
        headers=(("host", "127.0.0.1:8000"), ("host", "evil.example"))
    )

    with pytest.raises(SettingsSecurityError) as error:
        policy.check_request(request)
    assert error.value.code == "HOST_REQUIRED"


def test_exact_host_allowlist_blocks_other_authorities() -> None:
    policy = SetupSourcePolicy(
        runtime_kind=RuntimeKind.WINDOWS,
        allowed_hosts=frozenset({"127.0.0.1:8000"}),
    )

    allowed = policy.check_request(_request(headers=(("host", "127.0.0.1:8000"),)))
    assert str(allowed.host) == "127.0.0.1:8000"

    with pytest.raises(SettingsSecurityError) as rejected:
        policy.check_request(_request(headers=(("host", "127.0.0.1:8001"),)))
    assert rejected.value.code == "HOST_NOT_ALLOWED"


def test_origin_must_be_exact_same_origin() -> None:
    policy = SetupSourcePolicy()
    request = _request(headers=(("host", "127.0.0.1:8000"),))
    source = policy.check_request(request)

    assert (
        policy.match_origin("http://127.0.0.1:8000", source=source)
        == "http://127.0.0.1:8000"
    )

    for origin in (
        "https://127.0.0.1:8000",
        "http://127.0.0.1:8001",
        "http://localhost:8000",
        "https://evil.example",
    ):
        with pytest.raises(SettingsSecurityError) as rejected:
            policy.match_origin(origin, source=source)
        assert rejected.value.code == "ORIGIN_NOT_ALLOWED"

    for origin in (
        "null",
        "http://127.0.0.1:8000/",
        "http://user@127.0.0.1:8000",
        "http://127.0.0.1:8000,https://evil.example",
        "file://127.0.0.1",
        "127.0.0.1:8000",
    ):
        with pytest.raises(SettingsSecurityError) as invalid:
            parse_origin(origin)
        assert invalid.value.code == "ORIGIN_INVALID"


def test_origin_is_required_for_sensitive_actions_only() -> None:
    policy = SetupSourcePolicy()
    source = policy.check_request(_request(headers=(("host", "127.0.0.1:8000"),)))

    assert (
        policy.check_origin(
            _request(headers=(("host", "127.0.0.1:8000"),)),
            source=source,
            required=False,
        )
        is None
    )
    with pytest.raises(SettingsSecurityError) as required:
        policy.check_origin(
            _request(headers=(("host", "127.0.0.1:8000"),)),
            source=source,
            required=True,
        )
    assert required.value.code == "ORIGIN_REQUIRED"


def test_multiple_origin_headers_are_rejected() -> None:
    policy = SetupSourcePolicy()
    request = _request(
        headers=(
            ("host", "127.0.0.1:8000"),
            ("origin", "http://127.0.0.1:8000"),
            ("origin", "http://evil.example"),
        )
    )
    source = policy.check_request(request)

    with pytest.raises(SettingsSecurityError) as error:
        policy.check_origin(request, source=source, required=True)
    assert error.value.code == "ORIGIN_INVALID"


def test_localhost_is_not_blanket_allowed() -> None:
    policy = SetupSourcePolicy()
    source = policy.check_request(_request(headers=(("host", "127.0.0.1:8000"),)))

    with pytest.raises(SettingsSecurityError) as error:
        policy.match_origin("http://localhost:5173", source=source)
    assert error.value.code == "ORIGIN_NOT_ALLOWED"


def test_development_vite_origin_requires_explicit_opt_in() -> None:
    development = SetupSourcePolicy(
        environment=EnvironmentName.DEVELOPMENT,
        development_origins=frozenset({"http://localhost:5173"}),
    )
    source = development.check_request(_request(headers=(("host", "127.0.0.1:8000"),)))
    assert (
        development.match_origin("http://localhost:5173", source=source)
        == "http://localhost:5173"
    )

    with pytest.raises(ValueError):
        SetupSourcePolicy(
            environment=EnvironmentName.DEMO,
            development_origins=frozenset({"http://localhost:5173"}),
        )


@pytest.mark.parametrize("runtime", [RuntimeKind.WINDOWS, RuntimeKind.PYTHON])
def test_peer_must_be_loopback_for_local_runtimes(runtime: RuntimeKind) -> None:
    policy = SetupSourcePolicy(runtime_kind=runtime)

    policy.check_peer("127.0.0.1")
    policy.check_peer("::1")
    with pytest.raises(SettingsSecurityError) as rejected:
        policy.check_peer("203.0.113.9")
    assert rejected.value.code == "PEER_NOT_LOOPBACK"

    with pytest.raises(SettingsSecurityError):
        policy.check_request(_request(client=("203.0.113.9", 50000)))

    # 非 IP 对端（测试 transport / Unix socket）不冒充网络对端。
    assert policy.check_request(_request(client=("testclient", 50000))).peer == "testclient"
    assert policy.check_request(_request(client=None)).peer is None


def test_docker_nat_peer_is_not_trusted_as_loopback() -> None:
    policy = SetupSourcePolicy(
        environment=EnvironmentName.DEMO, runtime_kind=RuntimeKind.DOCKER
    )
    request = _request(
        headers=(
            ("host", "127.0.0.1:8000"),
            ("x-forwarded-for", "127.0.0.1"),
        ),
        client=("172.18.0.9", 50000),
    )

    source = policy.check_request(request)
    # 转发头不改变对端事实，容器 NAT 也不会被当成 loopback 而免去令牌。
    assert source.peer == "172.18.0.9"

    service = SetupSecurityService(
        environment_id=ENVIRONMENT_ID,
        environment=EnvironmentName.DEMO,
        runtime_kind=RuntimeKind.DOCKER,
        policy=policy,
    )
    with pytest.raises(SettingsSecurityError) as error:
        service.authorize("synthetic-token")
    assert error.value.code == "SESSION_INVALID"


def test_authorization_header_parser_is_strict() -> None:
    assert parse_setup_authorization(["Setup synthetic-token"]) == "synthetic-token"
    assert parse_setup_authorization(["setup synthetic-token"]) == "synthetic-token"

    for values in (
        [],
        ["Bearer synthetic-token"],
        ["Setup"],
        ["Setup one two"],
        ["Setup ", "Setup duplicate"],
        ["Basic synthetic-token"],
    ):
        assert parse_setup_authorization(values) is None


# ---------------------------------------------------------------------------
# 错误体与独立性
# ---------------------------------------------------------------------------


def test_error_payload_has_stable_shape_and_is_not_cacheable() -> None:
    response = error_response(
        SettingsSecurityError(
            "CODE_USED",
            "授权码已被使用，请从本机启动器重新获取。",
            retryable=True,
            stage="settings_session",
            headers=(("WWW-Authenticate", "Setup"),),
        )
    )
    body = bytes(response.body).decode("utf-8")

    assert response.status_code == 401
    assert set(json.loads(body)) == {
        "code",
        "message",
        "stage",
        "retryable",
        "correlation_id",
    }
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["www-authenticate"] == "Setup"
    assert "synthetic" not in body


def test_codes_and_tokens_are_not_in_repr(service: SetupSecurityService) -> None:
    issued = service.issue_code(OPERATOR)
    session = service.exchange(issued.code, peer=LOCAL_PEER)

    assert issued.code not in repr(issued)
    assert session.token not in repr(session)


FORBIDDEN_IMPORT_PREFIXES = (
    "backend.app.api",
    "backend.app.models",
    "backend.app.services",
    "backend.app.core.app",
    "backend.app.core.config",
    "backend.app.core.database",
    "backend.app.core.security",
    "backend.app.ai",
    "sqlalchemy",
    "jose",
    "jwt",
    "redis",
    "psycopg",
)


def test_security_modules_do_not_import_business_boundaries() -> None:
    """Setup 授权不得依赖业务数据库、业务 JWT 密钥或业务初始化。"""

    for relative in (
        "backend/app/core/settings_security.py",
        "backend/app/deployment/local_management.py",
    ):
        tree = ast.parse((REPO_ROOT / relative).read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        offenders = [
            name
            for name in imported
            if any(
                name == prefix or name.startswith(prefix + ".")
                for prefix in FORBIDDEN_IMPORT_PREFIXES
            )
        ]
        assert offenders == [], f"{T217}：{relative} 引入了业务边界 {offenders}"
