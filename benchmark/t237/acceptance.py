"""Three-request acceptance entry. Dry run uses synthetic DPAPI and Fake HTTP."""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import socket
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
ALLOWED_ENV = {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "SYSTEMDRIVE",
               "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA", "OS", "USERPROFILE",
               "LOCALAPPDATA", "APPDATA", "TEMP", "TMP"}
for name in list(os.environ):
    if name.upper() not in ALLOWED_ENV:
        del os.environ[name]
os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", GRADIO_ANALYTICS_ENABLED="false")
logging.disable(logging.CRITICAL)

import httpx
import psycopg
from psycopg import sql
from pydantic import SecretStr, ValidationError
from redis import Redis
from sqlalchemy import create_engine, func
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from backend.app.core.config import AppSettings
from backend.app.core.environment import resolve_environment_profile
from backend.app.core.retry_policy import RetryPolicy
from backend.app.domain.enums import DocumentStatus, QuestionType
from backend.app.models import Course, Document, DocumentChunk, KnowledgeBase, User
from backend.app.schemas.settings import CredentialReference
from backend.app.services.grading.grading_context import (
    SubjectiveGradingSource,
    build_grading_context,
    build_query_text,
)
from backend.app.services.grading.subjective_grader import (
    SubjectiveGrader,
    build_grading_messages,
)
from backend.app.services.settings.management import (
    build_settings_runtime,
    run_alembic_upgrade,
)
from backend.app.services.settings.runtime_snapshot import RuntimeSnapshotService
from benchmark.t237.budget import MODEL, BudgetLedger, BudgetTransport
from benchmark.t237.token_budget import OfflineInputBound

AppSettings.model_config["env_file"] = None
wire = contextvars.ContextVar("t237_budgeted_wire", default=False)
live = "--live" in sys.argv
resource_root = ROOT / ".cache/t237-mvp-20261019"
resources = json.loads((resource_root / "resources.json").read_text())
target = Path("D:/YJX/EduAgent-T237-Test") if live else resource_root / ("dry-" + uuid4().hex)
tokenizer = OfflineInputBound(resource_root / "tokenizer/deepseek_v4_tokenizer")
authorized_ips = {r[4][0] for r in socket.getaddrinfo("api.deepseek.com", 443, type=socket.SOCK_STREAM)} if live else set()


def audit(event, args):
    if event == "open" and isinstance(args[0], (str, bytes)) and Path(os.fsdecode(args[0])).name == ".env":
        raise RuntimeError("DOTENV_FORBIDDEN")
    if event != "socket.connect":
        return
    address = args[1]
    if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1", "localhost"):
        if address[1] in (resources["pg_port"], resources["redis_port"]):
            return
        frame = sys._getframe(1)
        while frame:
            if hasattr(socket, "_fallback_socketpair") and frame.f_code is socket._fallback_socketpair.__code__:
                return
            frame = frame.f_back
    if live and wire.get() and isinstance(address, tuple) and address[0] in authorized_ips and address[1] == 443:
        return
    raise RuntimeError("NETWORK_OUTSIDE_ACCEPTANCE")


sys.addaudithook(audit)


def emit(**facts):
    print(json.dumps(facts, ensure_ascii=True, default=str), flush=True)


class WireTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.inner = httpx.AsyncHTTPTransport(retries=0)
    async def handle_async_request(self, request):
        token = wire.set(True)
        try:
            return await self.inner.handle_async_request(request)
        finally:
            wire.reset(token)
    async def aclose(self):
        await self.inner.aclose()


def verify_resources():
    for item in resources["containers"]:
        actual = json.loads(subprocess.check_output(["docker", "inspect", item["id"]], text=True))[0]
        assert actual["Id"] == item["id"] and actual["Config"]["Labels"]["eduagent.test.run"] == resources["run"]
    with psycopg.connect(host="127.0.0.1", port=resources["pg_port"], dbname="eduagent_test",
                         user="eduagent_test_runner", password="synthetic-mvp-runner") as db:
        identity = db.execute("SELECT current_user,current_database(),current_schema(),system_identifier::text FROM pg_control_system()").fetchone()
        assert identity == ("eduagent_test_runner", "eduagent_test", "public", resources["pg_instance"])
        assert db.execute("SELECT rolsuper,rolcreatedb FROM pg_roles WHERE rolname=current_user").fetchone() == (False, False)
        schema = "test_" + uuid4().hex
        db.execute(sql.SQL("CREATE SCHEMA {} AUTHORIZATION eduagent_test_runner").format(sql.Identifier(schema)))
    redis = Redis(host="127.0.0.1", port=resources["redis_port"], socket_timeout=3)
    try:
        assert redis.info("server")["run_id"] == resources["redis_instance"]
    finally:
        redis.close()
    return schema


