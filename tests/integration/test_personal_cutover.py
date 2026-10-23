"""Approved Test cutover: actual PostgreSQL and files, never a legacy backup."""

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import inspect, text

from backend.app.core.environment import load_test_resource_profile
from backend.app.models import Course, Document, User
from backend.app.services.file_storage_service import FileStorageError
from backend.app.services.personal_cutover_service import PersonalCutoverService
from tests.integration import test_personal_recovery as recovery_tests

space = recovery_tests.space
recovery = recovery_tests.recovery

LEGACY = {
    "users", "roles", "user_roles", "courses", "exams", "exam_participants",
    "exam_questions", "submissions", "answers", "grading_results", "review_records",
    "exam_results", "diagnosis_reports", "export_files", "question_revision_comments",
    "question_source_papers",
}


def approval_container(space):
    """获批文件容器：环境根的真父目录，绝非环境根本身。

    TCR（T081）：生产侧 `approved_root` 来自独立批准文件（`scripts/backup_restore.py`
    读取 `approval["file_root"]`），语义是「被授权的上级容器」，环境根必须是它的
    严格子目录。旧夹具把回执根（等于环境根）当作批准根，触发自指批准被拒；
    这里改为夹具实际分配的上级目录，产品侧的层级/逃逸/联接校验一字未动。
    """

    root = space.profile.config_root.parent
    container = root.parent
    assert root != container and root.is_relative_to(container)
    return container


def service(case, **overrides):
    space, _snapshots, security, token, _tools = case
    receipt = load_test_resource_profile()
    with space.engine.connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
    values = {"profile": space.profile, "security": security, "token": token,
        "approved_database": receipt.database_identity.model_copy(update={"schema_name": schema}),
        "approved_root": approval_container(space)}
    values.update(overrides)
    return PersonalCutoverService.for_local_management(space.engine, **values)


def abandoned(case):
    """A positively owned old Course and original; no role or login is simulated."""
    space = case[0]
    content = b"Synthetic abandoned original: no archive or backup is authorized."
    relative = "uploads/" + uuid4().hex + ".txt"
    path = space.profile.storage_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    with space.subjects.sessions.begin() as session:
        user = User(username=uuid4().hex, email=uuid4().hex + "@example.invalid", password_hash="synthetic")
        session.add(user)
        session.flush()
        course = Course(name="Synthetic abandoned course", created_by=user.id)
        session.add(course)
        session.flush()
        document = Document(course_id=course.id, uploaded_by=user.id, original_filename="old.txt",
            file_format="txt", purpose="paper_source", storage_path=relative,
            file_metadata={"size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
                "migration": {"status": "not_required"}})
        session.add(document)
        session.flush()
        ids = {"user": user.id, "course": course.id, "document": document.id}
    return path, ids


def rows(space):
    with space.engine.connect() as connection:
        return {name: list(connection.scalars(text(f'SELECT row_to_json(t)::text FROM "{name}" t ORDER BY id')))
            for name in ("subjects", "questions", "question_revisions", "question_origins", "practice_sessions",
                "practice_items", "practice_answers", "grade_attempts", "score_disputes", "operation_runs",
                "conversations", "conversation_messages")}


def test_upgrade_only_prepares_cutover_and_never_disposes_existing_objects(recovery):
    path, ids = abandoned(recovery)
    with recovery[0].engine.connect() as connection:
        assert LEGACY <= set(inspect(connection).get_table_names(schema=connection.scalar(text("SELECT current_schema()"))))
        assert connection.scalar(text("SELECT count(*) FROM documents WHERE id=:id"), {"id": ids["document"]}) == 1
    assert path.is_file()
    assert not (recovery[0].profile.storage_root / ".maintenance.json").exists()


