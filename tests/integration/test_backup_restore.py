"""T151 real standard-client dump/restore; every database is created by this module.
TCR: docs/test-change-record-v2.md §9. No source-page/image acceptance is claimed.
"""
from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from backend.app.core.config import get_settings, reset_settings_cache
from backend.app.core.database import Base
from backend.app.core.environment import (
    load_test_resource_profile,
    resolve_environment_profile,
)
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
    ExamQuestion,
    GradingResult,
    KnowledgeBase,
    Question,
    QuestionSourceChunk,
    ReviewRecord,
    Role,
    Submission,
    User,
)
from backend.app.schemas.settings import DatabaseIdentity, EnvironmentProfile
from backend.app.services.auth_service import AuthService
from backend.app.services.backup_restore_service import BackupRestoreService
from backend.app.services.file_storage_service import (
    FileStorageError,
    FileStorageService,
)
from backend.app.services.postgres_backup import PostgresTools
from backend.app.services.storage_migration_service import StorageMigrationService
from tests.postgres_helpers import isolated_test_resource_profile


def _assert_owned_test_database(connection: Connection, name: str, identity: DatabaseIdentity) -> None:
    if (
        connection.scalar(text("SELECT current_database()")) != "postgres"
        or connection.scalar(text("SELECT current_user")) != identity.user
        or str(connection.scalar(text("SELECT system_identifier::text FROM pg_control_system()")))
            != identity.instance_id
        or connection.scalar(text(
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=:name"
        ), {"name": name}) != identity.user
    ):
        raise ValueError("TEST_BACKUP_DATABASE_OWNER_MISMATCH")


