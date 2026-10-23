"""A1/A3/B regressions authorized by the current implementation request.

These transports never access a socket and deliberately are not MockTransport:
the production paid-send boundary must execute for every request.
"""

import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from tests.contract.test_learning_operations import (
    Reply,
    confirm,
    execute,
    prepare,
)
from tests.contract.test_learning_operations import (
    env as operation_env,
)


@pytest.fixture
def env(tmp_path):
    yield from operation_env.__wrapped__(tmp_path)


class OfflineTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    async def handle_async_request(self, request):
        self.calls.append(json.loads(request.content))
        return await self.handler(request)


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_protocol_interruption_stays_unknown_without_automatic_resend(env, phase):
    _module, service, _harness, _calls, _sessions = env

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"choices":'
            raise httpx.RemoteProtocolError("synthetic incomplete response")

    async def interrupted(request):
        if phase == "headers":
            raise httpx.RemoteProtocolError("synthetic missing response headers")
        return httpx.Response(200, stream=BrokenStream())

    service.transport = httpx.MockTransport(interrupted)
    operation = prepare(env)
    confirm(env, operation)
    result = execute(env, operation)
    assert result.state == "outcome_unknown"
    assert result.actual_requests == 1
    assert result.steps[0]["requests"][0]["state"] == "outcome_unknown"
    assert execute(env, operation).actual_requests == 1
    assert service.snapshots.active_lease_count == 0


def test_budgeted_chat_uses_real_paid_boundary_offline(env):
    module, service, _harness, _calls, _sessions = env
    original = service.transport
    transport = OfflineTransport(original.handle_async_request)
    service.transport = transport
    service.paid_authorization = lambda view: True
    service.token_counter = lambda model, body: 5
    service.task_model_budgets = {
        "question_generation": module.ModelBudget(
            input_token_limit=20, output_token_limit=7, estimated_cost="0.001"
        )
    }
    operation = prepare(env)
    confirm(env, operation)
    result = execute(env, operation)
    assert result.state == "succeeded", result
    assert transport.calls[0]["max_tokens"] == 7
    assert result.actual_requests == len(transport.calls) == 1
    assert result.steps[0]["task_model_budgets"]["question_generation"]["output_token_limit"] == 7


def test_offline_transport_cannot_bypass_missing_paid_authorization(env):
    module, service, _harness, _calls, _sessions = env
    transport = OfflineTransport(service.transport.handle_async_request)
    service.transport = transport
    operation = prepare(env)
    confirm(env, operation)
    with pytest.raises(module.OperationError, match="PAID_SEND_NOT_AUTHORIZED"):
        execute(env, operation)
    assert transport.calls == []
    assert service.view(operation.id).actual_requests == 0


@pytest.mark.parametrize("task_kind", ["embedding", "rerank", "vision"])
def test_each_capability_uses_its_own_budget_through_real_adapters(env, task_kind):
    from tests.support import runtime_consumer_fakes as fakes

    module, _service, harness, _calls, sessions = env

    def configure(values):
        fakes.add_embedding_and_rerank(values)
        identity, _revision = fakes.add_vision(values, model="deepseek-v4-flash-vision-exp")
        values["providers"][str(identity)]["base_url"] = "https://api.deepseek.com/v1"
        values["providers"][str(identity)]["credential_ref"] = harness.provider_reference.model_dump(mode="json")

    config = fakes.SyntheticConfig(harness, fakes.synthetic_revision(harness, configure))

    async def response(request):
        if task_kind == "embedding":
            return httpx.Response(200, json={"model": "t213-embedding-a", "data": [
                {"object": "embedding", "index": 0, "embedding": [0.1] * 1024}]})
        payload = {"rankings": [{"chunk_id": "chunk-1", "score": 1.0}]} if task_kind == "rerank" else {"value": 1}
        return httpx.Response(200, json={"id": "synthetic", "object": "chat.completion", "created": 0,
            "model": json.loads(request.content)["model"], "choices": [{"index": 0,
                "message": {"role": "assistant", "content": json.dumps(payload)}, "finish_reason": "stop"}]})

    transport = OfflineTransport(response)
    service = module.LearningOperations(sessions=sessions, config=config, transport=transport,
        paid_authorization=lambda view: True, token_counter=lambda model, body: 9,
        task_model_budgets={task_kind: module.ModelBudget(input_token_limit=10,
            output_token_limit=None if task_kind == "embedding" else 11,
            embedding_dimension=1024 if task_kind == "embedding" else None, estimated_cost="0.002")})
    operation = service.prepare(purpose="practice_grading:synthetic", idempotency_key=uuid4().hex,
        steps=[module.StepPlan(step_id="derived", task_kind=task_kind, request_budget=1,
            messages=[], grading_input={"frozen": "synthetic"}, task_budgets={task_kind: 1})])
    service.confirm(operation.id, preview_digest=operation.preview_digest)

    async def send(runtime, step):
        client = runtime.acquire(task_kind).client
        if task_kind == "embedding":
            vector = await client.embed_query("synthetic")
            return Reply(value=len(vector))
        if task_kind == "rerank":
            chunks = await client.rerank_async("synthetic", [fakes.make_chunk()], 1)
            return Reply(value=len(chunks))
        return await client.generate_structured([{"role": "user", "content": "Return JSON"}], Reply)

    try:
        result = asyncio.run(service.execute(operation.id, invoke=send))
        assert result.state == "succeeded", result
        assert result.actual_requests == len(transport.calls) == 1
        if task_kind == "embedding":
            assert "max_tokens" not in transport.calls[0]
        else:
            assert transport.calls[0]["max_tokens"] == 11
        assert service.snapshots.active_lease_count == 0
    finally:
        asyncio.run(service.aclose())


