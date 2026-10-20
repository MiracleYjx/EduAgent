"""TCR-023：逐任务 AI Readiness 只读事实合同（T220-Fix）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from backend.app.schemas.settings import (
    ActivationValidation,
    ConfigurationValues,
)
from backend.app.services.settings.ai_readiness import evaluate_ai_readiness
from backend.app.services.settings.provider_registry import ProviderRegistry

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def _profile(
    *,
    profile_id: UUID,
    revision: UUID,
    adapter: str = "openai_compatible",
    model: str = "synthetic-model",
    capabilities: tuple[str, ...] = ("text", "json_object"),
    auth_mode: str = "none",
    enabled: bool = True,
    credential_ref: dict | None = None,
    resource_directory: str | None = None,
) -> dict:
    payload = {
        "profile_id": str(profile_id),
        "profile_revision": str(revision),
        "display_name": "Synthetic profile",
        "adapter_id": adapter,
        "model_id": model,
        "base_url": "https://example.invalid/v1",
        "auth_mode": auth_mode,
        "declared_capabilities": list(capabilities),
        "enabled": enabled,
    }
    if credential_ref is not None:
        payload["credential_ref"] = credential_ref
    if resource_directory is not None:
        payload["resource_directory"] = resource_directory
    return payload


def _values(
    *,
    provider: dict,
    bindings: dict[str, dict],
) -> ConfigurationValues:
    profile_id = provider["profile_id"]
    return ConfigurationValues.model_validate(
        {
            "providers": {profile_id: provider},
            "bindings": bindings,
        }
    )


def _binding(task: str, provider: dict, *, required: tuple[str, ...] = (), retrieval: str | None = None) -> dict:
    payload = {
        "task_kind": task,
        "profile_id": provider["profile_id"],
        "profile_revision": provider["profile_revision"],
        "required_capabilities": list(required),
    }
    if retrieval is not None:
        payload["retrieval_mode"] = retrieval
    return payload


def _acceptance(
    *,
    revision: UUID,
    capabilities: tuple[str, ...] = ("text", "json_object"),
    tested_at: datetime = NOW,
) -> dict[str, ActivationValidation]:
    return {
        f"task:{capability}": ActivationValidation(
            task_kind="task",
            profile_revision=revision,
            credential_version=None,
            probe_id=uuid4(),
            capability=capability,
            tested_at=tested_at,
            accepted_at=tested_at,
        )
        for capability in capabilities
    }


def _evaluate(values: ConfigurationValues | None, records: dict | None = None, **kwargs):
    environment_id = kwargs.pop("environment_id", None) or uuid4()
    return evaluate_ai_readiness(
        values=values,
        registry=ProviderRegistry(environment_id=environment_id),
        activation_validation=records or {},
        credential_status=kwargs.pop("credential_status", lambda _ref: "available"),
        resource_exists=kwargs.pop("resource_exists", lambda _path: True),
        now=NOW,
        **kwargs,
    )


def test_persisted_acceptance_covers_all_required_capabilities() -> None:
    pid, revision = uuid4(), uuid4()
    provider = _profile(profile_id=pid, revision=revision)
    values = _values(provider=provider, bindings={"default_llm": _binding("default_llm", provider)})

    report = _evaluate(values, _acceptance(revision=revision))

    task = report.tasks["default_llm"]
    assert report.evaluated is True
    assert task.ready is True
    assert task.reason == "ready"
    assert task.activation_validation == "accepted"
    assert task.capability == ("json_object", "text")
    assert task.last_probe is not None
    assert task.last_probe.status == "succeeded"
    assert task.last_probe.activation_expires_at == NOW + timedelta(minutes=10)
    assert report.tasks["reviewer"].reason == "ready_rule_based"


def test_missing_acceptance_is_unverified_not_ready() -> None:
    pid, revision = uuid4(), uuid4()
    provider = _profile(profile_id=pid, revision=revision)
    values = _values(provider=provider, bindings={"default_llm": _binding("default_llm", provider)})

    task = _evaluate(values).tasks["default_llm"]

    assert task.configured is True
    assert task.activation_validation == "unverified"
    assert task.ready is False
    assert task.reason == "activation_unverified"
    assert task.last_probe is None


def test_profile_revision_change_requires_revalidation() -> None:
    pid, revision = uuid4(), uuid4()
    provider = _profile(profile_id=pid, revision=revision)
    values = _values(provider=provider, bindings={"default_llm": _binding("default_llm", provider)})

    task = _evaluate(values, _acceptance(revision=uuid4())).tasks["default_llm"]

    assert task.activation_validation == "revalidation_required"
    assert task.ready is False
    assert task.reason == "revalidation_required"


def test_acceptance_survives_the_ten_minute_window_for_readiness() -> None:
    pid, revision = uuid4(), uuid4()
    provider = _profile(profile_id=pid, revision=revision)
    values = _values(provider=provider, bindings={"default_llm": _binding("default_llm", provider)})
    old = NOW - timedelta(days=30)

    task = _evaluate(values, _acceptance(revision=revision, tested_at=old)).tasks["default_llm"]

    assert task.ready is True
    assert task.activation_validation == "accepted"
    assert task.last_probe is not None
    assert task.last_probe.tested_at == old
    assert task.last_probe.activation_expires_at == old + timedelta(minutes=10)


def test_missing_credential_is_reported_even_with_acceptance() -> None:
    pid, revision = uuid4(), uuid4()
    provider = _profile(
        profile_id=pid, revision=revision, auth_mode="api_key", credential_ref=None
    )
    values = _values(provider=provider, bindings={"default_llm": _binding("default_llm", provider)})

    task = _evaluate(values, _acceptance(revision=revision)).tasks["default_llm"]

    assert task.credential == "missing"
    assert task.ready is False
    assert task.reason == "credential_missing"


def test_capability_not_declared_is_unsupported() -> None:
    pid, revision = uuid4(), uuid4()
    provider = _profile(profile_id=pid, revision=revision)
    values = _values(
        provider=provider,
        bindings={
            "default_llm": _binding(
                "default_llm", provider, required=("vision",)
            )
        },
    )

    task = _evaluate(values, _acceptance(revision=revision)).tasks["default_llm"]

    assert task.configured is True
    assert task.ready is False
    assert task.reason == "capability_unsupported"


def test_disabled_ocr_is_not_required() -> None:
    pid, revision = uuid4(), uuid4()
    provider = _profile(
        profile_id=pid,
        revision=revision,
        adapter="rapidocr",
        capabilities=("ocr",),
        enabled=False,
        model="PP-OCRv5-mobile",
    )
    provider.pop("base_url", None)
    values = _values(provider=provider, bindings={"ocr": _binding("ocr", provider)})

    task = _evaluate(values).tasks["ocr"]

    assert task.configured is False
    assert task.ready is True
    assert task.reason == "not_required"


def test_retrieval_dependency_blocks_task_readiness() -> None:
    pid, revision = uuid4(), uuid4()
    provider = _profile(profile_id=pid, revision=revision)
    values = _values(
        provider=provider,
        bindings={
            "question_generation": _binding(
                "question_generation", provider, retrieval="hybrid_rerank"
            )
        },
    )

    report = _evaluate(values, _acceptance(revision=revision))

    assert report.tasks["embedding"].ready is False
    assert report.tasks["rerank"].ready is False
    task = report.tasks["question_generation"]
    assert task.ready is False
    assert task.reason == "dependency_not_ready"
    assert task.dependencies == ("embedding", "rerank")


def test_missing_active_configuration_is_explicit() -> None:
    report = _evaluate(None)

    assert report.evaluated is True
    assert report.status == "not_configured"
    assert report.reason == "no_active_configuration"
    assert report.tasks == {}


def test_credential_status_failure_is_never_available() -> None:
    pid, revision = uuid4(), uuid4()

    def broken(_ref: object) -> str:
        raise RuntimeError("synthetic credential backend failure")

    environment_id = uuid4()
    provider = _profile(
        profile_id=pid,
        revision=revision,
        auth_mode="api_key",
        credential_ref={
            "credential_id": str(uuid4()),
            "version": str(uuid4()),
            "environment_id": str(environment_id),
            "backend": "dpapi",
            "purpose": "provider",
        },
    )
    values = _values(provider=provider, bindings={"default_llm": _binding("default_llm", provider)})

    task = _evaluate(
        values,
        _acceptance(revision=revision),
        credential_status=broken,
        environment_id=environment_id,
    ).tasks["default_llm"]

    assert task.credential == "unavailable"
    assert task.ready is False
    assert task.reason == "credential_unavailable"
