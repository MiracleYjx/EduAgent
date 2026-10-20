"""T203: contract fixtures must run real recovery on their own SQLite engine."""
import asyncio

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from backend.app.api import grading, paper_import, workflow
from backend.app.core.app import _application_lifespan
from backend.app.core.database import Base
from tests.support.application_lifecycle import bind_startup_session_factory


def test_real_startup_recovery_uses_only_the_explicit_fixture(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'recovery.db'}")
    Base.metadata.create_all(engine)
    statements = []

    @event.listens_for(engine, "before_cursor_execute")
    def record(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    bind_startup_session_factory(monkeypatch, factory)
    for module in (grading, workflow, paper_import):
        with module.get_session_factory()() as session:
            assert session.get_bind() is engine

    application = FastAPI()

    async def run():
        async with _application_lifespan(application):
            assert application.state.recovery == "completed"

    try:
        asyncio.run(run())
        sql = "\n".join(statements)
        # Both M3 and M4 recovery use the shared WorkflowRun fact table.
        assert sql.count("from workflow_runs") >= 2
        assert "paper_imports" in sql
    finally:
        engine.dispose()


def test_missing_fixture_binding_is_rejected_without_creating_an_engine(monkeypatch):
    with pytest.raises(ValueError, match="EXPLICIT_SQLITE_FIXTURE_REQUIRED"):
        bind_startup_session_factory(monkeypatch, sessionmaker())


def test_unowned_postgres_factory_is_rejected_before_connect(monkeypatch):
    engine = create_engine("postgresql+psycopg://synthetic:synthetic@database.invalid:1/synthetic")
    attempts = []

    def blocked():
        attempts.append(True)
        raise AssertionError("must reject before a PostgreSQL connection")

    monkeypatch.setattr(engine, "connect", blocked)
    try:
        with pytest.raises(ValueError, match="EXPLICIT_OWNED_TEST_FIXTURE_REQUIRED"):
            bind_startup_session_factory(monkeypatch, sessionmaker(bind=engine))
        assert attempts == []
    finally:
        engine.dispose()


def test_fixture_restores_consumers_after_its_own_scope(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    originals = {module: module.get_session_factory for module in (grading, workflow, paper_import)}
    try:
        with monkeypatch.context() as patch:
            bind_startup_session_factory(patch, sessionmaker(bind=engine))
            for module, original in originals.items():
                assert module.get_session_factory is not original
        for module, original in originals.items():
            assert module.get_session_factory is original
    finally:
        engine.dispose()
