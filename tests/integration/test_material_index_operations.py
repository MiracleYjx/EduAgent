"""Material indexing reaches READY only through explicit durable operations."""
import asyncio
import json
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from backend.app.ai.retrieval.base import RetrievalFilters
from backend.app.ai.retrieval.keyword_search import KeywordSearchRetriever
from backend.app.models.document_chunk import DocumentChunk
from backend.app.models.operation_run import OperationRun
from backend.app.services.learning_material_service import MaterialError
from backend.app.services.learning_operations import (
    LearningOperations,
    OperationError,
    StepPlan,
)
from backend.app.services.material_indexing import MaterialIndexing
from tests.integration import test_learning_material_versions as materials
from tests.support.runtime_consumer_fakes import add_profile, bind_task
from tests.support.runtime_settings_fakes import settings_harness

space = materials.space


@pytest.fixture
def index_env(space, tmp_path, monkeypatch, request):
    local = getattr(request, "param", "remote") == "local"
    native = []
    class NativeModel:
        def get_embedding_dimension(self): return 1024
        def encode(self, texts, **kw):
            native.extend(texts)
            return [[1.0]+[0.0]*1023 for _ in texts]
    if local:
        monkeypatch.setattr("backend.app.ai.embedding.providers.local.load_sentence_transformer", lambda _:NativeModel())
    harness = settings_harness(tmp_path/"index-config", environment_id=space.profile.environment_id)
    values = harness.values("index-text")
    directory = (tmp_path/"local-model").resolve(); directory.mkdir()
    pid, rev = add_profile(values, adapter_id="local" if local else "openai_compatible",
        model_id="index-local" if local else "index-remote", capabilities=["embedding"],
        base_url="http://127.0.0.1:9/v1", parameters={"dimension":1024,"batch_size":2},
        resource_directory=directory if local else None)
    bind_task(values,"embedding",pid,rev)
    calls=[]
    async def respond(request):
        body=json.loads(request.content)
        if request.url.path.endswith("embeddings"):
            calls.append(body)
            return httpx.Response(200,json={"model":"index-remote","object":"list",
                "data":[{"object":"embedding","index":i,"embedding":[1.0]+[0.0]*1023}
                    for i,_ in enumerate(body["input"])]})
        return httpx.Response(200,json={"id":"probe","choices":[{"index":0,
            "message":{"role":"assistant","content":'{"ok":true}'},"finish_reason":"stop"}]})
    harness.admission.probes._http_factory=lambda **kw:httpx.AsyncClient(transport=httpx.MockTransport(respond),**kw)
    draft=harness.draft(values)
    async def activate():
        evidence=await harness.admission.evidence(draft)
        evidence.append(await harness.admission.probes.run(candidate_revision=draft.revision_id,
            profile=draft.values.providers[str(pid)],capability="embedding",allow_billable=not local))
        assert all(e.safe_error_code=="SUCCESS" for e in evidence),evidence
        harness.service.activate(draft.revision_id,expected_revision=None,
            probe_ids=[e.probe_id for e in evidence],process_env={})
    asyncio.run(activate()); calls.clear(); native.clear()
    operations=LearningOperations(sessions=space.subjects.sessions,config=harness.service,
        transport=None if local else httpx.MockTransport(respond))
    space.client.app.state.learning_operations=operations
    sid=materials.subject(space)
    document=materials.upload(space,sid,"addition.md",b"# Addition\n\nAddition combines quantities. 2 + 2 = 4.\n\n## Even\n\n2 and 4 are even.").json()
    entry=MaterialIndexing(space.materials,operations)
    try: yield space,document,entry,operations,calls,native,harness
    finally:
        asyncio.run(operations.aclose()); harness.close()


def prepare(env,key=None):
    space,doc,*_=env
    response=space.client.post(f"/api/learning/materials/{doc['id']}/index-preview",
        json={"idempotency_key":key or uuid4().hex})
    assert response.status_code==200,response.text
    return response.json()


def confirm(env,operation):
    space,doc,*_=env
    response=space.client.post(f"/api/learning/materials/{doc['id']}/index/{operation['id']}/confirm",
        json={"preview_digest":operation["preview_digest"]})
    assert response.status_code==200,response.text
    return response.json()