def test_cutover_preserves_personal_history_config_credentials_and_unknown_trace(recovery):
    space, snapshots, *_ = recovery
    required = recovery_tests.stored_history(recovery)
    before = rows(space)
    config_bytes = {p.relative_to(space.profile.config_root.parent): p.read_bytes()
        for p in space.profile.config_root.parent.rglob("*") if p.is_file()
        and not p.is_relative_to(space.profile.storage_root) and p.name != "writer.lock"}
    path, _ids = abandoned(recovery)
    unknown = uuid4()
    with space.engine.begin() as connection:
        connection.execute(text("INSERT INTO agent_runs(id,agent_type,request_id,status) VALUES (:id,'independent','synthetic','success')"), {"id": unknown})
    svc = service(recovery)
    plan = svc.prepare(writers_stopped=True)
    assert path.exists(), "Preview/prepare must not dispose bytes"
    assert plan["tables"] == sorted(LEGACY)
    assert (space.profile.storage_root / ".maintenance.json").is_file()
    result = svc.execute(plan["operation_id"], plan["digest"], writers_stopped=True)
    assert result["state"] == "complete"
    assert result["deleted_rows"]["documents"] == 1
    assert result["deleted_files"] == 1
    assert not path.exists()
    with space.engine.connect() as connection:
        tables = set(inspect(connection).get_table_names(schema=connection.scalar(text("SELECT current_schema()"))))
        assert not (LEGACY & tables)
        assert "workflow_runs" in tables and "agent_runs" in tables
        assert connection.scalar(text("SELECT count(*) FROM agent_runs WHERE id=:id"), {"id": unknown}) == 1
        assert connection.scalar(text("SELECT count(*) FROM documents WHERE course_id IS NOT NULL")) == 0
    assert rows(space) == before
    for relative, content in config_bytes.items():
        assert (space.profile.config_root.parent / relative).read_bytes() == content
    assert {identifier for identifier in required if snapshots.load(identifier)} == required
    assert not (space.profile.storage_root / ".maintenance.json").exists()
    assert service(recovery).execute(plan["operation_id"], plan["digest"], writers_stopped=True) == result


@pytest.mark.parametrize("point", ["after_file_unlink", "after_database_commit"])
def test_cutover_process_interruption_reenters_same_durable_plan(recovery, monkeypatch, point):
    from backend.app.services import personal_cutover_service as module
    space = recovery[0]
    recovery_tests.stored_history(recovery)
    before = rows(space)
    path, _ids = abandoned(recovery)
    svc = service(recovery)
    plan = svc.prepare(writers_stopped=True)

    def interrupted(stage):
        if stage == point:
            raise KeyboardInterrupt("Synthetic process interruption")

    monkeypatch.setattr(module, "_checkpoint", interrupted)
    with pytest.raises(KeyboardInterrupt):
        svc.execute(plan["operation_id"], plan["digest"], writers_stopped=True)
    assert not path.exists()
    assert (space.profile.storage_root / ".maintenance.json").exists()
    monkeypatch.setattr(module, "_checkpoint", lambda _stage: None)
    result = service(recovery).execute(plan["operation_id"], plan["digest"], writers_stopped=True)
    assert result["state"] == "complete"
    assert rows(space) == before
    assert result["deleted_files"] == 1


def test_personal_json_reference_blocks_before_any_disposal(recovery):
    space = recovery[0]
    path, ids = abandoned(recovery)
    sid = recovery_tests.materials.subject(space)
    with space.engine.begin() as connection:
        connection.execute(text("INSERT INTO practice_sessions(id,subject_id,state,conditions) VALUES (:id,:sid,'preview',CAST(:value AS jsonb))"),
            {"id": uuid4(), "sid": sid, "value": json.dumps({"document_id": str(ids["document"])})})
    with pytest.raises(FileStorageError) as error:
        service(recovery).prepare(writers_stopped=True)
    assert error.value.code == "CUTOVER_RETAINED_REFERENCE"
    assert path.exists()
    with space.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM users")) == 1
    assert (space.profile.storage_root / ".maintenance.json").exists()


def test_file_changes_after_confirmation_keep_stopped_and_require_new_review(recovery):
    path, _ids = abandoned(recovery)
    svc = service(recovery)
    plan = svc.prepare(writers_stopped=True)
    path.write_bytes(b"Synthetic changed bytes")
    with pytest.raises(FileStorageError) as error:
        svc.execute(plan["operation_id"], plan["digest"], writers_stopped=True)
    assert error.value.code == "CUTOVER_PLAN_CHANGED"
    assert path.read_bytes() == b"Synthetic changed bytes"
    with recovery[0].engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM users")) == 1


@pytest.mark.parametrize("change", ["instance", "database", "schema", "root", "same"])
def test_cutover_rejects_wrong_approved_identity(recovery, change):
    svc = service(recovery)
    if change == "root":
        svc.approved_root = svc.profile.storage_root / "unrelated"
    elif change == "same":
        # 自指批准（批准根等于环境根）永远不成立：层级校验不能靠调用者自称。
        svc.approved_root = svc.profile.config_root.parent
    else:
        key = {"instance": "instance_id", "database": "database", "schema": "schema_name"}[change]
        svc.approved_database = svc.approved_database.model_copy(update={key: "unapproved"})
    with pytest.raises((FileStorageError, ValueError)):
        svc.prepare(writers_stopped=True)
    with recovery[0].engine.connect() as connection:
        assert "users" in inspect(connection).get_table_names(schema=connection.scalar(text("SELECT current_schema()")))


