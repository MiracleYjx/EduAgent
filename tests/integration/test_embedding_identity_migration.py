"""T214 真实 Test PostgreSQL 迁移：只增可空身份列，历史向量不变。

使用随机 owned schema 与真实 Alembic 链路；升级前先建立历史 KB/chunk，验证
`embedding_identity` 保持 NULL、`vector(1024)`/HNSW 不变。降级刻意保留该列，
再次升级保持幂等。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from backend.app.core import database
from tests.postgres_helpers import isolated_postgres_schema

ROOT = Path(__file__).resolve().parents[2]
OLD_REVISION = "0023_grading_exam_question"
NEW_REVISION = "0024_embedding_index_identity"


@contextmanager
def _empty_isolated_schema() -> Iterator[tuple[Engine, str]]:
    with isolated_postgres_schema() as (engine, schema):
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT current_schema()")) == schema
            connection.execute(
                text(
                    f'CREATE TABLE "{schema}".alembic_version '
                    "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
                )
            )
        yield engine, schema


def _revision(engine: Engine, expected_schema: str) -> str:
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT current_schema()")) == expected_schema
        return str(connection.scalar(text("SELECT version_num FROM alembic_version")))


def _vector_literal() -> str:
    return "[" + ",".join("0.125" for _ in range(1024)) + "]"


def _seed_legacy(engine: Engine) -> tuple[str, str, str, str]:
    user_id, course_id, kb_id, document_id, chunk_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    vector = _vector_literal()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, username, email, password_hash, is_active) "
                "VALUES (:id, :username, :email, :password_hash, true)"
            ),
            {
                "id": user_id,
                "username": f"t214-{uuid4().hex[:12]}",
                "email": f"t214-{uuid4().hex[:12]}@example.com",
                "password_hash": "synthetic",
            },
        )
        connection.execute(
            text(
                "INSERT INTO courses (id, name, description, created_by) "
                "VALUES (:id, :name, NULL, :created_by)"
            ),
            {"id": course_id, "name": "T214 迁移课程", "created_by": user_id},
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, course_id, name, description) "
                "VALUES (:id, :course_id, :name, NULL)"
            ),
            {"id": kb_id, "course_id": course_id, "name": "T214 历史库"},
        )
        connection.execute(
            text(
                "INSERT INTO documents "
                "(id, course_id, knowledge_base_id, uploaded_by, original_filename, "
                " file_format, storage_path, file_metadata, purpose, status, "
                " error_code, error_message, retryable) "
                "VALUES (:id, :course_id, :knowledge_base_id, :uploaded_by, "
                "'t214.txt', 'txt', NULL, NULL, 'knowledge_base', 'Ready', "
                "NULL, NULL, false)"
            ),
            {
                "id": document_id,
                "course_id": course_id,
                "knowledge_base_id": kb_id,
                "uploaded_by": user_id,
            },
        )
        connection.execute(
            text(
                "INSERT INTO document_chunks "
                "(id, document_id, course_id, knowledge_base_id, chunk_index, "
                " content, embedding, search_vector, metadata) "
                "VALUES (:id, :document_id, :course_id, :knowledge_base_id, 0, "
                "'历史向量', CAST(:embedding AS vector), NULL, CAST(:metadata AS json))"
            ),
            {
                "id": chunk_id,
                "document_id": document_id,
                "course_id": course_id,
                "knowledge_base_id": kb_id,
                "embedding": vector,
                "metadata": json.dumps({"source": "t214-migration"}),
            },
        )
    return str(user_id), str(course_id), str(kb_id), str(document_id)


def _vector_type(engine: Engine) -> str:
    """声明类型必须保持 ``vector(1024)``，不能由迁移改写。"""

    with engine.connect() as connection:
        return str(
            connection.scalar(
                text(
                    "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                    "WHERE attrelid = 'document_chunks'::regclass "
                    "AND attname = 'embedding'"
                )
            )
        )


def _vector_digest(engine: Engine, kb_id: str) -> tuple[str, str, int]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT md5(embedding::text), vector_dims(embedding) "
                "FROM document_chunks WHERE knowledge_base_id = :kb"
            ),
            {"kb": kb_id},
        ).one()
    return str(row[0]), _vector_type(engine), int(row[1])


def _identity_column(engine: Engine, schema: str) -> tuple[str, str] | None:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT data_type, is_nullable FROM information_schema.columns "
                "WHERE table_schema = :schema AND table_name = 'knowledge_bases' "
                "AND column_name = 'embedding_identity'"
            ),
            {"schema": schema},
        ).one_or_none()
    return None if row is None else (str(row[0]), str(row[1]))


def test_identity_migration_keeps_legacy_vectors_and_rollback_keeps_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _empty_isolated_schema() as (engine, schema):
        requests: list[bool] = []

        def isolated_engine() -> Engine:
            requests.append(True)
            return engine

        monkeypatch.setattr(database, "create_database_engine", isolated_engine)
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "migrations"))
        command.upgrade(config, OLD_REVISION)
        assert requests and _revision(engine, schema) == OLD_REVISION
        assert _identity_column(engine, schema) is None
        _, _, kb_id, _ = _seed_legacy(engine)
        before = _vector_digest(engine, kb_id)

        command.upgrade(config, NEW_REVISION)
        assert _revision(engine, schema) == NEW_REVISION
        assert _identity_column(engine, schema) == ("jsonb", "YES")
        assert _vector_digest(engine, kb_id) == before
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT embedding_identity FROM knowledge_bases WHERE id = :kb"),
                {"kb": kb_id},
            ) is None
            index_def = connection.scalar(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE schemaname = :schema "
                    "AND indexname = 'ix_document_chunks_embedding_hnsw'"
                ),
                {"schema": schema},
            )
            assert index_def is not None and "vector_cosine_ops" in str(index_def)

        command.downgrade(config, "-1")
        assert _revision(engine, schema) == OLD_REVISION
        assert _identity_column(engine, schema) == ("jsonb", "YES")
        assert _vector_digest(engine, kb_id) == before

        command.upgrade(config, NEW_REVISION)
        assert _revision(engine, schema) == NEW_REVISION
        assert _identity_column(engine, schema) == ("jsonb", "YES")
        assert _vector_digest(engine, kb_id) == before
        with engine.connect() as connection:
            columns = {
                item["name"]
                for item in inspect(connection).get_columns(
                    "knowledge_bases", schema=schema
                )
            }
            assert "embedding_identity" in columns
