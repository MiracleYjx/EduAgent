"""T208 / TCR-010: executable registry, captured profiles and binding boundaries."""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import AnyUrl, SecretStr

from backend.app.ai.embedding.factory import (
    EmbeddingProviderFactory,
    get_embedding_provider_factory,
)
from backend.app.ai.llm.factory import LLMProviderFactory, get_llm_provider_factory
from backend.app.core import config
from backend.app.schemas.settings import ProviderProfile
from tests.support.credential_fakes import (
    assert_no_secret,
    assert_secret_equal,
    error_surfaces,
    safe_surfaces,
)
from tests.support.provider_probe_fakes import (
    MinimalAnswer,
    RecordingLLM,
    make_profile,
    probe_sandbox,
    registry,
    required_symbol,
    resolve_binding,
    validate_profile,
    view,
)
from tests.unit.settings_helpers import build_test_settings

pytestmark = pytest.mark.usefixtures(probe_sandbox.__name__)
MODULE = "backend.app.services.settings.provider_registry"


def _default(profile: dict) -> dict:
    return {"default_llm": {"task_kind": "default_llm", "profile_id": profile["profile_id"],
                            "profile_revision": profile["profile_revision"]}}


def test_profile_parameters_round_trip_is_nonsecret_and_not_verified(probe_sandbox):
    cls = required_symbol(MODULE, "TextParameters")
    profile = ProviderProfile.model_validate({**probe_sandbox.profile, "parameters": {"temperature": 0.25, "max_tokens": 37}})
    value = view(registry().validate_profile(profile))
    assert profile.parameters == {"temperature": 0.25, "max_tokens": 37}
    assert cls.model_validate(profile.parameters).temperature == 0.25
    assert value["parameters"]["max_tokens"] == 37
    assert value["verified_capabilities"] == ()
    assert_no_secret(probe_sandbox.markers, safe_surfaces(value))


@pytest.mark.parametrize("parameters", [
    {"temperature": True}, {"temperature": -0.01}, {"temperature": 2.01},
    {"temperature": float("nan")}, {"temperature": float("inf")},
    {"max_tokens": 0}, {"max_tokens": 32769}, {"max_tokens": True},
    {"max_tokens": 1.5}, {"extra_headers": {"Authorization": "not-accepted"}},
])
def test_runtime_parameter_schema_rejects_unsafe_or_unsupported_values(parameters, probe_sandbox):
    with pytest.raises(ValueError):
        validate_profile(registry(), {**probe_sandbox.profile, "parameters": parameters})


@pytest.mark.parametrize("changes", [
    {"verified_capabilities": ("json_object",)},
    {"declared_capabilities": ("vision",)},
    {"base_url": "https://user:password@provider.invalid/v1"},
    {"base_url": "https://provider.invalid/v1?key=hidden"},
    {"base_url": "http://remote.invalid/v1"},
    {"auth_mode": "none"},
])
def test_profile_cannot_supply_verified_fact_or_unsafe_endpoint(changes, probe_sandbox):
    with pytest.raises(ValueError):
        validate_profile(registry(), {**probe_sandbox.profile, **changes})


def test_registry_create_captures_profile_not_global_settings(probe_sandbox, monkeypatch):
    owner = registry()
    profile = {**probe_sandbox.profile, "model_id": "t208-captured-model", "parameters": {"temperature": 0.25, "max_tokens": 37}}
    before = deepcopy(profile)

    def no_settings():
        pytest.fail("profile construction consulted global AppSettings", pytrace=False)

    monkeypatch.setattr(config, "get_settings", no_settings)

    async def exercise():
        client = probe_sandbox.http(follow_redirects=False, trust_env=False)
        provider = owner.create(profile, kind="llm", api_key=SecretStr(probe_sandbox.markers["key"]), http_client=client)
        try:
            result = await provider.generate_structured([{"role": "user", "content": "synthetic only"}], MinimalAnswer)
            assert isinstance(result, MinimalAnswer) and result.ok
            metadata = provider.describe()
            assert metadata["provider"] == "openai_compatible"
            assert metadata["model"] == "t208-captured-model"
        finally:
            await provider.aclose()
            assert not client.is_closed
            await client.aclose()

    asyncio.run(exercise())
    assert profile == before
    request = probe_sandbox.scenario.requests[0]
    payload = json.loads(request.content)
    assert request.url.path == "/v1/chat/completions"
    assert payload["model"] == "t208-captured-model"
    assert payload["temperature"] == 0.25 and payload["max_tokens"] == 37
    assert_secret_equal(request.headers["Authorization"], "Bearer " + probe_sandbox.markers["key"])


