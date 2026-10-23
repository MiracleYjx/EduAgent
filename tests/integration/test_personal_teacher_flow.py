"""Real Test PG + guarded Mock HTTP, not a teaching-quality evaluation."""

import asyncio
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select

from backend.app.models.conversation import Message
from backend.app.models.document_chunk import DocumentChunk
from backend.app.models.practice import GradeAttempt
from backend.app.models.subject import KnowledgePoint
from backend.app.services.learning_operations import LearningOperations
from backend.app.services.teacher_service import TeacherService
from tests.integration import test_learning_material_versions as materials
from tests.integration.test_personal_grading_flow import answer, start_practice
from tests.integration.test_personal_practice_flow import seed_bank
from tests.support.runtime_settings_fakes import settings_harness

space = materials.space


@pytest.fixture
def teacher(space, tmp_path):
    harness = settings_harness(
        tmp_path / "teacher-runtime", environment_id=space.profile.environment_id
    )
    asyncio.run(harness.activate_model("teacher-fake", with_credential=True))
    calls = []
    behavior = {"mode": "valid"}

    async def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        await asyncio.sleep(0.01)
        if behavior["mode"] == "unknown":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        if behavior["mode"] == "cancel":
            raise asyncio.CancelledError
        context = json.loads(body["messages"][-1]["content"])["context"]
        source_ids = [s["id"] for s in context["sources"]]
        output = {
            "answer_body": "Synthetic explanation; protocol evidence only.",
            "basis": "uploaded_material" if source_ids else "model_knowledge",
            "citations": source_ids,
            "uncertainties": [] if source_ids else ["No uploaded material evidence"],
            "practice_knowledge_point_ids": [
                p["id"] for p in context["knowledge_points"][:1]
            ],
        }
        if behavior["mode"] == "bad_citation":
            output["citations"] = ["not-provided"]
        return httpx.Response(
            200,
            json={
                "id": "fake-teacher",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "not JSON"
                            if behavior["mode"] == "bad_json"
                            else json.dumps(output),
                        },
                    }
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    operations = LearningOperations(
        sessions=space.subjects.sessions, config=harness.service, transport=transport
    )
    space.client.app.state.learning_operations = operations
    space.client.app.state.subject_service = space.subjects
    sid = materials.subject(space, "Teacher subject")
    created = space.client.post(
        f"/api/learning/subjects/{sid}/conversations", json={"title": "Synthetic chat"}
    )
    assert created.status_code == 201, created.text
    cid = created.json()["id"]
    result = SimpleNamespace(
        space=space,
        harness=harness,
        operations=operations,
        calls=calls,
        behavior=behavior,
        sid=sid,
        cid=cid,
        transport=transport,
    )
    try:
        yield result
    finally:
        asyncio.run(operations.aclose())
        harness.close()


def path(t, suffix=""):
    return f"/api/learning/conversations/{t.cid}{suffix}?subject_id={t.sid}"


def prepare(t, **changes):
    body = {"text": "Explain algebra", "idempotency_key": uuid4().hex, **changes}
    response = t.space.client.post(path(t, "/messages/preview"), json=body)
    assert response.status_code == 200, response.text
    return response.json()


