"""Real Test PG + Fake HTTP; no claim about real semantic model accuracy."""
import asyncio
import json
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text

from backend.app.ai.paper_extraction.schemas import PersonalPaperConfirm
from backend.app.schemas.personal_learning import PersonalAnswerRequest
from backend.app.services.answer_completion import AnswerCompletion
from backend.app.services.learning_operations import LearningOperations, OperationError
from backend.app.services.question_correction_service import PersonalQuestionCorrection
from tests.integration import test_learning_material_versions as material_tests
from tests.integration.test_personal_duplicate_identity import payload, service, source
from tests.support.runtime_settings_fakes import settings_harness

space = material_tests.space


def formal_question(space):
    sid, doc = source(space)
    entry = service(space)
    paper = entry.create(UUID(sid), UUID(doc["id"]))
    entry.store_candidates(paper.id, [payload(doc)])
    draft = entry.get(paper.id).questions[0]
    result = PersonalQuestionCorrection(space.subjects).confirm(paper.id, PersonalPaperConfirm(
        question_ids=[draft.id], expected_revisions={draft.id: 1},
    ))
    return UUID(sid), UUID(str(result.questions[0].question_id))


def answer_result(*, answer="A", verdict="pass", warning=False):
    return {
        "reference_answer": answer, "scoring_rubric": None,
        "explanation_blocks": [{"type":"text","text":"Synthetic reasoning for the test only"}],
        "quality": {"answer_basis":"The given conditions establish the first option.",
            "assessment": {"checks": [
                {"kind":k, "verdict":verdict if k=="condition_sufficiency" else "pass",
                 "reason":"Synthetic explicit basis"}
                for k in ("answer_correctness","condition_sufficiency","option_ambiguity","rubric_clarity")
            ], "issues": [{"code":"UNCERTAIN","severity":"warning","message":"Unproved condition"}] if warning else []}},
    }


@pytest.mark.parametrize("answer,verdict,warning,expected", [
    ("A","pass",False,"eligible"),
    ("Z","pass",False,"needs_correction"),
    ("A","insufficient_evidence",False,"needs_correction"),
    ("A","pass",True,"needs_correction"),
])
def test_explicit_missing_answer_completion_is_one_confirmed_request(
    space, tmp_path, answer, verdict, warning, expected,
):
    _, identity = formal_question(space)
    entry = AnswerCompletion(space.subjects)
    calls = []
    harness = settings_harness(tmp_path / "answer-runtime")
    asyncio.run(harness.activate_model("test-model-a", with_credential=True))
    async def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id":"fake-answer", "object":"chat.completion", "created":0, "model":"test-model-a",
            "choices":[{"index":0,"message":{"role":"assistant","content":json.dumps(
                answer_result(answer=answer,verdict=verdict,warning=warning))},"finish_reason":"stop"}],
            "usage":{"prompt_tokens":10,"completion_tokens":10,"total_tokens":20},
        })
    operations = LearningOperations(
        sessions=space.subjects.sessions, config=harness.service, transport=httpx.MockTransport(handler),
    )
    try:
        operation = entry.prepare(identity, PersonalAnswerRequest(
            expected_revision=1, mode="complete", idempotency_key=uuid4().hex,
        ), operations)
        assert entry.get(identity).reference_answer is None
        with pytest.raises(OperationError, match="CONFIRMATION_REQUIRED"):
            asyncio.run(entry.execute(identity, operation.id, operations))
        assert calls == []
        operations.confirm(operation.id, preview_digest=operation.preview_digest)
        result = asyncio.run(entry.execute(identity, operation.id, operations))
        repeated = asyncio.run(entry.execute(identity, operation.id, operations))
        assert result.id == repeated.id == identity
        assert result.revision_id == repeated.revision_id
        assert result.revision == 2 and result.answer_origin == "ai_completion"
        assert result.source_kind == "uploaded" and result.admission_state == expected
        assert len(calls) == 1 and operations.view(operation.id).actual_requests == 1
        assert operations.snapshots.active_lease_count == 0
        with space.engine.connect() as connection:
            assert connection.execute(text(
                "SELECT reference_answer,admission_state FROM question_revisions "
                "WHERE question_id=:id AND revision=1"
            ), {"id": identity}).one() == (None, "incomplete")
    finally:
        asyncio.run(operations.aclose())
        harness.close()


