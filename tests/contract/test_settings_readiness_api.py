"""TCR-S3-READINESS-021（T219）：分层就绪/安全诊断 HTTP 契约。

验证四层健康状态的 HTTP 行为、Setup 授权边界与信息脱敏：

- `/health` 仅存活；`/setup-ready` 只反映设置面；`/ready` 反映业务前置+装配+恢复；
- `GET /api/settings/` 与 `GET /api/settings/readiness` 需 Setup 令牌，`POST /diagnostics`
  另需合法 Origin 与 JSON；
- AI 段固定 `evaluated=false`/`not_evaluated`，不因 Provider 配置存在而伪造可用；
- 响应不含连接串、密码、路径或异常原文。

全部使用不可达依赖（127.0.0.1:1）与运行时合成令牌；不启动 lifespan、不连真实资源。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.core.bootstrap import (
    create_application,
    create_bootstrap_app,
    resolve_bootstrap_context,
)
from backend.app.core.settings_security import OperatorIdentity, PrincipalKind
from tests.unit.settings_helpers import build_test_settings

OPERATOR = OperatorIdentity(
    kind=PrincipalKind.LOCAL_OS, principal_id="synthetic-readiness-contract"
)
SECRET_MARKERS = (
    "postgresql://",
    "postgresql+",
    "redis://",
    "password",
    "api_key",
    "secret",
    "bearer ",
    "127.0.0.1",
    "traceback",
)
PREREQUISITE_NAMES = {
    "business_config",
    "jwt_reference",
    "postgres",
    "vector",
    "migration",
    "schema",
    "redis",
    "storage",
}


def _build_full_app(storage_root: Path) -> FastAPI:
    return create_application(
        settings=build_test_settings(
            database_url="postgresql+psycopg://user:password@127.0.0.1:1/eduagent",
            redis_url="redis://127.0.0.1:1/0",
            storage_root=storage_root,
        )
    )


@pytest.fixture(scope="module")
def full_app(tmp_path_factory: pytest.TempPathFactory) -> FastAPI:
    return _build_full_app(tmp_path_factory.mktemp("readiness-storage"))


@pytest.fixture(scope="module")
def client(full_app: FastAPI) -> Iterator[TestClient]:
    yield TestClient(full_app)


@pytest.fixture()
def token(client: TestClient, full_app: FastAPI) -> str:
    service = full_app.state.setup_security
    code = service.issue_code(OPERATOR).code
    exchanged = client.post(
        "/api/settings/session/exchange",
        json={"one_time_code": code},
        headers={"Origin": "http://testserver"},
    )
    assert exchanged.status_code == 200, exchanged.text
    return str(exchanged.json()["management_token"])


def _auth(token: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Setup {token}", **extra}


def _minimal_environ(root: Path) -> dict[str, str]:
    return {
        "EDUAGENT_ENVIRONMENT": "development",
        "EDUAGENT_DATA_ROOT": str(root / "data"),
        "GRADIO_ANALYTICS_ENABLED": "False",
    }


@pytest.fixture()
def bootstrap_client(tmp_path: Path) -> Iterator[TestClient]:
    context = resolve_bootstrap_context(environ=_minimal_environ(tmp_path.resolve()))
    yield TestClient(create_bootstrap_app(context=context))


def _bootstrap_token(client: TestClient) -> str:
    service = cast(FastAPI, client.app).state.setup_security
    code = service.issue_code(OPERATOR).code
    exchanged = client.post(
        "/api/settings/session/exchange",
        json={"one_time_code": code},
        headers={"Origin": "http://testserver"},
    )
    assert exchanged.status_code == 200, exchanged.text
    return str(exchanged.json()["management_token"])


# ---------------------------------------------------------------------------
# 授权边界
# ---------------------------------------------------------------------------


def test_readiness_and_view_require_setup_authorization(client: TestClient) -> None:
    for path in ("/api/settings/", "/api/settings/readiness"):
        anonymous = client.get(path)
        assert anonymous.status_code == 401, path
        assert anonymous.headers["www-authenticate"].startswith("Setup")

        bearer = client.get(path, headers={"Authorization": "Bearer synthetic-token"})
        assert bearer.status_code == 401, path


def test_diagnostics_requires_origin_and_json(client: TestClient, token: str) -> None:
    missing_origin = client.post(
        "/api/settings/diagnostics",
        json={"checks": ["storage"]},
        headers=_auth(token),
    )
    assert missing_origin.status_code == 403
    assert missing_origin.json()["code"] == "ORIGIN_REQUIRED"

    non_json = client.post(
        "/api/settings/diagnostics",
        content="checks=storage",
        headers=_auth(
            token,
            Origin="http://testserver",
            **{"Content-Type": "application/x-www-form-urlencoded"},
        ),
    )
    assert non_json.status_code == 415
    assert non_json.json()["code"] == "JSON_REQUIRED"


# ---------------------------------------------------------------------------
# 分层报告
# ---------------------------------------------------------------------------


def test_readiness_report_exposes_layers_and_never_evaluates_ai(
    client: TestClient, token: str
) -> None:
    response = client.get("/api/settings/readiness", headers=_auth(token))

    assert response.status_code == 200
    body = response.json()
    assert body["setup"]["ready"] is True
    assert body["business"]["assembly"] == "mounted"
    # 未启动 lifespan：恢复阶段不得被伪装成 completed。
    assert body["business"]["recovery"] == "not_started"
    assert body["business"]["ready"] is False
    assert body["business_prerequisites"]["ready"] is False

    checks = body["business_prerequisites"]["checks"]
    assert set(checks) == PREREQUISITE_NAMES
    assert checks["business_config"]["status"] == "ok"
    assert checks["jwt_reference"]["status"] == "ok"
    assert checks["postgres"]["status"] == "failed"
    assert checks["postgres"]["code"] == "postgres_unavailable"
    assert checks["migration"]["status"] == "skipped"

    assert body["ai"]["evaluated"] is False
    assert "not_evaluated" in body["ai"]["status"].lower()
    assert body["ai"]["tasks"] == {}
    assert body["restart_required"] is False
    assert body["static_resources"] == "not_bundled"


def test_readiness_never_leaks_paths_or_secrets(client: TestClient, token: str) -> None:
    response = client.get("/api/settings/readiness", headers=_auth(token))

    text = response.text.lower()
    for marker in SECRET_MARKERS:
        assert marker not in text, marker
    assert "synthetic" not in text


def test_settings_safe_view_is_non_sensitive(client: TestClient, token: str) -> None:
    response = client.get("/api/settings/", headers=_auth(token))

    assert response.status_code == 200
    body = response.json()
    assert body["configuration"] == "available"
    assert body["environment"] == "development"
    assert body["business_assembly"] == "mounted"
    assert body["static_resources"] == "not_bundled"
    assert body["active_revision"] is None
    assert body["restart_required"] is False
    text = response.text.lower()
    for marker in SECRET_MARKERS:
        assert marker not in text


# ---------------------------------------------------------------------------
# 诊断（只读、受限）
# ---------------------------------------------------------------------------


def test_diagnostics_runs_only_requested_core_checks(
    client: TestClient, token: str
) -> None:
    response = client.post(
        "/api/settings/diagnostics",
        json={"checks": ["storage"]},
        headers=_auth(token, Origin="http://testserver"),
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"status", "checks", "evaluated_at"}
    assert set(body["checks"]) == {"storage"}
    assert body["checks"]["storage"]["status"] == "ok"
    assert body["status"] == "ok"


def test_diagnostics_reports_unreachable_dependencies_safely(
    client: TestClient, token: str
) -> None:
    response = client.post(
        "/api/settings/diagnostics",
        json={"checks": ["postgres", "migrations", "redis"]},
        headers=_auth(token, Origin="http://testserver"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["postgres"]["code"] == "postgres_unavailable"
    assert body["checks"]["migrations"]["status"] == "skipped"
    assert body["checks"]["redis"]["code"] == "redis_unavailable"
    text = response.text.lower()
    for marker in SECRET_MARKERS:
        assert marker not in text


@pytest.mark.parametrize(
    "payload",
    [
        {"checks": ["ai"]},
        {"checks": []},
        {"checks": ["storage", "storage"]},
        {"checks": ["postgres", "shell"]},
    ],
)
def test_diagnostics_rejects_unknown_empty_or_duplicate_checks(
    client: TestClient, token: str, payload: dict[str, object]
) -> None:
    response = client.post(
        "/api/settings/diagnostics",
        json=payload,
        headers=_auth(token, Origin="http://testserver"),
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "INVALID_CONFIGURATION"
    assert body["stage"] == "validation"
    assert body["message"] == "配置结构无效，请检查字段取值。"
    assert body["retryable"] is False
    assert body["correlation_id"]
    assert "detail" not in body
    assert "input" not in body


# ---------------------------------------------------------------------------
# Bootstrap 模式：安全但不就绪
# ---------------------------------------------------------------------------


def test_bootstrap_mode_reports_safe_not_ready_state(
    bootstrap_client: TestClient,
) -> None:
    token = _bootstrap_token(bootstrap_client)

    report = bootstrap_client.get(
        "/api/settings/readiness", headers=_auth(token)
    ).json()
    assert report["setup"]["ready"] is True
    assert report["business_prerequisites"]["ready"] is False
    checks = report["business_prerequisites"]["checks"]
    assert checks["business_config"]["code"] == "business_config_missing"
    for name in ("postgres", "redis", "storage"):
        assert checks[name]["status"] == "skipped"
    assert report["business"]["assembly"] == "not_mounted"
    assert report["business"]["ready"] is False
    assert report["ai"]["evaluated"] is False
    assert report["static_resources"] == "not_bundled"

    view = bootstrap_client.get("/api/settings/", headers=_auth(token)).json()
    assert view["configuration"] == "not_configured"
    assert view["business_assembly"] == "not_mounted"

    ready = bootstrap_client.get("/ready")
    assert ready.status_code == 503
    assert ready.json()["failed_check"] == "assembly"
    assert bootstrap_client.get("/health").status_code == 200
    assert bootstrap_client.get("/setup-ready").status_code == 200


def test_bootstrap_diagnostics_is_skipped_and_degraded(
    bootstrap_client: TestClient,
) -> None:
    token = _bootstrap_token(bootstrap_client)

    response = bootstrap_client.post(
        "/api/settings/diagnostics",
        json={"checks": ["postgres", "storage"]},
        headers=_auth(token, Origin="http://testserver"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["postgres"]["status"] == "skipped"
    assert body["checks"]["storage"]["status"] == "skipped"


# ---------------------------------------------------------------------------
# /ready 与 /setup-ready 分离
# ---------------------------------------------------------------------------


def test_ready_reports_prerequisite_failure_while_setup_stays_available(
    client: TestClient,
) -> None:
    ready = client.get("/ready")
    assert ready.status_code == 503
    body = ready.json()
    assert body["failed_check"] == "postgres"
    assert body["detail"]

    assert client.get("/health").status_code == 200
    assert client.get("/setup-ready").status_code == 200
