"""T207 / TCR-009: single-capability Probe contracts before T209.

MockTransport observes real HTTP attempts beneath any SDK retry machinery.
Clock/deadline inputs are deterministic; this is not a real-cloud cost or wall-
clock performance test, nor T210 activation/T220 readiness integration.
"""

from __future__ import annotations

import asyncio
import json
import ssl
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import httpx
import openai
import pytest
from pydantic import SecretStr, ValidationError

from tests.support.credential_fakes import (
    assert_no_secret,
    observe_reads,
    safe_surfaces,
)
from tests.support.provider_probe_fakes import (
    NOW,
    MinimalAnswer,
    assert_one_closed_request,
    assert_safe_result,
    assess_evidence,
    classify_error,
    make_probe,
    make_profile,
    probe_sandbox,
    required_method,
    result_code,
    run_probe,
    successful_evidence,
    target_for,
    view,
)

pytestmark = pytest.mark.usefixtures(probe_sandbox.__name__)


@pytest.mark.parametrize("status,upstream_code,expected", [
    (401, "invalid_api_key", "KEY_INVALID"),
    (403, "invalid_api_key", "KEY_INVALID"),
    (403, "insufficient_quota", "UNKNOWN_PROVIDER_ERROR"),
    (403, None, "UNKNOWN_PROVIDER_ERROR"),
    (404, "model_not_found", "MODEL_NOT_FOUND"),
    (404, None, "UNKNOWN_PROVIDER_ERROR"),
    (429, "rate_limit_exceeded", "RATE_LIMITED"),
    (500, None, "UNKNOWN_PROVIDER_ERROR"),
])
def test_error_classification_uses_structured_facts_not_status_guess(status, upstream_code, expected, probe_sandbox):
    request = httpx.Request("POST", "https://provider.invalid/v1/chat/completions")
    body = {"error": {"message": probe_sandbox.markers["body"], "code": upstream_code}}
    response = httpx.Response(status, json=body, request=request)
    classes = {401: openai.AuthenticationError, 403: openai.PermissionDeniedError,
               404: openai.NotFoundError, 429: openai.RateLimitError, 500: openai.InternalServerError}
    error = classes[status](probe_sandbox.markers["key"], response=response, body=body)
    result = classify_error(error)
    assert result_code(result) == expected
    assert view(result)["correlation_id"] == "t207-correlation"
    assert_safe_result(probe_sandbox, result)


@pytest.mark.parametrize("kind", ["dns", "tls", "connect", "read_timeout", "deadline"])
def test_network_and_timeout_errors_have_safe_unreachable_classification(kind, probe_sandbox):
    request = httpx.Request("POST", "https://provider.invalid/v1/chat/completions")
    marker = probe_sandbox.markers["body"]
    error: BaseException
    if kind == "deadline":
        error = TimeoutError(marker)
    elif kind == "read_timeout":
        error = httpx.ReadTimeout(marker, request=request)
    else:
        error = httpx.ConnectError(marker, request=request)
        if kind == "tls":
            error.__cause__ = ssl.SSLError(marker)
        elif kind == "dns":
            import socket
            error.__cause__ = socket.gaierror(marker)
    result = classify_error(error)
    assert result_code(result) == "SERVICE_UNREACHABLE"
    assert_safe_result(probe_sandbox, result)


@pytest.mark.parametrize("kind,expected", [
    ("json", "STRUCTURED_OUTPUT_INVALID"),
    ("pydantic", "STRUCTURED_OUTPUT_INVALID"),
    ("unknown", "UNKNOWN_PROVIDER_ERROR"),
    ("cancelled", "CANCELLED"),
    ("resource", "RESOURCE_UNAVAILABLE"),
])
def test_non_http_error_results_are_safe_and_distinct(kind, expected, probe_sandbox):
    marker = probe_sandbox.markers["body"]
    error: BaseException
    if kind == "json":
        error = json.JSONDecodeError("synthetic invalid JSON", marker, 0)
    elif kind == "pydantic":
        try:
            MinimalAnswer.model_validate({"ok": marker})
        except ValidationError as caught:
            error = caught
        else:
            pytest.fail("synthetic non-boolean unexpectedly passed the target schema", pytrace=False)
    elif kind == "cancelled":
        error = asyncio.CancelledError(marker)
    elif kind == "resource":
        error = FileNotFoundError(marker)
    else:
        error = RuntimeError(marker)
    result = classify_error(error)
    assert result_code(result) == expected
    assert_safe_result(probe_sandbox, result)


