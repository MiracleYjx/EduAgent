"""TCR-S1-ENV-CONFIG-002: observable persistence, faults and process ownership."""

import hashlib
import json
import os
import subprocess
import sys
from uuid import uuid4

import pytest

from backend.app.services.settings.config_service import ConfigService


def values(root, env_id, *, minutes=60, port=8000):
    return {"bootstrap": {"port": port}, "business": {
        "database_locator": {"host": "test.invalid", "database": "synthetic_test", "user": "runner"},
        "redis_locator": {"host": "redis.invalid"}, "storage_root": str(root / "storage"),
        "jwt_secret_ref": {"credential_id": str(uuid4()), "version": str(uuid4()),
                           "environment_id": str(env_id), "backend": "dpapi", "purpose": "jwt"},
        "jwt_expire_minutes": minutes,
    }}


def activate(service, document, expected=None):
    draft = service.create_draft(document, expected_revision=expected)
    service.validate(draft.revision_id, expected_revision=expected)
    return service.activate(draft.revision_id, expected_revision=expected)


@pytest.fixture
def repository(tmp_path):
    env_id = uuid4()
    with ConfigService(tmp_path, env_id) as service:
        yield service, tmp_path, env_id


def test_save_validate_and_activate_are_separate(repository):
    service, root, env_id = repository
    draft = service.create_draft(values(root, env_id), expected_revision=None)
    assert service.read_manifest().active_revision is None
    service.validate(draft.revision_id, expected_revision=None)
    assert service.read_manifest().active_revision is None
    active = service.activate(draft.revision_id, expected_revision=None)
    manifest = json.loads((root / "config/manifest.json").read_text(encoding="utf-8"))
    assert manifest["active_revision"] == str(active.revision_id)
    assert manifest["pending_restart_revision"] is None
    assert service.active_revision().values.business.jwt_expire_minutes == 60
    assert active.activated_at is not None
    assert draft.activated_at is None


def test_incomplete_draft_can_be_saved_but_not_activated(repository):
    service, _, _ = repository
    draft = service.create_draft({}, expected_revision=None)
    with pytest.raises(ValueError, match="BUSINESS_CONFIGURATION_INCOMPLETE"):
        service.validate(draft.revision_id, expected_revision=None)
    assert service.read_manifest().active_revision is None


def test_activation_requires_explicit_validation(repository):
    service, root, env_id = repository
    draft = service.create_draft(values(root, env_id), expected_revision=None)
    with pytest.raises(ValueError, match="VALIDATION_REQUIRED"):
        service.activate(draft.revision_id, expected_revision=None)


def test_business_change_is_pending_and_revision_immutable(repository):
    service, root, env_id = repository
    first = activate(service, values(root, env_id))
    path = root / "config/revisions" / f"{first.revision_id}.json"
    original = path.read_bytes()
    changed = first.values.model_dump(mode="json")
    changed["business"]["database_locator"]["database"] = "other_synthetic_test"
    pending = activate(service, changed, first.revision_id)
    manifest = service.read_manifest()
    assert manifest.active_revision == first.revision_id
    assert manifest.pending_restart_revision == pending.revision_id
    assert service.active_revision().revision_id == first.revision_id
    assert path.read_bytes() == original
    assert pending.status == "pending_restart"
    with ConfigService(root, env_id, writable=False) as reader:
        assert reader.read_manifest() == manifest


def test_port_change_needs_restart(repository):
    service, root, env_id = repository
    first = activate(service, values(root, env_id))
    changed = first.values.model_dump(mode="json")
    changed["bootstrap"]["port"] = 8100
    pending = activate(service, changed, first.revision_id)
    assert service.read_manifest().pending_restart_revision == pending.revision_id
    assert service.active_revision().values.bootstrap.port == 8000