@pytest.mark.parametrize("kind", ["llm", "embedding", "rerank", "vision", "ocr"])
def test_registry_legacy_builders_are_same_records_as_presented_schema(kind):
    owner = registry()
    entries = owner.entries(kind=kind)
    assert entries
    for entry in entries:
        assert entry is owner.entry(kind, entry.adapter_id)
        assert callable(entry.factory) and callable(entry.legacy_factory) and callable(entry.probe_factory)
        assert entry.config_schema.model_config["extra"] == "forbid"
        assert entry.lifecycle
    if kind == "rerank":
        assert owner.entry(kind, "openai_compatible").canonical_id == "llm"
        assert owner.entry(kind, "openai_compatible").supported_capabilities == frozenset({"llm_rerank"})


def test_default_registration_does_not_suppress_builtins_and_replacement_is_conservative(monkeypatch):
    owner = registry()
    module = importlib.import_module(MODULE)
    monkeypatch.setattr(module, "get_provider_registry", lambda: owner)
    marker = RecordingLLM()
    factory = get_llm_provider_factory()
    settings = build_test_settings()
    factory.register("t208_extra", lambda _settings: marker)
    assert {"deepseek", "openai_compatible", "t208_extra"} <= set(factory.supported_providers())
    factory.register("deepseek", lambda _settings: marker, replace=True)
    assert factory.create(settings) is marker
    entry = owner.entry("llm", "deepseek")
    assert not entry.supported_capabilities
    with pytest.raises(ValueError):
        owner.validate_profile(make_profile(uuid4(), adapter_id="deepseek"))


def test_injected_factories_stay_local_and_default_names_match_configuration(monkeypatch):
    owner = registry()
    module = importlib.import_module(MODULE)
    monkeypatch.setattr(module, "get_provider_registry", lambda: owner)
    llm = RecordingLLM()
    custom = LLMProviderFactory({"deepseek": lambda _settings: llm})
    custom.register("local_stub", lambda _settings: llm)
    assert custom.supported_providers() == ("deepseek", "local_stub")
    assert "local_stub" not in get_llm_provider_factory().supported_providers()
    assert EmbeddingProviderFactory().supported_providers() == ()
    assert config.AppSettings.SUPPORTED_LLM_PROVIDERS == frozenset(get_llm_provider_factory().supported_providers())
    assert config.AppSettings.SUPPORTED_EMBEDDING_PROVIDERS == frozenset(get_embedding_provider_factory().supported_providers())
    assert "deepseek" not in config.AppSettings.SUPPORTED_EMBEDDING_PROVIDERS


@pytest.mark.parametrize("task", ["diagnosis", "content_validation", "paper_extraction"])
def test_inherited_tasks_reject_hidden_override(task, probe_sandbox):
    default = probe_sandbox.profile
    other = make_profile(probe_sandbox.environment_id)
    bindings = _default(default)
    bindings[task] = {"task_kind": task, "profile_id": other["profile_id"], "profile_revision": other["profile_revision"]}
    with pytest.raises(ValueError):
        resolve_binding(registry(), task, {str(p["profile_id"]): p for p in (default, other)}, bindings)


@pytest.mark.parametrize("task", ["embedding", "vision", "ocr"])
def test_independent_task_missing_binding_never_uses_default(task, probe_sandbox):
    profile = probe_sandbox.profile
    with pytest.raises(ValueError):
        resolve_binding(registry(), task, {str(profile["profile_id"]): profile}, _default(profile))


@pytest.mark.parametrize("changes", [
    {"profile_revision": uuid4()}, {"profile_revision": None},
    {"task_kind": "subjective_grading"}, {"inherit_default": True},
])
def test_binding_rejects_wrong_or_ambiguous_revision(changes, probe_sandbox):
    profile = probe_sandbox.profile
    bindings = _default(profile)
    bindings["default_llm"].update(changes)
    with pytest.raises(ValueError):
        resolve_binding(registry(), "question_generation", {str(profile["profile_id"]): profile}, bindings)


def test_empty_required_capabilities_cannot_weaken_text_contract(probe_sandbox):
    profile = {**probe_sandbox.profile, "declared_capabilities": ("text",)}
    bindings = _default(profile)
    bindings["default_llm"]["required_capabilities"] = ()
    with pytest.raises(ValueError):
        resolve_binding(registry(), "question_generation", {str(profile["profile_id"]): profile}, bindings)


def test_reviewer_resolution_is_rule_only_without_constructing_or_reading_settings(monkeypatch):
    owner = registry()

    def reject(*_args, **_kwargs):
        pytest.fail("rule reviewer acquired a model", pytrace=False)

    monkeypatch.setattr(owner, "create", reject)
    monkeypatch.setattr(config, "get_settings", reject)
    result = resolve_binding(owner, "reviewer", {}, {})
    assert result["mode"] == "rule_based" and result["profile_id"] is None
    assert result["dependencies"] == ()