def test_plain_404_html_is_not_model_not_found(probe_sandbox):
    request = httpx.Request("POST", "https://provider.invalid/wrong-route")
    response = httpx.Response(404, text=probe_sandbox.markers["body"], request=request)
    error = httpx.HTTPStatusError("route not found", request=request, response=response)
    assert result_code(classify_error(error)) == "UNKNOWN_PROVIDER_ERROR"


@pytest.mark.parametrize("header", ["garbage", "-5", "inf", "99999999999999999999999999"])
def test_retry_after_is_optional_safe_metadata_not_an_auto_retry(header, probe_sandbox):
    request = httpx.Request("POST", "https://provider.invalid/v1/chat/completions")
    response = httpx.Response(429, headers={"retry-after": header}, request=request)
    error = openai.RateLimitError("synthetic limit", response=response, body={"error": {"code": "rate_limit_exceeded"}})
    result = classify_error(error)
    assert result_code(result) == "RATE_LIMITED"
    retry_after = view(result).get("retry_after")
    assert retry_after is None or (isinstance(retry_after, (int, float)) and 0 <= retry_after < 10**9)
    assert probe_sandbox.scenario.requests == []


def test_missing_api_key_is_local_zero_request_result(probe_sandbox):
    service = make_probe(probe_sandbox)
    profile = {**probe_sandbox.profile, "credential_ref": None}
    result = asyncio.run(run_probe(service, probe_sandbox, profile=profile))
    assert result_code(result) == "KEY_MISSING"
    assert view(result)["external_requests"] == 0
    assert probe_sandbox.http.clients == []
    assert_safe_result(probe_sandbox, result)


def test_local_auth_none_does_not_require_or_send_key(probe_sandbox):
    service = make_probe(probe_sandbox)
    profile = {**probe_sandbox.profile, "auth_mode": "none", "credential_ref": None,
               "base_url": "http://127.0.0.1:18181/v1"}
    result = asyncio.run(run_probe(service, probe_sandbox, profile=profile))
    assert result_code(result) == "SUCCESS"
    assert probe_sandbox.credentials.calls == []
    assert "authorization" not in probe_sandbox.scenario.requests[0].headers
    assert_one_closed_request(probe_sandbox)


def test_external_probe_requires_explicit_fee_confirmation(probe_sandbox):
    service = make_probe(probe_sandbox)
    with pytest.raises(Exception) as caught:
        asyncio.run(run_probe(service, probe_sandbox, allow_billable=False))
    assert getattr(caught.value, "status_code", None) == 422
    assert probe_sandbox.http.clients == []
    assert probe_sandbox.scenario.requests == []
    assert probe_sandbox.credentials.calls == []


@pytest.mark.parametrize("capability", ["vision", "embedding", "json_schema", "telepathy"])
def test_unsupported_or_undeclared_capability_rejected_before_network(capability, probe_sandbox):
    service = make_probe(probe_sandbox)
    result = asyncio.run(run_probe(service, probe_sandbox, capability=capability))
    assert result_code(result) == "CAPABILITY_UNSUPPORTED"
    assert view(result)["external_requests"] == 0
    assert probe_sandbox.scenario.requests == []
    assert probe_sandbox.credentials.calls == []


