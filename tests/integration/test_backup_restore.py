"""T151 real standard-client dump/restore; every database is created by this module.
TCR: docs/test-change-record-v2.md §9. No source-page/image acceptance is claimed.
"""
from __future__ import annotations

import json
import os
import shutil
from contextlib import contextmanager
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from backend.app.core.config import get_settings
from backend.app.core.database import Base
from backend.app.core.maintenance import MARKER
from backend.app.domain.enums import (
    AnswerStatus,
    DocumentStatus,
    ExamStatus,
    QuestionStatus,
    QuestionType,
    ReviewStatus,
    SubmissionStatus,
    UserRole,
)
from backend.app.models import (
    Answer,
    Course,
    Document,
    Exam,
    GradingResult,
    KnowledgeBase,
    Question,
    QuestionSourceChunk,
    ReviewRecord,
    Role,
    Submission,
    User,
)
from backend.app.services.auth_service import AuthService
from backend.app.services.backup_restore_service import BackupRestoreService
from backend.app.services.file_storage_service import (
    FileStorageError,
    FileStorageService,
)
from backend.app.services.postgres_backup import PostgresTools
from backend.app.services.storage_migration_service import StorageMigrationService


@contextmanager
def isolated_database():
    name = "test_e1_backup_" + uuid4().hex
    base_url = make_url(str(get_settings().database_url))
    control = create_engine(base_url.set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool)
    created = False
    engine = None
    try:
        with control.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        created = True
        engine = create_engine(base_url.set(database=name), poolclass=NullPool)
        with engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            Base.metadata.create_all(connection)
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        if created:
            with control.connect() as connection:
                connection.execute(text(f'DROP DATABASE "{name}"'))
        control.dispose()


def tools_for(engine):
    container = os.environ.get("EDUAGENT_TEST_PG_CONTAINER")
    if not container and not shutil.which("pg_dump"):
        pytest.skip("标准 PostgreSQL 客户端不在 PATH；设置显式 EDUAGENT_TEST_PG_CONTAINER 后复验。")
    return PostgresTools(engine.url, container=container)


@pytest.fixture
def backup_case(tmp_path):
    with isolated_database() as engine:
        tools = tools_for(engine)
        root = tmp_path / "original-storage"
        with Session(engine, expire_on_commit=False) as session:
            teacher = User(username="backup_teacher", email="bt@example.com", password_hash="unused")
            teacher.roles.append(Role(name=UserRole.TEACHER))
            admin = User(username="backup_admin", email="ba@example.com", password_hash="unused")
            admin.roles.append(Role(name=UserRole.ADMIN))
            student = User(username="backup_student", email="bs@example.com", password_hash="unused")
            student.roles.append(Role(name=UserRole.STUDENT))
            session.add_all([teacher, admin, student])
            session.flush()
            course = Course(name="backup course", created_by=teacher.id)
            session.add(course)
            session.flush()
            kb = KnowledgeBase(name="source material", course_id=course.id)
            session.add(kb)
            session.flush()
            original = tmp_path / "historical.txt"
            original.write_bytes(b"same original for two document identities")
            documents = [Document(id=uuid4(), course_id=course.id, knowledge_base_id=kb.id, uploaded_by=teacher.id,
                                  original_filename="historical.txt", file_format="txt", storage_path=str(original),
                                  status=DocumentStatus.READY) for _ in range(2)]
            session.add_all(documents)
            question = Question(course_id=course.id, type=QuestionType.SHORT_ANSWER, content="source question",
                                reference_answer="actual reference", scoring_rubric="criterion", score=Decimal(10),
                                knowledge_points=["kp"], status=QuestionStatus.APPROVED, created_by=teacher.id)
            session.add(question)
            session.flush()
            source = QuestionSourceChunk(question_id=question.id, chunk_id=uuid4(), live_chunk_id=None,
                document_id=documents[1].id, course_id=course.id, source_order=1, content_snapshot="historical source",
                source_file="historical.txt", chunk_index=0)
            session.add(source)
            exam = Exam(course_id=course.id, created_by=teacher.id, title="published", status=ExamStatus.PUBLISHED)
            exam.questions.append(question)
            session.add(exam)
            session.flush()
            submission = Submission(exam_id=exam.id, student_id=student.id, status=SubmissionStatus.REVIEWED)
            session.add(submission)
            session.flush()
            answer = Answer(submission_id=submission.id, question_id=question.id, content="real test answer", status=AnswerStatus.GRADED)
            session.add(answer)
            session.flush()
            grade = GradingResult(answer_id=answer.id, submission_id=submission.id, question_type=QuestionType.SHORT_ANSWER,
                                  score=Decimal("7.25"), max_score=Decimal(10), reason="teacher adjusted",
                                  confidence=0.8, review_status=ReviewStatus.MODIFIED)
            session.add(grade)
            session.flush()
            review = ReviewRecord(grading_result_id=grade.id, reviewer_id=teacher.id, decision=ReviewStatus.MODIFIED,
                                  original_score=Decimal(7), original_reason="original reason",
                                  final_score=Decimal("7.25"), final_reason="teacher adjusted", review_round_id=None)
            session.add(review)
            session.commit()
            assert StorageMigrationService(session, root=root).run(actor_id=admin.id).counts["migrated"] == 2
            files = FileStorageService(session, root=root)
            export = files.create_export(filename="real-result.csv", content=b"score,7.25", actor_id=teacher.id,
                                         audience="submission_owner", submission_id=submission.id)
            # Real owned, failed operation material proves all four buckets are copied;
            # these are not fabricated SourcePage or QuestionAsset business entities.
            for bucket in ("papers", "assets"):
                stored = files._store_bytes(resource_type="document", resource_id=uuid4(),
                    owner={"course_id": str(course.id), "knowledge_base_id": str(kb.id)}, actor_id=teacher.id,
                    filename="failed-material.bin", content=b"known incomplete operation bytes", bucket=bucket)
                files.fail_receipt(stored, code="FILE_REFERENCE_FAILED", message="isolated failed registration")
            ids = {"admin": admin.id, "teacher": teacher.id, "student": student.id, "course": course.id,
                   "doc": documents[0].id, "alias": documents[1].id, "question": question.id,
                   "exam": exam.id, "submission": submission.id, "grade": grade.id, "review": review.id,
                   "source": source.id, "export": export.file_id}
            token = AuthService(session).issue_access_token(teacher)
        service = BackupRestoreService(engine, root=root, tools=tools)
        restore_name = "test_e1_restore_" + uuid4().hex
        yield service, ids, tmp_path, token, restore_name
        control = create_engine(engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool)
        try:
            with control.connect() as connection:
                if connection.scalar(text("SELECT 1 FROM pg_database WHERE datname=:name"), {"name": restore_name}):
                    connection.execute(text(f'DROP DATABASE "{restore_name}"'))
        finally:
            control.dispose()


