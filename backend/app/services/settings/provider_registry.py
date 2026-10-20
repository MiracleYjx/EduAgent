"""Executable Provider facts and pure task resolution; no probes or activation IO.

Only this module defines the built-in preset/factory/parameter/capability mapping.
Factories capture the supplied profile; they never resolve global AppSettings or
credentials implicitly. Declared support is not evidence of a successful call.
"""
from __future__ import annotations

import ipaddress
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from functools import lru_cache, partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, SecretStr, ValidationError

from backend.app.schemas.settings import ProviderProfile, SafeModel, TaskBinding

if TYPE_CHECKING:
    from backend.app.core.config import AppSettings


class RegistryValidationError(ValueError):
    """Safe local configuration failure, without untrusted inputs or SDK text."""

    def __init__(self, code: str = "INVALID_PROVIDER_CONFIGURATION") -> None:
        super().__init__("Provider 配置或任务绑定无效，请检查适配器、能力和修订。")
        self.code = code


class DuplicateProviderError(ValueError):
    """A runtime builder is already registered under the selected kind/name."""


class NoParameters(SafeModel):
    pass


class TextParameters(SafeModel):
    temperature: float | None = Field(default=None, strict=True, ge=0, le=2, allow_inf_nan=False)
    max_tokens: int | None = Field(default=None, strict=True, ge=1, le=32768)


class EmbeddingParameters(SafeModel):
    dimension: int | None = Field(default=None, strict=True, ge=1, le=65536)
    batch_size: int = Field(default=64, strict=True, ge=1, le=64)


class LocalEmbeddingParameters(SafeModel):
    dimension: int | None = Field(default=None, strict=True, ge=1, le=65536)
    batch_size: int = Field(default=32, strict=True, ge=1, le=256)


class LLMRerankParameters(SafeModel):
    max_candidates: int = Field(default=5, strict=True, ge=1, le=100)
    timeout_seconds: float = Field(default=30.0, strict=True, gt=0, le=120, allow_inf_nan=False)


class LocalRerankParameters(SafeModel):
    max_candidates: int = Field(default=5, strict=True, ge=1, le=100)


class ValidatedProfile(ProviderProfile):
    verified_capabilities: tuple[str, ...] = ()


class ResolvedBinding(SafeModel):
    task_kind: str
    mode: str
    profile_id: UUID | None = None
    profile_revision: UUID | None = None
    adapter_id: str | None = None
    required_capabilities: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RegistryEntry:
    kind: str
    adapter_id: str
    factory: Callable[..., Any]
    config_schema: type[SafeModel]
    supported_capabilities: frozenset[str]
    probe_factory: Callable[..., Any]
    lifecycle: str
    legacy_factory: Callable[..., Any]
    canonical_id: str


def _secret(runtime: dict[str, Any], profile: ProviderProfile) -> SecretStr | None:
    if profile.auth_mode == "none":
        if runtime.pop("api_key", None) is not None:
            raise RegistryValidationError("AUTH_MODE_CONFLICT")
        runtime.pop("credential_store", None)
        return None
    key = runtime.pop("api_key", None)
    store = runtime.pop("credential_store", None)
    if key is None and store is not None and profile.credential_ref is not None:
        ref = profile.credential_ref
        key = store.get(ref, ref.version)
    if isinstance(key, str):
        key = SecretStr(key)
    if not isinstance(key, SecretStr) or not key.get_secret_value().strip():
        raise RegistryValidationError("KEY_MISSING")
    return key


def _build_text(adapter_id: str, profile: ProviderProfile, **runtime: Any) -> Any:
    if profile.base_url is None:
        raise RegistryValidationError("ENDPOINT_MISSING")
    key = _secret(runtime, profile)
    if adapter_id == "deepseek":
        from backend.app.ai.llm.deepseek import DeepSeekProvider

        # Deliberately project only the existing adapter's three consumed fields;
        # no fake database/JWT/Embedding settings or environment lookup is needed.
        captured: Any = SimpleNamespace(deepseek_api_key=key, deepseek_base_url=profile.base_url,
                                   deepseek_model=profile.model_id)
        return DeepSeekProvider(captured, **runtime)
    from backend.app.ai.llm.openai_compatible import OpenAICompatibleProvider

    parameters = TextParameters.model_validate(profile.parameters)
    return OpenAICompatibleProvider(model=profile.model_id, base_url=profile.base_url,
                                    api_key=key, auth_mode=profile.auth_mode,
                                    temperature=parameters.temperature, max_tokens=parameters.max_tokens, **runtime)


