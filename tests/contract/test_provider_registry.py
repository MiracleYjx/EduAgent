"""T207 / TCR-009: Registry and task contracts, before T208 implementation.

Existing factories are tested separately from missing Registry ports: an enum
mismatch is a real callable behavior gap, not an import/fixture failure.
"""

from __future__ import annotations

import asyncio
import inspect
from copy import deepcopy

import pytest
from pydantic import ValidationError

from backend.app.ai.embedding.factory import get_embedding_provider_factory
from backend.app.ai.llm.base import BaseLLMProvider
from backend.app.ai.llm.factory import LLMProviderFactory, get_llm_provider_factory
from backend.app.ai.retrieval.reranker import LLMRerankAdapter, build_reranker
from backend.app.schemas.settings import ProviderProfile
from tests.support.credential_fakes import assert_no_secret, safe_surfaces
from tests.support.provider_probe_fakes import (
    MinimalAnswer,
    RecordingLLM,
    make_profile,
    probe_sandbox,
    registry,
    registry_entries,
    resolve_binding,
    validate_profile,
    view,
)
from tests.unit.settings_helpers import build_test_settings

pytestmark = pytest.mark.usefixtures(probe_sandbox.__name__)


def test_existing_llm_factory_exposes_general_text_adapter():
    # Known T208 gap: call the actual factory rather than a duplicated name list.
    assert "openai_compatible" in get_llm_provider_factory().supported_providers()


def test_existing_embedding_factory_does_not_advertise_deepseek():
    names = get_embedding_provider_factory().supported_providers()
    assert "deepseek" not in names
    assert "openai_compatible" in names


def test_existing_required_llm_signature_and_conservative_vision():
    signature = inspect.signature(BaseLLMProvider.generate_structured)
    assert list(signature.parameters) == ["self", "messages", "schema", "model"]
    assert signature.parameters["messages"].default is inspect.Parameter.empty
    assert signature.parameters["schema"].default is inspect.Parameter.empty
    assert signature.parameters["model"].default is None
    provider = RecordingLLM()
    assert provider.supports_vision() is False
    result = asyncio.run(provider.generate_structured([], MinimalAnswer, model="t207-explicit"))
    assert isinstance(result, MinimalAnswer)
    assert result.ok is True
    asyncio.run(provider.aclose())
    assert provider.close_calls == 1


def test_existing_factory_keeps_injected_builder_and_explicit_model():
    provider = RecordingLLM()
    seen = []

    def builder(settings):
        seen.append(settings)
        return provider

    factory = LLMProviderFactory({"deepseek": builder})
    settings = build_test_settings(llm_provider="deepseek")
    assert factory.create(settings) is provider
    assert seen == [settings]
    result = asyncio.run(provider.generate_structured([], MinimalAnswer, model="t207-no-global"))
    assert isinstance(result, MinimalAnswer)
    assert result.ok is True
    assert provider.calls[-1]["model"] == "t207-no-global"
    assert factory.supported_providers() == ("deepseek",)
    asyncio.run(provider.aclose())


def test_existing_rerank_compatible_alias_is_llm_not_cloud_rerank(probe_sandbox):
    provider = RecordingLLM()
    adapter = build_reranker(
        "openai_compatible", provider=provider, model="t207-rerank",
        settings=build_test_settings(rerank_provider="llm", rerank_model="t207-rerank"),
    )
    assert isinstance(adapter, LLMRerankAdapter)
    assert adapter.describe()["provider"] == "llm"
    assert probe_sandbox.scenario.requests == []
    assert provider.calls == []


@pytest.mark.parametrize("component", ["userinfo", "query", "fragment"])
def test_existing_profile_rejects_secret_url_components(component, probe_sandbox):
    marker = probe_sandbox.markers["key"]
    urls = {
        "userinfo": f"https://user:{marker}@provider.invalid/v1",
        "query": f"https://provider.invalid/v1?api_key={marker}",
        "fragment": f"https://provider.invalid/v1#{marker}",
    }
    data = {key: value for key, value in probe_sandbox.profile.items() if key != "parameters"}
    data["base_url"] = urls[component]
    with pytest.raises(ValidationError) as caught:
        ProviderProfile.model_validate(data)
    assert_no_secret(probe_sandbox.markers, safe_surfaces(caught.value))