def test_derived_task_cannot_borrow_primary_token_budget(env):
    module, service, _harness, _calls, _sessions = env
    transport = OfflineTransport(service.transport.handle_async_request)
    service.transport = transport
    service.paid_authorization = lambda view: True
    service.token_counter = lambda model, body: 1
    operation = service.prepare(purpose="practice_grading:synthetic", idempotency_key=uuid4().hex,
        steps=[module.StepPlan(step_id="grade", task_kind="subjective_grading", messages=[],
            request_budget=2, grading_input={"frozen": "synthetic"},
            task_budgets={"subjective_grading": 1, "rerank": 1},
            input_token_limit=20, output_token_limit=5, estimated_cost="0.002")])
    confirm(env, operation)
    with pytest.raises(module.OperationError, match="MODEL_BUDGET_REQUIRED"):
        execute(env, operation)
    assert transport.calls == []


def test_production_api_installs_only_exact_finite_manifest_and_can_revoke_it(env):
    from fastapi import FastAPI, Request

    from backend.app.api.learning_operations import service as api_service
    from backend.app.services.learning_paid_validation import (
        PaidValidationManifest,
        PaidValidationPolicy,
    )

    module, service, _harness, _calls, _sessions = env
    transport = OfflineTransport(service.transport.handle_async_request)
    service.transport = transport
    app = FastAPI()
    app.state.learning_operations = service
    request = Request({"type": "http", "app": app})
    budgets = {"question_generation": module.ModelBudget(input_token_limit=20,
        output_token_limit=7, estimated_cost="0.001")}
    proposal = PaidValidationManifest(environment_id=service.config.environment_id,
        task_model_budgets=budgets, max_requests=1, max_input_tokens=20, max_output_tokens=7)
    app.state.learning_paid_validation = PaidValidationPolicy(proposal, lambda model, body: 5)
    assert api_service(request) is service
    operation = prepare(env)
    confirm(env, operation)
    with pytest.raises(module.OperationError, match="PAID_SEND_NOT_AUTHORIZED"):
        execute(env, operation)
    assert transport.calls == []

    grant = PaidValidationPolicy.grant_for(service, operation.id)
    manifest = proposal.model_copy(update={"grants": (grant,)})
    app.state.learning_paid_validation = PaidValidationPolicy(manifest, lambda model, body: 5)
    api_service(request)
    assert execute(env, operation).state == "succeeded"
    assert len(transport.calls) == 1
    assert not app.state.learning_paid_validation.authorize(service, service.view(operation.id))

    unapproved = prepare(env)
    confirm(env, unapproved)
    with pytest.raises(module.OperationError, match="PAID_SEND_NOT_AUTHORIZED"):
        execute(env, unapproved)
    app.state.learning_paid_validation = None
    api_service(request)
    assert service.paid_authorization is None
    assert len(transport.calls) == 1