def test_confirmation_digest_and_writers_stopped_are_required(recovery):
    path, _ids = abandoned(recovery)
    svc = service(recovery)
    with pytest.raises(FileStorageError):
        svc.prepare(writers_stopped=False)
    plan = svc.prepare(writers_stopped=True)
    with pytest.raises(FileStorageError):
        svc.execute(plan["operation_id"], "0" * 64, writers_stopped=True)
    assert path.exists()


def test_maintenance_stops_non_file_personal_writers_before_execution(recovery):
    space = recovery[0]
    sid = recovery_tests.materials.subject(space)
    svc = service(recovery)
    plan = svc.prepare(writers_stopped=True)
    for suffix, body in ((f"subjects/{sid}/practices/preview", {"count": 1}),
            (f"subjects/{sid}/conversations", {"title": "must not be created"}),
            ("subjects", {"name": "must not be created"})):
        response = space.client.post("/api/learning/" + suffix, json=body)
        assert response.status_code == 503, response.text
        assert response.json()["code"] == "STORAGE_MAINTENANCE"
    svc.execute(plan["operation_id"], plan["digest"], writers_stopped=True)
    assert space.client.post("/api/learning/subjects", json={"name": "Enabled space"}).status_code == 201


def test_current_consumers_work_after_retirement_without_legacy_queries(recovery, tmp_path):
    from tests.integration import test_personal_learning_journey as journey

    space = recovery[0]
    abandoned(recovery)
    svc = service(recovery)
    plan = svc.prepare(writers_stopped=True)
    svc.execute(plan["operation_id"], plan["digest"], writers_stopped=True)
    # Actual upload/extraction/admission/selection/submission/reload over HTTP;
    # the existing journey explicitly accounts for its two Mock HTTP requests.
    journey.test_upload_confirm_practice_submit_and_reload(space, tmp_path)


def test_real_personal_restore_after_retirement(recovery, tmp_path):
    abandoned(recovery)
    svc = service(recovery)
    plan = svc.prepare(writers_stopped=True)
    svc.execute(plan["operation_id"], plan["digest"], writers_stopped=True)
    # Same real pg_dump/pg_restore oracle as T074, now with the sixteen tables
    # physically absent; includes vectors, materials, grades, disputes and chat.
    recovery_tests.test_real_restore_keeps_personal_history_and_leaves_target_closed(recovery, tmp_path)


def test_database_failure_rolls_back_tables_but_never_claims_files_were_restored(recovery, monkeypatch):
    from backend.app.services import personal_cutover_service as module

    path, ids = abandoned(recovery)
    svc = service(recovery)
    plan = svc.prepare(writers_stopped=True)
    retire = module._migration.retire_approved_tables

    def fail_after_ddl(connection, schema):
        retire(connection, schema)
        raise RuntimeError("Synthetic transactional DDL failure")

    monkeypatch.setattr(module._migration, "retire_approved_tables", fail_after_ddl)
    with pytest.raises(RuntimeError):
        svc.execute(plan["operation_id"], plan["digest"], writers_stopped=True)
    assert not path.exists(), "A database rollback cannot resurrect disposed bytes"
    with recovery[0].engine.connect() as connection:
        assert LEGACY <= set(inspect(connection).get_table_names(schema=svc.approved_database.schema_name))
        assert connection.scalar(text("SELECT count(*) FROM documents WHERE id=:id"), {"id": ids["document"]}) == 1
        assert connection.execute(text("SELECT state,phase FROM personal_cutovers")).one() == ("failed", "files_disposing")
    monkeypatch.setattr(module._migration, "retire_approved_tables", retire)
    assert service(recovery).execute(plan["operation_id"], plan["digest"], writers_stopped=True)["state"] == "complete"