def test_local_embedding_separates_logical_identity_from_preloaded_directory(tmp_path):
    owner = registry()
    directory = tmp_path / "preloaded"
    directory.mkdir()
    calls: list[str] = []

    def loader(value):
        calls.append(value)
        return SimpleNamespace(get_embedding_dimension=lambda: 2, encode=lambda texts, **_kwargs: [[0.25, 0.75] for _ in texts])

    profile = make_profile(uuid4(), adapter_id="local", model_id="t208-logical-id", declared_capabilities=("embedding",),
                           resource_directory=directory, base_url=None, auth_mode="none", credential_ref=None,
                           parameters={"dimension": 2})
    provider = owner.create(profile, kind="embedding", model_loader=loader)
    assert provider.describe()["model"] == "t208-logical-id"
    assert calls == []
    assert asyncio.run(provider.embed_query("fixed synthetic")) == [0.25, 0.75]
    assert calls == [str(directory)]


@pytest.mark.parametrize("adapter,capability,kind", [("bge", "embedding", "embedding"), ("cross_encoder", "local_rerank", "rerank")])
def test_missing_preloaded_resources_fail_without_loader_or_download(adapter, capability, kind, probe_sandbox):
    owner = registry()
    calls: list[Any] = []
    profile = make_profile(uuid4(), adapter_id=adapter, declared_capabilities=(capability,), base_url=None,
                           auth_mode="none", credential_ref=None, resource_directory=None)
    with pytest.raises(ValueError):
        owner.create(profile, kind=kind, model_loader=lambda value: calls.append(value))
    assert calls == [] and probe_sandbox.scenario.requests == []


def test_default_local_model_loaders_explicitly_disable_downloads(tmp_path, monkeypatch):
    registry()  # Keep missing-entry failures at the product seam, not collection.
    from backend.app.ai.embedding.providers import local
    from backend.app.ai.retrieval import reranker

    calls = []
    def record(kind, model, kwargs):
        calls.append((kind, model, kwargs))
        return object()

    fake_sdk = SimpleNamespace(
        SentenceTransformer=lambda model, **kwargs: record("embedding", model, kwargs),
        CrossEncoder=lambda model, **kwargs: record("rerank", model, kwargs),
    )
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_sdk)
    monkeypatch.setattr(local, "sentence_transformers_available", lambda: True)
    monkeypatch.setattr(reranker, "sentence_transformers_available", lambda: True)
    local.load_sentence_transformer(str(tmp_path))
    reranker._load_cross_encoder(str(tmp_path))
    assert len(calls) == 2
    assert all(kwargs["local_files_only"] is True for _kind, _model, kwargs in calls)


def test_ocr_registry_preserves_lazy_not_ready_and_no_sdk_import(tmp_path):
    owner = registry()
    profile = make_profile(uuid4(), adapter_id="rapidocr", model_id="PP-OCRv5-mobile", declared_capabilities=("ocr",),
                           base_url=None, auth_mode="none", credential_ref=None, resource_directory=tmp_path / "missing-models")
    provider = owner.create(profile, kind="ocr")
    assert not provider.describe().ready
    assert provider.describe().provider == "rapidocr"
    assert provider.describe().model == "PP-OCRv5-mobile"


@pytest.mark.parametrize("base_url,supported", [
    ("https://api.deepseek.com", True), ("https://api.deepseek.com/v1", True),
    ("http://api.deepseek.com", False), ("https://api.deepseek.com:444/v1", False),
    ("https://api.deepseek.com/unrelated", False), ("https://gateway.invalid/v1", False),
])
def test_deepseek_vision_gate_uses_captured_model_and_endpoint(base_url, supported):
    registry()
    from backend.app.ai.llm.deepseek import DeepSeekProvider

    settings = build_test_settings(deepseek_model="deepseek-flash", deepseek_base_url=base_url)
    provider = DeepSeekProvider(settings, client=object())
    settings.deepseek_base_url = AnyUrl("https://changed.invalid/v1")
    settings.deepseek_model = "deepseek-chat"
    assert provider.supports_vision() is supported
    asyncio.run(provider.aclose())


def test_registry_deepseek_construction_checks_actual_vision_model_before_client(probe_sandbox):
    owner = registry()
    profile = {**probe_sandbox.profile, "adapter_id": "deepseek", "model_id": "deepseek-chat",
               "base_url": "https://api.deepseek.com/v1", "declared_capabilities": ("vision",)}
    with pytest.raises(ValueError):
        owner.create(profile, kind="vision", api_key=SecretStr(probe_sandbox.markers["key"]))
    assert probe_sandbox.http.clients == [] and probe_sandbox.scenario.requests == []


