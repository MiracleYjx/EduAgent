"""Operation contract against real adapters and Fake HTTP; no model-effect claim."""

import asyncio
import importlib.util
import json
from uuid import uuid4

import httpx
import pytest
from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tests.support.runtime_settings_fakes import settings_harness


class Reply(BaseModel):
    value: int


def api():
    module = "backend.app.services.learning_operations"
    if importlib.util.find_spec(module) is None:
        pytest.fail("T012_IMPLEMENTATION_REQUIRED: learning_operations", pytrace=False)
    return __import__(module, fromlist=["LearningOperations"])


@pytest.fixture
def env(tmp_path):
    module = api()
    from backend.app.models.operation_run import OperationRun

    harness = settings_harness(tmp_path)
    asyncio.run(harness.activate_model("test-model-a", with_credential=True))
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    OperationRun.__table__.create(engine)
    calls = []

    async def handler(request):
        calls.append(json.loads(request.content))
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
                        "message": {"role": "assistant", "content": '{"value": 1}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 5,
                    "total_tokens": 10,
                },
            },
        )

    transport = httpx.MockTransport(handler)
    sessions = sessionmaker(engine, expire_on_commit=False)
    service = module.LearningOperations(
        sessions=sessions, config=harness.service, transport=transport
    )
    try:
        yield module, service, harness, calls, sessions
    finally:
        asyncio.run(service.aclose())
        harness.close()
        engine.dispose()


def steps(module, count=1, text="Return JSON"):
    return [
        module.StepPlan(
            step_id=f"step-{i}",
            task_kind="question_generation",
            messages=[{"role": "user", "content": text}],
            request_budget=1,
        )
        for i in range(count)
    ]


def prepare(env, count=1, **kwargs):
    module, service, *_ = env
    return service.prepare(
        purpose="generation",
        idempotency_key=uuid4().hex,
        steps=steps(module, count),
        **kwargs,
    )


async def invoke(runtime, step):
    lease = runtime.acquire(step.task_kind)
    return await lease.client.generate_structured(
        step.messages, Reply, model=runtime.requested_model(step.task_kind)
    )


def execute(env, operation):
    return asyncio.run(env[1].execute(operation.id, invoke=invoke))


def confirm(env, operation):
    return env[1].confirm(operation.id, preview_digest=operation.preview_digest)


def test_unconfirmed_and_missing_paid_authorization_send_zero(env):
    module, service, _, calls, _ = env
    operation = prepare(env)
    with pytest.raises(module.OperationError, match="CONFIRMATION_REQUIRED"):
        execute(env, operation)
    assert calls == []
    # A non-Mock transport is never authorized just because a user confirms.
    service.transport = None
    confirm(env, operation)
    with pytest.raises(module.OperationError, match="PAID_SEND_NOT_AUTHORIZED"):
        execute(env, operation)
    assert calls == [] and service.view(operation.id).actual_requests == 0


def test_actual_derived_steps_and_idempotent_repeated_execute(env):
    operation = prepare(env, 3)
    confirm(env, operation)
    result = execute(env, operation)
    again = execute(env, operation)
    assert result.state == again.state == "succeeded"
    assert result.request_budget == result.actual_requests == 3
    assert len(env[3]) == 3
    assert [s["state"] for s in result.steps] == ["succeeded"] * 3
    assert result.estimate_state == "unknown" and result.estimated_cost is None
    assert env[1].snapshots.active_lease_count == 0


def test_prepare_idempotency_and_scope_conflict(env):
    module, service, *_ = env
    key = uuid4().hex
    first = service.prepare(
        purpose="generation", idempotency_key=key, steps=steps(module)
    )
    assert (
        service.prepare(
            purpose="generation", idempotency_key=key, steps=steps(module)
        ).id
        == first.id
    )
    with pytest.raises(module.OperationError, match="IDEMPOTENCY_CONFLICT"):
        service.prepare(
            purpose="generation",
            idempotency_key=key,
            steps=steps(module, text="different"),
        )