def test_preflight_failure_is_durable_and_explicitly_reentered(recovery, monkeypatch):
    svc = service(recovery)
    collect = svc._files
    monkeypatch.setattr(svc, "_files", lambda *_: (_ for _ in ()).throw(OSError("Synthetic preflight read failure")))
    with pytest.raises(OSError):
        svc.prepare(writers_stopped=True)
    marker = json.loads((recovery[0].profile.storage_root / ".maintenance.json").read_bytes())
    assert marker["state"] == "failed" and marker["phase"] == "preflight"
    assert marker["error_code"] == "CUTOVER_PREFLIGHT_FAILED"
    monkeypatch.setattr(svc, "_files", collect)
    plan = svc.prepare(writers_stopped=True, resume_operation_id=UUID(marker["operation_id"]))
    assert plan["operation_id"] == marker["operation_id"]
    svc.execute(plan["operation_id"], plan["digest"], writers_stopped=True)


def test_completed_disposal_with_marker_removal_failure_stays_stopped_until_explicit_retry(recovery, monkeypatch):
    svc = service(recovery)
    plan = svc.prepare(writers_stopped=True)
    unlink = Path.unlink
    marker = recovery[0].profile.storage_root / ".maintenance.json"

    def fail_marker(path, *args, **kwargs):
        if path == marker:
            raise PermissionError("Synthetic marker release failure")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_marker)
    with pytest.raises(PermissionError):
        svc.execute(plan["operation_id"], plan["digest"], writers_stopped=True)
    assert marker.exists()
    monkeypatch.setattr(Path, "unlink", unlink)
    assert service(recovery).execute(plan["operation_id"], plan["digest"], writers_stopped=True)["state"] == "complete"
    assert not marker.exists()


def test_concurrent_reentry_does_not_own_the_active_operation(recovery, monkeypatch):
    from backend.app.services import personal_cutover_service as module

    abandoned(recovery)
    svc = service(recovery)
    plan = svc.prepare(writers_stopped=True)
    paused, release = threading.Event(), threading.Event()

    def pause(stage):
        if stage == "after_file_unlink":
            paused.set()
            assert release.wait(20), "Test must release the active operator"

    monkeypatch.setattr(module, "_checkpoint", pause)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(svc.execute, plan["operation_id"], plan["digest"], writers_stopped=True)
        try:
            assert paused.wait(20)
            with pytest.raises(FileStorageError) as error:
                service(recovery).execute(plan["operation_id"], plan["digest"], writers_stopped=True)
            assert error.value.code == "MAINTENANCE_IN_PROGRESS"
        finally:
            release.set()
        assert first.result(timeout=20)["state"] == "complete"


def test_local_cli_prepares_and_executes_the_same_real_operation(recovery, tmp_path, monkeypatch, capsys):
    import sys
    from types import SimpleNamespace

    from backend.app.core import bootstrap
    from backend.app.services.settings import management
    from scripts import backup_restore

    space, snapshots, *_ = recovery
    path, _ids = abandoned(recovery)
    expected = service(recovery).approved_database
    url = space.engine.url.set(query={"options": f"-csearch_path={expected.schema_name},public"})

    async def close():
        pass

    runtime = SimpleNamespace(credential_store=lambda: snapshots._config.credential_store,
        personal_learning_database=lambda: (url, space.profile), aclose=close)
    monkeypatch.setattr(management, "build_settings_runtime", lambda _profile: runtime)
    monkeypatch.setattr(bootstrap, "resolve_bootstrap_context", lambda: space.client.app.state.bootstrap)
    approval = tmp_path / "approved-target.json"
    approval.write_text(json.dumps({"database_identity": expected.model_dump(mode="json"),
        "file_root": str(approval_container(space))}), encoding="utf-8")
    args = ["backup_restore.py", "--local-management", "cutover-prepare", "--approved-target", str(approval), "--writers-stopped"]
    monkeypatch.setattr(sys, "argv", args)
    space.engine.dispose()
    assert backup_restore.main() == 0
    plan = json.loads(capsys.readouterr().out)
    assert path.exists()
    monkeypatch.setattr(sys, "argv", ["backup_restore.py", "--local-management", "cutover-execute",
        "--approved-target", str(approval), "--writers-stopped", "--operation-id", plan["operation_id"], "--digest", plan["digest"]])
    assert backup_restore.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["operation_id"] == plan["operation_id"] and result["state"] == "complete"
    assert not path.exists()