@pytest.mark.parametrize("change", ["request_limit", "input_limit", "output_limit", "snapshot", "purpose", "loopback"])
def test_manifest_rejects_changed_identity_and_insufficient_aggregate_bounds(env, change):
    from backend.app.services.learning_paid_validation import (
        PaidValidationManifest,
        PaidValidationPolicy,
    )

    module, service, _harness, _calls, _sessions = env
    service.task_model_budgets = {"question_generation": module.ModelBudget(
        input_token_limit=20, output_token_limit=7, estimated_cost="0.001")}
    operation = prepare(env, count=2)
    grant = PaidValidationPolicy.grant_for(service, operation.id)
    if change == "snapshot":
        grant = grant.model_copy(update={"snapshot_digest": "0" * 64})
    if change == "purpose":
        grant = grant.model_copy(update={"purpose": "different"})
    manifest = PaidValidationManifest(environment_id=service.config.environment_id,
        grants=(grant,), task_model_budgets=service.task_model_budgets,
        max_requests=1 if change == "request_limit" else 2,
        max_input_tokens=39 if change == "input_limit" else 40,
        max_output_tokens=13 if change == "output_limit" else 14,
        loopback_only=change == "loopback")
    policy = PaidValidationPolicy(manifest, lambda model, body: 5)
    assert not policy.authorize(service, operation)


def test_input_token_overflow_is_zero_send_even_after_preview(env):
    module, service, _harness, _calls, _sessions = env
    transport = OfflineTransport(service.transport.handle_async_request)
    service.transport = transport
    service.paid_authorization = lambda view: True
    service.token_counter = lambda model, body: 21
    service.task_model_budgets = {"question_generation": module.ModelBudget(
        input_token_limit=20, output_token_limit=7, estimated_cost="0.001")}
    operation = prepare(env)
    confirm(env, operation)
    result = execute(env, operation)
    assert result.state == "failed"
    assert result.steps[0]["error_code"] == "MODEL_BUDGET_REQUIRED"
    assert transport.calls == [] and result.actual_requests == 0


