"""TCR-S2-MOUNTED-008: allowlisted mounts, immutable versions and safe failure.

Only synthetic sources and a recording ACL port are used. Original T204/T205
assertions and fakes stay unchanged; no service, real user key or model is used.
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from tests.support import credential_fakes
from tests.support.credential_fakes import (
    PURPOSES,
    FakeFileSecurity,
    assert_no_secret,
    assert_same_files,
    assert_secret_equal,
    error_surfaces,
    files_under,
    make_reference,
    observe_reads,
    required_symbol,
    safe_surfaces,
    state_of,
)

credential_sandbox = credential_fakes.credential_sandbox
secret_bank = credential_fakes.secret_bank
pytestmark = pytest.mark.usefixtures("credential_sandbox")


def open_store(root, env, mapping, acl=None):
    cls = required_symbol("backend.app.services.settings.mounted_secrets", "MountedSecretStore")
    return cls(root=root, environment_id=env, mounts=mapping,
               file_security=acl if acl is not None else FakeFileSecurity())


def source(root, env, value, *, name="synthetic-v1", purpose="jwt"):
    directory = root / "mounts"
    directory.mkdir(exist_ok=True)
    path = directory / name
    path.write_bytes(value.encode("utf-8"))
    ref = make_reference(env, backend="mounted_secret", secret_id=name, purpose=purpose)
    return ref, path, {name: (ref, path)}


@pytest.mark.parametrize("purpose", PURPOSES)
def test_every_purpose_roundtrips_without_copying_plaintext(tmp_path, secret_bank, purpose):
    env = uuid4()
    ref, path, mapping = source(tmp_path, env, secret_bank[purpose], purpose=purpose)
    original = path.read_bytes()
    with open_store(tmp_path, env, mapping) as store:
        value = store.get(ref, ref.version)
        assert isinstance(value, SecretStr)
        assert_secret_equal(value, secret_bank[purpose])
        status = store.status(ref, ref.version)
        assert state_of(status) == "available"
        assert set(json.loads(status.model_dump_json())) == {"credential_ref", "version", "status"}
        assert_no_secret(secret_bank, safe_surfaces(status))
        assert_no_secret(secret_bank, files_under(tmp_path / "credentials"))
        before = files_under(tmp_path / "credentials")
    with open_store(tmp_path, env, mapping) as reopened:
        assert_secret_equal(reopened.get(ref, ref.version), secret_bank[purpose])
        assert_same_files(before, files_under(tmp_path / "credentials"))
    assert path.read_bytes() == original


@pytest.mark.parametrize("operation", ["get", "status"])
@pytest.mark.parametrize("change", ["environment", "backend", "purpose", "credential_id", "zero_id",
                                    "version", "version_argument", "external_id", "unhashable_id"])
def test_full_reference_is_authorized_before_any_source_read(tmp_path, secret_bank, operation, change):
    env = uuid4()
    ref, path, mapping = source(tmp_path, env, secret_bank["jwt"])
    updates = {"environment": {"environment_id": uuid4()}, "backend": {"backend": "dpapi"},
               "purpose": {"purpose": "database"}, "credential_id": {"credential_id": uuid4()},
               "zero_id": {"credential_id": UUID(int=0)}, "version": {"version": uuid4()},
               "version_argument": {}, "external_id": {"external_secret_id": "unregistered"},
               "unhashable_id": {"external_secret_id": ["unregistered"]}}[change]
    bad = ref.model_copy(update=updates)
    version = uuid4() if change == "version_argument" else bad.version
    with open_store(tmp_path, env, mapping) as store:
        before = files_under(tmp_path / "credentials")
        with observe_reads(path) as reads, pytest.raises(RuntimeError) as error:
            getattr(store, operation)(bad, version)
        assert reads == []
        assert_same_files(before, files_under(tmp_path / "credentials"))
        assert_no_secret(secret_bank, error_surfaces(error.value))


@pytest.mark.parametrize("name", ["", ".", "..", "../outside", "/run/secrets/outside", "a/b", "a\\b",
                                 "C:\\outside", "a%2fb", " leading", "trailing\n", "a" * 129])
def test_path_like_or_malformed_server_ids_are_rejected_before_io(tmp_path, secret_bank, name):
    env = uuid4()
    ref, path, _ = source(tmp_path, env, secret_bank["jwt"])
    ref = ref.model_copy(update={"external_secret_id": name})
    with observe_reads(path) as reads, pytest.raises(RuntimeError) as error:
        open_store(tmp_path, env, {name: (ref, path)})
    assert reads == []
    assert not (tmp_path / "credentials").exists()
    assert_no_secret(secret_bank, error_surfaces(error.value))


@pytest.mark.parametrize("change", ["key_mismatch", "wrong_environment", "wrong_backend", "bad_purpose",
                                    "zero_version", "not_reference", "not_pair", "not_path", "relative_path", "parent_path"])
def test_untrusted_mapping_shapes_fail_before_registration(tmp_path, secret_bank, change):
    env = uuid4()
    ref, path, mapping = source(tmp_path, env, secret_bank["jwt"])
    bad = {"key_mismatch": {"other-id": (ref, path)},
           "wrong_environment": {ref.external_secret_id: (ref.model_copy(update={"environment_id": uuid4()}), path)},
           "wrong_backend": {ref.external_secret_id: (ref.model_copy(update={"backend": "dpapi"}), path)},
           "bad_purpose": {ref.external_secret_id: (ref.model_copy(update={"purpose": secret_bank["provider"]}), path)},
           "zero_version": {ref.external_secret_id: (ref.model_copy(update={"version": UUID(int=0)}), path)},
           "not_reference": {ref.external_secret_id: ("raw", path)},
           "not_pair": {ref.external_secret_id: (ref,)},
           "not_path": {ref.external_secret_id: (ref, str(path))},
           "relative_path": {ref.external_secret_id: (ref, Path("relative-input"))},
           "parent_path": {ref.external_secret_id: (ref, path.parent / ".." / "mounts" / path.name)}}[change]
    with observe_reads(path) as reads, pytest.raises(RuntimeError) as error:
        open_store(tmp_path, env, bad)
    assert reads == []
    assert not (tmp_path / "credentials").exists()
    assert_no_secret(secret_bank, error_surfaces(error.value))
    assert mapping[ref.external_secret_id] == (ref, path)


@pytest.mark.parametrize("bad_root", ["relative", "string", "filesystem_root"])
def test_root_must_be_explicit_canonical_environment_directory(tmp_path, bad_root):
    root = Path("relative-root") if bad_root == "relative" else str(tmp_path) if bad_root == "string" else Path(tmp_path.anchor)
    with pytest.raises(RuntimeError):
        open_store(root, uuid4(), {})
    assert not Path("relative-root").exists()


def test_duplicate_source_path_cannot_claim_two_versions(tmp_path, secret_bank):
    env = uuid4()
    first, path, mapping = source(tmp_path, env, secret_bank["jwt"])
    second = make_reference(env, backend="mounted_secret", secret_id="synthetic-v2")
    mapping[second.external_secret_id] = (second, path)
    with observe_reads(path) as reads, pytest.raises(RuntimeError):
        open_store(tmp_path, env, mapping)
    assert reads == []
    assert first.version != second.version


def test_caller_mapping_mutation_cannot_add_runtime_permissions(tmp_path, secret_bank):
    env = uuid4()
    first, _, mapping = source(tmp_path, env, secret_bank["jwt"])
    second, other, second_mapping = source(tmp_path, env, secret_bank["provider"], name="synthetic-v2")
    with open_store(tmp_path, env, mapping) as store:
        mapping.clear()
        mapping.update(second_mapping)
        assert_secret_equal(store.get(first, first.version), secret_bank["jwt"])
        with observe_reads(other) as reads, pytest.raises(RuntimeError):
            store.get(second, second.version)
        assert reads == []


@pytest.mark.parametrize("replacement", ["different_ref", "different_path", "same_path_new_id"])
def test_registered_id_path_and_version_cannot_be_reassigned(tmp_path, secret_bank, replacement):
    env = uuid4()
    ref, path, mapping = source(tmp_path, env, secret_bank["jwt"])
    other = path.with_name("other-input")
    other.write_text(secret_bank["provider"], encoding="utf-8")
    with open_store(tmp_path, env, mapping):
        before = files_under(tmp_path / "credentials")
    new_ref = ref.model_copy(update={"version": uuid4()}) if replacement == "different_ref" else (
        ref.model_copy(update={"external_secret_id": "synthetic-v2", "version": uuid4()}) if replacement == "same_path_new_id" else ref)
    changed = {new_ref.external_secret_id: (new_ref, other if replacement == "different_path" else path)}
    with observe_reads(path) as reads, observe_reads(other) as other_reads, pytest.raises(RuntimeError):
        open_store(tmp_path, env, changed)
    assert reads == other_reads == []
    assert_same_files(before, files_under(tmp_path / "credentials"))
    with open_store(tmp_path, env, mapping) as store:
        assert_secret_equal(store.get(ref, ref.version), secret_bank["jwt"])


@pytest.mark.parametrize("rotation", ["overwrite", "atomic_replace", "delete"])
def test_old_version_is_unavailable_after_rotation_and_restart(tmp_path, secret_bank, rotation):
    env = uuid4()
    ref, path, mapping = source(tmp_path, env, secret_bank["jwt"])
    with open_store(tmp_path, env, mapping) as store:
        assert_secret_equal(store.get(ref, ref.version), secret_bank["jwt"])
        if rotation == "delete":
            path.unlink()
        elif rotation == "atomic_replace":
            other = path.with_name("replacement")
            other.write_text(secret_bank["provider"], encoding="utf-8")
            os.replace(other, path)
        else:
            path.write_text(secret_bank["provider"], encoding="utf-8")
        before = files_under(tmp_path / "credentials")
        with pytest.raises(RuntimeError) as error:
            store.get(ref, ref.version)
        assert error.value.code == "CREDENTIAL_UNAVAILABLE"
        assert_no_secret(secret_bank, error_surfaces(error.value))
        assert state_of(store.status(ref, ref.version)) == "unavailable"
        assert_same_files(before, files_under(tmp_path / "credentials"))
    with open_store(tmp_path, env, mapping) as store:
        with pytest.raises(RuntimeError) as error:
            store.get(ref, ref.version)
        assert error.value.code == "CREDENTIAL_UNAVAILABLE"
        assert_same_files(before, files_under(tmp_path / "credentials"))


def test_new_version_registration_keeps_old_identity_and_bytes(tmp_path, secret_bank):
    env = uuid4()
    first, old_path, mapping = source(tmp_path, env, secret_bank["jwt"])
    with open_store(tmp_path, env, mapping):
        pass
    second, new_path, new_mapping = source(tmp_path, env, secret_bank["provider"], name="synthetic-v2")
    mapping.update(new_mapping)
    old_bytes = old_path.read_bytes()
    with open_store(tmp_path, env, mapping) as store:
        assert_secret_equal(store.get(first, first.version), secret_bank["jwt"])
        assert_secret_equal(store.get(second, second.version), secret_bank["provider"])
        old_path.unlink()
        with pytest.raises(RuntimeError) as error:
            store.get(first, first.version)
        assert error.value.code == "CREDENTIAL_UNAVAILABLE"
        assert_secret_equal(store.get(second, second.version), secret_bank["provider"])
    assert new_path.read_bytes() != old_bytes
    with open_store(tmp_path, env, mapping) as store:
        with pytest.raises(RuntimeError):
            store.get(first, first.version)
        assert_secret_equal(store.get(second, second.version), secret_bank["provider"])


@pytest.mark.parametrize("content", [b"", b" \r\n\t", b"\xff\xfe", b"abc\x00def", b"x" * (1024 * 1024 + 1)],
                         ids=["empty", "blank", "invalid_utf8", "nul", "oversized"])
def test_invalid_source_is_unavailable_without_env_fallback(tmp_path, secret_bank, content):
    env = uuid4()
    ref, path, mapping = source(tmp_path, env, secret_bank["jwt"])
    path.write_bytes(content)
    fallback = tmp_path / ".env"
    fallback.write_text("JWT_SECRET_KEY=" + secret_bank["provider"], encoding="utf-8")
    with observe_reads(fallback) as fallback_reads, open_store(tmp_path, env, mapping) as store:
        assert state_of(store.status(ref, ref.version)) == "unavailable"
        with pytest.raises(RuntimeError) as error:
            store.get(ref, ref.version)
        assert error.value.code == "CREDENTIAL_UNAVAILABLE"
        assert_no_secret(secret_bank, error_surfaces(error.value))
    assert fallback_reads == []
    assert path.read_bytes() == content
    assert_no_secret(secret_bank, files_under(tmp_path / "credentials"))


def test_valid_secret_whitespace_is_not_silently_normalized(tmp_path, secret_bank):
    env = uuid4()
    value = " " + secret_bank["jwt"] + "\r\n"
    ref, _, mapping = source(tmp_path, env, value)
    with open_store(tmp_path, env, mapping) as store:
        assert_secret_equal(store.get(ref, ref.version), value)


@pytest.mark.parametrize("initial_failure", ["missing", "permission"])
def test_unproven_initial_version_is_not_adopted_when_source_later_appears(tmp_path, secret_bank, initial_failure):
    env = uuid4()
    ref, path, mapping = source(tmp_path, env, secret_bank["jwt"])
    if initial_failure == "missing":
        path.unlink()
    with observe_reads(path, failure_text=secret_bank["jwt"] if initial_failure == "permission" else None):
        store = open_store(tmp_path, env, mapping)
    with store:
        path.write_text(secret_bank["provider"], encoding="utf-8")
        with pytest.raises(RuntimeError) as error:
            store.get(ref, ref.version)
        assert error.value.code == "CREDENTIAL_UNAVAILABLE"
    with open_store(tmp_path, env, mapping) as reopened, pytest.raises(RuntimeError):
        reopened.get(ref, ref.version)


@pytest.mark.parametrize("damage", ["missing", "corrupt", "future", "environment"])
def test_lost_or_invalid_private_metadata_is_never_repaired(tmp_path, secret_bank, damage):
    env = uuid4()
    ref, path, mapping = source(tmp_path, env, secret_bank["jwt"])
    with open_store(tmp_path, env, mapping) as store:
        index = store.metadata_root / "index.json"
        if damage == "missing":
            index.unlink()
        elif damage == "corrupt":
            index.write_bytes(b"not-json")
        else:
            data = json.loads(index.read_bytes())
            data["schema_version" if damage == "future" else "environment_id"] = 999 if damage == "future" else str(uuid4())
            index.write_text(json.dumps(data), encoding="utf-8")
        before = files_under(tmp_path / "credentials")
        with observe_reads(path) as reads:
            assert state_of(store.status(ref, ref.version)) == "unavailable"
            with pytest.raises(RuntimeError):
                store.get(ref, ref.version)
        assert reads == []
        assert_same_files(before, files_under(tmp_path / "credentials"))
    with observe_reads(path) as reads, pytest.raises(RuntimeError):
        open_store(tmp_path, env, mapping)
    assert reads == []
    assert_same_files(before, files_under(tmp_path / "credentials"))


def test_environment_root_cannot_be_claimed_by_another_environment(tmp_path, secret_bank):
    env = uuid4()
    ref, path, mapping = source(tmp_path, env, secret_bank["jwt"])
    with open_store(tmp_path, env, mapping):
        before = files_under(tmp_path / "credentials")
    foreign_env = uuid4()
    foreign_ref = ref.model_copy(update={"environment_id": foreign_env})
    with observe_reads(path) as reads, pytest.raises(RuntimeError):
        open_store(tmp_path, foreign_env, {foreign_ref.external_secret_id: (foreign_ref, path)})
    assert reads == []
    assert_same_files(before, files_under(tmp_path / "credentials"))


def test_get_status_are_readonly_even_when_metadata_cannot_be_written(tmp_path, secret_bank, monkeypatch):
    env, acl = uuid4(), FakeFileSecurity()
    ref, _, mapping = source(tmp_path, env, secret_bank["jwt"])
    with open_store(tmp_path, env, mapping, acl) as store:
        before = files_under(tmp_path / "credentials")
        acl.calls.clear()
        def no_write(*_args, **_kwargs):
            raise AssertionError("read-only query attempted a mutation")
        with monkeypatch.context() as patch:
            for function in ("replace", "fsync", "chmod", "mkdir"):
                patch.setattr(os, function, no_write)
            patch.setattr(acl, "secure", no_write)
            assert_secret_equal(store.get(ref, ref.version), secret_bank["jwt"])
            assert state_of(store.status(ref, ref.version)) == "available"
        assert all(operation == "verify" for operation, _, _ in acl.calls)
        assert_same_files(before, files_under(tmp_path / "credentials"))


def test_permission_failure_does_not_leak_or_repair_metadata(tmp_path, secret_bank):
    env, acl = uuid4(), FakeFileSecurity()
    ref, _, mapping = source(tmp_path, env, secret_bank["jwt"])
    with open_store(tmp_path, env, mapping, acl) as store:
        before = files_under(tmp_path / "credentials")
        acl.calls.clear()
        acl.failure_text = secret_bank["jwt"]
        assert state_of(store.status(ref, ref.version)) == "unavailable"
        with pytest.raises(RuntimeError) as error:
            store.get(ref, ref.version)
        assert_no_secret(secret_bank, error_surfaces(error.value))
        assert all(operation == "verify" for operation, _, _ in acl.calls)
        assert_same_files(before, files_under(tmp_path / "credentials"))
        acl.failure_text = None
        assert_secret_equal(store.get(ref, ref.version), secret_bank["jwt"])


@pytest.mark.parametrize("fault", ["fsync", "replace"])
def test_new_version_metadata_failure_preserves_all_prior_bindings(tmp_path, secret_bank, monkeypatch, fault):
    env = uuid4()
    ref, _, mapping = source(tmp_path, env, secret_bank["jwt"])
    with open_store(tmp_path, env, mapping) as store:
        index = store.metadata_root / "index.json"
        before = index.read_bytes()
    _, _, addition = source(tmp_path, env, secret_bank["provider"], name="synthetic-v2")
    changed = {**mapping, **addition}
    def fail(*_args, **_kwargs):
        raise OSError(secret_bank["provider"])
    with monkeypatch.context() as patch:
        patch.setattr(os, fault, fail)
        with pytest.raises(RuntimeError) as error:
            open_store(tmp_path, env, changed)
        assert_no_secret(secret_bank, error_surfaces(error.value))
    assert index.read_bytes() == before
    with open_store(tmp_path, env, mapping) as store:
        assert_secret_equal(store.get(ref, ref.version), secret_bank["jwt"])
    assert_no_secret(secret_bank, files_under(tmp_path / "credentials"))


def test_source_directory_is_not_a_secret(tmp_path, secret_bank):
    env = uuid4()
    ref, path, mapping = source(tmp_path, env, secret_bank["jwt"])
    path.unlink()
    path.mkdir()
    with open_store(tmp_path, env, mapping) as store:
        assert state_of(store.status(ref, ref.version)) == "unavailable"
        with pytest.raises(RuntimeError):
            store.get(ref, ref.version)


def test_source_hardlink_is_rejected_without_reading_external_bytes(tmp_path, secret_bank):
    env = uuid4()
    ref, path, mapping = source(tmp_path, env, secret_bank["jwt"])
    other = path.with_name("unregistered-input")
    os.link(path, other)
    with observe_reads(path) as reads, open_store(tmp_path, env, mapping) as store:
        assert state_of(store.status(ref, ref.version)) == "unavailable"
        with pytest.raises(RuntimeError):
            store.get(ref, ref.version)
    assert reads == []
    assert_no_secret(secret_bank, files_under(tmp_path / "credentials"))


@pytest.mark.parametrize("target", ["source", "parent", "metadata"])
def test_later_reparse_points_are_rejected_before_secret_read(tmp_path, secret_bank, monkeypatch, target):
    env = uuid4()
    ref, path, mapping = source(tmp_path, env, secret_bank["jwt"])
    with open_store(tmp_path, env, mapping) as store:
        before = files_under(tmp_path / "credentials")
        selected = path if target == "source" else path.parent if target == "parent" else store.metadata_root
        original = Path.lstat
        def reparse(candidate):
            actual = original(candidate)
            return SimpleNamespace(st_mode=actual.st_mode, st_nlink=actual.st_nlink,
                                   st_file_attributes=0x400) if candidate == selected else actual
        with monkeypatch.context() as patch, observe_reads(path) as reads:
            patch.setattr(Path, "lstat", reparse)
            assert state_of(store.status(ref, ref.version)) == "unavailable"
            with pytest.raises(RuntimeError):
                store.get(ref, ref.version)
        assert reads == []
        assert_same_files(before, files_under(tmp_path / "credentials"))


def test_writer_lock_and_closed_store_have_explicit_lifetimes(tmp_path, secret_bank):
    env = uuid4()
    ref, _, mapping = source(tmp_path, env, secret_bank["jwt"])
    with open_store(tmp_path, env, mapping) as store:
        with pytest.raises(RuntimeError) as error:
            open_store(tmp_path, env, mapping)
        assert error.value.code == "CREDENTIAL_WRITER_LOCKED"
        store.close()
        with pytest.raises(RuntimeError):
            store.get(ref, ref.version)
    with open_store(tmp_path, env, mapping) as store:
        assert_secret_equal(store.get(ref, ref.version), secret_bank["jwt"])


def test_handoff_lost_metadata_directory_cannot_reenroll_old_version(tmp_path, secret_bank):
    env = uuid4()
    _ref, path, mapping = source(tmp_path, env, secret_bank["jwt"])
    with open_store(tmp_path, env, mapping) as store:
        directory = store.metadata_root
    directory.rename(tmp_path / "retained-metadata")
    path.write_bytes(secret_bank["provider"].encode("utf-8"))
    before = files_under(tmp_path / "credentials")
    with observe_reads(path) as reads, pytest.raises(RuntimeError) as error:
        open_store(tmp_path, env, mapping)
    assert reads == []
    assert not directory.exists()
    assert_same_files(before, files_under(tmp_path / "credentials"))
    assert_no_secret(secret_bank, error_surfaces(error.value))


@pytest.mark.parametrize("damage", ["missing", "corrupt", "foreign"])
def test_handoff_environment_binding_is_required_without_repair(tmp_path, secret_bank, damage):
    env = uuid4()
    ref, path, mapping = source(tmp_path, env, secret_bank["jwt"])
    with open_store(tmp_path, env, mapping) as store:
        binding = tmp_path / "credentials" / "mounted-environment.json"
        if damage == "missing":
            binding.unlink(missing_ok=True)
        else:
            binding.write_bytes(b"not-json" if damage == "corrupt" else json.dumps(
                {"schema_version": 1, "environment_id": str(uuid4())}).encode("utf-8"))
        before = files_under(tmp_path / "credentials")
        with observe_reads(path) as reads:
            assert state_of(store.status(ref, ref.version)) == "unavailable"
            with pytest.raises(RuntimeError) as error:
                store.get(ref, ref.version)
        assert reads == []
        assert_same_files(before, files_under(tmp_path / "credentials"))
        assert_no_secret(secret_bank, error_surfaces(error.value))
    with observe_reads(path) as reads, pytest.raises(RuntimeError):
        open_store(tmp_path, env, mapping)
    assert reads == []
    assert_same_files(before, files_under(tmp_path / "credentials"))


@pytest.mark.parametrize("damage", ["deleted_record", "changed_integrity"])
def test_handoff_index_content_loss_cannot_be_reenrolled(tmp_path, secret_bank, damage):
    env = uuid4()
    ref, path, mapping = source(tmp_path, env, secret_bank["jwt"])
    with open_store(tmp_path, env, mapping) as store:
        index = store.metadata_root / "index.json"
    value = json.loads(index.read_bytes())
    if damage == "deleted_record":
        value["records"].clear()
    else:
        value["records"][ref.external_secret_id]["integrity"] = "0" * 64
    index.write_bytes(json.dumps(value).encode("utf-8"))
    path.write_bytes(secret_bank["provider"].encode("utf-8"))
    before = files_under(tmp_path / "credentials")
    with observe_reads(path) as reads, pytest.raises(RuntimeError) as error:
        open_store(tmp_path, env, mapping)
    assert reads == []
    assert_same_files(before, files_under(tmp_path / "credentials"))
    assert_no_secret(secret_bank, error_surfaces(error.value))


def _stat_with(info, **changes):
    fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
    return SimpleNamespace(**({name: getattr(info, name) for name in fields} | changes))


def test_handoff_cross_interface_ctime_difference_does_not_reject_valid_value(tmp_path, secret_bank, monkeypatch):
    env = uuid4()
    ref, _, mapping = source(tmp_path, env, secret_bank["jwt"])
    with open_store(tmp_path, env, mapping) as store:
        original = os.fstat
        def shifted(descriptor):
            info = original(descriptor)
            return _stat_with(info, st_ctime_ns=info.st_ctime_ns + 1000000)
        with monkeypatch.context() as patch:
            patch.setattr(os, "fstat", shifted)
            assert_secret_equal(store.get(ref, ref.version), secret_bank["jwt"])


@pytest.mark.parametrize("changed_field", ["st_ctime_ns", "st_ino", "st_size", "st_mtime_ns", "st_nlink"])
def test_handoff_source_change_during_read_is_rejected(tmp_path, secret_bank, monkeypatch, changed_field):
    env = uuid4()
    ref, path, mapping = source(tmp_path, env, secret_bank["jwt"])
    with open_store(tmp_path, env, mapping) as store:
        original, inode = os.fstat, path.stat().st_ino
        count = 0
        def changed(descriptor):
            nonlocal count
            info = original(descriptor)
            if info.st_ino == inode:
                count += 1
                if count == 2:
                    return _stat_with(info, **{changed_field: getattr(info, changed_field) + 1})
            return info
        with monkeypatch.context() as patch:
            patch.setattr(os, "fstat", changed)
            with pytest.raises(RuntimeError) as error:
                store.get(ref, ref.version)
        assert error.value.code == "CREDENTIAL_UNAVAILABLE"
        assert_no_secret(secret_bank, error_surfaces(error.value))