def confirm(t, turn):
    operation = turn["operation"]
    response = t.space.client.post(
        path(t, f"/messages/{operation['id']}/confirm"),
        json={"preview_digest": operation["preview_digest"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def execute(t, turn):
    response = t.space.client.post(
        path(t, f"/messages/{turn['operation']['id']}/execute"), json={}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_first_confirmation_later_user_send_and_refresh_never_resends(teacher):
    t = teacher
    first = prepare(t)
    assert first["requires_confirmation"] and t.calls == []
    assert "messages" not in first["operation"]["steps"][0]
    blocked = t.space.client.post(
        path(t, f"/messages/{first['operation']['id']}/execute"), json={}
    )
    assert (
        blocked.status_code == 409
        and blocked.json()["code"] == "TEACHER_CONFIRMATION_REQUIRED"
    )
    confirm(t, first)
    confirm(t, first)
    assert t.calls == []
    done = execute(t, first)
    assert (
        done["operation"]["state"] == "succeeded"
        and done["operation"]["actual_requests"] == 1
    )
    for _ in range(2):
        assert t.space.client.get(path(t)).status_code == 200
        execute(t, first)
    assert len(t.calls) == 1
    second = prepare(t, text="Use a simpler example")
    assert not second["requires_confirmation"] and len(t.calls) == 1
    execute(t, second)
    assert (
        len(t.calls) == 2 and len(t.space.client.get(path(t)).json()["messages"]) == 4
    )
    assert t.operations.snapshots.active_lease_count == 0


def test_cancel_and_idempotency_do_not_make_a_paid_send(teacher):
    t = teacher
    key = uuid4().hex
    first = prepare(t, idempotency_key=key)
    assert (
        prepare(t, idempotency_key=key)["operation"]["id"] == first["operation"]["id"]
    )
    changed = t.space.client.post(
        path(t, "/messages/preview"), json={"text": "different", "idempotency_key": key}
    )
    assert (
        changed.status_code == 409 and changed.json()["code"] == "IDEMPOTENCY_CONFLICT"
    )
    cancelled = t.space.client.post(
        path(t, f"/messages/{first['operation']['id']}/cancel"), json={}
    )
    assert (
        cancelled.status_code == 200
        and cancelled.json()["pending_operation_id"] is None
    )
    assert (
        t.space.client.post(
            path(t, f"/messages/{first['operation']['id']}/execute"), json={}
        ).status_code
        == 409
    )
    assert t.calls == []


def test_model_change_needs_new_consent_and_old_preview_is_stale(teacher):
    t = teacher
    first = prepare(t)
    confirm(t, first)
    execute(t, first)
    pending = prepare(t, text="next")
    asyncio.run(
        t.harness.activate_model(
            "teacher-fake-b",
            with_credential=True,
            expected=t.harness.service.active_revision().revision_id,
        )
    )
    rejected = t.space.client.post(
        path(t, f"/messages/{pending['operation']['id']}/execute"), json={}
    )
    assert rejected.status_code == 409 and rejected.json()["code"] == "PREVIEW_CHANGED"
    assert len(t.calls) == 1
    assert (
        t.space.client.post(
            path(t, f"/messages/{pending['operation']['id']}/cancel"), json={}
        ).status_code
        == 200
    )
    fresh = prepare(t, text="next")
    assert (
        fresh["requires_confirmation"]
        and fresh["operation"]["steps"][0]["model"] == "teacher-fake-b"
    )
    assert (
        t.space.client.post(
            path(t, f"/messages/{fresh['operation']['id']}/execute"), json={}
        ).status_code
        == 409
    )


def test_input_context_change_requires_new_preview_before_confirmation(teacher):
    t = teacher
    turn = prepare(t)
    with t.space.subjects.sessions.begin() as session:
        session.add(
            KnowledgePoint(
                id=uuid4(),
                subject_id=UUID(t.sid),
                name="new confirmed point",
                confirmation_state="confirmed",
            )
        )
    response = t.space.client.post(
        path(t, f"/messages/{turn['operation']['id']}/confirm"),
        json={"preview_digest": turn["operation"]["preview_digest"]},
    )
    assert (
        response.status_code == 409
        and response.json()["code"] == "PREVIEW_CHANGED"
        and t.calls == []
    )


def test_no_hit_material_scope_change_still_expires_confirmation(teacher):
    t = teacher
    turn = prepare(t, text="unmatchedzzword")
    doc = materials.upload(t.space, t.sid, "scope.md", b"# Geometry\n\ntriangle angle.").json()
    materials.index(t.space, doc["id"])
    response = t.space.client.post(path(t, f"/messages/{turn['operation']['id']}/confirm"),
        json={"preview_digest": turn["operation"]["preview_digest"]})
    assert response.status_code == 409 and response.json()["code"] == "PREVIEW_CHANGED"
    assert t.calls == []


def test_production_send_stays_closed_without_real_paid_authorization(teacher):
    t = teacher
    turn = prepare(t)
    confirm(t, turn)
    mock = t.operations.transport
    real = httpx.AsyncHTTPTransport()
    t.operations.transport = real
    try:
        response = t.space.client.post(path(t, f"/messages/{turn['operation']['id']}/execute"), json={})
        assert response.status_code == 409 and response.json()["code"] == "PAID_SEND_NOT_AUTHORIZED"
        assert t.operations.view(UUID(turn["operation"]["id"])).actual_requests == 0
        assert t.calls == []
    finally:
        asyncio.run(real.aclose())
        t.operations.transport = mock


def test_concurrent_execute_claims_one_operation_and_appends_one_answer(teacher):
    t = teacher
    turn = prepare(t)
    confirm(t, turn)
    service = TeacherService(t.space.subjects, t.operations)

    async def both():
        return await asyncio.gather(
            *(
                service.execute(UUID(t.cid), UUID(t.sid), UUID(turn["operation"]["id"]))
                for _ in range(2)
            )
        )

    asyncio.run(both())
    assert len(t.calls) == 1
    with t.space.subjects.sessions() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.conversation_id == UUID(t.cid))
            )
            == 2
        )
    assert t.operations.view(UUID(turn["operation"]["id"])).actual_requests == 1
    assert t.operations.snapshots.active_lease_count == 0


@pytest.mark.parametrize("mode", ["unknown", "cancel"])
def test_unknown_survives_new_service_without_resending_or_new_turn(teacher, mode):
    t = teacher
    t.behavior["mode"] = mode
    turn = prepare(t)
    confirm(t, turn)
    done = execute(t, turn)
    assert done["operation"]["state"] == "outcome_unknown"
    restarted = LearningOperations(
        sessions=t.space.subjects.sessions,
        config=t.harness.service,
        transport=t.transport,
    )
    try:
        result = asyncio.run(
            TeacherService(t.space.subjects, restarted).execute(
                UUID(t.cid), UUID(t.sid), UUID(turn["operation"]["id"])
            )
        )
        assert result.operation.state == "outcome_unknown" and len(t.calls) == 1
        pending = t.space.client.post(
            path(t, "/messages/preview"),
            json={"text": "do not retry", "idempotency_key": uuid4().hex},
        )
        assert (
            pending.status_code == 409
            and pending.json()["code"] == "TEACHER_OPERATION_PENDING"
        )
        assert restarted.snapshots.active_lease_count == 0
    finally:
        asyncio.run(restarted.aclose())


@pytest.mark.parametrize("mode", ["bad_json", "bad_citation"])
def test_invalid_response_is_a_real_failure_with_no_fabricated_answer(teacher, mode):
    t = teacher
    t.behavior["mode"] = mode
    turn = prepare(t)
    confirm(t, turn)
    done = execute(t, turn)
    assert done["operation"]["state"] == "failed" and len(t.calls) == 1
    message = done["conversation"]["messages"][-1]
    assert (
        message["role"] == "assistant"
        and message["reply"] is None
        and message["text"] == ""
    )
    assert t.operations.snapshots.active_lease_count == 0


def test_subject_isolation_general_knowledge_and_chat_delete_leave_learning_unchanged(
    teacher,
):
    t = teacher
    before = t.space.client.get(f"/api/learning/subjects/{t.sid}/diagnosis").json()
    turn = prepare(
        t, text="I understand", grounding="general_knowledge", retrieval_mode="none"
    )
    confirm(t, turn)
    execute(t, turn)
    context = json.loads(t.calls[0]["messages"][-1]["content"])["context"]
    assert context["sources"] == [] and context["retrieval_mode"] == "none"
    snapshot = t.operations.snapshots.load(
        UUID(turn["operation"]["runtime_snapshot_id"])
    )
    assert set(snapshot.resolved_task_bindings) == {"default_llm"}
    other = materials.subject(t.space, "Other")
    assert t.space.client.get(path(t).replace(t.sid, other)).status_code == 404
    assert (
        t.space.client.delete(
            path(t).replace(t.sid, other), headers={"Content-Type": "application/json"}
        ).status_code
        == 404
    )
    assert (
        t.space.client.delete(
            path(t), headers={"Content-Type": "application/json"}
        ).status_code
        == 200
    )
    assert t.space.client.get(path(t)).status_code == 404
    assert (
        t.space.client.get(f"/api/learning/subjects/{t.sid}/diagnosis").json() == before
    )
    assert t.operations.view(UUID(turn["operation"]["id"])).actual_requests == 1
    with t.space.subjects.sessions() as session:
        assert session.scalar(select(func.count()).select_from(GradeAttempt)) == 0


def test_wrong_question_context_and_real_citations_are_frozen_scoped_and_read_only(
    teacher,
):
    t = teacher
    space = t.space
    sid, point, _, _ = seed_bank(space, name="Question teacher", history=False, count=1)
    doc = materials.upload(
        space, str(sid), "algebra.md", b"# algebra\n\nalgebra addition equals four."
    ).json()
    materials.index(space, doc["id"])
    with space.subjects.sessions.begin() as session:
        chunk = session.scalar(
            select(DocumentChunk).where(DocumentChunk.document_id == UUID(doc["id"]))
        )
        chunk.chunk_metadata = {
            **chunk.chunk_metadata,
            "knowledge_points": [str(point)],
            "scope_confirmation": {"knowledge_points": {str(point): "confirmed"}},
        }
    practice = start_practice(space, sid, count=1)
    item = practice["items"][0]
    t.sid = str(sid)
    t.cid = space.client.post(
        f"/api/learning/subjects/{sid}/conversations", json={}
    ).json()["id"]
    draft = space.client.post(
        path(t, "/messages/preview"),
        json={
            "text": "algebra",
            "idempotency_key": uuid4().hex,
            "practice_item_id": item["id"],
        },
    )
    assert draft.status_code == 409 and draft.json()["code"] == "PRACTICE_NOT_SUBMITTED"
    answer(space, practice, item, choice_ids=["B"])
    report = space.client.post(
        f"/api/learning/practices/{practice['id']}/submit", json={}
    ).json()
    before = space.client.get(f"/api/learning/subjects/{sid}/diagnosis").json()
    turn = prepare(t, text="algebra", practice_item_id=item["id"])
    confirm(t, turn)
    done = execute(t, turn)
    assert done["operation"]["state"] == "succeeded"
    context = json.loads(t.calls[-1]["messages"][-1]["content"])["context"]
    assert context["question"]["user_answer"] == ["B"]
    assert context["question"]["grade"]["score"] == "0.00"
    assert context["question"]["reference_answer"] == "A"
    assert context["sources"] and {s["document_id"] for s in context["sources"]} == {
        doc["id"]
    }
    assert len(json.dumps(context, ensure_ascii=False)) <= 6000
    citation = done["conversation"]["messages"][-1]["citations"][0]
    assert citation["document_id"] == doc["id"] and citation["location"]["anchor"]
    assert (
        space.client.get(
            f"/api/learning/materials/{citation['document_id']}"
        ).status_code
        == 200
    )
    assert (
        space.client.delete(
            path(t), headers={"Content-Type": "application/json"}
        ).status_code
        == 200
    )
    assert (
        space.client.get(f"/api/learning/practices/{practice['id']}/results").json()
        == report
    )
    assert space.client.get(f"/api/learning/subjects/{sid}/diagnosis").json() == before