def execute(env,operation):
    space,doc,*_=env
    return space.client.post(f"/api/learning/materials/{doc['id']}/index/{operation['id']}/execute",json={})


def test_http_confirm_index_publish_and_real_keyword_retrieval(index_env):
    space,doc,_,ops,calls,*_=index_env
    first=prepare(index_env,"same")
    assert prepare(index_env,"same")["id"]==first["id"] and calls==[]
    assert all("embedding_inputs" not in s and "material_input" not in s for s in first["steps"])
    assert execute(index_env,first).status_code==409 and calls==[]
    confirm(index_env,first); confirm(index_env,first)
    result=execute(index_env,first)
    assert result.status_code==200,result.text
    assert result.json()["state"]=="indexed" and result.json()["index_operation_id"]==first["id"]
    assert execute(index_env,first).json()==result.json()
    assert len(calls)==ops.view(UUID(first["id"])).actual_requests==doc["chunk_count"]
    assert ops.snapshots.active_lease_count==0
    with space.subjects.sessions() as session:
        hits=KeywordSearchRetriever().search(session,"Addition",filters=RetrievalFilters(subject_ids=(UUID(doc["subject_id"]),)))
        assert hits and {str(h.document_id) for h in hits}=={doc["id"]}


@pytest.mark.parametrize("change",["chunk","config","new_preview"])
def test_stale_confirmation_or_inputs_send_zero(index_env,change):
    space,doc,_,ops,calls,_,harness=index_env
    old=prepare(index_env);confirm(index_env,old)
    if change=="chunk":
        with space.subjects.sessions.begin() as session:
            chunk=session.scalar(select(DocumentChunk).where(DocumentChunk.document_id==UUID(doc["id"])))
            chunk.content+=" changed"
    elif change=="config":
        active=harness.service.active_revision()
        asyncio.run(harness.activate_model("new-model",expected=active.revision_id))
    else: prepare(index_env)
    result=execute(index_env,old)
    assert result.status_code==409 and result.json()["code"]=="PREVIEW_CHANGED"
    assert calls==[] and ops.view(UUID(old["id"])).actual_requests==0


def test_unknown_and_reopened_operation_never_resends(index_env):
    space,_doc,_,ops,calls,_,harness=index_env
    async def timeout(request):
        calls.append(json.loads(request.content));raise httpx.ReadTimeout("synthetic",request=request)
    ops.transport=httpx.MockTransport(timeout)
    operation=prepare(index_env);confirm(index_env,operation)
    first=execute(index_env,operation)
    assert first.status_code==200 and first.json()["state"]=="outcome_unknown"
    reopened=LearningOperations(sessions=space.subjects.sessions,config=harness.service,transport=ops.transport)
    space.client.app.state.learning_operations=reopened
    try:
        assert execute(index_env,operation).json()["state"]=="outcome_unknown"
        assert prepare(index_env)["id"]==operation["id"]
        assert len(calls)==reopened.view(UUID(operation["id"])).actual_requests==1
    finally: asyncio.run(reopened.aclose())


def test_paid_result_publication_failure_reenters_locally(index_env,monkeypatch):
    space,doc,entry,ops,calls,_,harness=index_env
    operation=prepare(index_env);confirm(index_env,operation)
    original=space.materials.index_material
    monkeypatch.setattr(space.materials,"index_material",lambda *a,**kw:(_ for _ in ()).throw(MaterialError("SYNTHETIC_PUBLISH_FAILURE",409)))
    with pytest.raises(MaterialError,match="SYNTHETIC_PUBLISH_FAILURE"):
        asyncio.run(entry.execute(UUID(doc["id"]),UUID(operation["id"])))
    assert ops.view(UUID(operation["id"])).state=="succeeded"
    sent=len(calls)
    monkeypatch.setattr(space.materials,"index_material",original)
    reopened=LearningOperations(sessions=space.subjects.sessions,config=harness.service,transport=ops.transport)
    try:
        result=asyncio.run(MaterialIndexing(space.materials,reopened).execute(UUID(doc["id"]),UUID(operation["id"])))
        assert result.state=="indexed" and len(calls)==sent
        assert reopened.snapshots.active_lease_count==0
    finally: asyncio.run(reopened.aclose())


