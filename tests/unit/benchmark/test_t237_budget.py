"""Acceptance budget safety uses Fake HTTP only; never loads credentials."""
import asyncio
import json
import multiprocessing
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from benchmark.t237.budget import BudgetError, BudgetLedger, BudgetTransport

URL = "https://api.deepseek.com/chat/completions"
PHASES = ("text", "json_object", "grading")


def body(**changes):
    return {"model": "deepseek-flash", "messages": [{"role": "user", "content": "synthetic"}],
            "max_tokens": 2048, "response_format": {"type": "json_object"}, **changes}


def ledger(tmp_path):
    path = tmp_path / "budget.sqlite3"
    BudgetLedger.initialize(path)
    return BudgetLedger(path)


def worker_reserve(path, output):
    try:
        BudgetLedger(path).reserve("text", 20)
        output.put("reserved")
    except BudgetError:
        output.put("blocked")


def test_global_counter_survives_reopen_and_rejects_fourth(tmp_path):
    state = ledger(tmp_path)
    for phase in PHASES:
        state = BudgetLedger(state.path)
        state.reserve(phase, 20)
        state.received(phase, {"http_status": 200})
        state.confirm(phase)
    with pytest.raises(BudgetError):
        BudgetLedger(state.path).reserve("grading", 20)
    assert len(state.rows()) == 3
    with pytest.raises(FileExistsError):
        BudgetLedger.initialize(state.path)


def test_missing_ledger_is_not_recreated(tmp_path):
    with pytest.raises(BudgetError):
        BudgetLedger(tmp_path / "missing").reserve("text", 1)
    assert not (tmp_path / "missing").exists()


def test_concurrent_processes_share_reservation(tmp_path):
    state = ledger(tmp_path)
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    processes = [context.Process(target=worker_reserve, args=(state.path, output)) for _ in range(3)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(15)
        assert process.exitcode == 0
        process.close()
    assert sorted(output.get(timeout=2) for _ in processes) == ["blocked", "blocked", "reserved"]
    output.close()
    output.join_thread()
    assert len(state.rows()) == 1


def test_concurrent_threads_and_unconfirmed_request_fail_closed(tmp_path):
    state = ledger(tmp_path)
    def reserve():
        try:
            state.reserve("text", 1)
            return True
        except BudgetError:
            return False
    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(lambda _: reserve(), range(6))) == 1
    with pytest.raises(BudgetError):
        BudgetLedger(state.path).reserve("json_object", 1)


@pytest.mark.parametrize("change", [
    {"model": "other"}, {"max_tokens": 2049}, {"max_tokens": None},
    {"stream": True}, {"tools": []}, {"n": 2},
    {"messages": [{"role": "user", "content": [{"type": "image_url"}]}]},
    {"response_format": {"type": "text"}}, {"temperature": float("nan")},
])
def test_invalid_parameters_rejected_before_send(tmp_path, change):
    state = ledger(tmp_path)
    calls = []
    async def run():
        transport = BudgetTransport(state, "text", httpx.MockTransport(lambda r: calls.append(r)),
                                    count_input=lambda _: 10)
        async with httpx.AsyncClient(transport=transport, timeout=30) as client:
            with pytest.raises((BudgetError, ValueError)):
                await client.post(URL, json=body(**change))
    asyncio.run(run())
    assert not calls and not state.rows()


@pytest.mark.parametrize("url,timeout,tokens", [
    ("https://attacker.invalid/chat/completions", 30, 1),
    (URL + "?key=private", 30, 1), (URL, 31, 1), (URL, None, 1),
    (URL, 30, 8193), (URL, 30, -1), (URL, 30, True),
])
def test_endpoint_timeout_and_token_caps(tmp_path, url, timeout, tokens):
    state = ledger(tmp_path)
    calls = []
    async def run():
        async with httpx.AsyncClient(transport=BudgetTransport(
            state, "text", httpx.MockTransport(lambda r: calls.append(r)),
            count_input=lambda _: tokens), timeout=timeout) as client:
            with pytest.raises(BudgetError):
                await client.post(url, json=body())
    asyncio.run(run())
    assert not calls and not state.rows()


