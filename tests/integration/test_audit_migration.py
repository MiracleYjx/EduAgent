"""T084 Alembic audit migration in an isolated PostgreSQL schema."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from backend.app.core import database
from tests.integration.test_question_source_migration import _empty_isolated_schema

ROOT = Path(__file__).resolve().parents[2]


def test_audit_migration_upgrade_head_and_downgrade(monkeypatch) -> None:
    with _empty_isolated_schema() as (engine, _schema):
        monkeypatch.setattr(database, "create_database_engine", lambda: engine)
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "migrations"))
        command.upgrade(config, "0011_question_source_persistence")
        with engine.connect() as connection:
            assert "audit_logs" not in inspect(connection).get_table_names(schema=_schema)
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert "audit_logs" in inspect(connection).get_table_names(schema=_schema)
        command.downgrade(config, "0011_question_source_persistence")
        with engine.connect() as connection:
            assert "audit_logs" not in inspect(connection).get_table_names(schema=_schema)