@pytest.mark.parametrize("index_env",["local"],indirect=True)
def test_local_embedding_uses_zero_http_budget_and_real_lease(index_env):
    _,doc,_,ops,calls,native,_=index_env
    operation=prepare(index_env)
    assert operation["request_budget"]==0 and all(s["local_only"] for s in operation["steps"])
    assert execute(index_env,operation).status_code==409 and native==[]
    confirm(index_env,operation)
    result=execute(index_env,operation)
    assert result.status_code==200 and result.json()["state"]=="indexed",result.text
    assert doc["chunk_count"] >= 2
    assert len(native)==doc["chunk_count"] and calls==[]
    assert ops.view(UUID(operation["id"])).actual_requests==0 and ops.snapshots.active_lease_count==0


def test_remote_profile_cannot_claim_free_local_execution(index_env):
    _,_,_,ops,calls,*_=index_env
    operation=ops.prepare(purpose="synthetic-local-spoof",idempotency_key=uuid4().hex,
        steps=[StepPlan(step_id="free",task_kind="embedding",messages=[],embedding_inputs=["data"],
            request_budget=0,local_only=True)])
    ops.confirm(operation.id,preview_digest=operation.preview_digest)
    with pytest.raises(OperationError,match="UNPLANNED_REMOTE_REQUEST"):
        asyncio.run(ops.execute(operation.id,invoke=lambda *_:None))
    assert calls==[] and ops.view(operation.id).actual_requests==0


def test_budget_exhaustion_blocks_before_http(index_env):
    space,_,_,ops,calls,*_=index_env
    operation=prepare(index_env);confirm(index_env,operation)
    with space.subjects.sessions.begin() as session:
        session.get(OperationRun,UUID(operation["id"])).request_budget=0
    result=execute(index_env,operation)
    assert result.json()["state"]=="failed" and result.json()["steps"][0]["error_code"]=="REQUEST_BUDGET_EXCEEDED"
    assert calls==[] and ops.view(UUID(operation["id"])).actual_requests==0


def test_concurrent_execute_claims_once(index_env):
    _,doc,entry,ops,calls,*_=index_env
    operation=prepare(index_env);confirm(index_env,operation)
    async def both():
        return await asyncio.gather(*(entry.execute(UUID(doc["id"]),UUID(operation["id"])) for _ in range(2)))
    asyncio.run(both())
    assert len(calls)==ops.view(UUID(operation["id"])).actual_requests==doc["chunk_count"]
    assert ops.snapshots.active_lease_count==0


@pytest.mark.parametrize("incomplete", [{"requires_ocr":True},{"unprocessed_pages":[2]},{"issues":["UNSUPPORTED_CONTENT"]}])
def test_wrong_material_operation_and_unfinished_parse_are_rejected(index_env,incomplete):
    space,doc,_,_,calls,*_=index_env
    operation=prepare(index_env)
    other=materials.upload(space,materials.subject(space,"other"),"other.md",b"Other legitimate text").json()
    response=space.client.post(f"/api/learning/materials/{other['id']}/index/{operation['id']}/confirm",
        json={"preview_digest":operation["preview_digest"]})
    assert response.status_code==409 and calls==[]
    from backend.app.models.document import Document
    with space.subjects.sessions.begin() as session:
        row=session.get(Document,UUID(doc["id"]));row.parse_facts={**row.parse_facts,**incomplete}
    response=space.client.post(f"/api/learning/materials/{doc['id']}/index-preview",json={"idempotency_key":uuid4().hex})
    assert response.status_code==409 and response.json()["code"]=="MATERIAL_PARSE_INCOMPLETE" and calls==[]