@pytest.mark.parametrize("kind,adapter,capability,model", [
    ("ocr", "rapidocr", "ocr", "PP-OCRv5-mobile"),
    ("embedding", "bge", "embedding", "BAAI/bge-m3"),
    ("rerank", "cross_encoder", "local_rerank", "BAAI/bge-reranker-base"),
])
def test_missing_local_resource_reports_unavailable_without_download(kind, adapter, capability, model, probe_sandbox, tmp_path):
    service = make_probe(probe_sandbox)
    missing = tmp_path / kind / "not-installed"
    profile = {**probe_sandbox.profile, "adapter_id": adapter, "model_id": model, "base_url": None,
               "declared_capabilities": (capability,), "auth_mode": "none", "credential_ref": None,
               "resource_directory": missing}
    result = asyncio.run(run_probe(service, probe_sandbox, profile=profile,
                                   capability=capability, allow_billable=False))
    assert result_code(result) == "RESOURCE_UNAVAILABLE"
    assert view(result)["external_requests"] == 0
    assert not missing.exists()
    assert probe_sandbox.scenario.requests == []
    assert probe_sandbox.local.calls == []


@pytest.mark.parametrize("status,expected", [
    (200, "SUCCESS"), (429, "RATE_LIMITED"), (500, "UNKNOWN_PROVIDER_ERROR"),
])
def test_exactly_one_transport_no_sdk_or_business_retry_or_fallback(status, expected, probe_sandbox, monkeypatch):
    sdk_retries = []
    original_init = openai.AsyncOpenAI.__init__

    def observe_sdk(instance, *args, **kwargs):
        original_init(instance, *args, **kwargs)
        sdk_retries.append(instance.max_retries)

    monkeypatch.setattr(openai.AsyncOpenAI, "__init__", observe_sdk)
    probe_sandbox.scenario.status = status
    if status != 200:
        probe_sandbox.scenario.json_body = {"error": {"message": probe_sandbox.markers["body"]}}
    service = make_probe(probe_sandbox)
    result = asyncio.run(run_probe(service, probe_sandbox))
    assert result_code(result) == expected
    assert view(result)["external_requests"] == 1
    assert view(result)["usage"] is None
    assert all(retries == 0 for retries in sdk_retries)
    assert_one_closed_request(probe_sandbox)
    request = probe_sandbox.scenario.requests[0]
    assert request.url.host == "provider.invalid"
    assert request.url.path.endswith("/chat/completions")
    assert_safe_result(probe_sandbox, result)


def test_connect_failure_never_retries_or_falls_back_and_closes_client(probe_sandbox):
    probe_sandbox.scenario.error = httpx.ConnectError("synthetic connection failure")
    result = asyncio.run(run_probe(make_probe(probe_sandbox), probe_sandbox))
    assert result_code(result) == "SERVICE_UNREACHABLE"
    assert view(result)["external_requests"] == 1
    assert_one_closed_request(probe_sandbox)


@pytest.mark.parametrize("content", ["not JSON", '{"ok": "not-a-boolean"}'])
def test_structured_output_requires_json_and_local_pydantic_validation(content, probe_sandbox):
    probe_sandbox.scenario.content = content
    result = asyncio.run(run_probe(make_probe(probe_sandbox), probe_sandbox))
    assert result_code(result) == "STRUCTURED_OUTPUT_INVALID"
    assert_one_closed_request(probe_sandbox)
    assert_safe_result(probe_sandbox, result)


def test_cross_host_redirect_never_forwards_authorization(probe_sandbox):
    probe_sandbox.scenario.status = 307
    probe_sandbox.scenario.headers = {"location": "https://untrusted.invalid/collect"}
    result = asyncio.run(run_probe(make_probe(probe_sandbox), probe_sandbox))
    assert result_code(result) != "SUCCESS"
    assert_one_closed_request(probe_sandbox)
    assert all(request.url.host == "provider.invalid" for request in probe_sandbox.scenario.requests)
    assert all(not client.follow_redirects for client in probe_sandbox.http.clients)


@pytest.mark.parametrize("timeout", [None, 5, 30])
def test_default_and_allowed_deadlines_reach_actual_waiting_primitive(timeout, probe_sandbox):
    arguments = {} if timeout is None else {"timeout_seconds": timeout}
    result = asyncio.run(run_probe(make_probe(probe_sandbox), probe_sandbox, **arguments))
    assert result_code(result) == "SUCCESS"
    assert probe_sandbox.deadline.timeouts[0] == (15 if timeout is None else timeout)
    assert all(0 < limit <= 2 for limit in probe_sandbox.deadline.timeouts[1:])
    assert_one_closed_request(probe_sandbox)


