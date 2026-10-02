"""T153 real PostgreSQL migration, constraints and legacy unknowns. TCR §10."""
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, inspect, select
from sqlalchemy.exc import IntegrityError

from backend.app.core import database
from tests.integration.test_question_source_migration import _empty_isolated_schema

ROOT = Path(__file__).resolve().parents[2]


def test_paper_import_upgrade_constraints_unknowns_and_safe_downgrade(monkeypatch):
    with _empty_isolated_schema() as (engine, schema):
        monkeypatch.setattr(database, "create_database_engine", lambda: engine)
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "migrations"))
        command.upgrade(config, "0013_file_storage")
        user_id, course_id, kb_id, doc_id, question_id = (uuid4() for _ in range(5))
        now = datetime.now(UTC)
        with engine.begin() as conn:
            def table(name):
                return Table(name, MetaData(), autoload_with=conn, schema=schema)
            conn.execute(table("users").insert().values(id=user_id, username="paper_migration", email="paper@migration.test", password_hash="unused", is_active=True, created_at=now, updated_at=now))
            conn.execute(table("courses").insert().values(id=course_id, name="legacy", created_by=user_id, created_at=now, updated_at=now))
            conn.execute(table("knowledge_bases").insert().values(id=kb_id, course_id=course_id, name="legacy", created_at=now, updated_at=now))
            conn.execute(table("documents").insert().values(id=doc_id, course_id=course_id, knowledge_base_id=kb_id, uploaded_by=user_id, original_filename="legacy.txt", file_format="txt", storage_path="C:/legacy.txt", status="Ready", retryable=False, created_at=now, updated_at=now))
            conn.execute(table("questions").insert().values(id=question_id, course_id=course_id, type="SHORT_ANSWER", content="legacy approved", knowledge_points=[], score="7.25", status="Approved", created_by=user_id, created_at=now, updated_at=now))
        command.upgrade(config, "0014_paper_import")
        with engine.begin() as conn:
            docs = table("documents")
            questions = table("questions")
            assert conn.execute(select(docs.c.purpose, docs.c.knowledge_base_id, docs.c.storage_path)).one() == ("knowledge_base", kb_id, "C:/legacy.txt")
            row = conn.execute(select(questions.c.source_type, questions.c.analysis, questions.c.frozen_at, questions.c.status, questions.c.score)).one()
            assert row[:4] == (None, None, None, "Approved")
            assert str(row.score) == "7.25"
            imports, pages, extracted = table("paper_imports"), table("source_pages"), table("extracted_questions")
            assert "original_file_path" not in imports.c
            for name in ("assets", "source_regions", "source_page_ids", "knowledge_points", "image_assessment"):
                actual = next(c for c in inspect(conn).get_columns("extracted_questions", schema=schema) if c["name"] == name)
                assert str(actual["type"]) == "JSONB"
            original_id, imported_id = uuid4(), uuid4()
            original = {"id": original_id, "course_id": course_id, "uploaded_by": user_id, "original_filename": "source.pdf", "file_format": "pdf", "purpose": "paper_source", "status": "Ready", "retryable": False, "storage_path": "papers/source.pdf", "created_at": now, "updated_at": now}
            with pytest.raises(IntegrityError) as error, conn.begin_nested():
                conn.execute(docs.insert().values(**original, knowledge_base_id=kb_id))
            assert error.value.orig.sqlstate == "23514"
            conn.execute(docs.insert().values(**original))
            conn.execute(imports.insert().values(id=imported_id, course_id=course_id, document_id=original_id, uploaded_by=user_id, original_filename="source.pdf", page_count=1))
            values = {"id": uuid4(), "paper_import_id": imported_id, "extracted_by": "TEXT", "source_page_ids": []}
            for invalid in ({"source_page_ids": {}}, {"assets": [{}] * 6}, {"status": "Corrected"}, {"score": "-1"}, {"image_assessment": []}):
                with pytest.raises(IntegrityError) as error, conn.begin_nested():
                    conn.execute(extracted.insert().values(**(values | invalid)))
                assert error.value.orig.sqlstate == "23514"
            with pytest.raises(IntegrityError) as error, conn.begin_nested():
                conn.execute(pages.insert().values(id=uuid4(), paper_import_id=imported_id, page_number=1, image_path="papers/page.png", width=0, height=1))
            assert error.value.orig.sqlstate == "23514"
        with pytest.raises(RuntimeError, match="导入数据"):
            command.downgrade(config, "0013_file_storage")
        with engine.begin() as conn:
            conn.execute(imports.delete().where(imports.c.id == imported_id))
            conn.execute(docs.delete().where(docs.c.id == original_id))
        command.downgrade(config, "0013_file_storage")
        with engine.connect() as conn:
            assert "paper_imports" not in inspect(conn).get_table_names(schema=schema)
            assert not next(c for c in inspect(conn).get_columns("documents", schema=schema) if c["name"] == "knowledge_base_id")["nullable"]
