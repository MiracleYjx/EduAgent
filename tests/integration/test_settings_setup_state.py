"""TCR-Phase13-M1: T088/T089 HTTP facts through the real setup boundary."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.core.bootstrap import (
    create_bootstrap_app,
    resolve_bootstrap_context,
)
from backend.app.core.environment import resolve_environment_profile
from backend.app.core.settings_security import OperatorIdentity, PrincipalKind
from backend.app.schemas.settings import SetupDatabaseData, SetupStorageData
from backend.app.services.settings import setup_state
from backend.app.services.settings.management import (
    SettingsRuntime,
    build_settings_runtime,
)
from tests.support.credential_fakes import FakeFileSecurity, FakeProtector

OPERATOR = OperatorIdentity(
    kind=PrincipalKind.LOCAL_OS, principal_id="synthetic-phase13-management"
)
SECRET_MARKERS = (
    "postgresql://",
    "postgresql+",
    "redis://",
    "password",
    "api_key",
    "secret",
    "bearer ",
)


def _environ(root: Path) -> dict[str, str]:
    return {
        "EDUAGENT_ENVIRONMENT": "development",
        "EDUAGENT_DATA_ROOT": str(root),
        "EDUAGENT_ENVIRONMENT_ID": str(uuid4()),
        "GRADIO_ANALYTICS_ENABLED": "False",
    }


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SettingsRuntime]:
    root = (tmp_path / "data").resolve()
    root.mkdir()
    environment = _environ(root)
    profile = resolve_environment_profile(process_env=environment)
    for name, value in _environ_for(profile).items():
        monkeypatch.setenv(name, value)
    instance = build_settings_runtime(
        profile,
        credential_backend="dpapi",
        protector=FakeProtector(),
        file_security=FakeFileSecurity(),
    )
    try:
        yield instance
    finally:
        instance.close()


def _environ_for(profile: Any) -> dict[str, str]:
    return _environ(profile.config_root.parent) | {
        "EDUAGENT_ENVIRONMENT_ID": str(profile.environment_id)
    }


@pytest.fixture
def app(runtime: SettingsRuntime) -> FastAPI:
    environment = _environ_for(runtime.profile)
    return create_bootstrap_app(
        context=resolve_bootstrap_context(environ=environment),
        settings_runtime=runtime,
    )


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as instance:
        yield instance


def _token(client: TestClient) -> str:
    service = client.app.state.setup_security
    code = service.issue_code(OPERATOR).code
    exchanged = client.post(
        "/api/settings/session/exchange",
        json={"one_time_code": code},
        headers={"Origin": "http://testserver"},
    )
    assert exchanged.status_code == 200, exchanged.text
    return str(exchanged.json()["management_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Setup {token}"}


def test_recommendation_is_read_only_and_non_secret(
    client: TestClient, runtime: SettingsRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://eduagent@postgres:5432/eduagent"
    )
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("STORAGE_ROOT", str(runtime.profile.storage_root))
    token = _token(client)
    before = {
        path.relative_to(runtime.config.root): path.read_bytes()
        for path in runtime.config.root.rglob("*")
        if path.is_file() and path.name != "writer.lock"
    }

    response = client.get(
        "/api/settings/onboarding/recommendation", headers=_auth(token)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["available"] is True
    assert body["auto_apply"] is True
    assert body["sources"]["business.database_locator"] == "process_env"
    assert body["sources"]["business.redis_locator"] == "process_env"
    assert body["sources"]["business.storage_root"] == "process_env"
    text = response.text.lower()
    for marker in SECRET_MARKERS:
        assert marker not in text
    after = {
        path.relative_to(runtime.config.root): path.read_bytes()
        for path in runtime.config.root.rglob("*")
        if path.is_file() and path.name != "writer.lock"
    }
    assert after == before


def test_main_path_recommendation_matches_dev_mode_and_keeps_override_strict(
    client: TestClient, runtime: SettingsRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://eduagent@postgres:5432/eduagent"
    )
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("STORAGE_ROOT", str(runtime.profile.storage_root))
    monkeypatch.setenv("DEV_MODE", "true")
    token = _token(client)

    recommendation = client.get(
        "/api/settings/onboarding/recommendation", headers=_auth(token)
    )
    assert recommendation.status_code == 200, recommendation.text
    business = recommendation.json()["business"]
    assert business["dev_mode"] is True
    assert recommendation.json()["sources"]["business.dev_mode"] == "process_env"

    headers = {**_auth(token), "Origin": "http://testserver"}
    created = client.post(
        "/api/settings/drafts",
        json={
            "base_revision": None,
            "changes": {"business": business},
            "acknowledge_override": False,
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text

    changed = {**business, "dev_mode": False}
    conflict = client.post(
        "/api/settings/drafts",
        json={
            "base_revision": None,
            "changes": {"business": changed},
            "acknowledge_override": False,
        },
        headers=headers,
    )
    assert conflict.status_code == 422, conflict.text
    assert conflict.json()["code"] == "OVERRIDE_ACKNOWLEDGEMENT_REQUIRED"


def test_data_state_is_read_only_and_distinguishes_states(
    client: TestClient, runtime: SettingsRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://eduagent@postgres:5432/eduagent"
    )
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("STORAGE_ROOT", str(runtime.profile.storage_root))
    monkeypatch.setattr(
        setup_state,
        "probe_database_data",
        lambda locator: SetupDatabaseData(status="empty", table_count=0),
    )
    monkeypatch.setattr(
        setup_state,
        "probe_storage_data",
        lambda root: SetupStorageData(status="empty", entry_count=0),
    )
    token = _token(client)
    before = {
        path.relative_to(runtime.config.root): path.read_bytes()
        for path in runtime.config.root.rglob("*")
        if path.is_file() and path.name != "writer.lock"
    }

    fresh = client.get("/api/settings/onboarding/data-state", headers=_auth(token))
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["status"] == "new"
    assert fresh.json()["can_initialize"] is True
    assert fresh.json()["database"]["status"] == "empty"
    assert fresh.json()["storage"]["status"] == "empty"

    monkeypatch.setattr(
        setup_state,
        "probe_database_data",
        lambda locator: SetupDatabaseData(
            status="existing", table_count=2, code="EXISTING_ROWS"
        ),
    )
    monkeypatch.setattr(
        setup_state,
        "probe_storage_data",
        lambda root: SetupStorageData(status="existing", entry_count=1),
    )
    existing = client.get("/api/settings/onboarding/data-state", headers=_auth(token))
    assert existing.status_code == 200, existing.text
    assert existing.json()["status"] == "existing"
    assert existing.json()["can_initialize"] is False

    monkeypatch.setattr(
        setup_state,
        "probe_database_data",
        lambda locator: SetupDatabaseData(
            status="unavailable", code="DATABASE_UNREACHABLE"
        ),
    )
    unknown = client.get("/api/settings/onboarding/data-state", headers=_auth(token))
    assert unknown.status_code == 200, unknown.text
    assert unknown.json()["status"] == "unknown"
    assert unknown.json()["can_initialize"] is False

    after = {
        path.relative_to(runtime.config.root): path.read_bytes()
        for path in runtime.config.root.rglob("*")
        if path.is_file() and path.name != "writer.lock"
    }
    assert after == before


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer synthetic-business-token"},
        {"Authorization": "Setup synthetic-unknown-token"},
    ],
)
def test_setup_reads_and_writes_still_require_authorization(
    client: TestClient, headers: dict[str, str]
) -> None:
    for path in (
        "/api/settings/onboarding/recommendation",
        "/api/settings/onboarding/data-state",
    ):
        assert client.get(path, headers=headers).status_code in {401, 403}

    response = client.post(
        "/api/settings/drafts",
        json={"base_revision": None, "changes": {}, "acknowledge_override": False},
        headers=headers,
    )
    assert response.status_code in {401, 403}