@pytest.mark.parametrize("timeout", [0, 4.99, 30.01, -1, float("inf"), float("nan")])
def test_invalid_timeout_is_rejected_without_dispatch(timeout, probe_sandbox):
    service = make_probe(probe_sandbox)
    with pytest.raises(Exception) as caught:
        asyncio.run(run_probe(service, probe_sandbox, timeout_seconds=timeout))
    assert getattr(caught.value, "status_code", None) == 422
    assert probe_sandbox.scenario.requests == []
    assert probe_sandbox.http.clients == []


def test_timeout_cancels_transport_and_allows_at_most_two_seconds_cleanup(probe_sandbox):
    probe_sandbox.scenario.block = True
    probe_sandbox.deadline.expire_calls = {1}
    result = asyncio.run(run_probe(make_probe(probe_sandbox), probe_sandbox, timeout_seconds=30))
    assert result_code(result) == "SERVICE_UNREACHABLE"
    assert probe_sandbox.scenario.finished.is_set()
    assert probe_sandbox.deadline.timeouts[0] == 30
    assert len(probe_sandbox.deadline.timeouts) >= 2
    assert all(0 < limit <= 2 for limit in probe_sandbox.deadline.timeouts[1:])
    assert 30 <= probe_sandbox.clock.seconds <= 32
    assert view(result)["usage"] is None
    assert_one_closed_request(probe_sandbox)


def test_cancellation_stops_own_request_closes_client_without_refund_promise(probe_sandbox):
    async def exercise():
        probe_sandbox.scenario.block = True
        service = make_probe(probe_sandbox)
        task = asyncio.create_task(run_probe(service, probe_sandbox))
        try:
            await asyncio.wait_for(probe_sandbox.scenario.started.wait(), 1)
            task.cancel()
            try:
                result = await task
            except asyncio.CancelledError as error:
                # Both coroutine cancellation styles must reach the real safe classifier.
                result = classify_error(error)
            assert result_code(result) == "CANCELLED"
            assert not view(result).get("refunded", False)
            assert_safe_result(probe_sandbox, result)
            assert probe_sandbox.scenario.finished.is_set()
            assert_one_closed_request(probe_sandbox)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())


def test_per_environment_paid_limit_includes_other_service_instances_and_releases(probe_sandbox):
    async def exercise():
        probe_sandbox.scenario.block = True
        service = make_probe(probe_sandbox)
        peer = make_probe(probe_sandbox)
        task = asyncio.create_task(run_probe(service, probe_sandbox))
        try:
            await asyncio.wait_for(probe_sandbox.scenario.started.wait(), 1)
            with pytest.raises(Exception) as caught:
                await run_probe(peer, probe_sandbox)
            assert getattr(caught.value, "status_code", None) == 429
            assert len(probe_sandbox.scenario.requests) == 1
            probe_sandbox.scenario.release.set()
            assert result_code(await task) == "SUCCESS"
            assert result_code(await run_probe(peer, probe_sandbox)) == "SUCCESS"
            assert len(probe_sandbox.scenario.requests) == 2
            assert all(client.is_closed for client in probe_sandbox.http.clients)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())


def test_paid_limit_is_not_a_global_lock_across_environments(probe_sandbox):
    async def exercise():
        probe_sandbox.scenario.block = True
        service = make_probe(probe_sandbox)
        first = asyncio.create_task(run_probe(service, probe_sandbox))
        second = None
        try:
            await asyncio.wait_for(probe_sandbox.scenario.started.wait(), 1)
            environment = uuid4()
            profile = make_profile(environment)
            ref = profile["credential_ref"]
            probe_sandbox.credentials.values[(ref.credential_id, ref.version)] = SecretStr(probe_sandbox.markers["key"])
            peer = make_probe(probe_sandbox, environment_id=environment)
            second = asyncio.create_task(run_probe(peer, probe_sandbox, profile=profile))
            async def both_started():
                while len(probe_sandbox.scenario.requests) < 2:
                    await asyncio.sleep(0)

            await asyncio.wait_for(both_started(), 1)
            assert len(probe_sandbox.scenario.requests) == 2
            probe_sandbox.scenario.release.set()
            assert result_code(await first) == "SUCCESS"
            assert result_code(await second) == "SUCCESS"
        finally:
            for task in (first, second):
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())


