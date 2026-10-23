"""Production personal factory after retirement; real owned PG, no paid model."""

import re
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, inspect, text

from backend.app.api.subjects import get_subject_service
from backend.app.core.app import create_app
from backend.app.core.config import AppSettings
from backend.app.services.settings import readiness
from tests.integration import test_personal_cutover as cutover
from tests.integration import test_personal_learning_journey as journey
from tests.integration import test_personal_recovery as recovery_tests

space = recovery_tests.space
recovery = recovery_tests.recovery


@pytest.fixture
def personal(recovery, monkeypatch):
    space = recovery[0]
    svc = cutover.service(recovery)
    plan = svc.prepare(writers_stopped=True)
    svc.execute(plan["operation_id"], plan["digest"], writers_stopped=True)
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    settings = AppSettings(_env_file=None, database_url=space.engine.url.render_as_string(hide_password=False),
        redis_url="redis://127.0.0.1:9/0", storage_root=space.profile.storage_root)
    app = create_app(settings=settings, bootstrap_context=space.client.app.state.bootstrap)
    app.dependency_overrides[get_subject_service] = lambda: space.subjects
    with TestClient(app, base_url="http://127.0.0.1:8000", headers={"Origin": "http://127.0.0.1:8000"}) as client:
        prior = space.client
        space.client = client
        try:
            yield space
        finally:
            space.client = prior


def test_factory_import_has_no_legacy_ui_auth_or_user_consumer():
    result = subprocess.run([sys.executable, "-B", "-c", ("import sys; import backend.app.core.app; "
        "assert not any(n in sys.modules for n in ['gradio','backend.app.api.auth','backend.app.api.exams',"
        "'backend.app.models.user','backend.app.services.auth_service']); print('personal factory only')")],
        capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    assert "personal factory only" in result.stdout


@pytest.mark.parametrize("path", ["/api/auth/login", "/api/admin/users", "/api/courses", "/api/exams",
    "/api/submissions", "/api/grading", "/api/results", "/api/workflow", "/api/traces", "/gradio/"])
def test_retired_entry_is_absent_from_production_factory(personal, path):
    assert personal.client.get(path).status_code == 404
    assert personal.client.post(path, json={}).status_code == 404
    assert path not in personal.client.app.openapi()["paths"]


def test_no_jwt_or_virtual_user_is_required_for_personal_journey(personal, tmp_path):
    with personal.engine.connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
        assert not cutover.LEGACY.intersection(inspect(connection).get_table_names(schema=schema))
    def reject_legacy_query(_conn, _cursor, statement, _parameters, _context, _executemany):
        assert not re.search(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+(?:\w+\.)?\"?(?:users|roles|user_roles|courses)\b", statement, re.IGNORECASE), statement
    event.listen(personal.engine, "before_cursor_execute", reject_legacy_query)
    try:
        journey.test_upload_confirm_practice_submit_and_reload(personal, tmp_path)
    finally:
        event.remove(personal.engine, "before_cursor_execute", reject_legacy_query)


def test_retired_tables_do_not_mask_missing_personal_tables(personal):
    with personal.engine.connect() as connection:
        assert readiness._probe_schema(connection).status == "ok"
        # Transaction-local rename is rolled back; no business rows are removed.
        connection.execute(text("ALTER TABLE practice_answers RENAME TO synthetic_missing_answers"))
        assert readiness._probe_schema(connection).code == "schema_drift"
        connection.rollback()


def test_setup_source_and_maintenance_protections_survive_retirement(personal):
    client = personal.client
    assert client.get("/api/settings/view").status_code == 401
    assert client.get("/api/learning/subjects", headers={"Host": "evil.invalid"}).status_code == 403
    assert client.post("/api/learning/subjects", json={"name": "untrusted"},
        headers={"Origin": "http://evil.invalid"}).status_code == 403
    request = client.build_request("POST", "/api/learning/subjects", json={"name": "missing origin"})
    request.headers.pop("Origin")
    assert client.send(request).status_code == 403
    marker = personal.profile.storage_root / ".maintenance.json"
    marker.write_text('{"purpose":"synthetic stopped space"}', encoding="utf-8")
    response = client.post("/api/learning/subjects", json={"name": "stopped"})
    assert response.status_code == 503 and response.json()["code"] == "STORAGE_MAINTENANCE"
    marker.unlink()
    assert client.post("/api/learning/subjects", json={"name": "enabled"}).status_code == 201