def backup(case):
    service, ids, tmp_path, _token, _name = case
    return service.backup(backup_root=tmp_path / "backups", actor_id=ids["admin"], writers_stopped=True, timeout=0.2)


def restore(case, destination):
    service, ids, tmp_path, _token, name = case
    return service.restore(backup_set=destination, target_database=name, target_root=tmp_path / "restored",
                           report_path=tmp_path / "restore-report.json", actor_id=ids["admin"])


def test_real_same_set_restores_business_facts_and_authorized_bytes(backup_case):
    service, ids, tmp_path, token, name = backup_case
    destination, manifest = backup(backup_case)
    assert manifest.outcome == "complete", manifest.issues
    assert {entry.relative_path.split("/")[0] for entry in manifest.files} == {"uploads", "papers", "assets", "exports"}
    assert len({r.relative_path for r in manifest.references if r.resource_type == "document"}) == 1
    original_manifest = (destination / "manifest.json").read_bytes()
    report = restore(backup_case, destination)
    assert report.outcome == "verified", report.issues
    assert (destination / "manifest.json").read_bytes() == original_manifest
    assert not (service.root / MARKER).exists()
    target = tmp_path / "restored"
    assert (target / MARKER).exists()
    restored = create_engine(service.engine.url.set(database=name), poolclass=NullPool)
    try:
        with Session(restored) as session:
            teacher = AuthService(session).get_current_user(token)
            files = FileStorageService(session, root=target)
            doc = session.get(Document, ids["doc"])
            assert files.read_document(doc) == b"same original for two document identities"
            assert doc.storage_path == session.get(Document, ids["alias"]).storage_path
            assert session.get(QuestionSourceChunk, ids["source"]).document_id == ids["alias"]
            assert session.get(QuestionSourceChunk, ids["source"]).content_snapshot == "historical source"
            assert session.get(Exam, ids["exam"]).questions[0].id == ids["question"]
            assert session.get(Submission, ids["submission"]).status == SubmissionStatus.REVIEWED
            assert session.get(GradingResult, ids["grade"]).score == Decimal("7.25")
            assert session.get(ReviewRecord, ids["review"]).review_round_id is None
            assert session.get(ReviewRecord, ids["review"]).final_score == Decimal("7.25")
            assert files.get_view("d_" + ids["doc"].hex, actor_id=teacher.id).availability == "available"
            with pytest.raises(FileStorageError):
                files.get_view("d_" + ids["doc"].hex, actor_id=ids["student"])
            assert files.download(ids["export"], actor_id=ids["student"])[0].read_bytes() == b"score,7.25"
            with pytest.raises(FileStorageError) as error:
                files.create_export(filename="x.csv", content=b"x", actor_id=teacher.id, audience="teacher_only", course_id=ids["course"])
            assert error.value.code == "STORAGE_MAINTENANCE"
    finally:
        restored.dispose()


