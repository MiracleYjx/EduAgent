"""T016–T019: real owned PG entry/persistence and current-space security seams."""

import asyncio
import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url

from backend.app.core.bootstrap import (
    BootstrapContext,
    create_application,
    create_bootstrap_app,
)
from backend.app.schemas.settings import (
    BootstrapOptions,
    BootstrapSettings,
    ConfigurationValues,
    RuntimeKind,
)
from backend.app.services.settings.management import (
    SettingsManagementError,
    build_settings_runtime,
)
from tests.integration.test_personal_learning_foundation import migrate, seed
from tests.postgres_helpers import (
    isolated_postgres_schema,
    isolated_test_resource_profile,
)
from tests.support.credential_fakes import FakeFileSecurity, FakeProtector


def clear_connection_overrides(monkeypatch):
    for key in tuple(os.environ):
        if (
            key.upper().startswith(
                (
                    "DATABASE_",
                    "REDIS_",
                    "JWT_",
                    "LLM_",
                    "DEEPSEEK_",
                    "EMBEDDING_",
                    "RERANK_",
                )
            )
            or key == "STORAGE_ROOT"
        ):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def personal(tmp_path, monkeypatch):
    with (
        isolated_test_resource_profile((tmp_path / "space").resolve()) as profile,
        isolated_postgres_schema() as (engine, schema),
    ):
        migrate(engine, "head")
        url = make_url(os.environ["DATABASE_URL"])
        redis = make_url(os.environ["REDIS_URL"])
        clear_connection_overrides(monkeypatch)
        profile.storage_root.mkdir(parents=True)
        protector, security = FakeProtector(), FakeFileSecurity()
        runtime = build_settings_runtime(
            profile,
            credential_backend="dpapi",
            protector=protector,
            file_security=security,
        )
        store = runtime.credential_store()
        reference = store.put(
            purpose="database", secret_value=SecretStr(url.password or "")
        )
        values = ConfigurationValues.model_validate(
            {
                "business": {
                    "database_locator": {
                        "host": url.host,
                        "port": url.port,
                        "database": url.database,
                        "user": url.username,
                        "schema": schema,
                    },
                    "database_password_ref": reference.model_dump(mode="json"),
                    "redis_locator": {
                        "host": redis.host,
                        "port": redis.port,
                        "database": 15,
                    },
                    "storage_root": str(profile.storage_root),
                }
            }
        )
        config = runtime.config
        draft = config.create_draft(values, expected_revision=None, process_env={})
        config.validate(draft.revision_id, expected_revision=None, process_env={})
        config.activate(draft.revision_id, expected_revision=None, process_env={})
        context = BootstrapContext(
            settings=BootstrapSettings(
                environment=profile.environment,
                environment_id=profile.environment_id,
                runtime_kind=RuntimeKind.PYTHON,
                config_root=profile.config_root,
            ),
            bind=BootstrapOptions(port=8000),
            static_root=None,
        )
        yield runtime, context, engine, schema, profile, values, protector, security
        asyncio.run(runtime.aclose())
        monkeypatch.undo()


def test_real_subject_create_patch_shared_read_and_application_restart(personal):
    runtime, context, engine, schema, *_ = personal
    app = create_bootstrap_app(context=context, settings_runtime=runtime)
    with TestClient(app, base_url="http://127.0.0.1:8000") as first:
        response = first.post(
            "/api/learning/subjects",
            json={"name": " 数学 ", "description": "代数"},
            headers={"Origin": "http://127.0.0.1:8000"},
        )
        assert response.status_code == 201, response.text
        identity = response.json()["id"]
        assert response.json()["name"] == "数学"
        with TestClient(app, base_url="http://127.0.0.1:8000") as second:
            assert (
                second.get("/api/learning/subjects/" + identity).json()
                == response.json()
            )
            updated = second.patch(
                "/api/learning/subjects/" + identity,
                json={"name": "高等数学", "description": None},
                headers={"Origin": "http://127.0.0.1:8000"},
            )
            assert updated.status_code == 200 and updated.json()["description"] is None
        assert first.get("/api/learning/subjects").json()["subjects"] == [
            updated.json()
        ]
        for body in ({}, {"name": None}, {"name": " "}, {"owner_id": str(uuid4())}):
            assert (
                first.patch(
                    "/api/learning/subjects/" + identity,
                    json=body,
                    headers={"Origin": "http://127.0.0.1:8000"},
                ).status_code
                == 422
            )
        assert first.get("/api/learning/subjects/" + str(uuid4())).status_code == 404
    reopened = create_bootstrap_app(context=context, settings_runtime=runtime)
    with TestClient(reopened, base_url="http://127.0.0.1:8000") as client:
        assert (
            client.get("/api/learning/subjects/" + identity).json()["name"]
            == "高等数学"
        )
    with engine.connect() as c:
        assert c.scalar(text("SELECT current_schema()")) == schema
        assert c.scalar(text("SELECT count(*) FROM subjects")) == 1
        assert c.scalar(text("SELECT count(*) FROM users")) == 0