@contextmanager
def isolated_database(*, migrated: bool = False):
    # The legacy default URL may point at Development. Refuse it before even
    # constructing a control connection; maintenance rights are audited later.
    profile = load_test_resource_profile()
    expected = profile.database_identity
    base_url = make_url(str(get_settings().database_url))
    if (
        base_url.host != expected.host
        or (base_url.port or 5432) != expected.port
        or base_url.database != expected.database
        or base_url.username != expected.user
    ):
        raise ValueError("TEST_BACKUP_DATABASE_URL_MISMATCH")
    maintenance = profile.maintenance_identity
    maintenance_raw = os.environ.get("EDUAGENT_TEST_MAINTENANCE_URL")
    if maintenance is None or not maintenance_raw:
        raise ValueError("TEST_BACKUP_MAINTENANCE_PROFILE_REQUIRED")
    maintenance_url = make_url(maintenance_raw)
    if (
        maintenance_url.host != maintenance.host
        or (maintenance_url.port or 5432) != maintenance.port
        or maintenance_url.database != maintenance.database
        or maintenance_url.username != maintenance.user
    ):
        raise ValueError("TEST_BACKUP_MAINTENANCE_URL_MISMATCH")
    name = "test_e1_backup_" + uuid4().hex
    control = create_engine(maintenance_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    created = False
    engine = None
    try:
        with control.connect() as connection:
            if (
                connection.scalar(text("SELECT current_database()")) != "postgres"
                or connection.scalar(text("SELECT current_user")) != maintenance.user
                or str(connection.scalar(text("SELECT system_identifier::text FROM pg_control_system()")))
                    != maintenance.instance_id
                or connection.scalar(text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user"))
                or not connection.scalar(text("SELECT rolcreatedb FROM pg_roles WHERE rolname = current_user"))
            ):
                raise ValueError("TEST_BACKUP_INSTANCE_MISMATCH")
            connection.execute(text(f'CREATE DATABASE "{name}"'))
            created = True
        engine = create_engine(maintenance_url.set(database=name), poolclass=NullPool)
        with engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            if migrated:
                from alembic import command
                from alembic.config import Config

                repository = Path(__file__).resolve().parents[2]
                migration = Config(str(repository / "alembic.ini"))
                migration.set_main_option("script_location", str(repository / "migrations"))
                migration.attributes["connection"] = connection
                command.upgrade(migration, "0023_grading_exam_question")
                command.upgrade(migration, "0024_embedding_index_identity")
            else:
                Base.metadata.create_all(connection)
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        try:
            if created:
                with control.connect() as connection:
                    _assert_owned_test_database(connection, name, maintenance)
                    connection.execute(text(f'DROP DATABASE "{name}"'))
        finally:
            control.dispose()


def tools_for(engine):
    container = os.environ.get("EDUAGENT_TEST_PG_CONTAINER")
    if not container and not shutil.which("pg_dump"):
        pytest.skip("标准 PostgreSQL 客户端不在 PATH；设置显式 EDUAGENT_TEST_PG_CONTAINER 后复验。")
    return PostgresTools(engine.url, container=container)


@contextmanager
def _activate_backup_profile(root: Path) -> Iterator[EnvironmentProfile]:
    """Re-enter a derived file profile, retaining the verified runner/maintenance facts."""
    receipt = load_test_resource_profile()
    settings = get_settings()
    expected = receipt.database_identity
    url = make_url(str(settings.database_url))
    if (
        url.host != expected.host
        or (url.port or 5432) != expected.port
        or url.database != expected.database
        or url.username != expected.user
    ):
        raise ValueError("TEST_BACKUP_DATABASE_URL_MISMATCH")
    if not root.is_absolute() or root != root.resolve():
        raise ValueError("ABSOLUTE_TEST_ROOT_REQUIRED")
    values = {
        "EDUAGENT_ENVIRONMENT": receipt.environment.value,
        "EDUAGENT_TEST_RESOURCE_PROFILE": str(root / "test-resource-profile.json"),
        "EDUAGENT_DATA_ROOT": str(root),
        "STORAGE_ROOT": str(root / "storage"),
        # The explicit engines still own their real maintenance databases. Do not
        # relabel a maintenance URL as this profile's primary runner identity.
        "DATABASE_URL": str(settings.database_url),
    }
    selected = load_test_resource_profile(process_env={**os.environ, **values})
    if selected != receipt.model_copy(update={"root": root}):
        raise ValueError("TEST_BACKUP_PROFILE_MISMATCH")
    with patch.dict(os.environ, values):
        reset_settings_cache()
        try:
            yield resolve_environment_profile()
        finally:
            reset_settings_cache()


@pytest.fixture
def backup_case(tmp_path, request):
    with (
        isolated_test_resource_profile(tmp_path / "original-storage") as source_profile,
        isolated_database(migrated=getattr(request, "param", None) == "migrated") as engine,
    ):
        # Prepare only the target receipt/root. Its canonical storage must remain
        # absent until restore creates it (or an existing-target test owns it).
        with isolated_test_resource_profile(tmp_path / "restored"):
            pass
        tools = tools_for(engine)
        root = source_profile.storage_root
        root.mkdir()
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
            original = root / "historical.txt"
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
            exam.exam_question_links.append(
                ExamQuestion(question=question, order_index=1)
            )
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
                    profile = load_test_resource_profile()
                    if profile.maintenance_identity is None:
                        raise ValueError("TEST_BACKUP_MAINTENANCE_PROFILE_REQUIRED")
                    _assert_owned_test_database(connection, restore_name, profile.maintenance_identity)
                    connection.execute(text(f'DROP DATABASE "{restore_name}"'))
        finally:
            control.dispose()


def backup(case):
    service, ids, tmp_path, _token, _name = case
    return service.backup(backup_root=tmp_path / "backups", actor_id=ids["admin"], writers_stopped=True, timeout=0.2)


def restore(case, destination):
    service, ids, tmp_path, _token, name = case
    with _activate_backup_profile(tmp_path / "restored"):
        return service.restore(backup_set=destination, target_database=name,
                               target_root=tmp_path / "restored" / "storage",
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
    target = tmp_path / "restored" / "storage"
    assert (target / MARKER).exists()
    restored = create_engine(service.engine.url.set(database=name), poolclass=NullPool)
    try:
        with _activate_backup_profile(tmp_path / "restored"), Session(restored) as session:
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
    assert not (tmp_path / "restored" / "storage").exists()


def test_restore_failure_keeps_closed_isolated_target(backup_case, monkeypatch):
    service, _ids, tmp_path, _token, _name = backup_case
    destination, manifest = backup(backup_case)
    assert manifest.outcome == "complete"
    monkeypatch.setattr(service.tools, "restore", lambda *_: (_ for _ in ()).throw(FileStorageError("POSTGRES_TOOL_FAILED", "actual restore failure")))
    report = restore(backup_case, destination)
    assert report.outcome == "failed"
    assert (tmp_path / "restored" / "storage" / MARKER).exists()
    assert not (service.root / MARKER).exists()
    assert json.loads((tmp_path / "restore-report.json").read_text(encoding="utf-8"))["outcome"] == "failed"


def test_existing_target_is_not_overwritten(backup_case):
    _service, _ids, tmp_path, _token, _name = backup_case
    destination, _manifest = backup(backup_case)
    target = tmp_path / "restored" / "storage"
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
        assert not (tmp_path / "restored" / "storage").exists()
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
    assert (tmp_path / "restored" / "storage" / MARKER).exists()
    assert not (service.root / MARKER).exists()
    assert not (tmp_path / "restore-report.json").exists()


@pytest.mark.parametrize("scenario", ["normal", "missing_snapshot", "occupied_target", "missing_backup_snapshot"])
def test_v3_backup_restores_snapshot_and_index_audit_without_secrets(backup_case, scenario):
    import asyncio

    from backend.app.domain.enums import WorkflowStatus
    from backend.app.models import DocumentChunk, WorkflowRun
    from backend.app.services.settings.config_service import ConfigService
    from backend.app.services.settings.runtime_snapshot import RuntimeSnapshotService
    from tests.integration import test_embedding_identity_postgres as index
    from tests.support import runtime_settings_fakes as snapshots

    service, ids, tmp_path, _token, name = backup_case
    profile = resolve_environment_profile()
    harness = snapshots.settings_harness(profile.config_root.parent, environment_id=profile.environment_id)
    try:
        asyncio.run(harness.activate_model("t238-original-model", with_credential=True))
        runtime = RuntimeSnapshotService(harness.service)
        facts = snapshots.create_snapshot(runtime, workflow_id="wf-t238-paused")
        snapshot_id = facts["snapshot_id"]
        original = runtime.path_for(snapshot_id).read_bytes()
        secret = harness.store.get(harness.provider_reference, harness.provider_reference.version).get_secret_value()
        payload_state = snapshots.sample_state(snapshot_id=str(snapshot_id))
        payload_state.update(workflow_id="wf-t238-paused", request_id="t238-request", submission_id=str(ids["submission"]))
        checkpoint = snapshots.current_payload(payload_state)
        with Session(service.engine) as session:
            document = session.get(Document, ids["doc"])
            kb_id = document.knowledge_base_id
            session.add(DocumentChunk(document_id=document.id, course_id=ids["course"], knowledge_base_id=kb_id,
                chunk_index=0, content="original historical vector", embedding=[0.125] * 1024, chunk_metadata={}))
            session.add(WorkflowRun(workflow_id="wf-t238-paused", request_id="t238-request", submission_id=ids["submission"],
                status=WorkflowStatus.PAUSED, checkpoint=checkpoint, resumable=True))
            session.commit()
            before = index._chunk_fingerprint(session, kb_id)
            principal = index._Principal(profile.environment_id)
            identity_service = index.IndexIdentityService(repository=index.SqlAlchemyIndexRepository(session),
                clock=index._Clock(), principal=principal)
            preview = index._preview(identity_service, kb_id=kb_id, identity=index._identity(index._StubEmbedding()), principal=principal)
            index._confirm(identity_service, preview, principal)
            identity = session.get(KnowledgeBase, kb_id).embedding_identity
            assert identity["status"] == "legacy_adopted" and identity["audits"]
            assert index._chunk_fingerprint(session, kb_id) == before
        if scenario == "missing_snapshot":
            runtime.path_for(snapshot_id).unlink()
        destination, manifest = backup(backup_case)
        if scenario == "missing_snapshot":
            assert manifest.outcome == "failed"
            assert any(issue.stage == "runtime_snapshots" for issue in manifest.issues)
            return
        assert manifest.outcome == "complete", manifest.issues
        assert len(manifest.runtime_snapshots) == 1
        entry = manifest.runtime_snapshots[0]
        assert (destination / entry.relative_path).read_bytes() == original
        assert secret.encode() not in original
        assert not (destination / "credentials").exists()
        if scenario == "occupied_target":
            occupied = tmp_path / "restored" / "config" / "snapshots"
            occupied.mkdir(parents=True)
            (occupied / "existing.json").write_bytes(b"existing-owned-test-sentinel")
            report = restore(backup_case, destination)
            assert report.outcome == "failed" and any(issue.code == "RESTORE_SNAPSHOTS_EXIST" for issue in report.issues)
            assert (occupied / "existing.json").read_bytes() == b"existing-owned-test-sentinel"
            assert not (tmp_path / "restored" / "storage").exists()
            return
        if scenario == "missing_backup_snapshot":
            (destination / entry.relative_path).unlink()
            report = restore(backup_case, destination)
            assert report.outcome == "failed" and any(issue.code == "BACKUP_FILE_SET_MISMATCH" for issue in report.issues)
            assert not (tmp_path / "restored" / "storage").exists()
            return
        report = restore(backup_case, destination)
        assert report.outcome == "verified", report.issues
        restored_path = tmp_path / "restored" / "config" / "snapshots" / f"{snapshot_id}.json"
        assert restored_path.read_bytes() == original
        assert not (tmp_path / "restored" / "credentials").exists()
        assert runtime.path_for(snapshot_id).read_bytes() == original
        restored = create_engine(service.engine.url.set(database=name), poolclass=NullPool)
        try:
            with Session(restored) as session:
                assert session.get(KnowledgeBase, kb_id).embedding_identity == identity
                assert index._chunk_fingerprint(session, kb_id) == before
                run = session.query(WorkflowRun).filter_by(workflow_id="wf-t238-paused").one()
                assert run.status == WorkflowStatus.PAUSED and run.checkpoint == checkpoint
        finally:
            restored.dispose()
        with ConfigService(tmp_path / "restored", profile.environment_id, writable=False) as config:
            restored_runtime = RuntimeSnapshotService(config)
            assert str(restored_runtime.load(snapshot_id).snapshot_id) == snapshot_id
            with pytest.raises(Exception) as absent:
                restored_runtime.resolve_for_state({"snapshot_id": snapshot_id})
            assert snapshots.safe_code(absent.value) == "SNAPSHOT_DEPENDENCY_UNAVAILABLE"
    finally:
        harness.close()


@pytest.mark.parametrize("backup_case", ["migrated"], indirect=True)
def test_migrated_backup_preserves_snapshot_identity_and_repeat_upgrade(backup_case):
    from alembic import command
    from alembic.config import Config

    service, _ids, tmp_path, _token, name = backup_case
    with service.engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0024_embedding_index_identity"
    # Reuse the complete data assertions with a real migration-built source.
    test_v3_backup_restores_snapshot_and_index_audit_without_secrets(backup_case, "normal")
    snapshots = tmp_path / "restored" / "config" / "snapshots"
    before_files = {p.name: p.read_bytes() for p in snapshots.iterdir()}
    restored = create_engine(service.engine.url.set(database=name), poolclass=NullPool)
    try:
        with restored.begin() as connection:
            def facts():
                return {
                    table: connection.execute(text(f"SELECT row_to_json(t)::text FROM {table} t ORDER BY 1")).scalars().all()
                    for table in ("knowledge_bases", "document_chunks", "workflow_runs", "grading_results", "review_records")
                }
            before = facts()
            repository = Path(__file__).resolve().parents[2]
            migration = Config(str(repository / "alembic.ini"))
            migration.set_main_option("script_location", str(repository / "migrations"))
            migration.attributes["connection"] = connection
            for _ in range(2):
                command.upgrade(migration, "head")
                assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0024_embedding_index_identity"
                assert facts() == before
    finally:
        restored.dispose()
    assert {p.name: p.read_bytes() for p in snapshots.iterdir()} == before_files
    assert (tmp_path / "restored" / "storage" / MARKER).exists()


def test_native_pg_restore_failure_keeps_target_closed_and_source_intact(backup_case):
    import hashlib

    service, ids, tmp_path, _token, name = backup_case
    destination, manifest = backup(backup_case)
    assert manifest.outcome == "complete"
    archive = destination / "database.dump"
    archive.write_bytes(b"PGDMP-invalid-synthetic-archive")
    # A coherent synthetic manifest reaches native archive validation rather
    # than merely repeating the existing content-hash rejection scenario.
    payload = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    payload["database"].update(size_bytes=archive.stat().st_size, sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
    (destination / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    report = restore(backup_case, destination)
    assert report.outcome == "failed"
    assert any(issue.code == "POSTGRES_TOOL_FAILED" and issue.stage == "restore_database" for issue in report.issues)
    marker = tmp_path / "restored" / "storage" / MARKER
    assert json.loads(marker.read_text(encoding="utf-8"))["writes_enabled"] is False
    restored = create_engine(service.engine.url.set(database=name), poolclass=NullPool)
    try:
        with restored.connect() as connection:
            assert connection.scalar(text("SELECT to_regclass('public.users')")) is None
    finally:
        restored.dispose()
    with Session(service.engine) as session:
        assert session.get(Course, ids["course"]).name == "backup course"
        assert session.get(GradingResult, ids["grade"]).score == Decimal("7.25")
    assert not (service.root / MARKER).exists()
    with _activate_backup_profile(tmp_path / "restored"):
        retry = service.restore(backup_set=destination, target_database=name,
            target_root=tmp_path / "restored" / "storage", report_path=tmp_path / "retry-native-failure.json", actor_id=ids["admin"])
    assert retry.outcome == "failed"
    assert any(issue.code == "RESTORE_INVALID_TARGET" for issue in retry.issues)
    assert marker.exists()