def test_fixed_llm_sample_does_not_read_business_inputs(probe_sandbox, tmp_path, monkeypatch):
    sample_source = tmp_path / "synthetic-business.txt"
    sample_source.write_text(probe_sandbox.markers["business"], encoding="utf-8")
    monkeypatch.setenv("T207_BUSINESS_INPUT", str(sample_source))
    service = make_probe(probe_sandbox)

    async def exercise():
        for _ in range(2):
            result = await run_probe(service, probe_sandbox, candidate_revision=uuid4())
            assert result_code(result) == "SUCCESS"

    with observe_reads(sample_source) as reads:
        asyncio.run(exercise())
    assert reads == []
    payloads = [json.loads(request.content) for request in probe_sandbox.scenario.requests]
    assert payloads[0]["messages"] == payloads[1]["messages"]
    messages = payloads[0]["messages"]
    assert 1 <= len(messages) <= 2
    assert len(json.dumps(messages)) < 2048
    assert_no_secret(probe_sandbox.markers, {"sample": json.dumps(messages)})
    assert all(client.is_closed for client in probe_sandbox.http.clients)


@pytest.mark.parametrize("capability", ["embedding", "llm_rerank", "vision"])
def test_each_capability_uses_one_minimal_synthetic_sample(capability, probe_sandbox):
    profile = {**probe_sandbox.profile, "declared_capabilities": (capability,)}
    if capability == "embedding":
        probe_sandbox.scenario.json_body = {"data": [{"index": 0, "embedding": [0.1] * 1024}],
                                           "model": "t207-model", "object": "list"}
    elif capability == "llm_rerank":
        probe_sandbox.scenario.content = '{"rankings":[{"chunk_id":"probe-a","score":0.9},{"chunk_id":"probe-b","score":0.1}]}'
    else:
        profile["adapter_id"] = "deepseek"
        profile["model_id"] = "deepseek-v4-flash"
        profile["base_url"] = "https://api.deepseek.com/v1"
    result = asyncio.run(run_probe(make_probe(probe_sandbox), probe_sandbox, profile=profile, capability=capability))
    assert result_code(result) == "SUCCESS"
    assert_one_closed_request(probe_sandbox)
    payload = json.loads(probe_sandbox.scenario.requests[0].content)
    assert_no_secret(probe_sandbox.markers, {"sample": json.dumps(payload)})
    if capability == "embedding":
        assert len(payload["input"]) == 1
        assert isinstance(payload["input"][0], str) and 0 < len(payload["input"][0]) < 128
    elif capability == "vision":
        images = [part["image_url"]["url"] for message in payload["messages"]
                  if isinstance(message["content"], list) for part in message["content"]
                  if part.get("type") == "image_url"]
        assert len(images) == 1 and images[0].startswith("data:image/")
        assert len(images[0]) < 32768
    else:
        sample = json.dumps(payload["messages"])
        assert "probe-a" in sample and "probe-b" in sample
        assert not probe_sandbox.scenario.requests[0].url.path.endswith("/rerank")


@pytest.mark.parametrize("delta,valid,code", [(599, True, None), (601, False, "PROBE_EXPIRED")])
def test_new_candidate_requires_fresh_successful_evidence(delta, valid, code, probe_sandbox):
    service = make_probe(probe_sandbox)
    target = target_for(probe_sandbox.candidate_revision, probe_sandbox.profile, "json_object")
    evidence = successful_evidence(target)
    before = deepcopy((target, evidence))
    probe_sandbox.clock.advance(delta)
    assessment = assess_evidence(service, target, evidence)
    assert assessment["valid"] is valid
    if code is not None:
        assert assessment["code"] == code
    assert (target, evidence) == before
    assert probe_sandbox.scenario.requests == []


