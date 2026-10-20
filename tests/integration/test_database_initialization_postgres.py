"""TCR-S3-SETTINGS-API-ASSEMBLY-024：T220 默认初始化执行器的真实 Test PG 边界。

只使用显式 Test 回执创建的随机 owned schema；连接串仅在内存中，不写环境、argv 或日志。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url

from backend.app.core.config import get_settings
from backend.app.core.environment import load_test_resource_profile
from backend.app.services.settings.database_initialization import (
    InitializationError,
    require_test_identity,
    run_alembic_upgrade,
    verify_database_url,
)
from tests.postgres_helpers import isolated_postgres_schema

ROOT = Path(__file__).resolve().parents[2]


def _scoped_url(schema: str) -> str:
    url = make_url(str(get_settings().database_url))
    options = f"-csearch_path={schema},public"
    return url.update_query_dict({"options": options}).render_as_string(hide_password=False)


def _expected(schema: str) -> dict[str, str]:
    identity = load_test_resource_profile().database_identity
    return {
        "database": identity.database,
        "user": identity.user,
        "schema": schema,
    }


def _head_revision() -> str:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    return str(ScriptDirectory.from_config(config).get_current_head())


def test_require_test_identity_rejects_unverified_target() -> None:
    with pytest.raises(InitializationError) as error:
        require_test_identity("test", None)
    assert error.value.code == "TARGET_IDENTITY_UNVERIFIED"

    require_test_identity("test", "7697651655622336550")
    require_test_identity("development", None)


def test_real_target_identity_and_pgvector_pass() -> None:
    profile = load_test_resource_profile()
    with isolated_postgres_schema() as (_engine, schema):
        verify_database_url(
            _scoped_url(schema),
            expected=_expected(schema),
            test_instance_id=profile.database_identity.instance_id,
        )


@pytest.mark.parametrize(
    ("field", "value", "instance_id"),
    [
        ("database", "other_database", None),
        ("user", "other_runner", None),
        ("schema", "public", None),
        ("instance", "ignored", "0"),
    ],
)
def test_real_target_mismatch_fails_closed(
    field: str, value: str, instance_id: str | None
) -> None:
    profile = load_test_resource_profile()
    with isolated_postgres_schema() as (_engine, schema):
        expected = _expected(schema)
        if field == "instance":
            instance_id = value
        else:
            expected[field] = value
        with pytest.raises(InitializationError) as error:
            verify_database_url(
                _scoped_url(schema),
                expected=expected,
                test_instance_id=instance_id or profile.database_identity.instance_id,
            )
        assert error.value.code == "TARGET_IDENTITY_MISMATCH"


def test_real_alembic_upgrade_reaches_head_and_is_idempotent() -> None:
    head = _head_revision()
    with isolated_postgres_schema() as (engine, schema):
        url = _scoped_url(schema)
        verify_database_url(
            url,
            expected=_expected(schema),
            test_instance_id=load_test_resource_profile().database_identity.instance_id,
        )
        run_alembic_upgrade(url)

        with engine.connect() as connection:
            assert connection.scalar(text("SELECT current_schema()")) == schema
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == head
            tables = connection.scalar(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = :schema"
                ),
                {"schema": schema},
            )
            assert tables is not None and tables > 20
            # 迁移只落在本次 owned schema，public 不得被写入。
            assert connection.scalar(text("SELECT to_regclass('public.alembic_version')")) is None

        # 重入升级保持同一 head，不重复建表。
        run_alembic_upgrade(url)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == head
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = :schema"
                ),
                {"schema": schema},
            ) == tables


def test_settings_locator_initializes_owned_schema_with_public_vector_extension():
    from pydantic import SecretStr

    from backend.app.schemas.settings import DatabaseLocator
    from backend.app.services.settings.management import _database_url

    configured = make_url(str(get_settings().database_url))
    with isolated_postgres_schema() as (engine, schema):
        locator = DatabaseLocator(host=configured.host, port=configured.port,
            database=configured.database, user=configured.username, schema_name=schema)
        url = _database_url(locator, SecretStr(configured.password))
        run_alembic_upgrade(url)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT current_schema()")) == schema
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == _head_revision()
            assert connection.scalar(text("SELECT to_regclass('public.alembic_version')")) is None


def test_empty_target_never_borrows_fallback_schema_migration_head():
    from backend.app.services.settings.management import read_current_alembic_head

    with (
        isolated_postgres_schema() as (target, schema),
        isolated_postgres_schema() as (fallback, fallback_schema),
    ):
        run_alembic_upgrade(_scoped_url(fallback_schema))
        base = make_url(str(get_settings().database_url))
        url = base.update_query_dict({"options": f"-csearch_path={schema},{fallback_schema},public"}).render_as_string(hide_password=False)
        assert read_current_alembic_head(url) is None
        run_alembic_upgrade(url)
        assert read_current_alembic_head(url) == _head_revision()
        with target.connect() as connection:
            before = connection.scalar(text("SELECT count(*) FROM information_schema.tables WHERE table_schema=:s"), {"s": schema})
            assert before > 20
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == _head_revision()
        run_alembic_upgrade(url)
        with target.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM information_schema.tables WHERE table_schema=:s"), {"s": schema}) == before
        with fallback.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == _head_revision()
