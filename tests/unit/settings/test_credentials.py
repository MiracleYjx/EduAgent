"""T204 / TCR-S2-CREDENTIALS-006: lifecycle through production store ports.

Missing T205/T206 entrypoints remain explicit call-phase failures, not xfail.
No real DPAPI, user secret, database, Redis, or model request is permitted.
"""

from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import SecretStr

from backend.app.schemas.settings import CredentialReference
from backend.app.services.settings.config_service import ConfigService
from tests.support import credential_fakes
from tests.support.credential_fakes import (
    PURPOSES,
    FakeProtector,
    FakeReferenceIndex,
    activate_reference,
    assert_no_secret,
    assert_same_files,
    assert_secret_equal,
    error_surfaces,
    files_under,
    make_reference,
    mounted_store,
    observe_reads,
    protected_files,
    safe_surfaces,
    stage_pending_reference,
    state_of,
    windows_store,
)

credential_sandbox = credential_fakes.credential_sandbox
secret_bank = credential_fakes.secret_bank
pytestmark = pytest.mark.usefixtures("credential_sandbox")


@pytest.mark.parametrize("empty", ["", " ", "\r\n\t"], ids=["empty", "space", "whitespace"])
def test_empty_write_is_not_keep_or_delete(tmp_path, empty):
    protector, index = FakeProtector(), FakeReferenceIndex()
    with windows_store(tmp_path, uuid4(), protector, index) as store:
        before = files_under(tmp_path / "credentials")
        with pytest.raises((ValueError, RuntimeError)):
            store.put(purpose="jwt", secret_value=SecretStr(empty))
        assert protector.protect_calls == 0
        assert_same_files(before, files_under(tmp_path / "credentials"))


@pytest.mark.parametrize("purpose", PURPOSES)
def test_all_purposes_roundtrip_as_opaque_reference(tmp_path, secret_bank, purpose):
    env = uuid4()
    protector, index = FakeProtector(), FakeReferenceIndex()
    with windows_store(tmp_path, env, protector, index) as store:
        ref = store.put(purpose=purpose, secret_value=SecretStr(secret_bank[purpose]))
        assert isinstance(ref, CredentialReference)
        assert ref.environment_id == env and ref.backend == "dpapi" and ref.purpose == purpose
        assert ref.credential_id.int != 0 and ref.version.int != 0
        assert protector.protect_calls == 1
        assert_secret_equal(store.get(ref, ref.version), secret_bank[purpose])
        status = store.status(ref, ref.version)
        assert state_of(status) == "available"
        assert_no_secret(secret_bank, safe_surfaces(status))
        persisted = files_under(tmp_path / "credentials")
        assert persisted, "put must persist a protected credential, not only cache it"
        assert_no_secret(secret_bank, persisted)
    # Same fake user protector, a fresh store object; not a real DPAPI restart.
    with windows_store(tmp_path, env, protector, index) as restarted:
        assert_secret_equal(restarted.get(ref, ref.version), secret_bank[purpose])


def test_missing_is_absent_without_decryption_or_fallback(tmp_path, secret_bank):
    env = uuid4()
    protector = FakeProtector()
    fallback = tmp_path / ".env"
    fallback.write_text("JWT_SECRET_KEY=" + secret_bank["jwt"], encoding="utf-8")
    with windows_store(tmp_path, env, protector, FakeReferenceIndex()) as store:
        ref = make_reference(env)
        assert state_of(store.status(ref, ref.version)) == "absent"
        with observe_reads(fallback) as reads, pytest.raises((ValueError, RuntimeError)) as error:
            store.get(ref, ref.version)
        assert reads == []
        assert protector.unprotect_calls == 0
        assert_no_secret(secret_bank, error_surfaces(error.value))


@pytest.mark.parametrize("reason", ["corrupt_ciphertext", "other_user", "profile_reset"])
def test_decryption_failure_requires_reentry_and_preserves_ciphertext(tmp_path, secret_bank, reason):
    env = uuid4()
    protector = FakeProtector(failure_text=secret_bank["jwt"] + ":" + reason)
    with windows_store(tmp_path, env, protector, FakeReferenceIndex()) as store:
        ref = store.put(purpose="jwt", secret_value=SecretStr(secret_bank["jwt"]))
        before = files_under(tmp_path / "credentials")
        fallback = tmp_path / ".env"
        fallback.write_text("JWT_SECRET_KEY=" + secret_bank["provider"], encoding="utf-8")
        protector.unprotect_error = True  # Simulated OS failure; no real DPAPI call.
        assert state_of(store.status(ref, ref.version)) == "reentry_required"
        with observe_reads(fallback) as reads, pytest.raises((ValueError, RuntimeError)) as error:
            store.get(ref, ref.version)
        assert reads == []
        assert_no_secret(secret_bank, error_surfaces(error.value))
        assert_same_files(before, files_under(tmp_path / "credentials"))
        assert_secret_equal(fallback.read_text(encoding="utf-8"), "JWT_SECRET_KEY=" + secret_bank["provider"])