@pytest.mark.parametrize("index_env",["local"],indirect=True)
@pytest.mark.parametrize("native_failure",[False,True])
def test_repeated_cancel_keeps_native_lease_and_slot_until_real_completion(index_env,monkeypatch,native_failure):
    import threading
    _,doc,entry,ops,calls,*_=index_env
    started=threading.Event();release=threading.Event();finished=threading.Event()
    def blocking(texts):
        started.set()
        try:
            assert release.wait(5)
            if native_failure: raise ValueError("synthetic native failure")
            return [[1.0]+[0.0]*1023 for _ in texts]
        finally: finished.set()
    async def encode(self,model,texts):
        return await asyncio.to_thread(blocking,texts)
    monkeypatch.setattr("backend.app.ai.embedding.providers.local.LocalEmbeddingProvider._encode",encode)
    operation=prepare(index_env);confirm(index_env,operation)
    async def run():
        task=asyncio.create_task(entry.execute(UUID(doc["id"]),UUID(operation["id"])))
        try:
            assert await asyncio.to_thread(started.wait,3)
            task.cancel();await asyncio.sleep(.01)
            task.cancel();await asyncio.sleep(.01)
            assert not task.done() and not finished.is_set()
            assert ops.snapshots.active_lease_count==1
            with ops.sessions() as session:
                row=session.get(OperationRun,UUID(operation["id"]))
                assert row.active_slot=="model" and row.state=="running" and row.actual_requests==0
        finally: release.set()
        result=await task
        assert result.state==("failed" if native_failure else "outcome_unknown")
        assert result.steps[0]["error_code"]==("PROVIDER_FAILED" if native_failure else "OUTCOME_UNKNOWN")
    asyncio.run(run())
    assert finished.is_set() and calls==[] and ops.snapshots.active_lease_count==0
    with ops.sessions() as session:
        assert session.get(OperationRun,UUID(operation["id"])).active_slot is None


def test_identity_conflict_is_rejected_before_paid_request(index_env):
    space,doc,_,_,calls,*_=index_env
    from backend.app.models.document import Document
    from backend.app.models.knowledge_base import KnowledgeBase
    with space.subjects.sessions.begin() as session:
        row=session.get(Document,UUID(doc["id"]))
        session.get(KnowledgeBase,row.knowledge_base_id).embedding_identity={
            "provider":"openai_compatible","adapter":"openai_compatible","model_id":"other-model",
            "endpoint_identity":"http://127.0.0.1:9/v1","model_version":None,
            "dimension":1024,"semantic_parameters":{},"status":"native","identity_revision":1}
    result=space.client.post(f"/api/learning/materials/{doc['id']}/index-preview",json={"idempotency_key":uuid4().hex})
    assert result.status_code==409 and result.json()["code"]=="EMBEDDING_INCOMPATIBLE" and calls==[]


@pytest.mark.parametrize("index_env",["local"],indirect=True)
def test_zero_budget_history_survives_refused_downgrade(index_env):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text
    space,_,_,ops,*_=index_env
    operation=prepare(index_env)
    cfg=Config(str(Path(__file__).resolve().parents[2]/"alembic.ini"))
    with pytest.raises(RuntimeError,match="Local operation history prevents downgrade"), space.engine.begin() as connection:
        original_head=connection.scalar(text("SELECT version_num FROM alembic_version"))
        cfg.attributes["connection"]=connection
        command.downgrade(cfg,"0033_personal_cutover")
    assert ops.view(UUID(operation["id"])).request_budget==0
    with space.engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version"))==original_head


def test_explicit_retry_only_sends_remaining_chunks(index_env):
    _,doc,_,ops,calls,*_=index_env
    original=ops.transport
    async def fail_second(request):
        if len(calls)==1:
            calls.append(json.loads(request.content))
            return httpx.Response(503,json={"error":"synthetic failure"})
        return await original.handle_async_request(request)
    ops.transport=httpx.MockTransport(fail_second)
    first=prepare(index_env);confirm(index_env,first)
    failed=execute(index_env,first).json()
    assert failed["state"]=="failed" and len(calls)==2
    assert ops.view(UUID(first["id"])).steps[0]["state"]=="succeeded"
    assert execute(index_env,first).json()["state"]=="failed" and len(calls)==2
    ops.transport=original
    retry=prepare(index_env)
    assert retry["id"]!=first["id"] and retry["retry_of_id"]==first["id"]
    assert retry["request_budget"]==doc["chunk_count"]-1 and len(calls)==2
    confirm(index_env,retry)
    result=execute(index_env,retry)
    assert result.status_code==200 and result.json()["state"]=="indexed",result.text
    assert len(calls)==doc["chunk_count"]+1
    assert calls[0]["input"] not in [c["input"] for c in calls[2:]]
    assert ops.snapshots.active_lease_count==0
