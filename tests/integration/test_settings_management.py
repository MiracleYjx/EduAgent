"""TCR-S3-SETTINGS-API-ASSEMBLY-022（T220）：设置管理 HTTP 与装配协调。

真实 FastAPI/TestClient + 真实 ConfigService/CredentialStore/ProbeService：

- 所有管理端点仍经 T217 中间件（令牌 + Host + Origin + JSON）；
- 草稿、校验、凭据、绑定、检测、激活、向导、初始化、操作、导出走真实 HTTP；
- 凭据后端使用合成 protector/file-security，不写真实 DPAPI、不读真实凭据；
- 依赖与迁移执行器以显式构造参数注入；探针只访问 `127.0.0.1:1` 的不可达端口；
- 不启动真实业务 lifespan、不连 Development/Demo、不调用付费模型。
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from backend.app.ai.errors import AIConfigurationError
from backend.app.ai.llm.factory import create_llm_provider
from backend.app.core.bootstrap import (
    create_application,
    create_bootstrap_app,
    resolve_bootstrap_context,
)
from backend.app.core.config import reset_settings_cache
from backend.app.core.environment import resolve_environment_profile
from backend.app.core.settings_security import OperatorIdentity, PrincipalKind
from backend.app.schemas.settings import ConfigurationValues, CredentialReference
from backend.app.services.settings import readiness
from backend.app.services.settings.database_initialization import (
    InitializationError,
    run_alembic_upgrade,
)
from backend.app.services.settings.management import (
    SettingsRuntime,
    build_settings_runtime,
    mounted_mounts_from_env,
)
from tests.support.credential_fakes import (
    FakeFileSecurity,
    FakeProtector,
    baseline_values,
)
from tests.support.provider_probe_fakes import HttpClientFactory, HttpScenario

OPERATOR = OperatorIdentity(
    kind=PrincipalKind.LOCAL_OS, principal_id="synthetic-t220-management"
)
SECRET_MARKERS = (
    "postgresql://",
    "postgresql+",
    "redis://",
    "password",
    "api_key",
    "bearer ",
    "synthetic-t220-secret",
)


def _environ(root: Path) -> dict[str, str]:
    return {
        "EDUAGENT_ENVIRONMENT": "development",
        "EDUAGENT_DATA_ROOT": str(root),
        "EDUAGENT_ENVIRONMENT_ID": str(uuid4()),
        "GRADIO_ANALYTICS_ENABLED": "False",
    }


def _environ_for(profile: Any) -> dict[str, str]:
    """从已有 profile 反推同一身份，避免 app 与 runtime 绑定不同环境。"""

    return _environ(profile.config_root.parent) | {
        "EDUAGENT_ENVIRONMENT_ID": str(profile.environment_id)
    }


@pytest.fixture(autouse=True)
def isolated_process_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    keys = {
        "DATABASE_URL", "REDIS_URL", "JWT_SECRET_KEY", "LLM_PROVIDER",
        "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL",
        "EMBEDDING_PROVIDER", "EMBEDDING_MODEL", "EMBEDDING_API_KEY",
        "RERANK_PROVIDER", "STORAGE_ROOT", "CONFIDENCE_THRESHOLD",
    }
    for name in tuple(os.environ):
        if name in keys or name.startswith("EDUAGENT__"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def profile(tmp_path: Path, monkeypatch):
    root = (tmp_path / "data").resolve()
    root.mkdir()
    selected = resolve_environment_profile(process_env=_environ(root))
    for key, value in _environ_for(selected).items():
        monkeypatch.setenv(key, value)
    return selected


@pytest.fixture
def runtime(profile: Any) -> Iterator[SettingsRuntime]:
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


@pytest.fixture
def app(profile: Any, runtime: SettingsRuntime) -> FastAPI:
    context = resolve_bootstrap_context(environ=_environ_for(profile))
    return create_bootstrap_app(context=context, settings_runtime=runtime)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    # Probe 创建早返后，客户端必须持有同一 ASGI event loop 直至任务结束。
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


@pytest.fixture
def token(client: TestClient) -> str:
    return _token(client)


def _auth(token: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Setup {token}", **extra}


def _write_headers(token: str, **extra: str) -> dict[str, str]:
    return _auth(token, Origin="http://testserver", **extra)


def _business_changes(profile: Any, reference: dict[str, Any], *, host: str = "127.0.0.1") -> dict[str, Any]:
    return {
        "business": {
            "database_locator": {
                "host": host,
                "port": 1,
                "database": "synthetic_t220",
                "user": "synthetic_runner",
            },
            "redis_locator": {"host": "127.0.0.1", "port": 1},
            "storage_root": str(profile.storage_root),
            "jwt_secret_ref": reference,
        }
    }


def _credential(
    client: TestClient,
    token: str,
    purpose: str = "jwt",
    secret_value: str = "synthetic-t220-jwt-secret-with-at-least-32-chars",
) -> dict[str, Any]:
    created = client.post(
        "/api/settings/credentials",
        json={"purpose": purpose, "secret_value": secret_value},
        headers=_write_headers(token),
    )
    assert created.status_code == 201, created.text
    body = created.json()
    return {
        "credential_id": body["credential_ref"],
        "version": body["version"],
        "environment_id": body["environment_id"],
        "backend": body["backend"],
        "purpose": body["purpose"],
    }


def _create_draft(client: TestClient, token: str, *, base: str | None, changes: dict[str, Any]):
    return client.post(
        "/api/settings/drafts",
        json={"base_revision": base, "changes": changes},
        headers=_write_headers(token),
    )


def _activate(client: TestClient, token: str, draft_id: str, *, active: str | None, probes: list[str] | None = None):
    return client.post(
        f"/api/settings/drafts/{draft_id}/activate",
        json={
            "expected_active_revision": active,
            "probe_ids": probes or [],
            "acknowledge_override": True,
        },
        headers=_write_headers(token),
    )


def _activate_business(client: TestClient, token: str, profile: Any) -> str:
    reference = _credential(client, token)
    draft = _create_draft(client, token, base=None, changes=_business_changes(profile, reference))
    assert draft.status_code == 201, draft.text
    draft_id = draft.json()["revision_id"]
    validated = client.post(
        f"/api/settings/drafts/{draft_id}/validate",
        json={"expected_revision": None},
        headers=_write_headers(token),
    )
    assert validated.status_code == 200 and validated.json()["valid"] is True, validated.text
    activated = _activate(client, token, draft_id, active=None)
    assert activated.status_code == 200, activated.text
    return str(activated.json()["revision_id"])


def _ready_probes() -> readiness.DependencyProbes:
    ok = readiness.ProbeOutcome("ok", code=None)

    def database(_settings: Any, _head: str | None) -> readiness.DatabaseProbeResult:
        return readiness.DatabaseProbeResult(
            postgres=ok, vector=ok, migration=ok, schema=ok
        )

    return readiness.DependencyProbes(
        database=database,
        redis=lambda _settings: ok,
        storage=lambda _settings: ok,
    )


def _wait_operation(client: TestClient, token: str, operation_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(
            f"/api/settings/operations/{operation_id}", headers=_auth(token)
        )
        assert response.status_code == 200, response.text
        body = response.json()
        if body["status"] in {"succeeded", "failed", "interrupted"}:
            return body
        time.sleep(0.05)
    raise AssertionError("operation did not finish in time")


# ---------------------------------------------------------------------------
# 授权矩阵
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/settings/providers"),
        ("get", "/api/settings/onboarding"),
        ("get", "/api/settings/index-identities"),
        ("get", f"/api/settings/index-identities/{uuid4()}"),
        ("post", "/api/settings/drafts"),
        ("post", "/api/settings/credentials"),
        ("post", "/api/settings/probes"),
        ("post", "/api/settings/initialization/preview"),
        ("post", "/api/settings/restart"),
        ("delete", f"/api/settings/credentials/{uuid4()}/versions/{uuid4()}"),
    ],
)
def test_management_endpoints_require_setup_token(
    client: TestClient, method: str, path: str
) -> None:
    response = client.request(method.upper(), path, json={})
    assert response.status_code == 401, path
    assert response.headers["www-authenticate"].startswith("Setup")


def test_business_bearer_cannot_use_management_endpoints(
    client: TestClient, token: str
) -> None:
    for path in ("/api/settings/providers", "/api/settings/onboarding"):
        response = client.get(path, headers={"Authorization": "Bearer synthetic-business"})
        assert response.status_code == 401, path

    response = client.post(
        "/api/settings/drafts",
        json={"base_revision": None, "changes": {}},
        headers={"Authorization": "Bearer synthetic-business", "Origin": "http://testserver"},
    )
    assert response.status_code == 401


def test_management_write_requires_origin(client: TestClient, token: str) -> None:
    response = client.post(
        "/api/settings/drafts",
        json={"base_revision": None, "changes": {}},
        headers=_auth(token),
    )
    assert response.status_code == 403
    assert response.json()["code"] == "ORIGIN_REQUIRED"


# ---------------------------------------------------------------------------
# 草稿 / 校验 / 激活
# ---------------------------------------------------------------------------


def test_draft_save_conflict_patch_and_validation(client: TestClient, token: str, profile: Any) -> None:
    reference = _credential(client, token)
    first = _create_draft(client, token, base=None, changes=_business_changes(profile, reference))
    assert first.status_code == 201, first.text
    draft = first.json()
    assert draft["parent_revision"] is None
    assert draft["values"]["business"]["storage_root"] == str(profile.storage_root)

    conflict = _create_draft(client, token, base=str(uuid4()), changes={"business": {}})
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "REVISION_CONFLICT"

    patched = client.patch(
        f"/api/settings/drafts/{draft['revision_id']}",
        json={
            "expected_revision": None,
            "changes": {"business": {"jwt_expire_minutes": 90}},
            "acknowledge_override": True,
        },
        headers=_write_headers(token),
    )
    assert patched.status_code == 200, patched.text
    patched_body = patched.json()
    assert patched_body["revision_id"] != draft["revision_id"]
    assert patched_body["values"]["business"]["jwt_expire_minutes"] == 90
    # 未给出的字段保持不变。
    assert patched_body["values"]["business"]["jwt_secret_ref"]["credential_id"] == reference["credential_id"]

    validated = client.post(
        f"/api/settings/drafts/{patched_body['revision_id']}/validate",
        json={"expected_revision": None},
        headers=_write_headers(token),
    )
    assert validated.status_code == 200, validated.text
    assert validated.json()["valid"] is True

    with_unknown = _create_draft(
        client, token, base=None, changes={"business": {"unexpected_field": 1}}
    )
    assert with_unknown.status_code == 422
    assert with_unknown.json()["code"] == "INVALID_CONFIGURATION"

    with_secret = _create_draft(
        client, token, base=None, changes={"business": {"database_password": "synthetic-t220-secret"}}
    )
    assert with_secret.status_code == 422
    assert "synthetic-t220-secret" not in with_secret.text


def test_credential_is_write_only_and_status_is_projected(
    client: TestClient, token: str
) -> None:
    created = client.post(
        "/api/settings/credentials",
        json={"purpose": "jwt", "secret_value": "synthetic-t220-secret"},
        headers=_write_headers(token),
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "available"
    assert body["backend"] == "dpapi"
    assert UUID(body["environment_id"]).int != 0
    assert "synthetic-t220-secret" not in created.text

    view = client.get("/api/settings/", headers=_auth(token))
    assert view.status_code == 200
    statuses = view.json()["credential_statuses"]
    assert statuses == []  # 尚未被任何修订引用

    retrieved = client.get(
        f"/api/settings/index-identities/{uuid4()}", headers=_auth(token)
    )
    assert retrieved.status_code == 503
    assert retrieved.json()["code"] == "BUSINESS_CONFIG_NOT_CONFIGURED"


def test_credential_delete_rejects_referenced_version_and_allows_candidate(
    client: TestClient, token: str, profile: Any
) -> None:
    reference = _credential(client, token)
    _activate_business_with(client, token, profile, reference)

    # 敏感操作统一要求 JSON 合同；DELETE 也发送空对象而不是无正文。
    in_use = client.request(
        "DELETE",
        f"/api/settings/credentials/{reference['credential_id']}/versions/{reference['version']}",
        json={},
        headers=_write_headers(token),
    )
    assert in_use.status_code == 409
    assert in_use.json()["code"] == "IN_USE"

    candidate = client.post(
        f"/api/settings/credentials/{reference['credential_id']}/versions",
        json={"secret_value": "synthetic-t220-secret"},
        headers=_write_headers(token),
    )
    assert candidate.status_code == 201, candidate.text
    # 未接可用的运行事实源时，无法证明候选版本没有被非终态快照引用：闭锁删除。
    unproven = client.request(
        "DELETE",
        f"/api/settings/credentials/{reference['credential_id']}/versions/{candidate.json()['version']}",
        json={},
        headers=_write_headers(token),
    )
    assert unproven.status_code == 409
    assert unproven.json()["code"] == "REFERENCE_INDEX_UNAVAILABLE"


def _activate_business_with(
    client: TestClient, token: str, profile: Any, reference: dict[str, Any]
) -> str:
    draft = _create_draft(client, token, base=None, changes=_business_changes(profile, reference))
    assert draft.status_code == 201, draft.text
    draft_id = draft.json()["revision_id"]
    assert (
        client.post(
            f"/api/settings/drafts/{draft_id}/validate",
            json={"expected_revision": None},
            headers=_write_headers(token),
        ).status_code
        == 200
    )
    activated = _activate(client, token, draft_id, active=None)
    assert activated.status_code == 200, activated.text
    return str(activated.json()["revision_id"])


def test_activation_missing_credential_keeps_original_active(
    client: TestClient, token: str, profile: Any
) -> None:
    active = _activate_business(client, token, profile)
    fabricated = {
        "credential_id": str(uuid4()),
        "version": str(uuid4()),
        "environment_id": str(profile.environment_id),
        "backend": "dpapi",
        "purpose": "jwt",
    }
    draft = _create_draft(
        client, token, base=active, changes=_business_changes(profile, fabricated)
    )
    assert draft.status_code == 201, draft.text
    draft_id = draft.json()["revision_id"]
    assert (
        client.post(
            f"/api/settings/drafts/{draft_id}/validate",
            json={"expected_revision": active},
            headers=_write_headers(token),
        ).status_code
        == 200
    )
    failed = _activate(client, token, draft_id, active=active)
    assert failed.status_code == 422
    assert failed.json()["code"] == "CREDENTIAL_UNAVAILABLE"

    view = client.get("/api/settings/", headers=_auth(token)).json()
    assert view["active_revision"] == active
    assert view["pending_revision"] is None


def test_weak_jwt_secret_is_rejected_before_activation(
    client: TestClient, token: str, profile: Any
) -> None:
    weak = _credential(client, token, secret_value="synthetic-short-jwt-secret")
    draft = _create_draft(client, token, base=None, changes=_business_changes(profile, weak))
    assert draft.status_code == 201, draft.text
    draft_id = draft.json()["revision_id"]
    validated = client.post(
        f"/api/settings/drafts/{draft_id}/validate",
        json={"expected_revision": None},
        headers=_write_headers(token),
    )
    assert validated.status_code == 200 and validated.json()["valid"] is True

    failed = _activate(client, token, draft_id, active=None)
    assert failed.status_code == 422, failed.text
    assert failed.json()["code"] == "JWT_SECRET_TOO_WEAK"
    assert "synthetic-short-jwt-secret" not in failed.text

    view = client.get("/api/settings/", headers=_auth(token)).json()
    assert view["active_revision"] is None
    assert view["pending_revision"] is None


def test_business_change_requires_restart_and_keeps_previous_active(
    client: TestClient, token: str, profile: Any
) -> None:
    active = _activate_business(client, token, profile)
    # 复用同一凭据版本，只改变数据库主机。
    view = client.get("/api/settings/", headers=_auth(token)).json()
    reference = view["values"]["business"]["jwt_secret_ref"]
    draft = _create_draft(
        client, token, base=active, changes=_business_changes(profile, reference, host="127.0.0.2")
    )
    assert draft.status_code == 201, draft.text
    draft_id = draft.json()["revision_id"]
    validated = client.post(
        f"/api/settings/drafts/{draft_id}/validate",
        json={"expected_revision": active},
        headers=_write_headers(token),
    )
    assert validated.status_code == 200, validated.text
    activated = _activate(client, token, draft_id, active=active)
    assert activated.status_code == 202, activated.text
    assert activated.json()["status"] == "pending_restart"

    view = client.get("/api/settings/", headers=_auth(token)).json()
    assert view["active_revision"] == active
    assert view["pending_revision"] == activated.json()["revision_id"]
    assert view["restart_required"] is True

    report = client.get("/api/settings/readiness", headers=_auth(token)).json()
    assert report["business_prerequisites"]["configuration_revision"] == active
    assert report["pending_revision"] == activated.json()["revision_id"]
    assert report["restart_required"] is True


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------


def _probe_draft(
    client: TestClient, token: str, profile: Any, reference: dict[str, Any]
) -> tuple[str, str]:
    profile_id = uuid4()
    profile_revision = uuid4()
    changes = _business_changes(profile, reference)
    changes["providers"] = {
        str(profile_id): {
            "profile_id": str(profile_id),
            "profile_revision": str(profile_revision),
            "display_name": "Synthetic local endpoint",
            "adapter_id": "openai_compatible",
            "model_id": "synthetic-model",
            "base_url": "http://127.0.0.1:1/v1",
            "auth_mode": "none",
            "declared_capabilities": ["text", "json_object"],
            "parameters": {},
            "enabled": True,
        }
    }
    changes["bindings"] = {
        "default_llm": {
            "task_kind": "default_llm",
            "profile_id": str(profile_id),
            "profile_revision": str(profile_revision),
            "required_capabilities": ["json_object"],
        }
    }
    draft = _create_draft(client, token, base=None, changes=changes)
    assert draft.status_code == 201, draft.text
    client.app.state.settings_runtime.probes()._http_factory = HttpClientFactory(
        HttpScenario(error=httpx.ConnectError("synthetic unreachable endpoint"))
    )
    return draft.json()["revision_id"], str(profile_id)


def _wait_probe(client: TestClient, token: str, probe_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/settings/probes/{probe_id}", headers=_auth(token))
        assert response.status_code == 200, response.text
        result = response.json()
        if result["status"] not in {"queued", "running"}:
            return result
        time.sleep(0.01)
    raise AssertionError("probe did not finish in time")


def test_probe_requires_billable_confirmation_and_reports_safe_failure(
    client: TestClient, token: str, profile: Any
) -> None:
    reference = _credential(client, token)
    draft_id, profile_id = _probe_draft(client, token, profile, reference)

    rejected = client.post(
        "/api/settings/probes",
        json={
            "draft_revision": draft_id,
            "profile_id": profile_id,
            "capability": "json_object",
            "allow_billable": False,
            "timeout_seconds": 5,
        },
        headers=_write_headers(token),
    )
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "BILLABLE_CONFIRMATION_REQUIRED"

    probed = client.post(
        "/api/settings/probes",
        json={
            "draft_revision": draft_id,
            "profile_id": profile_id,
            "capability": "json_object",
            "allow_billable": True,
            "timeout_seconds": 5,
        },
        headers=_write_headers(token),
    )
    assert probed.status_code == 202, probed.text
    body = _wait_probe(client, token, probed.json()["probe_id"])
    assert body["status"] in {"failed", "cancelled"}
    assert body["result_code"] in {
        "SERVICE_UNREACHABLE",
        "UNKNOWN_PROVIDER_ERROR",
        "TIMEOUT",
    }
    assert "synthetic-t220-secret" not in probed.text

    fetched = client.get(f"/api/settings/probes/{body['probe_id']}", headers=_auth(token))
    assert fetched.status_code == 200
    assert fetched.json()["result_code"] == body["result_code"]


def test_activation_with_failed_probe_keeps_no_active(
    client: TestClient, token: str, profile: Any
) -> None:
    reference = _credential(client, token)
    draft_id, profile_id = _probe_draft(client, token, profile, reference)
    probed = client.post(
        "/api/settings/probes",
        json={
            "draft_revision": draft_id,
            "profile_id": profile_id,
            "capability": "json_object",
            "allow_billable": True,
            "timeout_seconds": 5,
        },
        headers=_write_headers(token),
    )
    assert probed.status_code == 202
    _wait_probe(client, token, probed.json()["probe_id"])
    assert (
        client.post(
            f"/api/settings/drafts/{draft_id}/validate",
            json={"expected_revision": None},
            headers=_write_headers(token),
        ).status_code
        == 200
    )
    failed = _activate(
        client, token, draft_id, active=None, probes=[probed.json()["probe_id"]]
    )
    assert failed.status_code in {409, 422}, failed.text
    assert failed.json()["code"] in {"PROBE_REQUIRED", "PROBE_EXPIRED"}
    view = client.get("/api/settings/", headers=_auth(token)).json()
    assert view["active_revision"] is None


# ---------------------------------------------------------------------------
# 向导 / 初始化 / 重启 / 导出
# ---------------------------------------------------------------------------


def test_onboarding_revision_conflict(client: TestClient, token: str) -> None:
    initial = client.get("/api/settings/onboarding", headers=_auth(token))
    assert initial.status_code == 200
    assert initial.json()["progress"]["revision"] == 0
    # T220 已接真实计算：无 active 配置是“已评估且未配置”，不是伪造可用。
    assert initial.json()["readiness"]["ai"]["evaluated"] is True
    assert initial.json()["readiness"]["ai"]["status"] == "not_configured"
    assert initial.json()["readiness"]["ai"]["tasks"] == {}

    updated = client.patch(
        "/api/settings/onboarding",
        json={"expected_revision": 0, "current_step": 2, "draft_revision": None},
        headers=_write_headers(token),
    )
    assert updated.status_code == 200
    assert updated.json()["revision"] == 1
    assert updated.json()["completed_steps"] == [0, 1]

    conflict = client.patch(
        "/api/settings/onboarding",
        json={"expected_revision": 0, "current_step": 3, "draft_revision": None},
        headers=_write_headers(token),
    )
    assert conflict.status_code == 409


def test_initialization_preview_and_apply_with_injected_runner(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "init").resolve()
    root.mkdir()
    environment = _environ(root)
    profile = resolve_environment_profile(process_env=environment)
    environment = _environ_for(profile)
    target = readiness.discover_migration_head()
    assert target is not None
    state = {"head": "previous-head"}
    calls: list[str] = []

    def migrator(url: str) -> None:
        calls.append(url)
        state["head"] = target

    def read_head(_url: str) -> str | None:
        return state["head"]

    runtime = build_settings_runtime(
        profile,
        credential_backend="dpapi",
        protector=FakeProtector(),
        file_security=FakeFileSecurity(),
        migrator=migrator,
        read_head=read_head,
        initialization_verify=lambda _url, _expected: None,
    )
    context = resolve_bootstrap_context(environ=environment)
    client = TestClient(create_bootstrap_app(context=context, settings_runtime=runtime))
    try:
        token = _token(client)
        reference = _credential(client, token)
        draft = _create_draft(
            client, token, base=None, changes=_business_changes(profile, reference)
        )
        draft_id = draft.json()["revision_id"]
        preview = client.post(
            "/api/settings/initialization/preview",
            json={"draft_revision": draft_id},
            headers=_write_headers(token),
        )
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["target_head"] == target
        assert body["current_head"] == "previous-head"
        assert body["allowed"] is True

        applied = client.post(
            "/api/settings/initialization/apply",
            json={"confirmation_id": body["preview_id"], "expected_revision": draft_id},
            headers=_write_headers(token),
        )
        assert applied.status_code == 202, applied.text
        operation = _wait_operation(client, token, applied.json()["operation_id"])
        assert operation["status"] == "succeeded"
        assert calls and calls[0].startswith("postgresql+psycopg://")
        assert "synthetic-t220-secret" not in applied.text
        assert root.as_posix() not in applied.text
    finally:
        client.close()
        runtime.close()


def test_initialization_apply_rejects_changed_target(
    client: TestClient, token: str, profile: Any, monkeypatch: pytest.MonkeyPatch,
    runtime: SettingsRuntime,
) -> None:
    monkeypatch.setattr(runtime, "_read_head", lambda _url: None)
    reference = _credential(client, token)
    draft = _create_draft(client, token, base=None, changes=_business_changes(profile, reference))
    draft_id = draft.json()["revision_id"]
    preview = client.post(
        "/api/settings/initialization/preview",
        json={"draft_revision": draft_id},
        headers=_write_headers(token),
    )
    assert preview.status_code == 200, preview.text
    monkeypatch.setattr(readiness, "discover_migration_head", lambda: "changed-target-head")
    applied = client.post(
        "/api/settings/initialization/apply",
        json={"confirmation_id": preview.json()["preview_id"], "expected_revision": draft_id},
        headers=_write_headers(token),
    )
    assert applied.status_code == 409
    assert applied.json()["code"] == "REVISION_CONFLICT"


def test_restart_is_manual_and_guards_revision(
    client: TestClient, token: str, profile: Any
) -> None:
    active = _activate_business(client, token, profile)
    wrong = client.post(
        "/api/settings/restart",
        json={"expected_revision": str(uuid4())},
        headers=_write_headers(token),
    )
    assert wrong.status_code == 409

    requested = client.post(
        "/api/settings/restart",
        json={"expected_revision": active},
        headers=_write_headers(token),
    )
    assert requested.status_code == 200, requested.text
    assert requested.json()["status"] == "manual_restart_required"
    assert "请按运行方式重启" in requested.json()["reason"]
    assert "T221" not in requested.json()["reason"]


def test_export_is_redacted(client: TestClient, token: str, profile: Any) -> None:
    _activate_business(client, token, profile)
    exported = client.post(
        "/api/settings/diagnostics/export",
        json={"include": ["configuration", "readiness", "versions"]},
        headers=_write_headers(token),
    )
    assert exported.status_code == 200, exported.text
    assert "attachment" in exported.headers["content-disposition"]
    text = exported.text.lower()
    for marker in SECRET_MARKERS:
        assert marker not in text, marker
    assert str(profile.config_root.parent).lower() not in text
    body = exported.json()
    assert body["configuration"]["active_revision"]
    assert body["configuration"]["storage_root"] == "<configured>"
    assert body["versions"]["environment"] == "development"


def test_settings_view_never_contains_secret_values(
    client: TestClient, token: str, profile: Any
) -> None:
    _activate_business(client, token, profile)
    response = client.get("/api/settings/", headers=_auth(token))
    assert response.status_code == 200
    text = response.text.lower()
    # `*_password_ref` 是字段名而不是秘密；只禁实际秘密/连接串标记。
    for marker in ("synthetic-t220-secret", "secret_value", "bearer ", "postgresql://"):
        assert marker not in text, marker
    assert response.json()["credential_statuses"][0]["status"] == "available"


# ---------------------------------------------------------------------------
# Bootstrap→业务装配与恢复顺序
# ---------------------------------------------------------------------------


def test_bootstrap_ready_reports_recorded_prerequisite_failure(
    profile: Any, runtime: SettingsRuntime
) -> None:
    context = resolve_bootstrap_context(environ=_environ(profile.config_root.parent))
    app = create_bootstrap_app(
        context=context,
        settings_runtime=runtime,
        business_failure=("postgres", "PostgreSQL 未就绪。"),
    )
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    ready = client.get("/ready")
    assert ready.status_code == 503
    assert ready.json()["failed_check"] == "postgres"
    blocked = client.get("/api/courses")
    assert blocked.status_code == 503
    assert blocked.json()["code"] == "BUSINESS_NOT_READY"


def test_production_entry_stays_in_setup_without_activated_configuration(
    profile: Any, runtime: SettingsRuntime
) -> None:
    context = resolve_bootstrap_context(environ=_environ(profile.config_root.parent))
    app = create_application(context=context, settings_runtime=runtime)
    assert app.state.business_assembly == "not_assembled"
    assert app.state.business_failure is not None
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    assert client.get("/api/settings/").status_code == 401
    ready = client.get("/ready")
    assert ready.status_code == 503
    assert ready.json()["failed_check"] == "assembly"


def test_personal_startup_and_reads_never_replay_legacy_recovery(monkeypatch):
    from backend.app.core.app import create_app
    from tests.unit.settings_helpers import build_test_settings

    def forbidden():
        raise AssertionError("personal startup must not replay retired exam operations")
    for module, function in [("grading", "recover_interrupted_grading_tasks"),
            ("workflow", "recover_diagnosis_pending_runs"),
            ("paper_import", "recover_interrupted_paper_imports")]:
        monkeypatch.setattr(f"backend.app.api.{module}.{function}", forbidden)
    settings = build_test_settings(database_url="postgresql+psycopg://synthetic:synthetic@127.0.0.1:1/test",
        redis_url="redis://127.0.0.1:1/0")
    app = create_app(settings=settings)
    with TestClient(app) as client:
        assert app.state.recovery == "completed"
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 503
        assert app.state.recovery == "completed"


def test_readiness_unknown_migration_head_blocks_ready(
    client: TestClient, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(readiness, "discover_migration_head", lambda: None)
    report = client.get("/api/settings/readiness", headers=_auth(token)).json()
    migration = report["business_prerequisites"]["checks"]["migration"]
    # 无业务配置时迁移检查为 skipped；这里只固定“不把未知当通过”的分类映射。
    assert migration["status"] in {"skipped", "failed", "unknown"}
    assert report["business"]["ready"] is False


def test_operation_record_is_interrupted_for_foreign_instance(
    runtime: SettingsRuntime, tmp_path: Path
) -> None:
    registry = runtime.operations()
    created = registry.create("restart", ("stop", "start"))
    path = runtime.config.root / "config" / "operations" / f"{created.operation_id}.json"
    payload = path.read_text(encoding="utf-8")
    path.write_text(
        payload.replace(f'"instance": "{registry._instance}"', f'"instance": "{uuid4()}"'),
        encoding="utf-8",
    )
    interrupted = registry.get(created.operation_id)
    assert interrupted.status == "interrupted"


# ---------------------------------------------------------------------------
# T220-Fix：AI Readiness / 业务-only 装配 / 初始化 / 挂载 Secret
# ---------------------------------------------------------------------------


def test_readiness_reports_per_task_ai_state(
    client: TestClient, token: str, profile: Any
) -> None:
    _activate_business(client, token, profile)

    report = client.get("/api/settings/readiness", headers=_auth(token)).json()

    ai = report["ai"]
    assert ai["evaluated"] is True
    assert ai["status"] == "evaluated"
    assert ai["reason"] == "per_task"
    assert ai["tasks"]["default_llm"]["configured"] is False
    assert ai["tasks"]["default_llm"]["ready"] is False
    assert ai["tasks"]["reviewer"]["ready"] is True
    assert ai["tasks"]["reviewer"]["reason"] == "ready_rule_based"


def test_business_only_projection_and_application_assembly(
    client: TestClient,
    token: str,
    profile: Any,
    runtime: SettingsRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = _credential(
        client,
        token,
        secret_value="synthetic-t220-jwt-secret-with-at-least-32-chars",
    )
    draft = _create_draft(
        client, token, base=None, changes=_business_changes(profile, reference)
    )
    draft_id = draft.json()["revision_id"]
    validated = client.post(
        f"/api/settings/drafts/{draft_id}/validate",
        json={"expected_revision": None},
        headers=_write_headers(token),
    )
    assert validated.status_code == 200 and validated.json()["valid"] is True
    activated = _activate(client, token, draft_id, active=None)
    assert activated.status_code == 200, activated.text

    manifest = runtime.config.read_manifest()
    assert manifest.active_revision is not None
    revision = runtime.config.get_revision(manifest.active_revision)
    projection = runtime.app_settings_for(revision.values)
    assert projection.llm_provider == "none"
    assert projection.embedding_provider == "none"
    assert projection.rerank_provider == "none"
    assert projection.deepseek_api_key.get_secret_value() == ""
    assert projection.JWT_SECRET_KEY is None
    assert revision.values.business.jwt_secret_ref == CredentialReference.model_validate(reference)

    for name, value in _environ_for(profile).items():
        monkeypatch.setenv(name, value)
    context = resolve_bootstrap_context(environ=_environ_for(profile))
    try:
        application = create_application(
            context=context, settings_runtime=runtime, probes=_ready_probes()
        )
        assert application.state.business_assembly == "assembled"
        # 不启动 lifespan：恢复钩子需要真实 DB；这里只验证路由已装配。
        business_client = TestClient(application)
        try:
            rejected = business_client.get("/api/courses")
            assert rejected.status_code == 404
            assert rejected.json().get("code") != "BUSINESS_NOT_READY"
        finally:
            business_client.close()
        with pytest.raises(AIConfigurationError) as gate:
            create_llm_provider()
        assert gate.value.code == "AI_NOT_CONFIGURED"
    finally:
        reset_settings_cache()


def test_default_initialization_executor_is_wired(runtime: SettingsRuntime) -> None:
    assert runtime.initialization()._migrator is run_alembic_upgrade


def test_initialization_verification_failure_keeps_safe_pointer(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "init-verify").resolve()
    root.mkdir()
    profile = resolve_environment_profile(process_env=_environ(root))
    environment = _environ_for(profile)

    def reject(_url: str, _expected: Any) -> None:
        raise InitializationError("TARGET_IDENTITY_MISMATCH")

    runtime = build_settings_runtime(
        profile,
        credential_backend="dpapi",
        protector=FakeProtector(),
        file_security=FakeFileSecurity(),
        migrator=lambda _url: None,
        read_head=lambda _url: "previous-head",
        initialization_verify=reject,
    )
    client = TestClient(
        create_bootstrap_app(
            context=resolve_bootstrap_context(environ=environment),
            settings_runtime=runtime,
        )
    )
    try:
        token = _token(client)
        reference = _credential(client, token)
        draft = _create_draft(
            client, token, base=None, changes=_business_changes(profile, reference)
        )
        draft_id = draft.json()["revision_id"]
        preview = client.post(
            "/api/settings/initialization/preview",
            json={"draft_revision": draft_id},
            headers=_write_headers(token),
        )
        assert preview.status_code == 200, preview.text
        applied = client.post(
            "/api/settings/initialization/apply",
            json={
                "confirmation_id": preview.json()["preview_id"],
                "expected_revision": draft_id,
            },
            headers=_write_headers(token),
        )
        assert applied.status_code == 202, applied.text
        operation = _wait_operation(client, token, applied.json()["operation_id"])
        assert operation["status"] == "failed"
        assert operation["error_code"] == "TARGET_IDENTITY_MISMATCH"
        manifest = runtime.config.read_manifest()
        assert manifest.active_revision is None
        assert manifest.pending_restart_revision is None
    finally:
        client.close()
        runtime.close()


def test_mounted_secret_can_be_selected_but_not_written_or_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = (tmp_path / "mounted").resolve()
    root.mkdir()
    provider_secret = root / "provider.txt"
    jwt_secret = root / "jwt.txt"
    provider_secret.write_text("synthetic-mounted-provider-value", encoding="utf-8")
    jwt_secret.write_text(
        "synthetic-mounted-jwt-secret-with-at-least-32-chars", encoding="utf-8"
    )
    monkeypatch.setenv(
        "EDUAGENT_SECRET_MOUNTS",
        json.dumps(
            {
                "synthetic-provider": {
                    "path": str(provider_secret.resolve()),
                    "purpose": "provider",
                },
                "synthetic-jwt": {
                    "path": str(jwt_secret.resolve()),
                    "purpose": "jwt",
                },
            }
        ),
    )
    profile = resolve_environment_profile(process_env=_environ(root))
    environment = _environ_for(profile)
    runtime = build_settings_runtime(
        profile,
        credential_backend="mounted_secret",
        file_security=FakeFileSecurity(),
    )
    client = TestClient(
        create_bootstrap_app(
            context=resolve_bootstrap_context(environ=environment),
            settings_runtime=runtime,
        )
    )
    try:
        token = _token(client)
        _mounts, catalog = mounted_mounts_from_env(profile.environment_id)
        by_id = {reference.credential_id: reference for reference in catalog.values()}
        provider_ref = next(
            reference for reference in by_id.values() if reference.purpose == "provider"
        )
        jwt_ref = next(
            reference for reference in by_id.values() if reference.purpose == "jwt"
        )

        write = client.post(
            "/api/settings/credentials",
            json={"purpose": "provider", "secret_value": "synthetic-new-value"},
            headers=_write_headers(token),
        )
        assert write.status_code == 405
        assert write.json()["code"] == "EXTERNALLY_MANAGED"

        changes = _business_changes(profile, jwt_ref.model_dump(mode="json"))
        draft = _create_draft(client, token, base=None, changes=changes)
        draft_id = draft.json()["revision_id"]
        validated = client.post(
            f"/api/settings/drafts/{draft_id}/validate",
            json={"expected_revision": None},
            headers=_write_headers(token),
        )
        assert validated.status_code == 200, validated.text
        activated = _activate(client, token, draft_id, active=None)
        assert activated.status_code == 200, activated.text

        deleted = client.request(
            "DELETE",
            f"/api/settings/credentials/{jwt_ref.credential_id}/versions/{jwt_ref.version}",
            json={},
            headers=_write_headers(token),
        )
        assert deleted.status_code == 405
        assert deleted.json()["code"] == "EXTERNALLY_MANAGED"

        profile_id, profile_revision = uuid4(), uuid4()
        changes = _business_changes(profile, jwt_ref.model_dump(mode="json"))
        changes["providers"] = {
            str(profile_id): {
                "profile_id": str(profile_id),
                "profile_revision": str(profile_revision),
                "display_name": "Synthetic mounted provider",
                "adapter_id": "openai_compatible",
                "model_id": "synthetic-model",
                "base_url": "https://example.invalid/v1",
                "auth_mode": "api_key",
                "declared_capabilities": ["text", "json_object"],
                "parameters": {},
                "enabled": True,
            }
        }
        changes["bindings"] = {
            "default_llm": {
                "task_kind": "default_llm",
                "profile_id": str(profile_id),
                "profile_revision": str(profile_revision),
                "required_capabilities": ["json_object"],
            }
        }
        provider_draft = _create_draft(
            client,
            token,
            base=activated.json()["revision_id"],
            changes=changes,
        )
        assert provider_draft.status_code == 201, provider_draft.text
        provider_draft_id = provider_draft.json()["revision_id"]
        bound = client.post(
            f"/api/settings/drafts/{provider_draft_id}/credential-bindings",
            json={
                "profile_id": str(profile_id),
                "credential_ref": str(provider_ref.credential_id),
                "version": str(provider_ref.version),
            },
            headers=_write_headers(token),
        )
        assert bound.status_code == 200, bound.text
        bound_ref = bound.json()["values"]["providers"][str(profile_id)]["credential_ref"]
        assert bound_ref["external_secret_id"] == "synthetic-provider"
        assert bound_ref["backend"] == "mounted_secret"

        unknown = client.post(
            f"/api/settings/drafts/{bound.json()['revision_id']}/credential-bindings",
            json={
                "profile_id": str(profile_id),
                "credential_ref": str(uuid4()),
                "version": str(uuid4()),
            },
            headers=_write_headers(token),
        )
        assert unknown.status_code == 403
        assert unknown.json()["code"] == "SECRET_REFERENCE_NOT_ALLOWED"

        assert provider_secret.read_text(encoding="utf-8") == "synthetic-mounted-provider-value"
        assert jwt_secret.read_text(encoding="utf-8").startswith("synthetic-mounted-jwt")
    finally:
        client.close()
        runtime.close()


# ---------------------------------------------------------------------------
# T083：未配置或停用 rerank 时不得继承默认 LLM 的 rerank 投影
# ---------------------------------------------------------------------------


def _t083_provider(
    store: Any, *, adapter_id: str, model_id: str, capabilities: tuple[str, ...]
) -> tuple[str, str, dict[str, Any]]:
    """构造合成 provider；凭据只经真实 CredentialStore 写入，不读真实 Key。"""

    profile_id, revision = str(uuid4()), str(uuid4())
    reference = store.put(
        purpose="provider", secret_value=SecretStr("synthetic-t083-" + uuid4().hex)
    )
    record = {
        "profile_id": profile_id,
        "profile_revision": revision,
        "display_name": f"Synthetic {adapter_id}",
        "adapter_id": adapter_id,
        "model_id": model_id,
        "base_url": (
            "https://api.deepseek.com"
            if adapter_id == "deepseek"
            else "https://model.invalid/v1"
        ),
        "credential_ref": reference.model_dump(mode="json"),
        "auth_mode": "api_key",
        "declared_capabilities": list(capabilities),
        "enabled": True,
    }
    return profile_id, revision, record


def _t083_values(
    runtime: SettingsRuntime,
    profile: Any,
    *,
    rerank_binding: dict[str, Any] | None = None,
    rerank_profile: tuple[str, str] | None = None,
) -> ConfigurationValues:
    """DeepSeek 作为 default_llm，rerank 可选：缺省 / 显式停用 / 显式绑定。"""

    store = runtime.credential_store()
    jwt = store.put(
        purpose="jwt",
        secret_value=SecretStr("synthetic-t083-jwt-secret-with-at-least-32-chars"),
    )
    values = baseline_values(runtime.config.root, profile.environment_id, jwt)
    default_id, default_revision, default_record = _t083_provider(
        store,
        adapter_id="deepseek",
        model_id="deepseek-flash",
        capabilities=("text", "json_object"),
    )
    providers: dict[str, Any] = {default_id: default_record}
    bindings: dict[str, Any] = {
        "default_llm": {
            "task_kind": "default_llm",
            "profile_id": default_id,
            "profile_revision": default_revision,
        }
    }
    if rerank_profile is not None:
        adapter_id, model_id = rerank_profile
        rerank_id, rerank_revision, rerank_record = _t083_provider(
            store, adapter_id=adapter_id, model_id=model_id, capabilities=("llm_rerank",)
        )
        providers[rerank_id] = rerank_record
        bindings["rerank"] = {
            "task_kind": "rerank",
            "profile_id": rerank_id,
            "profile_revision": rerank_revision,
        }
    elif rerank_binding is not None:
        bindings["rerank"] = rerank_binding
    values["providers"] = providers
    values["bindings"] = bindings
    return ConfigurationValues.model_validate(values)


@pytest.mark.parametrize(
    "rerank_binding",
    [
        pytest.param(None, id="absent"),
        pytest.param({"task_kind": "rerank"}, id="disabled"),
    ],
)
def test_projection_keeps_rerank_disabled_without_explicit_binding(
    runtime: SettingsRuntime, profile: Any, rerank_binding: dict[str, Any] | None
) -> None:
    values = _t083_values(runtime, profile, rerank_binding=rerank_binding)
    before = values.model_dump(mode="json")

    projection = runtime.app_settings_for(values)

    assert projection.llm_provider == "deepseek"
    assert projection.deepseek_model == "deepseek-flash"
    assert projection.rerank_provider == "none"
    assert projection.rerank_model is None
    # 投影不得就地改写候选配置，否则激活前的差异比较会失真。
    assert values.model_dump(mode="json") == before


def test_projection_preserves_explicitly_bound_rerank_profile(
    runtime: SettingsRuntime, profile: Any
) -> None:
    values = _t083_values(runtime, profile, rerank_profile=("llm", "synthetic-rerank"))

    projection = runtime.app_settings_for(values)

    assert projection.llm_provider == "deepseek"
    assert projection.rerank_provider == "llm"
    assert projection.rerank_model == "synthetic-rerank"
