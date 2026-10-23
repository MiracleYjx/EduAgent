"""Explicit Test-root only seam for frozen-artifact loopback supplier acceptance.

The file is written by the owning test operator, never an HTTP endpoint. Real
supplier acceptance uses separately reviewed in-process policy and tokenizer.
"""
import json
from typing import Any
from urllib.parse import urlsplit

from backend.app.services.learning_paid_validation import (
    PaidValidationManifest,
    PaidValidationPolicy,
)


def _counter(model: str, body: Any) -> int:
    if not model.startswith("synthetic-"):
        raise ValueError("SYNTHETIC_MODEL_REQUIRED")
    # Only synthetic loopback protocols use this conservative byte budget.
    # It is not a tokenizer or a price estimate for any supplier model.
    return len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def load_test_validation(profile) -> PaidValidationPolicy | None:
    environment = getattr(profile.environment, "value", profile.environment)
    if environment != "test":
        return None
    root = profile.config_root.parent
    path = root / "paid-validation-test.json"
    if not path.exists():
        return None
    if path.is_symlink() or path.resolve().parent != root.resolve():
        raise ValueError("TEST_VALIDATION_SCOPE_INVALID")
    with path.open("rb") as source:
        data = source.read(262145)
    if len(data) > 262144:
        raise ValueError("TEST_VALIDATION_SCOPE_INVALID")
    manifest = PaidValidationManifest.model_validate_json(data)
    if not manifest.loopback_only or manifest.environment_id != profile.environment_id:
        raise ValueError("TEST_VALIDATION_SCOPE_INVALID")
    bindings = [binding for grant in manifest.grants for binding in grant.task_bindings.values()]
    bindings += [grant.profile.model_dump(mode="json") for grant in manifest.probe_grants]
    for binding in bindings:
        endpoint = binding.get("base_url")
        if endpoint is not None and (urlsplit(endpoint).hostname not in {"127.0.0.1", "::1"}
                or not str(binding.get("model_id", "")).startswith("synthetic-")):
            raise ValueError("TEST_VALIDATION_SCOPE_INVALID")
    state_root = root / "paid-validation-probes"
    state_root.mkdir(exist_ok=True)
    if state_root.resolve().parent != root.resolve() or state_root.is_symlink():
        raise ValueError("TEST_VALIDATION_SCOPE_INVALID")
    return PaidValidationPolicy(manifest, _counter, probe_state_root=state_root)


def refresh_test_validation(state) -> None:
    runtime = getattr(state, "settings_runtime", None)
    profile = getattr(runtime, "profile", None)
    if profile is None or getattr(profile.environment, "value", profile.environment) != "test":
        return
    policy = load_test_validation(profile)
    if policy is not None:
        state.learning_paid_validation = policy
        state.test_paid_validation_loaded = True
    elif getattr(state, "test_paid_validation_loaded", False):
        state.learning_paid_validation = None
        state.test_paid_validation_loaded = False
