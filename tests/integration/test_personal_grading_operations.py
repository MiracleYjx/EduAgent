"""T053/T056: production operation protocol against owned PG and Fake HTTP."""

import asyncio
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select, text

from backend.app.models.document_chunk import DocumentChunk
from backend.app.models.operation_run import OperationRun
from backend.app.models.practice import (
    GradeAttempt,
    PracticeAnswer,
    PracticeItem,
    PracticeSession,
)
from backend.app.models.question_revision import QuestionRevision
from backend.app.services.learning_operations import LearningOperations
from backend.app.services.settings.runtime_snapshot import RuntimeSnapshotService
from tests.integration import test_learning_material_versions as material_tests
from tests.integration.test_personal_grading_flow import (
    _general_knowledge_subjective,
    answer,
    item_of,
    start_practice,
)
from tests.integration.test_personal_practice_flow import seed_bank
from tests.postgres_helpers import postgres_subprocess_environment
from tests.support import runtime_consumer_fakes as runtime_fakes
from tests.support.runtime_settings_fakes import settings_harness

space = material_tests.space


@pytest.fixture
def paid(space, tmp_path, request):
    parameter = getattr(request, "param", 3)
    rag = parameter == "rag"
    count = 3 if rag else parameter
    sid, _point, ids, _document = seed_bank(space, history=False, count=count)
    if not rag:
        _general_knowledge_subjective(space, ids)
        with space.subjects.sessions.begin() as session:
            for revision in session.scalars(
                select(QuestionRevision).where(
                    QuestionRevision.question_id.in_(ids),
                    QuestionRevision.question_type == "short_answer",
                )
            ):
                revision.grounding = "general_knowledge"
    harness = settings_harness(
        tmp_path / "runtime", environment_id=space.profile.environment_id
    )
    if rag:
        asyncio.run(harness.activate_model("test-model-a"))
        revision = runtime_fakes.synthetic_revision(
            harness, runtime_fakes.add_embedding_and_rerank
        )
        config = runtime_fakes.SyntheticConfig(harness, revision)
        profile = next(
            p
            for p in config.active_revision().values.providers.values()
            if "embedding" in p.declared_capabilities
        )
        embedding = harness.service.provider_registry.create(
            profile, credential_store=harness.service.credential_store
        )
        identity = embedding.index_identity(dimension=1024)
        asyncio.run(embedding.aclose())
        material = material_tests.upload(
            space,
            str(sid),
            "teaching.txt",
            b"Addition: 2 + 2 = 4. Adding the two values gives four.",
        ).json()
        material_tests.index(space, material["id"], identity=identity)
        other_sid = material_tests.subject(space, "Other subject")
        other = material_tests.upload(
            space, other_sid, "private.txt", b"FOREIGN-PRIVATE-CONTEXT 2 + 2 = 4"
        ).json()
        material_tests.index(space, other["id"], identity=identity)
        with space.subjects.sessions() as session:
            chunk_ids = list(
                map(
                    str,
                    session.scalars(
                        select(DocumentChunk.id).where(
                            DocumentChunk.document_id == material["id"]
                        )
                    ),
                )
            )
    else:
        asyncio.run(harness.activate_model("test-model-a", with_credential=True))
        config = harness.service
    calls = []

    async def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        # A second connection observes the committed reservation before HTTP.
        with space.subjects.sessions() as session:
            active = session.scalar(
                select(OperationRun).where(OperationRun.active_slot == "model")
            )
            assert active is not None and active.actual_requests >= 1
            assert (
                sum(step["actual_requests"] for step in active.steps)
                == active.actual_requests
            )
        assert "FOREIGN-PRIVATE-CONTEXT" not in request.content.decode()
        if "input" in body:
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "model": body["model"],
                    "data": [
                        {
                            "object": "embedding",
                            "index": 0,
                            "embedding": material_tests.VECTOR,
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "total_tokens": 10},
                },
            )
        result = (
            {"rankings": [{"chunk_id": i, "score": 1.0} for i in chunk_ids]}
            if rag and body["model"] == "t213-rerank-a"
            else {
                "score": "6",
                "confidence": 0.99,
                "reason": "Synthetic grading",
                "correct_points": ["one"],
                "missing_knowledge_points": ["two"],
                "suggestions": ["revise"],
            }
        )
        return httpx.Response(
            200,
            json={
                "id": "fake",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": json.dumps(result)},
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            },
        )

    operations = LearningOperations(
        sessions=space.subjects.sessions,
        config=config,
        transport=httpx.MockTransport(handler),
    )
    space.client.app.state.learning_operations = operations
    space.client.app.state.subject_service = space.subjects
    space.subjects.config = config
    space.subjects.runtime_snapshots = RuntimeSnapshotService(config)
    space.subjects.grading_operations_required = True
    practice = start_practice(space, sid, count=count)
    short = item_of(practice, "short_answer")
    answer(space, practice, short, text_answer="四")
    try:
        yield space, practice, short, operations, harness, calls
    finally:
        asyncio.run(operations.aclose())
        asyncio.run(space.subjects.runtime_snapshots.aclose())
        harness.close()


