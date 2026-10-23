"""T170 original-table migration integrity; TCR §25."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, inspect, text
from sqlalchemy.exc import IntegrityError

from backend.app.core import database
from tests.integration.test_question_source_migration import _empty_isolated_schema

ROOT = Path(__file__).resolve().parents[2]
OLD = "0021_question_source_paper"
NEW = "0022_exam_question"


def test_upgrade_preserves_pairs_real_order_and_unknown_historical_basis(monkeypatch):
    with _empty_isolated_schema() as (engine, schema):
        monkeypatch.setattr(database, "create_database_engine", lambda: engine)
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "migrations"))
        command.upgrade(config, OLD)
        # TCR（T081）：0021 时点的 questions 尚无后续个人域列（如 subject_id），
        # 历史行必须按该时点表结构插入，不能用当前 ORM 模型（会引用后续迁移列）。
        with engine.begin() as connection:
            metadata = MetaData()
            users = Table("users", metadata, autoload_with=connection)
            courses = Table("courses", metadata, autoload_with=connection)
            legacy_questions = Table("questions", metadata, autoload_with=connection)
            exams = Table("exams", metadata, autoload_with=connection)
            links = Table("exam_questions", metadata, autoload_with=connection)
            teacher_id = uuid4()
            connection.execute(
                users.insert().values(
                    id=teacher_id,
                    username="historical",
                    email="historical@example.test",
                    password_hash="fixture",
                    is_active=True,
                )
            )
            course_id = uuid4()
            connection.execute(
                courses.insert().values(
                    id=course_id, name="historical", created_by=teacher_id
                )
            )
            now = datetime.now(UTC)
            question_ids = []
            for index in range(2):
                question_id = uuid4()
                question_ids.append(question_id)
                connection.execute(
                    legacy_questions.insert().values(
                        id=question_id,
                        course_id=course_id,
                        created_by=teacher_id,
                        type="SHORT_ANSWER",
                        content=str(index),
                        knowledge_points=[],
                        score=Decimal("7.00"),
                        status="Draft",
                        created_at=now + timedelta(seconds=index),
                    )
                )
            exam_ids = [uuid4(), uuid4()]
            for exam_id, status in zip(exam_ids, ["Draft", "Published"], strict=True):
                connection.execute(
                    exams.insert().values(
                        id=exam_id,
                        course_id=course_id,
                        created_by=teacher_id,
                        title=status,
                        status=status,
                    )
                )
                for question_id in reversed(question_ids):
                    connection.execute(
                        links.insert().values(exam_id=exam_id, question_id=question_id)
                    )
        command.upgrade(config, NEW)
        with engine.connect() as connection:
            rows = (
                connection.execute(
                    text("SELECT * FROM exam_questions ORDER BY exam_id, order_index")
                )
                .mappings()
                .all()
            )
            assert len(rows) == 4 and len({row["id"] for row in rows}) == 4
            for exam_id in exam_ids:
                current = [r for r in rows if r["exam_id"] == exam_id]
                assert [r["question_id"] for r in current] == question_ids
                assert [r["order_index"] for r in current] == [1, 2]
            assert all(
                r[key] is None
                for r in rows
                for key in [
                    "score",
                    "base_score",
                    "published_knowledge_points",
                    "scoring_basis",
                ]
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM exams WHERE assembly_constraints IS NULL"
                    )
                )
                == 2
            )
            actual = inspect(connection)
            assert actual.get_pk_constraint("exam_questions", schema=schema)[
                "constrained_columns"
            ] == ["id"]
            assert {
                tuple(item["column_names"])
                for item in actual.get_unique_constraints(
                    "exam_questions", schema=schema
                )
            } == {("exam_id", "question_id"), ("exam_id", "order_index")}
            assert {
                tuple(item["constrained_columns"]): item["options"]["ondelete"]
                for item in actual.get_foreign_keys("exam_questions", schema=schema)
            } == {("exam_id",): "CASCADE", ("question_id",): "RESTRICT"}
        for sql in [
            "UPDATE exams SET assembly_constraints='null'::jsonb",
            "UPDATE exam_questions SET scoring_basis='[]'::jsonb",
            "UPDATE exam_questions SET published_knowledge_points='null'::jsonb",
            "UPDATE exam_questions SET order_index=1",
        ]:
            with pytest.raises(IntegrityError), engine.begin() as connection:
                connection.execute(text(sql))
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE exam_questions SET score=3.00 WHERE id=:id"),
                {"id": rows[0]["id"]},
            )
        with pytest.raises(RuntimeError, match="降级"):
            command.downgrade(config, OLD)
        with engine.begin() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == NEW
            )
            connection.execute(text("UPDATE exam_questions SET score=NULL"))
        command.downgrade(config, OLD)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM exam_questions")) == 4
            assert inspect(connection).get_pk_constraint(
                "exam_questions", schema=schema
            )["constrained_columns"] == ["exam_id", "question_id"]
        command.upgrade(config, NEW)
        with engine.begin() as connection:
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(
                    text("DELETE FROM questions WHERE id=:id"), {"id": question_ids[0]}
                )
            connection.execute(
                text("DELETE FROM exams WHERE id=:id"), {"id": exam_ids[0]}
            )
            assert connection.scalar(text("SELECT count(*) FROM exam_questions")) == 2
