"""T177 migration keeps historical identity unknown and foreign key real (TCR §32)."""

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
        # TCR（T081）：0022 时点的 questions/answers 等表尚无个人域列（如 subject_id），
        # 历史行必须按该时点真实表结构插入，不能用当前 ORM 模型假装旧库已存在新列。
        with engine.begin() as connection:
            metadata = MetaData()
            users = Table("users", metadata, autoload_with=connection)
            courses = Table("courses", metadata, autoload_with=connection)
            legacy_questions = Table("questions", metadata, autoload_with=connection)
            exams = Table("exams", metadata, autoload_with=connection)
            links = Table("exam_questions", metadata, autoload_with=connection)
            submissions = Table("submissions", metadata, autoload_with=connection)
            answers = Table("answers", metadata, autoload_with=connection)
            teacher_id, student_id, course_id = uuid4(), uuid4(), uuid4()
            connection.execute(
                users.insert(),
                [
                    {"id": teacher_id, "username": "historical-teacher",
                     "email": "teacher@historical.test", "password_hash": "fixture", "is_active": True},
                    {"id": student_id, "username": "historical-student",
                     "email": "student@historical.test", "password_hash": "fixture", "is_active": True},
                ],
            )
            connection.execute(
                courses.insert().values(id=course_id, name="historical", created_by=teacher_id)
            )
            objective_question_id = uuid4()
            connection.execute(
                legacy_questions.insert().values(
                    id=objective_question_id,
                    course_id=course_id,
                    created_by=teacher_id,
                    type="SINGLE_CHOICE",
                    content="下列哪个是不可变类型？",
                    options=["list", "tuple"],
                    reference_answer="tuple",
                    knowledge_points=["数据类型"],
                    score=Decimal("10.00"),
                    status="Approved",
                )
            )
            exam_id = uuid4()
            connection.execute(
                exams.insert().values(
                    id=exam_id, course_id=course_id, created_by=teacher_id,
                    title="historical", status="Published",
                )
            )
            link_id = uuid4()
            connection.execute(
                links.insert().values(
                    id=link_id, exam_id=exam_id, question_id=objective_question_id, order_index=1
                )
            )
            submission_id = uuid4()
            connection.execute(
                submissions.insert().values(
                    id=submission_id, exam_id=exam_id, student_id=student_id, status="Submitted"
                )
            )
            objective_answer_id = uuid4()
            connection.execute(
                answers.insert().values(
                    id=objective_answer_id, submission_id=submission_id,
                    question_id=objective_question_id, content="tuple", status="Graded",
                )
            )
            table = Table("grading_results", metadata, autoload_with=connection)
            connection.execute(
                table.insert().values(
                    id=row_id,
                    answer_id=objective_answer_id,
                    submission_id=submission_id,
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