def preview(paid):
    space, practice, *_ = paid
    response = space.client.post(
        f"/api/learning/practices/{practice['id']}/grading-preview",
        json={"idempotency_key": uuid4().hex, "blank_confirmed": True},
    )
    assert response.status_code == 200, response.text
    return response.json()


def confirm(paid, operation):
    response = paid[0].client.post(
        f"/api/learning/operations/{operation['id']}/confirm",
        json={"preview_digest": operation["preview_digest"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def execute(paid, operation):
    space, practice, *_ = paid
    return space.client.post(
        f"/api/learning/practices/{practice['id']}/grade/{operation['id']}", json={}
    )


def test_confirmed_grading_is_durable_and_idempotent(paid):
    space, practice, short, operations, _harness, calls = paid
    blocked = space.client.post(
        f"/api/learning/practices/{practice['id']}/submit",
        json={"blank_confirmed": True},
    )
    assert (
        blocked.status_code == 409
        and blocked.json()["code"] == "GRADING_OPERATION_REQUIRED"
    )
    operation = preview(paid)
    assert calls == [] and operation["request_budget"] == 1
    assert all(
        "grading_input" not in s and "messages" not in s for s in operation["steps"]
    )
    assert (
        "grading_input"
        not in space.client.get(f"/api/learning/operations/{operation['id']}").json()[
            "steps"
        ][0]
    )
    assert execute(paid, operation).status_code == 409 and calls == []
    confirm(paid, operation)
    assert calls == []
    result = execute(paid, operation)
    assert result.status_code == 200, result.text
    report = result.json()
    scored = next(i for i in report["items"] if i["item_id"] == short["id"])
    assert scored["score"] == "6.00" and scored["validation_state"] == "valid"
    assert len(calls) == 1
    assert execute(paid, operation).json() == report
    assert len(calls) == 1
    saved = operations.view(operation["id"])
    assert saved.state == "succeeded" and saved.actual_requests == 1
    assert operations.snapshots.active_lease_count == 0


def test_changed_answers_and_configuration_require_new_preview(paid):
    space, practice, short, _operations, harness, calls = paid
    operation = preview(paid)
    answer(space, practice, short, text_answer="changed")
    response = space.client.post(
        f"/api/learning/operations/{operation['id']}/confirm",
        json={"preview_digest": operation["preview_digest"]},
    )
    assert response.status_code == 409 and response.json()["code"] == "PREVIEW_CHANGED"
    operation = preview(paid)
    confirm(paid, operation)
    current = harness.service.active_revision().revision_id
    asyncio.run(harness.activate_model("test-model-b", expected=current))
    response = execute(paid, operation)
    assert response.status_code == 409 and response.json()["code"] == "PREVIEW_CHANGED"
    assert calls == []
    fresh = preview(paid)
    confirm(paid, fresh)
    assert execute(paid, fresh).status_code == 200
    assert len(calls) == 1 and calls[0]["model"] == "test-model-b"


def test_missing_real_send_authorization_does_not_freeze_or_occupy_the_practice(paid):
    from uuid import UUID
    space, practice, _short, operations, _harness, calls = paid
    operation = preview(paid); confirm(paid, operation)
    operations.transport = None
    response = execute(paid, operation)
    assert response.status_code == 409 and response.json()["code"] == "PAID_SEND_NOT_AUTHORIZED"
    assert calls == [] and operations.view(operation["id"]).actual_requests == 0
    with space.subjects.sessions() as session:
        row = session.get(PracticeSession, UUID(practice["id"]))
        assert row.state == "in_progress" and not row.conditions.get("grading_executions")
        answers = list(session.scalars(select(PracticeAnswer).join(PracticeItem).where(
            PracticeItem.session_id == UUID(practice["id"]))))
        assert all(a.submission_state == "draft" for a in answers)


def test_duplicate_confirmation_and_concurrent_execute_send_once(paid):
    operation = preview(paid)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: confirm(paid, operation), range(2)))
        results = list(pool.map(lambda _: execute(paid, operation), range(2)))
    assert all(r.status_code == 200 for r in results)
    assert len(paid[5]) == 1
    assert paid[3].view(operation["id"]).actual_requests == 1
    with paid[0].subjects.sessions() as session:
        assert session.scalar(select(func.count()).select_from(GradeAttempt)) == 3


@pytest.mark.parametrize("failure", ["timeout", "disconnect", "invalid_json", "cancel"])
def test_failed_or_unknown_sends_are_not_repeated_after_restart(paid, failure):
    space, _practice, short, operations, harness, calls = paid

    async def fail(request):
        calls.append(json.loads(request.content))
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic", request=request)
        if failure == "disconnect":
            raise httpx.ReadError("synthetic", request=request)
        if failure == "cancel":
            raise asyncio.CancelledError()
        return httpx.Response(
            200,
            json={
                "id": "fake",
                "object": "chat.completion",
                "created": 0,
                "model": "test-model-a",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "invalid json"},
                    }
                ],
            },
        )

    operations.transport = httpx.MockTransport(fail)
    operation = preview(paid)
    confirm(paid, operation)
    response = execute(paid, operation)
    assert response.status_code == 200, response.text
    scored = next(i for i in response.json()["items"] if i["item_id"] == short["id"])
    expected = "failed" if failure == "invalid_json" else "outcome_unknown"
    assert scored["validation_state"] == expected and scored["score"] is None
    assert operations.view(operation["id"]).state == expected
    reopened = LearningOperations(
        sessions=space.subjects.sessions,
        config=harness.service,
        transport=httpx.MockTransport(fail),
    )
    space.client.app.state.learning_operations = reopened
    try:
        assert execute(paid, operation).json() == response.json()
        assert len(calls) == 1 and reopened.view(operation["id"]).actual_requests == 1
        assert (
            reopened.snapshots.active_lease_count
            == operations.snapshots.active_lease_count
            == 0
        )
    finally:
        asyncio.run(reopened.aclose())


