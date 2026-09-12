from unittest.mock import Mock

import gradio as gr
import pytest
from fastapi.testclient import TestClient

from backend.app.core import app as app_module
from backend.app.core.app import create_app
from backend.app.core.config import AppSettings, ConfigurationError
from backend.app.core.database import DatabaseNotReadyError
from backend.app.core.redis import RedisNotReadyError
from tests.unit.settings_helpers import build_test_settings


def build_settings() -> AppSettings:
    return build_test_settings()


def test_health_endpoint_and_gradio_mount() -> None:
    app = create_app(settings=build_settings())

    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "backend"}
    assert any(
        str(getattr(route, "path", "")).startswith("/gradio") for route in app.routes
    )


@pytest.mark.parametrize("failed", [None, "config", "postgres", "redis"])
def test_readiness_checks_dependencies_and_redacts_errors(
    monkeypatch: pytest.MonkeyPatch, failed: str | None
) -> None:
    """依赖按顺序检查，任一失败返回 503，存活探针始终不访问依赖。"""

    settings = build_settings()
    settings_loader = Mock(return_value=settings)
    monkeypatch.setattr(app_module, "get_settings", settings_loader)
    with gr.Blocks() as ui:
        gr.Markdown("就绪检查测试")
    app = create_app(gradio_app=ui)
    settings_loader.reset_mock()
    engine = Mock()
    redis = Mock()
    monkeypatch.setattr(app_module, "create_database_engine", Mock(return_value=engine))
    monkeypatch.setattr(app_module, "create_redis_client", Mock(return_value=redis))
    checks: list[str] = []

    def postgres_probe(_engine):
        checks.append("postgres")
        if failed == "postgres":
            raise DatabaseNotReadyError("数据库密码=不可泄露")
        return True

    def redis_probe(_client):
        checks.append("redis")
        if failed == "redis":
            raise RedisNotReadyError("Redis密码=不可泄露")
        return True

    monkeypatch.setattr(app_module, "check_postgres_ready", postgres_probe)
    monkeypatch.setattr(app_module, "check_redis_ready", redis_probe)
    if failed == "config":
        settings_loader.side_effect = ConfigurationError("配置密钥=不可泄露")
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    assert not checks
    settings_loader.assert_not_called()
    response = client.get("/ready")
    assert response.status_code == (503 if failed else 200)
    settings_loader.assert_called_once()
    assert "不可泄露" not in response.text
    assert checks == {"config": [], "postgres": ["postgres"]}.get(
        failed, ["postgres", "redis"]
    )
    if failed:
        assert response.json()["failed_check"] == failed
        assert response.json()["detail"]
    else:
        assert response.json()["status"] == "ok"
    if "postgres" in checks:
        engine.dispose.assert_called_once()
    if "redis" in checks:
        redis.close.assert_called_once()