def test_proven_old_workflow_audit_and_staged_media_are_disposed_without_losing_shared_tables(recovery):
    from backend.app.domain.enums import ExtractedBy, QuestionType
    from backend.app.models import (
        AgentRun,
        Answer,
        AuditLog,
        Exam,
        ExtractedQuestion,
        PaperImport,
        Question,
        SourcePage,
        Submission,
        WorkflowRun,
    )

    space = recovery[0]
    path, old = abandoned(recovery)
    page_id, asset_id = uuid4(), uuid4()
    media = []
    for label in ("page", "asset"):
        relative = "papers/" + uuid4().hex + ".png"
        target = space.profile.storage_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        # Disposal verifies bytes and ownership; it does not run image inference.
        content = ("Synthetic owned old " + label).encode()
        target.write_bytes(content)
        metadata = {"size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
            "migration": {"status": "not_required"}}
        media.append((target, relative, metadata))
    with space.subjects.sessions.begin() as session:
        question = Question(course_id=old["course"], created_by=old["user"], type=QuestionType.SINGLE_CHOICE,
            content="Synthetic abandoned question", knowledge_points=[], score=10)
        exam = Exam(course_id=old["course"], created_by=old["user"], title="Synthetic abandoned exam")
        session.add_all([question, exam])
        session.flush()
        submission = Submission(exam_id=exam.id, student_id=old["user"])
        imported = PaperImport(course_id=old["course"], uploaded_by=old["user"],
            document_id=old["document"], original_filename="old.txt", status="Uploaded")
        session.add_all([submission, imported])
        session.flush()
        answer = Answer(submission_id=submission.id, question_id=question.id, content="A")
        session.add(answer)
        session.flush()
        workflow = WorkflowRun(workflow_id=uuid4().hex, request_id=uuid4().hex,
            submission_id=submission.id, current_answer_id=answer.id)
        session.add(workflow)
        session.flush()
        session.add(AgentRun(agent_type="old_grading", workflow_id=workflow.workflow_id,
            user_id=old["user"], request_id=workflow.request_id, status="success"))
        session.add(AuditLog(actor_id=old["user"], actor_role="teacher", action="old_exam",
            resource_type="exam", resource_id=str(exam.id), result="success", detail={}))
        session.add(SourcePage(id=page_id, paper_import_id=imported.id, page_number=1,
            image_path=media[0][1], width=1, height=1, file_metadata=media[0][2]))
        session.add(ExtractedQuestion(paper_import_id=imported.id, extracted_by=ExtractedBy.TEXT,
            source_page_ids=[str(page_id)], assets=[{"id": str(asset_id), "file_id": "a_" + asset_id.hex,
                "asset_type": "figure", "source_page_id": str(page_id),
                "file_meta": {**media[1][2], "storage_path": media[1][1]}}]))
    svc = service(recovery)
    plan = svc.prepare(writers_stopped=True)
    result = svc.execute(plan["operation_id"], plan["digest"], writers_stopped=True)
    assert result["deleted_rows"]["workflow_runs"] == 1
    assert result["deleted_rows"]["agent_runs"] == 1
    assert result["deleted_rows"]["audit_logs"] == 1
    assert result["deleted_rows"]["extracted_questions"] == 1
    assert result["deleted_files"] == 3
    assert not path.exists() and all(not entry[0].exists() for entry in media)
    with space.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM workflow_runs")) == 0
        assert connection.scalar(text("SELECT count(*) FROM agent_runs")) == 0
        assert connection.scalar(text("SELECT count(*) FROM audit_logs")) == 0


@pytest.mark.parametrize("after_preview", [False, True])
def test_unknown_receipt_alias_prevents_disposal_even_without_a_database_row(recovery, after_preview):
    from datetime import UTC, datetime

    from backend.app.schemas.file_storage import OperationReceipt

    path, _ids = abandoned(recovery)
    svc = service(recovery)
    plan = svc.prepare(writers_stopped=True) if after_preview else None
    now = datetime.now(UTC)
    receipt = OperationReceipt(operation_id=uuid4(), resource_type="document", resource_id=uuid4(),
        owner={"subject_id": str(uuid4())}, candidate_locator=path.relative_to(recovery[0].profile.storage_root).as_posix(),
        stage="written", started_at=now, updated_at=now)
    unknown = path.with_name(receipt.operation_id.hex + ".receipt.json")
    unknown.write_text(receipt.model_dump_json(), encoding="utf-8")
    with pytest.raises(FileStorageError) as error:
        if after_preview:
            svc.execute(plan["operation_id"], plan["digest"], writers_stopped=True)
        else:
            svc.prepare(writers_stopped=True)
    assert error.value.code == ("CUTOVER_PLAN_CHANGED" if after_preview else "CUTOVER_RETAINED_REFERENCE")
    assert path.exists() and unknown.exists()
