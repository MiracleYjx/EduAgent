"""TCR-S3-SETTINGS-SECURITY-019（T217）：设置授权 HTTP 契约（正向与错误路径）。

固定 `contracts/settings-api.md` §1/§1.2 的实际行为：

- 本机一次性码只能由进程内管理通道发行，HTTP 只能交换；
- Setup 令牌可进入管理面，业务 Bearer 与未知令牌一律 401；
- 敏感写操作要求同源 Origin 与 JSON，缺失/跨站 403、非 JSON 415；
- 所有设置响应 `no-store`；未注册的管理端点授权通过后仍是真实 404（T219/T220）。

不启动应用 lifespan、不连真实 DB/Redis、不调用模型；令牌与授权码全部为运行时合成值。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.core.app import create_app
from backend.app.core.settings_security import (
    OperatorIdentity,
    PrincipalKind,
    SetupSecurityService,
    SetupSourcePolicy,
)
from backend.app.schemas.settings import EnvironmentName, RuntimeKind
from tests.unit.settings_helpers import build_test_settings

T217 = "T217_SETTINGS_SECURITY_REQUIRED"
DEMO_ENVIRONMENT_ID = UUID("66666666-6666-4666-8666-666666666666")

OPERATOR = OperatorIdentity(
    kind=PrincipalKind.LOCAL_OS, principal_id="synthetic-contract-operator"
)
DRAFT_BODY: dict[str, object] = {
    "base_revision": None,
    "changes": {},
    "acknowledge_override": False,
}


def _build_app() -> FastAPI:
    return create_app(
        settings=build_test_settings(
            database_url="postgresql+psycopg://user:password@127.0.0.1:1/eduagent",
            redis_url="redis://127.0.0.1:1/0",
        )
    )


@pytest.fixture(scope="module")
def app() -> FastAPI:
    return _build_app()


@pytest.fixture(scope="module")
def client(app: FastAPI) -> Iterator[TestClient]:
    # scope="module" 的会话事实源与 T216 的独立；各用例使用互不相同的授权码。
    yield TestClient(app)


@pytest.fixture(autouse=True)
def _restore_security(app: FastAPI) -> Iterator[None]:
    """用例可临时替换本进程授权事实源，结束后恢复，避免隐式串味。"""

    original = app.state.setup_security
    yield
    app.state.setup_security = original


def _exchange(client: TestClient, code: str, *, origin: str | None = "http://testserver"):
    headers = {} if origin is None else {"Origin": origin}
    return client.post(
        "/api/settings/session/exchange", json={"one_time_code": code}, headers=headers
    )


def _setup_token(client: TestClient, app: FastAPI) -> str:
    response = _exchange(client, app.state.setup_security.issue_code(OPERATOR).code)
    assert response.status_code == 200, response.text
    return str(response.json()["management_token"])


def _setup_headers(token: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Setup {token}", **extra}


# ---------------------------------------------------------------------------
# 正向授权
# ---------------------------------------------------------------------------


def test_exchange_returns_bound_token_and_is_not_cacheable(
    client: TestClient, app: FastAPI
) -> None:
    service = app.state.setup_security
    issued = service.issue_code(OPERATOR)

    response = _exchange(client, issued.code)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "management_token",
        "expires_at",
        "environment_id",
        "instance_id",
    }
    assert body["management_token"] != issued.code
    assert issued.code not in response.text
    assert body["environment_id"] == str(service.environment_id)
    assert body["instance_id"] == str(service.instance_id)
    assert datetime.fromisoformat(body["expires_at"]) > datetime.now(UTC)
    assert "no-store" in response.headers["cache-control"]
    assert "access-control-allow-origin" not in response.headers


def test_management_reads_pass_the_gate_only_with_setup_token(
    client: TestClient, app: FastAPI
) -> None:
    token = _setup_token(client, app)

    authorized = client.get("/api/settings/", headers=_setup_headers(token))
    # T219 已提供只读安全视图；字段源/凭据状态/绑定由 T220 扩展。
    assert authorized.status_code == 200
    view = authorized.json()
    assert view["configuration"] == "available"
    assert view["business_assembly"] == "mounted"
    assert view["environment"] == "development"
    assert view["restart_required"] is False
    text = authorized.text.lower()
    for marker in ("postgresql://", "postgresql+", "redis://", "password", "api_key", "secret"):
        assert marker not in text
    assert authorized.headers.get("www-authenticate") is None
    assert "no-store" in authorized.headers["cache-control"]

    anonymous = client.get("/api/settings/")
    assert anonymous.status_code == 401
    assert anonymous.headers["www-authenticate"].startswith("Setup")
    assert "no-store" in anonymous.headers["cache-control"]


def test_management_writes_require_origin_and_json(
    client: TestClient, app: FastAPI
) -> None:
    token = _setup_token(client, app)
    authorization = _setup_headers(token)

    missing_origin = client.post("/api/settings/drafts", json=DRAFT_BODY, headers=authorization)
    assert missing_origin.status_code == 403
    assert missing_origin.json()["code"] == "ORIGIN_REQUIRED"
    assert "no-store" in missing_origin.headers["cache-control"]

    cross_site = client.post(
        "/api/settings/drafts",
        json=DRAFT_BODY,
        headers={**authorization, "Origin": "http://localhost:5173"},
    )
    assert cross_site.status_code == 403
    assert cross_site.json()["code"] == "ORIGIN_NOT_ALLOWED"

    non_json = client.post(
        "/api/settings/drafts",
        content="base_revision=1",
        headers={
            **authorization,
            "Origin": "http://testserver",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    assert non_json.status_code == 415
    assert non_json.json()["code"] == "JSON_REQUIRED"
    assert "no-store" in non_json.headers["cache-control"]

    authorized = client.post(
        "/api/settings/drafts",
        json=DRAFT_BODY,
        headers={**authorization, "Origin": "http://testserver"},
    )
    # T220：路由已存在；本 app 由 `create_app` 直接构造、没有设置运行时，
    # 因此授权通过后明确 503，而不是用 404 冒充“尚未提供”。
    assert authorized.status_code == 503
    assert authorized.json()["code"] == "SETTINGS_STORAGE_UNAVAILABLE"


def test_business_bearer_cannot_authorize_management(
    client: TestClient, app: FastAPI
) -> None:
    response = client.post(
        "/api/settings/drafts",
        json=DRAFT_BODY,
        headers={
            "Authorization": "Bearer synthetic-business-token",
            "Origin": "http://testserver",
        },
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Setup")


# ---------------------------------------------------------------------------
# 错误路径：重放、限流、未知入口
# ---------------------------------------------------------------------------


def test_exchange_replay_returns_code_used(client: TestClient, app: FastAPI) -> None:
    code = app.state.setup_security.issue_code(OPERATOR).code
    assert _exchange(client, code).status_code == 200

    replay = _exchange(client, code)

    assert replay.status_code == 401
    assert replay.json()["code"] == "CODE_USED"
    assert code not in replay.text
    assert "no-store" in replay.headers["cache-control"]


def test_exchange_rate_limits_repeated_failures(client: TestClient) -> None:
    codes = [
        _exchange(client, "synthetic-contract-rate-limit").status_code
        for _ in range(6)
    ]

    assert codes == [401, 401, 401, 401, 401, 429]


def test_concurrent_http_exchange_succeeds_once(app: FastAPI) -> None:
    """并发交换在 HTTP 层也不能重复成功；失败方一律 CODE_USED。"""

    code = app.state.setup_security.issue_code(OPERATOR).code
    barrier = threading.Barrier(4)
    results: list[tuple[int, str]] = []
    lock = threading.Lock()

    def attempt() -> None:
        thread_client = TestClient(app)
        barrier.wait()
        response = thread_client.post(
            "/api/settings/session/exchange",
            json={"one_time_code": code},
            headers={"Origin": "http://testserver"},
        )
        with lock:
            results.append((response.status_code, str(response.json().get("code", ""))))

    with ThreadPoolExecutor(max_workers=4) as pool:
        for _ in range(4):
            pool.submit(attempt)

    statuses = sorted(status for status, _ in results)
    assert statuses == [200, 401, 401, 401]
    assert [code for status, code in results if status == 401] == ["CODE_USED"] * 3


def test_management_paths_without_registered_route_are_not_issuance_channels(
    client: TestClient, app: FastAPI
) -> None:
    token = _setup_token(client, app)
    paths = {
        path
        for path in app.openapi()["paths"]
        if path.startswith("/api/settings")
    }

    # T220 补齐管理端点后不再固定为四项；仍要求它们真实注册且不新增领码通道。
    assert {
        "/api/settings/",
        "/api/settings/readiness",
        "/api/settings/diagnostics",
        "/api/settings/session/exchange",
        "/api/settings/drafts",
        "/api/settings/credentials",
        "/api/settings/probes",
        "/api/settings/onboarding",
        "/api/settings/index-identities",
    } <= paths

    for candidate in (
        "/api/settings/session/issue",
        "/api/settings/session/authorization-code",
        "/api/settings/session/token",
    ):
        response = client.post(
            candidate, json={}, headers=_setup_headers(token, Origin="http://testserver")
        )
        assert response.status_code == 404


def test_probe_and_readiness_are_behind_the_same_gate(client: TestClient) -> None:
    assert client.post("/api/settings/probes", json={}).status_code == 401
    assert client.get("/api/settings/readiness").status_code == 401
    assert client.get("/api/settings/providers").status_code == 401


# ---------------------------------------------------------------------------
# 对端与转发头边界
# ---------------------------------------------------------------------------


def test_non_loopback_peer_is_rejected_before_authorization(app: FastAPI) -> None:
    app.state.setup_security = SetupSecurityService(
        environment_id=DEMO_ENVIRONMENT_ID,
        environment=EnvironmentName.DEVELOPMENT,
        runtime_kind=RuntimeKind.WINDOWS,
        policy=SetupSourcePolicy(runtime_kind=RuntimeKind.WINDOWS),
    )
    remote = TestClient(app, client=("203.0.113.9", 50000))

    denied = remote.get("/api/settings/")
    assert denied.status_code == 403
    assert denied.json()["code"] == "PEER_NOT_LOOPBACK"

    exchange = remote.post(
        "/api/settings/session/exchange",
        json={"one_time_code": "synthetic-remote-code"},
        headers={"Origin": "http://testserver"},
    )
    assert exchange.status_code == 403


def test_docker_nat_peer_needs_token_and_ignores_forwarded_headers(
    app: FastAPI,
) -> None:
    service = SetupSecurityService(
        environment_id=DEMO_ENVIRONMENT_ID,
        environment=EnvironmentName.DEMO,
        runtime_kind=RuntimeKind.DOCKER,
        policy=SetupSourcePolicy(
            environment=EnvironmentName.DEMO, runtime_kind=RuntimeKind.DOCKER
        ),
    )
    app.state.setup_security = service
    container_peer = TestClient(app, client=("172.18.0.9", 50000))

    # 容器 NAT 对端不是 loopback，也不会因为 NAT 就被放行：仍然必须持令牌。
    anonymous = container_peer.get(
        "/api/settings/", headers={"X-Forwarded-For": "127.0.0.1"}
    )
    assert anonymous.status_code == 401

    # 受限管理入口仍可完成交换，说明 NAT 边界本身不阻断合法操作。
    grant = service.issue_code(OPERATOR)
    exchanged = _exchange(container_peer, grant.code)
    assert exchanged.status_code == 200
    token = str(exchanged.json()["management_token"])

    # 转发头不构成同源证据：显式外来 Origin 仍被拒绝。
    cross_site = container_peer.get(
        "/api/settings/",
        headers={
            "Authorization": f"Setup {token}",
            "Origin": "https://evil.example",
            "X-Forwarded-For": "127.0.0.1",
            "X-Forwarded-Host": "testserver",
        },
    )
    assert cross_site.status_code == 403
    assert cross_site.json()["code"] == "ORIGIN_NOT_ALLOWED"

    assert (
        container_peer.get(
            "/api/settings/", headers={"Authorization": f"Setup {token}"}
        ).status_code
        == 200
    )
