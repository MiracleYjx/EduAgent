"""Owned PostgreSQL test schema, guarded by an explicit Test resource profile."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import event, text
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from backend.app.core.config import AppSettings, get_settings, reset_settings_cache
from backend.app.core.database import Base, create_database_engine
from backend.app.core.environment import (
    authorize_cleanup,
    load_test_resource_profile,
    resolve_environment_profile,
)
from backend.app.schemas.settings import (
    DatabaseIdentity,
    EnvironmentProfile,
    OwnedResource,
    ResourceManifest,
)


def _assert_configured_test_url(settings: AppSettings, identity: DatabaseIdentity) -> None:
    url = make_url(str(settings.database_url))
    if (
        url.host != identity.host
        or (url.port or 5432) != identity.port
        or url.database != identity.database
        or url.username != identity.user
    ):
        raise ValueError("TEST_DATABASE_URL_MISMATCH")


def _connected_identity(
    connection: Connection, expected: DatabaseIdentity, schema: str
) -> DatabaseIdentity:
    # system_identifier is read from the server, not inferred from its name.
    actual_schema = str(connection.scalar(text("SELECT current_schema()")))
    if actual_schema != schema:
        raise ValueError("TEST_SCHEMA_IDENTITY_MISMATCH")
    return DatabaseIdentity(
        host=expected.host,
        port=expected.port,
        database=str(connection.scalar(text("SELECT current_database()"))),
        user=str(connection.scalar(text("SELECT current_user"))),
        schema_name=actual_schema,
        instance_id=str(connection.scalar(text("SELECT system_identifier::text FROM pg_control_system()"))),
    )


@contextmanager
def isolated_test_resource_profile(root: Path) -> Iterator[EnvironmentProfile]:
    """Use a fresh pytest-owned file root without changing the verified DB identity."""

    receipt = load_test_resource_profile()
    settings = get_settings()
    _assert_configured_test_url(settings, receipt.database_identity)
    if not root.is_absolute() or root != root.resolve():
        raise ValueError("ABSOLUTE_TEST_ROOT_REQUIRED")
    # The caller supplies a new child of its tmp_path. Never remove shared roots.
    root.mkdir(parents=True, exist_ok=False)
    path = root / "test-resource-profile.json"
    derived = receipt.model_copy(update={"root": root})
    with path.open("x", encoding="utf-8") as handle:
        handle.write(derived.model_dump_json())
    values = {
        "EDUAGENT_ENVIRONMENT": receipt.environment.value,
        "EDUAGENT_TEST_RESOURCE_PROFILE": str(path),
        "EDUAGENT_DATA_ROOT": str(root),
        "STORAGE_ROOT": str(root / "storage"),
        "DATABASE_URL": str(settings.database_url),
    }
    with patch.dict(os.environ, values):
        reset_settings_cache()
        try:
            yield resolve_environment_profile()
        finally:
            reset_settings_cache()


def postgres_subprocess_environment(schema: str) -> dict[str, str]:
    """Borrow an owned schema in a child; its creator retains cleanup ownership."""

    receipt = load_test_resource_profile()
    settings = get_settings()
    _assert_configured_test_url(settings, receipt.database_identity)
    if re.fullmatch(r"test_[0-9a-f]{32}", schema) is None:
        raise ValueError("TEST_SUBPROCESS_SCHEMA_REQUIRED")
    profile = resolve_environment_profile()
    options = f"-csearch_path={schema},public"
    # Bind the URL as well: a URL's options must not override PGOPTIONS with a
    # stale/default schema. Keep credentials only in the child environment.
    url = make_url(str(settings.database_url)).update_query_dict({"options": options})
    values = dict(os.environ)
    values.update({
        "EDUAGENT_ENVIRONMENT": receipt.environment.value,
        "EDUAGENT_TEST_RESOURCE_PROFILE": os.environ["EDUAGENT_TEST_RESOURCE_PROFILE"],
        "EDUAGENT_DATA_ROOT": str(receipt.root),
        "STORAGE_ROOT": str(profile.storage_root),
        "DATABASE_URL": url.render_as_string(hide_password=False),
        "PGOPTIONS": options,
    })
    return values


@contextmanager
def _owned_postgres_schema(*, create_metadata: bool) -> Iterator[tuple[Engine, str]]:
    """Share the identity, ownership and cleanup boundary for both schema modes."""

    profile = load_test_resource_profile()
    settings = get_settings()
    _assert_configured_test_url(settings, profile.database_identity)
    run_id = uuid4()
    schema = f"test_{run_id.hex}"
    engine = create_database_engine(settings, connect_timeout=3)
    record: ResourceManifest | None = None

    @event.listens_for(engine, "connect")
    def set_test_schema(connection, _record) -> None:
        """每条连接都优先访问本次测试的表和枚举类型。"""

        with connection.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{schema}", public')
        connection.commit()

    try:
        with engine.connect() as connection:
            actual = _connected_identity(connection, profile.database_identity, "public")
            if actual != profile.database_identity:
                raise ValueError("TEST_DATABASE_IDENTITY_MISMATCH")
            installed = connection.scalar(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))
        if not installed:
            raise ValueError("TEST_PGVECTOR_MISSING")
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
            owner = connection.scalar(text(
                "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = :name"
            ), {"name": schema})
            if owner != profile.database_identity.user:
                raise ValueError("TEST_SCHEMA_OWNER_MISMATCH")
        resource = OwnedResource(kind="schema", target=schema, owner=str(owner),
                                 run_id=run_id, created_at=datetime.now(UTC))
        identity = profile.database_identity.model_copy(update={"schema_name": schema})
        record = ResourceManifest(environment=profile.environment,
            environment_id=profile.environment_id, run_id=run_id,
            database_identity=identity, root=(profile.root / run_id.hex).resolve(),
            resources=(resource,))
        with engine.begin() as connection:
            if _connected_identity(connection, profile.database_identity, schema) != identity:
                raise ValueError("TEST_SCHEMA_IDENTITY_MISMATCH")
            if create_metadata:
                Base.metadata.create_all(connection, checkfirst=False)
        engine.eduagent_test_manifest = record
        yield engine, schema
    finally:
        if hasattr(engine, "eduagent_test_manifest"):
            delattr(engine, "eduagent_test_manifest")
        try:
            if record is not None:
                with engine.begin() as connection:
                    actual = _connected_identity(connection, profile.database_identity, schema)
                    owner = connection.scalar(text(
                        "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = :name"
                    ), {"name": schema})
                    if owner != record.resources[0].owner:
                        raise ValueError("TEST_SCHEMA_OWNER_CHANGED")
                    authorize_cleanup(record, record.resources[0],
                                      actual_database=actual, actual_redis_instance=None)
                    connection.execute(DropSchema(schema, cascade=True))
        finally:
            engine.dispose()


@contextmanager
def isolated_postgres_schema() -> Iterator[tuple[Engine, str]]:
    """Own an empty schema; migrations, not metadata, choose its structure."""

    with _owned_postgres_schema(create_metadata=False) as resource:
        yield resource


@contextmanager
def isolated_postgres_engine() -> Iterator[Engine]:
    """Create current metadata inside the same guarded, owned Test schema."""

    with _owned_postgres_schema(create_metadata=True) as (engine, _schema):
        yield engine