def test_completed_operation_survives_grade_transaction_rollback_and_restart(paid):
    space, _practice, _short, operations, harness, calls = paid
    operation = preview(paid)
    confirm(paid, operation)
    from backend.app.services.learning_material_service import MaterialError

    with patch(
        "backend.app.services.personal_grading_operations.grade_practice",
        side_effect=MaterialError("SYNTHETIC_SAVE_INTERRUPTED", 409),
    ):
        assert execute(paid, operation).status_code == 409
    assert len(calls) == 1 and operations.view(operation["id"]).state == "succeeded"
    blocked_preview = space.client.post(
        f"/api/learning/practices/{_practice['id']}/grading-preview",
        json={"idempotency_key": uuid4().hex, "blank_confirmed": True},
    )
    assert blocked_preview.status_code == 409
    assert blocked_preview.json()["code"] == "GRADING_OPERATION_IN_PROGRESS"
    with space.subjects.sessions() as session:
        assert session.scalar(select(func.count()).select_from(GradeAttempt)) == 0
        schema = session.scalar(text("SELECT current_schema()"))
    child = subprocess.run([sys.executable, "-I", "-B", "-c",
        ("import os,json;from sqlalchemy import create_engine,text;"
        "e=create_engine(os.environ['DATABASE_URL']);"
        "c=e.connect();r=c.execute(text('SELECT state,actual_requests FROM operation_runs WHERE id=:id'),"
        "{'id':__import__('sys').argv[1]}).one();print(json.dumps(list(r)));c.close();e.dispose()"),
        operation["id"]], env=postgres_subprocess_environment(schema), capture_output=True,
        text=True, encoding="utf-8", timeout=20, check=True)
    assert json.loads(child.stdout) == ["succeeded", 1]
    reopened = LearningOperations(
        sessions=space.subjects.sessions,
        config=harness.service,
        transport=operations.transport,
    )
    space.client.app.state.learning_operations = reopened
    try:
        response = execute(paid, operation)
        assert response.status_code == 200 and response.json()["state"] == "completed"
        assert len(calls) == 1
    finally:
        asyncio.run(reopened.aclose())


