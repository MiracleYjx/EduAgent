"""TCR-Phase13-M1: T088/T089 read-only setup facts, no product writes."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.schemas.settings import (
    BusinessSettings,
    ConfigurationValues,
    DatabaseLocator,
    RedisLocator,
    SetupDatabaseData,
    SetupRecommendation,
    SetupStorageData,
)
from backend.app.services.settings.setup_state import (
    build_setup_recommendation,
    detect_setup_data_state,
    probe_database_data,
    probe_storage_data,
)


def _business(storage: Path | None = None, *, dev_mode: bool = False) -> BusinessSettings:
    return BusinessSettings(
        database_locator=DatabaseLocator(
            host="postgres", port=5432, database="eduagent", user="eduagent"
        ),
        redis_locator=RedisLocator(host="redis", port=6379, database=0),
        storage_root=storage,
        dev_mode=dev_mode,
    )


def test_recommendation_uses_docker_environment_without_secrets() -> None:
    recommendation = build_setup_recommendation(
        active=None,
        runtime_kind="docker",
        environ={
            "DATABASE_URL": "postgresql+psycopg://eduagent@postgres:5432/eduagent",
            "REDIS_URL": "redis://redis:6379/0",
            "STORAGE_ROOT": "/app/storage/storage",
            "DEV_MODE": "true",
        },
    )

    assert recommendation.available is True
    assert recommendation.auto_apply is True
    assert recommendation.mode == "docker"
    assert recommendation.business.database_locator is not None
    assert recommendation.business.database_locator.host == "postgres"
    assert recommendation.business.redis_locator is not None
    assert recommendation.business.storage_root is not None
    assert recommendation.business.dev_mode is True
    assert recommendation.sources == {
        "business.database_locator": "process_env",
        "business.redis_locator": "process_env",
        "business.storage_root": "process_env",
        "business.dev_mode": "process_env",
    }
    dumped = recommendation.model_dump_json()
    assert "postgresql" not in dumped
    assert "redis://" not in dumped
    assert "password" not in dumped


def test_recommendation_does_not_auto_apply_password_bearing_urls() -> None:
    recommendation = build_setup_recommendation(
        active=None,
        runtime_kind="docker",
        environ={
            "DATABASE_URL": "postgresql+psycopg://eduagent:secret@postgres:5432/eduagent",
            "REDIS_URL": "redis://:secret@redis:6379/0",
            "STORAGE_ROOT": "/app/storage/storage",
        },
    )

    assert recommendation.available is True
    assert recommendation.auto_apply is False
    assert recommendation.reason == "CREDENTIAL_OR_SOURCE_REQUIRED"
    assert "DATABASE_PASSWORD_NOT_INCLUDED" in recommendation.warnings
    assert "REDIS_PASSWORD_NOT_INCLUDED" in recommendation.warnings
    assert "secret" not in recommendation.model_dump_json()


def test_active_configuration_precedes_process_environment(tmp_path: Path) -> None:
    active = ConfigurationValues(business=_business(tmp_path, dev_mode=True))
    recommendation = build_setup_recommendation(
        active=active,
        runtime_kind="docker",
        environ={
            "DATABASE_URL": "postgresql+psycopg://other@other:5432/other",
            "REDIS_URL": "redis://other:6379/1",
            "STORAGE_ROOT": "/tmp/other",
            "DEV_MODE": "false",
        },
    )

    assert recommendation.sources == {
        "business.database_locator": "active_configuration",
        "business.redis_locator": "active_configuration",
        "business.storage_root": "active_configuration",
        "business.dev_mode": "active_configuration",
    }
    assert recommendation.business.database_locator is not None
    assert recommendation.business.database_locator.host == "postgres"
    assert recommendation.business.dev_mode is True


def test_missing_sources_are_unavailable_not_defaulted() -> None:
    recommendation = build_setup_recommendation(
        active=None,
        runtime_kind="python",
        environ={},
    )

    assert recommendation.available is False
    assert recommendation.auto_apply is False
    assert recommendation.reason == "DATABASE_NOT_CONFIGURED"
    assert recommendation.business.database_locator is None
    assert recommendation.business.redis_locator is None
    assert recommendation.business.storage_root is None


@pytest.mark.parametrize(
    ("database", "storage", "expected", "can_initialize"),
    [
        (
            SetupDatabaseData(status="empty", table_count=0),
            SetupStorageData(status="empty", entry_count=0),
            "new",
            True,
        ),
        (
            SetupDatabaseData(status="existing", table_count=3),
            SetupStorageData(status="existing", entry_count=2),
            "existing",
            False,
        ),
        (
            SetupDatabaseData(status="existing", table_count=3),
            SetupStorageData(status="empty", entry_count=0),
            "needs_confirmation",
            False,
        ),
        (
            SetupDatabaseData(status="needs_confirmation", code="SCHEMA_WITHOUT_ROWS"),
            SetupStorageData(status="empty", entry_count=0),
            "needs_confirmation",
            False,
        ),
        (
            SetupDatabaseData(status="unavailable", code="DATABASE_UNREACHABLE"),
            SetupStorageData(status="empty", entry_count=0),
            "unknown",
            False,
        ),
        (
            SetupDatabaseData(status="empty", table_count=0),
            SetupStorageData(status="unavailable", code="STORAGE_UNREADABLE"),
            "unknown",
            False,
        ),
    ],
)
def test_detection_only_allows_fully_empty_target(
    database: SetupDatabaseData,
    storage: SetupStorageData,
    expected: str,
    can_initialize: bool,
) -> None:
    recommendation = SetupRecommendation(
        available=True,
        auto_apply=True,
        mode="docker",
        business={"storage_root": "C:/synthetic/storage"},
    )

    state = detect_setup_data_state(
        recommendation=recommendation,
        database_probe=lambda _locator: database,
        storage_probe=lambda _root: storage,
    )

    assert state.status == expected
    assert state.can_initialize is can_initialize


def test_invalid_dev_mode_is_not_auto_applied() -> None:
    recommendation = build_setup_recommendation(
        active=None,
        runtime_kind="docker",
        environ={
            "DATABASE_URL": "postgresql+psycopg://eduagent@postgres:5432/eduagent",
            "REDIS_URL": "redis://redis:6379/0",
            "STORAGE_ROOT": "/app/storage/storage",
            "DEV_MODE": "sometimes",
        },
    )

    assert recommendation.available is True
    assert recommendation.auto_apply is False
    assert recommendation.reason == "INVALID_DEV_MODE"
    assert "INVALID_DEV_MODE" in recommendation.warnings
    assert recommendation.business.dev_mode is False


def test_storage_probe_is_read_only(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    assert probe_storage_data(missing).status == "empty"
    assert not missing.exists()

    empty = tmp_path / "empty"
    empty.mkdir()
    assert probe_storage_data(empty).status == "empty"

    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "material.txt").write_text("synthetic", encoding="utf-8")
    assert probe_storage_data(existing).status == "existing"


def test_database_probe_without_locator_is_unavailable() -> None:
    result = probe_database_data(None)

    assert result.status == "unavailable"
    assert result.code == "DATABASE_NOT_CONFIGURED"