def _build_embedding(adapter_id: str, profile: ProviderProfile, **runtime: Any) -> Any:
    if adapter_id == "openai_compatible":
        from backend.app.ai.embedding.providers.openai_compatible import (
            OpenAICompatibleEmbeddingProvider,
        )

        key = _secret(runtime, profile)
        parameters = EmbeddingParameters.model_validate(profile.parameters)
        return OpenAICompatibleEmbeddingProvider(
            model=profile.model_id, base_url=profile.base_url,
            api_key=key.get_secret_value() if key is not None else "",
            auth_mode=profile.auth_mode, dimension=parameters.dimension, batch_size=parameters.batch_size, **runtime,
        )
    from backend.app.ai.embedding.providers.bge import bge_query_prefix
    from backend.app.ai.embedding.providers.local import LocalEmbeddingProvider

    directory = profile.resource_directory
    if directory is None or not directory.is_dir():
        raise RegistryValidationError("RESOURCE_UNAVAILABLE")
    prefix = bge_query_prefix(profile.model_id) if adapter_id == "bge" or profile.model_id.lower().startswith("baai/bge-") else ""
    local_parameters = LocalEmbeddingParameters.model_validate(profile.parameters)
    return LocalEmbeddingProvider(model=profile.model_id, provider_name=adapter_id, query_prefix=prefix,
                                  resource_directory=directory, dimension=local_parameters.dimension,
                                  batch_size=local_parameters.batch_size, **runtime)


def _build_rerank(adapter_id: str, profile: ProviderProfile, **runtime: Any) -> Any:
    from backend.app.ai.retrieval.reranker import (
        CrossEncoderRerankAdapter,
        LLMRerankAdapter,
    )

    if adapter_id == "cross_encoder":
        directory = profile.resource_directory
        if directory is None or not directory.is_dir():
            raise RegistryValidationError("RESOURCE_UNAVAILABLE")
        local_parameters = LocalRerankParameters.model_validate(profile.parameters)
        return CrossEncoderRerankAdapter(model=profile.model_id, resource_directory=directory,
                                         max_candidates=local_parameters.max_candidates, **runtime)
    parameters = LLMRerankParameters.model_validate(profile.parameters)
    provider = runtime.pop("provider", None)
    owns_provider = provider is None
    if provider is None:
        text_adapter = "openai_compatible"
        text = profile.model_copy(update={"parameters": {}})
        provider = _build_text(text_adapter, text, **runtime)
    adapter = LLMRerankAdapter(provider=provider, model=profile.model_id,
                               max_candidates=parameters.max_candidates, timeout=parameters.timeout_seconds)
    adapter._owns_provider = owns_provider
    return adapter


def _build_vision(profile: ProviderProfile, **runtime: Any) -> Any:
    provider = _build_text("deepseek", profile, **runtime)
    if not provider.supports_vision():
        # DeepSeek constructs its SDK lazily; no client has been acquired yet.
        raise RegistryValidationError("CAPABILITY_UNSUPPORTED")
    return provider


def _build_ocr(profile: ProviderProfile, **_runtime: Any) -> Any:
    from backend.app.ai.ingestion.ocr.rapidocr import MODEL_ID, RapidOCRProvider

    if profile.model_id != MODEL_ID or profile.resource_directory is None:
        raise RegistryValidationError("RESOURCE_UNAVAILABLE")
    return RapidOCRProvider(profile.resource_directory)


def _legacy_text(adapter_id: str, settings: AppSettings, **runtime: Any) -> Any:
    if adapter_id == "deepseek":
        from backend.app.ai.llm.deepseek import DeepSeekProvider

        return DeepSeekProvider(settings, **runtime)
    from backend.app.ai.llm.openai_compatible import OpenAICompatibleProvider

    return OpenAICompatibleProvider.from_settings(settings, **runtime)