@pytest.mark.parametrize("field", [
    "candidate_revision", "profile_id", "profile_revision", "credential_version", "model_id", "capability",
])
def test_success_for_a_different_immutable_target_cannot_authorize_candidate(field, probe_sandbox):
    service = make_probe(probe_sandbox)
    target = target_for(probe_sandbox.candidate_revision, probe_sandbox.profile, "json_object")
    evidence = successful_evidence(target)
    evidence[field] = "t207-different" if field in {"model_id", "capability"} else uuid4()
    original = deepcopy(target)
    assessment = assess_evidence(service, target, evidence)
    assert assessment["valid"] is False
    assert assessment["code"] == "PROBE_REQUIRED"
    assert target == original
    assert probe_sandbox.scenario.requests == []


@pytest.mark.parametrize("status,code", [
    ("failed", "KEY_INVALID"), ("cancelled", "CANCELLED"), ("running", None),
])
def test_non_success_never_authorizes_candidate(status, code, probe_sandbox):
    service = make_probe(probe_sandbox)
    target = target_for(probe_sandbox.candidate_revision, probe_sandbox.profile, "json_object")
    evidence = {**successful_evidence(target), "status": status, "safe_error_code": code}
    assessment = assess_evidence(service, target, evidence)
    assert assessment["valid"] is False
    assert assessment["code"] == "PROBE_REQUIRED"


@pytest.mark.parametrize("state", ["active", "pending_restart"])
def test_accepted_binding_survives_expiry_reload_and_unrelated_revision_without_automatic_probe(state, probe_sandbox):
    target = target_for(probe_sandbox.candidate_revision, probe_sandbox.profile, "json_object")
    evidence = successful_evidence(target)
    accepted = {**target, "probe_id": evidence["probe_id"], "tested_at": NOW,
                "accepted_at": NOW + timedelta(seconds=1), "status": state}
    original = deepcopy(accepted)
    probe_sandbox.clock.advance(86400)
    # Reconstruct the real service and supply persisted facts; this is a unit
    # reload, not proof of T210/T220 disk/HTTP/restart wiring.
    for _ in range(2):
        service = make_probe(probe_sandbox)
        assessment = assess_evidence(service, {**target, "candidate_revision": uuid4()}, evidence, accepted)
        assert assessment["valid"] is True
    assert accepted == original
    assert probe_sandbox.scenario.requests == []
    assert probe_sandbox.credentials.calls == []


def test_reading_completed_probe_is_not_a_paid_retest(probe_sandbox):
    async def exercise():
        service = make_probe(probe_sandbox)
        result = await run_probe(service, probe_sandbox)
        assert result_code(result) == "SUCCESS"
        probe_sandbox.clock.advance(86400)
        for _ in range(3):
            read = required_method(service, "get")(view(result)["probe_id"])
            if asyncio.iscoroutine(read):
                read = await read
            assert result_code(read) == "SUCCESS"
        assert_one_closed_request(probe_sandbox)

    asyncio.run(exercise())


@pytest.mark.parametrize("surface", ["dto", "exception", "logs", "response"])
def test_scanner_negative_control_detects_marker_without_echoing_it(surface, probe_sandbox):
    with pytest.raises(pytest.fail.Exception) as caught:
        assert_no_secret(probe_sandbox.markers, {surface: probe_sandbox.markers["key"]})
    assert "SECRET_DISCLOSURE" in str(caught.value)
    assert_no_secret(probe_sandbox.markers, safe_surfaces(caught.value))


def test_existing_async_facility_runs_without_real_external_network():
    async def exercise():
        event = asyncio.Event()
        asyncio.get_running_loop().call_soon(event.set)
        await asyncio.wait_for(event.wait(), 1)
        return "internal-loop-only"

    assert asyncio.run(exercise()) == "internal-loop-only"


