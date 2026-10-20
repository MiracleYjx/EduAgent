"""TCR-S1-ENV-CONFIG-002: T198 red contracts, implemented by T199 later.

Missing entry points are reported separately from the existing-helper behavior
gap in test_environment_contract. No xfail or weakened safety assertions.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from backend.app.core import environment
from backend.app.schemas.settings import (
    DatabaseIdentity,
    OwnedResource,
    ResourceManifest,
)
from backend.app.schemas.settings import TestResourceProfile as ResourceProfile


def manifest(root, *, kind="schema", target=None):
    run = uuid4()
    identity = DatabaseIdentity(host="test.invalid", database="synthetic_test",
        user="runner", schema_name=f"test_{run.hex}", instance_id="isolated-instance")
    item = OwnedResource(kind=kind, target=target or identity.schema_name, owner="runner",
                         run_id=run, created_at=datetime.now(UTC))
    return ResourceManifest(environment="test", environment_id=uuid4(), run_id=run,
        database_identity=identity, redis_instance_id="isolated-redis", root=root, resources=(item,))


def authorize(record, resource, *, actual_database=None, actual_redis_instance="isolated-redis"):
    guard = getattr(environment, "authorize_cleanup", None)
    assert callable(guard), "T199 authorize_cleanup boundary is not implemented"
    return guard(record, resource, actual_database=actual_database or record.database_identity,
                 actual_redis_instance=actual_redis_instance)


@pytest.mark.parametrize("field,value", [
    ("database", "development"), ("user", "developer"), ("instance_id", "other-instance"),
    ("host", "demo.invalid"), ("schema_name", "public"),
])
def test_actual_connection_mismatch_rejected_before_delete(tmp_path, field, value):
    record = manifest(tmp_path)
    actual = record.database_identity.model_copy(update={field: value})
    deletions = []
    with pytest.raises(ValueError):
        authorize(record, record.resources[0], actual_database=actual)
        deletions.append(record.resources[0].target)
    assert deletions == []


@pytest.mark.parametrize("env", ["development", "demo"])
def test_non_test_profile_rejected(tmp_path, env):
    record = manifest(tmp_path).model_copy(update={"environment": env})
    with pytest.raises(ValueError):
        authorize(record, record.resources[0])


def test_public_and_other_run_schema_rejected(tmp_path):
    record = manifest(tmp_path)
    for item in [record.resources[0].model_copy(update={"target": "public"}),
                 record.resources[0].model_copy(update={"run_id": uuid4()}),
                 record.resources[0].model_copy(update={"target": "test_other_run"})]:
        with pytest.raises(ValueError):
            authorize(record, item)


@pytest.mark.parametrize("target_kind", ["parent", "outside", "root", "prefix_collision"])
def test_file_escape_rejected_and_non_test_sentinel_unchanged(tmp_path, target_kind):
    root = tmp_path / "test"
    root.mkdir()
    sentinel = tmp_path / "demo" / "sentinel"
    sentinel.parent.mkdir()
    sentinel.write_bytes(b"synthetic-demo-untouched")
    target = {"parent": root / "..", "outside": sentinel, "root": root,
              "prefix_collision": tmp_path / "test-other" / "file"}[target_kind]
    record = manifest(root, kind="file", target=str(target))
    with pytest.raises(ValueError):
        authorize(record, record.resources[0])
    assert sentinel.read_bytes() == b"synthetic-demo-untouched"


def test_resolved_reparse_escape_rejected_with_fake_path(monkeypatch, tmp_path):
    from pathlib import Path
    root = tmp_path / "test"
    root.mkdir()
    escaping = root / "junction" / "sentinel"
    outside = tmp_path / "demo" / "sentinel"
    original = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda path, *a, **k:
                        outside if path == escaping else original(path, *a, **k))
    record = manifest(root, kind="file", target=str(escaping))
    with pytest.raises(ValueError):
        authorize(record, record.resources[0])


@pytest.mark.parametrize("target,instance", [
    ("FLUSHALL", "isolated-redis"), ("FLUSHDB", "isolated-redis"),
    ("*", "isolated-redis"), ("test:other-run:key", "isolated-redis"),
    ("test:own:key", "development-redis"),
])
def test_redis_full_or_unowned_cleanup_rejected(tmp_path, target, instance):
    record = manifest(tmp_path, kind="redis_key", target=target)
    with pytest.raises(ValueError):
        authorize(record, record.resources[0], actual_redis_instance=instance)


def test_cleanup_failure_reports_residuals_without_expanding_targets(tmp_path):
    record = manifest(tmp_path)
    cleanup = getattr(environment, "cleanup_owned_resources", None)
    assert callable(cleanup), "T199 cleanup_owned_resources boundary is not implemented"
    attempted = []
    def delete(item):
        attempted.append(item.target)
        raise OSError("synthetic-cleanup-failure")
    result = cleanup(record, actual_database=record.database_identity,
                     actual_redis_instance="isolated-redis", delete=delete)
    assert attempted == [record.resources[0].target]
    assert result.cleanup_state == "failed"
    assert result.residual_targets == (record.resources[0].target,)


def test_only_owned_valid_target_can_be_authorized(tmp_path):
    root = tmp_path / "test"
    root.mkdir()
    sentinel = tmp_path / "demo" / "sentinel"
    sentinel.parent.mkdir()
    sentinel.write_bytes(b"demo-remains")
    record = manifest(root)
    authorize(record, record.resources[0], actual_database=record.database_identity)
    assert sentinel.read_bytes() == b"demo-remains"


def test_owned_cleanup_completes_only_recorded_resource(tmp_path):
    record = manifest(tmp_path)
    attempted = []
    result = environment.cleanup_owned_resources(record,
        actual_database=record.database_identity,
        actual_redis_instance=record.redis_instance_id,
        delete=lambda item: attempted.append(item.target))
    assert attempted == [record.resources[0].target]
    assert result.cleanup_state == "completed"
    assert result.residual_targets == ()


def test_invalid_later_receipt_rejects_before_any_cleanup(tmp_path):
    record = manifest(tmp_path)
    invalid = record.resources[0].model_copy(update={"target": "public"})
    record = record.model_copy(update={"resources": (*record.resources, invalid)})
    attempted = []
    with pytest.raises(ValueError):
        environment.cleanup_owned_resources(record,
            actual_database=record.database_identity,
            actual_redis_instance=record.redis_instance_id,
            delete=lambda item: attempted.append(item.target))
    assert attempted == []


def test_resource_profile_requires_explicit_test_identity(tmp_path):
    identity = DatabaseIdentity(host="test.invalid", database="eduagent_test",
        user="eduagent_test_runner", schema_name="public", instance_id="isolated-instance")
    profile = ResourceProfile(environment="test", environment_id=uuid4(),
        database_identity=identity, root=tmp_path)
    path = tmp_path / "profile.json"
    path.write_text(profile.model_dump_json(), encoding="utf-8")
    process = {"EDUAGENT_ENVIRONMENT": "test", "EDUAGENT_TEST_RESOURCE_PROFILE": str(path)}
    assert environment.load_test_resource_profile(process_env=process) == profile
    with pytest.raises(ValueError, match="EXPLICIT_TEST_ENVIRONMENT_REQUIRED"):
        environment.load_test_resource_profile(process_env={**process, "EDUAGENT_ENVIRONMENT": "development"})
    wrong = profile.model_copy(update={"database_identity": identity.model_copy(update={"database": "development"})})
    path.write_text(wrong.model_dump_json(), encoding="utf-8")
    with pytest.raises(ValueError, match="UNVERIFIED_TEST_RESOURCE_PROFILE"):
        environment.load_test_resource_profile(process_env=process)