def test_wrong_preview_and_oversized_input_are_rejected(env):
    module, service, *_ = env
    operation = prepare(env)
    with pytest.raises(module.OperationError, match="PREVIEW_CHANGED"):
        service.confirm(operation.id, preview_digest="wrong")
    with pytest.raises(module.OperationError, match="LIMIT_EXCEEDED"):
        service.prepare(
            purpose="generation",
            idempotency_key=uuid4().hex,
            steps=steps(module, text="x" * 65536),
        )
    assert env[3] == []


@pytest.mark.parametrize(
    "failure", ["timeout", "disconnect", "schema", "oversize", "429"]
)
def test_failure_does_not_retry_and_releases_leases(env, failure):
    _module, service, _, calls, _ = env

    async def fail(request):
        calls.append(json.loads(request.content))
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic", request=request)
        if failure == "disconnect":
            raise httpx.ReadError("synthetic", request=request)
        if failure == "429":
            return httpx.Response(429, json={"error": {"message": "synthetic"}})
        content = "bad json" if failure == "schema" else "x" * 65537
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
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    service.transport = httpx.MockTransport(fail)
    operation = prepare(env)
    confirm(env, operation)
    result = execute(env, operation)
    assert len(calls) == result.actual_requests == 1
    assert result.state == (
        "outcome_unknown" if failure in {"timeout", "disconnect", "oversize"} else "failed"
    )
    # Rejecting an oversized stream before recording its complete response
    # preserves the already reserved request's unknown billing outcome.
    assert result.steps[0]["requests"][0]["state"] == (
        "outcome_unknown" if failure in {"timeout", "disconnect", "oversize"}
        else "response_received"
    )
    assert result.steps[0]["state"] == result.state
    assert service.snapshots.active_lease_count == 0
    assert execute(env, operation).actual_requests == 1 and len(calls) == 1


def test_empty_content_is_a_single_send_failure_with_no_result(env):
    _module, service, _, calls, _ = env

    async def empty(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "fake-empty",
                "object": "chat.completion",
                "created": 0,
                "model": "test-model-a",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "reasoning_content": "private reasoning",
                        },
                        "finish_reason": "length",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 2000,
                    "total_tokens": 2010,
                },
            },
        )

    service.transport = httpx.MockTransport(empty)
    operation = prepare(env)
    confirm(env, operation)
    result = execute(env, operation)

    assert len(calls) == result.actual_requests == 1
    assert result.state == "failed"
    assert result.steps[0]["state"] == "failed"
    assert result.steps[0]["error_code"] == "ProviderEmptyResponse"
    assert result.steps[0]["result"] is None
    assert result.steps[0]["usage"]["completion_tokens"] == 2000
    assert execute(env, operation).actual_requests == 1
    assert len(calls) == 1
    assert service.snapshots.active_lease_count == 0


def test_partial_success_and_explicit_user_retry_excludes_success(env):
    _module, service, _, calls, _ = env
    original = service.transport

    async def fail_second(request):
        if len(calls) == 1:
            calls.append(json.loads(request.content))
            raise httpx.ReadTimeout("synthetic", request=request)
        return await original.handle_async_request(request)

    service.transport = httpx.MockTransport(fail_second)
    operation = prepare(env, 2)
    confirm(env, operation)
    result = execute(env, operation)
    assert result.state == "outcome_unknown" and result.steps[0]["result"] == {
        "value": 1
    }
    service.transport = original
    retry = service.retry(
        operation.id, idempotency_key=uuid4().hex, acknowledge_unknown=True
    )
    assert (
        retry.id != operation.id
        and retry.request_budget == 1
        and retry.state == "prepared"
    )
    assert len(calls) == 2
    confirm(env, retry)
    assert execute(env, retry).state == "succeeded"
    assert len(calls) == 3 and service.view(operation.id).steps[0]["result"] == {
        "value": 1
    }


def test_restart_never_resends_and_configuration_switch_does_not_rebind(env):
    module, service, harness, calls, sessions = env
    operation = prepare(env)
    confirm(env, operation)
    old_revision = harness.service.active_revision().revision_id
    asyncio.run(harness.activate_model("test-model-b", expected=old_revision))
    reopened = module.LearningOperations(
        sessions=sessions, config=harness.service, transport=service.transport
    )
    assert calls == [] and reopened.view(operation.id).state == "confirmed"
    result = asyncio.run(reopened.execute(operation.id, invoke=invoke))
    assert result.state == "succeeded" and calls[0]["model"] == "test-model-a"
    assert reopened.snapshots.active_lease_count == 0
    asyncio.run(reopened.aclose())


