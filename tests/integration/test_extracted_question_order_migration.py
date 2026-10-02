"""T157/T158 real migration: historical order unknown, uniqueness and safe downgrade. TCR §13."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, select
from sqlalchemy.exc import IntegrityError

from backend.app.core import database
from tests.integration.test_question_source_migration import _empty_isolated_schema


def test_extracted_order_migration(monkeypatch):
    with _empty_isolated_schema() as (engine, schema):
        monkeypatch.setattr(database, "create_database_engine", lambda: engine)
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        command.upgrade(config, "0016_asset_visibility")
        user_id, course_id, doc_id, import_id, question_id = (uuid4() for _ in range(5))
        now = datetime.now(UTC)

        def table(conn, name):
            return Table(name, MetaData(), autoload_with=conn, schema=schema)

        with engine.begin() as conn:
            conn.execute(
                table(conn, "users")
                .insert()
                .values(
                    id=user_id,
                    username="order",
                    email="order@test.invalid",
                    password_hash="unused",
                    is_active=True,
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                table(conn, "courses")
                .insert()
                .values(
                    id=course_id,
                    name="order",
                    created_by=user_id,
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                table(conn, "documents")
                .insert()
                .values(
                    id=doc_id,
                    course_id=course_id,
                    uploaded_by=user_id,
                    original_filename="paper.pdf",
                    file_format="pdf",
                    purpose="paper_source",
                    status="Ready",
                    retryable=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                table(conn, "paper_imports")
                .insert()
                .values(
                    id=import_id,
                    course_id=course_id,
                    uploaded_by=user_id,
                    document_id=doc_id,
                    original_filename="paper.pdf",
                )
            )
            conn.execute(
                table(conn, "extracted_questions")
                .insert()
                .values(
                    id=question_id,
                    paper_import_id=import_id,
                    source_page_ids=[],
                    extracted_by="LLM",
                    question_number="01",
                )
            )
        command.upgrade(config, "0017_extracted_order")
        with engine.begin() as conn:
            questions = table(conn, "extracted_questions")
            assert conn.execute(
                select(questions.c.order_index, questions.c.question_number).where(
                    questions.c.id == question_id
                )
            ).one() == (None, "01")
            conn.execute(
                questions.insert().values(
                    id=uuid4(),
                    paper_import_id=import_id,
                    source_page_ids=[],
                    extracted_by="LLM",
                )
            )
            conn.execute(
                questions.update()
                .where(questions.c.id == question_id)
                .values(order_index=1)
            )
            for value, state in [(0, "23514"), (1, "23505")]:
                with pytest.raises(IntegrityError) as error, conn.begin_nested():
                    conn.execute(
                        questions.insert().values(
                            id=uuid4(),
                            paper_import_id=import_id,
                            source_page_ids=[],
                            extracted_by="LLM",
                            order_index=value,
                        )
                    )
                assert error.value.orig.sqlstate == state
        with pytest.raises(RuntimeError, match="题序"):
            command.downgrade(config, "0016_asset_visibility")
        with engine.begin() as conn:
            conn.execute(questions.update().values(order_index=None))
        command.downgrade(config, "0016_asset_visibility")
        with engine.connect() as conn:
            assert "order_index" not in table(conn, "extracted_questions").c
        command.upgrade(config, "0017_extracted_order")
