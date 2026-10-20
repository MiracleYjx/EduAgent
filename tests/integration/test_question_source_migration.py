"""P4B.3.1 TCR：仅在临时 PostgreSQL schema 运行 Alembic 升降级。"""

from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from backend.app.core import database
from backend.app.domain.enums import QuestionType
from backend.app.models import Course, Question, User
from tests.integration.legacy_question_fixture import insert_legacy_question
from tests.postgres_helpers import isolated_postgres_schema

ROOT = Path(__file__).resolve().parents[2]
REVISION = "0011_question_source_persistence"
OLD_REVISION = "0010_review_round_ids"
NEW_TABLES = {
    "question_source_chunks",
    "question_generation_metadata",
    "question_revision_comments",
}


@contextmanager
def _empty_isolated_schema() -> Iterator[tuple[Engine, str]]:
    with isolated_postgres_schema() as (engine, schema):
        with engine.begin() as connection:
            # public 已有开发库的 alembic_version。先在本 schema 建空版本表，
            # 避免 Alembic 沿 search_path 误读 public 的 head 后跳过本次迁移。
            assert connection.scalar(text("SELECT current_schema()")) == schema
            connection.execute(
                text(
                    f'CREATE TABLE "{schema}".alembic_version '
                    "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
                )
            )
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT current_schema()")) == schema
        yield engine, schema


def _revision(engine: Engine, expected_schema: str) -> str:
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT current_schema()")) == expected_schema
        assert (
            connection.scalar(text("SELECT to_regclass('alembic_version')")) is not None
        )
        return str(connection.scalar(text("SELECT version_num FROM alembic_version")))


def test_alembic_upgrade_head_downgrade_and_reupgrade_in_isolated_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _empty_isolated_schema() as (engine, schema):
        # migrations/env.py 从 core.database 导入引擎工厂；绑定本测试引擎后，
        # Alembic 的所有连接都进入随机 schema，不读取/修改开发 schema。
        engine_requests: list[bool] = []

        def isolated_engine() -> Engine:
            engine_requests.append(True)
            return engine

        monkeypatch.setattr(database, "create_database_engine", isolated_engine)
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "migrations"))
        command.upgrade(config, OLD_REVISION)
        assert engine_requests
        with engine.connect() as connection:
            assert "alembic_version" in inspect(connection).get_table_names(
                schema=schema
            )
        assert _revision(engine, schema) == OLD_REVISION
        with engine.connect() as connection:
            inspector = inspect(connection)
            old_question_indexes = {
                item["name"] for item in inspector.get_indexes("questions", schema=schema)
            }
            old_chunk_indexes = {
                item["name"]
                for item in inspector.get_indexes("document_chunks", schema=schema)
            }
            assert not NEW_TABLES & set(inspector.get_table_names(schema=schema))

        # 旧题目在迁移前存在，也不能凭空回填未知来源。
        with Session(engine) as session:
            teacher = User(
                username="historical",
                email="historical@example.com",
                password_hash="hash",
            )
            course = Course(name="历史课程", creator=teacher)
            session.add(course)
            session.flush()
            question_id = insert_legacy_question(
                session.connection(), course_id=course.id, created_by=teacher.id,
                type=QuestionType.SHORT_ANSWER.value, content="历史题", score=Decimal("10.00"),
            )
            session.commit()


        command.upgrade(config, REVISION)
        assert _revision(engine, schema) == REVISION
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT current_schema()")) == schema
            inspector = inspect(connection)
            assert NEW_TABLES <= set(inspector.get_table_names(schema=schema))
            assert old_question_indexes == {
                item["name"] for item in inspector.get_indexes("questions", schema=schema)
            }
            assert old_chunk_indexes == {
                item["name"]
                for item in inspector.get_indexes("document_chunks", schema=schema)
            }
            source_fks = {
                tuple(item["constrained_columns"]): (
                    item["referred_table"],
                    item["options"].get("ondelete"),
                )
                for item in inspector.get_foreign_keys(
                    "question_source_chunks", schema=schema
                )
            }
            assert source_fks == {
                ("question_id",): ("questions", "CASCADE"),
                ("live_chunk_id",): ("document_chunks", "SET NULL"),
            }
            assert {
                tuple(item["column_names"])
                for item in inspector.get_unique_constraints(
                    "question_source_chunks", schema=schema
                )
            } >= {("question_id", "chunk_id"), ("question_id", "source_order")}
            assert any(
                item["column_names"] == ["course_id", "question_id"]
                for item in inspector.get_indexes("question_source_chunks", schema=schema)
            )
            assert inspector.get_pk_constraint(
                "question_generation_metadata", schema=schema
            )["constrained_columns"] == ["question_id"]
            assert any(
                item["column_names"] == ["question_id", "commented_at"]
                for item in inspector.get_indexes(
                    "question_revision_comments", schema=schema
                )
            )
            assert {
                item["name"]
                for item in inspector.get_check_constraints(
                    "question_revision_comments", schema=schema
                )
            } >= {"ck_question_revision_comments_content_length"}
            for table in NEW_TABLES:
                assert connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0
            assert connection.scalar(select(func.count()).select_from(Question)) == 1

        command.downgrade(config, "-1")
        assert _revision(engine, schema) == OLD_REVISION
        with engine.connect() as connection:
            inspector = inspect(connection)
            assert not NEW_TABLES & set(inspector.get_table_names(schema=schema))
            assert (
                connection.scalar(select(Question.id).where(Question.id == question_id))
                == question_id
            )
            assert old_question_indexes == {
                item["name"] for item in inspector.get_indexes("questions", schema=schema)
            }
            assert old_chunk_indexes == {
                item["name"]
                for item in inspector.get_indexes("document_chunks", schema=schema)
            }

        command.upgrade(config, REVISION)
        assert _revision(engine, schema) == REVISION
        with engine.connect() as connection:
            assert NEW_TABLES <= set(inspect(connection).get_table_names(schema=schema))
            assert connection.scalar(select(func.count()).select_from(Question)) == 1
