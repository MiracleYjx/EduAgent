"""TCR-S2-DPAPI-007: production storage effects, no native crypto or services."""

import os
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from tests.support import credential_fakes
from tests.support.credential_fakes import (
    FakeFileSecurity,
    FakeProtector,
    FakeReferenceIndex,
    assert_no_secret,
    assert_same_files,
    assert_secret_equal,
    error_surfaces,
    files_under,
    required_symbol,
    state_of,
)

credential_sandbox = credential_fakes.credential_sandbox
secret_bank = credential_fakes.secret_bank
pytestmark = pytest.mark.usefixtures("credential_sandbox")


@pytest.fixture
def store_class():
    return required_symbol("backend.app.services.settings.credential_store", "CredentialStore")


@pytest.fixture
def vault(tmp_path, store_class):
    protector, acl, index = FakeProtector(), FakeFileSecurity(), FakeReferenceIndex()
    with store_class(root=tmp_path, environment_id=uuid4(), protector=protector,
                     file_security=acl, reference_index=index) as store:
        yield store, protector, acl, index


def write(store, secret):
    return store.put(purpose="jwt", secret_value=SecretStr(secret))


def blob_path(root, protector):
    return next(path for path in root.rglob("*") if path.is_file() and path.name != "writer.lock"
                and path.read_bytes() in protector.blobs)


def test_no_reference_index_never_assumes_unused(tmp_path, store_class, secret_bank):
    with store_class(root=tmp_path, environment_id=uuid4(), protector=FakeProtector(),
                     file_security=FakeFileSecurity()) as store:
        ref = write(store, secret_bank["jwt"])
        before = files_under(tmp_path / "credentials")
        with pytest.raises(RuntimeError) as error:
            store.delete(ref, ref.version)
        assert error.value.code == "REFERENCE_INDEX_UNAVAILABLE"
        assert_same_files(before, files_under(tmp_path / "credentials"))
        assert_secret_equal(store.get(ref, ref.version), secret_bank["jwt"])


@pytest.mark.parametrize("uses", [("unknown_kind",), ("terminal_history", "active"), "", None])
def test_unknown_or_malformed_reference_facts_fail_closed(vault, secret_bank, uses):
    store, _, _, index = vault
    ref = write(store, secret_bank["jwt"])
    index.uses = uses
    before = files_under(store.root / "credentials")
    with pytest.raises(RuntimeError):
        store.delete(ref, ref.version)
    assert_same_files(before, files_under(store.root / "credentials"))


@pytest.mark.parametrize("operation", ["get", "status", "delete"])
@pytest.mark.parametrize("change", ["backend", "version_argument", "purpose", "zero_id"])
def test_invalid_reference_is_safe_and_never_decrypts_or_mutates(vault, secret_bank, operation, change):
    store, protector, _, _ = vault
    ref = write(store, secret_bank["jwt"])
    updates = {"backend": {"backend": "mounted_secret"}, "purpose": {"purpose": secret_bank["provider"]},
               "zero_id": {"credential_id": UUID(int=0)}, "version_argument": {}}[change]
    bad = ref.model_copy(update=updates)
    version = uuid4() if change == "version_argument" else bad.version
    before, decryptions = files_under(store.root / "credentials"), protector.unprotect_calls
    with pytest.raises(RuntimeError) as error:
        getattr(store, operation)(bad, version)
    assert protector.unprotect_calls == decryptions
    assert_same_files(before, files_under(store.root / "credentials"))
    assert_no_secret(secret_bank, error_surfaces(error.value))


@pytest.mark.parametrize("bad", ["raw_string", "blank", "invalid_purpose"])
def test_input_rejected_before_crypto(vault, secret_bank, bad):
    store, protector, _, _ = vault
    before = files_under(store.root / "credentials")
    with pytest.raises(RuntimeError) as error:
        store.put(purpose=secret_bank["provider"] if bad == "invalid_purpose" else "jwt",
                  secret_value=secret_bank["jwt"] if bad == "raw_string" else SecretStr(" " if bad == "blank" else secret_bank["jwt"]))
    assert protector.protect_calls == 0
    assert_same_files(before, files_under(store.root / "credentials"))
    assert_no_secret(secret_bank, error_surfaces(error.value))