def test_policy_activation_only_affects_new_active(repository):
    service, root, env_id = repository
    first = activate(service, values(root, env_id))
    changed = first.values.model_dump(mode="json")
    changed["policies"]["confidence_threshold"] = 0.7
    second = activate(service, changed, first.revision_id)
    assert service.read_manifest().active_revision == second.revision_id
    assert service.get_revision(first.revision_id).values.policies.confidence_threshold == 0.8


def test_stale_expected_revision_conflicts_without_overwrite(repository):
    service, root, env_id = repository
    first = activate(service, values(root, env_id))
    before = (root / "config/manifest.json").read_bytes()
    with pytest.raises(ValueError, match="REVISION_CONFLICT") as error:
        service.create_draft(first.values, expected_revision=uuid4())
    assert error.value.status_code == 409
    assert (root / "config/manifest.json").read_bytes() == before


@pytest.mark.parametrize("fault", ["revision_write", "revision_flush", "revision_replace", "manifest_replace"])
def test_write_failure_preserves_active_and_candidate(repository, monkeypatch, fault):
    import backend.app.services.settings.config_service as module
    service, root, env_id = repository
    first = activate(service, values(root, env_id))
    changed = first.values.model_dump(mode="json")
    changed["policies"]["confidence_threshold"] = 0.6
    draft = service.create_draft(changed, expected_revision=first.revision_id)
    service.validate(draft.revision_id, expected_revision=first.revision_id)
    before = (root / "config/manifest.json").read_bytes()
    original_replace = module.os.replace
    original_write = service._atomic_write

    def replacement(src, dst):
        if (fault == "manifest_replace" and str(dst).endswith("manifest.json")) or (
            fault == "revision_replace" and "/revisions/" in str(dst).replace("\\", "/")
        ):
            raise OSError("synthetic-sensitive-disk-failure")
        return original_replace(src, dst)

    def writer(path, payload, stage):
        if fault == "revision_write" and stage == "revision":
            raise OSError("synthetic-sensitive-disk-failure")
        return original_write(path, payload, stage)

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "replace", replacement)
        patch.setattr(service, "_atomic_write", writer)
        if fault == "revision_flush":
            patch.setattr(module.os, "fsync", lambda _: (_ for _ in ()).throw(OSError("synthetic-sensitive-disk-failure")))
        with pytest.raises(ValueError, match="CONFIG_IO_ERROR") as error:
            service.activate(draft.revision_id, expected_revision=first.revision_id)
        assert "synthetic-sensitive" not in str(error.value)
        assert error.value.stage in {"revision", "manifest"}
    assert (root / "config/manifest.json").read_bytes() == before
    assert service.active_revision().revision_id == first.revision_id
    assert service.get_draft(draft.revision_id).values == draft.values
    if fault == "manifest_replace":
        assert len(list((root / "config/revisions").glob("*.json"))) == 2


@pytest.mark.parametrize("damage", [b"{broken", b'{"schema_version":999}'])
def test_corruption_never_auto_repaired_and_restore_is_explicit(repository, damage):
    service, root, env_id = repository
    first = activate(service, values(root, env_id))
    path = root / "config/manifest.json"
    path.write_bytes(damage)
    with pytest.raises(ValueError, match="CORRUPT_CONFIGURATION|UNSUPPORTED_SCHEMA"):
        service.read_manifest()
    with pytest.raises(ValueError, match="CORRUPT_CONFIGURATION|UNSUPPORTED_SCHEMA"):
        service.create_draft(first.values, expected_revision=first.revision_id)
    assert path.read_bytes() == damage
    with pytest.raises(ValueError, match="RESTORE_CONFIRMATION_REQUIRED"):
        service.restore_revision(first.revision_id, expected_revision=None)
    fingerprint = hashlib.sha256(damage).hexdigest()
    restored = service.restore_revision(first.revision_id, expected_revision=None,
                                        expected_manifest_fingerprint=fingerprint)
    assert restored.pending_restart_revision == first.revision_id
    assert restored.active_revision is None
    assert damage in [file.read_bytes() for file in (root / "config").glob("manifest-recovery-*.json")]
    with ConfigService(root, env_id, writable=False) as reader:
        assert reader.read_manifest() == restored