def test_missing_unknown_unowned_materials_remain_incomplete(backup_case):
    service, ids, _tmp_path, _token, _name = backup_case
    with Session(service.engine) as session:
        doc = session.get(Document, ids["doc"])
        (service.root / doc.storage_path).unlink()
        alias = session.get(Document, ids["alias"])
        alias.storage_path = None
        alias.file_metadata = None
        session.commit()
    (service.root / "exports" / "unowned.bin").write_bytes(b"unknown")
    destination, manifest = backup(backup_case)
    assert manifest.outcome == "incomplete"
    codes = {issue.code for issue in manifest.issues}
    assert {"FILE_MISSING", "FILE_HISTORY_UNKNOWN", "UNOWNED_MATERIAL"} <= codes
    assert restore(backup_case, destination).outcome == "failed"


def test_active_database_connection_prevents_window_and_preserves_source(backup_case):
    service, _ids, _tmp_path, _token, _name = backup_case
    with service.engine.connect() as active:
        active.execute(text("SELECT 1"))
        _destination, manifest = backup(backup_case)
    assert manifest.outcome == "failed"
    assert any(issue.code == "MAINTENANCE_NOT_DRAINED" for issue in manifest.issues)
    assert manifest.database is None
    assert not (service.root / MARKER).exists()


def test_share_lock_blocks_actual_write_during_dump(backup_case, monkeypatch):
    service, ids, _tmp_path, _token, _name = backup_case
    original_dump = service.tools.dump
    def probe(path):
        with service.engine.connect() as writer:
            writer.execute(text("SET LOCAL lock_timeout='100ms'"))
            with pytest.raises(OperationalError) as error:
                writer.execute(text("UPDATE courses SET name='unexpected' WHERE id=:id"), {"id": ids["course"]})
            assert error.value.orig.sqlstate == "55P03"
            writer.rollback()
        return original_dump(path)
    monkeypatch.setattr(service.tools, "dump", probe)
    _destination, manifest = backup(backup_case)
    assert manifest.outcome == "complete", manifest.issues
    with Session(service.engine) as session:
        assert session.get(Course, ids["course"]).name == "backup course"


def test_copy_failure_retains_dump_partial_materials_and_original(backup_case, monkeypatch):
    service, _ids, _tmp_path, _token, _name = backup_case
    from backend.app.services import backup_restore_service as module
    def fail(source, target):
        target.write_bytes(b"partial")
        raise OSError("actual copy failure")
    monkeypatch.setattr(module, "copy_verified", fail)
    destination, manifest = backup(backup_case)
    assert manifest.outcome == "failed"
    assert (destination / "database.dump").is_file()
    assert any(p.read_bytes() == b"partial" for p in (destination / "files").rglob("*") if p.is_file())
    assert not (service.root / MARKER).exists()


def test_tampered_backup_rejected_before_target_creation(backup_case):
    _service, _ids, tmp_path, _token, _name = backup_case
    destination, manifest = backup(backup_case)
    assert manifest.outcome == "complete"
    file = destination / "files" / manifest.files[0].relative_path
    file.write_bytes(b"tampered")
    report = restore(backup_case, destination)
    assert report.outcome == "failed"
    assert any(issue.code == "BACKUP_CONTENT_CHANGED" for issue in report.issues)
    assert not (tmp_path / "restored").exists()


def test_restore_failure_keeps_closed_isolated_target(backup_case, monkeypatch):
    service, _ids, tmp_path, _token, _name = backup_case
    destination, manifest = backup(backup_case)
    assert manifest.outcome == "complete"
    monkeypatch.setattr(service.tools, "restore", lambda *_: (_ for _ in ()).throw(FileStorageError("POSTGRES_TOOL_FAILED", "actual restore failure")))
    report = restore(backup_case, destination)
    assert report.outcome == "failed"
    assert (tmp_path / "restored" / MARKER).exists()
    assert not (service.root / MARKER).exists()
    assert json.loads((tmp_path / "restore-report.json").read_text(encoding="utf-8"))["outcome"] == "failed"


