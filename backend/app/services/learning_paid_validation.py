"""Explicit finite acceptance grants; never discovers credentials or sends IO.

The caller installs a reviewed manifest and a model-specific offline input
counter. No manifest is created by startup, confirmation or a business route.
"""

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from backend.app.schemas.personal_learning import LearningOperationView
from backend.app.schemas.settings import ProviderProfile
from backend.app.services.learning_operations import (
    LearningOperations,
    ModelBudget,
    StepPlan,
    digest,
)


class PaidOperationGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    operation_id: UUID
    purpose: str
    plan_digest: str = Field(min_length=64, max_length=64)
    snapshot_id: UUID
    snapshot_digest: str = Field(min_length=64, max_length=64)
    # Human-reviewable identities; references contain IDs/versions, never keys.
    task_bindings: dict[str, dict[str, Any]]
    request_budget: int = Field(ge=0, strict=True)
    input_token_budget: int = Field(ge=0, strict=True)
    output_token_budget: int = Field(ge=0, strict=True)


class PaidProbeGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    grant_id: UUID
    candidate_revision: UUID
    capability: str
    profile: ProviderProfile
    model_budget: ModelBudget


class PaidValidationManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    environment_id: UUID
    # An empty proposal supplies preview budgets but authorizes zero sends.
    grants: tuple[PaidOperationGrant, ...] = ()
    probe_grants: tuple[PaidProbeGrant, ...] = ()
    task_model_budgets: dict[str, ModelBudget]
    max_requests: int = Field(gt=0, strict=True)
    max_input_tokens: int = Field(gt=0, strict=True)
    max_output_tokens: int = Field(ge=0, strict=True)
    loopback_only: bool = False