def test_unknown_task_and_retrieval_mode_are_rejected(probe_sandbox):
    owner = registry()
    profile = probe_sandbox.profile
    profiles = {str(profile["profile_id"]): profile}
    with pytest.raises(ValueError):
        resolve_binding(owner, "unimplemented_task", profiles, _default(profile))
    bindings = _default(profile)
    bindings["question_generation"] = {"task_kind": "question_generation", "inherit_default": True, "retrieval_mode": "telepathy"}
    with pytest.raises(ValueError):
        resolve_binding(owner, "question_generation", profiles, bindings)


@pytest.mark.parametrize("case", ["remote_http", "auth_conflict", "wrong_purpose", "wrong_environment", "relative_resource"])
def test_review_binding_and_profile_share_the_same_pure_rejections(case, probe_sandbox):
    from pathlib import Path

    cls = required_symbol(MODULE, "ProviderRegistry")
    owner = cls(environment_id=probe_sandbox.environment_id)
    profile = deepcopy(probe_sandbox.profile)
    if case == "remote_http":
        profile["base_url"] = "http://remote.invalid/v1"
    elif case == "auth_conflict":
        profile["auth_mode"] = "none"
    elif case == "wrong_purpose":
        profile["credential_ref"] = profile["credential_ref"].model_copy(update={"purpose": "jwt"})
    elif case == "wrong_environment":
        profile["credential_ref"] = profile["credential_ref"].model_copy(update={"environment_id": uuid4()})
    else:
        profile["resource_directory"] = Path("relative-resource")
    with pytest.raises(ValueError):
        validate_profile(owner, profile)
    with pytest.raises(ValueError):
        resolve_binding(owner, "question_generation", {str(profile["profile_id"]): profile}, _default(profile))
    assert probe_sandbox.credentials.calls == [] and probe_sandbox.http.clients == []


def test_rerank_can_inherit_default_without_inventing_cloud_rerank(probe_sandbox):
    profile = probe_sandbox.profile
    result = resolve_binding(registry(), "rerank", {str(profile["profile_id"]): profile}, _default(profile))
    assert result["profile_id"] == profile["profile_id"]
    assert set(result["required_capabilities"]) == {"text", "json_object"}
    assert probe_sandbox.scenario.requests == []


def test_local_rerank_binding_is_independent_of_default(probe_sandbox, tmp_path):
    owner = registry()
    local = make_profile(probe_sandbox.environment_id, adapter_id="cross_encoder", model_id="t208-local-rerank",
                         declared_capabilities=("local_rerank",), base_url=None, auth_mode="none", credential_ref=None,
                         resource_directory=tmp_path)
    bindings = {"rerank": {"task_kind": "rerank", "profile_id": local["profile_id"], "profile_revision": local["profile_revision"]}}
    result = resolve_binding(owner, "rerank", {str(local["profile_id"]): local}, bindings)
    assert result["profile_id"] == local["profile_id"]
    assert set(result["required_capabilities"]) == {"local_rerank"}
    calls = []
    def load(value):
        calls.append(value)
        return object()

    provider = owner.create(local, kind="rerank", model_loader=load)
    assert provider.describe()["model"] == "t208-local-rerank"
    assert provider._ensure_model() is not None
    assert calls == [str(tmp_path)]


def test_local_rerank_does_not_forward_snapshot_credential_store_to_model(probe_sandbox, tmp_path):
    owner = registry()
    local = make_profile(probe_sandbox.environment_id, adapter_id="cross_encoder", model_id="t208-local-rerank",
        declared_capabilities=("local_rerank",), base_url=None, auth_mode="none", credential_ref=None,
        resource_directory=tmp_path)
    loaded = []

    def load(value):
        loaded.append(value)
        return object()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("local rerank must not read credential storage")

    provider = owner.create(local, kind="rerank", credential_store=SimpleNamespace(get=forbidden), model_loader=load)
    assert loaded == []
    assert provider.describe()["model"] == "t208-local-rerank"
    assert provider._ensure_model() is not None
    assert loaded == [str(tmp_path)]
    assert probe_sandbox.scenario.requests == []


@pytest.mark.parametrize("owns", [False, True])
def test_rerank_legacy_record_releases_only_its_owned_provider(owns):
    owner = registry()
    provider = RecordingLLM()
    settings = build_test_settings(rerank_provider="llm")
    arguments: dict[str, Any] = {"model": "t208-rerank", "max_candidates": 2, "timeout": 15.0}
    if owns:
        arguments["llm_factory"] = lambda *_args: provider
    else:
        arguments["provider"] = provider
    adapter = owner.entry("rerank", "llm").legacy_factory(settings, **arguments)
    asyncio.run(adapter.aclose())
    asyncio.run(adapter.aclose())
    assert provider.close_calls == (1 if owns else 0)