@pytest.mark.parametrize("capability", ["json_object", "vision", "embedding", "llm_rerank"])
@pytest.mark.parametrize("response_kind", ["valid", "protocol", "malformed"])
def test_finite_probe_grants_apply_capability_budget_and_survive_restart(env, tmp_path, capability, response_kind):
    from backend.app.schemas.settings import ProviderProfile
    from backend.app.services.learning_paid_validation import (
        PaidProbeGrant,
        PaidValidationManifest,
        PaidValidationPolicy,
    )
    from backend.app.services.settings.probe_service import (
        ProbeRequestError,
        ProbeService,
    )

    module, _service, harness, _calls, _sessions = env
    raw = next(iter(harness.service.active_revision().values.providers.values())).model_dump(mode="python")
    kind = {"json_object": "llm", "vision": "vision", "embedding": "embedding", "llm_rerank": "rerank"}[capability]
    raw["declared_capabilities"] = [capability]
    raw["parameters"] = {}
    if capability == "vision":
        raw.update(adapter_id="deepseek", model_id="deepseek-v4-flash-vision-exp",
            base_url="https://api.deepseek.com/v1")
    elif capability == "embedding":
        raw["parameters"] = {"dimension": 1024, "batch_size": 2}
    elif capability == "llm_rerank":
        raw["parameters"] = {"max_candidates": 2, "timeout_seconds": 5.0}
    registry = harness.service.provider_registry
    profile = ProviderProfile.model_validate(
        registry.validate_profile(raw, kind=kind).model_dump(mode="python", exclude={"verified_capabilities"}))
    budget = module.ModelBudget(input_token_limit=20,
        output_token_limit=None if capability == "embedding" else 7,
        embedding_dimension=1024 if capability == "embedding" else None, estimated_cost="0.001")
    candidate = uuid4()
    grant = PaidProbeGrant(grant_id=uuid4(), candidate_revision=candidate,
        capability=capability, profile=profile, model_budget=budget)
    manifest = PaidValidationManifest(environment_id=harness.environment_id,
        probe_grants=(grant,), task_model_budgets={}, max_requests=1,
        max_input_tokens=20, max_output_tokens=budget.output_token_limit or 0)
    policy = PaidValidationPolicy(manifest, lambda model, body: 5, probe_state_root=tmp_path)

    async def response(request):
        if response_kind == "protocol":
            raise httpx.RemoteProtocolError("synthetic interrupted response")
        if response_kind == "malformed":
            return httpx.Response(200, content=b"not JSON")
        if capability == "embedding":
            return httpx.Response(200, json={"model": profile.model_id, "data": [
                {"object": "embedding", "index": 0, "embedding": [0.1] * 1024}]})
        content = {"rankings": [{"chunk_id": "probe-a", "score": 1.0},
            {"chunk_id": "probe-b", "score": 0.1}]} if capability == "llm_rerank" else {"ok": True}
        return httpx.Response(200, json={"id": "synthetic", "object": "chat.completion", "created": 0,
            "model": profile.model_id, "choices": [{"index": 0, "message": {
                "role": "assistant", "content": json.dumps(content)}, "finish_reason": "stop"}]})

    transport = OfflineTransport(response)

    def make():
        return ProbeService(environment_id=harness.environment_id, registry=registry,
            credential_store=harness.store, validation_policy=policy,
            http_client_factory=lambda **options: httpx.AsyncClient(transport=transport, **options))

    async def send():
        service = make()
        try:
            # Existing single-probe consent remains required, even with a grant.
            with pytest.raises(ProbeRequestError) as rejected:
                await service.run(candidate_revision=candidate, profile=profile, capability=capability)
            assert rejected.value.code == "BILLABLE_CONFIRMATION_REQUIRED"
            return await service.run(candidate_revision=candidate, profile=profile,
                capability=capability, allow_billable=True)
        finally:
            await service.aclose()

    result = asyncio.run(send())
    assert len(transport.calls) == 1
    assert result.external_requests == 1
    assert (result.status == "succeeded") == (response_kind == "valid")
    assert transport.calls[0].get("max_tokens") == budget.output_token_limit
    facts = json.loads((tmp_path / (str(grant.grant_id) + ".json")).read_text(encoding="utf-8"))
    assert facts["actual_requests"] == 1
    assert facts["state"] == ("outcome_unknown" if response_kind == "protocol" else "response_received")
    # A newly created policy/service cannot spend the same grant a second time.
    policy = PaidValidationPolicy(manifest, lambda model, body: 5, probe_state_root=tmp_path)
    again = asyncio.run(send())
    assert again.status == "failed" and len(transport.calls) == 1


def test_probe_and_business_plan_share_the_same_aggregate_grant(env, tmp_path):
    from backend.app.services.learning_paid_validation import (
        PaidProbeGrant,
        PaidValidationManifest,
        PaidValidationPolicy,
    )
    from backend.app.services.settings.probe_service import ProbeRequestError

    module, service, harness, _calls, _sessions = env
    budget = module.ModelBudget(input_token_limit=20, output_token_limit=7, estimated_cost="0.001")
    service.task_model_budgets = {"question_generation": budget}
    operation = prepare(env)
    profile = next(iter(harness.service.active_revision().values.providers.values()))
    probe = PaidProbeGrant(grant_id=uuid4(), candidate_revision=uuid4(),
        capability="json_object", profile=profile, model_budget=budget)
    manifest = PaidValidationManifest(environment_id=harness.environment_id,
        grants=(PaidValidationPolicy.grant_for(service, operation.id),), probe_grants=(probe,),
        task_model_budgets=service.task_model_budgets, max_requests=1, max_input_tokens=40, max_output_tokens=14)
    policy = PaidValidationPolicy(manifest, lambda model, body: 5, probe_state_root=tmp_path)
    assert not policy.authorize(service, operation)
    with pytest.raises(ProbeRequestError) as rejected:
        policy.probe_guard(environment_id=harness.environment_id, candidate_revision=probe.candidate_revision,
            profile=profile, capability="json_object")
    assert rejected.value.code == "VALIDATION_NOT_AUTHORIZED"
    assert not list(tmp_path.glob(str(probe.grant_id) + "*"))


