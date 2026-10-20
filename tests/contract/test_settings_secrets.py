"""T204 / TCR-S2-CREDENTIALS-006: safe DTOs, errors and mounted-secret policy.

These are service/serialization contracts, not HTTP/browser/backup acceptance.
The eight scanner controls deliberately contain secrets and are not app outputs.
"""

import json
import logging
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError

from backend.app.core.config import resolve_configuration
from backend.app.services.settings.config_service import (
    ConfigService,
    ConfigServiceError,
)
from tests.support import credential_fakes
from tests.support.credential_fakes import (
    PURPOSES,
    activate_reference,
    assert_no_secret,
    assert_same_files,
    assert_secret_equal,
    error_surfaces,
    files_under,
    make_reference,
    mounted_store,
    observe_reads,
    required_symbol,
    safe_surfaces,
    state_of,
)

credential_sandbox = credential_fakes.credential_sandbox
secret_bank = credential_fakes.secret_bank
pytestmark = pytest.mark.usefixtures("credential_sandbox")


@pytest.mark.parametrize("purpose", PURPOSES)
def test_ordinary_configuration_rejects_plaintext_without_changing_active(tmp_path, secret_bank, caplog, purpose):
    env = uuid4()
    with ConfigService(tmp_path, env) as config:
        active = activate_reference(config, make_reference(env))
        before = files_under(tmp_path / "config")
        changed = active.values.model_dump(mode="json")
        if purpose == "provider":
            profile_id = uuid4()
            changed["providers"][str(profile_id)] = {
                "profile_id": str(profile_id), "profile_revision": str(uuid4()), "display_name": "synthetic",
                "adapter_id": "fake", "model_id": "fake", "api_key": secret_bank[purpose],
            }
        else:
            name = {"database": "database_url", "redis": "redis_url", "jwt": "jwt_secret_key"}[purpose]
            changed["business"][name] = secret_bank[purpose]
        with pytest.raises(ConfigServiceError) as error:
            config.create_draft(changed, expected_revision=active.revision_id, process_env={})
        assert error.value.code == "INVALID_CONFIGURATION" and error.value.stage == "schema"
        logging.getLogger("t204.config").warning("%s", error.value)
        assert_no_secret(secret_bank, {**error_surfaces(error.value), "logs": caplog.text})
        assert_same_files(before, files_under(tmp_path / "config"))
        assert config.read_manifest().active_revision == active.revision_id
        assert config.read_manifest().pending_restart_revision is None


def test_secret_as_unknown_field_name_does_not_escape_safe_error(tmp_path, secret_bank, caplog):
    with ConfigService(tmp_path, uuid4()) as config:
        before = files_under(tmp_path / "config")
        with pytest.raises(ConfigServiceError) as error:
            config.create_draft({secret_bank["provider"]: secret_bank["jwt"]}, expected_revision=None, process_env={})
        logging.getLogger("t204.config").warning("%s", error.value)
        assert_no_secret(secret_bank, {**error_surfaces(error.value), "logs": caplog.text})
        assert_same_files(before, files_under(tmp_path / "config"))


@pytest.mark.parametrize("purpose", PURPOSES)
def test_source_error_view_never_contains_high_priority_secret(secret_bank, purpose):
    resolved = resolve_configuration(saved={"business": {"jwt_expire_minutes": 60}},
                                     process_env={"JWT_EXPIRE_MINUTES": secret_bank[purpose]})
    assert "business" not in resolved.segments
    assert resolved.errors and resolved.errors[0].fields == ("business.jwt_expire_minutes",)
    assert_no_secret(secret_bank, safe_surfaces(resolved))


