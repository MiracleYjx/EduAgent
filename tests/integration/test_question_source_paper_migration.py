"""T165 actual upgrade/downgrade, parent foreign keys and historical unknown preservation."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, inspect, select
from sqlalchemy.exc import IntegrityError

from backend.app.core import database
from tests.integration.legacy_question_fixture import insert_legacy_question
from tests.integration.test_question_source_migration import _empty_isolated_schema


def test_parent_source_migration_preserves_history_and_constraints(monkeypatch):
    with _empty_isolated_schema() as (engine, schema):
        monkeypatch.setattr(database, "create_database_engine", lambda: engine)
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        command.upgrade(config, "0020_content_validation")
        teacher_id, course_id = uuid4(), uuid4()
        now = datetime.now(UTC)
        with engine.begin() as conn:
            users = Table("users", MetaData(), autoload_with=conn, schema=schema)
            courses = Table("courses", MetaData(), autoload_with=conn, schema=schema)
            conn.execute(
                users.insert().values(
                    id=teacher_id,
                    username="migration165",
                    email="migration165@example.com",
                    password_hash="unused",
                    is_active=True,
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                courses.insert().values(
                    id=course_id,
                    name="T165 migration",
                    created_by=teacher_id,
                    created_at=now,
                    updated_at=now,
                )
            )
            parent = insert_legacy_question(
                conn,
                course_id=course_id,
                type="SINGLE_CHOICE",
                content="Original",
                score=2,
                created_by=teacher_id,
            )
            child = insert_legacy_question(
                conn,
                course_id=course_id,
                type="SHORT_ANSWER",
                content="Derived",
                score=3,
                created_by=teacher_id,
            )
        command.upgrade(config, "0021_question_source_paper")
        with engine.begin() as conn:
            questions = Table(
                "questions", MetaData(), autoload_with=conn, schema=schema
            )
            edges = Table(
                "question_source_papers", MetaData(), autoload_with=conn, schema=schema
            )
            assert conn.execute(
                select(questions.c.source_type, questions.c.content).where(
                    questions.c.id == parent
                )
            ).one() == (None, "Original")
            conn.execute(
                edges.insert().values(
                    id=uuid4(),
                    derived_question_id=child,
                    source_question_id=parent,
                    adaptation_type="rewrite",
                )
            )
            for values in (
                {
                    "derived_question_id": child,
                    "source_question_id": parent,
                    "adaptation_type": "rewrite",
                },
                {
                    "derived_question_id": parent,
                    "source_question_id": parent,
                    "adaptation_type": "rewrite",
                },
                {
                    "derived_question_id": parent,
                    "source_question_id": child,
                    "adaptation_type": "unknown",
                },
                {
                    "derived_question_id": child,
                    "source_question_id": uuid4(),
                    "adaptation_type": "rewrite",
                },
            ):
                with pytest.raises(IntegrityError), conn.begin_nested():
                    conn.execute(edges.insert().values(id=uuid4(), **values))
            with pytest.raises(IntegrityError), conn.begin_nested():
                conn.execute(questions.delete().where(questions.c.id == parent))
            fks = inspect(conn).get_foreign_keys(
                "question_source_papers", schema=schema
            )
            assert {fk["options"]["ondelete"] for fk in fks} == {"RESTRICT"}
        command.downgrade(config, "0020_content_validation")
        with engine.connect() as conn:
            assert not inspect(conn).has_table("question_source_papers", schema=schema)
        command.upgrade(config, "0021_question_source_paper")
        with engine.connect() as conn:
            questions = Table(
                "questions", MetaData(), autoload_with=conn, schema=schema
            )
            assert (
                conn.execute(
                    select(questions.c.content).where(questions.c.id == parent)
                ).scalar_one()
                == "Original"
            )