async def exercise():
    schema = verify_resources()
    url = URL.create("postgresql+psycopg", username="eduagent_test_runner",
                     password="synthetic-mvp-runner", host="127.0.0.1", port=resources["pg_port"],
                     database="eduagent_test", query={"options": "-csearch_path=" + schema + ",public"})
    run_alembic_upgrade(url.render_as_string(hide_password=False))
    engine = create_engine(url)
    runtime = snapshots = probes = None
    clients = []
    state = None
    try:
        if live:
            profile_path = target / "test-resource-profile.json"
            assert target == target.resolve()
            state = BudgetLedger(target / "acceptance/t237-20261019.sqlite3")
            assert state.rows() == [], "GRANT_ALREADY_USED"
        else:
            target.mkdir(parents=True)
            data = json.loads((resource_root / "profile.json").read_text())
            data.update(root=str(target.resolve()), environment_id=str(uuid4()))
            profile_path = target / "test-resource-profile.json"
            profile_path.write_text(json.dumps(data))
            path = target / "acceptance.sqlite3"
            BudgetLedger.initialize(path)
            state = BudgetLedger(path)
        env = {"EDUAGENT_ENVIRONMENT": "test", "EDUAGENT_TEST_RESOURCE_PROFILE": str(profile_path.resolve())}
        os.environ.update(env)
        profile = resolve_environment_profile(process_env=env)
        assert profile.config_root.parent == target.resolve()
        runtime = build_settings_runtime(profile, credential_backend="dpapi")
        assert runtime.failure is None
        config = runtime.config
        assert config is not None and config.active_revision() is None
        store = runtime.credential_store()
        if live:
            assert str(profile.environment_id) == "d9f88942-3a7c-4634-8c2f-83431e97965f"
            reference = CredentialReference(credential_id=UUID("9f7cefda-2fd2-4413-b041-897a3b5b92d4"),
                version=UUID("50a51ead-acf2-4d4c-9aa0-1a362a7b655a"), environment_id=profile.environment_id,
                backend="dpapi", purpose="provider")
            assert store.status(reference, reference.version).status == "available"
        else:
            reference = store.put(purpose="provider", secret_value=SecretStr("synthetic-dry-run-only"))
        jwt = store.put(purpose="jwt", secret_value=SecretStr("synthetic-t237-jwt-" + uuid4().hex))
        database = store.put(purpose="database", secret_value=SecretStr("synthetic-mvp-runner"))
        profile_id, revision = uuid4(), uuid4()
        values = {
            "business": {"database_locator": {"host": "127.0.0.1", "port": resources["pg_port"],
                "database": "eduagent_test", "user": "eduagent_test_runner", "schema_name": schema},
                "database_password_ref": database.model_dump(mode="json"),
                "redis_locator": {"host": "127.0.0.1", "port": resources["redis_port"]},
                "storage_root": str(target / "storage"), "jwt_secret_ref": jwt.model_dump(mode="json")},
            "providers": {str(profile_id): {"profile_id": str(profile_id), "profile_revision": str(revision),
                "display_name": "deepseek-t237-test", "adapter_id": "openai_compatible",
                "model_id": MODEL, "base_url": "https://api.deepseek.com", "auth_mode": "api_key",
                "credential_ref": reference.model_dump(mode="json"), "declared_capabilities": ["text", "json_object"],
                "parameters": {"max_tokens": 2048, "temperature": 0}, "enabled": True}},
            "bindings": {"subjective_grading": {"task_kind": "subjective_grading",
                "profile_id": str(profile_id), "profile_revision": str(revision), "retrieval_mode": "keyword"}},
            "policies": {"confidence_threshold": 0.8}}
        draft = config.create_draft(values, expected_revision=None, process_env={})
        config.validate(draft.revision_id, expected_revision=None, process_env={})
        selected = draft.values.providers[str(profile_id)]
        probes = runtime.probes()
        phase = "text"
        captured_lengths = []

        async def fake_response(request):
            body = json.loads(request.content)
            captured_lengths.append({"phase": phase, "input_upper_bound": tokenizer(body)})
            output = {"ok": True} if phase != "grading" else {
                "score": 2, "confidence": 0.95, "reason": "The answer states the correct sum.",
                "correct_points": ["2 plus 2 equals 4"], "missing_knowledge_points": [],
                "suggestions": ["Keep checking arithmetic carefully."]}
            return httpx.Response(200, json={"id": "synthetic", "object": "chat.completion", "created": 1,
                "model": "deepseek-flash", "usage": {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140},
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(output)}}]})

        def http_factory(**kwargs):
            inner = WireTransport() if live else httpx.MockTransport(fake_response)
            client = httpx.AsyncClient(transport=BudgetTransport(state, phase, inner, count_input=tokenizer), **kwargs)
            clients.append(client)
            return client
        probes._http_factory = http_factory

        with Session(engine) as session:
            user = User(username="t237-" + uuid4().hex, email=uuid4().hex + "@test.invalid",
                        password_hash="synthetic-unusable-hash")
            session.add(user); session.flush()
            course = Course(name="T237 synthetic arithmetic", created_by=user.id)
            session.add(course); session.flush()
            kb = KnowledgeBase(course_id=course.id, name="T237 synthetic keyword corpus")
            session.add(kb); session.flush()
            document = Document(course_id=course.id, knowledge_base_id=kb.id, uploaded_by=user.id,
                original_filename="synthetic.txt", file_format="txt", status=DocumentStatus.READY)
            session.add(document); session.flush()
            source = SubjectiveGradingSource(question_type=QuestionType.SHORT_ANSWER, course_id=course.id,
                question_content="What is 2 plus 2?", reference_answer="2 plus 2 equals 4.",
                student_answer="2 plus 2 equals 4.", scoring_rubric="Award 2 points for the correct answer 4.",
                knowledge_base_ids=(str(kb.id),))
            content = build_query_text(source) + "\nAddition combines quantities. Two items plus two items make four items."
            chunk = DocumentChunk(document_id=document.id, course_id=course.id, knowledge_base_id=kb.id,
                chunk_index=0, content=content, search_vector=func.to_tsvector("simple", content),
                chunk_metadata={"source": "t237-synthetic"})
            session.add(chunk); session.commit()
            # Validate real keyword retrieval, production prompt/schema and budget before any paid request.
            from backend.app.ai.retrieval.keyword_search import KeywordSearchRetriever
            context = await build_grading_context(session, source, mode="keyword_only",
                retriever=KeywordSearchRetriever(), settings=AppSettings(_env_file=None,
                database_url=url.render_as_string(hide_password=False), redis_url=f"redis://127.0.0.1:{resources['redis_port']}/0",
                JWT_SECRET_KEY="synthetic-t237-preflight-jwt-secret", confidence_threshold=0.8,
                llm_provider="none", embedding_provider="none", rerank_provider="none"))
            context.ensure_sufficient()
            assert context.retrieved_context_ids == [str(chunk.id)] or tuple(context.retrieved_context_ids) == (str(chunk.id),)
                        # Provider appends the response schema; use its actual SDK preparation in the dry run.
            prepared = build_grading_messages(context, max_score=2)
            assert tokenizer({"messages": prepared}) <= 8192
            emit(event="preflight", live=live, database_identity=True, keyword_contexts=1,
                 credential_reference=str(reference.credential_id), spent=len(state.rows()))
            evidence = []
            for phase in ("text", "json_object"):
                result = await probes.run(candidate_revision=draft.revision_id, profile=selected,
                    capability=phase, allow_billable=True, timeout_seconds=30)
                emit(event="probe", phase=phase, status=result.status, code=result.safe_error_code,
                     usage=result.usage, elapsed_ms=result.elapsed_ms)
                if result.status != "succeeded":
                    raise RuntimeError("PROBE_NOT_SUCCESSFUL")
                state.confirm(phase)
                evidence.append(result.probe_id)
            active = config.activate(draft.revision_id, expected_revision=None, process_env={}, probe_ids=evidence)
            assert active.status == "active"
            phase = "grading"
            grading_http = http_factory(timeout=30, follow_redirects=False, trust_env=False)
            def client_factory(task_kind, provider_profile):
                assert task_kind == "subjective_grading"
                return runtime.registry.create(provider_profile, credential_store=store, http_client=grading_http,
                    retry_policy=RetryPolicy(max_retries=0), timeout=30)
            snapshots = RuntimeSnapshotService(config, store=store, client_factory=client_factory)
            run = snapshots.open_run(workflow_id="t237-" + uuid4().hex, request_id=str(uuid4()),
                task_kinds=("subjective_grading",), storage_root=target / "storage")
            result = await SubjectiveGrader().grade(session, source, max_score=2, runtime=run)
            assert 0 <= result.score <= 2 and 0 <= result.confidence <= 1
            assert result.reason and result.suggestions and result.retrieved_context_ids == [str(chunk.id)]
            assert run.snapshot.resolved_task_bindings["subjective_grading"].credential_ref == reference
            assert snapshots.active_lease_count == 0
            state.confirm("grading")
            emit(event="grading", score=str(result.score), max_score=str(result.max_score),
                 confidence=float(result.confidence), structured=True, context_count=len(result.retrieved_context_ids),
                 review_status=result.review_status, adapter="openai_compatible",
                 requested_model=run.requested_model("subjective_grading"))
        emit(event="complete", live=live, ledger=state.rows(), dry_input_bounds=captured_lengths)
    finally:
        if snapshots is not None: await snapshots.aclose()
        if runtime is not None: await runtime.aclose()
        for client in clients: await client.aclose()
        engine.dispose()
        if state is not None: emit(event="resource_release", http_closed=all(c.is_closed for c in clients), ledger=state.rows())


if __name__ == "__main__":
    try:
        asyncio.run(exercise())
    except BaseException as error:  # noqa: BLE001 - secrets must never enter traceback output
        # Never stringify upstream exceptions or responses.
        import traceback
        frames = [(Path(f.filename).name, f.lineno, f.name) for f in traceback.extract_tb(error.__traceback__)]
        emit(event="failure_location", frames=frames)
        if isinstance(error, ValidationError):
            emit(event="validation_fields", errors=[{"location": list(e["loc"]), "type": e["type"]} for e in error.errors(include_input=False, include_url=False, include_context=False)])
        code = getattr(error, "code", None)
        emit(event="failed", type=type(error).__name__, code=code if isinstance(code, str) and code.isupper() else None)
        raise SystemExit(1) from None
