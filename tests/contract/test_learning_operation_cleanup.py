"""Terminal request facts survive provider/SDK/transport cleanup failures."""
import asyncio
import logging
from uuid import uuid4

import httpx
import pytest

from tests.contract.test_learning_operations import (
    Reply,
    confirm,
    invoke,
    prepare,
)
from tests.contract.test_learning_operations import (
    env as operation_env,
)
from tests.contract.test_learning_paid_budgets import OfflineTransport


@pytest.fixture
def env(tmp_path):
    yield from operation_env.__wrapped__(tmp_path)


@pytest.fixture
def cleanup_log(caplog):
    from backend.app.services.learning_operations import logger

    disabled, level = logger.disabled, logger.level
    try:
        logger.disabled = False
        with caplog.at_level(logging.ERROR, logger=logger.name):
            yield caplog
    finally:
        logger.disabled = disabled
        logger.setLevel(level)


@pytest.mark.parametrize("request_outcome", ["succeeded", "failed", "outcome_unknown"])
def test_cleanup_failures_keep_terminal_facts_close_all_clients_and_do_not_resend(env, request_outcome, cleanup_log):
    module, service, _harness, calls, _sessions = env
    original = service.transport
    cleanup = []

    async def respond(request):
        if request_outcome == "outcome_unknown":
            calls.append("interrupted")
            raise httpx.RemoteProtocolError("synthetic disconnected request")
        if request_outcome == "failed":
            calls.append("known response")
            return httpx.Response(429, json={"error": {"message": "synthetic"}})
        return await original.handle_async_request(request)

    transport = OfflineTransport(respond)
    service.transport = transport
    service.paid_authorization = lambda view: True
    service.token_counter = lambda model, body: 5
    service.task_model_budgets = {"question_generation": module.ModelBudget(
        input_token_limit=20, output_token_limit=7, estimated_cost="0")}
    operation = prepare(env)
    confirm(env, operation)

    class Client:
        def __init__(self, name, fail=False):
            self.name, self.fail = name, fail

        async def aclose(self):
            cleanup.append(self.name)
            if self.fail:
                raise OSError("synthetic original cleanup " + self.name)

    async def send(runtime, step):
        # A provider lease and independent retained SDK/HTTP clients are all
        # exercised; failures occur only after the real adapter handled a send.
        lease = runtime.acquire(step.task_kind)
        original_close = lease.client.aclose

        async def failed_provider_close():
            await original_close()
            cleanup.append("provider")
            raise OSError("synthetic original cleanup provider")

        lease.client.aclose = failed_provider_close
        call = service._current.get()
        call.clients.extend((Client("last"), Client("independent", fail=True)))
        return await lease.client.generate_structured(step.messages, Reply,
            model=runtime.requested_model(step.task_kind))

    async def run():
        result = await service.execute(operation.id, invoke=send)
        with pytest.raises(LookupError):
            service._current.get()
        return result

    result = asyncio.run(run())
    assert result.state == request_outcome and result.actual_requests == len(transport.calls) == 1
    step = result.steps[0]
    assert step["state"] == request_outcome
    assert step["cleanup_error_code"] == "RESOURCE_RELEASE_FAILED"
    assert step["requests"][0]["state"] == ("outcome_unknown" if request_outcome == "outcome_unknown" else "response_received")
    assert (step["result"] == {"value": 1}) if request_outcome == "succeeded" else step["result"] is None
    if request_outcome != "succeeded":
        assert step["error_code"] != "RESOURCE_RELEASE_FAILED"
    assert cleanup == ["provider", "independent", "last"]
    assert service.snapshots.active_lease_count == 0
    assert "synthetic original cleanup provider" in cleanup_log.text
    assert "synthetic original cleanup independent" in cleanup_log.text
    repeated = asyncio.run(service.execute(operation.id, invoke=send))
    assert repeated.state == request_outcome and len(transport.calls) == 1
    if request_outcome == "succeeded":
        with pytest.raises(module.OperationError, match="RETRY_NOT_AVAILABLE"):
            service.retry(operation.id, idempotency_key=uuid4().hex)


