"""T177 migration keeps historical identity unknown and foreign key real (TCR §32)."""

from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.core import database
from backend.app.models import ExamQuestion
from tests.integration.test_question_source_migration import _empty_isolated_schema
from tests.unit.models.sqlite_support import seed_submission

ROOT = Path(__file__).resolve().parents[2]
OLD = "0022_exam_question"
NEW = "0023_grading_exam_question"


def test_upgrade_preserves_result_pk_score_unknown_link_and_real_restrict_fk(
    monkeypatch,
):
    with _empty_isolated_schema() as (engine, schema):
        monkeypatch.setattr(database, "create_database_engine", lambda: engine)
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "migrations"))
        command.upgrade(config, OLD)
        row_id = uuid4()
        with Session(engine) as session:
            info = seed_submission(session)
            table = Table(
                "grading_results", MetaData(), autoload_with=session.connection()
            )
            session.execute(
                table.insert().values(
                    id=row_id,
                    answer_id=info.objective_answer_id,
                    submission_id=info.submission_id,
                    question_type="SINGLE_CHOICE",
                    score=Decimal("0.01"),
                    max_score=Decimal("10.00"),
                    reason="Historical fixture",
                    correct_points=[],
                    missing_knowledge_points=[],
                    knowledge_points=["original"],
                    suggestions=[],
                    retrieved_context_ids=[],
                    confidence=1,
                    validation_status="Validated",
                    review_status="Not Required",
                )
            )
            link_id = session.scalar(
                select(ExamQuestion.id).where(
                    ExamQuestion.question_id == info.objective_question_id
                )
            )
            session.commit()
        command.upgrade(config, NEW)
        with engine.connect() as connection:
            actual = (
                connection.execute(
                    text(
                        "SELECT id,score,max_score,exam_question_id FROM grading_results WHERE id=:id"
                    ),
                    {"id": row_id},
                )
                .mappings()
                .one()
            )
            assert actual == {
                "id": row_id,
                "score": Decimal("0.01"),
                "max_score": Decimal("10.00"),
                "exam_question_id": None,
            }
            column = next(
                c
                for c in inspect(connection).get_columns(
                    "grading_results", schema=schema
                )
                if c["name"] == "exam_question_id"
            )
            assert column["nullable"] is True
            fk = next(
                f
                for f in inspect(connection).get_foreign_keys(
                    "grading_results", schema=schema
                )
                if f["constrained_columns"] == ["exam_question_id"]
            )
            assert (
                fk["referred_table"] == "exam_questions"
                and fk["options"]["ondelete"] == "RESTRICT"
            )
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text("UPDATE grading_results SET exam_question_id=:link WHERE id=:id"),
                {"link": uuid4(), "id": row_id},
            )
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE grading_results SET exam_question_id=:link WHERE id=:id"),
                {"link": link_id, "id": row_id},
            )
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text("DELETE FROM exam_questions WHERE id=:id"), {"id": link_id}
            )
        command.downgrade(config, OLD)
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT score FROM grading_results WHERE id=:id"), {"id": row_id}
            ) == Decimal("0.01")
        command.upgrade(config, NEW)
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT exam_question_id FROM grading_results WHERE id=:id"),
                    {"id": row_id},
                )
                is None
            )
