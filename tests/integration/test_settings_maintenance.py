"""TCR-S4-MAINTENANCE-032: isolated HTTP maintenance; no real services."""
from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

from tests.integration.test_settings_lifecycle import (
    _validated,
    synthetic_boundary,  # noqa: F401
)
from tests.integration.test_settings_lifecycle import (
    flow as flow_fixture,
)
from tests.integration.test_settings_management import (
    _activate,
    _auth,
    _token,
    _write_headers,
)
from tests.support import runtime_settings_fakes as fake

flow = flow_fixture


def test_sources_show_saved_effective_and_pending_separately(flow, monkeypatch):
    client = flow["client"]
    token, draft = _validated(flow)
    active = _activate(client, token, draft["revision_id"], active=None).json()["revision_id"]
    _pending_token, pending_draft = _validated(flow, base=active, host="127.0.0.2")
    pending = _activate(client, token, pending_draft["revision_id"], active=active)
    assert pending.status_code == 202
    monkeypatch.setenv("JWT_EXPIRE_MINUTES", "95")
    view = client.get("/api/settings/", headers=_auth(token)).json()
    assert view["values"]["business"]["jwt_expire_minutes"] != 95
    assert view["effective_values"]["business"]["jwt_expire_minutes"] == 95
    assert view["sources"]["business.jwt_expire_minutes"]["source"] == "process_env"
    assert view["active_revision"] == active
    assert view["pending_values"]["business"]["database_locator"]["host"] == "127.0.0.2"
    assert view["effective_values"]["business"]["database_locator"]["host"] == "127.0.0.1"
    assert "business.database_locator" not in view["sources"]
    assert view["sources"]["business.database_locator.host"]["source"] == "saved"
    assert len(view["credential_statuses"]) == 2
    assert "synthetic-t220-jwt-secret" not in str(view)


def test_recovery_is_read_only_until_confirmed_then_only_stages_pending(flow):
    client, runtime = flow["client"], flow["runtime"]
    token, draft = _validated(flow)
    active = _activate(client, token, draft["revision_id"], active=None).json()["revision_id"]
    manifest = runtime.config.config / "manifest.json"
    before = manifest.read_bytes()
    preview = client.get("/api/settings/recovery", headers=_auth(token))
    assert preview.status_code == 200
    assert manifest.read_bytes() == before
    row = next(item for item in preview.json()["revisions"] if item["revision_id"] == active)
    assert row["available"] is True
    payload = {"revision_id": active, "expected_revision": active, "confirm": False}
    denied = client.post("/api/settings/recovery/restore", json=payload, headers=_write_headers(token))
    assert denied.status_code == 422
    assert manifest.read_bytes() == before
    restored = client.post("/api/settings/recovery/restore", json=payload | {"confirm": True}, headers=_write_headers(token))
    assert restored.status_code == 200, restored.text
    assert restored.json()["status"] == "pending_restart"
    assert runtime.config.read_manifest().active_revision == UUID(active)
    assert runtime.config.read_manifest().pending_restart_revision == UUID(active)


def test_damaged_manifest_requires_unchanged_fingerprint_and_preserves_damage(flow):
    client, runtime = flow["client"], flow["runtime"]
    token, draft = _validated(flow)
    active = _activate(client, token, draft["revision_id"], active=None).json()["revision_id"]
    manifest = runtime.config.config / "manifest.json"
    manifest.write_bytes(b"{synthetic-damage")
    preview = client.get("/api/settings/recovery", headers=_auth(token)).json()
    assert preview["damaged"] is True
    payload = {"revision_id": active, "expected_revision": None, "expected_manifest_fingerprint": preview["manifest_fingerprint"], "confirm": True}
    manifest.write_bytes(b"{changed-damage")
    rejected = client.post("/api/settings/recovery/restore", json=payload, headers=_write_headers(token))
    assert rejected.status_code == 409
    assert manifest.read_bytes() == b"{changed-damage"
    fresh = client.get("/api/settings/recovery", headers=_auth(token)).json()
    payload["expected_manifest_fingerprint"] = fresh["manifest_fingerprint"]
    restored = client.post("/api/settings/recovery/restore", json=payload, headers=_write_headers(token))
    assert restored.status_code == 200, restored.text
    assert runtime.config.read_manifest().active_revision is None
    assert runtime.config.read_manifest().pending_restart_revision == UUID(active)
    assert list(runtime.config.config.glob("manifest-recovery-*.json"))


