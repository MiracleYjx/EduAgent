"""T074: real owned PG, standard clients and synthetic local authority; no paid calls."""

import asyncio
import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event, select, text

from backend.app.core.bootstrap import build_bootstrap_security
from backend.app.core.settings_security import (
    OperatorIdentity,
    PrincipalKind,
    SettingsSecurityError,
)
from backend.app.models.conversation import Conversation, Message
from backend.app.models.document import Document
from backend.app.models.operation_run import OperationRun
from backend.app.models.practice import GradeAttempt
from backend.app.models.score_dispute import ScoreDispute
from backend.app.models.subject import Subject
from backend.app.services.backup_restore_service import (
    BackupRestoreService,
    _runtime_snapshot_ids,
)
from backend.app.services.postgres_backup import PostgresTools
from backend.app.services.settings.config_service import ConfigService
from backend.app.services.settings.runtime_snapshot import RuntimeSnapshotService
from backend.app.services.storage_migration_service import storage_references
from tests.integration import test_learning_material_versions as materials
from tests.integration.test_personal_practice_flow import add_history, seed_bank
from tests.postgres_helpers import isolated_test_resource_profile
from tests.support.runtime_settings_fakes import settings_harness

space = materials.space


@pytest.fixture
def recovery(space):
    harness = settings_harness(space.profile.config_root.parent, environment_id=space.profile.environment_id)
    asyncio.run(harness.activate_model("synthetic-recovery", with_credential=True))
    snapshots = RuntimeSnapshotService(harness.service)
    security = build_bootstrap_security(space.client.app.state.bootstrap)
    code = security.issue_code(OperatorIdentity(kind=PrincipalKind.LOCAL_OS, principal_id="synthetic-recovery"))
    token = security.exchange(code.code).token
    tools = PostgresTools(space.engine.url, container=os.environ["EDUAGENT_TEST_PG_CONTAINER"])
    try:
        yield space, snapshots, security, token, tools
    finally:
        snapshots.close()
        harness.close()


def service(case):
    space, snapshots, security, token, tools = case
    return BackupRestoreService.for_local_management(
        space.engine, profile=space.profile, security=security, token=token,
        tools=tools, snapshots=snapshots,
    )


def stored_history(recovery):
    space, snapshots, *_ = recovery
    sid, _point, ids, document_id = seed_bank(space, count=3)
    answer_id = add_history(space, sid, ids[0])
    required = [snapshots.create(workflow_id=uuid4().hex, request_id=uuid4().hex,
        task_kinds=["default_llm"], storage_root=space.profile.storage_root).snapshot_id for _ in range(5)]
    with space.subjects.sessions.begin() as session:
        document = session.get(Document, document_id)
        document.parse_facts = {**document.parse_facts,
            "local_ocr": {"runtime_snapshot_id": str(required[3]), "state": "partial"},
            "ocr": {"page:1": {"snapshot_id": str(required[4]), "adapter": "rapidocr",
                                "text": "Synthetic retained page OCR"}}}
        prior = session.scalar(select(GradeAttempt).where(GradeAttempt.answer_id == answer_id))
        regraded = GradeAttempt(id=uuid4(), answer_id=answer_id, round=2, validation_state="valid",
            execution_source="model", score=8, max_score=10, reason="Synthetic stored history",
            scoring_input=prior.scoring_input, runtime_snapshot_id=required[0])
        session.add(regraded)
        session.flush()
        session.add(ScoreDispute(answer_id=answer_id, disputed_attempt_id=prior.id, state="resolved",
            reason="Synthetic accepted dispute", accepted_attempt_id=regraded.id))
        session.add(OperationRun(environment_id=space.profile.environment_id, idempotency_key=uuid4().hex,
            purpose="teacher_chat", preview_digest="a"*64, plan_digest="b"*64,
            runtime_snapshot_id=required[1], state="succeeded", request_budget=1, actual_requests=1, steps=[]))
        chat = Conversation(subject_id=sid, title="Synthetic retained chat")
        session.add(chat)
        session.flush()
        session.add(Message(conversation_id=chat.id, role="assistant", text="Synthetic explanation",
            state="succeeded", operation_id=uuid4(), runtime_snapshot_id=required[2]))
    return set(required)


