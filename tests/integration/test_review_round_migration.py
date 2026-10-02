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
from backend.app.models import Answer, Course, Exam, Role, Submission, User
from tests.integration.legacy_question_fixture import insert_legacy_question
from tests.unit.models.sqlite_support import SubmissionFixture
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
            teacher = User(username="teacher", email="teacher@example.com", password_hash="hashed-password", roles=[Role(name="Teacher")])
            student = User(username="student", email="student@example.com", password_hash="hashed-password", roles=[Role(name="Student")])
            course = Course(name="Python 基础", creator=teacher)
            session.add_all([student, course])
            session.flush()
            objective = insert_legacy_question(session.connection(), course_id=course.id, created_by=teacher.id, type="SINGLE_CHOICE", content="下列哪个是不可变类型？", options=["list", "tuple"], reference_answer="tuple", score=10)
            subjective = insert_legacy_question(session.connection(), course_id=course.id, created_by=teacher.id, type="SHORT_ANSWER", content="解释变量的作用。", reference_answer="变量用于保存数据。", scoring_rubric="说明保存和引用数据即可。", score=10)
            old_questions = Table("questions", MetaData(), autoload_with=session.connection())
            session.execute(old_questions.update().values(status="Approved", knowledge_points=["数据类型"]).where(old_questions.c.id == objective))
            session.execute(old_questions.update().values(status="Approved", knowledge_points=["变量"]).where(old_questions.c.id == subjective))
            exam = Exam(course=course, creator=teacher, title="第一章测验", status="Published")
            submission = Submission(exam=exam, student=student, status="Submitted")
            objective_answer = Answer(submission=submission, question_id=objective, content="tuple", status="Graded")
            subjective_answer = Answer(submission=submission, question_id=subjective, content="变量用于保存数据。", status="Graded")
            session.add(submission)
            session.flush()
            associations = Table("exam_questions", MetaData(), autoload_with=session.connection())
            session.execute(associations.insert(), [{"exam_id": exam.id, "question_id": identity} for identity in (objective, subjective)])
            session.commit()
            fixture = SubmissionFixture(teacher.id, student.id, course.id, objective, subjective, exam.id, submission.id, objective_answer.id, subjective_answer.id)
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
        alembic("upgrade", "0010_review_round_ids")
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
        alembic("upgrade", "0010_review_round_ids")
    finally:
        if created:
            with engine.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
        engine.dispose()