def test_wrong_environment_and_future_revision_rejected(repository):
    service, root, env_id = repository
    first = activate(service, values(root, env_id))
    path = root / "config/revisions" / f"{first.revision_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["environment_id"] = str(uuid4())
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="ENVIRONMENT_MISMATCH"):
        service.active_revision()
    data["environment_id"] = str(env_id)
    data["schema_version"] = 999
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="UNSUPPORTED_SCHEMA"):
        service.active_revision()


def test_second_process_writer_rejected_then_lock_released(repository):
    service, root, env_id = repository
    script = """
import sys
from pathlib import Path
from uuid import UUID
from backend.app.services.settings.config_service import ConfigService
try:
    with ConfigService(Path(sys.argv[1]), UUID(sys.argv[2])):
        print("ACQUIRED")
except ValueError as exc:
    print(str(exc))
    sys.exit(23)
"""
    # Child gets a small explicit environment, never model/database secrets.
    env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP") if key in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    command = [sys.executable, "-B", "-c", script, str(root), str(env_id)]
    second = subprocess.run(command, env=env, capture_output=True, text=True, timeout=20, check=False)
    assert second.returncode == 23
    assert "CONFIG_WRITER_LOCKED" in second.stdout
    service.close()
    after = subprocess.run(command, env=env, capture_output=True, text=True, timeout=20, check=False)
    assert after.returncode == 0
    assert after.stdout.strip() == "ACQUIRED"


def test_env_override_requires_ack_and_never_persists_override(repository):
    service, root, env_id = repository
    doc = values(root, env_id)
    env = {"JWT_EXPIRE_MINUTES": "90"}
    with pytest.raises(ValueError, match="OVERRIDE_ACKNOWLEDGEMENT_REQUIRED"):
        service.create_draft(doc, expected_revision=None, process_env=env)
    draft = service.create_draft(doc, expected_revision=None, process_env=env, acknowledge_override=True)
    assert draft.values.business.jwt_expire_minutes == 60
    assert draft.source_metadata["business.jwt_expire_minutes"].effective is False
    assert "90" not in (root / "config/drafts" / f"{draft.revision_id}.json").read_text(encoding="utf-8").split('"jwt_expire_minutes":')[1].split(",")[0]


def test_enabled_ai_cannot_bypass_later_probe_and_index_gate(repository):
    service, root, env_id = repository
    doc = values(root, env_id)
    profile_id, profile_revision = uuid4(), uuid4()
    doc["providers"] = {str(profile_id): {"profile_id": str(profile_id),
        "profile_revision": str(profile_revision), "display_name": "fake",
        "adapter_id": "fake", "model_id": "fake", "enabled": True, "auth_mode": "none"}}
    doc["bindings"] = {"default_llm": {"task_kind": "default_llm", "profile_id": str(profile_id),
                                    "profile_revision": str(profile_revision)}}
    draft = service.create_draft(doc, expected_revision=None)
    service.validate(draft.revision_id, expected_revision=None)
    with pytest.raises(ValueError, match="ACTIVATION_GATE_REQUIRED"):
        service.activate(draft.revision_id, expected_revision=None)
    assert service.read_manifest().active_revision is None


def test_manifest_loss_with_existing_revision_is_not_a_fresh_setup(repository):
    service, root, env_id = repository
    active = activate(service, values(root, env_id))
    path = root / "config/manifest.json"
    path.unlink()  # Only this test-owned tmp_path.
    with pytest.raises(ValueError, match="MANIFEST_MISSING"):
        service.read_manifest()
    with pytest.raises(ValueError, match="MANIFEST_MISSING"):
        service.create_draft(active.values, expected_revision=None)
    assert service.get_revision(active.revision_id).revision_id == active.revision_id