def test_all_personal_history_snapshot_bindings_are_collected(recovery):
    space = recovery[0]
    required = stored_history(recovery)
    with space.engine.connect() as connection:
        assert _runtime_snapshot_ids(connection) == required


def test_personal_backup_uses_local_authority_and_covers_materials(recovery, tmp_path):
    space, *_ = recovery
    sid = materials.subject(space)
    response = materials.upload(space, sid, "source.docx", materials.sample_docx())
    assert response.status_code == 201, response.text
    assert response.json()["assets"][0]["stored"] is True
    destination, manifest = service(recovery).backup(backup_root=tmp_path / "backup", writers_stopped=True)
    assert manifest.outcome == "complete", manifest.issues
    assert manifest.manifest_version == 2
    assert manifest.environment_id == space.profile.environment_id
    paths = {f.relative_path for f in manifest.files}
    assert any(p.startswith("materials/") and "/assets/" in p for p in paths)
    assert any(r.resource_type == "material_asset" for r in manifest.references)
    for entry in manifest.files:
        assert (destination / "files" / entry.relative_path).is_file()
    assert not (space.profile.storage_root / ".maintenance.json").exists()


def test_personal_file_catalog_has_no_legacy_course_or_export_query(recovery):
    space = recovery[0]
    sid = materials.subject(space)
    uploaded = materials.upload(space, sid, "source.md", b"# Synthetic personal catalog")
    assert uploaded.status_code == 201

    def reject_legacy_query(_connection, _cursor, statement, _parameters, _context, _many):
        lowered = statement.lower()
        assert "from export_files" not in lowered
        assert "from courses" not in lowered

    event.listen(space.engine, "before_cursor_execute", reject_legacy_query)
    try:
        with space.subjects.sessions() as session:
            references = storage_references(session, personal_only=True)
        assert len(references) == 1
        assert references[0].owner["subject_id"] == sid
        assert references[0].file_id == "d_" + UUID(uploaded.json()["id"]).hex
    finally:
        event.remove(space.engine, "before_cursor_execute", reject_legacy_query)


def test_personal_backup_rejects_absent_or_foreign_management_authority(recovery):
    space, snapshots, security, _token, tools = recovery
    from backend.app.core.settings_security import SettingsSecurityError
    with pytest.raises(SettingsSecurityError):
        BackupRestoreService.for_local_management(space.engine, profile=space.profile,
            security=security, token="not-authorized", tools=tools, snapshots=snapshots)
    wrong = space.profile.model_copy(update={"environment_id": uuid4()})
    with pytest.raises(SettingsSecurityError):
        BackupRestoreService.for_local_management(space.engine, profile=wrong,
            security=security, token=recovery[3], tools=tools, snapshots=snapshots)