def _legacy_embedding(adapter_id: str, settings: AppSettings, **_runtime: Any) -> Any:
    if adapter_id == "openai_compatible":
        from backend.app.ai.embedding.providers.openai_compatible import (
            OpenAICompatibleEmbeddingProvider,
        )

        return OpenAICompatibleEmbeddingProvider.from_settings(settings)
    from backend.app.ai.embedding.providers.bge import BgeEmbeddingProvider
    from backend.app.ai.embedding.providers.local import LocalEmbeddingProvider

    use_bge = adapter_id == "bge" or (settings.embedding_model or "").lower().startswith("baai/bge-")
    provider = BgeEmbeddingProvider.from_settings(settings) if use_bge else LocalEmbeddingProvider.from_settings(settings)
    provider.provider_name = adapter_id
    return provider


def _legacy_rerank(adapter_id: str, runtime_settings: AppSettings, **runtime: Any) -> Any:
    from backend.app.ai.retrieval.reranker import (
        CrossEncoderRerankAdapter,
        LLMRerankAdapter,
        RerankProviderNotReadyError,
        create_llm_provider,
    )

    llm_factory = runtime.pop("llm_factory", create_llm_provider)
    runtime.setdefault("max_candidates", runtime_settings.rerank_max_candidates)
    if "model" not in runtime:
        configured_model = runtime_settings.rerank_model if adapter_id == runtime_settings.rerank_provider else None
        if configured_model:
            runtime["model"] = configured_model
        elif adapter_id != "cross_encoder":
            runtime["model"] = runtime_settings.deepseek_model
    if adapter_id == "cross_encoder":
        return CrossEncoderRerankAdapter(**runtime)
    runtime.setdefault("timeout", runtime_settings.rerank_timeout_seconds)
    if runtime.get("provider") is None and llm_factory is not None:
        try:
            runtime["provider"] = llm_factory(runtime_settings)
        except Exception as error:
            raise RerankProviderNotReadyError(
                f"LLM Rerank 依赖的 LLM Provider 未就绪：{type(error).__name__}。"
            ) from error
        owns_provider = True
    else:
        owns_provider = runtime.get("provider") is None
    runtime.setdefault("settings", runtime_settings)
    result = LLMRerankAdapter(**runtime)
    result._owns_provider = owns_provider
    return result


def _legacy_vision(settings: AppSettings, **runtime: Any) -> Any:
    llm_factory = runtime.pop("llm_factory", None)
    if llm_factory is None:
        from backend.app.ai.llm.factory import create_llm_provider

        llm_factory = create_llm_provider
    return llm_factory(settings, **runtime)


def _legacy_ocr(settings: AppSettings, **_runtime: Any) -> Any:
    from backend.app.ai.ingestion.ocr.base import OCRProviderError
    from backend.app.ai.ingestion.ocr.rapidocr import MODEL_ID, RapidOCRProvider

    if settings.ocr_model not in (None, MODEL_ID):
        raise OCRProviderError(
            "OCR_PROVIDER_NOT_READY", "当前适配器只支持 OCR_MODEL=PP-OCRv5-mobile。"
        )
    if settings.ocr_model_dir is None:
        raise OCRProviderError(
            "OCR_PROVIDER_NOT_READY", "请配置 OCR_MODEL_DIR 并预置已确认的三个 ONNX 模型。"
        )
    return RapidOCRProvider(settings.ocr_model_dir)


def _probe_factory(factory: Callable[..., Any], kind: str, profile: ProviderProfile, **runtime: Any) -> Any:
    # Transport construction only. Billing, concurrency, evidence, timeouts and
    # error classification remain exclusively the unfinished T209 service.
    from backend.app.core.retry_policy import RetryPolicy

    if runtime.pop("fallback_provider", None) is not None:
        raise RegistryValidationError("PROBE_FALLBACK_FORBIDDEN")
    if kind in {"llm", "vision"}:
        runtime["retry_policy"] = RetryPolicy(max_retries=0)
    elif kind == "embedding" and profile.adapter_id == "openai_compatible":
        runtime["max_retries"] = 0
    elif kind == "rerank" and profile.adapter_id != "cross_encoder":
        runtime["retry_policy"] = RetryPolicy(max_retries=0)
    return factory(profile, **runtime)


