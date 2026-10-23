"""TCR-S3-BOOTSTRAP-HEALTH-018（T216）：设置 API 权限/来源矩阵与健康语义契约。

本文件只固定 `contracts/settings-api.md` §1、§1.2、§5 的目标接口：

- 已实现部分（`/health` 仅存活、`/ready` 失败体形态、业务鉴权不变）必须保持通过；
- T217–T220 的目标项以 `*_REQUIRED` 任务码归因为**可预期红灯**，不得用 skip 或放宽断言掩盖。

边界：合成设置指向 `127.0.0.1:1` 的不可达 PG/Redis，不启动应用 lifespan，不读写真实
凭据、不调用模型、不触碰 Development/Demo；管理令牌一律用合成值，不伪造真实会话。
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.core.app import create_app
from backend.app.core.settings_security import OperatorIdentity, PrincipalKind
from tests.unit.settings_helpers import build_test_settings

T217 = "T217_SETTINGS_SECURITY_REQUIRED"
T218 = "T218_BOOTSTRAP_REQUIRED"
T219 = "T219_READINESS_REQUIRED"

#: 契约 §5 允许的失败检查项；新增检查项不得把 AI 缺失混成 PG 故障。
READY_CHECKS = frozenset(
    {
        "config",
        "postgres",
        "redis",
        "storage",
        "migrations",
        "business_prerequisites",
        "assembly",
        "recovery",
        "business",
    }
)

#: 公开/失败响应中不得出现的敏感片段（产品名不算秘密，只禁连接串与密钥词）。
SECRET_MARKERS = (
    "postgresql://",
    "postgresql+",
    "redis://",
    "password",
    "api_key",
    "secret",
    "bearer ",
)

DRAFT_BODY: dict[str, object] = {
    "base_revision": None,
    "changes": {},
    "acknowledge_override": False,
}


def _require(condition: bool, code: str, detail: str) -> None:
    """把“后续任务尚未实现”与真实缺陷分开归因。"""

    if not condition:
        pytest.fail(f"{code}：{detail}", pytrace=False)


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    """未装配业务的真实应用；依赖指向不可达端口，且不触发 lifespan。"""

    app = create_app(
        settings=build_test_settings(
            database_url="postgresql+psycopg://user:password@127.0.0.1:1/eduagent",
            redis_url="redis://127.0.0.1:1/0",
        )
    )
    yield TestClient(app)


# ---------------------------------------------------------------------------
# §5 公开健康语义：存活、就绪与静态可用分离
# ---------------------------------------------------------------------------


def test_health_is_liveness_only_and_secret_free(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "backend"}
    body = response.text.lower()
    for marker in SECRET_MARKERS:
        assert marker not in body
    headers = {name.lower() for name in response.headers}
    assert "set-cookie" not in headers


def test_health_needs_no_authorization(client: TestClient) -> None:
    assert client.get("/health").status_code == 200
    # 业务/管理令牌都不是存活检查的前提；携带也不改变结果。
    assert (
        client.get(
            "/health", headers={"Authorization": "Bearer synthetic-not-a-token"}
        ).status_code
        == 200
    )


def test_ready_failure_body_is_safe_and_classified(client: TestClient) -> None:
    response = client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["service"] == "backend"
    assert body["failed_check"] in READY_CHECKS
    assert isinstance(body["detail"], str) and body["detail"]
    text = response.text.lower()
    for marker in SECRET_MARKERS:
        assert marker not in text


def test_setup_ready_is_available_while_business_is_not_ready(
    client: TestClient,
) -> None:
    response = client.get("/setup-ready")

    _require(
        response.status_code != 404,
        T218,
        "/setup-ready 尚未提供（最小根应用与公开健康路由归 T218）",
    )
    assert response.status_code == 200
    assert response.json().get("status") == "ok"
    # 静态可用不等于业务健康：同一进程的 /ready 仍必须失败。
    assert client.get("/ready").status_code == 503


def test_setup_ready_does_not_leak_paths_or_credentials(client: TestClient) -> None:
    response = client.get("/setup-ready")

    _require(
        response.status_code != 404,
        T218,
        "/setup-ready 尚未提供（T218）",
    )
    text = response.text.lower()
    for marker in SECRET_MARKERS:
        assert marker not in text


def test_minimal_app_builds_without_business_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """T218：最小引导不得要求 DB/Key/JWT，且不得在导入期拉起 Gradio 全壳。"""

    for name in (
        "DATABASE_URL",
        "REDIS_URL",
        "JWT_SECRET_KEY",
        "DEEPSEEK_API_KEY",
        "EMBEDDING_API_KEY",
        "LLM_PROVIDER",
        "EMBEDDING_PROVIDER",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EDUAGENT_ENVIRONMENT", "development")
    monkeypatch.setenv("EDUAGENT_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("GRADIO_ANALYTICS_ENABLED", "False")
    sys.modules.pop("backend.main", None)

    try:
        module = importlib.import_module("backend.main")
    except Exception as error:  # noqa: BLE001 - 归因需要保留原始异常类型
        pytest.fail(
            f"{T218}：最小引导仍要求业务配置（{type(error).__name__}: {error}）",
            pytrace=False,
        )

    client = TestClient(module.app)
    business = client.get("/api/courses")
    _require(
        business.status_code == 503
        and business.json().get("code") == "BUSINESS_NOT_READY",
        T218,
        "最小引导仍装配业务入口（契约要求未装配时 503 BUSINESS_NOT_READY，业务装配归 T220）",
    )
    assert client.get("/health").status_code == 200


# ---------------------------------------------------------------------------
# §1/§1.2 Settings 读权限：Setup 令牌、来源与 Host
# ---------------------------------------------------------------------------


def test_settings_read_requires_setup_token(client: TestClient) -> None:
    response = client.get("/api/settings/")

    _require(
        response.status_code != 404,
        T217,
        "Settings GET 尚未提供（独立管理授权归 T217）",
    )
    assert response.status_code == 401
    assert "www-authenticate" in {name.lower() for name in response.headers}


def test_settings_read_rejects_business_bearer_token(client: TestClient) -> None:
    response = client.get(
        "/api/settings/", headers={"Authorization": "Bearer synthetic-business-token"}
    )

    _require(
        response.status_code != 404,
        T217,
        "Settings GET 尚未提供（T217）",
    )
    assert response.status_code == 401


def test_settings_read_rejects_unknown_setup_token(client: TestClient) -> None:
    response = client.get(
        "/api/settings/", headers={"Authorization": "Setup synthetic-unknown-token"}
    )

    _require(
        response.status_code != 404,
        T217,
        "Settings GET 尚未提供（T217）",
    )
    assert response.status_code == 401


def test_settings_read_rejects_foreign_host(client: TestClient) -> None:
    response = client.get(
        "/api/settings/",
        headers={"Host": "evil.example", "Authorization": "Setup synthetic-unknown-token"},
    )

    _require(
        response.status_code != 404,
        T217,
        "Settings GET 尚未提供（精确 Host 校验归 T217）",
    )
    assert response.status_code in {401, 403}


# ---------------------------------------------------------------------------
# §1.2 管理员入口 exchange：来源、一次性码与限流（无需现有令牌即可验证）
# ---------------------------------------------------------------------------


def _exchange(client: TestClient, *, origin: str | None, code: str = "synthetic-code"):
    headers = {} if origin is None else {"Origin": origin}
    return client.post(
        "/api/settings/session/exchange", json={"one_time_code": code}, headers=headers
    )


@pytest.mark.parametrize("origin", [None, "null", "https://evil.example"])
def test_exchange_rejects_missing_null_or_foreign_origin(
    client: TestClient, origin: str | None
) -> None:
    response = _exchange(client, origin=origin)

    _require(
        response.status_code != 404,
        T217,
        "POST /api/settings/session/exchange 尚未提供（T217）",
    )
    assert response.status_code == 403
    assert "access-control-allow-origin" not in {
        name.lower() for name in response.headers
    }


def test_exchange_rejects_expired_or_unknown_code(client: TestClient) -> None:
    response = _exchange(client, origin="http://testserver", code="synthetic-expired-code")

    _require(
        response.status_code != 404,
        T217,
        "POST /api/settings/session/exchange 尚未提供（T217）",
    )
    assert response.status_code == 401
    body = response.json()
    assert body.get("code") in {"CODE_EXPIRED", "CODE_USED"}


def test_exchange_rate_limits_repeated_failures(client: TestClient) -> None:
    codes = []
    for _ in range(6):
        response = _exchange(
            client, origin="http://testserver", code="synthetic-rate-limited"
        )
        codes.append(response.status_code)

    _require(
        404 not in codes,
        T217,
        "POST /api/settings/session/exchange 尚未提供（T217）",
    )
    assert codes[:5] == [401] * 5
    assert codes[5] == 429


def test_exchange_response_is_not_cacheable(client: TestClient) -> None:
    response = _exchange(client, origin="http://testserver")

    _require(
        response.status_code != 404,
        T217,
        "POST /api/settings/session/exchange 尚未提供（T217）",
    )
    assert "no-store" in response.headers.get("cache-control", "")


# ---------------------------------------------------------------------------
# §1.2 敏感写操作：无令牌/无来源/跨站一律不得成功
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "null"},
        {"Origin": "https://evil.example"},
        {"Origin": "http://localhost:5173"},
        {"Authorization": "Bearer synthetic-business-token"},
    ],
)
def test_settings_write_never_succeeds_without_setup_authorization(
    client: TestClient, headers: dict[str, str]
) -> None:
    response = client.post("/api/settings/drafts", json=DRAFT_BODY, headers=headers)

    _require(
        response.status_code != 404,
        T217,
        "Settings 写入口尚未提供（T217）",
    )
    assert response.status_code in {401, 403}


def test_settings_write_rejects_non_json_contract(client: TestClient) -> None:
    response = client.post(
        "/api/settings/drafts",
        content="base_revision=1",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    _require(
        response.status_code != 404,
        T217,
        "Settings 写入口尚未提供（T217）",
    )
    assert response.status_code in {401, 403, 415, 422}


# ---------------------------------------------------------------------------
# §1.2 开发来源：默认（非 development）不得放行 Vite 来源
# ---------------------------------------------------------------------------


def test_vite_origin_is_not_allowed_by_default(client: TestClient) -> None:
    response = client.options(
        "/api/settings/session/exchange",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    # 默认（非 development）不得回显任何允许来源；无通配 CORS。
    assert not response.headers.get("access-control-allow-origin")


# ---------------------------------------------------------------------------
# 权限边界：Setup 令牌不能授权业务 API，旧业务 JWT 语义不变
# ---------------------------------------------------------------------------


def test_setup_token_cannot_authorize_business_api(client: TestClient) -> None:
    response = client.get(
        "/api/courses", headers={"Authorization": "Setup synthetic-management-token"}
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_retired_business_api_is_absent(client: TestClient) -> None:
    assert client.get("/api/courses").status_code == 404


# ---------------------------------------------------------------------------
# §5 就绪报告：查询不触发模型，AI 段必须显式未评估
# ---------------------------------------------------------------------------


def test_readiness_reports_ai_as_not_evaluated(client: TestClient) -> None:
    """T219：经 T217 本机发行接缝取真实令牌后再读 readiness。"""

    service = cast(FastAPI, client.app).state.setup_security
    issued = service.issue_code(
        OperatorIdentity(
            kind=PrincipalKind.LOCAL_OS, principal_id="synthetic-t216-readiness"
        )
    )
    exchanged = client.post(
        "/api/settings/session/exchange",
        json={"one_time_code": issued.code},
        headers={"Origin": "http://testserver"},
    )
    assert exchanged.status_code == 200

    response = client.get(
        "/api/settings/readiness",
        headers={"Authorization": f"Setup {exchanged.json()['management_token']}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["setup"]["ready"] is True
    assert body["business"]["assembly"] == "mounted"
    assert body["ai"]["evaluated"] is False
    assert "not_evaluated" in str(body["ai"].get("status", "")).lower()