def test_missing_jwt_does_not_open_legacy_consumers(personal):
    runtime, context, *_rest, values, _protector, _security = personal
    values = values.model_copy(
        update={"business": values.business.model_copy(update={"jwt_secret_ref": None})}
    )
    with pytest.raises(
        SettingsManagementError, match="BUSINESS_CONFIGURATION_INCOMPLETE"
    ):
        runtime.app_settings_for(values)
    with TestClient(
        create_bootstrap_app(context=context, settings_runtime=runtime),
        base_url="http://127.0.0.1:8000",
    ) as client:
        for path in ("/api/admin/users", "/api/courses", "/api/files/d_" + uuid4().hex):
            assert client.get(path).status_code == 503
        assert client.get("/api/settings/").status_code == 401


def test_subject_writes_respect_maintenance_marker(personal):
    runtime, context, _engine, _schema, profile, *_ = personal
    marker = profile.storage_root / ".maintenance.json"
    marker.write_text('{"operation_id":"synthetic-maintenance"}', encoding="utf-8")
    with TestClient(
        create_bootstrap_app(context=context, settings_runtime=runtime),
        base_url="http://127.0.0.1:8000",
    ) as client:
        response = client.post(
            "/api/learning/subjects",
            json={"name": "数学"},
            headers={"Origin": "http://127.0.0.1:8000"},
        )
        assert (
            response.status_code == 503
            and response.json()["code"] == "STORAGE_MAINTENANCE"
        )
        assert client.get("/api/learning/subjects").json()["subjects"] == []
    assert marker.exists()


def test_no_jwt_configuration_opens_only_personal_entry_after_restart(personal):
    runtime, context, _engine, _schema, _profile, _values, *_ = personal
    assert runtime.config.active_revision().values.business.jwt_secret_ref is None
    settings, reason = runtime.effective_business_settings()
    assert settings is None and reason == "BUSINESS_CONFIGURATION_INCOMPLETE"
    with TestClient(
        create_application(context=context, settings_runtime=runtime),
        base_url="http://127.0.0.1:8000",
    ) as client:
        result = client.post(
            "/api/learning/subjects",
            json={"name": "无登录科目"},
            headers={"Origin": "http://127.0.0.1:8000"},
        )
        assert result.status_code == 201, result.text
        for path in ("/api/admin/users", "/api/courses", "/api/files/d_" + uuid4().hex):
            assert client.get(path).status_code == 503
        assert client.get("/api/settings/").status_code == 401
        assert client.get("/gradio/").status_code == 404


def test_personal_pending_restart_is_checked_and_accepted_without_legacy_assembly(personal):
    runtime, context, _engine, _schema, _profile, values, *_ = personal
    config = runtime.config
    previous = config.active_revision().revision_id
    changed = values.model_copy(update={"business": values.business.model_copy(update={
        "redis_locator": values.business.redis_locator.model_copy(update={"database": 14})})})
    draft = config.create_draft(changed, expected_revision=previous, process_env={})
    config.validate(draft.revision_id, expected_revision=previous, process_env={})
    pending = config.activate(draft.revision_id, expected_revision=previous, process_env={})
    assert config.read_manifest().pending_restart_revision == pending.revision_id
    with TestClient(create_application(context=context, settings_runtime=runtime), base_url="http://127.0.0.1:8000") as client:
        assert client.get("/api/learning/subjects").status_code == 200
        assert client.get("/api/admin/users").status_code == 503
    manifest = config.read_manifest()
    assert manifest.active_revision == pending.revision_id and manifest.pending_restart_revision is None