def test_registry_probe_factory_constructs_single_attempt_without_claiming_probe_service(probe_sandbox):
    owner = registry()
    probe_sandbox.scenario.status = 500
    probe_sandbox.scenario.json_body = {"error": {"message": "synthetic upstream failure"}}
    from backend.app.core.retry_policy import ProviderExecutionError

    async def exercise():
        client = probe_sandbox.http()
        provider = owner.create(probe_sandbox.profile, kind="llm", probe=True,
                                api_key=SecretStr(probe_sandbox.markers["key"]), http_client=client)
        try:
            with pytest.raises(ProviderExecutionError):
                await provider.generate_structured([{"role": "user", "content": "fixed synthetic"}], MinimalAnswer)
            assert len(probe_sandbox.scenario.requests) == 1
            assert not client.is_closed
        finally:
            await provider.aclose()
            await client.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize("parameter", ["api_key", "authorization", "secret_value", "extra_headers", "arbitrary_sdk_kwargs"])
def test_review_nonsecret_profile_rejects_plain_secret_or_sdk_parameter(parameter, probe_sandbox):
    with pytest.raises(ValueError) as caught:
        ProviderProfile.model_validate({**probe_sandbox.profile, "parameters": {parameter: probe_sandbox.markers["key"]}})
    assert_no_secret(probe_sandbox.markers, safe_surfaces(caught.value))


def test_review_nonsecret_resolver_does_not_return_a_secret_parameter(probe_sandbox):
    profile = {**probe_sandbox.profile, "parameters": {"api_key": probe_sandbox.markers["key"]}}
    result = config.resolve_configuration(saved={"providers": {str(profile["profile_id"]): profile}}, process_env={})
    if not result.errors:
        pytest.fail("nonsecret resolver admitted an unregistered secret parameter", pytrace=False)
    assert "providers" not in result.segments
    assert_no_secret(probe_sandbox.markers, safe_surfaces(result))


def _embedding_body():
    return {"object": "list", "model": "t208-embedding-model",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.25, 0.75]}],
            "usage": {"prompt_tokens": 1, "total_tokens": 1}}


@pytest.mark.parametrize("borrowed_sdk", [False, True])
def test_cloud_embedding_none_ignores_ambient_credentials_and_borrowed_headers(borrowed_sdk, probe_sandbox, monkeypatch):
    from backend.app.ai.embedding.providers.openai_compatible import (
        OpenAICompatibleEmbeddingProvider,
    )

    probe_sandbox.scenario.json_body = _embedding_body()
    names = {"OPENAI_API_KEY", "OPENAI_ADMIN_KEY", "OPENAI_WEBHOOK_SECRET"}
    for name in names:
        monkeypatch.setenv(name, probe_sandbox.markers["key"])
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", '{"X-SDK-Ambient":"not-a-request-option"}')
    original_get = os.environ.get

    def guarded_get(name, default=None):
        if name in names:
            pytest.fail("anonymous Embedding read an environment secret", pytrace=False)
        return original_get(name, default)

    monkeypatch.setattr(os.environ, "get", guarded_get)

    async def exercise():
        http = probe_sandbox.http(headers={"Authorization": "caller-default", "X-Pool-Ambient": "not-a-request-option"},
                                  auth=httpx.BasicAuth("synthetic-user", "synthetic-password"), follow_redirects=True)
        sdk = AsyncOpenAI(api_key="synthetic-borrowed-sdk", admin_api_key="", webhook_secret="",
                          base_url="https://provider.invalid/v1", http_client=http, max_retries=2) if borrowed_sdk else None
        provider = OpenAICompatibleEmbeddingProvider(model="t208-embedding-model", api_key="", auth_mode="none",
                   base_url="http://127.0.0.1:18181/v1", dimension=2, max_retries=0,
                   client=sdk, http_client=None if sdk is not None else http)
        try:
            assert await provider.embed_query("fixed synthetic embedding") == [0.25, 0.75]
            request = probe_sandbox.scenario.requests[0]
            assert request.url.path == "/v1/embeddings"
            assert request.url.host == "127.0.0.1"
            if any(name in request.headers for name in ("Authorization", "X-SDK-Ambient", "X-Pool-Ambient")):
                pytest.fail("anonymous Embedding forwarded an uncontrolled header", pytrace=False)
            assert json.loads(request.content)["model"] == "t208-embedding-model"
            await provider.aclose()
            await provider.aclose()
            assert not http.is_closed
            if sdk is not None:
                assert not sdk.is_closed()
        finally:
            await provider.aclose()
            if sdk is not None:
                await sdk.close()
            else:
                await http.aclose()

    asyncio.run(exercise())


