"""TCR-S1-ENV-CONFIG-002: identity and public non-secret schemas."""

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.app.core.environment import bootstrap_settings, select_environment
from backend.app.schemas.settings import (
    BusinessSettings,
    ConfigurationValues,
    ProviderProfile,
    RuntimeKind,
)


@pytest.mark.parametrize("runtime,expected", [
    (RuntimeKind.PYTHON, "development"), (RuntimeKind.WINDOWS, "development"),
    (RuntimeKind.DOCKER, "demo"),
])
def test_runtime_default_and_dev_mode_independent(runtime, expected):
    assert select_environment(runtime_kind=runtime, process_env={"DEV_MODE": "true"}) == expected
    assert select_environment(runtime_kind=runtime, process_env={"DEV_MODE": "false"}) == expected


def test_cli_wins_and_test_is_explicit():
    assert select_environment(cli_environment="test", process_env={"EDUAGENT_ENVIRONMENT": "demo"}) == "test"
    assert select_environment(process_env={"EDUAGENT_ENVIRONMENT": "test"}) == "test"


@pytest.mark.parametrize("value", ["", "staging", "TEST", "synthetic-secret"])
def test_invalid_identity_never_defaults(value):
    with pytest.raises(ValueError, match="INVALID_ENVIRONMENT") as error:
        select_environment(process_env={"EDUAGENT_ENVIRONMENT": value})
    assert value not in str(error.value) or value == ""


def test_bootstrap_does_not_require_business_or_model_keys(tmp_path):
    boot = bootstrap_settings(config_root=tmp_path, environment_id=uuid4(), cli_environment="test")
    assert boot.environment == "test"
    assert ConfigurationValues().business.jwt_secret_ref is None
    assert ConfigurationValues().providers == {}


def test_plaintext_secret_rejected_and_error_safe():
    secret = "synthetic-never-persist"
    with pytest.raises(ValidationError) as error:
        BusinessSettings.model_validate({"jwt_secret_key": secret})
    assert secret not in str(error.value)


@pytest.mark.parametrize("url", [
    "https://user:synthetic-key@example.test/v1",
    "https://example.test/v1?key=synthetic-key",
    "https://example.test/v1#synthetic-key", "file:///secret",
])
def test_provider_url_cannot_contain_credentials(url):
    with pytest.raises(ValidationError) as error:
        ProviderProfile(profile_id=uuid4(), profile_revision=uuid4(),
                        display_name="fake", adapter_id="fake", model_id="fake", base_url=url)
    assert "synthetic-key" not in str(error.value)


def test_public_dto_only_has_credential_reference(tmp_path):
    env_id = uuid4()
    ref = {"credential_id": str(uuid4()), "version": str(uuid4()),
           "environment_id": str(env_id), "backend": "dpapi", "purpose": "jwt", "external_secret_id": None}
    value = ConfigurationValues.model_validate({"business": {"jwt_secret_ref": ref, "storage_root": str(tmp_path)}})
    public = json.loads(value.model_dump_json())
    assert public["business"]["jwt_secret_ref"] == ref
    assert "secret_key" not in value.model_dump_json()

