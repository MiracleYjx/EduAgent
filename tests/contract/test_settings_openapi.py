"""TCR-S4-UI-CONTRACT-028: generated UI contract, without runtime resources."""
from uuid import UUID

from fastapi.testclient import TestClient

from backend.app.core.bootstrap import BootstrapContext, create_bootstrap_app
from backend.app.schemas.settings import BootstrapSettings, EnvironmentName, RuntimeKind


def test_settings_openapi_declares_consumed_dtos_and_write_only_secrets(tmp_path):
    app = create_bootstrap_app(context=BootstrapContext(
        settings=BootstrapSettings(environment=EnvironmentName.TEST,
            environment_id=UUID("11111111-1111-4111-8111-111111111111"),
            runtime_kind=RuntimeKind.PYTHON, config_root=tmp_path / "config"),
        bind=None, static_root=None,
    ))
    schema = TestClient(app).get("/openapi.json").json()
    models = schema["components"]["schemas"]
    for path, method, name in [
        ("/api/settings/providers", "get", "ProviderCatalogResponse"),
        ("/api/settings/session/exchange", "post", "SessionExchangeResponse"),
        ("/api/settings/credentials", "post", "CredentialStatusView"),
        ("/api/settings/credentials/{credential_ref}/versions", "post", "CredentialStatusView"),
        ("/setup-ready", "get", "SetupReadyResponse"),
        ("/health", "get", "PublicHealthResponse"),
    ]:
        assert schema["paths"][path][method]["responses"]["201" if "credentials" in path else "200"]["content"]["application/json"]["schema"]["$ref"] == f"#/components/schemas/{name}"
    assert set(models["ProviderCatalogEntry"]["required"]) == {
        "kind", "adapter_id", "capabilities", "lifecycle", "canonical_id", "parameters"
    }
    for status in ("401", "403", "409", "422", "503"):
        assert schema["paths"]["/api/settings/providers"]["get"]["responses"][status]["content"]["application/json"]["schema"]["$ref"].endswith("/ApiErrorEnvelope")
    assert models["CredentialCreateRequest"]["properties"]["secret_value"]["writeOnly"] is True
    assert models["SessionExchangeRequest"]["properties"]["one_time_code"]["writeOnly"] is True
    assert not (tmp_path / "config").exists()