@pytest.mark.parametrize("failure", ["http", "timeout", "cancel", "disconnect", "invalid_json"])
def test_failure_is_spent_no_retry_and_owned_transport_closes(tmp_path, failure):
    state = ledger(tmp_path)
    calls = []
    closed = []
    class Fake(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            calls.append(1)
            if failure == "timeout":
                raise httpx.ReadTimeout("synthetic private")
            if failure == "cancel":
                raise asyncio.CancelledError()
            if failure == "disconnect":
                raise httpx.ReadError("synthetic private")
            return httpx.Response(500 if failure == "http" else 200, content=b"private")
        async def aclose(self):
            closed.append(1)
    async def run():
        async with httpx.AsyncClient(transport=BudgetTransport(
            state, "text", Fake(), count_input=lambda _: 10), timeout=30) as client:
            try:
                await client.post(URL, json=body())
            except (httpx.HTTPError, asyncio.CancelledError, BudgetError):
                pass
            with pytest.raises(BudgetError):
                await client.post(URL, json=body())
    asyncio.run(run())
    assert calls == [1] and closed == [1] and len(state.rows()) == 1
    with pytest.raises(BudgetError):
        state.confirm("text")
    assert "private" not in json.dumps(state.rows())


def test_success_only_persists_allowlisted_metadata_and_fourth_never_sends(tmp_path):
    state = ledger(tmp_path)
    calls = []
    async def send(request):
        calls.append(1)
        return httpx.Response(200, json={"model": "DeepSeek-V4.1-Flash-1017",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "choices": [{"message": {"content": "private-result"}}]})
    async def run():
        for phase in PHASES:
            async with httpx.AsyncClient(transport=BudgetTransport(
                BudgetLedger(state.path), phase, httpx.MockTransport(send),
                count_input=lambda _: 10), timeout=30) as client:
                response = await client.post(URL, json=body())
                assert response.status_code == 200
                state.confirm(phase)
        async with httpx.AsyncClient(transport=BudgetTransport(
            state, "grading", httpx.MockTransport(send), count_input=lambda _: 10), timeout=30) as client:
            with pytest.raises(BudgetError):
                await client.post(URL, json=body())
    asyncio.run(run())
    assert len(calls) == 3
    encoded = json.dumps(state.rows())
    assert "private-result" not in encoded and "synthetic" not in encoded
    assert state.rows()[0]["metadata"]["usage"]["total_tokens"] == 15


def test_absent_token_counter_cannot_send(tmp_path):
    calls = []
    async def run():
        async with httpx.AsyncClient(transport=BudgetTransport(
            ledger(tmp_path), "text", httpx.MockTransport(lambda r: calls.append(r))), timeout=30) as client:
            with pytest.raises(BudgetError, match="TOKEN_COUNTER_REQUIRED"):
                await client.post(URL, json=body())
    asyncio.run(run())
    assert not calls

@pytest.mark.parametrize("outcome", ["ok", "invalid_json", "http_error", "timeout"])
def test_production_registry_sdk_and_retry_policy_use_budget_once(tmp_path, outcome):
    from uuid import uuid4

    from pydantic import BaseModel, SecretStr

    from backend.app.core.retry_policy import (
        ProviderCallError,
        ProviderExecutionError,
        RetryPolicy,
    )
    from backend.app.services.settings.provider_registry import ProviderRegistry
    state = ledger(tmp_path)
    sent = []
    class Answer(BaseModel):
        ok: bool
    async def handler(request):
        sent.append(json.loads(request.content))
        if outcome == "timeout":
            raise httpx.ReadTimeout("synthetic-private")
        return httpx.Response(503 if outcome == "http_error" else 200, json={
            "id": "synthetic", "object": "chat.completion", "created": 1,
            "model": "DeepSeek-V4.1-Flash-1017",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": '{"ok":true}' if outcome == "ok" else "invalid"}}]})
    async def run():
        transport = BudgetTransport(state, "text", httpx.MockTransport(handler), count_input=lambda _: 10)
        async with httpx.AsyncClient(transport=transport, timeout=30, trust_env=False) as client:
            provider = ProviderRegistry().create({
                "profile_id": uuid4(), "profile_revision": uuid4(), "display_name": "synthetic",
                "adapter_id": "openai_compatible", "model_id": "deepseek-flash",
                "base_url": "https://api.deepseek.com", "parameters": {"max_tokens": 2048},
                "declared_capabilities": ("text", "json_object"), "enabled": True},
                api_key=SecretStr("synthetic-not-real"), http_client=client,
                retry_policy=RetryPolicy(max_retries=0), timeout=30)
            try:
                if outcome == "ok":
                    result = await provider.generate_structured([{"role": "user", "content": "JSON synthetic"}], Answer)
                    assert result.ok is True
                    state.confirm("text")
                else:
                    with pytest.raises((ProviderCallError, ProviderExecutionError)):
                        await provider.generate_structured([{"role": "user", "content": "JSON synthetic"}], Answer)
            finally:
                await provider.aclose()
                assert not client.is_closed  # borrowed client remains caller-owned
        assert client.is_closed and transport.closed
    asyncio.run(run())
    assert len(sent) == 1 and sent[0]["max_tokens"] == 2048
    assert sent[0]["model"] == "deepseek-flash"
    assert len(state.rows()) == 1


def test_total_network_deadline_consumes_slot_and_closes(tmp_path):
    state = ledger(tmp_path)
    observed = []
    async def slow(request):
        observed.append("sent")
        try:
            await asyncio.sleep(10)
        finally:
            observed.append("cancelled")
    async def run():
        async with httpx.AsyncClient(transport=BudgetTransport(
            state, "text", httpx.MockTransport(slow), count_input=lambda _: 1), timeout=0.01) as client:
            with pytest.raises(TimeoutError):
                await client.post(URL, json=body())
        assert client.is_closed
    asyncio.run(run())
    assert observed == ["sent", "cancelled"]
    assert state.rows()[0]["state"] == "failed"

def test_offline_counter_rejects_unverified_assets(tmp_path):
    from benchmark.t237.token_budget import OfflineInputBound
    with pytest.raises(BudgetError, match="TOKENIZER_ASSET_UNVERIFIED"):
        OfflineInputBound(tmp_path)
    (tmp_path / "tokenizer.json").write_text("{}")
    with pytest.raises(BudgetError, match="TOKENIZER_ASSET_UNVERIFIED"):
        OfflineInputBound(tmp_path)


def test_conservative_bound_covers_unicode_and_template_framing():
    from benchmark.t237.token_budget import OfflineInputBound
    class Template:
        def apply_chat_template(self, messages, **kwargs):
            assert kwargs == {"tokenize": False, "add_generation_prompt": True}
            return "<bos><user>" + messages[0]["content"] + "<assistant>"
        def encode(self, rendered, **kwargs):
            return [1] * len(rendered)
    counter = OfflineInputBound.__new__(OfflineInputBound)
    counter._tokenizer = Template()
    request = body()
    request["messages"][0]["content"] = "中文😀"
    assert counter(request) == len("<bos><user>中文😀<assistant>".encode()) + 256


def test_oversized_real_text_bound_is_rejected_before_reservation(tmp_path):
    from benchmark.t237.token_budget import OfflineInputBound
    class Template:
        def apply_chat_template(self, messages, **kwargs):
            return messages[0]["content"]
        def encode(self, rendered, **kwargs):
            return [1]
    counter = OfflineInputBound.__new__(OfflineInputBound)
    counter._tokenizer = Template()
    state = ledger(tmp_path)
    sent = []
    async def run():
        payload = body()
        payload["messages"][0]["content"] = "界" * 3000
        async with httpx.AsyncClient(transport=BudgetTransport(
            state, "text", httpx.MockTransport(lambda r: sent.append(r)),
            count_input=counter), timeout=30) as client:
            with pytest.raises(BudgetError, match="INPUT_TOKEN_LIMIT"):
                await client.post(URL, json=payload)
    asyncio.run(run())
    assert sent == [] and state.rows() == []
