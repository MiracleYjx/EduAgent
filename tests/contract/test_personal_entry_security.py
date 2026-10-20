"""T015: personal source boundary and independent management authorization."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.core.bootstrap import BootstrapContext, create_bootstrap_app
from backend.app.core.settings_security import OperatorIdentity, PrincipalKind
from backend.app.schemas.settings import (
    BootstrapOptions,
    BootstrapSettings,
    EnvironmentName,
    RuntimeKind,
)


@pytest.fixture
def entry(tmp_path):
    from backend.app.api.subjects import get_subject_service

    app = create_bootstrap_app(
        context=BootstrapContext(
            settings=BootstrapSettings(
                environment=EnvironmentName.TEST,
                environment_id=uuid4(),
                runtime_kind=RuntimeKind.PYTHON,
                config_root=tmp_path / "config",
            ),
            bind=BootstrapOptions(bind_host="127.0.0.1", port=8000),
            static_root=None,
        )
    )
    rows = {}

    def create(body):
        item = {"id": str(uuid4()), **body.model_dump()}
        rows[item["id"]] = item
        return item

    app.dependency_overrides[get_subject_service] = lambda: SimpleNamespace(
        list=lambda: {"subjects": list(rows.values())},
        create=create,
    )
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        yield app, client


def test_personal_subjects_are_shared_without_business_or_setup_login(entry):
    app, client = entry
    created = client.post(
        "/api/learning/subjects",
        json={"name": "数学"},
        headers={"Origin": str(client.base_url).rstrip("/")},
    )
    assert created.status_code == 201, created.text
    with TestClient(app, base_url=str(client.base_url)) as other:
        assert other.get("/api/learning/subjects").json()["subjects"] == [
            created.json()
        ]
    assert client.get("/api/settings/").status_code == 401
    assert client.get("/api/courses").status_code == 503


@pytest.mark.parametrize(
    "headers,code",
    [
        ({"Host": "evil.invalid"}, "HOST_NOT_ALLOWED"),
        ({"Origin": "http://evil.invalid"}, "ORIGIN_NOT_ALLOWED"),
        ({"Origin": "null"}, "ORIGIN_INVALID"),
    ],
)
def test_wrong_source_rejected_before_subject_access(entry, headers, code):
    response = entry[1].get("/api/learning/subjects", headers=headers)
    assert response.status_code == 403
    assert response.json()["code"] == code


def test_writes_require_exact_origin_json_and_reject_client_identity(entry):
    client = entry[1]
    path = "/api/learning/subjects"
    assert client.post(path, json={"name": "数学"}).status_code == 403
    headers = {"Origin": "http://127.0.0.1:8000"}
    assert client.post(path, content="name=math", headers=headers).status_code == 415
    for extra in (
        {"owner_id": str(uuid4())},
        {"role": "admin"},
        {"subject_id": str(uuid4())},
    ):
        response = client.post(path, json={"name": "数学", **extra}, headers=headers)
        assert response.status_code == 422 and "admin" not in response.text
    for name in ("", "   ", "x" * 161):
        assert (
            client.post(path, json={"name": name}, headers=headers).status_code == 422
        )
    assert (
        client.post(
            path,
            content="x" * 65537,
            headers={**headers, "Content-Type": "application/json"},
        ).status_code
        == 413
    )


def test_setup_authorization_stays_independent_and_origin_bound(entry):
    app, client = entry
    issued = app.state.setup_security.issue_code(
        OperatorIdentity(
            kind=PrincipalKind.LOCAL_OS, principal_id="synthetic-entry-operator"
        )
    )
    exchange = client.post(
        "/api/settings/session/exchange",
        json={"one_time_code": issued.code},
        headers={"Origin": "http://127.0.0.1:8000"},
    )
    assert exchange.status_code == 200
    token = exchange.json()["management_token"]
    assert issued.code not in exchange.text
    assert (
        client.get(
            "/api/settings/", headers={"Authorization": "Setup " + token}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/settings/drafts",
            json={},
            headers={
                "Authorization": "Setup " + token,
                "Origin": "http://evil.invalid",
            },
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/settings/drafts", json={}, headers={"Origin": "http://127.0.0.1:8000"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/settings/session/exchange",
            json={"one_time_code": issued.code},
            headers={"Origin": "http://127.0.0.1:8000"},
        ).status_code
        == 401
    )


def test_unconfigured_personal_entry_is_not_a_successful_empty_space(tmp_path):
    app = create_bootstrap_app(
        context=BootstrapContext(
            settings=BootstrapSettings(
                environment=EnvironmentName.TEST,
                environment_id=uuid4(),
                runtime_kind=RuntimeKind.PYTHON,
                config_root=tmp_path / "config",
            ),
            bind=BootstrapOptions(bind_host="127.0.0.1", port=8000),
            static_root=None,
        )
    )
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.get("/api/learning/subjects")
        assert response.status_code == 503
        assert response.json()["code"] == "LEARNING_CONFIGURATION_UNAVAILABLE"
        assert "subjects" not in response.json()


def test_docker_personal_source_uses_explicit_published_port(tmp_path, monkeypatch):
    from backend.app.core.bootstrap import build_bootstrap_security

    context = BootstrapContext(
        settings=BootstrapSettings(
            environment=EnvironmentName.TEST,
            environment_id=uuid4(),
            runtime_kind=RuntimeKind.DOCKER,
            config_root=tmp_path / "config",
        ),
        bind=BootstrapOptions(bind_host="0.0.0.0"),
        static_root=None,
    )
    monkeypatch.delenv("EDUAGENT_PUBLIC_PORT", raising=False)
    assert build_bootstrap_security(context).policy.allowed_hosts == frozenset()
    monkeypatch.setenv("EDUAGENT_PUBLIC_PORT", "48123")
    assert build_bootstrap_security(context).policy.allowed_hosts == frozenset(
        {"127.0.0.1:48123", "localhost:48123"}
    )
