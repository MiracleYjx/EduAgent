"""TCR-023：设置驱动初始化的目标核验与 Alembic 执行器（T220-Fix）。"""

from __future__ import annotations

import os
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from backend.app.schemas.settings import (
    ConfigurationValues,
    DatabaseLocator,
)
from backend.app.services.settings import database_initialization as runner
from backend.app.services.settings import readiness
from backend.app.services.settings.config_service import ConfigService
from backend.app.services.settings.management import InitializationService
from backend.app.services.settings.storage_preparation import StoragePreparationError


class _Result:
    def __init__(self, *, one: Any = None, scalar: Any = None, first: Any = None) -> None:
        self._one = one
        self._scalar = scalar
        self._first = first

    def one(self) -> Any:
        return self._one

    def scalar_one(self) -> Any:
        return self._scalar

    def first(self) -> Any:
        return self._first


class FakeConnection:
    def __init__(self, *, identity: tuple, system: str, privileges: tuple, vector: Any) -> None:
        self.identity = identity
        self.system = system
        self.privileges = privileges
        self.vector = vector
        self.calls: list[str] = []

    def execute(self, statement: Any) -> _Result:
        sql = str(statement)
        self.calls.append(sql)
        if "has_database_privilege" in sql:
            return _Result(one=self.privileges)
        if "current_database()" in sql:
            return _Result(one=self.identity)
        if "pg_control_system" in sql:
            return _Result(scalar=self.system)
        if "pg_available_extensions" in sql:
            return _Result(first=self.vector)
        raise AssertionError(f"unexpected SQL: {sql}")


def _expected(database: str = "eduagent") -> dict[str, str]:
    return {"database": database, "user": "synthetic_runner", "schema": "public"}


def test_test_environment_without_instance_identity_is_blocked() -> None:
    with pytest.raises(runner.InitializationError) as error:
        runner.require_test_identity("test", None)
    assert error.value.code == "TARGET_IDENTITY_UNVERIFIED"

    runner.require_test_identity("test", "7690000000000000001")
    runner.require_test_identity("development", None)


def test_target_identity_mismatch_is_blocked() -> None:
    connection = FakeConnection(
        identity=("other_database", "synthetic_runner", "public"),
        system="7690000000000000001",
        privileges=(True, True, True),
        vector=(1,),
    )

    with pytest.raises(runner.InitializationError) as error:
        runner.verify_database_target(
            connection, expected=_expected(), test_instance_id="7690000000000000001"
        )

    assert error.value.code == "TARGET_IDENTITY_MISMATCH"


def test_test_instance_identity_is_required_and_compared() -> None:
    connection = FakeConnection(
        identity=("eduagent", "synthetic_runner", "public"),
        system="7690000000000009999",
        privileges=(True, True, True),
        vector=(1,),
    )

    with pytest.raises(runner.InitializationError) as error:
        runner.verify_database_target(
            connection, expected=_expected(), test_instance_id="7690000000000000001"
        )

    assert error.value.code == "TARGET_IDENTITY_MISMATCH"


def test_privileges_and_pgvector_are_required() -> None:
    no_create = FakeConnection(
        identity=("eduagent", "synthetic_runner", "public"),
        system="7690000000000000001",
        privileges=(True, True, False),
        vector=(1,),
    )
    with pytest.raises(runner.InitializationError) as error:
        runner.verify_database_target(no_create, expected=_expected())
    assert error.value.code == "TARGET_PRIVILEGE_MISSING"

    no_vector = FakeConnection(
        identity=("eduagent", "synthetic_runner", "public"),
        system="7690000000000000001",
        privileges=(True, True, True),
        vector=None,
    )
    with pytest.raises(runner.InitializationError) as error:
        runner.verify_database_target(no_vector, expected=_expected())
    assert error.value.code == "PGVECTOR_UNAVAILABLE"


def test_verified_target_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    connection = FakeConnection(
        identity=("eduagent", "synthetic_runner", "public"),
        system="7690000000000000001",
        privileges=(True, True, True),
        vector=(1,),
    )

    runner.verify_database_target(
        connection, expected=_expected(), test_instance_id="7690000000000000001"
    )


def test_alembic_runner_uses_programmatic_connection_without_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    connection = object()
    calls: dict[str, Any] = {}

    class FakeEngine:
        def connect(self) -> Any:
            return nullcontext(connection)

        def dispose(self) -> None:
            calls["disposed"] = True

    monkeypatch.setattr(
        runner, "create_engine", lambda url, **_kwargs: FakeEngine()
    )

    def fake_upgrade(config: Any, revision: str) -> None:
        calls["connection"] = config.attributes.get("connection")
        calls["script_location"] = config.get_main_option("script_location")
        calls["revision"] = revision

    monkeypatch.setattr("alembic.command.upgrade", fake_upgrade)
    before = dict(os.environ)

    runner.run_alembic_upgrade(
        "postgresql+psycopg://synthetic:secret@127.0.0.1:1/eduagent",
        project_root=tmp_path,
    )

    assert calls["connection"] is connection
    assert calls["script_location"] == str(tmp_path / "migrations")
    assert calls["revision"] == "head"
    assert calls["disposed"] is True
    assert os.environ == before
    assert "secret" not in str(calls)