def test_personal_pending_restart_failure_preserves_active_and_candidate(personal):
    runtime, context, _engine, _schema, profile, values, *_ = personal
    config = runtime.config
    previous = config.active_revision().revision_id
    changed = values.model_copy(update={"business": values.business.model_copy(update={
        "storage_root": profile.storage_root.parent / "unadopted-storage"})})
    draft = config.create_draft(changed, expected_revision=previous, process_env={})
    config.validate(draft.revision_id, expected_revision=previous, process_env={})
    pending = config.activate(draft.revision_id, expected_revision=previous, process_env={})
    app = create_application(context=context, settings_runtime=runtime)
    assert app.state.business_assembly == "not_assembled"
    manifest = config.read_manifest()
    assert manifest.active_revision == previous and manifest.pending_restart_revision == pending.revision_id
    assert not changed.business.storage_root.exists()


def test_personal_file_seam_preserves_space_stage_path_and_integrity(personal):
    import hashlib
    import json

    from sqlalchemy.orm import Session

    from backend.app.models import Document
    from backend.app.services.file_storage_service import (
        FileStorageError,
        FileStorageService,
    )

    _runtime, _context, engine, _schema, profile, *_ = personal
    ids = seed(engine)
    path = profile.storage_root / "paper.txt"
    body = b"synthetic paper with reference answers"
    path.write_bytes(body)
    with engine.begin() as c:
        c.execute(
            text(
                "UPDATE documents SET storage_path='paper.txt', file_metadata=CAST(:meta AS json) WHERE id=:id"
            ),
            {
                "id": ids["document"],
                "meta": json.dumps(
                    {
                        "size_bytes": len(body),
                        "sha256": hashlib.sha256(body).hexdigest(),
                        "migration": {"status": "not_required"},
                    }
                ),
            },
        )
    with Session(engine) as db:
        service = FileStorageService(db, profile=profile)
        with pytest.raises(FileStorageError, match="当前科目"):
            service.personal_document_path(
                ids["document"], subject_id=uuid4(), practice_id=ids["session"]
            )
        with pytest.raises(FileStorageError, match="作答阶段"):
            service.personal_document_path(
                ids["document"], subject_id=ids["subject"], practice_id=ids["session"]
            )
    with engine.begin() as c:
        c.execute(
            text(
                "UPDATE practice_answers SET submission_state='submitted' WHERE id=:id"
            ),
            {"id": ids["answer"]},
        )
        c.execute(
            text(
                "UPDATE practice_sessions SET state='submitted', submitted_at=now() WHERE id=:id"
            ),
            {"id": ids["session"]},
        )
    with Session(engine) as db:
        service = FileStorageService(db, profile=profile)
        assert (
            service.personal_document_path(
                ids["document"], subject_id=ids["subject"], practice_id=ids["session"]
            )[0]
            == path
        )
        path.write_bytes(b"changed")
        with pytest.raises(FileStorageError, match="登记内容"):
            service.personal_document_path(
                ids["document"], subject_id=ids["subject"], practice_id=ids["session"]
            )
        document = db.get(Document, ids["document"])
        document.storage_path = "../outside.txt"
        with pytest.raises(FileStorageError, match="规范相对路径"):
            service.personal_document_path(
                ids["document"], subject_id=ids["subject"], practice_id=ids["session"]
            )
        db.rollback()


def test_local_maintenance_requires_matching_management_session(personal):
    from backend.app.core.bootstrap import build_bootstrap_security
    from backend.app.core.maintenance import MaintenanceWindow
    from backend.app.core.settings_security import (
        OperatorIdentity,
        PrincipalKind,
        SettingsSecurityError,
    )

    _runtime, context, engine, _schema, profile, *_ = personal
    security = build_bootstrap_security(context)
    with pytest.raises(SettingsSecurityError):
        MaintenanceWindow.for_local_management(
            engine, profile=profile, security=security, token="no-authority"
        )
    code = security.issue_code(
        OperatorIdentity(
            kind=PrincipalKind.LOCAL_OS, principal_id="synthetic-maintenance"
        )
    )
    token = security.exchange(code.code).token
    window = MaintenanceWindow.for_local_management(
        engine, profile=profile, security=security, token=token
    )
    assert (
        window.root == profile.storage_root
        and window.management_session.environment_id == profile.environment_id
    )
    assert window.actor_id is None
    wrong = profile.model_copy(update={"environment_id": uuid4()})
    with pytest.raises(SettingsSecurityError, match="不匹配"):
        MaintenanceWindow.for_local_management(
            engine, profile=wrong, security=security, token=token
        )
    assert not (profile.storage_root / ".maintenance.json").exists()