def test_owned_transport_close_error_keeps_received_response_and_final_state(env, monkeypatch):
    module, service, _harness, calls, _sessions = env
    original = service.transport

    class OwnedTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return await original.handle_async_request(request)

        async def aclose(self):
            raise OSError("synthetic owned transport close")

    monkeypatch.setattr(module.httpx, "AsyncHTTPTransport", lambda **_kwargs: OwnedTransport())
    service.transport = None
    service.paid_authorization = lambda view: True
    service.token_counter = lambda model, body: 5
    service.task_model_budgets = {"question_generation": module.ModelBudget(
        input_token_limit=20, output_token_limit=7, estimated_cost="0")}
    operation = prepare(env)
    confirm(env, operation)
    result = asyncio.run(service.execute(operation.id, invoke=invoke))
    assert result.state == "succeeded" and result.actual_requests == len(calls) == 1
    assert result.steps[0]["result"] == {"value": 1}
    assert result.steps[0]["requests"][0]["state"] == "response_received"
    assert result.steps[0]["cleanup_error_code"] == "RESOURCE_RELEASE_FAILED"
    assert service.snapshots.active_lease_count == 0


@pytest.mark.parametrize("interrupted", [False, True])
def test_response_stream_close_error_does_not_change_response_completeness(env, interrupted):
    module, service, _harness, _calls, _sessions = env
    original = service.transport

    class Stream(httpx.AsyncByteStream):
        def __init__(self, content):
            self.content = content

        async def __aiter__(self):
            yield self.content
            if interrupted:
                raise httpx.RemoteProtocolError("synthetic incomplete stream")

        async def aclose(self):
            raise OSError("synthetic response stream close")

    async def respond(request):
        response = await original.handle_async_request(request)
        content = response.content if not interrupted else b'{"choices":'
        return httpx.Response(200, headers={"Content-Type": "application/json"}, stream=Stream(content))

    transport = OfflineTransport(respond)
    service.transport = transport
    service.paid_authorization = lambda view: True
    service.token_counter = lambda model, body: 5
    service.task_model_budgets = {"question_generation": module.ModelBudget(
        input_token_limit=20, output_token_limit=7, estimated_cost="0")}
    operation = prepare(env)
    confirm(env, operation)
    result = asyncio.run(service.execute(operation.id, invoke=invoke))
    assert result.state == ("outcome_unknown" if interrupted else "succeeded")
    assert result.actual_requests == len(transport.calls) == 1
    assert result.steps[0]["requests"][0]["state"] == ("outcome_unknown" if interrupted else "response_received")
    assert result.steps[0]["cleanup_error_code"] == "RESOURCE_RELEASE_FAILED"
    if not interrupted:
        assert result.steps[0]["result"] == {"value": 1}


def test_operation_owner_closes_transport_and_engine_after_snapshot_close_failure(env):
    from types import SimpleNamespace

    service = env[1]
    calls = []
    failure = OSError("synthetic first owner close failure")

    class Snapshots:
        async def aclose(self):
            calls.append("snapshots")
            raise failure

    class Transport:
        async def aclose(self):
            calls.append("transport")
            raise ValueError("synthetic second owner close failure")

    original_snapshots, original_transport = service.snapshots, service.transport
    service.snapshots, service.transport = Snapshots(), Transport()
    service.owned_engine = SimpleNamespace(dispose=lambda: calls.append("engine"))
    try:
        with pytest.raises(OSError) as error:
            asyncio.run(service.aclose())
        assert error.value is failure
        assert calls == ["snapshots", "transport", "engine"]
    finally:
        service.snapshots, service.transport, service.owned_engine = original_snapshots, original_transport, None