class PaidValidationPolicy:
    """Each send checks the same finite set against persisted request facts.

    Summed *planned* maxima fit the grant before any send, so concurrent
    operations cannot borrow another approved operation's request/token quota.
    The estimated costs remain estimates, not a supplier invoice guarantee.
    """

    def __init__(self, manifest: PaidValidationManifest,
                 token_counter: Callable[[str, Any], int], *, probe_state_root: Path | None = None):
        self.manifest = manifest
        self.token_counter = token_counter
        self.probe_state_root = None if probe_state_root is None else probe_state_root.resolve(strict=True)
        if self.probe_state_root is not None and not self.probe_state_root.is_dir():
            raise ValueError("Probe state root must be an existing approved directory")

    @staticmethod
    def _operation_bounds(steps: list[dict[str, Any]]) -> tuple[int, int]:
        inputs = outputs = 0
        for saved in steps:
            step = StepPlan.model_validate({k: v for k, v in saved.items() if k in StepPlan.model_fields})
            for kind, count in (step.task_budgets or {step.task_kind: step.request_budget}).items():
                budget = step.model_budget(kind)
                if budget is not None:
                    inputs += count * budget.input_token_limit
                    outputs += count * (budget.output_token_limit or 0)
        return inputs, outputs

    def _within_limits(self) -> bool:
        manifest = self.manifest
        operation_ids = [grant.operation_id for grant in manifest.grants]
        probe_ids = [grant.grant_id for grant in manifest.probe_grants]
        return (len(operation_ids) == len(set(operation_ids))
            and len(probe_ids) == len(set(probe_ids))
            and sum(grant.request_budget for grant in manifest.grants) + len(probe_ids) <= manifest.max_requests
            and sum(grant.input_token_budget for grant in manifest.grants)
                + sum(grant.model_budget.input_token_limit for grant in manifest.probe_grants) <= manifest.max_input_tokens
            and sum(grant.output_token_budget for grant in manifest.grants)
                + sum(grant.model_budget.output_token_limit or 0 for grant in manifest.probe_grants) <= manifest.max_output_tokens)

    @staticmethod
    def grant_for(service: LearningOperations, operation_id: UUID) -> PaidOperationGrant:
        with service.sessions() as session:
            row = service._row(session, operation_id)
            snapshot = service.snapshots.load(row.runtime_snapshot_id)
            inputs, outputs = PaidValidationPolicy._operation_bounds(row.steps)
            return PaidOperationGrant(operation_id=row.id, purpose=row.purpose,
                plan_digest=row.plan_digest, snapshot_id=snapshot.snapshot_id,
                snapshot_digest=digest(snapshot.model_dump(mode="json")),
                task_bindings={kind: binding.model_dump(mode="json")
                    for kind, binding in snapshot.resolved_task_bindings.items()},
                request_budget=row.request_budget, input_token_budget=inputs, output_token_budget=outputs)

    def authorize(self, service: LearningOperations, view: LearningOperationView) -> bool:
        manifest = self.manifest
        ids = [grant.operation_id for grant in manifest.grants]
        if (manifest.environment_id != service.config.environment_id
                or not self._within_limits() or view.id not in ids):
            return False
        planned_requests = actual_requests = input_tokens = output_tokens = 0
        with service.sessions() as session:
            for grant in manifest.grants:
                row = service._row(session, grant.operation_id)
                snapshot = service.snapshots.load(row.runtime_snapshot_id)
                if (row.purpose != grant.purpose or row.plan_digest != grant.plan_digest
                        or row.request_budget != grant.request_budget
                        or self._operation_bounds(row.steps) != (grant.input_token_budget, grant.output_token_budget)
                        or snapshot.snapshot_id != grant.snapshot_id
                        or digest(snapshot.model_dump(mode="json")) != grant.snapshot_digest
                        or {kind: binding.model_dump(mode="json") for kind, binding
                            in snapshot.resolved_task_bindings.items()} != grant.task_bindings):
                    return False
                if manifest.loopback_only and any(
                    binding.base_url is not None and urlsplit(binding.base_url).hostname
                        not in {"127.0.0.1", "::1", "localhost"}
                    for binding in snapshot.resolved_task_bindings.values()
                ):
                    return False
                planned_requests += row.request_budget
                actual_requests += row.actual_requests
                for saved in row.steps:
                    step = StepPlan.model_validate({k: v for k, v in saved.items()
                        if k in StepPlan.model_fields})
                    for kind, count in (step.task_budgets or {step.task_kind: step.request_budget}).items():
                        if not count:
                            continue
                        binding = snapshot.resolved_task_bindings.get(kind)
                        if binding is not None and binding.adapter_id in {
                            "local", "bge", "huggingface", "cross_encoder"
                        }:
                            continue
                        budget = step.model_budget(kind)
                        if budget is None or budget != manifest.task_model_budgets.get(kind):
                            return False
                        input_tokens += count * budget.input_token_limit
                        output_tokens += count * (budget.output_token_limit or 0)
        return (actual_requests < manifest.max_requests
            and planned_requests <= manifest.max_requests
            and input_tokens <= manifest.max_input_tokens
            and output_tokens <= manifest.max_output_tokens)

    def probe_guard(self, *, environment_id: UUID, candidate_revision: UUID,
                    profile: ProviderProfile, capability: str) -> "ProbeValidationGuard":
        from backend.app.services.settings.probe_service import ProbeRequestError

        matches = [grant for grant in self.manifest.probe_grants
            if grant.candidate_revision == candidate_revision and grant.capability == capability
            and grant.profile.model_dump(mode="json", exclude={"verified_capabilities"})
                == profile.model_dump(mode="json", exclude={"verified_capabilities"})]
        if (environment_id != self.manifest.environment_id or not self._within_limits()
                or self.probe_state_root is None or len(matches) != 1
                or (self.manifest.loopback_only and urlsplit(profile.base_url or "").hostname
                    not in {"127.0.0.1", "::1", "localhost"})):
            raise ProbeRequestError("VALIDATION_NOT_AUTHORIZED", 409)
        grant = matches[0]
        budget = grant.model_budget
        if ((capability == "embedding" and (budget.embedding_dimension is None or budget.output_token_limit is not None
                    or budget.embedding_dimension != profile.parameters.get("dimension")))
                or (capability != "embedding" and (budget.output_token_limit is None
                    or budget.embedding_dimension is not None))):
            raise ProbeRequestError("VALIDATION_BUDGET_REQUIRED", 409)
        return ProbeValidationGuard(self, grant, self.probe_state_root / (str(grant.grant_id) + ".json"))


class ProbeValidationGuard:
    def __init__(self, policy: PaidValidationPolicy, grant: PaidProbeGrant, path: Path):
        self.policy, self.grant, self.path = policy, grant, path

    @property
    def max_tokens(self) -> int | None:
        return self.grant.model_budget.output_token_limit

    def reserve(self, body: dict[str, Any]) -> None:
        from backend.app.services.settings.probe_service import ProbeRequestError

        budget = self.grant.model_budget
        count = self.policy.token_counter(self.grant.profile.model_id, body)
        if (body.get("model") != self.grant.profile.model_id
                or type(count) is not int or not 0 <= count <= budget.input_token_limit
                or (self.grant.capability != "embedding" and body.get("max_tokens") != budget.output_token_limit)
                or (self.grant.capability == "embedding" and "max_tokens" in body)):
            raise ProbeRequestError("VALIDATION_BUDGET_CHANGED", 409)
        facts = {"grant_id": str(self.grant.grant_id), "state": "outcome_unknown",
            "environment_id": str(self.policy.manifest.environment_id),
            "request_digest": digest(body), "actual_requests": 1}
        try:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise ProbeRequestError("VALIDATION_PROBE_ALREADY_ATTEMPTED", 409) from None
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(facts, handle)
            handle.flush()
            os.fsync(handle.fileno())

    def received(self, status_code: int) -> None:
        facts = json.loads(self.path.read_text(encoding="utf-8"))
        facts.update(state="response_received", status_code=status_code)
        temporary = self.path.with_name(self.path.name + "." + uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                json.dump(facts, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