def test_cloud_embedding_probe_record_overrides_borrowed_sdk_retry_policy(probe_sandbox):
    from backend.app.ai.embedding.base import EmbeddingProviderError

    owner = registry()
    probe_sandbox.scenario.status = 500
    probe_sandbox.scenario.json_body = {"error": {"message": "fixed synthetic upstream failure"}}
    profile = {**probe_sandbox.profile, "model_id": "t208-embedding-model", "declared_capabilities": ("embedding",), "parameters": {"dimension": 2}}

    async def exercise():
        http = probe_sandbox.http()
        sdk = AsyncOpenAI(api_key="different-synthetic-sdk-key", base_url="https://different.invalid/v1", http_client=http, max_retries=2)
        provider = owner.create(profile, kind="embedding", probe=True, client=sdk,
                                api_key=SecretStr(probe_sandbox.markers["key"]))
        try:
            with pytest.raises(EmbeddingProviderError):
                await provider.embed_query("fixed synthetic embedding")
            assert len(probe_sandbox.scenario.requests) == 1
            request = probe_sandbox.scenario.requests[0]
            assert request.url.host == "provider.invalid"
            assert_secret_equal(request.headers.get("Authorization"), "Bearer " + probe_sandbox.markers["key"])
            await provider.aclose()
            assert not sdk.is_closed() and not http.is_closed
        finally:
            await provider.aclose()
            await sdk.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("anonymous", [False, True])
def test_cloud_embedding_does_not_follow_redirect_from_borrowed_pool(anonymous, probe_sandbox):
    from backend.app.ai.embedding.base import EmbeddingProviderError
    from backend.app.ai.embedding.providers.openai_compatible import (
        OpenAICompatibleEmbeddingProvider,
    )

    probe_sandbox.scenario.status = 307
    probe_sandbox.scenario.headers = {"location": "https://other.invalid/embedding"}

    async def exercise():
        http = probe_sandbox.http(follow_redirects=True)
        provider = OpenAICompatibleEmbeddingProvider(model="t208-embedding-model", base_url="https://provider.invalid/v1",
                   api_key="" if anonymous else probe_sandbox.markers["key"], auth_mode="none" if anonymous else "api_key",
                   http_client=http, max_retries=0)
        try:
            with pytest.raises(EmbeddingProviderError):
                await provider.embed_query("fixed synthetic embedding")
            assert len(probe_sandbox.scenario.requests) == 1
            assert probe_sandbox.scenario.requests[0].url.host == "provider.invalid"
        finally:
            await provider.aclose()
            assert not http.is_closed
            await http.aclose()

    asyncio.run(exercise())


def test_cloud_embedding_owned_http_and_sdk_close_once(probe_sandbox, monkeypatch):
    from backend.app.ai.embedding.base import EmbeddingProviderNotReadyError
    from backend.app.ai.embedding.providers.openai_compatible import (
        OpenAICompatibleEmbeddingProvider,
    )

    probe_sandbox.scenario.json_body = _embedding_body()
    clients: list[httpx.AsyncClient] = []
    closes: list[httpx.AsyncClient] = []
    original_init = httpx.AsyncClient.__init__
    original_close = httpx.AsyncClient.aclose

    def initialize(client, *args, **kwargs):
        if "transport" not in kwargs:
            kwargs["transport"] = httpx.MockTransport(probe_sandbox.scenario.respond)
            clients.append(client)
        original_init(client, *args, **kwargs)

    async def close(client):
        closes.append(client)
        await original_close(client)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", initialize)
    monkeypatch.setattr(httpx.AsyncClient, "aclose", close)

    async def exercise():
        provider = OpenAICompatibleEmbeddingProvider(model="t208-embedding-model", base_url="https://provider.invalid/v1",
                   api_key=probe_sandbox.markers["key"], dimension=2, max_retries=0)
        assert await provider.embed_query("fixed synthetic embedding") == [0.25, 0.75]
        await provider.aclose()
        await provider.aclose()
        assert len(clients) == 1 and clients[0].is_closed
        assert closes.count(clients[0]) == 1
        with pytest.raises(EmbeddingProviderNotReadyError):
            await provider.embed_query("fixed synthetic embedding")
        assert len(probe_sandbox.scenario.requests) == 1

    asyncio.run(exercise())