def test_ciphertext_substitution_cannot_change_reference_identity(vault, secret_bank):
    store, protector, _, _ = vault
    first = write(store, secret_bank["jwt"])
    first_path = blob_path(store.root / "credentials", protector)
    second = write(store, secret_bank["provider"])
    second_path = next(path for path in (store.root / "credentials").rglob("*")
                       if path.is_file() and path != first_path and path.name != "writer.lock"
                       and path.read_bytes() in protector.blobs)
    first_path.write_bytes(second_path.read_bytes())
    assert state_of(store.status(first, first.version)) == "reentry_required"
    with pytest.raises(RuntimeError) as error:
        store.get(first, first.version)
    assert_no_secret(secret_bank, error_surfaces(error.value))
    assert_secret_equal(store.get(second, second.version), secret_bank["provider"])


def test_same_environment_id_cannot_be_reassigned_to_existing_root(tmp_path, store_class, secret_bank):
    protector, acl = FakeProtector(), FakeFileSecurity()
    env = uuid4()
    with store_class(root=tmp_path, environment_id=env, protector=protector, file_security=acl) as store:
        ref = write(store, secret_bank["jwt"])
    before = files_under(tmp_path / "credentials")
    with pytest.raises(RuntimeError) as error:
        store_class(root=tmp_path, environment_id=uuid4(), protector=protector, file_security=acl)
    assert error.value.code == "ENVIRONMENT_MISMATCH"
    assert_same_files(before, files_under(tmp_path / "credentials"))
    with store_class(root=tmp_path, environment_id=env, protector=protector, file_security=acl) as store:
        assert_secret_equal(store.get(ref, ref.version), secret_bank["jwt"])


def test_writer_lock_is_exclusive_and_released(tmp_path, store_class):
    env, protector, acl = uuid4(), FakeProtector(), FakeFileSecurity()
    with store_class(root=tmp_path, environment_id=env, protector=protector, file_security=acl):
        with pytest.raises(RuntimeError) as error:
            store_class(root=tmp_path, environment_id=env, protector=protector, file_security=acl)
        assert error.value.code == "CREDENTIAL_WRITER_LOCKED"
    with store_class(root=tmp_path, environment_id=env, protector=protector, file_security=acl):
        pass


def test_closed_store_does_not_write(vault, secret_bank):
    store, protector, _, _ = vault
    store.close()
    before = files_under(store.root / "credentials")
    with pytest.raises(RuntimeError):
        write(store, secret_bank["jwt"])
    assert protector.protect_calls == 0
    assert_same_files(before, files_under(store.root / "credentials"))


def test_acl_failure_before_ciphertext_write_is_safe(vault, secret_bank):
    store, protector, acl, _ = vault
    ref = write(store, secret_bank["jwt"])
    before = files_under(store.root / "credentials")
    acl.failure_text = secret_bank["provider"]
    with pytest.raises(RuntimeError) as error:
        store.put(purpose="jwt", secret_value=SecretStr(secret_bank["provider"]), credential_id=ref.credential_id)
    assert_no_secret(secret_bank, error_surfaces(error.value))
    assert_same_files(before, files_under(store.root / "credentials"))
    assert protector.protect_calls == 1
    acl.failure_text = None
    assert_secret_equal(store.get(ref, ref.version), secret_bank["jwt"])


