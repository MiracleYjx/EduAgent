"""TCR-027 / T223：首次向导的合成 HTTP 生命周期，不访问真实依赖或模型。

保留真实配置、凭据事务、Registry、Probe 和 Bootstrap 工厂；只替换外部传输、
依赖事实、迁移执行器与原恢复钩子。保存/读取/恢复绝不自动重放付费操作。
"""

from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from backend.app.core.bootstrap import (
    create_application,
    create_bootstrap_app,
    resolve_bootstrap_context,
)
from backend.app.core.config import reset_settings_cache
from backend.app.core.environment import resolve_environment_profile
from backend.app.schemas.settings import (
    CredentialReference,
    DatabaseIdentity,
    EnvironmentName,
)
from backend.app.schemas.settings import (
    TestResourceProfile as ResourceProfile,
)
from backend.app.services.settings import readiness
from backend.app.services.settings.management import (
    OperationRegistry,
    build_settings_runtime,
)
from tests.integration.test_settings_management import (
    _activate,
    _auth,
    _business_changes,
    _create_draft,
    _credential,
    _probe_draft,
    _ready_probes,
    _token,
    _wait_operation,
    _write_headers,
)
from tests.support.credential_fakes import FakeFileSecurity, FakeProtector
from tests.support.provider_probe_fakes import (
    FakeClock,
    HttpClientFactory,
    HttpScenario,
    _internal_selfpipe,
)

_REAL_CONNECT = socket.socket.connect
_REAL_BIND = socket.socket.bind


