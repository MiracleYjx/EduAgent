"""Actual Test PG and guarded Fake Provider: selection, generation, freezing and visibility."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select, text

from backend.app.domain.enums import QuestionSourceType
from backend.app.models.practice import (
    GradeAttempt,
    PracticeAnswer,
    PracticeItem,
    PracticeSession,
)
from backend.app.models.question import Question
from backend.app.models.question_origin import QuestionOrigin
from backend.app.models.question_revision import (
    QuestionRevision,
    question_revision_knowledge_points,
)
from backend.app.models.subject import KnowledgePoint
from backend.app.schemas.personal_learning import PersonalQuestionEdit
from backend.app.schemas.personal_practice import (
    GenerationRequest,
    PracticeConditions,
    PracticeStartRequest,
)
from backend.app.services.learning_material_service import MaterialError
from backend.app.services.learning_operations import LearningOperations, OperationError
from backend.app.services.personal_question_generation import PersonalQuestionGeneration
from backend.app.services.personal_question_service import PersonalQuestionService
from backend.app.services.practice_service import PracticeService
from tests.integration import test_learning_material_versions as material_tests
from tests.integration.test_personal_paper_flow import answer_result
from tests.support.runtime_settings_fakes import settings_harness

space=material_tests.space


def seed_bank(space, *, name="algebra", history=True, count=8):
    sid=UUID(material_tests.subject(space,name))
    document=material_tests.upload(space,str(sid),"synthetic-paper.txt",
                                  b"Synthetic original conditions: 2 + 2 = 4. A:4 B:5 C:6",purpose="exam_paper").json()
    location={"anchor":document["blocks"][0]["anchor"]}
    point=uuid4()
    ids=[]
    with space.subjects.sessions.begin() as session:
        session.add(KnowledgePoint(id=point,subject_id=sid,name="algebra",confirmation_state="confirmed"))
        session.flush()
        for n in range(count):
            question=Question(id=uuid4(),subject_id=sid,source_type=QuestionSourceType.PAPER_IMPORTED,
                              course_id=None,created_by=None)
            session.add(question);session.flush()
            kind="short_answer" if n%3==2 else "multiple_choice" if n%3==1 else "single_choice"
            blocks=[{"type":"text","text":f"Synthetic question {n+1}: What is 2 + 2?"}]
            if n==6:
                blocks += [{"type":"formula","expression":{"kind":"sup","children":[
                    {"kind":"identifier","text":"x"},{"kind":"number","text":"2"}]}}]
            if n==7:
                blocks += [{"type":"table","headers":["x","y"],"rows":[["2","4"]]}]
            options=[] if kind=="short_answer" else [
                {"id":key,"blocks":[{"type":"text","text":value}]} for key,value in [("A","4"),("B","5"),("C","6")]]
            revision=QuestionRevision(id=uuid4(),question_id=question.id,subject_id=sid,revision=1,
                question_type=kind,stem_blocks=blocks,options=options,
                reference_answer="4" if kind=="short_answer" else "A,C" if kind=="multiple_choice" else "A",
                scoring_rubric="Addition explanation for full credit" if kind=="short_answer" else None,
                explanation_blocks=[{"type":"text","text":"PRIVATE explanation must never appear while answering."}],
                grounding="uploaded_material",grounding_refs={"answer_origin":"paper","quality":answer_result()["quality"]},
                admission_state="eligible",difficulty="基础",default_score=8 if n==0 else 10)
            session.add(revision);session.flush()
            question.current_revision_id=revision.id
            session.execute(question_revision_knowledge_points.insert().values(
                revision_id=revision.id,subject_id=sid,knowledge_point_id=point))
            session.add(QuestionOrigin(id=uuid4(),question_id=question.id,subject_id=sid,source_kind="uploaded",
                document_id=UUID(document["id"]),source_location=location,answer_origin="paper"))
            ids.append(question.id)
        session.flush()
    if history:
        for n,identity in enumerate(ids[:6]):
            add_history(space,sid,identity,answered=n<5,score=0)
    return sid,point,ids,UUID(document["id"])


def add_history(space,sid,identity,*,answered=True,score=10):
    with space.subjects.sessions.begin() as session:
        question=session.get(Question,identity)
        revision=session.get(QuestionRevision,question.current_revision_id)
        points=list(session.scalars(select(question_revision_knowledge_points.c.knowledge_point_id).where(
            question_revision_knowledge_points.c.revision_id==revision.id)))
        practice=PracticeSession(id=uuid4(),subject_id=sid,state="preview",conditions={})
        session.add(practice);session.flush()
        item=PracticeItem(id=uuid4(),session_id=practice.id,subject_id=sid,position=1,
            question_id=question.id,question_revision_id=revision.id,fixed_max_score=10,
            scoring_input={"reference_answer":revision.reference_answer},knowledge_point_ids=list(map(str,points)))
        session.add(item);session.flush()
        answer=PracticeAnswer(id=uuid4(),practice_item_id=item.id,answer_state="answered" if answered else "unanswered",
            submission_state="submitted",choice_ids=["A"] if answered and revision.question_type!="short_answer" else None,
            text_answer="4" if answered and revision.question_type=="short_answer" else None,
            submitted_at=datetime.now(UTC))
        session.add(answer);session.flush()
        practice.state="completed"
        session.add(GradeAttempt(id=uuid4(),answer_id=answer.id,round=1,validation_state="valid",
            execution_source="local_objective" if answered else "local_unanswered",score=score,max_score=10,
            reason="Synthetic stored history",scoring_input=item.scoring_input))
    return answer.id


def assert_no_answers(value):
    if isinstance(value,dict):
        assert not {"reference_answer","scoring_rubric","explanation_blocks","scoring_input","quality"} & value.keys()
        for child in value.values(): assert_no_answers(child)
    if isinstance(value,list):
        for child in value: assert_no_answers(child)


def test_real_http_twenty_eight_three_then_explicit_reduction_and_answer_boundary(space):
    sid,point,ids,_=seed_bank(space)
    response=space.client.post(f"/api/learning/subjects/{sid}/practices/preview",json={})
    assert response.status_code==200,response.text
    preview=response.json()
    assert (preview["eligible_count"],preview["uploaded_count"],preview["unseen_count"],
            preview["practiced_count"],preview["selected_count"])==(8,8,3,5,3)
    assert (preview["candidate_shortage"],preview["unseen_shortage"])==(12,17)
    assert preview["weak_knowledge_point_ids"]==[str(point)] and not preview["can_start"]
    blocked=space.client.post(f"/api/learning/practice-previews/{preview['id']}/start",
                              json={"preview_digest":preview["preview_digest"]})
    assert blocked.status_code==409 and blocked.json()["code"]=="INSUFFICIENT_CANDIDATES"
    reduced=space.client.post(f"/api/learning/subjects/{sid}/practices/preview",json={"count":3}).json()
    start=space.client.post(f"/api/learning/practice-previews/{reduced['id']}/start",
                           json={"preview_digest":reduced["preview_digest"]})
    assert start.status_code==200,start.text
    practice=start.json()
    assert len(practice["items"])==3 and {i["content"]["question_id"] for i in practice["items"]}==set(map(str,ids[5:]))
    assert_no_answers(practice)
    assert {item["content"]["question_type"] for item in practice["items"]}=={"single_choice","multiple_choice","short_answer"}
    assert all(item["answer"]["answer_state"]=="unanswered" for item in practice["items"])
    repeat=space.client.post(f"/api/learning/practice-previews/{reduced['id']}/start",
                            json={"preview_digest":reduced["preview_digest"]})
    assert repeat.json()==practice
    after=space.client.post(f"/api/learning/subjects/{sid}/practices/preview",json={"count":3}).json()
    assert after["unseen_count"]==3  # Started/draft/unanswered do not consume first valid identity.


def test_explicit_repractice_freezes_raw_revision_canonical_knowledge_score_and_private_basis(space):
    sid,point,ids,_=seed_bank(space)
    entry=PracticeService(space.subjects)
    preview=entry.preview(sid,PracticeConditions(count=8,allow_practiced=True))
    result=entry.start(preview.id,PracticeStartRequest(preview_digest=preview.preview_digest,scores={ids[0]:"9.50"}))
    assert len(result.items)==8 and len({i.canonical_id for i in result.items})==8
    item=next(i for i in result.items if i.canonical_id==ids[0])
    assert item.score==9.50 and item.knowledge_point_ids==[point]
    with space.subjects.sessions() as session:
        frozen=session.get(PracticeItem,item.id)
        before=dict(frozen.scoring_input);rid=frozen.question_revision_id
        assert frozen.knowledge_point_ids==[str(point)] and before["reference_answer"]=="A"
    bank=PersonalQuestionService(space.subjects)
    original=bank.get(ids[0])
    body=original.model_dump(include={"question_type","stem_blocks","options","explanation_blocks",
        "reference_answer","scoring_rubric","source_locations"})
    body["stem_blocks"]=[{"type":"text","text":"Updated question for future practice only"}]
    bank.revise(ids[0],PersonalQuestionEdit(**body,expected_revision=1,difficulty="进阶",score=20))
    with space.subjects.sessions() as session:
        saved=session.get(PracticeItem,item.id)
        assert saved.question_revision_id==rid and saved.scoring_input==before and saved.fixed_max_score==9.50
    assert entry.practice(result.id).items[item.position-1].content.stem_blocks[0].text.startswith("Synthetic")


def test_stale_preview_returns_real_candidate_difference_and_requires_new_confirmation(space):
    sid,_,ids,_=seed_bank(space,history=False)
    entry=PracticeService(space.subjects)
    preview=entry.preview(sid,PracticeConditions(count=8))
    bank=PersonalQuestionService(space.subjects);old=bank.get(ids[0])
    data=old.model_dump(include={"question_type","stem_blocks","options","explanation_blocks",
                                "reference_answer","scoring_rubric","source_locations"})
    bank.revise(old.id,PersonalQuestionEdit(**data,expected_revision=1,difficulty="基础"))
    response=space.client.post(f"/api/learning/practice-previews/{preview.id}/start",
                              json={"preview_digest":preview.preview_digest})
    assert response.status_code==409
    diff=response.json()
    assert diff["code"]=="PRACTICE_PREVIEW_CHANGED" and str(ids[0]) in diff["removed_question_ids"]
    assert diff["current_preview"]["eligible_count"]==7 and not diff["current_preview"]["can_start"]
    with space.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM practice_items WHERE session_id=:id"),{"id":preview.id})==0


def test_scope_intersection_illegal_values_and_empty_pool_are_fail_closed(space):
    sid,point,_,doc=seed_bank(space,history=False)
    _other,otherpoint,_,otherdoc=seed_bank(space,name="Other")
    path=f"/api/learning/subjects/{sid}/practices/preview"
    for data,code in [
        ({"knowledge_point_ids":[str(otherpoint)]},404),({"source_document_ids":[str(otherdoc)]},404),
        ({"knowledge_point_ids":False},422),({"difficulty":""},422),({"source":"invalid"},422),
    ]:
        result=space.client.post(path,json=data)
        assert result.status_code==code,result.text
    valid=space.client.post(path,json={"source_document_ids":[str(doc)],"knowledge_point_ids":[str(point)],
                                      "difficulty":"基础","count":8}).json()
    assert valid["eligible_count"]==8
    zero=space.client.post(path,json={"source":"ai","count":1}).json()
    assert zero["eligible_count"]==0 and not zero["can_start"]
    denied=space.client.post(f"/api/learning/practice-previews/{zero['id']}/start",
                             json={"preview_digest":zero["preview_digest"]})
    assert denied.status_code==409


def test_draft_answers_have_no_grades_or_history_and_validate_frozen_type(space):
    sid,_,_,_=seed_bank(space,history=False,count=3)
    entry=PracticeService(space.subjects);preview=entry.preview(sid,PracticeConditions(count=3))
    practice=entry.start(preview.id,PracticeStartRequest(preview_digest=preview.preview_digest))
    for item in practice.items:
        path=f"/api/learning/practices/{practice.id}/items/{item.id}/answer"
        good={"text_answer":"0"} if item.content.question_type=="short_answer" else {"choice_ids":["A"]}
        saved=space.client.put(path,json=good)
        assert saved.status_code==200 and saved.json()["answer_state"]=="answered"
        cleared=space.client.put(path,json={})
        assert cleared.status_code==200 and cleared.json()["answer_state"]=="unanswered"
        assert space.client.put(path,json={"choice_ids":["NONEXISTENT"]}).status_code==422
        assert space.client.put(path,json={"reference_answer":"A"}).status_code==422
    assert entry.preview(sid,PracticeConditions(count=3)).unseen_count==3
    with space.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM grade_attempts"))==0


def test_concurrent_start_is_idempotent_and_merge_keeps_first_valid_history(space):
    sid,_,ids,_=seed_bank(space)
    # An explicitly confirmed alias retains the answered family's identity.
    with space.subjects.sessions.begin() as session:
        q=session.get(Question,ids[6]);q.canonical_id=ids[0]
    entry=PracticeService(space.subjects)
    preview=entry.preview(sid,PracticeConditions(count=7,allow_practiced=True))
    body=PracticeStartRequest(preview_digest=preview.preview_digest)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:entry.start(preview.id,body),range(2)))
    assert [i.id for i in results[0].items]==[i.id for i in results[1].items]
    assert len(results[0].items)==7
    current=entry.preview(sid,PracticeConditions(count=20))
    assert current.unseen_count==2 and current.practiced_count==5


def fake_generation(payload,*,answer="A",unknown_evidence=False,chart=False):
    quality=answer_result()["quality"]
    result=[]
    for n in range(payload["count"]):
        result.append({
            "question_type":"single_choice","stem_blocks":[{"type":"text","text":f"AI synthetic {n+1}: 2 + 2?"}],
            "options":[{"id":k,"blocks":[{"type":"text","text":v}]} for k,v in [("A","4"),("B","5")]],
            "reference_answer":answer,"quality":quality,
            "knowledge_point_ids":[p["id"] for p in payload["knowledge_points"]],
            "difficulty":payload["conditions"]["difficulty"],
            "evidence_chunk_ids":[str(uuid4())] if unknown_evidence else
                [e["chunk_id"] for e in payload["evidence"][:1]],
        })
    return {"questions":result}


def runtime(space,tmp_path,*,answer="A",unknown_evidence=False):
    harness=settings_harness(tmp_path/"practice-runtime",environment_id=space.profile.environment_id)
    asyncio.run(harness.activate_model("test-model-a",with_credential=True))
    calls=[]
    async def handler(request):
        body=json.loads(request.content);calls.append(body)
        payload=json.loads(body["messages"][-1]["content"])
        return httpx.Response(200,json={
            "id":"fake-generation","object":"chat.completion","created":0,"model":"test-model-a",
            "choices":[{"index":0,"message":{"role":"assistant","content":json.dumps(
                fake_generation(payload,answer=answer,unknown_evidence=unknown_evidence))},"finish_reason":"stop"}],
            "usage":{"prompt_tokens":10,"completion_tokens":10,"total_tokens":20}})
    operations=LearningOperations(sessions=space.subjects.sessions,config=harness.service,transport=httpx.MockTransport(handler))
    return harness,operations,calls


@pytest.mark.parametrize("answer,unknown,state",[("A",False,"eligible"),("Z",False,"needs_correction"),("A",True,"needs_correction")])
def test_general_generation_is_explicit_one_snapshot_one_send_and_no_teaching_dependencies(
    space,tmp_path,answer,unknown,state,
):
    sid=UUID(material_tests.subject(space,"No material or embedding index"))
    entry=PersonalQuestionGeneration(space.subjects)
    preview=entry.preview(sid,PracticeConditions(source="ai",count=1))
    harness,operations,calls=runtime(space,tmp_path,answer=answer,unknown_evidence=unknown)
    try:
        operation=entry.prepare(preview.id,GenerationRequest(
            grounding="general_knowledge",idempotency_key=uuid4().hex),operations)
        assert calls==[] and operation.request_budget==1 and operation.actual_requests==0
        assert "通用知识" in operation.steps[0]["scope_summary"]
        assert entry.preview_view(preview.id).generation_grounding=="general_knowledge"
        with pytest.raises(OperationError,match="CONFIRMATION_REQUIRED"):
            asyncio.run(entry.execute(preview.id,operation.id,operations))
        assert calls==[]
        operations.confirm(operation.id,preview_digest=operation.preview_digest)
        result=asyncio.run(entry.execute(preview.id,operation.id,operations))
        repeated=asyncio.run(entry.execute(preview.id,operation.id,operations))
        assert result.preview_digest==repeated.preview_digest and len(calls)==1
        payload=json.loads(calls[0]["messages"][-1]["content"])
        assert payload["grounding"]=="general_knowledge" and payload["evidence"]==[] and payload["retrieval_mode"]=="none"
        assert operations.view(operation.id).actual_requests==1 and operations.snapshots.active_lease_count==0
        bank=PersonalQuestionService(space.subjects).list(sid)
        assert len(bank)==1 and bank[0].source_kind=="ai" and bank[0].answer_origin=="ai_completion"
        assert bank[0].source_locations==[] and bank[0].admission_state==state
        with space.subjects.sessions() as session:
            revision=session.get(QuestionRevision,bank[0].revision_id)
            assert revision.grounding=="general_knowledge"
            assert revision.grounding_refs["runtime_snapshot_id"]==str(operation.runtime_snapshot_id)
            assert revision.default_score==10
        assert result.can_start==(state=="eligible")
    finally:
        asyncio.run(operations.aclose());harness.close()


def test_uploaded_generation_never_falls_back_or_widens_selected_version(space,tmp_path):
    sid=UUID(material_tests.subject(space,"algebra"))
    doc=material_tests.upload(space,str(sid),"first.txt",b"algebra first edition ONLY").json()
    other=material_tests.upload(space,str(sid),"second.txt",b"algebra second edition EXCLUDED").json()
    series=space.client.post(f"/api/learning/subjects/{sid}/material-series",
        json={"document_id":doc["id"],"series_name":"Algebra","label":"first"}).json()
    version=UUID(series["default_version_id"])
    entry=PersonalQuestionGeneration(space.subjects)
    preview=entry.preview(sid,PracticeConditions(source="ai",count=1))
    harness,operations,calls=runtime(space,tmp_path)
    try:
        with pytest.raises(MaterialError,match="GENERATION_MATERIAL_INSUFFICIENT"):
            entry.prepare(preview.id,GenerationRequest(grounding="uploaded_material",
                material_version_ids=[version],idempotency_key=uuid4().hex),operations)
        assert calls==[]
        material_tests.index(space,doc["id"]);material_tests.index(space,other["id"])
        operation=entry.prepare(preview.id,GenerationRequest(grounding="uploaded_material",
            material_version_ids=[version],idempotency_key=uuid4().hex),operations)
        operations.confirm(operation.id,preview_digest=operation.preview_digest)
        result=asyncio.run(entry.execute(preview.id,operation.id,operations))
        assert result.can_start and len(calls)==1
        payload=json.loads(calls[0]["messages"][-1]["content"])
        assert {e["document_id"] for e in payload["evidence"]}=={doc["id"]}
        assert {e["material_version_id"] for e in payload["evidence"]}=={str(version)}
        assert "EXCLUDED" not in calls[0]["messages"][-1]["content"]
        assert [step["task_kind"] for step in operations.view(operation.id).steps]==["question_generation"]
        bank=PersonalQuestionService(space.subjects).list(sid)
        assert bank[0].source_locations[0].document_id==UUID(doc["id"])
    finally:
        asyncio.run(operations.aclose());harness.close()


def test_concurrent_question_correction_refreshes_revision_after_waiting_for_subject_lock(space,monkeypatch):
    from threading import Barrier, local
    _sid,_,ids,_=seed_bank(space,history=False,count=1)
    bank=PersonalQuestionService(space.subjects);original=bank.get(ids[0])
    body=PersonalQuestionEdit(**original.model_dump(include={
        "question_type","stem_blocks","options","explanation_blocks","reference_answer","scoring_rubric","source_locations"}),
        expected_revision=1,difficulty="基础")
    gate=Barrier(2);state=local();real=PersonalQuestionService.current
    def current(self,session,identity,*,lock=False):
        result=real(self,session,identity,lock=lock)
        if not lock and not getattr(state,"read",False):
            state.read=True;gate.wait(timeout=10)
        return result
    monkeypatch.setattr(PersonalQuestionService,"current",current)
    def revise():
        try:
            return bank.revise(ids[0],body).revision
        except MaterialError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes=list(pool.map(lambda _:revise(),range(2)))
    assert sorted(map(str,outcomes))==["2","REVISION_CHANGED"]


@pytest.mark.parametrize("consistent",[False,True])
def test_generated_chart_requires_real_answer_data_and_renders_only_after_admission(space,tmp_path,consistent):
    sid=UUID(material_tests.subject(space,"Data charts"))
    harness,operations,calls=runtime(space,tmp_path)
    async def chart_handler(request):
        body=json.loads(request.content);calls.append(body)
        result=fake_generation(json.loads(body["messages"][-1]["content"]))
        chart={"kind":"line","title":"Given measurements","x_axis":{"label":"time","unit":"s"},
               "y_axis":{"label":"distance","unit":"m"},"points":[{"x":1.0,"y":4.0}],"basis":"Given data"}
        result["questions"][0]["stem_blocks"].append({"type":"chart","chart":chart})
        result["questions"][0]["quality"]["chart_answer_points"]={"1":[{"x":1.0,"y":4.0 if consistent else 99.0}]}
        return httpx.Response(200,json={
            "id":"fake-chart","object":"chat.completion","created":0,"model":"test-model-a",
            "choices":[{"index":0,"message":{"role":"assistant","content":json.dumps(result)},"finish_reason":"stop"}]})
    operations.transport=httpx.MockTransport(chart_handler)
    try:
        entry=PersonalQuestionGeneration(space.subjects)
        preview=entry.preview(sid,PracticeConditions(source="ai",count=1))
        op=entry.prepare(preview.id,GenerationRequest(grounding="general_knowledge",idempotency_key=uuid4().hex),operations)
        operations.confirm(op.id,preview_digest=op.preview_digest)
        result=asyncio.run(entry.execute(preview.id,op.id,operations))
        assert len(calls)==1 and result.can_start==consistent
        if consistent:
            practice=entry.start(result.id,PracticeStartRequest(preview_digest=result.preview_digest))
            assert practice.items[0].content.automatic_grading_ready is True
            assert practice.items[0].content.stem_blocks[1].chart.points[0].y==4.0
        else:
            bank=entry.list(sid)
            assert "CHART_BASIS_INVALID" in bank[0].issues
    finally:
        asyncio.run(operations.aclose());harness.close()


def test_changed_generation_grounding_requires_a_new_operation_and_rejects_old_binding(space,tmp_path):
    sid=UUID(material_tests.subject(space,"algebra"))
    doc=material_tests.upload(space,str(sid),"basis.txt",b"algebra selected source").json()
    material_tests.index(space,doc["id"])
    version=space.client.post(f"/api/learning/subjects/{sid}/material-series",
        json={"document_id":doc["id"],"series_name":"Basis","label":"v1"}).json()["default_version_id"]
    harness,operations,calls=runtime(space,tmp_path)
    try:
        entry=PersonalQuestionGeneration(space.subjects)
        preview=entry.preview(sid,PracticeConditions(source="ai",count=1))
        first=entry.prepare(preview.id,GenerationRequest(grounding="general_knowledge",idempotency_key=uuid4().hex),operations)
        second=entry.prepare(preview.id,GenerationRequest(grounding="uploaded_material",
            material_version_ids=[UUID(version)],idempotency_key=uuid4().hex),operations)
        assert first.id!=second.id and calls==[]
        assert entry.preview_view(preview.id).generation_operation_id==second.id
        operations.confirm(first.id,preview_digest=first.preview_digest)
        with pytest.raises(MaterialError,match="GENERATION_OPERATION_MISMATCH"):
            asyncio.run(entry.execute(preview.id,first.id,operations))
        assert calls==[]
    finally:
        asyncio.run(operations.aclose());harness.close()


@pytest.mark.parametrize("unknown",[False,True])
def test_generation_failure_has_one_attempt_no_auto_fallback_or_fake_bank_rows(space,tmp_path,unknown):
    sid=UUID(material_tests.subject(space,"Isolated failure"))
    harness,operations,calls=runtime(space,tmp_path)
    async def failing(request):
        calls.append(request)
        if unknown:raise httpx.ReadTimeout("Synthetic outcome unknown",request=request)
        return httpx.Response(503,json={"error":{"message":"Synthetic unavailable"}})
    operations.transport=httpx.MockTransport(failing)
    try:
        entry=PersonalQuestionGeneration(space.subjects)
        preview=entry.preview(sid,PracticeConditions(source="ai",count=1))
        op=entry.prepare(preview.id,GenerationRequest(grounding="general_knowledge",idempotency_key=uuid4().hex),operations)
        operations.confirm(op.id,preview_digest=op.preview_digest)
        result=asyncio.run(entry.execute(preview.id,op.id,operations))
        assert result.state==("outcome_unknown" if unknown else "failed")
        assert result.actual_requests==1 and len(calls)==1 and entry.list(sid)==[]
        assert operations.snapshots.active_lease_count==0
        assert not entry.preview_view(preview.id).can_start
    finally:
        asyncio.run(operations.aclose());harness.close()


def test_general_ai_question_can_be_corrected_without_inventing_a_source(space,tmp_path):
    sid=UUID(material_tests.subject(space,"No textbook"))
    harness,operations,_=runtime(space,tmp_path)
    try:
        entry=PersonalQuestionGeneration(space.subjects)
        preview=entry.preview(sid,PracticeConditions(source="ai",count=1))
        op=entry.prepare(preview.id,GenerationRequest(grounding="general_knowledge",idempotency_key=uuid4().hex),operations)
        operations.confirm(op.id,preview_digest=op.preview_digest)
        asyncio.run(entry.execute(preview.id,op.id,operations))
        q=entry.list(sid)[0]
        data=q.model_dump(mode="json",include={"question_type","stem_blocks","options","explanation_blocks",
                            "reference_answer","scoring_rubric","source_locations"})
        result=space.client.post(f"/api/learning/questions/{q.id}/revisions",
                                 json={**data,"expected_revision":q.revision,"difficulty":"基础"})
        assert result.status_code==200,result.text
        assert result.json()["source_locations"]==[] and result.json()["source_kind"]=="ai"
    finally:
        asyncio.run(operations.aclose());harness.close()


def test_practice_start_preserves_actual_managed_images_and_excludes_explanation_assets(space):
    from tests.support.personal_practice_samples import seed_practice_images
    ids=seed_practice_images(space.engine,space.profile)
    entry=PracticeService(space.subjects)
    preview=entry.preview(ids["subject"],PracticeConditions(count=1))
    assert preview.can_start
    practice=entry.start(preview.id,PracticeStartRequest(preview_digest=preview.preview_digest))
    item=practice.items[0]
    path=f"/api/learning/subjects/{practice.subject_id}/practices/{practice.id}/items/{item.id}"
    body=space.client.get(f"/api/learning/practices/{practice.id}")
    assert body.status_code==200
    assert_no_answers(body.json())
    assert item.content.automatic_grading_ready
    for asset in ids["assets"][:6]:
        result=space.client.get(path+"/assets/"+str(asset))
        assert result.status_code==200 and result.content.startswith(b"\x89PNG\r\n\x1a\n")
    for asset in ids["assets"][6:]:
        assert str(asset) not in body.text
        assert space.client.get(path+"/assets/"+str(asset)).status_code==404
    image=space.profile.storage_root/"content/image-0.png"
    image.write_bytes(b"Synthetic corrupted registered bytes")
    invalid=entry.preview(ids["subject"],PracticeConditions(count=1))
    assert invalid.eligible_count==0 and invalid.excluded_count==1 and not invalid.can_start


def test_older_resolved_dispute_does_not_hide_a_later_accepted_valid_round(space):
    from backend.app.models.score_dispute import ScoreDispute
    sid,_,ids,_=seed_bank(space,history=False,count=1)
    answer_id=add_history(space,sid,ids[0],score=0)
    with space.subjects.sessions.begin() as session:
        first=session.scalar(select(GradeAttempt).where(GradeAttempt.answer_id==answer_id))
        second=GradeAttempt(id=uuid4(),answer_id=answer_id,round=2,validation_state="valid",
            execution_source="local_objective",score=5,max_score=10,reason="Synthetic accepted round 2",
            scoring_input=first.scoring_input)
        session.add(second);session.flush()
        session.add(ScoreDispute(id=uuid4(),answer_id=answer_id,disputed_attempt_id=first.id,
            accepted_attempt_id=second.id,state="resolved",reason="Synthetic first dispute"))
        session.flush()
        third=GradeAttempt(id=uuid4(),answer_id=answer_id,round=3,validation_state="valid",
            execution_source="local_objective",score=10,max_score=10,reason="Synthetic accepted round 3",
            scoring_input=first.scoring_input)
        session.add(third);session.flush()
        session.add(ScoreDispute(id=uuid4(),answer_id=answer_id,disputed_attempt_id=second.id,
            accepted_attempt_id=third.id,state="resolved",reason="Synthetic later dispute"))
    preview=PracticeService(space.subjects).preview(sid,PracticeConditions(count=1))
    assert preview.practiced_count==1 and preview.unseen_count==0 and not preview.can_start


def test_start_freezes_study_scope_and_never_uses_paper_filter_for_grading(space):
    sid, _point, _ids, paper_id = seed_bank(space, history=False, count=3)
    first = material_tests.upload(space, str(sid), "book-v1.txt", b"algebra addition first edition").json()
    second = material_tests.upload(space, str(sid), "book-v2.txt", b"algebra addition second edition").json()
    material_tests.index(space, first["id"])
    material_tests.index(space, second["id"])
    series = space.client.post(f"/api/learning/subjects/{sid}/material-series", json={
        "document_id": first["id"], "series_name": "Book", "label": "v1"}).json()
    versions = space.client.post(f"/api/learning/subjects/{sid}/material-series", json={
        "document_id": second["id"], "series_id": series["id"], "label": "v2"}).json()
    v2 = next(v["id"] for v in versions["versions"] if v["document_id"] == second["id"])
    service = PracticeService(space.subjects)
    preview = service.preview(sid, PracticeConditions(count=3, source_document_ids=[paper_id]))
    practice = service.start(preview.id, PracticeStartRequest(preview_digest=preview.preview_digest))
    with space.subjects.sessions() as session:
        before = {i.id: dict(i.scoring_input) for i in session.scalars(
            select(PracticeItem).where(PracticeItem.session_id == practice.id))}
    assert all(value["scope_document_ids"] == [first["id"]] for value in before.values())
    space.client.post(f"/api/learning/material-series/{series['id']}/default-version", json={"version_id": v2}).raise_for_status()
    with space.subjects.sessions() as session:
        after = {i.id: dict(i.scoring_input) for i in session.scalars(
            select(PracticeItem).where(PracticeItem.session_id == practice.id))}
    assert after == before
    next_preview = service.preview(sid, PracticeConditions(count=3))
    next_practice = service.start(next_preview.id, PracticeStartRequest(preview_digest=next_preview.preview_digest))
    with space.subjects.sessions() as session:
        scopes = [i.scoring_input["scope_document_ids"] for i in session.scalars(
            select(PracticeItem).where(PracticeItem.session_id == next_practice.id))]
    assert scopes == [[second["id"]]] * 3