def test_read_only_reader_has_no_write_side_effect(tmp_path):
    root = tmp_path / "unused"
    with ConfigService(root, uuid4(), writable=False) as reader:
        assert reader.read_manifest().active_revision is None
        with pytest.raises(ValueError, match="CONFIG_READ_ONLY"):
            reader.create_draft({}, expected_revision=None)
    assert not root.exists()


def test_reference_to_other_environment_is_rejected(repository):
    service, root, env_id = repository
    doc = values(root, env_id)
    doc["business"]["jwt_secret_ref"]["environment_id"] = str(uuid4())
    with pytest.raises(ValueError, match="ENVIRONMENT_MISMATCH"):
        service.create_draft(doc, expected_revision=None)
    assert service.read_manifest().active_revision is None


def test_pending_head_prevents_stale_activation(repository):
    service, root, env_id = repository
    first = activate(service, values(root, env_id))
    changed = first.values.model_dump(mode="json")
    changed["business"]["jwt_expire_minutes"] = 31
    pending = activate(service, changed, first.revision_id)
    with pytest.raises(ValueError, match="REVISION_CONFLICT"):
        service.create_draft(first.values, expected_revision=first.revision_id)
    assert service.read_manifest().pending_restart_revision == pending.revision_id


def test_activation_override_needs_current_ack(repository):
    service, root, env_id = repository
    doc = values(root, env_id)
    env = {"JWT_EXPIRE_MINUTES": "90"}
    draft = service.create_draft(doc, expected_revision=None, process_env=env,
                                 acknowledge_override=True)
    service.validate(draft.revision_id, expected_revision=None, process_env=env)
    with pytest.raises(ValueError, match="OVERRIDE_ACKNOWLEDGEMENT_REQUIRED"):
        service.activate(draft.revision_id, expected_revision=None, process_env=env)
    assert service.read_manifest().active_revision is None


def test_corrupt_revision_blocks_status_and_writes(repository):
    service, root, env_id = repository
    active = activate(service, values(root, env_id))
    revision = root / "config/revisions" / f"{active.revision_id}.json"
    original = revision.read_bytes()
    revision.write_bytes(b"{damaged")
    with pytest.raises(ValueError, match="CORRUPT_CONFIGURATION"):
        service.read_manifest()
    with pytest.raises(ValueError, match="CORRUPT_CONFIGURATION"):
        service.create_draft(active.values, expected_revision=active.revision_id)
    assert revision.read_bytes() == b"{damaged"
    assert original != revision.read_bytes()


def test_restore_failure_keeps_original_bytes(repository, monkeypatch):
    import backend.app.services.settings.config_service as module
    service, root, env_id = repository
    active = activate(service, values(root, env_id))
    path = root / "config/manifest.json"
    original = path.read_bytes()
    def fail_manifest(src, dst):
        if str(dst).endswith("manifest.json"):
            raise OSError("synthetic-restore-failure")
        return old_replace(src, dst)
    old_replace = module.os.replace
    with monkeypatch.context() as patch:
        patch.setattr(module.os, "replace", fail_manifest)
        with pytest.raises(ValueError, match="CONFIG_IO_ERROR"):
            service.restore_revision(active.revision_id, expected_revision=active.revision_id)
    assert path.read_bytes() == original



def test_unbound_provider_profile_does_not_require_probe(repository):
    service, root, env_id = repository
    doc = values(root, env_id)
    profile_id, profile_revision = uuid4(), uuid4()
    doc["providers"] = {str(profile_id): {"profile_id": str(profile_id),
        "profile_revision": str(profile_revision), "display_name": "unused fake",
        "adapter_id": "fake", "model_id": "fake", "enabled": True, "auth_mode": "none"}}
    active = activate(service, doc)
    assert service.read_manifest().active_revision == active.revision_id
    assert active.activation_validation == {}