def test_index_preview_survives_http_request_but_not_a_different_session(flow, monkeypatch):
    import backend.app.services.settings.index_identity as identity
    from backend.app.core import database

    repository = fake.FakeKnowledgeBaseRepository()
    row = repository.add(chunks={"legacy": fake.synthetic_vector(1)})
    runtime, client = flow["runtime"], flow["client"]
    closed: list[str] = []
    monkeypatch.setattr(runtime, "effective_business_settings", lambda: (object(), None))
    monkeypatch.setattr(database, "create_database_engine", lambda *_a, **_kw: SimpleNamespace(dispose=lambda: closed.append("engine")))
    monkeypatch.setattr(database, "create_session_factory", lambda _engine: lambda: SimpleNamespace(close=lambda: closed.append("session")))
    monkeypatch.setattr(identity, "SqlAlchemyIndexRepository", lambda _db: repository)
    token = _token(client)
    previous = client.get(f"/api/settings/index-identities/{row.kb_id}", headers=_auth(token)).json()
    assert previous["status"] == "legacy_unknown" and previous["model_id"] is None
    payload = {"expected_identity_revision": row.identity_revision, "proposed_identity": fake.proposed_identity(),
               "evidence": [fake.evidence_record(row.kb_id)], "operator_attestation": {"verified": True, "covers": "synthetic legacy vectors"}}
    preview = client.post(f"/api/settings/index-identities/{row.kb_id}/adoption-preview", json=payload, headers=_write_headers(token))
    assert preview.status_code == 200, preview.text
    confirmation = {"preview_id": preview.json()["preview_id"], "expected_identity_revision": row.identity_revision, "confirm": True}
    wrong_target = client.post("/api/settings/index-identities/22222222-2222-4222-8222-222222222222/adoption-confirm", json=confirmation, headers=_write_headers(token))
    assert wrong_target.status_code == 409
    second = _token(client)
    denied = client.post(f"/api/settings/index-identities/{row.kb_id}/adoption-confirm", json=confirmation, headers=_write_headers(second))
    assert denied.status_code in (403, 409), denied.text
    confirmed = client.post(f"/api/settings/index-identities/{row.kb_id}/adoption-confirm", json=confirmation, headers=_write_headers(token))
    assert confirmed.status_code == 200, confirmed.text
    after = client.get(f"/api/settings/index-identities/{row.kb_id}", headers=_auth(token)).json()
    assert after["status"] == "legacy_adopted" and after["audits"]
    assert row.chunks == {"legacy": fake.synthetic_vector(1)}
    assert closed.count("engine") == closed.count("session") == 6


def test_legacy_preview_apply_survives_requests_and_leaves_source_bytes(flow, monkeypatch, tmp_path):
    import json

    client = flow["client"]
    path = tmp_path / "synthetic-import.env"
    path.write_text(
        "DATABASE_URL=postgresql+psycopg://runner:synthetic-db@database.invalid:5432/old_test\n"
        "REDIS_URL=redis://:synthetic-redis@redis.invalid:6379/2\n"
        "JWT_SECRET_KEY=synthetic-jwt-with-at-least-32-characters\n"
        "LLM_PROVIDER=openai_compatible\nDEEPSEEK_MODEL=synthetic-model\n"
        "DEEPSEEK_BASE_URL=https://model.invalid/v1\nDEEPSEEK_API_KEY=synthetic-model-key\n"
        f"STORAGE_ROOT={tmp_path / 'storage'}\nRERANK_PROVIDER=none\nOCR_ENABLED=false\n",
        encoding="utf-8",
    )
    original = path.read_bytes()
    monkeypatch.setenv("EDUAGENT_LEGACY_IMPORT_SOURCES", json.dumps({"selected": str(path.resolve())}))
    token = _token(client)
    sources = client.get("/api/settings/legacy-import/sources", headers=_auth(token)).json()
    assert sources == {"sources": ["selected"]}
    preview = client.post("/api/settings/legacy-import/preview", json={"source_id": "selected"}, headers=_write_headers(token))
    assert preview.status_code == 200, preview.text
    assert "synthetic-model-key" not in preview.text
    payload = {"preview_id": preview.json()["preview_id"], "confirm": True}
    imported = client.post("/api/settings/legacy-import/apply", json=payload, headers=_write_headers(token))
    assert imported.status_code == 200, imported.text
    assert imported.json()["status"] == "draft"
    assert path.read_bytes() == original
    assert "synthetic-model-key" not in imported.text
    assert flow["runtime"].config.read_manifest().active_revision is None