def _builtin_entries() -> tuple[RegistryEntry, ...]:
    def entry(kind: str, name: str, schema: type[SafeModel], capabilities: tuple[str, ...],
              factory: Callable[..., Any], legacy: Callable[..., Any], lifecycle: str = "owned_per_run",
              canonical_id: str | None = None) -> RegistryEntry:
        return RegistryEntry(kind, name, factory, schema, frozenset(capabilities),
                             partial(_probe_factory, factory, kind), lifecycle, legacy, canonical_id or name)

    result: list[RegistryEntry] = [
        entry("llm", "deepseek", NoParameters, ("text", "json_object", "vision"),
              partial(_build_text, "deepseek"), partial(_legacy_text, "deepseek")),
        entry("llm", "openai_compatible", TextParameters, ("text", "json_object"),
              partial(_build_text, "openai_compatible"), partial(_legacy_text, "openai_compatible")),
        entry("embedding", "openai_compatible", EmbeddingParameters, ("embedding",),
              partial(_build_embedding, "openai_compatible"), partial(_legacy_embedding, "openai_compatible")),
        entry("rerank", "cross_encoder", LocalRerankParameters, ("local_rerank",),
              partial(_build_rerank, "cross_encoder"), partial(_legacy_rerank, "cross_encoder"), "local_until_restart"),
        entry("vision", "deepseek", NoParameters, ("vision", "text", "json_object"), _build_vision, _legacy_vision),
        entry("ocr", "rapidocr", NoParameters, ("ocr",), _build_ocr, _legacy_ocr, "local_until_restart"),
    ]
    for name in ("bge", "huggingface", "local"):
        result.append(entry("embedding", name, LocalEmbeddingParameters, ("embedding",),
                            partial(_build_embedding, name), partial(_legacy_embedding, name), "local_until_restart"))
    for name in ("llm", "openai_compatible"):
        result.append(entry("rerank", name, LLMRerankParameters, ("llm_rerank",),
                            partial(_build_rerank, name), partial(_legacy_rerank, name), canonical_id="llm"))
    return tuple(result)


def _loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.lower().rstrip(".") == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_provider_endpoint(value: str) -> str:
    """Pure transport boundary shared by profile and legacy construction."""
    try:
        endpoint = urlsplit(value)
        valid = endpoint.scheme in {"http", "https"} and bool(endpoint.hostname)
        valid = valid and not (endpoint.username or endpoint.password or endpoint.query or endpoint.fragment)
        valid = valid and not any(ord(character) <= 32 for character in value)
        _ = endpoint.port
    except (TypeError, ValueError):
        raise RegistryValidationError("INVALID_ENDPOINT") from None
    if not valid:
        raise RegistryValidationError("INVALID_ENDPOINT")
    if endpoint.scheme == "http" and not _loopback(endpoint.hostname):
        raise RegistryValidationError("HTTPS_REQUIRED")
    return value


