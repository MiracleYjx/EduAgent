"""TCR 005: guarded empty schemas and explicit child/file profiles; Fakes only."""

import os
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from backend.app.core.environment import load_test_resource_profile
from backend.app.schemas.settings import DatabaseIdentity
from backend.app.schemas.settings import TestResourceProfile as ResourceProfile
from tests import postgres_helpers


def _profile(root):
    return ResourceProfile(
        environment="test",
        environment_id=uuid4(),
        database_identity=DatabaseIdentity(
            host="test.invalid",
            database="eduagent_test",
            user="eduagent_test_runner",
            schema_name="public",
            instance_id="fake-isolated-instance",
        ),
        root=root,
    )


@pytest.fixture
def fake_postgres(monkeypatch, tmp_path):
    profile = _profile(tmp_path)
    writes = []
    state = {
        "schema": None,
        "owner": profile.database_identity.user,
        "instance": profile.database_identity.instance_id,
        "disposed": False,
    }

    class Connection:
        def scalar(self, statement, parameters=None):
            sql = str(statement)
            if "current_schema" in sql:
                return state["schema"] or "public"
            if "current_database" in sql:
                return profile.database_identity.database
            if "current_user" in sql:
                return profile.database_identity.user
            if "pg_control_system" in sql:
                return state["instance"]
            if "pg_get_userbyid" in sql:
                return state["owner"]
            return 1  # pgvector is installed in this Fake only.

        def execute(self, statement):
            writes.append(type(statement).__name__)
            if isinstance(statement, CreateSchema):
                state["schema"] = statement.element
            elif isinstance(statement, DropSchema):
                state["schema"] = None
            return SimpleNamespace()

    connection = Connection()

    class Engine:
        def connect(self):
            return nullcontext(connection)

        def begin(self):
            return nullcontext(connection)

        def dispose(self):
            state["disposed"] = True

    engine = Engine()
    monkeypatch.setattr(postgres_helpers, "load_test_resource_profile", lambda: profile)
    monkeypatch.setattr(
        postgres_helpers,
        "get_settings",
        lambda: SimpleNamespace(
            database_url="postgresql+psycopg://eduagent_test_runner@test.invalid:5432/eduagent_test"
        ),
    )
    monkeypatch.setattr(postgres_helpers, "create_database_engine", lambda *a, **k: engine)
    monkeypatch.setattr(postgres_helpers.event, "listens_for", lambda *a, **k: lambda fn: fn)
    monkeypatch.setattr(
        postgres_helpers.Base.metadata,
        "create_all",
        lambda *a, **k: writes.append("create_all"),
    )
    return SimpleNamespace(engine=engine, state=state, writes=writes)


def test_empty_schema_has_no_metadata_and_only_its_owner_cleans_it(fake_postgres):
    with postgres_helpers.isolated_postgres_schema() as (engine, schema):
        assert engine is fake_postgres.engine
        assert schema == fake_postgres.state["schema"]
        assert schema.startswith("test_")
        assert fake_postgres.writes == ["CreateSchema"]
    assert fake_postgres.writes == ["CreateSchema", "DropSchema"]
    assert fake_postgres.state["disposed"] is True


def test_current_metadata_wrapper_keeps_original_lifecycle(fake_postgres):
    with postgres_helpers.isolated_postgres_engine() as engine:
        assert engine is fake_postgres.engine
        assert fake_postgres.writes == ["CreateSchema", "create_all"]
    assert fake_postgres.writes == ["CreateSchema", "create_all", "DropSchema"]


def test_empty_schema_migration_failure_still_cleans_only_owned_schema(fake_postgres):
    with (
        pytest.raises(RuntimeError, match="fake migration failed"),
        postgres_helpers.isolated_postgres_schema(),
    ):
        raise RuntimeError("fake migration failed")
    assert fake_postgres.writes == ["CreateSchema", "DropSchema"]
    assert fake_postgres.state["disposed"] is True


def test_metadata_failure_still_uses_the_same_owned_cleanup(fake_postgres, monkeypatch):
    def fail_metadata(*args, **kwargs):
        raise RuntimeError("fake metadata failed")

    monkeypatch.setattr(postgres_helpers.Base.metadata, "create_all", fail_metadata)
    with (
        pytest.raises(RuntimeError, match="fake metadata failed"),
        postgres_helpers.isolated_postgres_engine(),
    ):
        pytest.fail("failed metadata must not yield an engine")
    assert fake_postgres.writes == ["CreateSchema", "DropSchema"]
    assert fake_postgres.state["disposed"] is True


def test_empty_schema_wrong_instance_is_rejected_before_ddl(fake_postgres):
    fake_postgres.state["instance"] = "another-instance"
    with (
        pytest.raises(ValueError, match="TEST_DATABASE_IDENTITY_MISMATCH"),
        postgres_helpers.isolated_postgres_schema(),
    ):
        pytest.fail("wrong instance must not yield an engine")
    assert fake_postgres.writes == []
    assert fake_postgres.state["disposed"] is True


def test_empty_schema_owner_change_cannot_trigger_drop(fake_postgres):
    with (
        pytest.raises(ValueError, match="TEST_SCHEMA_OWNER_CHANGED"),
        postgres_helpers.isolated_postgres_schema(),
    ):
        fake_postgres.state["owner"] = "another-owner"
    assert fake_postgres.writes == ["CreateSchema"]
    assert fake_postgres.state["disposed"] is True


