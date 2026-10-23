"""TCR-S3-BOOTSTRAP-SPLIT-020（T218）：最小引导 HTTP 契约与业务未就绪保护。

固定四件事：

1. 业务 DB/Redis/JWT/AI 配置缺失时，`backend.main` 可导入并启动，`/health` 仍只表示存活；
2. `/setup-ready` 不依赖业务装配；`/ready` 仍 503（业务未装配），`/gradio` 未挂载；
3. 未装配业务时所有 `/api/*` 以 `503 BUSINESS_NOT_READY` 拒绝，**不先落到业务鉴权**；
4. 业务配置显式齐备时仍装配原完整应用（业务 401、Gradio 可用），Setup 令牌不授权业务 API。

授权码与令牌全部为运行时合成值；不启动 lifespan、不连真实 DB/Redis、不调用模型。
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.core.bootstrap import (
    BUSINESS_NOT_READY,
    BootstrapContext,
    create_application,
    create_bootstrap_app,
    resolve_bootstrap_context,
)
from backend.app.core.settings_security import OperatorIdentity, PrincipalKind
from tests.unit.settings_helpers import build_test_settings

BUSINESS_ENV = (
    "DATABASE_URL",
    "REDIS_URL",
    "JWT_SECRET_KEY",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "EMBEDDING_API_KEY",
    "EMBEDDING_MODEL",
    "EMBEDDING_BASE_URL",
    "LLM_PROVIDER",
    "EMBEDDING_PROVIDER",
    "RERANK_PROVIDER",
    "CONFIDENCE_THRESHOLD",
)
BIND_ENV = ("EDUAGENT_BIND_HOST", "EDUAGENT_BIND_PORT", "EDUAGENT_SETTINGS_UI_ROOT")

#: 一份“若被隐式读取就会让业务工厂通过”的合成 .env；全部是假值，不含真实凭据。
SYNTHETIC_DOTENV = """DATABASE_URL=postgresql+psycopg://synthetic:synthetic@127.0.0.1:1/synthetic
REDIS_URL=redis://127.0.0.1:1/0
JWT_SECRET_KEY=synthetic-jwt-secret-that-is-not-a-production-secret
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=synthetic-key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
EMBEDDING_PROVIDER=local
EMBEDDING_MODEL=synthetic-embedding-model
EMBEDDING_BASE_URL=https://embedding.example.com/v1
EMBEDDING_API_KEY=synthetic-embedding-key
RERANK_PROVIDER=none
CONFIDENCE_THRESHOLD=0.8
"""

SECRET_MARKERS = (
    "postgresql://",
    "postgresql+",
    "redis://",
    "password",
    "api_key",
    "secret",
    "bearer ",
)

OPERATOR = OperatorIdentity(
    kind=PrincipalKind.LOCAL_OS, principal_id="synthetic-bootstrap-contract"
)


def _clear_business_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*BUSINESS_ENV, *BIND_ENV):
        monkeypatch.delenv(name, raising=False)


def _setup_headers(token: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Setup {token}", **extra}


@pytest.fixture()
def bootstrap_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[FastAPI]:
    """无业务配置、且当前目录存在“完整 .env”的启动场景。"""

    _clear_business_env(monkeypatch)
    data_root = (tmp_path / "data").resolve()
    data_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("EDUAGENT_ENVIRONMENT", "development")
    monkeypatch.setenv("EDUAGENT_DATA_ROOT", str(data_root))
    monkeypatch.setenv("GRADIO_ANALYTICS_ENABLED", "False")
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(SYNTHETIC_DOTENV, encoding="utf-8")
    sys.modules.pop("backend.main", None)

    module = importlib.import_module("backend.main")

    yield module.app
    sys.modules.pop("backend.main", None)


@pytest.fixture(scope="module")
def full_app() -> FastAPI:
    """显式业务配置：仍走原完整工厂（Gradio 全壳 + 全部业务路由）。"""

    return create_application(
        settings=build_test_settings(
            database_url="postgresql+psycopg://user:password@127.0.0.1:1/eduagent",
            redis_url="redis://127.0.0.1:1/0",
        )
    )


@pytest.fixture(scope="module")
def full_client(full_app: FastAPI) -> Iterator[TestClient]:
    yield TestClient(full_app)


# ---------------------------------------------------------------------------
# 无业务配置启动
# ---------------------------------------------------------------------------


def test_minimal_bootstrap_imports_and_serves_setup(bootstrap_main: FastAPI) -> None:
    client = TestClient(bootstrap_main)

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "service": "backend"}

    setup = client.get("/setup-ready")
    assert setup.status_code == 200
    body = setup.json()
    assert body["status"] == "ok"
    assert body["setup"]["configuration"] == "available"
    assert body["setup"]["settings_authorization"] == "available"
    assert body["setup"]["static_resources"] == "not_bundled"
    text = setup.text.lower()
    for marker in SECRET_MARKERS:
        assert marker not in text


def test_business_api_is_blocked_with_503(bootstrap_main: FastAPI) -> None:
    client = TestClient(bootstrap_main)

    for method, path in (
        ("get", "/api/courses"),
        ("get", "/api/workflow/runs"),
        ("post", "/api/workflow/run"),
        ("put", "/api/courses/1"),
        ("delete", "/api/courses/1"),
    ):
        response = getattr(client, method)(path)
        assert response.status_code == 503, f"{method.upper()} {path}"
        body = response.json()
        assert body["code"] == BUSINESS_NOT_READY
        assert body["stage"] == "business_assembly"
        assert body["retryable"] is True
        assert "postgresql" not in response.text.lower()
        assert "redis://" not in response.text.lower()


def test_business_block_happens_before_authentication(bootstrap_main: FastAPI) -> None:
    client = TestClient(bootstrap_main)

    assert client.get("/api/courses").status_code == 503
    assert (
        client.get(
            "/api/courses", headers={"Authorization": "Bearer synthetic-business-token"}
        ).status_code
        == 503
    )
    assert client.post("/api/workflow/run", json={"synthetic": True}).status_code == 503


def test_ready_is_503_and_gradio_is_not_mounted(bootstrap_main: FastAPI) -> None:
    client = TestClient(bootstrap_main)

    ready = client.get("/ready")
    assert ready.status_code == 503
    assert ready.json()["failed_check"] == "assembly"

    assert client.get("/gradio/").status_code == 404

    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/settings/session/exchange" in paths
    assert not any(path.startswith(("/api/courses", "/api/workflow")) for path in paths)


def test_settings_authorization_stays_authoritative(bootstrap_main: FastAPI) -> None:
    client = TestClient(bootstrap_main)

    assert client.get("/api/settings/").status_code == 401
    assert (
        client.get(
            "/api/settings/", headers={"Authorization": "Bearer synthetic-business-token"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/settings/session/exchange", json={"one_time_code": "synthetic-code"}
        ).status_code
        == 403
    )

    security = bootstrap_main.state.setup_security
    code = security.issue_code(OPERATOR).code
    exchanged = client.post(
        "/api/settings/session/exchange",
        json={"one_time_code": code},
        headers={"Origin": "http://testserver"},
    )
    assert exchanged.status_code == 200
    token = exchanged.json()["management_token"]

    # 已授权只读安全视图（T219）；Setup 令牌仍不变成业务权限。
    view = client.get("/api/settings/", headers=_setup_headers(token))
    assert view.status_code == 200
    assert view.json()["configuration"] == "not_configured"
    assert view.json()["business_assembly"] == "not_mounted"
    assert (
        client.get("/api/courses", headers=_setup_headers(token)).status_code == 503
    )


# ---------------------------------------------------------------------------
# 绑定、静态资源与降级
# ---------------------------------------------------------------------------


def _minimal_environ(root: Path, **overrides: str) -> dict[str, str]:
    values = {
        "EDUAGENT_ENVIRONMENT": "development",
        "EDUAGENT_DATA_ROOT": str(root / "data"),
        "GRADIO_ANALYTICS_ENABLED": "False",
    }
    values.update(overrides)
    return values


def test_explicit_bind_limits_settings_hosts(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    context = resolve_bootstrap_context(
        environ=_minimal_environ(
            root, EDUAGENT_BIND_HOST="127.0.0.1", EDUAGENT_BIND_PORT="8123"
        )
    )
    client = TestClient(
        create_bootstrap_app(context=context), base_url="http://127.0.0.1:8123"
    )

    assert client.get("/api/settings/").status_code == 401

    foreign = client.get("/api/settings/", headers={"Host": "evil.example:8123"})
    assert foreign.status_code == 403
    assert foreign.json()["code"] == "HOST_NOT_ALLOWED"

    other_port = client.get("/api/settings/", headers={"Host": "127.0.0.1:8124"})
    assert other_port.status_code == 403

    # 未装配业务的保护不受绑定校验影响。
    assert client.get("/api/courses").status_code == 503


def test_static_bundle_mounts_without_shadowing_health_or_api(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    (root / "index.html").write_text("<html>settings-ui</html>", encoding="utf-8")
    context = resolve_bootstrap_context(
        environ=_minimal_environ(root, EDUAGENT_SETTINGS_UI_ROOT=str(root))
    )
    client = TestClient(create_bootstrap_app(context=context))

    assert client.get("/setup-ready").json()["setup"]["static_resources"] == "available"
    assert client.get("/health").status_code == 200
    assert client.get("/api/courses").status_code == 503
    index = client.get("/")
    assert index.status_code == 200
    assert "settings-ui" in index.text


def test_degraded_context_serves_only_safe_status() -> None:
    context = BootstrapContext(
        settings=None,
        bind=None,
        static_root=None,
        failure="ENVIRONMENT_PROFILE_UNAVAILABLE",
    )
    client = TestClient(create_bootstrap_app(context=context))

    assert client.get("/health").status_code == 200

    setup = client.get("/setup-ready")
    assert setup.status_code == 503
    assert setup.json()["failed_check"] == "config"
    assert "ENVIRONMENT_PROFILE_UNAVAILABLE" not in setup.text

    settings = client.get("/api/settings/")
    assert settings.status_code == 503
    assert settings.json()["code"] == "SETTINGS_SECURITY_UNAVAILABLE"

    business = client.get("/api/courses")
    assert business.status_code == 503
    assert business.json()["code"] == BUSINESS_NOT_READY


# ---------------------------------------------------------------------------
# 显式配置下的完整应用（不破坏既有业务）
# ---------------------------------------------------------------------------


def test_explicit_business_configuration_keeps_full_app(
    full_app: FastAPI, full_client: TestClient
) -> None:
    assert full_app.state.business_assembly == "assembled"

    assert full_client.get("/api/courses").status_code == 404
    assert full_client.get("/api/settings/").status_code == 401
    assert full_client.get("/setup-ready").status_code == 200

    ready = full_client.get("/ready")
    assert ready.status_code == 503
    assert ready.json()["failed_check"] == "postgres"

    gradio = full_client.get("/gradio/", follow_redirects=False)
    assert gradio.status_code == 404


def test_setup_token_never_authorizes_business_api(
    full_app: FastAPI, full_client: TestClient
) -> None:
    security = full_app.state.setup_security
    code = security.issue_code(OPERATOR).code
    exchanged = full_client.post(
        "/api/settings/session/exchange",
        json={"one_time_code": code},
        headers={"Origin": "http://testserver"},
    )
    assert exchanged.status_code == 200
    token = exchanged.json()["management_token"]

    assert (
        full_client.get("/api/courses", headers=_setup_headers(token)).status_code == 404
    )