@pytest.mark.parametrize("paid", [6], indirect=True)
def test_single_dispute_regrade_only_sends_selected_item_and_keeps_history(paid):
    space, practice, short, operations, _harness, calls = paid
    shorts = [
        i for i in practice["items"] if i["content"]["question_type"] == "short_answer"
    ]
    for item in shorts:
        answer(space, practice, item, text_answer="四")
    operation = preview(paid)
    assert operation["request_budget"] == 2
    confirm(paid, operation)
    original = execute(paid, operation).json()
    assert len(calls) == 2
    selected = next(i for i in original["items"] if i["item_id"] == short["id"])
    raised = space.client.post(
        f"/api/learning/practices/{practice['id']}/disputes",
        json={"item_id": short["id"], "reason": "please review"},
    ).json()
    body = {
        "idempotency_key": uuid4().hex,
        "expected_attempt_id": selected["latest_attempt_id"],
    }
    response = space.client.post(
        f"/api/learning/disputes/{raised['id']}/regrade-preview", json=body
    )
    assert response.status_code == 200, response.text
    regrade = response.json()
    assert regrade["request_budget"] == 1 and len(calls) == 2
    assert (
        space.client.post(
            f"/api/learning/disputes/{raised['id']}/regrade/{regrade['id']}", json={}
        ).status_code
        == 409
    )
    confirm(paid, regrade)
    response = space.client.post(
        f"/api/learning/disputes/{raised['id']}/regrade/{regrade['id']}", json={}
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "awaiting_acceptance" and len(calls) == 3
    replay = space.client.post(
        f"/api/learning/disputes/{raised['id']}/regrade/{regrade['id']}", json={}
    )
    assert replay.status_code == 200 and len(calls) == 3
    report = space.client.get(
        f"/api/learning/practices/{practice['id']}/results"
    ).json()
    for item in report["items"]:
        assert len(item["history"]) == (2 if item["item_id"] == short["id"] else 1)
    selected = next(i for i in report["items"] if i["item_id"] == short["id"])
    assert selected["disputed"]
    stale = space.client.post(
        f"/api/learning/disputes/{raised['id']}/accept",
        json={"attempt_id": raised["disputed_attempt_id"]},
    )
    assert stale.status_code == 409
    accepted = space.client.post(
        f"/api/learning/disputes/{raised['id']}/accept",
        json={"attempt_id": selected["latest_attempt_id"]},
    )
    assert accepted.status_code == 200 and accepted.json()["state"] == "resolved"
    assert operations.snapshots.active_lease_count == 0


def test_objective_and_blank_subjective_still_need_no_operation(paid):
    space, practice, short, _operations, _harness, calls = paid
    answer(space, practice, short, text_answer="")
    single = item_of(practice, "single_choice")
    answer(space, practice, single, choice_ids=["A"])
    response = space.client.post(
        f"/api/learning/practices/{practice['id']}/submit",
        json={"blank_confirmed": True},
    )
    assert response.status_code == 200 and calls == []
    assert all(i["execution_source"] != "model" for i in response.json()["items"])


def test_answered_subjective_with_blank_choices_can_preview_without_a_blank_text_dialog(
    paid,
):
    space, practice, _short, _operations, _harness, calls = paid
    response = space.client.post(
        f"/api/learning/practices/{practice['id']}/grading-preview",
        json={"idempotency_key": uuid4().hex, "blank_confirmed": False},
    )
    assert response.status_code == 200, response.text
    operation = response.json()
    confirm(paid, operation)
    result = execute(paid, operation).json()
    assert result["unanswered_count"] == 2 and result["choice_sample_count"] == 0
    assert len(calls) == 1


@pytest.mark.parametrize("paid", ["rag"], indirect=True)
def test_retrieval_embedding_rerank_and_grading_share_the_confirmed_budget(paid):
    operation = preview(paid)
    assert operation["request_budget"] == 3
    assert paid[5] == []
    confirm(paid, operation)
    response = execute(paid, operation)
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["state"] == "completed", report
    assert len(paid[5]) == 3
    saved = paid[3].view(operation["id"])
    assert saved.actual_requests == 3
    assert saved.steps[0]["task_actual_requests"] == {
        "embedding": 1,
        "rerank": 1,
        "subjective_grading": 1,
    }
    assert all(r["state"] == "response_received" for r in saved.steps[0]["requests"])
    assert paid[3].snapshots.active_lease_count == 0


@pytest.mark.parametrize("fail", [False, True])
def test_explicit_paid_revision_and_failed_regrade_preserve_original_basis(paid, fail):
    from uuid import UUID

    from backend.app.models.question import Question

    space, practice, short, operations, _harness, calls = paid
    operation = preview(paid)
    confirm(paid, operation)
    original = execute(paid, operation).json()
    old_result = next(i for i in original["items"] if i["item_id"] == short["id"])
    raised = space.client.post(
        f"/api/learning/practices/{practice['id']}/disputes",
        json={"item_id": short["id"], "reason": "rubric corrected"},
    ).json()
    with space.subjects.sessions.begin() as session:
        item = session.get(PracticeItem, UUID(short["id"]))
        fixed = dict(item.scoring_input)
        old = session.get(QuestionRevision, item.question_revision_id)
        revision = QuestionRevision(
            id=uuid4(),
            question_id=old.question_id,
            subject_id=old.subject_id,
            revision=old.revision + 1,
            question_type=old.question_type,
            stem_blocks=old.stem_blocks,
            options=old.options,
            reference_answer="corrected four",
            scoring_rubric="corrected rubric",
            explanation_blocks=old.explanation_blocks,
            grounding=old.grounding,
            grounding_refs=old.grounding_refs,
            admission_state="eligible",
            default_score=old.default_score,
        )
        session.add(revision)
        session.flush()
        session.get(Question, item.question_id).current_revision_id = revision.id
        revision_id = str(revision.id)
    body = {
        "idempotency_key": uuid4().hex,
        "expected_attempt_id": old_result["latest_attempt_id"],
        "revision_id": revision_id,
    }
    response = space.client.post(
        f"/api/learning/disputes/{raised['id']}/regrade-preview", json=body
    )
    assert response.status_code == 200, response.text
    regrade = response.json()
    confirm(paid, regrade)
    if fail:

        async def broken(request):
            calls.append(json.loads(request.content))
            return httpx.Response(503, json={"error": {"message": "synthetic failure"}})

        operations.transport = httpx.MockTransport(broken)
    url = f"/api/learning/disputes/{raised['id']}/regrade/{regrade['id']}"
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: space.client.post(url, json={}), range(2)))
    assert all(r.status_code == 200 for r in responses)
    assert len(calls) == 2
    result = space.client.get(
        f"/api/learning/practices/{practice['id']}/results"
    ).json()
    scored = next(i for i in result["items"] if i["item_id"] == short["id"])
    assert [a["reference_answer"] for a in scored["history"]] == ["4", "corrected four"]
    assert scored["history"][0] == old_result["history"][0]
    assert scored["history"][-1]["operation_id"] == regrade["id"]
    assert scored["history"][-1]["question_revision_id"] == revision_id
    if fail:
        assert scored["validation_state"] == "failed" and scored["score"] is None
        rejected = space.client.post(
            f"/api/learning/disputes/{raised['id']}/accept",
            json={"attempt_id": scored["latest_attempt_id"]},
        )
        assert rejected.status_code == 409
    else:
        assert "corrected rubric" in json.dumps(calls[-1]["messages"])
    with space.subjects.sessions() as session:
        assert session.get(PracticeItem, UUID(short["id"])).scoring_input == fixed
    assert operations.snapshots.active_lease_count == 0


