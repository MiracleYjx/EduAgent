"""P4B.3.1 TCR：仅在临时 PostgreSQL schema 运行 Alembic 升降级。"""

from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, func, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema

from backend.app.core import database
from backend.app.domain.enums import QuestionType
from backend.app.models import Course, Question, User

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
    schema = f"test_source_migration_{uuid4().hex}"
    engine = database.create_database_engine(connect_timeout=3)
    created = False

    @event.listens_for(engine, "connect")
    def set_schema(connection, _record) -> None:
        with connection.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{schema}", public')
        connection.commit()

    try:
        try:
            with engine.connect() as connection:
                installed = connection.scalar(
                    text("SELECT 1 FROM pg_extension WHERE extname='vector'")
                )
        except SQLAlchemyError as exc:
            pytest.skip(f"PostgreSQL 不可用：{type(exc).__name__}")
        if not installed:
            pytest.skip("缺少 pgvector 扩展")
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
            # public 已有开发库的 alembic_version。先在本 schema 建空版本表，
            # 避免 Alembic 沿 search_path 误读 public 的 head 后跳过本次迁移。
            assert connection.scalar(text("SELECT current_schema()")) == schema
            connection.execute(
                text(
                    "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
                )
            )
        created = True
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT current_schema()")) == schema
        yield engine, schema
    finally:
        if created:
            with engine.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
        engine.dispose()


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
                item["name"] for item in inspector.get_indexes("questions")
            }
            old_chunk_indexes = {
                item["name"] for item in inspector.get_indexes("document_chunks")
            }
            assert not NEW_TABLES & set(inspector.get_table_names())

        # 旧题目在迁移前存在，也不能凭空回填未知来源。
        with Session(engine) as session:
            teacher = User(
                username="historical",
                email="historical@example.com",
                password_hash="hash",
            )
            course = Course(name="历史课程", creator=teacher)
            question = Question(
                course=course,
                creator=teacher,
                type=QuestionType.SHORT_ANSWER,
                content="历史题",
                score=Decimal("10.00"),
            )
            session.add(question)
            session.commit()
            question_id = question.id

        command.upgrade(config, "head")
        assert _revision(engine, schema) == REVISION
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT current_schema()")) == schema
            inspector = inspect(connection)
            assert NEW_TABLES <= set(inspector.get_table_names())
            assert old_question_indexes == {
                item["name"] for item in inspector.get_indexes("questions")
            }
            assert old_chunk_indexes == {
                item["name"] for item in inspector.get_indexes("document_chunks")
            }
            source_fks = {
                tuple(item["constrained_columns"]): (
                    item["referred_table"],
                    item["options"].get("ondelete"),
                )
                for item in inspector.get_foreign_keys("question_source_chunks")
            }
            assert source_fks == {
                ("question_id",): ("questions", "CASCADE"),
                ("live_chunk_id",): ("document_chunks", "SET NULL"),
            }
            assert {
                tuple(item["column_names"])
                for item in inspector.get_unique_constraints("question_source_chunks")
            } >= {("question_id", "chunk_id"), ("question_id", "source_order")}
            assert any(
                item["column_names"] == ["course_id", "question_id"]
                for item in inspector.get_indexes("question_source_chunks")
            )
            assert inspector.get_pk_constraint("question_generation_metadata")[
                "constrained_columns"
            ] == ["question_id"]
            assert any(
                item["column_names"] == ["question_id", "commented_at"]
                for item in inspector.get_indexes("question_revision_comments")
            )
            assert {
                item["name"]
                for item in inspector.get_check_constraints(
                    "question_revision_comments"
                )
            } >= {"ck_question_revision_comments_content_length"}
            for table in NEW_TABLES:
                assert connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0
            assert connection.scalar(select(func.count()).select_from(Question)) == 1

        command.downgrade(config, "-1")
        assert _revision(engine, schema) == OLD_REVISION
        with engine.connect() as connection:
            inspector = inspect(connection)
            assert not NEW_TABLES & set(inspector.get_table_names())
            assert (
                connection.scalar(select(Question.id).where(Question.id == question_id))
                == question_id
            )
            assert old_question_indexes == {
                item["name"] for item in inspector.get_indexes("questions")
            }
            assert old_chunk_indexes == {
                item["name"] for item in inspector.get_indexes("document_chunks")
            }

        command.upgrade(config, "head")
        assert _revision(engine, schema) == REVISION
        with engine.connect() as connection:
            assert NEW_TABLES <= set(inspect(connection).get_table_names())
            assert connection.scalar(select(func.count()).select_from(Question)) == 1