def test_bootstrap_personal_operations_use_verified_space_without_legacy_settings(space,tmp_path):
    from types import SimpleNamespace

    sid,doc=source(space)
    paper=service(space).create(UUID(sid),UUID(doc["id"]))
    harness=settings_harness(tmp_path/"bootstrap-runtime",environment_id=space.profile.environment_id)
    asyncio.run(harness.activate_model("test-model-a"))
    app=space.client.app
    app.state.subject_service=space.subjects
    app.state.settings_runtime=SimpleNamespace(config=harness.service,credential_store=harness.store)
    try:
        response=space.client.post(f"/api/learning/imports/{paper.id}/extraction-preview",
                                  json={"idempotency_key":uuid4().hex})
        assert response.status_code==200,response.text
        operation=response.json()
        assert operation["actual_requests"]==0 and operation["steps"][0]["model"]=="test-model-a"
        viewed=space.client.get("/api/learning/operations/"+operation["id"])
        assert viewed.status_code==200 and viewed.json()["id"]==operation["id"]
        confirmed=space.client.post("/api/learning/operations/"+operation["id"]+"/confirm",
                                   json={"preview_digest":operation["preview_digest"]})
        assert confirmed.status_code==200 and confirmed.json()["actual_requests"]==0
    finally:
        harness.close()


@pytest.mark.parametrize("unknown",[False,True])
def test_user_retry_requires_a_new_confirmed_preview_and_unknown_ack(space,tmp_path,unknown):
    from backend.app.services.learning_material_service import MaterialError

    sid,doc=source(space)
    entry=service(space)
    paper=entry.create(UUID(sid),UUID(doc["id"]))
    calls=[]
    harness=settings_harness(tmp_path/"retry-runtime")
    asyncio.run(harness.activate_model("test-model-a",with_credential=True))
    async def handler(request):
        calls.append(request)
        if len(calls)==1:
            if unknown: raise httpx.ReadTimeout("synthetic unknown",request=request)
            return httpx.Response(503,json={"error":"synthetic failure"})
        return httpx.Response(200,json={
            "id":"fake-retry","object":"chat.completion","created":0,"model":"test-model-a",
            "choices":[{"index":0,"message":{"role":"assistant","content":json.dumps({"questions":[payload(doc)]})},
                        "finish_reason":"stop"}],
        })
    operations=LearningOperations(sessions=space.subjects.sessions,config=harness.service,
                                 transport=httpx.MockTransport(handler))
    try:
        old=entry.prepare_extraction(paper.id,operations,uuid4().hex)
        operations.confirm(old.id,preview_digest=old.preview_digest)
        failed=asyncio.run(entry.execute_extraction(paper.id,operations,old.id))
        assert failed.state==("outcome_unknown" if unknown else "failed") and len(calls)==1
        assert entry.prepare_extraction(paper.id,operations,uuid4().hex).id==old.id
        if unknown:
            with pytest.raises(OperationError,match="UNKNOWN_OUTCOME_ACK_REQUIRED"):
                entry.prepare_extraction(paper.id,operations,uuid4().hex,retry_of_id=old.id)
        retry=entry.prepare_extraction(paper.id,operations,uuid4().hex,retry_of_id=old.id,
                                      acknowledge_unknown=unknown)
        assert retry.id!=old.id and retry.state=="prepared" and retry.retry_of_id==old.id
        with pytest.raises(OperationError,match="CONFIRMATION_REQUIRED"):
            asyncio.run(entry.execute_extraction(paper.id,operations,retry.id))
        assert len(calls)==1
        with pytest.raises(MaterialError,match="PAPER_OPERATION_CHANGED"):
            asyncio.run(entry.execute_extraction(paper.id,operations,old.id))
        operations.confirm(retry.id,preview_digest=retry.preview_digest)
        result=asyncio.run(entry.execute_extraction(paper.id,operations,retry.id))
        assert len(result.questions)==1 and len(calls)==2
        assert operations.snapshots.active_lease_count==0
    finally:
        asyncio.run(operations.aclose());harness.close()


