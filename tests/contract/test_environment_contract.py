"""TCR-S1-ENV-CONFIG-002: real legacy helper behavior through only Fakes."""

from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.schema import CreateSchema

from backend.app.schemas.settings import DatabaseIdentity
from backend.app.schemas.settings import TestResourceProfile as ResourceProfile
from tests import postgres_helpers


@pytest.mark.parametrize("actual", [
    {"database": "development", "user": "developer", "instance": "development-instance"},
    {"database": "demo", "user": "demo-user", "instance": "demo-instance"},
    {"database": "synthetic_test", "user": "developer", "instance": "isolated-instance"},
    {"database": "synthetic_test", "user": "runner", "instance": "other-instance"},
])
def test_legacy_helper_rejects_wrong_actual_target_before_write(monkeypatch, tmp_path, actual):
    writes = []
    profile = ResourceProfile(environment="test", environment_id=uuid4(),
        database_identity=DatabaseIdentity(host="test.invalid", database="synthetic_test",
            user="runner", schema_name="public", instance_id="isolated-instance"),
        root=tmp_path)
    class Connection:
        def scalar(self, statement):
            sql = str(statement)
            if "current_database" in sql:
                return actual["database"]
            if "current_user" in sql:
                return actual["user"]
            if "current_schema" in sql:
                return "public"
            if "pg_control_system" in sql:
                return actual["instance"]
            return 1  # pgvector installed in this Fake only
        def execute(self, statement):
            writes.append(type(statement).__name__)
            return SimpleNamespace()
    connection = Connection()
    class Engine:
        def connect(self):
            return nullcontext(connection)
        def begin(self):
            return nullcontext(connection)
        def dispose(self):
            pass
    fake = Engine()
    # Never call the real get_settings, SQLAlchemy engine factory or listener.
    monkeypatch.setattr(postgres_helpers, "load_test_resource_profile", lambda: profile)
    monkeypatch.setattr(postgres_helpers, "get_settings",
                        lambda: SimpleNamespace(database_url="postgresql+psycopg://runner@test.invalid:5432/synthetic_test"))
    monkeypatch.setattr(postgres_helpers, "create_database_engine", lambda *a, **k: fake)
    monkeypatch.setattr(postgres_helpers.event, "listens_for", lambda *a, **k: lambda fn: fn)
    monkeypatch.setattr(postgres_helpers.Base.metadata, "create_all", lambda *a, **k: writes.append("create_all"))
    rejected = False
    try:
        with postgres_helpers.isolated_postgres_engine():
            pass
    except ValueError:
        rejected = True
    assert rejected, f"Wrong actual target was accepted; Fake writes={writes}"
    assert writes == []


def test_legacy_helper_accepts_only_owned_test_schema(monkeypatch, tmp_path):
    writes = []
    created_schema = None
    profile = ResourceProfile(environment="test", environment_id=uuid4(),
        database_identity=DatabaseIdentity(host="test.invalid", database="synthetic_test",
            user="runner", schema_name="public", instance_id="isolated-instance"),
        root=tmp_path)

    class Connection:
        def scalar(self, statement, parameters=None):
            sql = str(statement)
            if "current_database" in sql:
                return "synthetic_test"
            if "current_user" in sql:
                return "runner"
            if "current_schema" in sql:
                return created_schema or "public"
            if "pg_control_system" in sql:
                return "isolated-instance"
            if "pg_get_userbyid" in sql:
                return "runner"
            return 1

        def execute(self, statement):
            nonlocal created_schema
            writes.append(type(statement).__name__)
            if isinstance(statement, CreateSchema):
                created_schema = statement.element
            return SimpleNamespace()

    connection = Connection()
    class Engine:
        def connect(self):
            return nullcontext(connection)
        def begin(self):
            return nullcontext(connection)
        def dispose(self):
            pass

    monkeypatch.setattr(postgres_helpers, "load_test_resource_profile", lambda: profile)
    monkeypatch.setattr(postgres_helpers, "get_settings", lambda: SimpleNamespace(
        database_url="postgresql+psycopg://runner@test.invalid:5432/synthetic_test"))
    monkeypatch.setattr(postgres_helpers, "create_database_engine", lambda *a, **k: Engine())
    monkeypatch.setattr(postgres_helpers.event, "listens_for", lambda *a, **k: lambda fn: fn)
    monkeypatch.setattr(postgres_helpers.Base.metadata, "create_all",
                        lambda *a, **k: writes.append("create_all"))
    with postgres_helpers.isolated_postgres_engine():
        assert writes == ["CreateSchema", "create_all"]
    assert writes == ["CreateSchema", "create_all", "DropSchema"]


def test_legacy_helper_refuses_without_explicit_profile_before_engine(monkeypatch):
    monkeypatch.setattr(postgres_helpers, "load_test_resource_profile",
                        lambda: (_ for _ in ()).throw(ValueError("TEST_RESOURCE_PROFILE_REQUIRED")))
    monkeypatch.setattr(postgres_helpers, "create_database_engine",
                        lambda *a, **k: pytest.fail("engine constructed before profile validation"))
    with (
        pytest.raises(ValueError, match="TEST_RESOURCE_PROFILE_REQUIRED"),
        postgres_helpers.isolated_postgres_engine(),
    ):
        pass


def test_backup_fixture_rejects_wrong_url_before_control_connection(monkeypatch, tmp_path):
    from tests.integration import test_backup_restore as backup_module

    profile = ResourceProfile(environment="test", environment_id=uuid4(),
        database_identity=DatabaseIdentity(host="test.invalid", database="eduagent_test",
            user="eduagent_test_runner", schema_name="public", instance_id="isolated-instance"),
        root=tmp_path)
    monkeypatch.setattr(backup_module, "load_test_resource_profile", lambda: profile)
    monkeypatch.setattr(backup_module, "get_settings", lambda: SimpleNamespace(
        database_url="postgresql+psycopg://developer@dev.invalid:5432/development"))
    monkeypatch.setattr(backup_module, "create_engine",
                        lambda *a, **k: pytest.fail("control engine built for wrong Test target"))
    with (
        pytest.raises(ValueError, match="TEST_BACKUP_DATABASE_URL_MISMATCH"),
        backup_module.isolated_database(),
    ):
        pass


def test_backup_fixture_requires_separate_maintenance_identity(monkeypatch, tmp_path):
    from tests.integration import test_backup_restore as backup_module

    profile = ResourceProfile(environment="test", environment_id=uuid4(),
        database_identity=DatabaseIdentity(host="test.invalid", database="eduagent_test",
            user="eduagent_test_runner", schema_name="public", instance_id="isolated-instance"),
        root=tmp_path)
    monkeypatch.setattr(backup_module, "load_test_resource_profile", lambda: profile)
    monkeypatch.setattr(backup_module, "get_settings", lambda: SimpleNamespace(
        database_url="postgresql+psycopg://eduagent_test_runner@test.invalid:5432/eduagent_test"))
    monkeypatch.setattr(backup_module, "create_engine",
                        lambda *a, **k: pytest.fail("control engine built without maintenance identity"))
    with (
        pytest.raises(ValueError, match="TEST_BACKUP_MAINTENANCE_PROFILE_REQUIRED"),
        backup_module.isolated_database(),
    ):
        pass