@pytest.mark.parametrize("kind", ["llm", "embedding", "rerank", "vision", "ocr"])
def test_registry_entries_are_executable_fact_records(kind):
    entries = registry_entries(registry(), kind)
    assert entries
    identities = []
    for entry in entries:
        assert entry.adapter_id
        identities.append(entry.adapter_id)
        assert callable(entry.factory)
        assert callable(entry.probe_factory)
        assert callable(entry.config_schema.model_json_schema)
        assert entry.supported_capabilities
        assert entry.lifecycle
    assert len(identities) == len(set(identities))


@pytest.mark.parametrize("kind", ["llm", "embedding"])
def test_registry_and_runtime_factory_enumerations_do_not_diverge(kind):
    owner = registry()
    names = {entry.adapter_id for entry in registry_entries(owner, kind)}
    factory = get_llm_provider_factory() if kind == "llm" else get_embedding_provider_factory()
    assert names == set(factory.supported_providers())
    if kind == "llm":
        assert {"deepseek", "openai_compatible"} <= names
    else:
        assert "deepseek" not in names


def test_registry_general_text_name_does_not_imply_other_capabilities(probe_sandbox):
    data = view(validate_profile(registry(), probe_sandbox.profile))
    assert set(data["declared_capabilities"]) == {"text", "json_object"}
    assert not set(data["verified_capabilities"])
    assert not {"vision", "embedding", "json_schema"} & set(data["declared_capabilities"])
    assert probe_sandbox.scenario.requests == []


def test_registry_declaring_capability_does_not_claim_it_was_verified(probe_sandbox):
    profile = {**probe_sandbox.profile, "declared_capabilities": ("text", "json_object", "json_schema")}
    owner = registry()
    entries = registry_entries(owner, "llm")
    entry = next(item for item in entries if item.adapter_id == "openai_compatible")
    if "json_schema" in entry.supported_capabilities:
        data = view(validate_profile(owner, profile))
        assert "json_schema" not in data["verified_capabilities"]
    else:
        with pytest.raises(ValueError):
            validate_profile(owner, profile)
    assert probe_sandbox.scenario.requests == []


@pytest.mark.parametrize("field,value", [
    ("adapter_id", "t207-unknown"),
    ("declared_capabilities", ("telepathy",)),
    ("parameters", {"extra_headers": {"x-custom": "not-allowed"}}),
    ("parameters", {"max_retries": 3}),
    ("parameters", {"arbitrary_sdk_kwargs": True}),
])
def test_registry_rejects_unimplemented_or_unbounded_options(field, value, probe_sandbox):
    with pytest.raises(ValueError):
        validate_profile(registry(), {**probe_sandbox.profile, field: value})
    assert probe_sandbox.scenario.requests == []


@pytest.mark.parametrize("value", [-1e308, 1e308, float("inf"), float("nan")])
def test_registry_actual_parameter_schema_rejects_outside_or_unsupported_values(value, probe_sandbox):
    owner = registry()
    entry = next(item for item in registry_entries(owner, "llm") if item.adapter_id == "openai_compatible")
    # Do not require a vendor-specific parameter or a particular JSON Schema
    # syntax. The real parameter model supplies the legal positive control.
    defaults = entry.config_schema().model_dump(exclude_none=True)
    validate_profile(owner, {**probe_sandbox.profile, "parameters": defaults})
    if "temperature" not in entry.config_schema.model_fields:
        with pytest.raises(ValueError):
            validate_profile(owner, {**probe_sandbox.profile, "parameters": {"temperature": 0.5}})
    with pytest.raises(ValueError):
        validate_profile(owner, {**probe_sandbox.profile, "parameters": {"temperature": value}})
    assert probe_sandbox.scenario.requests == []


def test_registry_remote_key_requires_https_but_local_auth_none_is_allowed(probe_sandbox):
    owner = registry()
    with pytest.raises(ValueError):
        validate_profile(owner, {**probe_sandbox.profile, "base_url": "http://remote.invalid/v1"})
    local = {**probe_sandbox.profile, "base_url": "http://127.0.0.1:18181/v1",
             "auth_mode": "none", "credential_ref": None}
    result = view(validate_profile(owner, local))
    assert result["auth_mode"] == "none"
    assert result["credential_ref"] is None
    assert probe_sandbox.credentials.calls == []