def test_protector_unavailable_never_creates_plaintext_fallback(tmp_path, secret_bank):
    protector = FakeProtector(failure_text=secret_bank["jwt"], protect_error=True)
    with windows_store(tmp_path, uuid4(), protector, FakeReferenceIndex()) as store:
        before = files_under(tmp_path)
        with pytest.raises((ValueError, RuntimeError)) as error:
            store.put(purpose="jwt", secret_value=SecretStr(secret_bank["jwt"]))
        assert_no_secret(secret_bank, error_surfaces(error.value))
        assert_same_files(before, files_under(tmp_path))


def test_new_candidate_version_does_not_change_active_or_pending(tmp_path, secret_bank):
    env = uuid4()
    protector = FakeProtector()
    with windows_store(tmp_path, env, protector, FakeReferenceIndex()) as store:
        old = store.put(purpose="jwt", secret_value=SecretStr(secret_bank["jwt"]))
        with ConfigService(tmp_path, env) as config:
            active = activate_reference(config, old)
            pending = stage_pending_reference(config, active)
            before = files_under(tmp_path / "config")
            new = store.put(purpose="jwt", secret_value=SecretStr(secret_bank["provider"]), credential_id=old.credential_id)
            assert new.credential_id == old.credential_id and new.version != old.version
            assert_secret_equal(store.get(old, old.version), secret_bank["jwt"])
            assert_secret_equal(store.get(new, new.version), secret_bank["provider"])
            assert config.active_revision().revision_id == active.revision_id
            assert config.active_revision().values.business.jwt_secret_ref == old
            assert config.read_manifest().pending_restart_revision == pending.revision_id
            assert config.get_revision(pending.revision_id).values.business.jwt_secret_ref == old
            assert_same_files(before, files_under(tmp_path / "config"))


@pytest.mark.parametrize("fault", ["protect", "fsync", "replace"])
def test_replacement_failure_preserves_old_active_and_old_bytes(tmp_path, secret_bank, monkeypatch, fault):
    import os

    env = uuid4()
    protector = FakeProtector(failure_text=secret_bank["provider"])
    with windows_store(tmp_path, env, protector, FakeReferenceIndex()) as store:
        old = store.put(purpose="jwt", secret_value=SecretStr(secret_bank["jwt"]))
        old_files = protected_files(tmp_path / "credentials", protector)
        assert old_files, "old ciphertext must be persisted before fault injection"
        with ConfigService(tmp_path, env) as config:
            active = activate_reference(config, old)
            pending = stage_pending_reference(config, active)
            config_before = files_under(tmp_path / "config")
            def fail(*_args, **_kwargs):
                raise OSError(secret_bank["provider"])
            with monkeypatch.context() as patch:
                if fault == "protect":
                    protector.protect_error = True
                else:
                    patch.setattr(os, fault, fail)
                with pytest.raises((ValueError, RuntimeError)) as error:
                    store.put(purpose="jwt", secret_value=SecretStr(secret_bank["provider"]), credential_id=old.credential_id)
                assert_no_secret(secret_bank, error_surfaces(error.value))
            protector.protect_error = False
            # Orphan candidates may remain, but every previously persisted byte survives.
            now = files_under(tmp_path / "credentials")
            assert_same_files(old_files, {name: now.get(name) for name in old_files})
            assert_secret_equal(store.get(old, old.version), secret_bank["jwt"])
            assert config.active_revision().values.business.jwt_secret_ref == old
            assert config.read_manifest().pending_restart_revision == pending.revision_id
            assert_same_files(config_before, files_under(tmp_path / "config"))


@pytest.mark.parametrize("use", ["active", "pending_restart", "nonterminal_snapshot"])
def test_referenced_version_delete_returns_409_before_any_mutation(tmp_path, secret_bank, use):
    index = FakeReferenceIndex(uses=(use,))
    with windows_store(tmp_path, uuid4(), FakeProtector(), index) as store:
        ref = store.put(purpose="jwt", secret_value=SecretStr(secret_bank["jwt"]))
        before = files_under(tmp_path / "credentials")
        with pytest.raises((ValueError, RuntimeError)) as error:
            store.delete(ref, ref.version)
        assert error.value.code == "IN_USE" and error.value.status_code == 409
        assert (ref.credential_id, ref.version) in index.calls
        assert_same_files(before, files_under(tmp_path / "credentials"))
        assert_secret_equal(store.get(ref, ref.version), secret_bank["jwt"])


