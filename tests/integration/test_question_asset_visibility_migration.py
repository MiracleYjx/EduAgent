"""T154 real PostgreSQL visibility default and upgrade/downgrade. TCR §11."""
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, inspect, select
from sqlalchemy.exc import IntegrityError

from backend.app.core import database
from tests.integration.test_question_source_migration import _empty_isolated_schema

ROOT = Path(__file__).resolve().parents[2]


def test_visibility_migration_defaults_preserves_assets_and_reverses(monkeypatch):
    with _empty_isolated_schema() as (engine, schema):
        monkeypatch.setattr(database, "create_database_engine", lambda: engine)
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "migrations"))
        command.upgrade(config, "0015_question_assets")
        user_id, course_id, question_id, asset_id = (uuid4() for _ in range(4))
        now = datetime.now(UTC)
        def table(conn, name):
            return Table(name, MetaData(), autoload_with=conn, schema=schema)
        with engine.begin() as conn:
            conn.execute(table(conn, "users").insert().values(id=user_id, username="visibility_migration", email="visibility@migration.test", password_hash="unused", is_active=True, created_at=now, updated_at=now))
            conn.execute(table(conn, "courses").insert().values(id=course_id, name="visibility", created_by=user_id, created_at=now, updated_at=now))
            conn.execute(table(conn, "questions").insert().values(id=question_id, course_id=course_id, type="SHORT_ANSWER", content="legacy", knowledge_points=[], score="7.25", status="Draft", created_by=user_id, created_at=now, updated_at=now))
            conn.execute(table(conn, "question_assets").insert().values(id=asset_id, question_id=question_id, asset_type="figure", file_path="assets/legacy.png", width=120, height=80, order_index=1))
        command.upgrade(config, "0016_asset_visibility")
        with engine.begin() as conn:
            assets = table(conn, "question_assets")
            row = conn.execute(select(assets).where(assets.c.id == asset_id)).one()
            assert row.student_visible is False and row.file_path == "assets/legacy.png"
            assert (row.width, row.height, row.order_index) == (120, 80, 1)
            column = next(c for c in inspect(conn).get_columns("question_assets", schema=schema) if c["name"] == "student_visible")
            assert str(column["type"]) == "BOOLEAN" and not column["nullable"]
            second_id = uuid4()
            conn.execute(assets.insert().values(id=second_id, question_id=question_id, asset_type="table", file_path="assets/new.png", width=20, height=30, order_index=2))
            assert conn.scalar(select(assets.c.student_visible).where(assets.c.id == second_id)) is False
            with pytest.raises(IntegrityError) as error, conn.begin_nested():
                conn.execute(assets.update().where(assets.c.id == asset_id).values(student_visible=None))
            assert error.value.orig.sqlstate == "23502"
            conn.execute(assets.update().where(assets.c.id == asset_id).values(student_visible=True))
        # An open grant cannot be silently lost and recreated false by a downgrade.
        with pytest.raises(RuntimeError, match="学生展示"):
            command.downgrade(config, "0015_question_assets")
        with engine.begin() as conn:
            assets = table(conn, "question_assets")
            conn.execute(assets.update().values(student_visible=False))
        command.downgrade(config, "0015_question_assets")
        with engine.connect() as conn:
            assets = table(conn, "question_assets")
            assert "student_visible" not in assets.c
            assert conn.scalar(select(assets.c.file_path).where(assets.c.id == asset_id)) == "assets/legacy.png"
        command.upgrade(config, "0016_asset_visibility")
        with engine.connect() as conn:
            assets = table(conn, "question_assets")
            assert conn.scalar(select(assets.c.student_visible).where(assets.c.id == asset_id)) is False