@pytest.mark.parametrize("backend", ["dpapi", "mounted_secret", "process_env"])
def test_nonsecret_revision_response_and_persistence_contain_only_references(tmp_path, secret_bank, backend):
    env = uuid4()
    ref = make_reference(env, backend=backend, secret_id="synthetic-jwt-v1" if backend == "mounted_secret" else None)
    with ConfigService(tmp_path, env) as config:
        active = activate_reference(config, ref)
        public = config.resolve(process_env={})
        assert active.values.business.jwt_secret_ref == ref
        assert public.segments["business"].jwt_secret_ref == ref
        assert_no_secret(secret_bank, safe_surfaces(public))
        assert_no_secret(secret_bank, safe_surfaces(active))
        assert_no_secret(secret_bank, files_under(tmp_path / "config"))
        restored = config.get_revision(active.revision_id)
        assert restored.values.business.jwt_secret_ref == ref
    # This only tests nonsecret configuration persistence, not business backup.


def test_secret_write_is_secretstr_write_only_and_log_safe(secret_bank, caplog):
    cls = required_symbol("backend.app.schemas.settings", "SecretWrite")
    model = cls.model_validate({"purpose": "jwt", "secret_value": secret_bank["jwt"]})
    assert isinstance(model.secret_value, SecretStr)
    assert_secret_equal(model.secret_value, secret_bank["jwt"])
    assert cls.model_json_schema()["properties"]["secret_value"]["writeOnly"] is True
    logging.getLogger("t204.secret-write").info("%r", model)
    assert_no_secret(secret_bank, {**safe_surfaces(model), "logs": caplog.text})


@pytest.mark.parametrize("empty", ["", " ", "\r\n\t"], ids=["empty", "space", "whitespace"])
def test_secret_write_rejects_blank_with_safe_error(empty, secret_bank):
    cls = required_symbol("backend.app.schemas.settings", "SecretWrite")
    with pytest.raises(ValidationError) as error:
        cls.model_validate({"purpose": "jwt", "secret_value": empty})
    assert_no_secret(secret_bank, {"error": str(error.value), "repr": repr(error.value)})


@pytest.mark.parametrize("status", ["absent", "available", "invalid", "unavailable", "revoked", "reentry_required"])
def test_public_status_preserves_distinct_state_without_secret_or_integrity_data(status, secret_bank):
    cls = required_symbol("backend.app.schemas.settings", "CredentialStatus")
    ref = make_reference(uuid4())
    # Public write/status envelope from settings-api §2; not private metadata.
    view = cls.model_validate({"credential_ref": str(ref.credential_id), "version": str(ref.version), "status": status})
    data = json.loads(view.model_dump_json())
    assert data["status"] == status
    assert data["credential_ref"] == str(ref.credential_id) and data["version"] == str(ref.version)
    forbidden = {"secret_value", "plaintext", "ciphertext", "fingerprint", "digest", "last_four", "suffix"}
    assert not (set(data) & forbidden)
    assert_no_secret(secret_bank, safe_surfaces(view))


@pytest.mark.parametrize("operation", ["put", "delete"])
def test_docker_mutations_are_externally_managed_and_source_unchanged(tmp_path, secret_bank, operation):
    env = uuid4()
    ref = make_reference(env, backend="mounted_secret", secret_id="synthetic-jwt-v1")
    source = tmp_path / "mounted-input"
    source.write_text(secret_bank["jwt"], encoding="utf-8")
    with mounted_store(tmp_path, env, {ref.external_secret_id: (ref, source)}) as store:
        with pytest.raises((ValueError, RuntimeError)) as error:
            if operation == "put":
                store.put(purpose="jwt", secret_value=SecretStr(secret_bank["provider"]))
            else:
                store.delete(ref, ref.version)
        assert error.value.code == "EXTERNALLY_MANAGED" and error.value.status_code == 405
        assert_secret_equal(source.read_text(encoding="utf-8"), secret_bank["jwt"])
        assert_no_secret(secret_bank, error_surfaces(error.value))