def _select_profile(monkeypatch, tmp_path):
    profile = _profile(tmp_path)
    path = tmp_path / "source-test-resource-profile.json"
    path.write_text(profile.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("EDUAGENT_ENVIRONMENT", "test")
    monkeypatch.setenv("EDUAGENT_TEST_RESOURCE_PROFILE", str(path))
    monkeypatch.setenv("EDUAGENT_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("PGOPTIONS", "-csearch_path=public")
    monkeypatch.setattr(
        postgres_helpers,
        "get_settings",
        lambda: SimpleNamespace(
            database_url=(
                "postgresql+psycopg://eduagent_test_runner:fake-password@test.invalid:5432/"
                "eduagent_test?options=-csearch_path%3Dpublic"
            )
        ),
    )
    return profile, path


def test_subprocess_environment_binds_test_receipt_and_schema(monkeypatch, tmp_path):
    profile, path = _select_profile(monkeypatch, tmp_path)
    schema = f"test_{uuid4().hex}"
    before = dict(os.environ)
    values = postgres_helpers.postgres_subprocess_environment(schema)
    url = make_url(values["DATABASE_URL"])
    assert values["EDUAGENT_ENVIRONMENT"] == "test"
    assert values["EDUAGENT_TEST_RESOURCE_PROFILE"] == str(path)
    assert values["EDUAGENT_DATA_ROOT"] == str(profile.root)
    assert values["STORAGE_ROOT"] == str(profile.root / "storage")
    assert values["PGOPTIONS"] == f"-csearch_path={schema},public"
    assert url.query["options"] == values["PGOPTIONS"]
    assert url.host == profile.database_identity.host
    assert url.database == profile.database_identity.database
    assert url.username == profile.database_identity.user
    assert url.password == "fake-password"  # Never serialize a redacted engine URL.
    assert dict(os.environ) == before  # No process-global PGOPTIONS leak.


@pytest.mark.parametrize("schema", ["public", "benchmark_" + "a" * 32, "test_other_run", "test_x;DROP SCHEMA public"])
def test_subprocess_environment_rejects_uncontrolled_schema(monkeypatch, tmp_path, schema):
    _select_profile(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="TEST_SUBPROCESS_SCHEMA_REQUIRED"):
        postgres_helpers.postgres_subprocess_environment(schema)


def test_subprocess_environment_requires_explicit_test_before_settings(monkeypatch):
    monkeypatch.setenv("EDUAGENT_ENVIRONMENT", "development")
    monkeypatch.setattr(
        postgres_helpers,
        "get_settings",
        lambda: pytest.fail("settings must not load before explicit Test validation"),
    )
    with pytest.raises(ValueError, match="EXPLICIT_TEST_ENVIRONMENT_REQUIRED"):
        postgres_helpers.postgres_subprocess_environment(f"test_{uuid4().hex}")


def test_derived_file_profile_preserves_identity_and_restores_environment(monkeypatch, tmp_path):
    original, _ = _select_profile(monkeypatch, tmp_path)
    cache_resets = []
    monkeypatch.setattr(postgres_helpers, "reset_settings_cache", lambda: cache_resets.append(True))
    keys = ("EDUAGENT_ENVIRONMENT", "EDUAGENT_TEST_RESOURCE_PROFILE", "EDUAGENT_DATA_ROOT", "STORAGE_ROOT", "DATABASE_URL")
    before = {key: os.environ.get(key) for key in keys}
    root = tmp_path / "pytest-owned-run"
    with postgres_helpers.isolated_test_resource_profile(root) as profile:
        current = load_test_resource_profile()
        assert current.database_identity == original.database_identity
        assert current.environment_id == original.environment_id
        assert current.root == root
        assert profile.storage_root == root / "storage"
        assert profile.results_root == root / "results"
        assert os.environ["STORAGE_ROOT"] == str(profile.storage_root)
        assert os.environ["EDUAGENT_DATA_ROOT"] == str(root)
    assert {key: os.environ.get(key) for key in keys} == before
    assert cache_resets == [True, True]


def test_derived_file_profile_rejects_wrong_database_before_creating_files(monkeypatch, tmp_path):
    _select_profile(monkeypatch, tmp_path)
    monkeypatch.setattr(
        postgres_helpers,
        "get_settings",
        lambda: SimpleNamespace(database_url="postgresql+psycopg://developer@dev.invalid:5432/development"),
    )
    root = tmp_path / "must-not-exist"
    with (
        pytest.raises(ValueError, match="TEST_DATABASE_URL_MISMATCH"),
        postgres_helpers.isolated_test_resource_profile(root),
    ):
        pytest.fail("wrong Test URL must not yield a file profile")
    assert not root.exists()


def test_derived_file_profile_does_not_reuse_or_delete_existing_root(monkeypatch, tmp_path):
    _select_profile(monkeypatch, tmp_path)
    root = tmp_path / "already-owned"
    root.mkdir()
    sentinel = root / "sentinel"
    sentinel.write_bytes(b"keep the existing owner")
    with (
        pytest.raises(FileExistsError),
        postgres_helpers.isolated_test_resource_profile(root),
    ):
        pytest.fail("existing root must not be reused")
    assert sentinel.read_bytes() == b"keep the existing owner"