class ProviderRegistry:
    """Actual built-ins, optional explicit runtime registration, and pure resolution."""

    def __init__(self, *, environment_id: UUID | None = None) -> None:
        self.environment_id = environment_id
        self._entries = {(item.kind, item.adapter_id): item for item in _builtin_entries()}

    def entries(self, *, kind: str) -> tuple[RegistryEntry, ...]:
        return tuple(value for key, value in sorted(self._entries.items()) if key[0] == kind)

    def entry(self, kind: str, adapter_id: str) -> RegistryEntry:
        entry = self._entries.get((kind, adapter_id.strip().lower()))
        if entry is None:
            raise RegistryValidationError("ADAPTER_UNSUPPORTED")
        return entry

    def register_legacy(self, kind: str, adapter_id: str, builder: Callable[..., Any], *, replace: bool = False) -> None:
        normalized = adapter_id.strip().lower()
        if not normalized or not callable(builder):
            raise RegistryValidationError()
        key = (kind, normalized)
        if key in self._entries and not replace:
            raise DuplicateProviderError("Provider 已注册。")

        def unsupported(*_args: Any, **_kwargs: Any) -> Any:
            raise RegistryValidationError("PROFILE_FACTORY_NOT_REGISTERED")

        # A custom legacy callback has no proven profile/parameter/capability
        # contract. Never attach the replaced vendor's metadata to that callback.
        item = self._entries.get(key)
        if item is not None:
            self._entries[key] = dataclass_replace(item, legacy_factory=builder, factory=unsupported,
                                         probe_factory=unsupported, supported_capabilities=frozenset(),
                                         config_schema=NoParameters, lifecycle="caller_defined")
        else:
            self._entries[key] = RegistryEntry(kind, normalized, unsupported, NoParameters, frozenset(),
                                               unsupported, "caller_defined", builder, normalized)

    def _kind(self, profile: ProviderProfile) -> str:
        return self._kind_for(profile.adapter_id, profile.declared_capabilities)

    def parameter_schema(self, adapter_id: str, declared_capabilities: tuple[str, ...]) -> type[SafeModel]:
        """The non-secret persisted shape uses the very same executable schema."""
        return self.entry(self._kind_for(adapter_id, declared_capabilities), adapter_id).config_schema

    def _kind_for(self, adapter_id: str, declared_capabilities: tuple[str, ...]) -> str:
        declared = set(declared_capabilities)
        for capability, kind in (("embedding", "embedding"), ("ocr", "ocr"), ("local_rerank", "rerank"),
                                  ("llm_rerank", "rerank"), ("vision", "vision")):
            if capability in declared:
                return kind
        if ("llm", adapter_id) in self._entries:
            return "llm"
        kinds = [kind for kind, name in self._entries if name == adapter_id]
        if len(kinds) != 1:
            raise RegistryValidationError("ADAPTER_UNSUPPORTED")
        return kinds[0]

    def validate_profile(self, profile: ProviderProfile | Mapping[str, Any], *, kind: str | None = None) -> ValidatedProfile:
        try:
            raw = profile.model_dump(mode="python") if isinstance(profile, ProviderProfile) else dict(profile)
            if isinstance(profile, ValidatedProfile) and not profile.verified_capabilities:
                raw.pop("verified_capabilities", None)
            context = {"provider_registry": self}
            parsed = ProviderProfile.model_validate(raw, context=context)
            record = self.entry(kind or self._kind(parsed), parsed.adapter_id)
            if not record.supported_capabilities or not set(parsed.declared_capabilities) <= record.supported_capabilities:
                raise RegistryValidationError("CAPABILITY_UNSUPPORTED")
            parameters = record.config_schema.model_validate(parsed.parameters).model_dump(exclude_none=True)
            reference = parsed.credential_ref
            if reference is not None and (reference.purpose != "provider" or (
                self.environment_id is not None and reference.environment_id != self.environment_id
            )):
                raise RegistryValidationError("CREDENTIAL_REFERENCE_MISMATCH")
            if parsed.auth_mode == "none" and reference is not None:
                raise RegistryValidationError("AUTH_MODE_CONFLICT")
            if parsed.adapter_id == "deepseek" and parsed.auth_mode != "api_key":
                raise RegistryValidationError("AUTH_MODE_UNSUPPORTED")
            if parsed.base_url is not None:
                validate_provider_endpoint(parsed.base_url)
            if parsed.resource_directory is not None and not parsed.resource_directory.is_absolute():
                raise RegistryValidationError("ABSOLUTE_RESOURCE_DIRECTORY_REQUIRED")
            return ValidatedProfile.model_validate({**parsed.model_dump(mode="python"), "parameters": parameters,
                                                     "verified_capabilities": ()}, context=context)
        except (ValidationError, TypeError, KeyError):
            raise RegistryValidationError() from None

    def create(self, profile: ProviderProfile | Mapping[str, Any], *, kind: str | None = None,
               probe: bool = False, **runtime: Any) -> Any:
        parsed = self.validate_profile(profile, kind=kind)
        record = self.entry(kind or self._kind(parsed), parsed.adapter_id)
        if not parsed.enabled:
            raise RegistryValidationError("PROFILE_DISABLED")
        remote = record.kind in {"llm", "vision"} or (
            record.kind == "embedding" and record.adapter_id == "openai_compatible"
        ) or (record.kind == "rerank" and record.adapter_id != "cross_encoder")
        if remote and parsed.base_url is None:
            raise RegistryValidationError("ENDPOINT_MISSING")
        factory = record.probe_factory if probe else record.factory
        return factory(parsed, **runtime)

    @staticmethod
    def _binding(task: str, bindings: Mapping[str, Any]) -> TaskBinding | None:
        raw = bindings.get(task)
        if raw is None:
            return None
        try:
            binding = TaskBinding.model_validate(raw)
        except ValidationError:
            raise RegistryValidationError("INVALID_TASK_BINDING") from None
        if binding.task_kind != task:
            raise RegistryValidationError("TASK_KIND_MISMATCH")
        if binding.inherit_default and (binding.profile_id is not None or binding.profile_revision is not None):
            raise RegistryValidationError("AMBIGUOUS_TASK_BINDING")
        if (binding.profile_id is None) != (binding.profile_revision is None):
            raise RegistryValidationError("INCOMPLETE_PROFILE_REFERENCE")
        return binding

    def resolve_binding(self, *, task_kind: str, profiles: Mapping[str, Any], bindings: Mapping[str, Any]) -> ResolvedBinding:
        inherited = {"diagnosis", "content_validation", "paper_extraction"}
        overrides = {"question_generation", "subjective_grading"}
        independent = {"embedding", "vision", "ocr"}
        if task_kind not in inherited | overrides | independent | {"reviewer", "default_llm", "rerank"}:
            raise RegistryValidationError("TASK_UNSUPPORTED")
        task = self._binding(task_kind, bindings)
        if task_kind == "reviewer":
            if task is not None and (task.profile_id is not None or task.required_capabilities):
                raise RegistryValidationError("REVIEWER_RULE_ONLY")
            return ResolvedBinding(task_kind=task_kind, mode="rule_based")
        if task_kind in inherited and task is not None and (task.profile_id is not None or task.profile_revision is not None):
            raise RegistryValidationError("TASK_INHERITS_DEFAULT")
        selected: TaskBinding | None
        if task_kind in independent:
            if task is None or task.inherit_default:
                raise RegistryValidationError("INDEPENDENT_BINDING_REQUIRED")
            selected, mode = task, "independent"
        elif task_kind == "default_llm":
            selected, mode = task, "default"
            if task is not None and task.inherit_default:
                raise RegistryValidationError("DEFAULT_CANNOT_INHERIT")
        elif task is not None and task.profile_id is not None:
            selected, mode = task, "override"
        else:
            selected, mode = self._binding("default_llm", bindings), "inherited"
        if selected is None or selected.inherit_default or selected.profile_id is None or selected.profile_revision is None:
            raise RegistryValidationError("PROFILE_BINDING_REQUIRED")
        raw = profiles.get(str(selected.profile_id))
        if raw is None:
            raise RegistryValidationError("PROFILE_BINDING_MISSING")
        try:
            profile = ProviderProfile.model_validate(raw, context={"provider_registry": self})
        except ValidationError:
            raise RegistryValidationError("INVALID_PROVIDER_PROFILE") from None
        if profile.profile_id != selected.profile_id or profile.profile_revision != selected.profile_revision:
            raise RegistryValidationError("PROFILE_REVISION_MISMATCH")
        if not profile.enabled:
            raise RegistryValidationError("PROFILE_DISABLED")
        if task_kind in independent:
            kind = task_kind
            required = {task_kind}
        elif task_kind == "rerank" and profile.adapter_id == "cross_encoder":
            kind, required = "rerank", {"local_rerank"}
        elif task_kind == "rerank" and "llm_rerank" in profile.declared_capabilities:
            kind, required = "rerank", {"llm_rerank"}
        else:
            kind, required = "llm", {"text", "json_object"}
        required.update(selected.required_capabilities)
        if task is not None:
            required.update(task.required_capabilities)
        # Task resolution describes dependencies; it neither acquires clients nor
        # declares readiness. Local-resource/model availability is checked at use.
        profile = self.validate_profile(profile, kind=kind)
        record = self.entry(kind, profile.adapter_id)
        if not required <= set(profile.declared_capabilities) or not required <= record.supported_capabilities:
            raise RegistryValidationError("CAPABILITY_UNSUPPORTED")
        if not set(profile.declared_capabilities) <= record.supported_capabilities:
            raise RegistryValidationError("CAPABILITY_UNSUPPORTED")
        retrieval = task.retrieval_mode if task is not None else None
        if retrieval not in {None, "none", "keyword", "semantic", "hybrid", "hybrid_rerank"}:
            raise RegistryValidationError("RETRIEVAL_MODE_UNSUPPORTED")
        dependencies: tuple[str, ...] = ()
        if retrieval in {"semantic", "hybrid", "hybrid_rerank"}:
            dependencies = ("embedding", "rerank") if retrieval == "hybrid_rerank" else ("embedding",)
        return ResolvedBinding(task_kind=task_kind, mode=mode, profile_id=profile.profile_id,
                               profile_revision=profile.profile_revision, adapter_id=profile.adapter_id,
                               required_capabilities=tuple(sorted(required)), dependencies=dependencies)


@lru_cache(maxsize=1)
def get_provider_registry() -> ProviderRegistry:
    return ProviderRegistry()
