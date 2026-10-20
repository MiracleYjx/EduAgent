"""T222：设置 UI 静态入口只回退已确认的 SPA 路由。"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.core.bootstrap import BootstrapContext, create_bootstrap_app
from backend.app.schemas.settings import (
    BootstrapOptions,
    BootstrapSettings,
    EnvironmentName,
    RuntimeKind,
)

MARKER = "T222_SETTINGS_SHELL"


def _client(tmp_path: Path) -> TestClient:
    static_root = tmp_path / "ui"
    static_root.mkdir()
    (static_root / "index.html").write_text(
        f'<!doctype html><html><body><div id="root">{MARKER}</div></body></html>',
        encoding="utf-8",
    )
    config_root = tmp_path / "environment" / "config"
    config_root.mkdir(parents=True)
    settings = BootstrapSettings(
        environment=EnvironmentName.DEVELOPMENT,
        environment_id=uuid4(),
        runtime_kind=RuntimeKind.PYTHON,
        config_root=config_root,
        static_root=static_root,
        options=BootstrapOptions(),
    )
    context = BootstrapContext(
        settings=settings,
        bind=None,
        static_root=static_root,
    )
    return TestClient(create_bootstrap_app(context=context))


@pytest.mark.parametrize(
    "path",
    ["/setup", "/setup/next", "/settings", "/diagnostics", "/business"],
)
def test_settings_spa_routes_serve_the_ui_entry(tmp_path: Path, path: str) -> None:
    response = _client(tmp_path).get(path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert MARKER in response.text


def test_settings_api_is_not_replaced_by_spa_html(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/api/settings/")

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["code"] == "SESSION_REQUIRED"
    assert MARKER not in response.text


def test_system_api_and_unknown_paths_are_not_swallowed(tmp_path: Path) -> None:
    client = _client(tmp_path)

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    ready = client.get("/ready")
    assert ready.status_code == 503
    assert ready.headers["content-type"].startswith("application/json")
    assert MARKER not in ready.text

    for path in ("/unknown", "/assets/missing.js", "/gradio"):
        response = client.get(path)
        assert response.status_code == 404
        assert MARKER not in response.text
