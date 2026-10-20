"""TCR-S1-ENV-CONFIG-002: explicit source priority and segment failure."""

from backend.app.core.config import resolve_configuration
from backend.app.services.settings.config_service import ConfigService


def test_priority_and_source_for_each_layer(tmp_path):
    dotenv = tmp_path / "selected.env"
    dotenv.write_text("JWT_EXPIRE_MINUTES=30\n", encoding="utf-8")
    layers = {"defaults": {"business": {"jwt_expire_minutes": 10}},
              "saved": {"business": {"jwt_expire_minutes": 20}}, "dotenv_path": dotenv,
              "process_env": {"JWT_EXPIRE_MINUTES": "40"}}
    for expected, source, removed in [(40, "process_env", None), (30, "dotenv", "process_env"),
                                      (20, "saved", "dotenv_path"), (10, "default", "saved")]:
        if removed:
            layers.pop(removed)
        resolved = resolve_configuration(**layers)
        assert not resolved.errors
        assert resolved.segments["business"].jwt_expire_minutes == expected
        assert resolved.sources["business.jwt_expire_minutes"].source == source


def test_invalid_overlay_disables_only_affected_segment():
    value = resolve_configuration(saved={"business": {"jwt_expire_minutes": 30}},
                                  process_env={"JWT_EXPIRE_MINUTES": "synthetic-invalid-secret"})
    assert "business" not in value.segments
    assert "bootstrap" in value.segments
    assert value.sources["business.jwt_expire_minutes"].source == "process_env"
    assert value.errors[0].fields == ("business.jwt_expire_minutes",)
    assert "synthetic-invalid-secret" not in value.model_dump_json()


def test_invalid_explicit_dotenv_does_not_fall_back(tmp_path):
    path = tmp_path / "selected.env"
    path.write_text("EDUAGENT__BOOTSTRAP__PORT=70000\n", encoding="utf-8")
    value = resolve_configuration(dotenv_path=path)
    assert "bootstrap" not in value.segments
    assert value.errors[0].segment == "bootstrap"


def test_no_implicit_cwd_dotenv_and_no_secret_env(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("JWT_EXPIRE_MINUTES=99\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    value = resolve_configuration(process_env={"DEEPSEEK_API_KEY": "synthetic-secret"})
    assert value.segments["business"].jwt_expire_minutes == 60
    assert "synthetic-secret" not in value.model_dump_json()


def test_selected_dotenv_rejects_secret_and_interpolation(tmp_path):
    path = tmp_path / "selected.env"
    path.write_text("JWT_SECRET_KEY=synthetic-secret\nJWT_EXPIRE_MINUTES=${SECRET_VALUE}\n", encoding="utf-8")
    value = resolve_configuration(dotenv_path=path)
    assert {error.code for error in value.errors} == {"SECRET_IN_DOTENV", "INVALID_CONFIGURATION"}
    assert "business" not in value.segments
    assert "synthetic-secret" not in value.model_dump_json()


def test_unknown_high_priority_field_is_reported():
    value = resolve_configuration(process_env={"EDUAGENT__BUSINESS__unexpected": "synthetic-unique-sensitive-value"})
    assert "business" not in value.segments
    assert value.errors[0].fields == ("business.UNKNOWN_FIELD",)
    assert "synthetic-unique-sensitive-value" not in value.model_dump_json()


def test_service_uses_common_resolution_and_mounted_source(tmp_path):
    from uuid import uuid4
    with ConfigService(tmp_path, uuid4()) as service:
        value = service.resolve(mounted_config={"business": {"jwt_expire_minutes": 13}},
                                process_env={"JWT_EXPIRE_MINUTES": "15"})
        assert value.segments["business"].jwt_expire_minutes == 15
        assert value.sources["business.jwt_expire_minutes"].source == "process_env"



def test_unknown_field_name_does_not_leak_in_sources_or_error():
    marker = "synthetic-sensitive-field-name-9b18"
    value = resolve_configuration(process_env={"EDUAGENT__BUSINESS__" + marker: "12"})
    assert "business" not in value.segments
    assert value.errors[0].fields == ("business.UNKNOWN_FIELD",)
    assert marker not in value.model_dump_json()



def test_object_overlay_replaces_null_parent_source_with_leaf_sources():
    resolved = resolve_configuration(
        saved={"business": {"database_locator": {"host": "saved-test", "port": 15432,
                "database": "eduagent_test", "user": "test_runner", "schema": "public"}}},
        process_env={"EDUAGENT__BUSINESS__DATABASE_LOCATOR__HOST": "effective-test"},
    )
    assert not resolved.errors
    assert "business.database_locator" not in resolved.sources
    assert resolved.sources["business.database_locator.host"].source == "process_env"
    assert resolved.sources["business.database_locator.port"].source == "saved"
    assert resolved.segments["business"].database_locator.host == "effective-test"
    assert resolved.segments["business"].database_locator.port == 15432
