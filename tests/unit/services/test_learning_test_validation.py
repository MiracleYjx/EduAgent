"""The frozen artifact's fake-supplier seam cannot authorize a remote provider."""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest


def profile(root, environment="test"):
    return SimpleNamespace(environment=environment, environment_id=uuid4(), config_root=root / "config")


def test_non_test_environment_never_loads_test_authorization(tmp_path):
    from backend.app.services.learning_test_validation import load_test_validation
    selected = profile(tmp_path, "development")
    (tmp_path / "paid-validation-test.json").write_text("invalid", "utf-8")
    assert load_test_validation(selected) is None


def test_test_proposal_supplies_budgets_but_no_send_grants(tmp_path):
    from backend.app.services.learning_test_validation import load_test_validation
    selected = profile(tmp_path)
    (tmp_path / "paid-validation-test.json").write_text(json.dumps({
        "environment_id":str(selected.environment_id), "loopback_only":True,
        "task_model_budgets":{}, "max_requests":1, "max_input_tokens":100,
        "max_output_tokens":100}), "utf-8")
    policy = load_test_validation(selected)
    assert policy.manifest.grants == ()
    assert policy.token_counter("synthetic-artifact", {"input":"hello"}) > 0
    with pytest.raises(ValueError, match="SYNTHETIC_MODEL_REQUIRED"):
        policy.token_counter("real-vendor-model", {"input":"hello"})


def test_non_loopback_manifest_and_wrong_space_are_rejected(tmp_path):
    from backend.app.services.learning_test_validation import load_test_validation
    selected = profile(tmp_path)
    path = tmp_path / "paid-validation-test.json"
    base = {"environment_id":str(selected.environment_id), "loopback_only":False,
        "task_model_budgets":{}, "max_requests":1,"max_input_tokens":100,"max_output_tokens":100}
    path.write_text(json.dumps(base), "utf-8")
    with pytest.raises(ValueError, match="TEST_VALIDATION_SCOPE_INVALID"):
        load_test_validation(selected)
    base.update(loopback_only=True, environment_id=str(uuid4()))
    path.write_text(json.dumps(base), "utf-8")
    with pytest.raises(ValueError, match="TEST_VALIDATION_SCOPE_INVALID"):
        load_test_validation(selected)