def test_reference_lookup_failure_is_fail_closed(tmp_path, secret_bank):
    index = FakeReferenceIndex(failure_text=secret_bank["jwt"])
    with windows_store(tmp_path, uuid4(), FakeProtector(), index) as store:
        ref = store.put(purpose="jwt", secret_value=SecretStr(secret_bank["jwt"]))
        before = files_under(tmp_path / "credentials")
        with pytest.raises((ValueError, RuntimeError)) as error:
            store.delete(ref, ref.version)
        assert_no_secret(secret_bank, error_surfaces(error.value))
        assert_same_files(before, files_under(tmp_path / "credentials"))
        assert state_of(store.status(ref, ref.version)) == "available"


@pytest.mark.parametrize("uses", [(), ("terminal_history",)], ids=["unreferenced", "completed_history"])
def test_explicit_delete_removes_unreferenced_ciphertext(tmp_path, secret_bank, uses):
    protector = FakeProtector()
    with windows_store(tmp_path, uuid4(), protector, FakeReferenceIndex(uses=uses)) as store:
        ref = store.put(purpose="jwt", secret_value=SecretStr(secret_bank["jwt"]))
        before = files_under(tmp_path / "credentials")
        assert any(blob in content for blob in protector.blobs for content in before.values())
        store.delete(ref, ref.version)
        after = files_under(tmp_path / "credentials")
        assert not any(blob in content for blob in protector.blobs for content in after.values())
        assert state_of(store.status(ref, ref.version)) in {"absent", "revoked"}
        with pytest.raises((ValueError, RuntimeError)):
            store.get(ref, ref.version)


def test_physical_delete_failure_does_not_advance_status(tmp_path, secret_bank, monkeypatch):
    protector = FakeProtector()
    with windows_store(tmp_path, uuid4(), protector, FakeReferenceIndex()) as store:
        ref = store.put(purpose="jwt", secret_value=SecretStr(secret_bank["jwt"]))
        before = protected_files(tmp_path / "credentials", protector)
        assert before, "old ciphertext must be persisted before fault injection"
        original = Path.unlink
        def fail_owned(path, *args, **kwargs):
            if path.is_relative_to(tmp_path / "credentials"):
                raise OSError(secret_bank["jwt"])
            return original(path, *args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(Path, "unlink", fail_owned)
            with pytest.raises((ValueError, RuntimeError)) as error:
                store.delete(ref, ref.version)
            assert_no_secret(secret_bank, error_surfaces(error.value))
        assert state_of(store.status(ref, ref.version)) == "available"
        assert_secret_equal(store.get(ref, ref.version), secret_bank["jwt"])
        assert_same_files(before, protected_files(tmp_path / "credentials", protector))


@pytest.mark.parametrize("operation", ["get", "status", "delete"])
def test_cross_environment_reference_is_rejected_before_decrypt(tmp_path, secret_bank, operation):
    protector = FakeProtector()
    with windows_store(tmp_path, uuid4(), protector, FakeReferenceIndex()) as store:
        ref = store.put(purpose="jwt", secret_value=SecretStr(secret_bank["jwt"]))
        foreign = ref.model_copy(update={"environment_id": uuid4()})
        before, decryptions = files_under(tmp_path / "credentials"), protector.unprotect_calls
        with pytest.raises((ValueError, RuntimeError)) as error:
            getattr(store, operation)(foreign, foreign.version)
        assert protector.unprotect_calls == decryptions
        assert_no_secret(secret_bank, error_surfaces(error.value))
        assert_same_files(before, files_under(tmp_path / "credentials"))


def test_mounted_v2_does_not_silently_rebind_v1(tmp_path, secret_bank):
    env = uuid4()
    v1 = make_reference(env, backend="mounted_secret", secret_id="synthetic-jwt-v1")
    v2 = make_reference(env, backend="mounted_secret", secret_id="synthetic-jwt-v2")
    mounts = tmp_path / "mounts"
    mounts.mkdir()
    first, second = mounts / "v1", mounts / "v2"
    first.write_text(secret_bank["jwt"], encoding="utf-8")
    second.write_text(secret_bank["provider"], encoding="utf-8")
    mapping = {v1.external_secret_id: (v1, first), v2.external_secret_id: (v2, second)}
    with mounted_store(tmp_path, env, mapping) as store:
        assert_secret_equal(store.get(v1, v1.version), secret_bank["jwt"])
        assert_secret_equal(store.get(v2, v2.version), secret_bank["provider"])
        first.unlink()  # Only this test-owned synthetic source.
        with pytest.raises((ValueError, RuntimeError)) as error:
            store.get(v1, v1.version)
        assert error.value.code == "CREDENTIAL_UNAVAILABLE"
        assert state_of(store.status(v1, v1.version)) == "unavailable"
        assert_secret_equal(store.get(v2, v2.version), secret_bank["provider"])
        assert_no_secret(secret_bank, error_surfaces(error.value))