def test_probe_grant_concurrent_reservation_is_single_attempt(env, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from backend.app.services.learning_paid_validation import (
        PaidProbeGrant,
        PaidValidationManifest,
        PaidValidationPolicy,
    )
    from backend.app.services.settings.probe_service import ProbeRequestError

    module, _service, harness, _calls, _sessions = env
    profile = next(iter(harness.service.active_revision().values.providers.values()))
    budget = module.ModelBudget(input_token_limit=20, output_token_limit=7, estimated_cost="0.001")
    grant = PaidProbeGrant(grant_id=uuid4(), candidate_revision=uuid4(),
        capability="json_object", profile=profile, model_budget=budget)
    manifest = PaidValidationManifest(environment_id=harness.environment_id,
        probe_grants=(grant,), task_model_budgets={}, max_requests=1,
        max_input_tokens=20, max_output_tokens=7)
    barrier = Barrier(2)

    def reserve():
        policy = PaidValidationPolicy(manifest, lambda model, body: 5, probe_state_root=tmp_path)
        guard = policy.probe_guard(environment_id=harness.environment_id,
            candidate_revision=grant.candidate_revision, profile=profile, capability="json_object")
        barrier.wait(timeout=5)
        try:
            guard.reserve({"model": profile.model_id, "max_tokens": 7})
            return "reserved"
        except ProbeRequestError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: reserve(), range(2)))
    assert sorted(results) == ["VALIDATION_PROBE_ALREADY_ATTEMPTED", "reserved"]
    facts = json.loads((tmp_path / (str(grant.grant_id) + ".json")).read_text(encoding="utf-8"))
    assert facts["state"] == "outcome_unknown" and facts["actual_requests"] == 1


# ---------------------------------------------------------------------------
# T083：图片输入按“固定预留 + 文本 token”计量，不把 base64 折算成文本预算
# ---------------------------------------------------------------------------


def _frozen_png_request_body():
    import base64

    from backend.app.services import learning_operations as module
    from tests.support.personal_ai_acceptance_batch2 import V1_PNG

    payload = base64.b64encode(V1_PNG.read_bytes()).decode("ascii")
    body = {
        "model": "deepseek-flash",
        "max_tokens": 5000,
        "messages": [
            {"role": "system", "content": "synthetic system prompt"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "根据题图求一次函数表达式，说明依据。"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64," + payload},
                    },
                ],
            },
        ],
    }
    return module, body, payload


def _harness_style_counter(model, value):
    """镜像部署口径：bytes/2 上限，另加每张图的固定项。"""

    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    images = 0
    for message in value.get("messages") or []:
        content = message.get("content")
        if isinstance(content, list):
            images += sum(
                1
                for part in content
                if isinstance(part, dict) and part.get("type") == "image_url"
            )
    return max(len(raw), (len(raw.encode("utf-8")) + 1) // 2) + 1024 * images


def test_frozen_png_fits_the_authorized_six_thousand_token_budget():
    module, body, _payload = _frozen_png_request_body()
    assert _harness_style_counter("deepseek-flash", body) > 6_000

    count = module.budget_input_tokens(_harness_style_counter, "deepseek-flash", body)

    assert count <= 6_000
    text_only_body = {
        **body,
        "messages": [
            body["messages"][0],
            {**body["messages"][1], "content": [
                {"type": "text", "text": "根据题图求一次函数表达式，说明依据。"}
            ]},
        ],
    }
    assert count == _harness_style_counter("deepseek-flash", text_only_body) + 1_500
    assert count == module.IMAGE_INPUT_TOKEN_RESERVATION + _harness_style_counter(
        "deepseek-flash", text_only_body
    )


def test_text_only_budget_count_is_unchanged():
    from backend.app.services import learning_operations as module

    body = {"model": "synthetic", "messages": [{"role": "user", "content": "纯文本请求"}]}
    for value in (0, 5, 17):
        assert (
            module.budget_input_tokens(lambda _model, _body, value=value: value, "synthetic", body)
            == value
        )


def test_image_payload_never_reaches_the_counter_and_body_is_unchanged():
    module, body, payload = _frozen_png_request_body()
    before = json.dumps(body, sort_keys=True)
    seen = []

    def counter(model, value):
        seen.append(value)
        return 3

    count = module.budget_input_tokens(counter, "deepseek-flash", body)

    assert count == 3 + module.IMAGE_INPUT_TOKEN_RESERVATION
    assert len(seen) == 1
    assert payload[:64] not in json.dumps(seen[0], ensure_ascii=False)
    assert json.dumps(body, sort_keys=True) == before