def test_real_run_uses_each_explicit_credential_model_and_returns_bound_evidence(probe_sandbox):
    async def exercise():
        service = make_probe(probe_sandbox)
        pairs = (("t207-model-a", probe_sandbox.markers["key"]),
                 ("t207-model-b", probe_sandbox.markers["body"]))
        for model, key in pairs:
            profile = make_profile(probe_sandbox.environment_id, model_id=model)
            ref = profile["credential_ref"]
            probe_sandbox.credentials.values[(ref.credential_id, ref.version)] = SecretStr(key)
            candidate = uuid4()
            result = await run_probe(service, probe_sandbox, profile=profile, candidate_revision=candidate)
            assert result_code(result) == "SUCCESS"
            assert probe_sandbox.credentials.calls[-1] == (ref.credential_id, ref.version)
            request = probe_sandbox.scenario.requests[-1]
            if request.headers.get("authorization") != "Bearer " + key:
                pytest.fail("request did not use the explicit controlled credential version", pytrace=False)
            assert json.loads(request.content)["model"] == model
            data = view(result)
            target = target_for(candidate, profile, "json_object")
            for field in ("candidate_revision", "profile_revision", "credential_version", "capability"):
                assert data[field] == target[field]
            assert assess_evidence(service, target, data)["valid"] is True
            assert_safe_result(probe_sandbox, result)
        assert len(probe_sandbox.scenario.requests) == 2
        assert all(client.is_closed for client in probe_sandbox.http.clients)

    asyncio.run(exercise())


@pytest.mark.parametrize("state", ["active", "pending_restart"])
@pytest.mark.parametrize("field", ["profile_id", "profile_revision", "credential_version", "model_id", "capability"])
def test_existing_acceptance_cannot_authorize_changed_binding(state, field, probe_sandbox):
    target = target_for(probe_sandbox.candidate_revision, probe_sandbox.profile, "json_object")
    evidence = successful_evidence(target)
    accepted = {**target, "probe_id": evidence["probe_id"], "tested_at": NOW,
                "accepted_at": NOW + timedelta(seconds=1), "status": state}
    original = deepcopy(accepted)
    changed = {**target, "candidate_revision": uuid4()}
    changed[field] = "t207-changed" if field in {"model_id", "capability"} else uuid4()
    probe_sandbox.clock.advance(86400)
    assessment = assess_evidence(make_probe(probe_sandbox), changed, evidence, accepted)
    assert assessment["valid"] is False
    assert assessment["code"] == "PROBE_REQUIRED"
    assert accepted == original
    assert probe_sandbox.scenario.requests == []


@pytest.mark.parametrize("outcome", ["failure", "timeout", "cancelled"])
def test_terminal_outcome_releases_environment_slot_for_explicit_next_probe(outcome, probe_sandbox):
    async def exercise():
        service = make_probe(probe_sandbox)
        if outcome == "failure":
            probe_sandbox.scenario.status = 500
            result = await run_probe(service, probe_sandbox)
            assert result_code(result) == "UNKNOWN_PROVIDER_ERROR"
        elif outcome == "timeout":
            probe_sandbox.scenario.block = True
            probe_sandbox.deadline.expire_calls = {1}
            result = await run_probe(service, probe_sandbox)
            assert result_code(result) == "SERVICE_UNREACHABLE"
        else:
            probe_sandbox.scenario.block = True
            task = asyncio.create_task(run_probe(service, probe_sandbox))
            try:
                await asyncio.wait_for(probe_sandbox.scenario.started.wait(), 1)
                task.cancel()
                try:
                    result = await task
                except asyncio.CancelledError as error:
                    result = classify_error(error)
                assert result_code(result) == "CANCELLED"
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        assert_one_closed_request(probe_sandbox)
        probe_sandbox.scenario.status = 200
        probe_sandbox.scenario.block = False
        probe_sandbox.deadline.expire_calls.clear()
        peer = make_probe(probe_sandbox)
        result = await run_probe(peer, probe_sandbox)
        assert result_code(result) == "SUCCESS"
        assert len(probe_sandbox.scenario.requests) == 2
        assert all(client.is_closed for client in probe_sandbox.http.clients)

    asyncio.run(exercise())