def _service(
    tmp_path: Path,
    *,
    verify: Any,
    migrator: Any,
    read_head: Any = None,
    storage_root: Path | None = None,
    prepare_storage: Any = None,
) -> tuple[InitializationService, ConfigService, UUID]:
    environment_id = uuid4()
    config = ConfigService(tmp_path, environment_id)
    values = ConfigurationValues.model_validate(
        {
            "business": {
                "database_locator": DatabaseLocator(
                    host="127.0.0.1", port=1, database="synthetic", user="synthetic"
                ).model_dump(mode="json"),
                "redis_locator": {"host": "127.0.0.1", "port": 1},
                "storage_root": str(tmp_path / "storage"),
            }
        }
    )
    draft = config.create_draft(values, expected_revision=None, process_env={})
    service = InitializationService(
        config=config,
        environment="development",
        database_url=lambda _values: "postgresql+psycopg://synthetic@127.0.0.1:1/synthetic",
        migrator=migrator,
        read_head=read_head or (lambda _url: "previous-head"),
        verify_database=verify,
        storage_root=storage_root,
        prepare_storage=prepare_storage,
    )
    return service, config, draft.revision_id


def _wait(service: InitializationService, operation_id: UUID) -> Any:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        operation = service._operations.get(operation_id)
        if operation.status in {"succeeded", "failed", "interrupted"}:
            return operation
        time.sleep(0.02)
    raise AssertionError("operation did not finish")


def test_verification_failure_is_a_failed_operation_and_keeps_pointer(
    tmp_path: Path,
) -> None:
    def verify(_url: str, _expected: Any) -> None:
        raise runner.InitializationError("TARGET_IDENTITY_MISMATCH")

    service, config, draft_id = _service(tmp_path, verify=verify, migrator=lambda _url: None)
    try:
        preview = service.preview(draft_id)
        operation = service.apply(preview.preview_id, draft_id)
        final = _wait(service, operation.operation_id)
        assert final.status == "failed"
        assert final.error_code == "TARGET_IDENTITY_MISMATCH"
        manifest = config.read_manifest()
        assert manifest.active_revision is None
        assert manifest.pending_restart_revision is None
    finally:
        config.close()


def test_initialization_creates_nested_storage_root_before_success(
    tmp_path: Path,
) -> None:
    root = tmp_path / "custom" / "nested" / "storage"
    heads = {"value": "previous-head"}

    def migrate(_url: str) -> None:
        heads["value"] = readiness.discover_migration_head()

    service, config, draft_id = _service(
        tmp_path,
        verify=lambda _url, _expected: None,
        migrator=migrate,
        read_head=lambda _url: heads["value"],
        storage_root=root,
    )
    try:
        preview = service.preview(draft_id)
        operation = service.apply(preview.preview_id, draft_id)
        final = _wait(service, operation.operation_id)
        assert final.status == "succeeded"
        assert root.is_dir()
        assert list(root.glob(".readiness-init-*")) == []
    finally:
        config.close()


def test_storage_preparation_failure_is_safe_and_does_not_migrate(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def fail_storage(_root: Path) -> None:
        raise StoragePreparationError("STORAGE_ROOT_UNAVAILABLE")

    def migrate(url: str) -> None:
        calls.append(url)

    service, config, draft_id = _service(
        tmp_path,
        verify=lambda _url, _expected: None,
        migrator=migrate,
        storage_root=tmp_path / "storage",
        prepare_storage=fail_storage,
    )
    try:
        preview = service.preview(draft_id)
        operation = service.apply(preview.preview_id, draft_id)
        final = _wait(service, operation.operation_id)
        assert final.status == "failed"
        assert final.error_code == "STORAGE_ROOT_UNAVAILABLE"
        assert calls == []
    finally:
        config.close()


def test_migration_failure_keeps_active_and_pending_unchanged(
    tmp_path: Path,
) -> None:
    def broken(_url: str) -> None:
        raise RuntimeError("synthetic migration failure")

    service, config, draft_id = _service(
        tmp_path, verify=lambda _url, _expected: None, migrator=broken
    )
    try:
        preview = service.preview(draft_id)
        operation = service.apply(preview.preview_id, draft_id)
        final = _wait(service, operation.operation_id)
        assert final.status == "failed"
        assert final.error_code == "MIGRATION_FAILED"
        manifest = config.read_manifest()
        assert manifest.active_revision is None
        assert manifest.pending_restart_revision is None
    finally:
        config.close()
