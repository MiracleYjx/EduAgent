"""P3.1.1 在隔离 PostgreSQL schema 执行真实 Alembic CLI，不修改默认 schema。"""

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from sqlalchemy import MetaData, Table, event, inspect, text
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema

from backend.app.core.database import create_database_engine
from backend.app.domain.enums import ReviewStatus
from tests.unit.models.sqlite_support import seed_submission
from tests.unit.models.test_grading_result import _grading_result


def test_review_round_migration_upgrade_downgrade_preserves_legacy_rows() -> None:
    engine = create_database_engine()
    schema = f"test_review_round_{uuid4().hex}"
    created = False

    @event.listens_for(engine, "connect")
    def set_schema(connection, _record) -> None:
        with connection.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{schema}", public')
        connection.commit()

    root = Path(__file__).resolve().parents[2]
    migration_env = {**os.environ, "PGOPTIONS": f"-csearch_path={schema},public"}

    def alembic(*args: str) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=root,
            env=migration_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    try:
        with engine.begin() as connection:
            assert connection.scalar(
                text("SELECT 1 FROM pg_extension WHERE extname='vector'")
            )
            connection.execute(CreateSchema(schema))
            created = True
            # 遮蔽 public 中可能已有的 Alembic 版本表；迁移只能操作本次 schema。
            connection.execute(
                text(
                    f'CREATE TABLE "{schema}".alembic_version '
                    "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
                )
            )
        alembic("upgrade", "0009_agent_runs")
        with Session(engine) as session:
            fixture = seed_submission(session)
        metadata = MetaData()
        grading = Table("grading_results", metadata, autoload_with=engine)
        reviews = Table("review_records", metadata, autoload_with=engine)
        old_grade = _grading_result(
            fixture.subjective_answer_id,
            fixture.submission_id,
            review_status=ReviewStatus.PENDING_REVIEW,
        )
        grading_id, record_id = uuid4(), uuid4()
        with engine.begin() as connection:
            connection.execute(
                grading.insert().values(
                    id=grading_id,
                    **{
                        col.name: getattr(old_grade, col.name)
                        for col in grading.columns
                        if col.name not in {"id", "created_at", "updated_at"}
                    },
                )
            )
            connection.execute(
                reviews.insert().values(
                    id=record_id,
                    grading_result_id=grading_id,
                    reviewer_id=fixture.teacher_id,
                    decision=ReviewStatus.MODIFIED.value,
                    original_score=6,
                    original_reason="旧评分",
                    original_knowledge_points=[],
                    final_score=8,
                    final_reason="旧复核",
                    final_knowledge_points=[],
                )
            )
        alembic("upgrade", "head")
        inspector = inspect(engine)
        for table, field in [
            ("grading_results", "pending_review_round_id"),
            ("review_records", "review_round_id"),
        ]:
            column = next(
                c
                for c in inspector.get_columns(table, schema=schema)
                if c["name"] == field
            )
            assert column["nullable"] is True
            assert str(column["type"]) == "UUID"
        index = next(
            i
            for i in inspector.get_indexes("grading_results", schema=schema)
            if i["name"] == "ix_grading_results_pending_review_round_id"
        )
        assert index["unique"] is False
        assert index["column_names"] == ["pending_review_round_id"]
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "0010_review_round_ids"
            )
            assert connection.execute(
                text("SELECT score, pending_review_round_id FROM grading_results")
            ).one() == (8, None)
            assert connection.execute(
                text("SELECT final_score, review_round_id FROM review_records")
            ).one() == (8, None)
        alembic("downgrade", "-1")
        inspector = inspect(engine)
        assert "pending_review_round_id" not in {
            c["name"] for c in inspector.get_columns("grading_results", schema=schema)
        }
        assert "review_round_id" not in {
            c["name"] for c in inspector.get_columns("review_records", schema=schema)
        }
        assert "ix_grading_results_pending_review_round_id" not in {
            i["name"] for i in inspector.get_indexes("grading_results", schema=schema)
        }
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "0009_agent_runs"
            )
            assert (
                connection.scalar(text("SELECT id FROM grading_results")) == grading_id
            )
            assert connection.scalar(text("SELECT id FROM review_records")) == record_id
        alembic("upgrade", "head")
    finally:
        if created:
            with engine.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
        engine.dispose()