def test_accept_waits_for_the_active_paid_regrade_result_to_be_applied(paid):
    from backend.app.services.learning_material_service import MaterialError

    space, practice, short, _operations, _harness, calls = paid
    operation = preview(paid)
    confirm(paid, operation)
    first = execute(paid, operation).json()
    scored = next(i for i in first["items"] if i["item_id"] == short["id"])
    raised = space.client.post(
        f"/api/learning/practices/{practice['id']}/disputes",
        json={"item_id": short["id"], "reason": "review"},
    ).json()
    url = f"/api/learning/disputes/{raised['id']}"
    response = space.client.post(
        url + "/regrade-preview",
        json={
            "idempotency_key": uuid4().hex,
            "expected_attempt_id": scored["latest_attempt_id"],
        },
    )
    assert response.status_code == 200, response.text
    regrade = response.json()
    confirm(paid, regrade)
    assert (
        space.client.post(url + f"/regrade/{regrade['id']}", json={}).status_code == 200
    )
    second = space.client.get(
        f"/api/learning/practices/{practice['id']}/results"
    ).json()
    latest = next(i for i in second["items"] if i["item_id"] == short["id"])[
        "latest_attempt_id"
    ]
    response = space.client.post(
        url + "/regrade-preview",
        json={"idempotency_key": uuid4().hex, "expected_attempt_id": latest},
    )
    assert response.status_code == 200, response.text
    next_round = response.json()
    confirm(paid, next_round)
    with patch(
        "backend.app.services.personal_grading_operations.grade_practice",
        side_effect=MaterialError("SYNTHETIC_SAVE_INTERRUPTED", 409),
    ):
        assert (
            space.client.post(url + f"/regrade/{next_round['id']}", json={}).status_code
            == 409
        )
    assert len(calls) == 3
    rejected = space.client.post(url + "/accept", json={"attempt_id": latest})
    assert (
        rejected.status_code == 409
        and rejected.json()["code"] == "GRADING_OPERATION_IN_PROGRESS"
    )
    assert space.client.get(url).json()["state"] != "resolved"
    assert (
        space.client.post(url + f"/regrade/{next_round['id']}", json={}).status_code
        == 200
    )
    assert len(calls) == 3
    assert (
        space.client.post(url + "/accept", json={"attempt_id": latest}).status_code
        == 409
    )


