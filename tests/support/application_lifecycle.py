"""Bind real startup recovery to an already owned contract fixture engine."""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker


def bind_startup_session_factory(monkeypatch: Any, factory: sessionmaker[Session]) -> None:
    engine = factory.kw.get("bind")
    if engine is None:
        raise ValueError("EXPLICIT_SQLITE_FIXTURE_REQUIRED")
    if engine.dialect.name != "sqlite":
        from backend.app.core.environment import (
            authorize_cleanup,
            load_test_resource_profile,
        )
        from backend.app.schemas.settings import ResourceManifest
        from tests.postgres_helpers import _connected_identity

        record = getattr(engine, "eduagent_test_manifest", None)
        if engine.dialect.name != "postgresql" or not isinstance(record, ResourceManifest):
            raise ValueError("EXPLICIT_OWNED_TEST_FIXTURE_REQUIRED")
        profile = load_test_resource_profile()
        identity = record.database_identity
        if identity is None or identity.model_copy(update={"schema_name": "public"}) != profile.database_identity:
            raise ValueError("TEST_FIXTURE_IDENTITY_MISMATCH")
        with engine.connect() as connection:
            actual = _connected_identity(connection, profile.database_identity, identity.schema_name)
            owner = connection.scalar(text("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname=:name"), {"name": identity.schema_name})
            if owner != record.resources[0].owner:
                raise ValueError("TEST_FIXTURE_OWNER_MISMATCH")
            authorize_cleanup(record, record.resources[0], actual_database=actual, actual_redis_instance=None)

    from backend.app.api import grading, paper_import, workflow

    for consumer in (grading, workflow, paper_import):
        monkeypatch.setattr(consumer, "get_session_factory", lambda: factory)


def bind_startup_to_session(monkeypatch: Any, session: Session) -> None:
    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    bind_startup_session_factory(monkeypatch, factory)
