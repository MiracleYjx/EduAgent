"""TCR-011: adversarial Probe facts, never real services or paid models."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from threading import Event, get_ident
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from tests.support.provider_probe_fakes import (
    NOW,
    assert_one_closed_request,
    assert_safe_result,
    assess_evidence,
    classify_error,
    make_probe,
    probe_sandbox,
    result_code,
    run_probe,
    successful_evidence,
    target_for,
    view,
)

pytestmark = pytest.mark.usefixtures(probe_sandbox.__name__)


@pytest.mark.parametrize("status,body,expected", [
    (403, {"error": {"code": "invalid_api_key"}}, "KEY_INVALID"),
    (403, {"error": {"code": "permission_denied"}}, "UNKNOWN_PROVIDER_ERROR"),
    (404, {"error": {"message": "model_not_found"}}, "UNKNOWN_PROVIDER_ERROR"),
    (404, {"error": {"code": "model_not_found"}}, "MODEL_NOT_FOUND"),
    (400, {"error": {"code": "unsupported_response_format"}}, "CAPABILITY_UNSUPPORTED"),
    (502, {"error": {"message": "invalid_api_key DNS timeout"}}, "UNKNOWN_PROVIDER_ERROR"),
])
def test_http_classification_never_searches_message_text(status, body, expected, probe_sandbox):
    request = httpx.Request("POST", "https://provider.invalid/v1")
    response = httpx.Response(status, json=body, request=request)
    error = httpx.HTTPStatusError(probe_sandbox.markers["body"], request=request, response=response)
    result = classify_error(error)
    assert result_code(result) == expected
    assert_safe_result(probe_sandbox, result)


@pytest.mark.parametrize("status,body,expected", [
    (401, {"error": {"code": "invalid_api_key"}}, "KEY_INVALID"),
    (403, {"error": {"code": "insufficient_quota"}}, "UNKNOWN_PROVIDER_ERROR"),
    (404, {"error": {"code": "model_not_found"}}, "MODEL_NOT_FOUND"),
    (404, {"error": {"message": "model missing"}}, "UNKNOWN_PROVIDER_ERROR"),
    (400, {"error": {"code": "unsupported_response_format"}}, "CAPABILITY_UNSUPPORTED"),
])
@pytest.mark.parametrize("capability", ["json_object", "embedding"])
def test_real_adapter_wrappers_preserve_only_confirmed_error_facts(status, body, expected, capability, probe_sandbox):
    probe_sandbox.scenario.status = status
    probe_sandbox.scenario.json_body = body
    profile = {**probe_sandbox.profile, "declared_capabilities": (capability,)}
    result = asyncio.run(run_probe(make_probe(probe_sandbox), probe_sandbox, profile=profile, capability=capability))
    assert result_code(result) == expected
    assert_one_closed_request(probe_sandbox)
    assert_safe_result(probe_sandbox, result)


@pytest.mark.parametrize("base_url,model", [
    ("https://provider.invalid/v1", "deepseek-v4-flash"),
    ("https://api.deepseek.com/v1", "t207-model"),
])
def test_vision_model_and_endpoint_gate_is_not_weakened_for_probe(base_url, model, probe_sandbox):
    profile = {**probe_sandbox.profile, "adapter_id": "deepseek", "model_id": model,
               "base_url": base_url, "declared_capabilities": ("vision",)}
    result = asyncio.run(run_probe(make_probe(probe_sandbox), probe_sandbox, profile=profile, capability="vision"))
    assert result_code(result) == "CAPABILITY_UNSUPPORTED"
    assert view(result)["external_requests"] == 0
    assert probe_sandbox.scenario.requests == []
    assert all(client.is_closed for client in probe_sandbox.http.clients)


def test_wrong_environment_reference_is_rejected_before_secret_read(probe_sandbox):
    ref = probe_sandbox.profile["credential_ref"].model_copy(update={"environment_id": uuid4()})
    profile = {**probe_sandbox.profile, "credential_ref": ref}
    with pytest.raises(Exception) as caught:
        asyncio.run(run_probe(make_probe(probe_sandbox), probe_sandbox, profile=profile))
    assert getattr(caught.value, "status_code", None) == 422
    assert probe_sandbox.credentials.calls == []
    assert probe_sandbox.http.clients == []
    assert_safe_result(probe_sandbox, classify_error(caught.value))


def test_unavailable_credential_is_not_guessed_invalid_key(probe_sandbox):
    from backend.app.services.settings.credential_store import CredentialStoreError

    def unavailable(*_args):
        raise CredentialStoreError("CREDENTIAL_UNAVAILABLE", stage="get")

    probe_sandbox.credentials.get = unavailable
    result = asyncio.run(run_probe(make_probe(probe_sandbox), probe_sandbox))
    assert result_code(result) == "CREDENTIAL_UNAVAILABLE"
    assert view(result)["external_requests"] == 0
    assert probe_sandbox.http.clients == []
    assert_safe_result(probe_sandbox, result)


def test_empty_credential_is_missing_not_an_anonymous_fallback(probe_sandbox):
    ref = probe_sandbox.profile["credential_ref"]
    probe_sandbox.credentials.values[(ref.credential_id, ref.version)] = SecretStr("")
    result = asyncio.run(run_probe(make_probe(probe_sandbox), probe_sandbox))
    assert result_code(result) == "KEY_MISSING"
    assert probe_sandbox.http.clients == []


def test_usage_allowlist_does_not_return_sdk_extensions(probe_sandbox):
    probe_sandbox.scenario.json_body = {
        "id": "t209", "object": "chat.completion", "created": 1, "model": "t207-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": '{"ok":true}'}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5,
                  "secret_extension": probe_sandbox.markers["body"]},
    }
    result = asyncio.run(run_probe(make_probe(probe_sandbox), probe_sandbox))
    assert result_code(result) == "SUCCESS"
    assert view(result)["usage"] == {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}
    assert_safe_result(probe_sandbox, result)


@pytest.mark.parametrize("field", ["profile_id", "model_id", "probe_id", "tested_at", "expires_at"])
def test_incomplete_evidence_cannot_authorize_a_candidate(field, probe_sandbox):
    service = make_probe(probe_sandbox)
    target = target_for(probe_sandbox.candidate_revision, probe_sandbox.profile, "json_object")
    evidence = successful_evidence(target)
    evidence.pop(field)
    assert assess_evidence(service, target, evidence)["valid"] is False
    assert probe_sandbox.scenario.requests == []


def test_future_success_and_forged_expiration_do_not_extend_acceptance(probe_sandbox):
    service = make_probe(probe_sandbox)
    target = target_for(probe_sandbox.candidate_revision, probe_sandbox.profile, "json_object")
    future = {**successful_evidence(target), "tested_at": NOW + timedelta(seconds=1)}
    assert assess_evidence(service, target, future)["valid"] is False
    evidence = {**successful_evidence(target), "expires_at": NOW + timedelta(days=30)}
    probe_sandbox.clock.advance(601)
    assert assess_evidence(service, target, evidence)["valid"] is False


def test_accepted_fact_missing_identity_does_not_bypass_freshness(probe_sandbox):
    service = make_probe(probe_sandbox)
    target = target_for(probe_sandbox.candidate_revision, probe_sandbox.profile, "json_object")
    evidence = successful_evidence(target)
    accepted = {"status": "active", "probe_id": evidence["probe_id"], "tested_at": NOW, "accepted_at": NOW}
    probe_sandbox.clock.advance(601)
    assert assess_evidence(service, target, evidence, accepted)["valid"] is False


def test_close_failure_is_safe_and_does_not_hide_original_failure(probe_sandbox, monkeypatch):
    from tests.support.provider_probe_fakes import ObservedAsyncClient

    original = ObservedAsyncClient.aclose
    async def close_then_fail(client):
        await original(client)
        raise RuntimeError(probe_sandbox.markers["body"])

    monkeypatch.setattr(ObservedAsyncClient, "aclose", close_then_fail)
    probe_sandbox.scenario.status = 500
    result = asyncio.run(run_probe(make_probe(probe_sandbox), probe_sandbox))
    assert result_code(result) == "UNKNOWN_PROVIDER_ERROR"
    assert view(result)["cleanup_error_code"] == "RESOURCE_RELEASE_FAILED"
    assert all(client.is_closed for client in probe_sandbox.http.clients)
    assert_safe_result(probe_sandbox, result)


def test_unloadable_local_resource_does_not_guess_upstream_failure(probe_sandbox, tmp_path):
    directory = tmp_path / "ocr"
    directory.mkdir()
    probe_sandbox.local.error = RuntimeError(probe_sandbox.markers["body"])
    profile = {**probe_sandbox.profile, "adapter_id": "rapidocr", "model_id": "PP-OCRv5-mobile",
               "auth_mode": "none", "credential_ref": None, "base_url": None,
               "declared_capabilities": ("ocr",), "resource_directory": directory}
    result = asyncio.run(run_probe(make_probe(probe_sandbox), probe_sandbox, profile=profile,
                                   capability="ocr", allow_billable=False))
    assert result_code(result) == "RESOURCE_UNAVAILABLE"
    assert probe_sandbox.scenario.requests == []
    assert_safe_result(probe_sandbox, result)


def test_sdk_setup_failure_releases_owned_http_without_a_request(probe_sandbox, monkeypatch):
    import openai

    def fail_setup(*_args, **_kwargs):
        raise RuntimeError(probe_sandbox.markers["body"])

    monkeypatch.setattr(openai.AsyncOpenAI, "__init__", fail_setup)
    result = asyncio.run(run_probe(make_probe(probe_sandbox), probe_sandbox))
    assert result_code(result) == "UNKNOWN_PROVIDER_ERROR"
    assert view(result)["external_requests"] == 0
    assert len(probe_sandbox.http.clients) == 1
    assert probe_sandbox.http.clients[0].is_closed
    assert_safe_result(probe_sandbox, result)


@pytest.mark.parametrize("header,expected", [("nan", None), ("1e309", None), ("-2", None), ("0", 0), ("1.5", 1.5)])
def test_retry_after_is_bounded_numeric_metadata_only(header, expected, probe_sandbox):
    request = httpx.Request("POST", "https://provider.invalid/v1")
    response = httpx.Response(429, headers={"Retry-After": header}, request=request)
    error = httpx.HTTPStatusError(probe_sandbox.markers["body"], request=request, response=response)
    result = classify_error(error)
    assert view(result)["retry_after"] == expected
    assert result_code(result) == "RATE_LIMITED"
    assert_safe_result(probe_sandbox, result)


def test_plain_disk_oserror_is_unknown_not_a_guessed_network_or_key_failure(probe_sandbox):
    result = classify_error(OSError("invalid_api_key model_not_found DNS timeout"))
    assert result_code(result) == "UNKNOWN_PROVIDER_ERROR"
    assert_safe_result(probe_sandbox, result)


def test_local_load_outlives_deadline_but_is_released_when_it_really_returns(probe_sandbox, tmp_path):
    async def exercise():
        directory = tmp_path / "delayed-local"
        directory.mkdir()
        profile = {**probe_sandbox.profile, "adapter_id": "rapidocr", "model_id": "PP-OCRv5-mobile",
                   "auth_mode": "none", "credential_ref": None, "base_url": None,
                   "declared_capabilities": ("ocr",), "resource_directory": directory}
        started = asyncio.Event()
        released = asyncio.Event()
        gate = Event()
        loop = asyncio.get_running_loop()
        calls = []
        class Resource:
            async def run(self, sample):
                calls.append(sample)
            async def aclose(self):
                released.set()
        def loader(**_kwargs):
            loop.call_soon_threadsafe(started.set)
            if not gate.wait(3):
                raise TimeoutError("synthetic test gate not released")
            return Resource()
        service = make_probe(probe_sandbox)
        service._local_loader = loader
        probe_sandbox.deadline.started = started
        probe_sandbox.deadline.expire_calls = {1, 2}
        try:
            result = await run_probe(service, probe_sandbox, profile=profile, capability="ocr",
                                     allow_billable=False, timeout_seconds=30)
            assert result_code(result) == "SERVICE_UNREACHABLE"
            assert view(result)["cleanup_error_code"] == "RESOURCE_RELEASE_TIMEOUT"
            assert probe_sandbox.clock.seconds == 32
            assert not released.is_set()
            with pytest.raises(Exception) as caught:
                await asyncio.wait_for(run_probe(make_probe(probe_sandbox), probe_sandbox,
                                                 profile=profile, capability="ocr", allow_billable=False), 1)
            assert getattr(caught.value, "status_code", None) == 429
            gate.set()
            await asyncio.wait_for(released.wait(), 1)
            assert calls == []
            assert probe_sandbox.scenario.requests == []
            assert_safe_result(probe_sandbox, result)
        finally:
            gate.set()
            await service.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("state", ["probe", "active", "pending_restart"])
def test_existing_environment_facts_cannot_be_ignored_by_omitting_target_field(state, probe_sandbox):
    service = make_probe(probe_sandbox)
    target = target_for(probe_sandbox.candidate_revision, probe_sandbox.profile, "json_object")
    evidence = {**successful_evidence(target), "environment_id": uuid4()}
    accepted = None
    if state != "probe":
        accepted = {**evidence, "status": state, "accepted_at": NOW}
        probe_sandbox.clock.advance(601)
    assert assess_evidence(service, target, evidence, accepted)["valid"] is False
    assert probe_sandbox.scenario.requests == []
    assert probe_sandbox.credentials.calls == []


def test_target_and_evidence_belonging_to_other_environment_are_not_accepted(probe_sandbox):
    service = make_probe(probe_sandbox)
    target = {**target_for(probe_sandbox.candidate_revision, probe_sandbox.profile, "json_object"),
              "environment_id": uuid4()}
    assert assess_evidence(service, target, successful_evidence(target))["valid"] is False


def test_local_stat_is_off_event_loop_so_deadline_can_run(probe_sandbox, tmp_path, monkeypatch):
    directory = tmp_path / "thread-checked-local"
    directory.mkdir()
    original = Path.is_dir
    loop_thread = get_ident()
    def checked_stat(path):
        if path == directory:
            assert get_ident() != loop_thread, "resource filesystem check blocked the event-loop thread"
        return original(path)
    monkeypatch.setattr(Path, "is_dir", checked_stat)
    profile = {**probe_sandbox.profile, "adapter_id": "rapidocr", "model_id": "PP-OCRv5-mobile",
               "auth_mode": "none", "credential_ref": None, "base_url": None,
               "declared_capabilities": ("ocr",), "resource_directory": directory}
    result = asyncio.run(run_probe(make_probe(probe_sandbox), probe_sandbox,
                                   profile=profile, capability="ocr", allow_billable=False))
    assert result_code(result) == "SUCCESS"
    assert probe_sandbox.local.resource.close_calls == 1


def test_service_capacity_is_bounded_even_for_unbilled_local_work(probe_sandbox, tmp_path):
    async def exercise():
        started, gate = asyncio.Event(), asyncio.Event()
        class Resource:
            async def run(self, _sample):
                started.set()
                await gate.wait()
                return {"text": "synthetic"}
            async def aclose(self):
                pass
        directory = tmp_path / "local"
        directory.mkdir()
        profile = {**probe_sandbox.profile, "adapter_id": "rapidocr", "model_id": "PP-OCRv5-mobile",
                   "auth_mode": "none", "credential_ref": None, "base_url": None,
                   "declared_capabilities": ("ocr",), "resource_directory": directory}
        service = make_probe(probe_sandbox)
        service._max_results = 1
        service._local_loader = lambda **_kwargs: Resource()
        task = asyncio.create_task(run_probe(service, probe_sandbox, profile=profile,
                                            capability="ocr", allow_billable=False))
        try:
            await asyncio.wait_for(started.wait(), 1)
            with pytest.raises(Exception) as caught:
                await asyncio.wait_for(run_probe(service, probe_sandbox, profile=profile,
                                                 capability="ocr", allow_billable=False), 1)
            assert getattr(caught.value, "status_code", None) == 429
        finally:
            gate.set()
            await task
            await service.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("stop", ["timeout", "cancel"])
def test_blocked_credential_read_keeps_lease_until_thread_really_finishes(stop, probe_sandbox):
    async def exercise():
        started = asyncio.Event()
        gate = Event()
        loop = asyncio.get_running_loop()
        original = probe_sandbox.credentials.get
        def blocked_get(*args):
            loop.call_soon_threadsafe(started.set)
            if not gate.wait(3):
                raise TimeoutError("synthetic credential gate not released")
            return original(*args)
        probe_sandbox.credentials.get = blocked_get
        probe_sandbox.deadline.started = started
        probe_sandbox.deadline.expire_calls = {1, 2} if stop == "timeout" else {2}
        service, peer = make_probe(probe_sandbox), make_probe(probe_sandbox)
        task = asyncio.create_task(run_probe(service, probe_sandbox, timeout_seconds=30))
        try:
            if stop == "cancel":
                await asyncio.wait_for(started.wait(), 1)
                task.cancel()
            result = await asyncio.wait_for(task, 1)
            assert result_code(result) == ("SERVICE_UNREACHABLE" if stop == "timeout" else "CANCELLED")
            assert view(result)["cleanup_error_code"] == "RESOURCE_RELEASE_TIMEOUT"
            assert probe_sandbox.scenario.requests == []
            # Settle already queued callbacks; immediate peer admission would
            # otherwise observe a stale lease rather than the blocked read.
            for _ in range(8):
                await asyncio.sleep(0)
            assert any(not cleanup.done() for cleanup in service._cleanup)
            with pytest.raises(Exception) as caught:
                await asyncio.wait_for(run_probe(peer, probe_sandbox), 1)
            assert getattr(caught.value, "status_code", None) == 429
            gate.set()
            async def drain_owned_cleanup():
                while service._cleanup:
                    await asyncio.sleep(0)
            await asyncio.wait_for(drain_owned_cleanup(), 1)
            probe_sandbox.credentials.get = original
            assert result_code(await run_probe(peer, probe_sandbox)) == "SUCCESS"
            assert len(probe_sandbox.scenario.requests) == 1
            assert_safe_result(probe_sandbox, result)
        finally:
            gate.set()
            await service.aclose()
            await peer.aclose()
    asyncio.run(exercise())


def test_cancelled_resource_close_does_not_skip_other_owned_clients(probe_sandbox, monkeypatch):
    from backend.app.ai.llm.openai_compatible import OpenAICompatibleProvider

    async def cancelled_close(_provider):
        raise asyncio.CancelledError()
    monkeypatch.setattr(OpenAICompatibleProvider, "aclose", cancelled_close)
    result = asyncio.run(run_probe(make_probe(probe_sandbox), probe_sandbox))
    assert result_code(result) == "UNKNOWN_PROVIDER_ERROR"
    assert view(result)["cleanup_error_code"] == "RESOURCE_RELEASE_FAILED"
    assert_one_closed_request(probe_sandbox)
    assert_safe_result(probe_sandbox, result)


@pytest.mark.parametrize("phase,expected", [("load", "RESOURCE_UNAVAILABLE"),
                                             ("encode", "UNKNOWN_PROVIDER_ERROR"),
                                             ("dimension", "EMBEDDING_INCOMPATIBLE")])
def test_real_local_embedding_distinguishes_load_from_inference_failure(phase, expected, probe_sandbox, tmp_path, monkeypatch):
    from backend.app.ai.embedding.providers.local import LocalEmbeddingProvider

    directory = tmp_path / "synthetic-embedding"
    directory.mkdir()
    original = LocalEmbeddingProvider.__init__
    class Model:
        def get_embedding_dimension(self):
            return 3 if phase == "dimension" else 1024
        def encode(self, *_args, **_kwargs):
            raise RuntimeError(probe_sandbox.markers["body"])
    calls = []
    def loader(path):
        calls.append(path)
        if phase == "load":
            raise ValueError(probe_sandbox.markers["body"])
        return Model()
    def injected(provider, **kwargs):
        original(provider, **{**kwargs, "model_loader": loader})
    monkeypatch.setattr(LocalEmbeddingProvider, "__init__", injected)
    profile = {**probe_sandbox.profile, "adapter_id": "local", "auth_mode": "none",
               "credential_ref": None, "base_url": None, "resource_directory": directory,
               "parameters": {"dimension": 1024}, "declared_capabilities": ("embedding",)}
    service = make_probe(probe_sandbox)
    service._local_loader = None
    result = asyncio.run(run_probe(service, probe_sandbox, profile=profile,
                                   capability="embedding", allow_billable=False))
    assert calls == [str(directory)]
    assert result_code(result) == expected
    assert probe_sandbox.scenario.requests == []
    assert_safe_result(probe_sandbox, result)