def _bindings(profile):
    return {"default_llm": {
        "task_kind": "default_llm", "profile_id": profile["profile_id"],
        "profile_revision": profile["profile_revision"], "required_capabilities": ("json_object",),
    }}


@pytest.mark.parametrize("task", [
    "question_generation", "subjective_grading", "diagnosis", "content_validation", "paper_extraction",
])
def test_default_tasks_resolve_without_unrelated_optional_capabilities(task, probe_sandbox):
    owner = registry()
    profile = probe_sandbox.profile
    profiles = {str(profile["profile_id"]): profile}
    result = resolve_binding(owner, task, profiles, _bindings(profile))
    assert result["profile_id"] == profile["profile_id"]
    assert result["profile_revision"] == profile["profile_revision"]
    assert not {"embedding", "vision", "ocr"} & set(result["dependencies"])
    assert probe_sandbox.scenario.requests == []


@pytest.mark.parametrize("task", ["question_generation", "subjective_grading"])
def test_only_actual_task_override_changes_its_profile(task, probe_sandbox):
    owner = registry()
    default = probe_sandbox.profile
    override = make_profile(probe_sandbox.environment_id, model_id="t207-override")
    profiles = {str(item["profile_id"]): item for item in (default, override)}
    bindings = _bindings(default)
    bindings[task] = {"task_kind": task, "profile_id": override["profile_id"],
                      "profile_revision": override["profile_revision"], "inherit_default": False}
    assert resolve_binding(owner, task, profiles, bindings)["profile_id"] == override["profile_id"]
    for inherited in ("diagnosis", "content_validation", "paper_extraction"):
        assert resolve_binding(owner, inherited, profiles, bindings)["profile_id"] == default["profile_id"]


def test_reviewer_is_rule_based_even_without_any_default_model(probe_sandbox):
    result = resolve_binding(registry(), "reviewer", {}, {})
    assert result["mode"] == "rule_based"
    assert result["profile_id"] is None
    assert not result["dependencies"]
    assert probe_sandbox.credentials.calls == []
    assert probe_sandbox.http.clients == []


@pytest.mark.parametrize("task,adapter,capability", [
    ("embedding", "openai_compatible", "embedding"),
    ("vision", "deepseek", "vision"),
    ("ocr", "rapidocr", "ocr"),
])
def test_independent_capabilities_never_borrow_default_model_or_key(task, adapter, capability, probe_sandbox):
    default = probe_sandbox.profile
    independent = make_profile(probe_sandbox.environment_id, adapter_id=adapter,
                               model_id="t207-independent", declared_capabilities=(capability,))
    bindings = _bindings(default)
    bindings[task] = {"task_kind": task, "profile_id": independent["profile_id"],
                      "profile_revision": independent["profile_revision"]}
    profiles = {str(item["profile_id"]): item for item in (default, independent)}
    result = resolve_binding(registry(), task, profiles, bindings)
    assert result["profile_id"] == independent["profile_id"]
    assert result["profile_id"] != default["profile_id"]
    assert result["profile_revision"] == independent["profile_revision"]
    assert probe_sandbox.credentials.calls == []


@pytest.mark.parametrize("mode,expected", [
    ("semantic", {"embedding"}),
    ("hybrid", {"embedding"}),
    ("hybrid_rerank", {"embedding", "rerank"}),
])
def test_retrieval_adds_only_its_actual_dependencies(mode, expected, probe_sandbox):
    profile = probe_sandbox.profile
    bindings = _bindings(profile)
    bindings["question_generation"] = {"task_kind": "question_generation", "inherit_default": True,
                                       "retrieval_mode": mode}
    result = resolve_binding(registry(), "question_generation", {str(profile["profile_id"]): profile}, bindings)
    assert set(result["dependencies"]) == expected


def test_resolving_or_validating_does_not_mutate_candidate(probe_sandbox):
    profile = probe_sandbox.profile
    bindings = _bindings(profile)
    profiles = {str(profile["profile_id"]): profile}
    before = deepcopy((profiles, bindings))
    owner = registry()
    validate_profile(owner, profile)
    resolve_binding(owner, "question_generation", profiles, bindings)
    assert (profiles, bindings) == before
    assert probe_sandbox.scenario.requests == []
