"""T161 real PostgreSQL nullable locations, legacy preservation and FK lifecycle."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, inspect, select, text
from sqlalchemy.exc import IntegrityError

from backend.app.core import database
from tests.integration.test_question_source_migration import _empty_isolated_schema


def test_chapter_migration_preserves_unknown_legacy_data_and_enforces_storage(
    monkeypatch,
) -> None:
    with _empty_isolated_schema() as (engine, schema):
        monkeypatch.setattr(database, "create_database_engine", lambda: engine)
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        command.upgrade(config, "0018_options_json")
        user, course, knowledge, document, chunk, chapter = [uuid4() for _ in range(6)]

        def table(connection, name):
            return Table(name, MetaData(), autoload_with=connection, schema=schema)

        with engine.begin() as connection:
            connection.execute(
                table(connection, "users")
                .insert()
                .values(
                    id=user,
                    username="scope",
                    email="scope@test.invalid",
                    password_hash="unused",
                    is_active=True,
                )
            )
            connection.execute(
                table(connection, "courses")
                .insert()
                .values(id=course, name="Scope", created_by=user)
            )
            connection.execute(
                table(connection, "knowledge_bases")
                .insert()
                .values(id=knowledge, course_id=course, name="Material")
            )
            connection.execute(
                table(connection, "documents")
                .insert()
                .values(
                    id=document,
                    course_id=course,
                    knowledge_base_id=knowledge,
                    uploaded_by=user,
                    original_filename="lesson.txt",
                    file_format="txt",
                    status="Ready",
                    retryable=False,
                )
            )
            legacy = {
                "section_index": 99,
                "knowledge_points": {"legacy": "unconfirmed"},
                "custom": [3, 1],
            }
            connection.execute(
                table(connection, "document_chunks")
                .insert()
                .values(
                    id=chunk,
                    document_id=document,
                    course_id=course,
                    knowledge_base_id=knowledge,
                    chunk_index=0,
                    content="Legacy source",
                    metadata=legacy,
                )
            )
            original = connection.scalar(
                text("SELECT metadata::text FROM document_chunks WHERE id=:id"),
                {"id": chunk},
            )

        command.upgrade(config, "0019_chapter_scope")
        with engine.begin() as connection:
            chunks = table(connection, "document_chunks")
            row = connection.execute(select(chunks).where(chunks.c.id == chunk)).one()
            assert row.chapter_id is None and row.section_order is None
            assert row.metadata == legacy
            assert (
                connection.scalar(
                    text("SELECT metadata::text FROM document_chunks WHERE id=:id"),
                    {"id": chunk},
                )
                == original
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT pg_typeof(metadata)::text FROM document_chunks LIMIT 1"
                    )
                )
                == "json"
            )
            assert connection.scalar(text("SELECT count(*) FROM chapters")) == 0
            indexes = {
                item["name"]
                for item in inspect(connection).get_indexes(
                    "document_chunks", schema=schema
                )
            }
            assert {
                "ix_document_chunks_embedding_hnsw",
                "ix_document_chunks_search_vector_gin",
                "ix_document_chunks_course_chapter_section",
            } <= indexes
            scope_index = next(
                item
                for item in inspect(connection).get_indexes(
                    "document_chunks", schema=schema
                )
                if item["name"] == "ix_document_chunks_course_chapter_section"
            )
            assert scope_index["column_names"] == [
                "course_id",
                "chapter_id",
                "section_order",
            ]

            for changes in (
                {"section_order": 1},
                {"chapter_id": uuid4()},
                {"chapter_id": None, "section_order": -1},
            ):
                with pytest.raises(IntegrityError), connection.begin_nested():
                    connection.execute(
                        chunks.update().where(chunks.c.id == chunk).values(**changes)
                    )
            chapters = table(connection, "chapters")
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(
                    chapters.insert().values(
                        id=chapter,
                        course_id=course,
                        title="Chapter",
                        sections={},
                        confirmed_by=user,
                        confirmed_at=datetime.now(UTC),
                    )
                )
            connection.execute(
                chapters.insert().values(
                    id=chapter,
                    course_id=course,
                    title="Chapter",
                    sections=[],
                    confirmed_by=user,
                    confirmed_at=datetime.now(UTC),
                )
            )
            connection.execute(
                chunks.update().where(chunks.c.id == chunk).values(chapter_id=chapter)
            )
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(chapters.delete().where(chapters.c.id == chapter))

        with pytest.raises(RuntimeError, match="chapter data"):
            command.downgrade(config, "0018_options_json")
        with engine.begin() as connection:
            connection.execute(
                chunks.update().where(chunks.c.id == chunk).values(chapter_id=None)
            )
            connection.execute(chapters.delete().where(chapters.c.id == chapter))
        command.downgrade(config, "0018_options_json")
        with engine.connect() as connection:
            assert "chapter_id" not in table(connection, "document_chunks").c
            assert (
                connection.scalar(
                    text("SELECT metadata::text FROM document_chunks WHERE id=:id"),
                    {"id": chunk},
                )
                == original
            )
        command.upgrade(config, "0019_chapter_scope")
