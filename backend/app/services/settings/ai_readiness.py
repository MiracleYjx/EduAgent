"""逐任务 AI 就绪：只读既有事实，不调用 Probe、模型或外部服务。

来源仅限：active 修订中的绑定/Profile、Registry 实际声明能力、凭据后端状态、
已持久化的 ``activation_validation``、本地资源目录与任务依赖。没有接纳证据时
返回 ``unverified``；身份/版本变化返回 ``revalidation_required``；绝不把
"Registry 声称支持"当成真实模型可用。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from backend.app.schemas.settings import (
    ActivationValidation,
    AiProbeView,
    AiReadiness,
    AiTaskReadiness,
    ConfigurationValues,
    CredentialReference,
    CredentialState,
    ProviderProfile,
)
from backend.app.services.settings.provider_registry import (
    ProviderRegistry,
    RegistryValidationError,
    ResolvedBinding,
)

TASK_KINDS: tuple[str, ...] = (
    "default_llm",
    "question_generation",
    "subjective_grading",
    "diagnosis",
    "content_validation",
    "paper_extraction",
    "reviewer",
    "embedding",
    "rerank",
    "vision",
    "ocr",
)

ACCEPTANCE_WINDOW = timedelta(minutes=10)

CredentialFact = Literal[
    "available", "missing", "unavailable", "not_required", "not_configured"
]
ActivationFact = Literal[
    "accepted", "unverified", "revalidation_required", "not_applicable"
]

CredentialStatusSource = Callable[[CredentialReference], CredentialState | None]
ResourceExists = Callable[[Path], bool]


def _credential_fact(
    profile: ProviderProfile | None,
    *,
    status_of: CredentialStatusSource | None,
) -> CredentialFact:
    if profile is None:
        return "not_configured"
    if profile.auth_mode == "none":
        return "not_required"
    reference = profile.credential_ref
    if reference is None:
        return "missing"
    if status_of is None:
        return "unavailable"
    try:
        status = status_of(reference)
    except Exception:  # noqa: BLE001 - 凭据状态失败不得变成“可用”
        return "unavailable"
    if status == "available":
        return "available"
    if status == "absent":
        return "missing"
    return "unavailable"


def _acceptance(
    *,
    profile: ProviderProfile,
    required: tuple[str, ...],
    records: Mapping[str, ActivationValidation],
) -> tuple[ActivationFact, AiProbeView | None]:
    """按能力匹配持久接纳；不依赖调用方传入当前 Probe 结果。"""

    reference = profile.credential_ref
    accepted: list[ActivationValidation] = []
    seen_other: list[ActivationValidation] = []
    for record in records.values():
        if record.capability not in required:
            continue
        if record.profile_revision != profile.profile_revision:
            seen_other.append(record)
            continue
        if reference is not None and record.credential_version != reference.version:
            seen_other.append(record)
            continue
        accepted.append(record)
    if required and len({item.capability for item in accepted}) == len(set(required)):
        oldest = min(accepted, key=lambda item: item.tested_at)
        return (
            "accepted",
            AiProbeView(
                status="succeeded",
                tested_at=oldest.tested_at,
                activation_expires_at=oldest.tested_at + ACCEPTANCE_WINDOW,
            ),
        )
    if seen_other or accepted:
        return ("revalidation_required", None)
    return ("unverified", None)


def _base_task(
    *,
    task_kind: str,
    values: ConfigurationValues,
    registry: ProviderRegistry,
    records: Mapping[str, ActivationValidation],
    status_of: CredentialStatusSource | None,
    resource_exists: ResourceExists | None,
    now: datetime,
) -> tuple[AiTaskReadiness, ResolvedBinding | None, ProviderProfile | None]:
    del now  # 接纳记录本身没有过期租约；窗口只用于展示。
    if task_kind == "reviewer":
        return (
            AiTaskReadiness(
                configured=True,
                credential="not_required",
                activation_validation="not_applicable",
                ready=True,
                reason="ready_rule_based",
            ),
            None,
            None,
        )
    try:
        resolved = registry.resolve_binding(
            task_kind=task_kind,
            profiles=values.providers,
            bindings=values.bindings,
        )
    except RegistryValidationError as error:
        code = error.code
        if task_kind == "ocr" and code in {
            "INDEPENDENT_BINDING_REQUIRED",
            "PROFILE_BINDING_REQUIRED",
            "PROFILE_DISABLED",
        }:
            return (
                AiTaskReadiness(
                    configured=False,
                    credential="not_required",
                    activation_validation="not_applicable",
                    ready=True,
                    reason="not_required",
                ),
                None,
                None,
            )
        reason = {
            "PROFILE_DISABLED": "profile_disabled",
            "CAPABILITY_UNSUPPORTED": "capability_unsupported",
            "PROFILE_BINDING_MISSING": "binding_invalid",
            "PROFILE_REVISION_MISMATCH": "binding_invalid",
            "INVALID_PROVIDER_PROFILE": "binding_invalid",
        }.get(code, "not_configured")
        return (
            AiTaskReadiness(
                configured=code not in {
                    "INDEPENDENT_BINDING_REQUIRED",
                    "PROFILE_BINDING_REQUIRED",
                    "TASK_INHERITS_DEFAULT",
                },
                ready=False,
                reason=reason,
            ),
            None,
            None,
        )
    profile = values.providers.get(str(resolved.profile_id))
    if profile is None:
        return (
            AiTaskReadiness(configured=True, ready=False, reason="binding_invalid"),
            resolved,
            None,
        )
    required = tuple(sorted(resolved.required_capabilities))
    credential = _credential_fact(profile, status_of=status_of)
    activation, last_probe = _acceptance(
        profile=profile, required=required, records=records
    )
    resource_ok = True
    if profile.resource_directory is not None:
        resource_ok = bool(
            resource_exists(profile.resource_directory)
            if resource_exists is not None
            else profile.resource_directory.is_dir()
        )
    reasons: list[str] = []
    if credential == "missing":
        reasons.append("credential_missing")
    elif credential == "unavailable":
        reasons.append("credential_unavailable")
    if not resource_ok:
        reasons.append("resource_unavailable")
    if task_kind == "ocr" and profile.auth_mode == "none" and not profile.enabled:
        return (
            AiTaskReadiness(
                configured=False,
                capability=required,
                credential=credential,
                activation_validation="not_applicable",
                ready=True,
                reason="not_required",
            ),
            resolved,
            profile,
        )
    if activation == "unverified":
        reasons.append("activation_unverified")
    elif activation == "revalidation_required":
        reasons.append("revalidation_required")
    ready = not reasons
    return (
        AiTaskReadiness(
            configured=True,
            capability=required,
            credential=credential,
            activation_validation=activation,
            last_probe=last_probe,
            ready=ready,
            reason="ready" if ready else reasons[0],
            dependencies=resolved.dependencies,
        ),
        resolved,
        profile,
    )


def evaluate_ai_readiness(
    *,
    values: ConfigurationValues | None,
    registry: ProviderRegistry,
    activation_validation: Mapping[str, ActivationValidation] | None = None,
    credential_status: CredentialStatusSource | None = None,
    resource_exists: ResourceExists | None = None,
    now: datetime | None = None,
) -> AiReadiness:
    """计算逐任务状态；没有 active 配置时明确未配置，而不是伪造全部可用。"""

    if values is None:
        return AiReadiness(
            evaluated=True,
            status="not_configured",
            reason="no_active_configuration",
            tasks={},
        )
    current = now or datetime.now().astimezone()
    records = activation_validation or {}
    tasks: dict[str, AiTaskReadiness] = {}
    resolutions: dict[str, ResolvedBinding | None] = {}
    for task_kind in TASK_KINDS:
        task, resolved, _profile = _base_task(
            task_kind=task_kind,
            values=values,
            registry=registry,
            records=records,
            status_of=credential_status,
            resource_exists=resource_exists,
            now=current,
        )
        tasks[task_kind] = task
        resolutions[task_kind] = resolved
    # 第二遍只处理声明依赖；嵌入/重排未就绪时，依赖它们的任务不得宣称可用。
    for task_kind, resolved in resolutions.items():
        task = tasks[task_kind]
        if resolved is None or not task.ready or not resolved.dependencies:
            continue
        blocked = [
            dependency
            for dependency in resolved.dependencies
            if not tasks.get(dependency, AiTaskReadiness()).ready
        ]
        if blocked:
            tasks[task_kind] = task.model_copy(
                update={
                    "ready": False,
                    "reason": "dependency_not_ready",
                    "dependencies": tuple(resolved.dependencies),
                }
            )
    return AiReadiness(
        evaluated=True,
        status="evaluated",
        reason="per_task",
        tasks=tasks,
    )


__all__ = ["ACCEPTANCE_WINDOW", "TASK_KINDS", "evaluate_ai_readiness"]