@contextmanager
def restored_target(case):
    """Only this test's UUID database can be released, after instance/owner checks."""
    source = case[0]
    name = "test_personal_restore_" + uuid4().hex
    control = create_engine(source.engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with control.connect() as connection:
            assert str(connection.scalar(text("SELECT system_identifier::text FROM pg_control_system()"))) == source.profile.database_locator.instance_id
            assert not connection.scalar(text("SELECT 1 FROM pg_database WHERE datname=:name"), {"name": name})
        yield name
    finally:
        with control.connect() as connection:
            assert str(connection.scalar(text("SELECT system_identifier::text FROM pg_control_system()"))) == source.profile.database_locator.instance_id
            owner = connection.scalar(text("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=:name"), {"name": name})
            if owner is not None:
                assert owner == source.engine.url.username
                connection.execute(text(f'DROP DATABASE "{name}"'))
        control.dispose()


def test_real_restore_keeps_personal_history_and_leaves_target_closed(recovery, tmp_path):
    space, snapshots, *_ = recovery
    required = stored_history(recovery)
    with space.subjects.sessions() as session:
        subject_id = str(session.scalar(select(Subject.id)))
    first = materials.upload(space, subject_id, "v1.docx", materials.sample_docx()).json()
    second = materials.upload(space, subject_id, "v2.md", b"# Synthetic second edition").json()
    materials.index(space, first["id"])
    series = space.client.post(f"/api/learning/subjects/{subject_id}/material-series",
        json={"document_id": first["id"], "series_name": "Synthetic editions", "label": "v1"})
    assert series.status_code == 201, series.text
    version = space.client.post(f"/api/learning/subjects/{subject_id}/material-series",
        json={"document_id": second["id"], "series_id": series.json()["id"], "label": "v2"})
    assert version.status_code == 201, version.text
    unknown_id = uuid4()
    with space.subjects.sessions.begin() as session:
        session.add(OperationRun(id=unknown_id, environment_id=space.profile.environment_id,
            idempotency_key=uuid4().hex, purpose="teacher_chat", preview_digest="c"*64,
            plan_digest="d"*64, runtime_snapshot_id=next(iter(required)), state="outcome_unknown",
            request_budget=1, actual_requests=1, steps=[], active_slot="model"))
    svc = service(recovery)
    destination, manifest = svc.backup(backup_root=tmp_path / "backup", writers_stopped=True)
    assert manifest.outcome == "complete", manifest.issues
    assert {UUID(Path(entry.relative_path).stem) for entry in manifest.runtime_snapshots} == required
    assert len(manifest.configuration_revisions) == 1
    assert b"t211_synthetic_provider_" not in (destination / manifest.configuration_revisions[0].relative_path).read_bytes()
    target_root = (tmp_path / "restored").resolve()
    with restored_target(recovery) as database, isolated_test_resource_profile(target_root) as profile:
        with ConfigService(profile.config_root.parent, profile.environment_id) as target_config:
            # Configuration/credential recovery is independent: no active config is substituted.
            for entry in manifest.configuration_revisions:
                target = target_config.config / "revisions" / Path(entry.relative_path).name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((destination / entry.relative_path).read_bytes())
            target_snapshots = RuntimeSnapshotService(target_config, store=snapshots._config.credential_store)
            report = svc.restore(backup_set=destination, target_database=database,
                target_root=profile.storage_root, report_path=tmp_path / "restore.json",
                target_profile=profile, target_snapshots=target_snapshots)
            assert report.outcome == "verified", report.issues
            assert (profile.storage_root / ".maintenance.json").exists()
            assert not (target_config.config / "manifest.json").exists()
            for identifier in required:
                assert target_snapshots.load(identifier) == snapshots.load(identifier)
            assert target_snapshots.active_lease_count == 0
        restored = create_engine(space.engine.url.set(database=database))
        @event.listens_for(restored, "connect")
        def schema(dbapi, _record):
            with dbapi.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{manifest.database.schema_name}", public')
            dbapi.commit()
        try:
            with space.engine.connect() as original, restored.connect() as connection:
                for table in ("subjects", "knowledge_bases", "documents", "document_chunks", "questions", "question_revisions",
                        "question_origins", "material_series", "material_versions", "practice_sessions", "practice_items",
                        "practice_answers", "grade_attempts", "score_disputes", "conversations", "conversation_messages", "operation_runs"):
                    query = text(f'SELECT row_to_json(t)::text FROM "{table}" t ORDER BY id')
                    assert list(connection.scalars(query)) == list(original.scalars(query)), table
                assert _runtime_snapshot_ids(connection) == required
                assert connection.execute(text("SELECT state,actual_requests,active_slot FROM operation_runs WHERE id=:id"),
                    {"id": unknown_id}).one() == ("outcome_unknown", 1, "model")
        finally:
            restored.dispose()
    for entry in manifest.files:
        assert (target_root / "storage" / entry.relative_path).read_bytes() == (destination / "files" / entry.relative_path).read_bytes()


@pytest.mark.parametrize("missing", ["revision", "credential"])
def test_missing_credential_or_revision_never_claims_restored(recovery, tmp_path, missing):
    stored_history(recovery)
    svc = service(recovery)
    destination, manifest = svc.backup(backup_root=tmp_path / "backup", writers_stopped=True)
    assert manifest.outcome == "complete", manifest.issues
    with (
        restored_target(recovery) as database,
        isolated_test_resource_profile((tmp_path / "target").resolve()) as profile,
        ConfigService(profile.config_root.parent, profile.environment_id) as config,
    ):
        if missing == "credential":
            for entry in manifest.configuration_revisions:
                target = config.config / "revisions" / Path(entry.relative_path).name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((destination / entry.relative_path).read_bytes())
        target_snapshots = RuntimeSnapshotService(config)
        report = svc.restore(backup_set=destination, target_database=database,
            target_root=profile.storage_root, report_path=tmp_path / "missing.json",
            target_profile=profile, target_snapshots=target_snapshots)
        assert report.outcome == "failed"
        assert report.issues[0].code == "SNAPSHOT_DEPENDENCY_UNAVAILABLE"
        assert not profile.storage_root.exists()
        assert not target_snapshots.root.exists()


def test_missing_original_snapshot_keeps_personal_backup_failed_and_stopped(recovery, tmp_path):
    required = stored_history(recovery)
    recovery[1].path_for(next(iter(required))).unlink()
    destination, manifest = service(recovery).backup(backup_root=tmp_path / "backup", writers_stopped=True)
    assert manifest.outcome == "failed"
    assert (recovery[0].profile.storage_root / ".maintenance.json").exists()
    assert json.loads((destination / "manifest.json").read_bytes())["outcome"] == "failed"


def test_personal_backup_does_not_copy_abandoned_legacy_records(recovery, tmp_path):
    from backend.app.models import Course, User
    space = recovery[0]
    with space.subjects.sessions.begin() as session:
        user = User(username="synthetic-abandoned", email="abandoned@example.invalid", password_hash="unused")
        session.add(user)
        session.flush()
        session.add(Course(name="Synthetic abandoned course", created_by=user.id))
    destination, manifest = service(recovery).backup(backup_root=tmp_path / "backup", writers_stopped=True)
    assert manifest.outcome == "failed"
    assert manifest.issues[-1].code == "BACKUP_LEGACY_DATA_PRESENT"
    assert not (destination / "database.dump").exists()
    assert not manifest.files and not manifest.runtime_snapshots


def test_old_role_audit_is_not_silently_copied_as_personal_history(recovery, tmp_path):
    from backend.app.models.audit_log import AuditLog

    with recovery[0].subjects.sessions.begin() as session:
        session.add(AuditLog(actor_id=uuid4(), actor_role="teacher", action="old_exam",
            resource_type="exam", resource_id=str(uuid4()), result="success", detail={}))
    destination, manifest = service(recovery).backup(backup_root=tmp_path / "backup", writers_stopped=True)
    assert manifest.outcome == "failed"
    assert manifest.issues[-1].code == "BACKUP_LEGACY_DATA_PRESENT"
    assert not (destination / "database.dump").exists()


@pytest.mark.parametrize("table", ["documents", "users", "unmapped_personal_facts"])
def test_unverified_other_business_schema_blocks_before_dump(recovery, tmp_path, monkeypatch, table):
    space = recovery[0]
    schema = "test_unverified_" + uuid4().hex
    dumped = []

    def forbidden_dump(path):
        dumped.append(path)
        path.write_bytes(b"Synthetic dump; no actual unverified data copied")

    svc = service(recovery)
    monkeypatch.setattr(svc.tools, "dump", forbidden_dump)
    with space.engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.execute(text(f'CREATE TABLE "{schema}"."{table}" (fact text)'))
        connection.execute(text(f'INSERT INTO "{schema}"."{table}" VALUES (\'Synthetic forbidden archive\')'))
    try:
        destination, manifest = svc.backup(backup_root=tmp_path / "backup", writers_stopped=True)
        assert dumped == [], "pg_dump must never run before the entire database scope is verified"
        assert manifest.outcome == "failed"
        assert manifest.issues[-1].code == "UNMAPPED_SCHEMA"
        assert manifest.issues[-1].stage == "schema_scope"
        assert not (destination / "database.dump").exists()
        assert not manifest.files and not manifest.runtime_snapshots
        with space.engine.connect() as connection:
            assert connection.scalar(text(f'SELECT fact FROM "{schema}"."{table}"')) == "Synthetic forbidden archive"
    finally:
        # Only the UUID schema and table created by this exact test are removed.
        with space.engine.begin() as connection:
            connection.execute(text(f'DROP TABLE "{schema}"."{table}"'))
            connection.execute(text(f'DROP SCHEMA "{schema}"'))


def test_unregistered_bytes_are_not_copied_to_personal_backup(recovery, tmp_path):
    space = recovery[0]
    path = space.profile.storage_root / "materials" / "unregistered-old.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"Synthetic unclassified ownership; do not archive")
    destination, manifest = service(recovery).backup(backup_root=tmp_path / "backup", writers_stopped=True)
    assert manifest.outcome == "failed"
    assert manifest.issues[-1].code == "UNOWNED_MATERIAL"
    assert not (destination / "files" / "materials" / path.name).exists()


def test_expired_management_authority_cannot_start_backup(recovery, tmp_path, monkeypatch):
    svc = service(recovery)
    monkeypatch.setattr(recovery[2], "_clock", lambda: datetime.now(UTC) + timedelta(hours=1))
    with pytest.raises(SettingsSecurityError) as error:
        svc.backup(backup_root=tmp_path / "never-created", writers_stopped=True)
    assert error.value.code == "SESSION_EXPIRED"
    assert not (tmp_path / "never-created").exists()


def test_failed_drain_keeps_personal_maintenance_marker(recovery, tmp_path):
    space = recovery[0]
    with space.engine.connect() as peer:
        destination, manifest = service(recovery).backup(
            backup_root=tmp_path / "backup", writers_stopped=True, timeout=0.1)
        # Maintenance disposes its engine pool. Close this deliberately held
        # connection instead of returning it to that detached pool.
        peer.invalidate()
    assert manifest.outcome == "failed"
    assert manifest.issues[-1].code == "MAINTENANCE_NOT_DRAINED"
    assert (space.profile.storage_root / ".maintenance.json").exists()
    assert not (destination / "database.dump").exists()


def test_incomplete_personal_backup_keeps_writers_stopped(recovery, tmp_path, monkeypatch):
    """Inject a fingerprint conflict; do not damage or erase personal fixture bytes."""
    from backend.app.services import backup_restore_service as backup_module

    space = recovery[0]
    sid = materials.subject(space)
    assert materials.upload(space, sid, "source.md", b"# Synthetic integrity boundary").status_code == 201
    measured = backup_module.fingerprint

    def conflicting_digest(path):
        size, digest = measured(path)
        return (size, "0" * 64) if path.is_relative_to(space.profile.storage_root) else (size, digest)

    monkeypatch.setattr(backup_module, "fingerprint", conflicting_digest)
    _, manifest = service(recovery).backup(backup_root=tmp_path / "backup", writers_stopped=True)
    assert manifest.outcome == "incomplete"
    assert any(issue.code == "FILE_CONTENT_CHANGED" for issue in manifest.issues)
    assert (space.profile.storage_root / ".maintenance.json").exists()


def test_offline_cli_uses_setup_local_authority_without_business_jwt(recovery, tmp_path, monkeypatch, capsys):
    import sys

    from backend.app.core import bootstrap
    from backend.app.services.settings import management
    from scripts import backup_restore

    space, snapshots, *_ = recovery
    sid = materials.subject(space)
    assert materials.upload(space, sid, "source.md", b"# Synthetic local maintenance").status_code == 201
    with space.engine.connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
    url = space.engine.url.set(query={"options": f"-csearch_path={schema},public"})

    async def close():
        pass

    runtime = SimpleNamespace(credential_store=lambda: snapshots._config.credential_store,
        personal_learning_database=lambda: (url, space.profile),
        _require_config=lambda: snapshots._config, aclose=close)
    monkeypatch.setattr(management, "build_settings_runtime", lambda profile: runtime)
    monkeypatch.setattr(bootstrap, "resolve_bootstrap_context", lambda: space.client.app.state.bootstrap)
    monkeypatch.delenv("EDUAGENT_MAINTENANCE_TOKEN", raising=False)
    monkeypatch.setattr(sys, "argv", ["backup_restore.py", "--local-management", "--pg-container",
        recovery[4].container, "backup", "--backup-root", str(tmp_path / "cli-backup"), "--writers-stopped"])
    # This is genuinely offline: release the fixture's idle connection too.
    space.engine.dispose()
    assert backup_restore.main() == 0
    output = capsys.readouterr()
    assert "complete:" in output.out
    assert not output.err
    assert recovery[3] not in output.out