def test_concurrent_same_operation_sends_once(env):
    operation = prepare(env)
    confirm(env, operation)

    async def run():
        return await asyncio.gather(
            env[1].execute(operation.id, invoke=invoke),
            env[1].execute(operation.id, invoke=invoke),
        )

    asyncio.run(run())
    assert len(env[3]) == 1 and env[1].view(operation.id).actual_requests == 1


def test_unplanned_extra_provider_request_blocked_before_second_send(env):
    _module, service, _, calls, _ = env

    async def twice(runtime, step):
        await invoke(runtime, step)
        return await invoke(runtime, step)

    operation = prepare(env)
    confirm(env, operation)
    result = asyncio.run(service.execute(operation.id, invoke=twice))
    assert result.state == "failed" and result.actual_requests == 1 and len(calls) == 1
    assert service.snapshots.active_lease_count == 0


def test_grading_dependency_budget_cannot_be_borrowed_by_another_task(env):
    module, service, _, calls, _ = env
    operation = service.prepare(purpose="practice_grading:synthetic", idempotency_key=uuid4().hex,
        steps=[module.StepPlan(step_id="item", task_kind="subjective_grading", messages=[],
            grading_input={"frozen_input": "synthetic"}, request_budget=2,
            task_budgets={"subjective_grading": 1, "rerank": 1})])
    confirm(env, operation)

    async def twice(runtime, step):
        provider = runtime.acquire("subjective_grading").client
        await provider.generate_structured([{"role":"user","content":"Return JSON"}], Reply)
        return await provider.generate_structured([{"role":"user","content":"Return JSON"}], Reply)

    result = asyncio.run(service.execute(operation.id, invoke=twice))
    assert result.state == "failed" and result.request_budget == 2
    assert result.actual_requests == len(calls) == 1
    assert result.steps[0]["error_code"] == "REQUEST_BUDGET_EXCEEDED"
    assert result.steps[0]["task_actual_requests"] == {"subjective_grading": 1}
    assert service.snapshots.active_lease_count == 0


def test_vision_grading_uses_its_own_image_budget_and_releases_the_lease(env):
    import base64
    import hashlib
    from io import BytesIO

    from PIL import Image

    from tests.support import runtime_consumer_fakes as fakes
    module, _service, harness, calls, sessions = env

    def add_vision(values):
        identifier, _revision = fakes.add_vision(values, model="deepseek-v4-flash-vision-exp")
        # The real adapter requires this exact capability identity. MockTransport
        # still intercepts every byte; this is not a vendor capability test.
        values["providers"][str(identifier)]["base_url"] = "https://api.deepseek.com/v1"
        values["providers"][str(identifier)]["credential_ref"] = harness.provider_reference.model_dump(mode="json")

    config = fakes.SyntheticConfig(harness, fakes.synthetic_revision(harness, add_vision))
    service = module.LearningOperations(sessions=sessions, config=config, transport=_service.transport)
    picture = BytesIO()
    Image.effect_noise((512,512),100).convert("RGB").save(picture,format="PNG")
    pixels = picture.getvalue()
    assert len(pixels) > service.limits.text_request_bytes
    operation = service.prepare(purpose="practice_grading:vision", idempotency_key=uuid4().hex,
        steps=[module.StepPlan(step_id="image", task_kind="vision", messages=[], request_budget=1,
            grading_input={"asset_sha256":hashlib.sha256(pixels).hexdigest()}, task_budgets={"vision":1})])
    service.confirm(operation.id,preview_digest=operation.preview_digest)

    async def vision(runtime, step):
        lease = runtime.acquire("vision")
        return await lease.client.generate_structured([{"role":"user","content":[
            {"type":"text","text":"Return JSON"},
            {"type":"image","image":{"encoding":"base64","mime_type":"image/png",
                "value":base64.b64encode(pixels).decode()}}
        ]}], Reply, model=runtime.requested_model("vision"))

    try:
        result = asyncio.run(service.execute(operation.id,invoke=vision))
        assert result.state == "succeeded", result
        assert result.actual_requests == len(calls) == 1
        assert calls[0]["model"] == "deepseek-v4-flash-vision-exp"
        assert service.snapshots.active_lease_count == 0
    finally:
        asyncio.run(service.aclose())