@pytest.mark.parametrize("outcome", ["failed", "succeeded", "outcome_unknown"])
def test_next_dispute_preview_requires_known_applied_facts_and_new_confirmation(paid, outcome):
    space, practice, short, operations, _harness, calls = paid
    initial = preview(paid)
    confirm(paid, initial)
    first = execute(paid, initial).json()
    scored = next(item for item in first["items"] if item["item_id"] == short["id"])
    raised = space.client.post(f"/api/learning/practices/{practice['id']}/disputes",
        json={"item_id": short["id"], "reason": "Synthetic new round"}).json()
    url = f"/api/learning/disputes/{raised['id']}"
    previous = space.client.post(url + "/regrade-preview", json={"idempotency_key": uuid4().hex,
        "expected_attempt_id": scored["latest_attempt_id"]}).json()
    confirm(paid, previous)
    if outcome != "succeeded":
        async def fail(request):
            calls.append(json.loads(request.content))
            if outcome == "outcome_unknown":
                raise httpx.ReadTimeout("Synthetic response lost", request=request)
            return httpx.Response(503, json={"error": {"message": "Synthetic completed failure"}})
        operations.transport = httpx.MockTransport(fail)
    result = space.client.post(url + f"/regrade/{previous['id']}", json={})
    assert result.status_code == 200, result.text
    latest = next(item for item in space.client.get(f"/api/learning/practices/{practice['id']}/results").json()["items"]
                  if item["item_id"] == short["id"])
    assert latest["history"][0] == scored["history"][0]
    assert latest["validation_state"] == ("valid" if outcome == "succeeded" else outcome)
    count = len(calls)
    next_preview = space.client.post(url + "/regrade-preview", json={"idempotency_key": uuid4().hex,
        "expected_attempt_id": latest["latest_attempt_id"]})
    assert len(calls) == count
    if outcome == "outcome_unknown":
        assert next_preview.status_code == 409, next_preview.text
        assert next_preview.json()["code"] == "UNKNOWN_OUTCOME_ACK_REQUIRED"
    else:
        assert next_preview.status_code == 200, next_preview.text
        assert next_preview.json()["id"] != previous["id"]
        assert next_preview.json()["state"] == "prepared"
        blocked = space.client.post(url + f"/regrade/{next_preview.json()['id']}", json={})
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "CONFIRMATION_REQUIRED"
        assert len(calls) == count