@pytest.fixture(autouse=True)
def synthetic_boundary(monkeypatch: pytest.MonkeyPatch):
    for name in tuple(os.environ):
        upper = name.upper()
        if upper.startswith(("EDUAGENT_", "LANGCHAIN_", "LANGSMITH_")) or upper.endswith("_API_KEY") or upper in {
            "DATABASE_URL", "REDIS_URL", "JWT_SECRET_KEY", "DEV_MODE", "STORAGE_ROOT",
            "LLM_PROVIDER", "EMBEDDING_PROVIDER", "RERANK_PROVIDER", "CONFIDENCE_THRESHOLD",
            "JWT_ALGORITHM", "JWT_EXPIRE_MINUTES",
        }:
            monkeypatch.delenv(name, raising=False)

    def forbidden(*_args: Any, **_kwargs: Any):
        raise AssertionError("T223 禁止真实网络、数据库或子进程访问")

    def connect(sock: socket.socket, address: Any):
        if _internal_selfpipe():
            return _REAL_CONNECT(sock, address)
        return forbidden()

    def bind(sock: socket.socket, address: Any):
        if _internal_selfpipe():
            return _REAL_BIND(sock, address)
        return forbidden()

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "bind", bind)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(Engine, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setenv("GRADIO_ANALYTICS_ENABLED", "False")
    monkeypatch.setattr(readiness, "DEFAULT_PROBES", _ready_probes())
    yield
    reset_settings_cache()


@pytest.fixture
def flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = (tmp_path / "synthetic").resolve()
    root.mkdir()
    resource = ResourceProfile(
        environment=EnvironmentName.TEST, environment_id=uuid4(), root=root,
        database_identity=DatabaseIdentity(
            host="127.0.0.1", port=1, database="eduagent_test", user="eduagent_test_runner",
            schema_name="public", instance_id="synthetic-t223-no-service",
        ),
    )
    receipt = tmp_path / "test-resource-profile.json"
    receipt.write_text(resource.model_dump_json(), encoding="utf-8")
    environment = {
        "EDUAGENT_ENVIRONMENT": "test", "EDUAGENT_DATA_ROOT": str(root),
        "EDUAGENT_ENVIRONMENT_ID": str(resource.environment_id),
        "EDUAGENT_TEST_RESOURCE_PROFILE": str(receipt),
        "GRADIO_ANALYTICS_ENABLED": "False",
    }
    profile = resolve_environment_profile(process_env=environment)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    clock = FakeClock()
    head = {"value": "previous-head"}
    calls: list[str] = []

    def migrate(url: str) -> None:
        calls.append(url)
        head["value"] = readiness.discover_migration_head()

    protector = FakeProtector()

    def build():
        runtime = build_settings_runtime(
            profile,
            credential_backend="dpapi",
            protector=protector,
            file_security=FakeFileSecurity(),
            clock=clock,
            read_head=lambda _url: head["value"],
            migrator=migrate,
            initialization_verify=lambda _url, _expected: None,
        )
        # Explicit non-terminal-run fact source, not a bypass of admission.
        monkeypatch.setattr(runtime.run_facts, "bindings", lambda: ())
        return runtime

    runtime = build()
    app = create_bootstrap_app(
        context=resolve_bootstrap_context(environ=environment), settings_runtime=runtime
    )
    app.state.setup_security._clock = clock
    with TestClient(app) as client:
        try:
            yield {
                "client": client, "runtime": runtime, "profile": profile,
                "clock": clock, "head": head, "calls": calls, "build": build, "environment": environment,
            }
        finally:
            runtime.close()


def _validated(flow: dict[str, Any], *, base: str | None = None, host: str = "127.0.0.1"):
    client = flow["client"]
    token = _token(client)
    reference = _credential(client, token)
    response = _create_draft(
        client, token, base=base,
        changes=_business_changes(flow["profile"], reference, host=host),
    )
    assert response.status_code == 201, response.text
    draft = response.json()
    checked = client.post(
        f"/api/settings/drafts/{draft['revision_id']}/validate",
        json={"expected_revision": base}, headers=_write_headers(token),
    )
    assert checked.status_code == 200 and checked.json()["valid"] is True, checked.text
    return token, draft


def _finish_probe(client: TestClient, token: str, probe_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/settings/probes/{probe_id}", headers=_auth(token))
        assert response.status_code == 200, response.text
        result = response.json()
        if result["status"] not in {"queued", "running"}:
            return result
        time.sleep(0.01)
    raise AssertionError("合成 Probe 未在期限内结束")


def test_empty_bootstrap_and_saved_draft_survive_reauthorization_without_writes(flow):
    client, runtime = flow["client"], flow["runtime"]
    assert client.get("/health").status_code == 200
    assert client.get("/setup-ready").status_code == 200
    assert client.get("/ready").status_code == 503
    assert client.get("/api/courses").json()["code"] == "BUSINESS_NOT_READY"
    token, draft = _validated(flow)
    saved = client.patch(
        "/api/settings/onboarding",
        json={"expected_revision": 0, "current_step": 2, "draft_revision": draft["revision_id"]},
        headers=_write_headers(token),
    )
    assert saved.status_code == 200
    before = {path.relative_to(runtime.config.root): path.read_bytes()
              for path in runtime.config.root.rglob("*") if path.is_file() and path.name != "writer.lock"}
    flow["clock"].advance(601)
    assert client.get("/api/settings/onboarding", headers=_auth(token)).status_code == 401
    token = _token(client)
    for _ in range(2):
        result = client.get("/api/settings/onboarding", headers=_auth(token))
        assert result.status_code == 200, result.text
        body = result.json()
        assert body["draft"]["revision_id"] == draft["revision_id"]
        assert body["draft"]["values"]["business"]["jwt_secret_ref"]["purpose"] == "jwt"
        assert body["probes"] == [] and body["initialization"] is None
        assert body["readiness"]["business"]["ready"] is False
        assert "synthetic-t220-jwt-secret-with" not in result.text
    after = {path.relative_to(runtime.config.root): path.read_bytes()
             for path in runtime.config.root.rglob("*") if path.is_file() and path.name != "writer.lock"}
    assert after == before
    assert flow["calls"] == []


def test_initialization_is_associated_with_draft_and_confirmed_only_once(flow):
    client = flow["client"]
    token, draft = _validated(flow)
    client.patch(
        "/api/settings/onboarding",
        json={"expected_revision": 0, "current_step": 3, "draft_revision": draft["revision_id"]},
        headers=_write_headers(token),
    )
    preview = client.post(
        "/api/settings/initialization/preview", json={"draft_revision": draft["revision_id"]},
        headers=_write_headers(token),
    ).json()
    applied = client.post(
        "/api/settings/initialization/apply",
        json={"confirmation_id": preview["preview_id"], "expected_revision": draft["revision_id"]},
        headers=_write_headers(token),
    )
    assert applied.status_code == 202, applied.text
    operation = _wait_operation(client, token, applied.json()["operation_id"])
    assert operation["status"] == "succeeded"
    assert flow["profile"].storage_root.is_dir()
    repeated = client.post(
        "/api/settings/initialization/apply",
        json={"confirmation_id": preview["preview_id"], "expected_revision": draft["revision_id"]},
        headers=_write_headers(token),
    )
    assert repeated.status_code == 202, repeated.text
    assert repeated.json()["operation_id"] == operation["operation_id"]
    assert len(flow["calls"]) == 1
    restored = client.get("/api/settings/onboarding", headers=_auth(token)).json()
    assert restored["initialization"]["operation_id"] == operation["operation_id"]
    assert restored["initialization"]["status"] == "succeeded"
    manifest = flow["runtime"].config.read_manifest()
    assert manifest.active_revision is None and manifest.pending_restart_revision is None
    registry = OperationRegistry(flow["runtime"].config.root)
    assert registry.latest_initialization(UUID(draft["revision_id"])).operation_id == UUID(operation["operation_id"])


def test_initialization_same_confirmation_concurrency_runs_at_most_once(flow):
    token, draft = _validated(flow)
    client, runtime = flow["client"], flow["runtime"]
    service = runtime.initialization()
    preview = service.preview(UUID(draft["revision_id"]))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(service.apply, preview.preview_id, UUID(draft["revision_id"])) for _ in range(2)]
        results = [future.result() for future in futures]
    assert results[0].operation_id == results[1].operation_id
    result = _wait_operation(client, token, str(results[0].operation_id))
    assert result["status"] == "succeeded"
    assert len(flow["calls"]) == 1


@pytest.mark.parametrize("change", ["unknown_head", "changed_head", "expired"])
def test_initialization_unknown_changed_or_expired_target_never_runs(flow, monkeypatch, change):
    token, draft = _validated(flow)
    client = flow["client"]
    if change == "unknown_head":
        monkeypatch.setattr(readiness, "discover_migration_head", lambda: None)
    preview = client.post(
        "/api/settings/initialization/preview", json={"draft_revision": draft["revision_id"]},
        headers=_write_headers(token),
    ).json()
    if change == "changed_head":
        monkeypatch.setattr(readiness, "discover_migration_head", lambda: "another-head")
    elif change == "expired":
        flow["clock"].advance(601)
        token = _token(client)
    else:
        assert preview["allowed"] is False
    response = client.post(
        "/api/settings/initialization/apply",
        json={"confirmation_id": preview["preview_id"], "expected_revision": draft["revision_id"]},
        headers=_write_headers(token),
    )
    assert response.status_code in {409, 503}, response.text
    assert flow["calls"] == []


def test_pending_is_consumed_only_by_new_startup_and_uses_candidate_values(flow, monkeypatch):
    token, first = _validated(flow)
    client, runtime = flow["client"], flow["runtime"]
    first_active = _activate(client, token, first["revision_id"], active=None).json()["revision_id"]
    _, second = _validated(flow, base=first_active, host="127.0.0.2")
    changed = _activate(client, token, second["revision_id"], active=first_active)
    assert changed.status_code == 202, changed.text
    pending_id = changed.json()["revision_id"]
    assert client.get("/ready").status_code == 503
    assert runtime.config.read_manifest().active_revision == UUID(first_active)
    runtime.close()
    reopened = flow["build"]()
    recorded: list[str] = []

    def factory(*, settings, **_kwargs):
        from fastapi import FastAPI

        recorded.append(str(settings.database_url))
        app = FastAPI()
        app.state.business_assembly = "assembled"
        return app

    import backend.app.core.app as full_app

    monkeypatch.setattr(full_app, "create_app", factory)
    try:
        result = create_application(
            context=resolve_bootstrap_context(environ=flow["environment"]),
            settings_runtime=reopened, probes=_ready_probes(),
        )
        assert result.state.business_assembly == "assembled", getattr(result.state, "business_failure", None)
        manifest = reopened.config.read_manifest()
        assert str(manifest.active_revision) == pending_id
        assert manifest.pending_restart_revision is None
        assert "127.0.0.2" in recorded[0]
        assert flow["calls"] == []
    finally:
        reopened.close()


def test_pending_with_unavailable_credential_preserves_both_pointers(flow, monkeypatch):
    token, first = _validated(flow)
    client, runtime = flow["client"], flow["runtime"]
    active = _activate(client, token, first["revision_id"], active=None).json()["revision_id"]
    _, candidate = _validated(flow, base=active, host="127.0.0.2")
    pending = _activate(client, token, candidate["revision_id"], active=active).json()["revision_id"]
    store = runtime.credential_store()
    original = store.status

    def unavailable(reference, version):
        return original(reference, version).model_copy(update={"status": "unavailable"})

    monkeypatch.setattr(store, "status", unavailable)
    application = create_application(
        context=resolve_bootstrap_context(environ=flow["environment"]),
        settings_runtime=runtime, probes=_ready_probes(),
    )
    assert application.state.business_assembly == "not_assembled"
    manifest = runtime.config.read_manifest()
    assert str(manifest.active_revision) == active
    assert str(manifest.pending_restart_revision) == pending


def test_database_locator_http_roundtrip_keeps_custom_schema(flow):
    token, draft = _validated(flow)
    client = flow["client"]
    patched = client.patch(
        f"/api/settings/drafts/{draft['revision_id']}",
        json={"expected_revision": None, "changes": {"business": {"database_locator": {"schema": "owned_t223"}}}},
        headers=_write_headers(token),
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["values"]["business"]["database_locator"]["schema"] == "owned_t223"
    copied = _create_draft(client, token, base=None, changes=body["values"])
    assert copied.status_code == 201, copied.text
    assert copied.json()["values"]["business"]["database_locator"]["schema"] == "owned_t223"


@pytest.mark.parametrize("body", [
    {"purpose": "provider", "secret_value": ["synthetic-t223-secret-marker"]},
    {"purpose": "provider", "secret_value": "synthetic-t223-secret-marker", "api_key": "synthetic-t223-secret-marker"},
])
def test_bootstrap_invalid_secret_input_never_echoes_input(flow, body):
    client = flow["client"]
    response = client.post("/api/settings/credentials", json=body, headers=_write_headers(_token(client)))
    assert response.status_code == 422
    assert "synthetic-t223-secret-marker" not in response.text
    assert "input" not in response.json()


def test_async_probes_are_pollable_cancellable_and_restore_existing_facts(flow):
    client, runtime = flow["client"], flow["runtime"]
    token = _token(client)
    draft_id, profile_id = _probe_draft(client, token, flow["profile"], _credential(client, token))
    client.patch(
        "/api/settings/onboarding",
        json={"expected_revision": 0, "current_step": 2, "draft_revision": draft_id},
        headers=_write_headers(token),
    )
    started, release = threading.Event(), threading.Event()
    scenario = HttpScenario()

    # The wrapper delegates to a separate scenario, without a real thread/socket.
    successful = HttpScenario()

    async def wrapped(request):
        started.set()
        import asyncio

        while not release.is_set():
            await asyncio.sleep(0.005)
        return await successful.respond(request)

    scenario.respond = wrapped
    factory = HttpClientFactory(scenario)
    runtime.probes()._http_factory = factory
    rejected = client.post(
        "/api/settings/probes",
        json={"draft_revision": draft_id, "profile_id": profile_id, "capability": "json_object", "allow_billable": False},
        headers=_write_headers(token),
    )
    assert rejected.status_code == 422 and not factory.clients
    created = client.post(
        "/api/settings/probes",
        json={"draft_revision": draft_id, "profile_id": profile_id, "capability": "json_object", "allow_billable": True},
        headers=_write_headers(token),
    )
    assert created.status_code == 202, created.text
    assert created.json()["status"] in {"queued", "running"}
    assert started.wait(1)
    probe_id = created.json()["probe_id"]
    restored = client.get("/api/settings/onboarding", headers=_auth(token)).json()
    assert restored["draft"]["revision_id"] == draft_id
    assert restored["probes"][0]["probe_id"] == probe_id
    cancelled = client.request("DELETE", f"/api/settings/probes/{probe_id}", json={}, headers=_write_headers(token))
    assert cancelled.status_code in {200, 202}
    terminal = _finish_probe(client, token, probe_id)
    assert terminal["status"] == "cancelled"
    assert terminal["may_have_been_billed"] is True
    ended = client.request("DELETE", f"/api/settings/probes/{probe_id}", json={}, headers=_write_headers(token))
    assert ended.status_code == 200
    assert all(item.is_closed for item in factory.clients)


@pytest.mark.parametrize("restart_pending", [False, True])
def test_accepted_ai_does_not_expire_or_trigger_automatic_probe(flow, monkeypatch, restart_pending):
    client, runtime = flow["client"], flow["runtime"]
    token = _token(client)
    draft_id, profile_id = _probe_draft(client, token, flow["profile"], _credential(client, token))
    scenario = HttpScenario()
    runtime.probes()._http_factory = HttpClientFactory(scenario)
    probe_ids = []
    for capability in ("text", "json_object"):
        response = client.post(
            "/api/settings/probes",
            json={"draft_revision": draft_id, "profile_id": profile_id, "capability": capability, "allow_billable": True},
            headers=_write_headers(token),
        )
        assert response.status_code == 202, response.text
        result = _finish_probe(client, token, response.json()["probe_id"])
        assert result["status"] == "succeeded", result
        assert result["external_requests"] == 1
        probe_ids.append(result["probe_id"])
    validated = client.post(
        f"/api/settings/drafts/{draft_id}/validate", json={"expected_revision": None},
        headers=_write_headers(token),
    )
    assert validated.json()["valid"] is True
    accepted = _activate(client, token, draft_id, active=None, probes=probe_ids)
    assert accepted.status_code == 200, accepted.text
    reopened = None
    if restart_pending:
        active_id = accepted.json()["revision_id"]
        values = runtime.config.get_revision(UUID(active_id)).values.model_dump(mode="json", by_alias=True)
        values["business"]["database_locator"]["host"] = "127.0.0.2"
        changed = _create_draft(client, token, base=active_id, changes=values)
        assert changed.status_code == 201, changed.text
        candidate_id = changed.json()["revision_id"]
        checked = client.post(
            f"/api/settings/drafts/{candidate_id}/validate",
            json={"expected_revision": active_id}, headers=_write_headers(token),
        )
        assert checked.status_code == 200 and checked.json()["valid"] is True
        pending = _activate(client, token, candidate_id, active=active_id)
        assert pending.status_code == 202, pending.text
        pending_id = pending.json()["revision_id"]
    flow["clock"].advance(601)
    if restart_pending:
        from fastapi import FastAPI

        import backend.app.core.app as full_app

        def factory(**_kwargs):
            application = FastAPI()
            application.state.business_assembly = "assembled"
            return application

        monkeypatch.setattr(full_app, "create_app", factory)
        runtime.close()
        reopened = flow["build"]()
        try:
            application = create_application(
                context=resolve_bootstrap_context(environ=flow["environment"]),
                settings_runtime=reopened, probes=_ready_probes(),
            )
            assert application.state.business_assembly == "assembled", getattr(application.state, "business_failure", None)
            manifest = reopened.config.read_manifest()
            assert str(manifest.active_revision) == pending_id
            assert manifest.pending_restart_revision is None
            assert reopened.ai_readiness().tasks["default_llm"].activation_validation == "accepted"
            assert len(scenario.requests) == 2
        finally:
            reopened.close()
        return
    token = _token(client)
    for _ in range(2):
        report = client.get("/api/settings/readiness", headers=_auth(token)).json()
        task = report["ai"]["tasks"]["default_llm"]
        assert task["activation_validation"] == "accepted"
        assert task["ready"] is True
        client.get("/api/settings/", headers=_auth(token))
        client.get("/api/settings/onboarding", headers=_auth(token))
    assert len(scenario.requests) == 2


def test_explicit_skip_ai_clears_a_saved_binding_without_any_probe(flow):
    client, runtime = flow["client"], flow["runtime"]
    token = _token(client)
    draft_id, _profile_id = _probe_draft(client, token, flow["profile"], _credential(client, token))
    original = runtime.config.get_draft(UUID(draft_id))
    assert original.values.bindings["default_llm"].profile_id is not None
    skipped = client.patch(
        f"/api/settings/drafts/{draft_id}",
        json={"expected_revision": None, "changes": {"bindings": {"default_llm": {
            "task_kind": "default_llm", "profile_id": None, "profile_revision": None,
            "inherit_default": False, "required_capabilities": [],
        }}}}, headers=_write_headers(token),
    )
    assert skipped.status_code == 200, skipped.text
    draft_id = skipped.json()["revision_id"]
    checked = client.post(
        f"/api/settings/drafts/{draft_id}/validate", json={"expected_revision": None},
        headers=_write_headers(token),
    )
    assert checked.status_code == 200 and checked.json()["valid"] is True
    activated = _activate(client, token, draft_id, active=None)
    assert activated.status_code == 200, activated.text
    report = client.get("/api/settings/readiness", headers=_auth(token)).json()
    assert report["ai"]["tasks"]["default_llm"]["configured"] is False
    assert report["ai"]["tasks"]["reviewer"]["ready"] is True
    assert runtime.existing_probes(UUID(draft_id)) == ()


def test_mounted_catalog_get_is_readonly_and_reference_roundtrips(flow, monkeypatch):
    root, profile = flow["runtime"].config.root, flow["profile"]
    flow["runtime"].close()
    secret = root / "mounted-jwt.txt"
    secret.write_text("synthetic-t223-mounted-jwt-with-at-least-32-characters", encoding="utf-8")
    reference = CredentialReference(
        credential_id=uuid4(), version=uuid4(), environment_id=profile.environment_id,
        backend="mounted_secret", purpose="jwt", external_secret_id="synthetic-jwt",
    )
    mounted = build_settings_runtime(
        profile, credential_backend="mounted_secret", file_security=FakeFileSecurity(),
        mounts={"synthetic-jwt": (reference, secret)},
    )
    app = create_bootstrap_app(
        context=resolve_bootstrap_context(environ=flow["environment"]), settings_runtime=mounted,
    )
    try:
        with TestClient(app) as client:
            token = _token(client)
            before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file() and path.name != "writer.lock"}
            with monkeypatch.context() as guard:
                guard.setattr(mounted, "_open_store", lambda: pytest.fail("GET must not initialize a secret backend"))
                response = client.get("/api/settings/", headers=_auth(token))
                assert response.status_code == 200, response.text
                body = response.json()
                assert "EXTERNALLY_MANAGED" in body["warnings"]
                status = body["credential_statuses"][0]
                assert status["status"] == "unavailable"
                assert status["external_secret_id"] == "synthetic-jwt"
                assert client.get("/api/settings/onboarding", headers=_auth(token)).status_code == 200
            after = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file() and path.name != "writer.lock"}
            assert after == before
            full_ref = {
                "credential_id": status["credential_ref"], "version": status["version"],
                "environment_id": status["environment_id"], "backend": status["backend"],
                "purpose": status["purpose"], "external_secret_id": status["external_secret_id"],
            }
            created = _create_draft(client, token, base=None, changes=_business_changes(profile, full_ref))
            assert created.status_code == 201, created.text
            draft_id = created.json()["revision_id"]
            validated = client.post(
                f"/api/settings/drafts/{draft_id}/validate", json={"expected_revision": None},
                headers=_write_headers(token),
            )
            assert validated.json()["valid"] is True
            activated = _activate(client, token, draft_id, active=None)
            assert activated.status_code == 200, activated.text
            assert secret.read_text(encoding="utf-8") == "synthetic-t223-mounted-jwt-with-at-least-32-characters"
    finally:
        mounted.close()


