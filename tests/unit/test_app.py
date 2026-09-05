from fastapi.testclient import TestClient
from pydantic import SecretStr

from backend.app.core.app import create_app
from backend.app.core.config import AppSettings


def build_settings() -> AppSettings:
    return AppSettings(
        database_url="postgresql+psycopg://user:password@localhost:5432/eduagent",
        redis_url="redis://localhost:6379/0",
        llm_provider="deepseek",
        deepseek_api_key=SecretStr("test-key"),
        deepseek_base_url="https://api.deepseek.com",
        deepseek_model="deepseek-chat",
        embedding_provider="local",
        rerank_provider="none",
        confidence_threshold=0.8,
    )


def test_health_endpoint_and_gradio_mount() -> None:
    app = create_app(settings=build_settings())

    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "backend"}
    assert any(route.path.startswith("/gradio") for route in app.routes)