@pytest.mark.parametrize("kind",["txt","md","pdf","scanned_pdf","docx","png","jpeg"])
@pytest.mark.parametrize("stress",[False,True],ids=["clear","stress"])
def test_seven_formats_use_real_sources_and_local_ocr_before_fake_extraction(space,tmp_path,kind,stress):
    import os
    from pathlib import Path

    from backend.app.services.settings.runtime_snapshot import RuntimeSnapshotService
    from tests.integration.test_learning_material_versions import subject, upload
    from tests.support.personal_paper_runtime import configured_paper_runtime
    from tests.support.personal_paper_samples import personal_sample

    model_dir=os.environ.get("EDUAGENT_TEST_OCR_MODEL_DIR")
    assert model_dir,"T041 native acceptance requires an explicit verified Test OCR model directory"
    sid=subject(space)
    filename,data=personal_sample(kind,stress=stress)
    uploaded=upload(space,sid,filename,data,purpose="exam_paper")
    assert uploaded.status_code==201,uploaded.text
    doc=uploaded.json()
    entry=service(space);paper=entry.create(UUID(sid),UUID(doc["id"]))
    harness,operations,calls=asyncio.run(configured_paper_runtime(
        tmp_path/"format-runtime",space.profile.environment_id,Path(model_dir),space.subjects.sessions,
    ))
    try:
        initial=space.materials.get(UUID(doc["id"]))
        needs_ocr=initial.requires_ocr
        if needs_ocr:
            assert initial.parse_state=="partial"
            asyncio.run(entry.process_local_ocr(
                paper.id,RuntimeSnapshotService(harness.service),limits=operations.limits,
            ))
            after=space.materials.get(UUID(doc["id"]))
            assert after.requires_ocr is False
            assert any(b.text and b.text.strip() for b in after.blocks)
            assert "OCR_REQUIRES_REVIEW" in after.issues
        operation=entry.prepare_extraction(paper.id,operations,uuid4().hex)
        assert calls==[]
        operations.confirm(operation.id,preview_digest=operation.preview_digest)
        result=asyncio.run(entry.execute_extraction(paper.id,operations,operation.id))
        assert len(result.questions)==3
        assert {q.question_type for q in result.questions}=={"single_choice","multiple_choice","short_answer"}
        assert len(calls)==1 and operations.view(operation.id).actual_requests==1
        assert all(loc.document_id==UUID(doc["id"]) for q in result.questions for loc in q.source_locations)
        if kind=="docx":
            assert len(result.assets)==(8 if stress else 2)
            assert all(loc.page is None for q in result.questions for loc in q.source_locations)
            assert len(result.questions[0].asset_ids())==(8 if stress else 2)
            assert any(b.type=="formula" for q in result.questions for b in q.stem_blocks)
            assert any(b.type=="table" for q in result.questions for b in q.stem_blocks)
        if kind=="pdf" and stress:
            assert {loc.page for q in result.questions for loc in q.source_locations}=={1,2}
        assert result.questions[1].reference_answer is None and result.questions[2].reference_answer is None
        if needs_ocr:
            assert all("OCR_REQUIRES_REVIEW" in q.uncertainties for q in result.questions)
        confirmed=PersonalQuestionCorrection(space.subjects).confirm(paper.id,PersonalPaperConfirm(
            question_ids=[q.id for q in result.questions],expected_revisions={q.id:q.revision for q in result.questions},
        ))
        assert all(q.question_id is not None for q in confirmed.questions)
        assert operations.snapshots.active_lease_count==0
        print({"format":kind,"stress":stress,"source_bytes":len(data),"real_local_ocr":needs_ocr,
               "fake_model_calls":len(calls),"question_count":len(result.questions)})
    finally:
        asyncio.run(operations.aclose());harness.close()