@pytest.mark.parametrize("random_detail", [False, True])
def test_cloud_embedding_safe_failure_hides_sdk_chain_and_debug_body(random_detail, probe_sandbox, caplog):
    from backend.app.ai.embedding.base import EmbeddingProviderError
    from backend.app.ai.embedding.providers.openai_compatible import (
        OpenAICompatibleEmbeddingProvider,
    )

    # The original red-light input remains; random markers are enabled only
    # after cloud02 proved the repaired SDK logging/exception boundary.
    detail = probe_sandbox.markers["body"] if random_detail else "T208-sensitive-detail-placeholder"
    probe_sandbox.scenario.error = httpx.ConnectError(detail)

    async def exercise():
        http = probe_sandbox.http()
        provider = OpenAICompatibleEmbeddingProvider(model="t208-embedding-model", base_url="https://provider.invalid/v1",
                   api_key=probe_sandbox.markers["key"], http_client=http, max_retries=0)
        try:
            with pytest.raises(EmbeddingProviderError) as caught:
                await provider.embed_query("fixed synthetic embedding")
            assert caught.value.__cause__ is None
            assert caught.value.__context__ is None or caught.value.__suppress_context__
            assert detail not in error_surfaces(caught.value)["trace"]
            assert detail not in caplog.text
            assert_no_secret(probe_sandbox.markers, error_surfaces(caught.value))
            assert_no_secret(probe_sandbox.markers, {"logs": caplog.text})
        finally:
            await provider.aclose()
            await http.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize("explicit_name", [False, True])
def test_review_registered_rerank_legacy_record_drives_both_routes(explicit_name, monkeypatch):
    from backend.app.ai.retrieval.reranker import build_reranker

    owner = registry()
    module = importlib.import_module(MODULE)
    monkeypatch.setattr(module, "get_provider_registry", lambda: owner)
    marker = object()
    seen = []

    def builder(settings, **kwargs):
        seen.append((settings, kwargs))
        return marker

    owner.register_legacy("rerank", "t208_extra", builder)
    settings = build_test_settings(rerank_provider="t208_extra")
    assert "t208_extra" in config.AppSettings.SUPPORTED_RERANK_PROVIDERS
    result = build_reranker("t208_extra" if explicit_name else None, settings=settings, explicit_option=17)
    assert result is marker
    assert len(seen) == 1 and seen[0][0] is settings
    assert seen[0][1] == {"explicit_option": 17}


def test_review_replaced_rerank_builder_receives_no_old_vendor_defaults(monkeypatch):
    from backend.app.ai.retrieval.reranker import build_reranker

    owner = registry()
    module = importlib.import_module(MODULE)
    monkeypatch.setattr(module, "get_provider_registry", lambda: owner)
    settings = build_test_settings(rerank_provider="llm")
    marker = object()
    seen = []

    def builder(value):
        seen.append(value)
        return marker

    owner.register_legacy("rerank", "llm", builder, replace=True)
    assert not owner.entry("rerank", "llm").supported_capabilities
    assert build_reranker(settings=settings) is marker
    assert len(seen) == 1 and seen[0] is settings


def test_review_registered_ocr_legacy_record_is_the_actual_dispatch(monkeypatch):
    from backend.app.ai.ingestion.ocr.factory import create_ocr_provider

    owner = registry()
    module = importlib.import_module(MODULE)
    monkeypatch.setattr(module, "get_provider_registry", lambda: owner)
    marker = object()
    seen = []

    def builder(settings):
        seen.append(settings)
        return marker

    owner.register_legacy("ocr", "t208_ocr", builder)
    settings = build_test_settings(ocr_enabled=True, ocr_provider="t208_ocr")
    assert create_ocr_provider(settings) is marker
    assert len(seen) == 1 and seen[0] is settings
    assert not owner.entry("ocr", "t208_ocr").supported_capabilities


def test_review_vision_legacy_replacement_cannot_inherit_vendor_capability(monkeypatch):
    from backend.app.ai.vision.base import VisionFailure
    from backend.app.ai.vision.provider import create_vision_provider

    owner = registry()
    module = importlib.import_module(MODULE)
    monkeypatch.setattr(module, "get_provider_registry", lambda: owner)
    calls = []
    owner.register_legacy("vision", "deepseek", lambda *_args, **_kwargs: calls.append("constructed"), replace=True)
    settings = build_test_settings(vision_model="deepseek-flash")
    with pytest.raises(VisionFailure):
        create_vision_provider(settings)
    assert calls == []


def test_review_rerank_record_uses_its_explicit_settings_without_second_copy():
    owner = registry()
    settings = build_test_settings(rerank_provider="llm", rerank_model="t208-explicit-rerank")
    provider = RecordingLLM()
    seen = []

    def builder(value=None):
        seen.append(value)
        return provider

    adapter = owner.entry("rerank", "llm").legacy_factory(
        settings, llm_factory=builder, max_candidates=2, timeout=15.0,
    )
    try:
        assert len(seen) == 1 and seen[0] is settings
        assert adapter.model_name == "t208-explicit-rerank"
    finally:
        asyncio.run(adapter.aclose())