@pytest.mark.parametrize("dimension", [3, 1023, 1025])
def test_embedding_wrong_dimension_is_not_a_success_or_silent_coercion(dimension, probe_sandbox):
    probe_sandbox.scenario.json_body = {"data": [{"index": 0, "embedding": [0.1] * dimension}],
                                       "model": "t207-model", "object": "list"}
    profile = {**probe_sandbox.profile, "declared_capabilities": ("embedding",)}
    result = asyncio.run(run_probe(make_probe(probe_sandbox), probe_sandbox,
                                   profile=profile, capability="embedding"))
    assert result_code(result) == "EMBEDDING_INCOMPATIBLE"
    assert_one_closed_request(probe_sandbox)


@pytest.mark.parametrize("load_fails", [False, True])
def test_preinstalled_ocr_uses_fixed_image_or_safe_load_error_without_http(load_fails, probe_sandbox, tmp_path):
    directory = tmp_path / "preinstalled-ocr"
    directory.mkdir()
    # Synthetic files are only a resource-location input. The injected loader,
    # not a real OCR SDK, supplies the preinstalled-resource fact in this unit.
    for name in ("det.onnx", "rec.onnx", "cls.onnx"):
        (directory / name).write_bytes(b"T207 fake model - not executable weights")
    profile = {**probe_sandbox.profile, "adapter_id": "rapidocr", "model_id": "PP-OCRv5-mobile",
               "base_url": None, "auth_mode": "none", "credential_ref": None,
               "declared_capabilities": ("ocr",), "resource_directory": directory}
    if load_fails:
        probe_sandbox.local.error = FileNotFoundError(probe_sandbox.markers["body"])

    async def exercise():
        service = make_probe(probe_sandbox)
        for _ in range(1 if load_fails else 2):
            result = await run_probe(service, probe_sandbox, profile=profile,
                                     capability="ocr", allow_billable=False)
            assert result_code(result) == ("RESOURCE_UNAVAILABLE" if load_fails else "SUCCESS")
            assert view(result)["external_requests"] == 0
            assert_safe_result(probe_sandbox, result)

    asyncio.run(exercise())
    assert probe_sandbox.scenario.requests == []
    assert probe_sandbox.credentials.calls == []
    if load_fails:
        assert len(probe_sandbox.local.calls) == 1
        assert probe_sandbox.local.resource.close_calls == 0
    else:
        samples = probe_sandbox.local.resource.samples
        assert len(samples) == 2 and samples[0] == samples[1]
        assert isinstance(samples[0], bytes) and samples[0].startswith(b"\x89PNG\r\n\x1a\n")
        assert len(samples[0]) < 32768
        assert probe_sandbox.local.resource.close_calls == 2
        assert_no_secret(probe_sandbox.markers, {"ocr_sample": samples[0]})


def test_existing_mock_transport_facility_records_real_httpx_dispatch_and_close(probe_sandbox):
    async def exercise():
        client = probe_sandbox.http(follow_redirects=False, timeout=15)
        try:
            response = await client.post("https://provider.invalid/v1/chat/completions", json={"synthetic": True})
            assert response.status_code == 200
        finally:
            await client.aclose()
        assert_one_closed_request(probe_sandbox)

    asyncio.run(exercise())


def test_existing_deadline_facility_waits_for_dispatch_after_async_preparation(probe_sandbox):
    async def exercise():
        probe_sandbox.scenario.block = True
        probe_sandbox.deadline.expire_calls = {1}
        client = probe_sandbox.http(follow_redirects=False, timeout=30)

        async def prepared_request():
            for _ in range(5):
                await asyncio.sleep(0)
            return await client.post("https://provider.invalid/v1/chat/completions", json={"synthetic": True})

        try:
            with pytest.raises(TimeoutError, match="synthetic deadline"):
                await probe_sandbox.deadline.wait_for(prepared_request(), 30)
        finally:
            await client.aclose()
        assert probe_sandbox.scenario.started.is_set()
        assert probe_sandbox.scenario.finished.is_set()
        assert probe_sandbox.clock.seconds == 30
        assert_one_closed_request(probe_sandbox)

    asyncio.run(exercise())