def test_noauth_profile_projects_to_adapter_without_fabricated_key(flow):
    import asyncio

    from pydantic import BaseModel, ValidationError

    from backend.app.ai.llm.openai_compatible import OpenAICompatibleProvider

    client, runtime = flow["client"], flow["runtime"]
    token = _token(client)
    draft_id, _ = _probe_draft(client, token, flow["profile"], _credential(client, token))
    draft = runtime.config.get_draft(UUID(draft_id))
    projection = runtime.app_settings_for(draft.values)
    assert projection.llm_provider == "openai_compatible"
    assert projection.llm_auth_mode == "none"
    assert projection.deepseek_api_key.get_secret_value() == ""

    class Answer(BaseModel):
        ok: bool

    scenario = HttpScenario()

    async def request_once():
        http = HttpClientFactory(scenario)()
        provider = OpenAICompatibleProvider.from_settings(projection, http_client=http)
        try:
            answer = await provider.generate_structured([{"role": "user", "content": "synthetic T223"}], Answer)
            assert answer.ok is True
        finally:
            await provider.aclose()
            await http.aclose()

    asyncio.run(request_once())
    assert len(scenario.requests) == 1
    assert "Authorization" not in scenario.requests[0].headers
    payload = projection.model_dump()
    with pytest.raises(ValidationError):
        type(projection)(_env_file=None, **{**payload, "llm_auth_mode": "api_key"})
    with pytest.raises(ValidationError):
        type(projection)(_env_file=None, **{**payload, "llm_provider": "deepseek"})