@pytest.mark.parametrize("operation", ["profile", "binding"])
def test_review_independent_registry_parameters_do_not_consult_global_replacement(operation, monkeypatch, probe_sandbox):
    owner, ambient = registry(), registry()
    ambient.register_legacy("llm", "openai_compatible", lambda _settings: RecordingLLM(), replace=True)
    module = importlib.import_module(MODULE)
    monkeypatch.setattr(module, "get_provider_registry", lambda: ambient)
    profile = {**probe_sandbox.profile, "parameters": {"temperature": 0.25}}
    with pytest.raises(ValueError):
        ProviderProfile.model_validate(profile)
    if operation == "profile":
        validated = owner.validate_profile(profile)
        assert validated.parameters["temperature"] == 0.25
    else:
        resolved = owner.resolve_binding(task_kind="question_generation", profiles={str(profile["profile_id"]): profile}, bindings=_default(profile))
        assert resolved.profile_id == profile["profile_id"]


@pytest.mark.parametrize("entrypoint", ["direct", "settings"])
@pytest.mark.parametrize("base_url", [
    "https://user:synthetic-not-secret@provider.invalid/v1",
    "https://provider.invalid/v1?key=synthetic-not-secret",
    "https://provider.invalid/v1#synthetic-not-secret",
    "http://remote.invalid/v1",
])
def test_cloud_embedding_legacy_and_direct_reject_unsafe_endpoints(entrypoint, base_url, probe_sandbox):
    from backend.app.ai.embedding.providers.openai_compatible import (
        OpenAICompatibleEmbeddingProvider,
    )

    provider = None
    try:
        with pytest.raises(ValueError):
            if entrypoint == "direct":
                provider = OpenAICompatibleEmbeddingProvider(model="t208-embedding-model", api_key="synthetic-not-secret", base_url=base_url)
            else:
                settings = build_test_settings(embedding_provider="openai_compatible", embedding_model="t208-embedding-model", embedding_base_url=base_url)
                provider = OpenAICompatibleEmbeddingProvider.from_settings(settings)
    finally:
        if provider is not None:
            asyncio.run(provider.aclose())
    assert probe_sandbox.scenario.requests == []


@pytest.mark.parametrize("borrowed_pool", [False, True])
@pytest.mark.parametrize("random_detail", [False, True])
def test_cloud_embedding_close_failure_is_safe_and_releases_owned_view(borrowed_pool, random_detail, probe_sandbox, monkeypatch, caplog):
    import logging

    from backend.app.ai.embedding.base import EmbeddingProviderError
    from backend.app.ai.embedding.providers.openai_compatible import (
        OpenAICompatibleEmbeddingProvider,
    )

    # cloud04 passed fixed diagnostic cases before enabling random markers.
    detail = probe_sandbox.markers["body"] if random_detail else "T208-close-sensitive-placeholder"
    clients: list[httpx.AsyncClient] = []
    sdk_closes = []
    original_init = httpx.AsyncClient.__init__
    original_close = httpx.AsyncClient.aclose

    def initialize(client, *args, **kwargs):
        if "transport" not in kwargs:
            kwargs["transport"] = httpx.MockTransport(probe_sandbox.scenario.respond)
        original_init(client, *args, **kwargs)
        clients.append(client)

    async def fail_close(sdk):
        sdk_closes.append(sdk)
        logging.getLogger("openai._base_client").debug(detail)
        raise RuntimeError(detail)

    async def exercise():
        pool = probe_sandbox.http() if borrowed_pool else None
        monkeypatch.setattr(httpx.AsyncClient, "__init__", initialize)
        monkeypatch.setattr(AsyncOpenAI, "close", fail_close)
        provider = OpenAICompatibleEmbeddingProvider(model="t208-embedding-model", api_key="synthetic-not-secret",
                   base_url="https://provider.invalid/v1", http_client=pool, max_retries=0)
        try:
            with pytest.raises(EmbeddingProviderError) as caught:
                await provider.aclose()
            assert caught.value.__cause__ is None
            assert detail not in error_surfaces(caught.value)["trace"]
            assert detail not in caplog.text
            assert_no_secret(probe_sandbox.markers, {"error": error_surfaces(caught.value), "logs": caplog.text})
            assert len(clients) == 1 and clients[0].is_closed
            assert len(sdk_closes) == 1
            await provider.aclose()
            assert len(sdk_closes) == 1
            if pool is not None:
                assert not pool.is_closed
            logging.getLogger("openai._base_client").debug("t208-after-close-context-restored")
            assert "t208-after-close-context-restored" in caplog.text
        finally:
            for client in clients:
                if not client.is_closed:
                    await original_close(client)
            if pool is not None:
                await pool.aclose()

    asyncio.run(exercise())
