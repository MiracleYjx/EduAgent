from fastapi.testclient import TestClient

from backend.app.core.app import create_app
from backend.app.core.config import AppSettings
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
