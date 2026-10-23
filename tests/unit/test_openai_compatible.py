"""T208 / TCR-010: real SDK transport observations, no external services."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import json
import os
from copy import deepcopy
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import AnyUrl, BaseModel, SecretStr

from backend.app.ai.llm.base import BaseLLMProvider, describe_llm_provider
from backend.app.ai.llm.deepseek import DeepSeekProvider
from backend.app.core.retry_policy import (
    ProviderCallError,
    ProviderExecutionError,
    RetryPolicy,
)
from tests.support.credential_fakes import (
    assert_no_secret,
    assert_secret_equal,
    error_surfaces,
    safe_surfaces,
)
from tests.support.provider_probe_fakes import ProbeSandbox, probe_sandbox
from tests.unit.settings_helpers import build_test_settings

pytestmark = pytest.mark.usefixtures(probe_sandbox.__name__)
_MODULE = "backend.app.ai.llm.openai_compatible"
_MESSAGES = [{"role": "user", "content": "T208 synthetic text request"}]
_SAFE_ERRORS = (ProviderCallError, ProviderExecutionError)


class TextAnswer(BaseModel):
    ok: bool


def provider_class() -> Any:
    # Missing implementation is a call-time red light, not a collection error.
    if importlib.util.find_spec(_MODULE) is None:
        pytest.fail("T208_IMPLEMENTATION_REQUIRED: " + _MODULE, pytrace=False)
    module = importlib.import_module(_MODULE)
    cls = getattr(module, "OpenAICompatibleProvider", None)
    if cls is None:
        pytest.fail("T208_IMPLEMENTATION_REQUIRED: OpenAICompatibleProvider", pytrace=False)
    return cls


def make_provider(sandbox: ProbeSandbox, **changes: Any) -> Any:
    options: dict[str, Any] = {
        "model": "t208-text-model", "base_url": "https://provider.invalid/v1",
        "api_key": SecretStr(sandbox.markers["key"]),
        "retry_policy": RetryPolicy(max_retries=0),
    }
    options.update(changes)
    return provider_class()(**options)


@dataclass
class ClientObservations:
    clients: list[httpx.AsyncClient] = field(default_factory=list, repr=False)
    options: list[dict[str, Any]] = field(default_factory=list, repr=False)
    http_closes: list[httpx.AsyncClient] = field(default_factory=list, repr=False)
    sdk_closes: list[AsyncOpenAI] = field(default_factory=list, repr=False)


def observe_owned_clients(sandbox: ProbeSandbox, monkeypatch: Any) -> ClientObservations:
    """Replace only transport IO; SDK construction, retries and close stay real."""
    observed = ClientObservations()
    original_init = httpx.AsyncClient.__init__
    original_http_close = httpx.AsyncClient.aclose
    original_sdk_close = AsyncOpenAI.close

    def initialize(client: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        if "transport" not in kwargs:
            observed.options.append(dict(kwargs))
            kwargs["transport"] = httpx.MockTransport(sandbox.scenario.respond)
            observed.clients.append(client)
        original_init(client, *args, **kwargs)

    async def close_http(client: httpx.AsyncClient) -> None:
        observed.http_closes.append(client)
        await original_http_close(client)

    async def close_sdk(client: AsyncOpenAI) -> None:
        observed.sdk_closes.append(client)
        await original_sdk_close(client)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", initialize)
    monkeypatch.setattr(httpx.AsyncClient, "aclose", close_http)
    monkeypatch.setattr(AsyncOpenAI, "close", close_sdk)
    return observed


def assert_safe_error(sandbox: ProbeSandbox, error: BaseException) -> None:
    assert_no_secret(sandbox.markers, error_surfaces(error))
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__


def test_independent_provider_preserves_original_required_signature(probe_sandbox):
    cls = provider_class()
    assert issubclass(cls, BaseLLMProvider)
    assert not issubclass(cls, DeepSeekProvider)
    signature = inspect.signature(cls.generate_structured)
    assert list(signature.parameters) == ["self", "messages", "schema", "model"]
    assert signature.parameters["messages"].default is inspect.Parameter.empty
    assert signature.parameters["schema"].default is inspect.Parameter.empty
    assert signature.parameters["model"].default is None
    provider = make_provider(probe_sandbox)
    try:
        assert provider.supports_vision() is False
        metadata = describe_llm_provider(provider, prompt_version="t208-prompt-v1")
        assert metadata["provider"] == "openai_compatible"
        assert metadata["model"] == "t208-text-model"
        assert metadata["prompt_version"] == "t208-prompt-v1"
        assert metadata["call_path"] == _MODULE + ".OpenAICompatibleProvider.generate_structured"
        assert_no_secret(probe_sandbox.markers, safe_surfaces(metadata))
        assert "https://" not in str(metadata)
        assert probe_sandbox.scenario.requests == []
    finally:
        asyncio.run(provider.aclose())


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_model", [None, "t208-explicit-model"])
async def test_real_sdk_json_request_uses_model_whitelist_and_unchanged_input(explicit_model, probe_sandbox):
    http = probe_sandbox.http(follow_redirects=False, trust_env=False)
    provider = make_provider(probe_sandbox, http_client=http, temperature=0.4, max_tokens=48)
    messages = deepcopy(_MESSAGES)
    before = deepcopy(messages)
    try:
        result = await provider.generate_structured(messages, TextAnswer, model=explicit_model)
        assert result == TextAnswer(ok=True)
        assert messages == before
        assert len(probe_sandbox.scenario.requests) == 1
        request = probe_sandbox.scenario.requests[0]
        assert request.method == "POST"
        assert str(request.url) == "https://provider.invalid/v1/chat/completions"
        assert_secret_equal(request.headers.get("Authorization"), "Bearer " + probe_sandbox.markers["key"])
        body = json.loads(request.content)
        assert set(body) == {"model", "messages", "response_format", "temperature", "max_tokens"}
        assert body["model"] == (explicit_model or "t208-text-model")
        assert body["response_format"] == {"type": "json_object"}
        assert body["temperature"] == 0.4
        assert body["max_tokens"] == 48
        assert body["messages"][1:] == messages
        instruction = body["messages"][0]
        assert instruction["role"] == "system"
        assert "json" in instruction["content"].lower()
        assert provider.describe()["provider"] == "openai_compatible"
    finally:
        await provider.aclose()
        await http.aclose()


@pytest.mark.asyncio
async def test_existing_json_instruction_is_not_duplicated_and_none_parameters_are_omitted(probe_sandbox):
    http = probe_sandbox.http()
    provider = make_provider(probe_sandbox, http_client=http)
    messages = [{"role": "system", "content": "Return a JSON object."}, *_MESSAGES]
    before = deepcopy(messages)
    try:
        await provider.generate_structured(messages, TextAnswer)
        body = json.loads(probe_sandbox.scenario.requests[0].content)
        assert set(body) == {"model", "messages", "response_format"}
        assert body["messages"] == before == messages
    finally:
        await provider.aclose()
        await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("temperature,max_tokens", [(0.0, 1), (2.0, 32768)])
async def test_text_parameter_boundaries_are_forwarded(temperature, max_tokens, probe_sandbox):
    http = probe_sandbox.http()
    provider = make_provider(probe_sandbox, http_client=http, temperature=temperature, max_tokens=max_tokens)
    try:
        await provider.generate_structured(_MESSAGES, TextAnswer)
        body = json.loads(probe_sandbox.scenario.requests[0].content)
        assert body["temperature"] == temperature
        assert body["max_tokens"] == max_tokens
    finally:
        await provider.aclose()
        await http.aclose()


@pytest.mark.parametrize("parameters", [
    {"temperature": -0.01}, {"temperature": 2.01},
    {"temperature": float("inf")}, {"temperature": float("nan")},
    {"max_tokens": 0}, {"max_tokens": 32769},
    {"reasoning_effort": "medium"},
])
def test_invalid_text_parameters_fail_before_network(parameters, probe_sandbox):
    with pytest.raises((ValueError, ProviderCallError)):
        make_provider(probe_sandbox, **parameters)
    assert probe_sandbox.scenario.requests == []
    assert probe_sandbox.http.clients == []


@pytest.mark.parametrize("options", [
    {"extra_headers": {"x-t208": "not-allowed"}}, {"max_retries": 9},
    {"response_format": {"type": "json_schema"}}, {"tools": []},
])
def test_arbitrary_sdk_options_are_not_public_constructor_parameters(options, probe_sandbox):
    with pytest.raises(TypeError):
        make_provider(probe_sandbox, **options)
    assert probe_sandbox.scenario.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,expected_code", [
    ("missing_choices", "ProviderEmptyResponse"), ("empty_choices", "ProviderEmptyResponse"),
    ("none", "ProviderEmptyResponse"), ("blank", "ProviderEmptyResponse"),
    ("invalid_json", "StructuredOutputFailed"), ("array", "StructuredOutputFailed"),
    ("scalar", "StructuredOutputFailed"), ("null", "StructuredOutputFailed"),
    ("schema", "StructuredOutputFailed"),
])
async def test_empty_json_object_and_local_schema_failures_are_safe(kind, expected_code, probe_sandbox):
    scenario = probe_sandbox.scenario
    if kind == "missing_choices":
        scenario.json_body = {"id": "t208-no-choices"}
    elif kind == "empty_choices":
        scenario.json_body = {"choices": []}
    elif kind == "none":
        scenario.json_body = {"choices": [{"message": {"content": None}}]}
    else:
        scenario.content = {
            "blank": " \n ", "invalid_json": probe_sandbox.markers["body"],
            "array": "[]", "scalar": "7", "null": "null",
            "schema": json.dumps({"ok": probe_sandbox.markers["body"]}),
        }[kind]
    http = probe_sandbox.http()
    provider = make_provider(probe_sandbox, http_client=http)
    try:
        with pytest.raises(_SAFE_ERRORS) as caught:
            await provider.generate_structured(_MESSAGES, TextAnswer)
        assert isinstance(caught.value, (ProviderCallError, ProviderExecutionError))
        assert caught.value.code == expected_code
        assert_safe_error(probe_sandbox, caught.value)
        assert len(scenario.requests) == 1
    finally:
        await provider.aclose()
        await http.aclose()


@pytest.mark.asyncio
async def test_reasoning_content_is_separate_and_empty_content_never_retries(probe_sandbox):
    probe_sandbox.scenario.json_body = {
        "id": "t208-reasoning-only",
        "object": "chat.completion",
        "created": 0,
        "model": "t208-text-model",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "private reasoning must not be the answer",
                },
                "finish_reason": "length",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
    }
    http = probe_sandbox.http()
    provider = make_provider(probe_sandbox, http_client=http)
    try:
        with pytest.raises(_SAFE_ERRORS) as caught:
            await provider.generate_structured(_MESSAGES, TextAnswer)
        assert caught.value.code == "ProviderEmptyResponse"
        assert caught.value.info.retryable is False
        assert len(probe_sandbox.scenario.requests) == 1
        assert "private reasoning" not in str(caught.value)
    finally:
        await provider.aclose()
        await http.aclose()


@pytest.mark.asyncio
async def test_finish_reason_length_with_valid_json_is_rejected_once(probe_sandbox):
    probe_sandbox.scenario.json_body = {
        "id": "t208-truncated",
        "object": "chat.completion",
        "created": 0,
        "model": "t208-text-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": '{"ok": true}'},
                "finish_reason": "length",
            }
        ],
    }
    http = probe_sandbox.http()
    provider = make_provider(probe_sandbox, http_client=http)
    try:
        with pytest.raises(_SAFE_ERRORS) as caught:
            await provider.generate_structured(_MESSAGES, TextAnswer)
        assert caught.value.code == "ProviderOutputTruncated"
        assert caught.value.info.retryable is False
        assert len(probe_sandbox.scenario.requests) == 1
    finally:
        await provider.aclose()
        await http.aclose()


@pytest.mark.asyncio
async def test_reasoning_effort_is_optional_and_forwarded(probe_sandbox):
    http = probe_sandbox.http()
    provider = make_provider(probe_sandbox, http_client=http, reasoning_effort="low")
    try:
        await provider.generate_structured(_MESSAGES, TextAnswer)
        body = json.loads(probe_sandbox.scenario.requests[0].content)
        assert body["reasoning_effort"] == "low"
    finally:
        await provider.aclose()
        await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
async def test_real_sdk_status_failures_are_safe_and_single_attempt_when_requested(status, probe_sandbox):
    probe_sandbox.scenario.status = status
    probe_sandbox.scenario.json_body = {"error": {"message": probe_sandbox.markers["body"]}}
    http = probe_sandbox.http()
    provider = make_provider(probe_sandbox, http_client=http)
    try:
        with pytest.raises(_SAFE_ERRORS) as caught:
            await provider.generate_structured(_MESSAGES, TextAnswer)
        assert_safe_error(probe_sandbox, caught.value)
        assert len(probe_sandbox.scenario.requests) == 1
    finally:
        await provider.aclose()
        await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["connection", "timeout"])
async def test_real_sdk_transport_exception_has_no_secret_message_chain_or_log(kind, probe_sandbox):
    cls = httpx.ConnectError if kind == "connection" else httpx.ReadTimeout
    probe_sandbox.scenario.error = cls(probe_sandbox.markers["body"])
    http = probe_sandbox.http()
    provider = make_provider(probe_sandbox, http_client=http)
    try:
        with pytest.raises(_SAFE_ERRORS) as caught:
            await provider.generate_structured(_MESSAGES, TextAnswer)
        assert_safe_error(probe_sandbox, caught.value)
        assert len(probe_sandbox.scenario.requests) == 1
    finally:
        await provider.aclose()
        await http.aclose()


@pytest.mark.asyncio
async def test_non_sdk_client_is_borrowed_and_its_identity_is_unknown(probe_sandbox):
    close = AsyncMock()
    client = SimpleNamespace(
        close=close,
        chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))],
        )))),
    )
    provider = make_provider(probe_sandbox, client=client)
    try:
        assert await provider.generate_structured(_MESSAGES, TextAnswer) == TextAnswer(ok=True)
        metadata = provider.describe()
        assert metadata["provider"] == metadata["model"] == "unknown"
        assert_no_secret(probe_sandbox.markers, safe_surfaces(metadata))
    finally:
        await provider.aclose()
        await provider.aclose()
    close.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_client_exception_is_safely_wrapped(probe_sandbox):
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=AsyncMock(side_effect=RuntimeError(probe_sandbox.markers["key"])),
    )))
    provider = make_provider(probe_sandbox, client=client)
    try:
        with pytest.raises(_SAFE_ERRORS) as caught:
            await provider.generate_structured(_MESSAGES, TextAnswer)
        assert_safe_error(probe_sandbox, caught.value)
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_from_settings_captures_legacy_fields_but_has_general_provider_identity(probe_sandbox):
    settings = build_test_settings(
        llm_provider="openai_compatible", deepseek_model="t208-captured-model",
        deepseek_base_url="https://captured.invalid/v1", deepseek_api_key=probe_sandbox.markers["key"],
    )
    http = probe_sandbox.http()
    provider = provider_class().from_settings(settings, http_client=http, retry_policy=RetryPolicy(max_retries=0))
    settings.deepseek_model = "t208-changed-model"
    settings.deepseek_base_url = AnyUrl("https://changed.invalid/v1")
    settings.deepseek_api_key = SecretStr(probe_sandbox.markers["body"])
    try:
        await provider.generate_structured(_MESSAGES, TextAnswer)
        request = probe_sandbox.scenario.requests[0]
        assert str(request.url) == "https://captured.invalid/v1/chat/completions"
        assert json.loads(request.content)["model"] == "t208-captured-model"
        assert_secret_equal(request.headers.get("Authorization"), "Bearer " + probe_sandbox.markers["key"])
        assert provider.describe()["provider"] == "openai_compatible"
        assert provider.describe()["model"] == "t208-captured-model"
    finally:
        await provider.aclose()
        await http.aclose()


class UnreadSecret(SecretStr):
    def get_secret_value(self) -> str:
        pytest.fail("auth_none must not read the supplied secret", pytrace=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("supplied_key", [False, True])
async def test_auth_none_never_reads_environment_keys_or_sends_authorization(supplied_key, probe_sandbox, monkeypatch):
    cls = provider_class()
    key_names = {"OPENAI_API_KEY", "OPENAI_ADMIN_KEY", "DEEPSEEK_API_KEY"}
    for name in key_names:
        monkeypatch.setenv(name, probe_sandbox.markers["key"])
    original_get = os.environ.get

    def guarded_get(name: str, default: Any = None) -> Any:
        if name in key_names:
            pytest.fail("auth_none attempted to read an environment credential", pytrace=False)
        return original_get(name, default)

    monkeypatch.setattr(os.environ, "get", guarded_get)
    http = probe_sandbox.http()
    provider = cls(
        model="t208-local", base_url="http://127.0.0.1:18181/v1", auth_mode="none",
        api_key=UnreadSecret(probe_sandbox.markers["key"]) if supplied_key else None,
        http_client=http, retry_policy=RetryPolicy(max_retries=0),
    )
    try:
        result = await provider.generate_structured(_MESSAGES, TextAnswer)
        assert result == TextAnswer(ok=True)
        assert len(probe_sandbox.scenario.requests) == 1
        assert "Authorization" not in probe_sandbox.scenario.requests[0].headers
    finally:
        await provider.aclose()
        await http.aclose()


@pytest.mark.asyncio
async def test_auth_none_also_omits_authorization_with_borrowed_real_sdk(probe_sandbox):
    http = probe_sandbox.http()
    client = AsyncOpenAI(
        api_key=probe_sandbox.markers["key"], base_url="https://provider.invalid/v1",
        http_client=http, max_retries=0,
    )
    provider = make_provider(probe_sandbox, client=client, auth_mode="none", api_key=None)
    try:
        await provider.generate_structured(_MESSAGES, TextAnswer)
        assert "Authorization" not in probe_sandbox.scenario.requests[0].headers
        await provider.aclose()
        assert not client.is_closed()
    finally:
        await provider.aclose()
        await client.close()


@pytest.mark.parametrize("api_key", [None, "", "   "])
def test_api_key_auth_rejects_missing_key_without_environment_fallback(api_key, probe_sandbox, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", probe_sandbox.markers["key"])
    with pytest.raises((ValueError, ProviderCallError)):
        make_provider(probe_sandbox, api_key=api_key)
    assert probe_sandbox.scenario.requests == []
    assert probe_sandbox.http.clients == []


@pytest.mark.asyncio
@pytest.mark.parametrize("block", [
    {"type": "image", "image": {"value": "synthetic"}},
    {"type": "image_url", "image_url": {"url": "https://image.invalid/t208.png"}},
    {"type": "input_image", "image_url": "https://image.invalid/t208.png"},
    {"type": "input_audio", "input_audio": {"data": "synthetic", "format": "wav"}},
    {"type": "file", "file": {"file_data": "synthetic"}},
    {"type": "video_url", "video_url": {"url": "https://video.invalid/t208.mp4"}},
])
async def test_all_multimodal_content_is_rejected_before_client_or_network(block, probe_sandbox, monkeypatch):
    observed = observe_owned_clients(probe_sandbox, monkeypatch)
    provider = make_provider(probe_sandbox)
    messages = [{"role": "user", "content": [block]}]
    before = deepcopy(messages)
    try:
        with pytest.raises(_SAFE_ERRORS) as caught:
            await provider.generate_structured(messages, TextAnswer, model="t208-vision-looking-name")
        assert_safe_error(probe_sandbox, caught.value)
        assert messages == before
        assert probe_sandbox.scenario.requests == []
        assert observed.clients == []
        assert provider.supports_vision() is False
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_multimodal_message_fields_cannot_bypass_text_content_guard(probe_sandbox):
    provider = make_provider(probe_sandbox)
    try:
        with pytest.raises(_SAFE_ERRORS):
            await provider.generate_structured(
                [{"role": "assistant", "content": "T208 text", "audio": {"id": "t208-audio"}}], TextAnswer,
            )
        assert probe_sandbox.scenario.requests == []
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_non_basemodel_schema_is_rejected_locally(probe_sandbox):
    provider = make_provider(probe_sandbox)
    try:
        with pytest.raises(TypeError):
            await provider.generate_structured(_MESSAGES, dict)
        assert probe_sandbox.scenario.requests == []
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_owned_sdk_and_http_close_exactly_once_and_no_use_after_close(probe_sandbox, monkeypatch):
    observed = observe_owned_clients(probe_sandbox, monkeypatch)
    provider = make_provider(probe_sandbox)
    assert observed.clients == []
    try:
        await provider.generate_structured(_MESSAGES, TextAnswer)
    finally:
        await provider.aclose()
        await provider.aclose()
    assert len(observed.sdk_closes) == 1
    assert observed.sdk_closes[0].is_closed()
    assert len(observed.clients) == 1
    assert observed.clients[0].is_closed
    assert observed.http_closes.count(observed.clients[0]) == 1
    assert observed.options[0].get("follow_redirects", False) is False
    with pytest.raises((*_SAFE_ERRORS, RuntimeError)):
        await provider.generate_structured(_MESSAGES, TextAnswer)
    assert len(probe_sandbox.scenario.requests) == 1


@pytest.mark.asyncio
async def test_owned_sdk_closes_without_closing_borrowed_http_pool(probe_sandbox, monkeypatch):
    http = probe_sandbox.http()
    observed = observe_owned_clients(probe_sandbox, monkeypatch)
    provider = make_provider(probe_sandbox, http_client=http)
    try:
        await provider.generate_structured(_MESSAGES, TextAnswer)
        await provider.aclose()
        await provider.aclose()
        assert len(observed.sdk_closes) == 1
        assert observed.sdk_closes[0].is_closed()
        assert not http.is_closed
        assert http.close_calls == 0
    finally:
        await provider.aclose()
        await http.aclose()


@pytest.mark.asyncio
async def test_borrowed_real_sdk_and_its_http_remain_open(probe_sandbox):
    http = probe_sandbox.http()
    client = AsyncOpenAI(
        api_key=probe_sandbox.markers["key"], base_url="https://provider.invalid/v1",
        http_client=http, max_retries=0,
    )
    provider = make_provider(probe_sandbox, client=client)
    try:
        await provider.generate_structured(_MESSAGES, TextAnswer)
        assert provider.describe()["provider"] == "openai_compatible"
        await provider.aclose()
        await provider.aclose()
        assert not client.is_closed()
        assert not http.is_closed
        assert http.close_calls == 0
    finally:
        await provider.aclose()
        await client.close()


@pytest.mark.asyncio
async def test_unused_close_never_constructs_sdk_or_http(probe_sandbox, monkeypatch):
    observed = observe_owned_clients(probe_sandbox, monkeypatch)
    provider = make_provider(probe_sandbox)
    await provider.aclose()
    await provider.aclose()
    assert observed.clients == observed.sdk_closes == []
    assert probe_sandbox.scenario.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("borrowed_http", [False, True])
async def test_inflight_cancellation_propagates_and_owned_resources_can_be_closed(borrowed_http, probe_sandbox, monkeypatch):
    observed = observe_owned_clients(probe_sandbox, monkeypatch)
    probe_sandbox.scenario.block = True
    http = probe_sandbox.http() if borrowed_http else None
    provider = make_provider(probe_sandbox, http_client=http)
    task = asyncio.create_task(provider.generate_structured(_MESSAGES, TextAnswer))
    try:
        await asyncio.wait_for(probe_sandbox.scenario.started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert probe_sandbox.scenario.finished.is_set()
        assert len(probe_sandbox.scenario.requests) == 1
        await provider.aclose()
        await provider.aclose()
        assert len(observed.sdk_closes) == 1
        assert observed.sdk_closes[0].is_closed()
        if http is not None:
            assert not http.is_closed
            assert http.close_calls == 0
        else:
            assert observed.clients and all(client.is_closed for client in observed.clients)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await provider.aclose()
        if http is not None:
            await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("borrowed_http", [False, True])
@pytest.mark.parametrize("location", ["https://other.invalid/t208", "/v1/redirected"])
async def test_redirects_never_create_a_second_request(location, borrowed_http, probe_sandbox, monkeypatch):
    observe_owned_clients(probe_sandbox, monkeypatch)
    scenario = probe_sandbox.scenario
    scenario.status = 307
    scenario.headers = {"location": location}
    http = probe_sandbox.http(follow_redirects=True) if borrowed_http else None
    provider = make_provider(probe_sandbox, http_client=http)
    try:
        with pytest.raises(_SAFE_ERRORS) as caught:
            await provider.generate_structured(_MESSAGES, TextAnswer)
        assert_safe_error(probe_sandbox, caught.value)
        assert len(scenario.requests) == 1
        assert scenario.requests[0].url.host == "provider.invalid"
        if http is not None:
            assert http.follow_redirects is True
    finally:
        await provider.aclose()
        if http is not None:
            await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("use_default_policy", [False, True])
async def test_business_retry_remains_available_outside_single_attempt_probe_policy(use_default_policy, probe_sandbox):
    scenario = probe_sandbox.scenario
    scenario.status = 500

    async def first_failure_then_success(request: httpx.Request) -> httpx.Response:
        response = await scenario.respond(request)
        scenario.status = 200
        return response

    http = httpx.AsyncClient(transport=httpx.MockTransport(first_failure_then_success))
    policy = None if use_default_policy else RetryPolicy(max_retries=1, backoff_seconds=(0,), jitter_seconds=0)
    provider = make_provider(probe_sandbox, http_client=http, retry_policy=policy)
    try:
        assert await provider.generate_structured(_MESSAGES, TextAnswer) == TextAnswer(ok=True)
        assert len(scenario.requests) == 2
        assert all(request.url.path == "/v1/chat/completions" for request in scenario.requests)
    finally:
        await provider.aclose()
        await http.aclose()