def test_runtime_dependency_loss_is_not_current_configuration_fallback(env):
    _module, service, _harness, calls, _ = env
    operation = prepare(env)
    confirm(env, operation)
    snapshot = service.snapshots.load(operation.runtime_snapshot_id)
    path = service.snapshots.root / (str(snapshot.snapshot_id) + ".json")
    assert path.exists()
    path.unlink()
    from backend.app.services.settings.runtime_snapshot import RuntimeSnapshotError

    with pytest.raises(RuntimeSnapshotError):
        execute(env, operation)
    assert calls == []


def test_changed_input_blocked_before_send(env):
    _module, service, _, calls, _ = env

    async def changed(runtime, step):
        return await runtime.acquire(step.task_kind).client.generate_structured(
            [{"role": "user", "content": "unconfirmed content"}],
            Reply,
            model=runtime.requested_model(step.task_kind),
        )

    operation = prepare(env)
    confirm(env, operation)
    result = asyncio.run(service.execute(operation.id, invoke=changed))
    assert result.state == "failed" and result.actual_requests == 0 and calls == []
    assert result.steps[0]["error_code"] == "INPUT_SCOPE_CHANGED"


def test_different_operations_share_one_actual_request_slot(env):
    _module, service, _, calls, _ = env
    first = prepare(env)
    second = prepare(env)
    confirm(env, first)
    confirm(env, second)
    original = service.transport

    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def delayed(request):
            entered.set()
            await release.wait()
            return await original.handle_async_request(request)

        service.transport = httpx.MockTransport(delayed)
        active = asyncio.create_task(service.execute(first.id, invoke=invoke))
        await entered.wait()
        rejected = await service.execute(second.id, invoke=invoke)
        assert rejected.actual_requests == 0 and rejected.state == "failed"
        release.set()
        return await active

    assert asyncio.run(run()).state == "succeeded"
    assert len(calls) == 1 and service.snapshots.active_lease_count == 0


def test_cancelled_in_flight_request_stays_unknown_without_resend(env):
    _module, service, _, calls, _ = env

    async def run():
        entered = asyncio.Event()

        async def delayed(request):
            calls.append(json.loads(request.content))
            entered.set()
            await asyncio.Event().wait()

        service.transport = httpx.MockTransport(delayed)
        operation = prepare(env)
        confirm(env, operation)
        active = asyncio.create_task(service.execute(operation.id, invoke=invoke))
        await entered.wait()
        active.cancel()
        result = await active
        assert result.state == "outcome_unknown" and result.actual_requests == 1
        assert (await service.execute(operation.id, invoke=invoke)).actual_requests == 1

    asyncio.run(run())
    assert len(calls) == 1 and service.snapshots.active_lease_count == 0


