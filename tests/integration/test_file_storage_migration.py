"""T147 JSONB/export migration; public business tables are never upgraded.

TCR: docs/test-change-record-v2.md §8.
"""
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, inspect, select

from backend.app.core import database
from backend.app.models import Document
from tests.integration.test_question_source_migration import _empty_isolated_schema

ROOT = Path(__file__).resolve().parents[2]


def test_file_storage_migration_preserves_legacy_and_reverses(monkeypatch):
    with _empty_isolated_schema() as (engine, schema):
        monkeypatch.setattr(database, "create_database_engine", lambda: engine)
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "migrations"))
        command.upgrade(config, "0012_audit_logs")
        user_id, course_id, kb_id, doc_id = (uuid4() for _ in range(4))
        now = datetime.now(UTC)
        with engine.begin() as conn:
            metadata = MetaData()
            users = Table("users", metadata, autoload_with=conn, schema=schema)
            courses = Table("courses", metadata, autoload_with=conn, schema=schema)
            kbs = Table("knowledge_bases", metadata, autoload_with=conn, schema=schema)
            docs = Table("documents", metadata, autoload_with=conn, schema=schema)
            conn.execute(users.insert().values(
                id=user_id, username="migration_owner", email="migration@example.com",
                password_hash="unused", is_active=True, created_at=now, updated_at=now,
            ))
            conn.execute(courses.insert().values(
                id=course_id, name="legacy", created_by=user_id, created_at=now, updated_at=now,
            ))
            conn.execute(kbs.insert().values(
                id=kb_id, course_id=course_id, name="legacy", created_at=now, updated_at=now,
            ))
            conn.execute(docs.insert().values(
                id=doc_id, course_id=course_id, knowledge_base_id=kb_id, uploaded_by=user_id,
                original_filename="legacy.txt", file_format="txt", storage_path="C:/known/legacy.txt",
                status="Ready", retryable=False, created_at=now, updated_at=now,
            ))
            assert "file_metadata" not in {c["name"] for c in inspect(conn).get_columns("documents", schema=schema)}
        command.upgrade(config, "head")
        with engine.connect() as conn:
            inspector = inspect(conn)
            assert "export_files" in inspector.get_table_names(schema=schema)
            column = next(c for c in inspector.get_columns("documents", schema=schema) if c["name"] == "file_metadata")
            assert str(column["type"]) == "JSONB" and column["nullable"]
            row = conn.execute(select(Document.storage_path, Document.file_metadata, Document.status)).one()
            assert row.storage_path == "C:/known/legacy.txt"
            assert row.file_metadata is None
            assert row.status.value == "Ready"
            names = {c["name"] for c in inspector.get_check_constraints("export_files", schema=schema)}
            assert {"ck_export_single_owner", "ck_export_audience", "ck_export_lifecycle"} <= names

        with engine.begin() as conn:
            import pytest
            from sqlalchemy.exc import IntegrityError

            exports = Table("export_files", MetaData(), autoload_with=conn, schema=schema)
            values = {
                "id": uuid4(), "course_id": course_id, "created_by": user_id,
                "audience": "teacher_only", "original_filename": "actual-format.csv",
                "status": "writing", "created_at": now,
            }
            for invalid in (
                {"exam_id": uuid4()},
                {"audience": "submission_owner"},
                {"status": "ready", "completed_at": now},
                {"file_metadata": []},
            ):
                with pytest.raises(IntegrityError) as error, conn.begin_nested():
                    conn.execute(exports.insert().values(**(values | invalid)))
                assert error.value.orig.sqlstate == "23514"
            conn.execute(exports.insert().values(**values))
            courses = Table("courses", MetaData(), autoload_with=conn, schema=schema)
            with pytest.raises(IntegrityError) as error, conn.begin_nested():
                conn.execute(courses.delete().where(courses.c.id == course_id))
            assert error.value.orig.sqlstate == "23503"
            assert error.value.orig.diag.constraint_name == "export_files_course_id_fkey"

        command.downgrade(config, "0012_audit_logs")
        with engine.connect() as conn:
            assert "export_files" not in inspect(conn).get_table_names(schema=schema)
            assert "file_metadata" not in {c["name"] for c in inspect(conn).get_columns("documents", schema=schema)}
            legacy_docs = Table("documents", MetaData(), autoload_with=conn, schema=schema)
            row = conn.execute(select(legacy_docs.c.storage_path, legacy_docs.c.status)).one()
            assert tuple(row) == ("C:/known/legacy.txt", "Ready")

def test_postgres_locator_lock_serializes_link_and_delete(tmp_path):
    import pytest
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    from backend.app.services.file_storage_service import FileStorageService

    with (
        _empty_isolated_schema() as (engine, _schema),
        Session(engine) as first,
        Session(engine) as second,
    ):
        first_files = FileStorageService(first, root=tmp_path)
        second_files = FileStorageService(second, root=tmp_path)
        locator = "uploads/shared/original.txt"
        first_files.lock_locator(locator)
        second.execute(text("SET LOCAL lock_timeout = '100ms'"))
        with pytest.raises(OperationalError) as error:
            second_files.lock_locator(locator)
        assert error.value.orig.sqlstate == "55P03"
        second.rollback()
        first.rollback()
        second.execute(text("SET LOCAL lock_timeout = '500ms'"))
        second_files.lock_locator(locator)
        second.rollback()