@pytest.mark.parametrize("failure", ["missing", "permission"])
def test_missing_or_unreadable_mount_is_unavailable(tmp_path, secret_bank, failure):
    env = uuid4()
    ref = make_reference(env, backend="mounted_secret", secret_id="synthetic-jwt-v1")
    source = tmp_path / "mounted-input"
    if failure == "permission":
        source.write_text(secret_bank["jwt"], encoding="utf-8")
    with (
        observe_reads(source, failure_text=secret_bank["jwt"] if failure == "permission" else None),
        mounted_store(tmp_path, env, {ref.external_secret_id: (ref, source)}) as store,
    ):
        assert state_of(store.status(ref, ref.version)) == "unavailable"
        with pytest.raises((ValueError, RuntimeError)) as error:
            store.get(ref, ref.version)
        assert error.value.code == "CREDENTIAL_UNAVAILABLE"
        assert_no_secret(secret_bank, error_surfaces(error.value))


@pytest.mark.parametrize("bad", ["unknown_id", "absolute_path", "parent_path", "wrong_version"])
def test_only_server_allowlisted_id_and_version_may_be_read(tmp_path, secret_bank, bad):
    env = uuid4()
    ref = make_reference(env, backend="mounted_secret", secret_id="synthetic-jwt-v1")
    allowed, unauthorized = tmp_path / "mounted-input", tmp_path / "unregistered-input"
    allowed.write_text(secret_bank["jwt"], encoding="utf-8")
    unauthorized.write_text(secret_bank["provider"], encoding="utf-8")
    updates = {"unknown_id": {"external_secret_id": "unregistered"},
               "absolute_path": {"external_secret_id": str(unauthorized)},
               "parent_path": {"external_secret_id": "../unregistered-input"},
               "wrong_version": {"version": uuid4()}}[bad]
    candidate = ref.model_copy(update=updates)
    with (
        observe_reads(unauthorized) as reads,
        mounted_store(tmp_path, env, {ref.external_secret_id: (ref, allowed)}) as store,
    ):
        with pytest.raises((ValueError, RuntimeError)) as error:
            store.get(candidate, candidate.version)
        assert reads == []
        assert_no_secret(secret_bank, error_surfaces(error.value))


def test_in_place_mount_rotation_invalidates_old_version_even_after_reopen(tmp_path, secret_bank):
    env = uuid4()
    ref = make_reference(env, backend="mounted_secret", secret_id="synthetic-jwt-v1")
    source = tmp_path / "mounted-input"
    source.write_text(secret_bank["jwt"], encoding="utf-8")
    mapping = {ref.external_secret_id: (ref, source)}
    with mounted_store(tmp_path, env, mapping) as store:
        assert_secret_equal(store.get(ref, ref.version), secret_bank["jwt"])
        source.write_text(secret_bank["provider"], encoding="utf-8")
        with pytest.raises((ValueError, RuntimeError)) as error:
            store.get(ref, ref.version)
        assert error.value.code == "CREDENTIAL_UNAVAILABLE"
        assert_no_secret(secret_bank, error_surfaces(error.value))
        assert_no_secret(secret_bank, safe_surfaces(store.status(ref, ref.version)))
    with mounted_store(tmp_path, env, mapping) as restarted:
        with pytest.raises((ValueError, RuntimeError)) as error:
            restarted.get(ref, ref.version)
        assert error.value.code == "CREDENTIAL_UNAVAILABLE"
        assert_no_secret(secret_bank, safe_surfaces(restarted.status(ref, ref.version)))


@pytest.mark.parametrize("sink", ["configuration", "response", "error", "logs", "trace", "audit", "benchmark", "diagnostics"])
def test_scanner_negative_control_detects_each_prohibited_sink(secret_bank, sink):
    # A scanner sensitivity check, never a claim that a product sink was exercised.
    clean = {sink: b'{"code":"SAFE","value":null}'}
    assert_no_secret(secret_bank, clean)
    with pytest.raises(pytest.fail.Exception, match="SECRET_DISCLOSURE"):
        assert_no_secret(secret_bank, {sink: ("prefix:" + secret_bank["provider"]).encode("utf-8")})