@pytest.fixture
def http(env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.app.api.learning_operations import install_learning_operations
    from backend.app.core.settings_security import SetupSourcePolicy

    app = FastAPI()
    install_learning_operations(app)
    app.state.learning_operations = env[1]
    app.state.learning_source_policy = SetupSourcePolicy(
        allowed_hosts=frozenset({"127.0.0.1:8000"})
    )
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        yield client


def test_personal_http_no_setup_token_and_exact_source_boundary(env, http):
    operation = prepare(env)
    response = http.get("/api/learning/operations/" + str(operation.id))
    assert response.status_code == 200 and response.json()["state"] == "prepared"
    assert (
        http.get(
            "/api/learning/operations/" + str(operation.id),
            headers={"Host": "evil.invalid"},
        ).status_code
        == 403
    )
    path = "/api/learning/operations/" + str(operation.id) + "/confirm"
    body = {"preview_digest": operation.preview_digest}
    assert http.post(path, json=body).status_code == 403
    assert (
        http.post(
            path, json=body, headers={"Origin": "http://evil.invalid"}
        ).status_code
        == 403
    )
    assert (
        http.post(
            path, json=body, headers={"Origin": "http://127.0.0.1:8000"}
        ).status_code
        == 200
    )
    assert env[3] == []


def test_personal_http_validation_and_no_implicit_business_planner(env, http):
    path = "/api/learning/operations/preview"
    headers = {"Origin": "http://127.0.0.1:8000"}
    body = {"purpose": "generation", "idempotency_key": "test-key", "scope": {}}
    assert http.post(path, json=body, headers=headers).status_code == 503
    response = http.post(
        path,
        json={**body, "api_key": "synthetic-secret-must-not-echo"},
        headers=headers,
    )
    assert response.status_code == 422 and "synthetic-secret" not in response.text
    response = http.post(
        path,
        content="x" * 65537,
        headers={**headers, "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert env[3] == []


def test_embedding_batches_query_and_main_response_all_consume_budget(env, monkeypatch):
    module, service, harness, calls, _ = env
    profile_id = str(uuid4())
    revision_id = str(uuid4())
    old = harness.service.active_revision().revision_id
    values = harness.values("test-model-a")
    values["providers"][profile_id] = {
        "profile_id": profile_id,
        "profile_revision": revision_id,
        "display_name": "synthetic embedding",
        "adapter_id": "openai_compatible",
        "model_id": "embed-a",
        "base_url": "https://model.invalid/v1",
        "auth_mode": "none",
        "declared_capabilities": ["embedding"],
        "parameters": {"dimension": 1024, "batch_size": 2},
        "enabled": True,
    }
    values["bindings"]["embedding"] = {
        "task_kind": "embedding",
        "profile_id": profile_id,
        "profile_revision": revision_id,
    }

    async def fake(request):
        body = json.loads(request.content)
        calls.append(body)
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(
                200,
                json={
                    "model": "embed-a",
                    "object": "list",
                    "data": [
                        {"object": "embedding", "index": i, "embedding": [0.1] * 1024}
                        for i, _ in enumerate(body["input"])
                    ],
                },
            )
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
                        "message": {
                            "role": "assistant",
                            "content": '{"ok": true}'
                            if "synthetic probe" in json.dumps(body)
                            else '{"value": 1}',
                        },
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    monkeypatch.setattr(
        harness.admission.probes,
        "_http_factory",
        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(fake), **kw),
    )
    draft = harness.draft(values, expected=old)

    async def activate():
        evidence = await harness.admission.evidence(draft)
        evidence.append(
            await harness.admission.probes.run(
                candidate_revision=draft.revision_id,
                profile=draft.values.providers[profile_id],
                capability="embedding",
                allow_billable=True,
            )
        )
        harness.service.activate(
            draft.revision_id,
            expected_revision=old,
            probe_ids=[e.probe_id for e in evidence],
            process_env={},
        )

    asyncio.run(activate())
    calls.clear()
    service.transport = httpx.MockTransport(fake)
    planned = [
        module.StepPlan(
            step_id="index",
            task_kind="embedding",
            messages=[],
            embedding_inputs=["a", "b", "c", "d", "e"],
            request_budget=3,
        ),
        module.StepPlan(
            step_id="query",
            task_kind="embedding",
            messages=[],
            embedding_inputs=["query"],
            request_budget=1,
        ),
        *steps(module),
    ]
    operation = service.prepare(
        purpose="generation", idempotency_key=uuid4().hex, steps=planned
    )
    service.confirm(operation.id, preview_digest=operation.preview_digest)

    async def all_steps(runtime, step):
        if step.task_kind == "embedding":
            client = runtime.acquire("embedding").client
            result = (
                await client.embed_query(step.embedding_inputs[0])
                if step.step_id == "query"
                else await client.embed_documents(step.embedding_inputs)
            )
            return Reply(value=len(result))
        return await invoke(runtime, step)

    result = asyncio.run(service.execute(operation.id, invoke=all_steps))
    assert (
        result.state == "succeeded"
        and result.request_budget == result.actual_requests == 5
    )
    assert [len(c["input"]) for c in calls if "input" in c] == [2, 2, 1, 1]
    assert [s["actual_requests"] for s in result.steps] == [3, 1, 1]
    assert service.snapshots.active_lease_count == 0