def test_io_failure_is_unavailable_not_absent_or_reentry(vault, secret_bank, monkeypatch):
    store, protector, _, _ = vault
    ref = write(store, secret_bank["jwt"])
    target = blob_path(store.root / "credentials", protector)
    original = Path.read_bytes

    def fail(path):
        if path == target:
            raise PermissionError(secret_bank["jwt"])
        return original(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", fail)
        assert state_of(store.status(ref, ref.version)) == "unavailable"
        with pytest.raises(RuntimeError) as error:
            store.get(ref, ref.version)
        assert_no_secret(secret_bank, error_surfaces(error.value))


def test_atomic_write_flushes_private_candidate_before_rename(vault, secret_bank, monkeypatch):
    store, protector, acl, _ = vault
    events = []
    original_secure, original_fsync, original_replace = acl.secure, os.fsync, os.replace

    def secure(path, *, directory):
        events.append(("secure", Path(path)))
        original_secure(path, directory=directory)

    def fsync(fd):
        events.append(("fsync", None))
        original_fsync(fd)

    def replace(source, destination):
        source, destination = Path(source), Path(destination)
        assert source.parent == destination.parent == store.root / "credentials"
        assert not destination.exists()
        assert source.read_bytes() in protector.blobs
        assert ("secure", source) in events
        assert events[-1][0] == "fsync"
        events.append(("replace", destination))
        original_replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(acl, "secure", secure)
        patch.setattr(os, "fsync", fsync)
        patch.setattr(os, "replace", replace)
        ref = write(store, secret_bank["jwt"])
    assert events[-1][0] == "replace"
    assert_secret_equal(store.get(ref, ref.version), secret_bank["jwt"])


def test_hardlinked_ciphertext_rejected_without_touching_external_file(vault, secret_bank):
    store, protector, _, _ = vault
    ref = write(store, secret_bank["jwt"])
    target = blob_path(store.root / "credentials", protector)
    outside = store.root / "owned-outside-vault"
    outside.write_bytes(b"nonsecret-sentinel")
    target.unlink()
    os.link(outside, target)
    before = outside.read_bytes()
    with pytest.raises(RuntimeError):
        store.delete(ref, ref.version)
    assert outside.read_bytes() == before


def test_relative_root_is_rejected_before_creation(store_class):
    with pytest.raises(RuntimeError):
        store_class(root=Path("unapproved-relative"), environment_id=uuid4(), protector=FakeProtector(),
                    file_security=FakeFileSecurity())
    assert not Path("unapproved-relative").exists()


def test_decrypt_success_with_changed_secret_bytes_still_requires_reentry(vault, secret_bank):
    store, protector, _, _ = vault
    ref = write(store, secret_bank["jwt"])
    path = blob_path(store.credentials, protector)
    blob = path.read_bytes()
    protector.blobs[blob] = protector.blobs[blob].replace(secret_bank["jwt"].encode(), secret_bank["provider"].encode())
    assert state_of(store.status(ref, ref.version)) == "reentry_required"
    with pytest.raises(RuntimeError) as error:
        store.get(ref, ref.version)
    assert_no_secret(secret_bank, error_surfaces(error.value))
    assert path.read_bytes() == blob


@pytest.mark.parametrize("damage", ["missing", "corrupt", "future"])
def test_reads_and_writes_never_repair_environment_binding(vault, secret_bank, damage):
    store, _, _, _ = vault
    ref = write(store, secret_bank["jwt"])
    metadata = store.credentials / "environment.json"
    if damage == "missing":
        metadata.unlink()
    else:
        metadata.write_bytes(b"not-json" if damage == "corrupt" else b'{"schema_version":999}')
    before = files_under(store.credentials)
    assert state_of(store.status(ref, ref.version)) == "unavailable"
    with pytest.raises(RuntimeError):
        store.get(ref, ref.version)
    with pytest.raises(RuntimeError):
        write(store, secret_bank["provider"])
    assert_same_files(before, files_under(store.credentials))


def test_version_collision_never_overwrites_previous_ciphertext(vault, secret_bank, monkeypatch):
    import backend.app.services.settings.credential_store as implementation

    store, _, _, _ = vault
    ref = write(store, secret_bank["jwt"])
    before = files_under(store.credentials)
    with monkeypatch.context() as patch:
        patch.setattr(implementation, "uuid4", lambda: ref.version)
        with pytest.raises(RuntimeError) as error:
            store.put(purpose="jwt", secret_value=SecretStr(secret_bank["provider"]), credential_id=ref.credential_id)
    assert error.value.code == "IMMUTABLE_CREDENTIAL_EXISTS"
    assert_same_files(before, files_under(store.credentials))
    assert_secret_equal(store.get(ref, ref.version), secret_bank["jwt"])


def test_read_acl_failure_projects_unavailable_without_repair(vault, secret_bank):
    store, _, acl, _ = vault
    ref = write(store, secret_bank["jwt"])
    before = files_under(store.credentials)
    acl.calls.clear()
    acl.failure_text = secret_bank["jwt"]
    assert state_of(store.status(ref, ref.version)) == "unavailable"
    with pytest.raises(RuntimeError) as error:
        store.get(ref, ref.version)
    assert_no_secret(secret_bank, error_surfaces(error.value))
    assert all(operation == "verify" for operation, _, _ in acl.calls)
    assert_same_files(before, files_under(store.credentials))
    acl.failure_text = None


def test_later_reparse_directory_rejected_before_crypto_or_write(vault, secret_bank, monkeypatch):
    store, protector, _, _ = vault
    ref = write(store, secret_bank["jwt"])
    before, calls = files_under(store.credentials), protector.protect_calls
    original = Path.lstat

    def reparse(path):
        if path == store.credentials:
            actual = original(path)
            return SimpleNamespace(st_mode=actual.st_mode, st_nlink=actual.st_nlink, st_file_attributes=0x400)
        return original(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", reparse)
        with pytest.raises(RuntimeError):
            store.put(purpose="jwt", secret_value=SecretStr(secret_bank["provider"]), credential_id=ref.credential_id)
    assert protector.protect_calls == calls
    assert_same_files(before, files_under(store.credentials))