def test_existing_target_is_not_overwritten(backup_case):
    _service, _ids, tmp_path, _token, _name = backup_case
    destination, _manifest = backup(backup_case)
    target = tmp_path / "restored"
    target.mkdir()
    keep = target / "owned.txt"
    keep.write_bytes(b"existing environment")
    assert restore(backup_case, destination).outcome == "failed"
    assert keep.read_bytes() == b"existing environment"
    assert not (target / MARKER).exists()


def test_receipt_acknowledgement_connection_must_be_drained(backup_case):
    service, ids, _tmp_path, _token, _name = backup_case
    from backend.app.schemas.file_storage import FileMetadata
    from backend.app.services.file_storage_service import StoredFile
    with Session(service.engine, expire_on_commit=False) as writer:
        doc = writer.get(Document, ids["doc"])
        locator = doc.storage_path
        metadata = FileMetadata.model_validate(doc.file_metadata)
        receipt = next(p for p in service.root.rglob("*.receipt.json")
                       if json.loads(p.read_text(encoding="utf-8"))["resource_id"] == str(doc.id))
        writer.commit()
        FileStorageService(writer, root=service.root).commit_receipt(StoredFile(locator, metadata, receipt),
                                                                   current_status="migrated")
        _destination, manifest = backup(backup_case)
        assert manifest.outcome == "failed"
        assert any(issue.code == "MAINTENANCE_NOT_DRAINED" for issue in manifest.issues)


def test_actual_cli_uses_env_jwt_and_current_admin_role(backup_case):
    import subprocess
    import sys
    from pathlib import Path
    service, ids, tmp_path, _token, _name = backup_case
    with Session(service.engine) as session:
        token = AuthService(session).issue_access_token(session.get(User, ids["admin"]))
    environment = os.environ.copy()
    environment["DATABASE_URL"] = service.engine.url.render_as_string(hide_password=False)
    environment["STORAGE_ROOT"] = str(service.root)
    environment["EDUAGENT_MAINTENANCE_TOKEN"] = token
    environment["PYTHONIOENCODING"] = "utf-8"
    script = Path(__file__).resolve().parents[2] / "scripts" / "migrate_storage_paths.py"
    report = tmp_path / "cli-migration.json"
    result = subprocess.run([sys.executable, str(script), "--report", str(report)], env=environment,
                            capture_output=True, text=True, encoding="utf-8", check=False)
    assert result.returncode == 0, result.stderr
    assert token not in result.stdout + result.stderr + report.read_text(encoding="utf-8")
    with Session(service.engine) as session:
        current_admin = session.get(User, ids["admin"])
        current_admin.roles.clear()
        session.commit()
    rejected = subprocess.run([sys.executable, str(script), "--report", str(tmp_path / "rejected.json")],
                               env=environment, capture_output=True, text=True, encoding="utf-8", check=False)
    assert rejected.returncode == 1
    assert not (tmp_path / "rejected.json").exists()
    assert token not in rejected.stdout + rejected.stderr


def test_existing_database_is_preserved(backup_case):
    service, _ids, tmp_path, _token, name = backup_case
    destination, _manifest = backup(backup_case)
    control = create_engine(service.engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        with control.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        control.dispose()
    existing = create_engine(service.engine.url.set(database=name), poolclass=NullPool)
    try:
        with existing.begin() as connection:
            connection.execute(text("CREATE TABLE sentinel (value text)"))
            connection.execute(text("INSERT INTO sentinel VALUES ('existing environment')"))
        report = restore(backup_case, destination)
        assert report.outcome == "failed"
        assert any(issue.code == "RESTORE_DATABASE_EXISTS" for issue in report.issues)
        assert not (tmp_path / "restored").exists()
        with existing.connect() as connection:
            assert connection.scalar(text("SELECT value FROM sentinel")) == "existing environment"
    finally:
        existing.dispose()


def test_report_write_failure_does_not_open_target(backup_case, monkeypatch):
    service, _ids, tmp_path, _token, _name = backup_case
    from backend.app.services import backup_restore_service as module
    destination, _manifest = backup(backup_case)
    original = module.durable_json
    def fail_report(path, value, **kwargs):
        if path.name == "restore-report.json":
            raise OSError("restore report fsync failure")
        return original(path, value, **kwargs)
    monkeypatch.setattr(module, "durable_json", fail_report)
    with pytest.raises(OSError, match="report fsync"):
        restore(backup_case, destination)
    assert (tmp_path / "restored" / MARKER).exists()
    assert not (service.root / MARKER).exists()
    assert not (tmp_path / "restore-report.json").exists()
