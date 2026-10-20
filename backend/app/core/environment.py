"""Select a boot identity explicitly; no DB, provider, or directory side effects."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import NAMESPACE_URL, UUID, uuid5

from backend.app.schemas.settings import (
    BootstrapSettings,
    DatabaseIdentity,
    DatabaseLocator,
    EnvironmentName,
    EnvironmentProfile,
    OwnedResource,
    ResourceManifest,
    RuntimeKind,
    TestResourceProfile,
)


class EnvironmentSelectionError(ValueError):
    pass


def select_environment(
    *,
    cli_environment: str | None = None,
    process_env: Mapping[str, str] | None = None,
    runtime_kind: RuntimeKind = RuntimeKind.PYTHON,
) -> EnvironmentName:
    # Callers supply the process snapshot; only this named variable is inspected.
    if process_env is None:
        process_env = {"EDUAGENT_ENVIRONMENT": os.environ["EDUAGENT_ENVIRONMENT"]} if "EDUAGENT_ENVIRONMENT" in os.environ else {}
    selected = cli_environment
    if selected is None and process_env is not None:
        selected = process_env.get("EDUAGENT_ENVIRONMENT")
    if selected is None:
        selected = "demo" if runtime_kind == RuntimeKind.DOCKER else "development"
    try:
        return EnvironmentName(selected)
    except ValueError:
        raise EnvironmentSelectionError("INVALID_ENVIRONMENT") from None


def bootstrap_settings(
    *,
    environment_id: UUID,
    config_root: Path,
    runtime_kind: RuntimeKind = RuntimeKind.PYTHON,
    cli_environment: str | None = None,
    process_env: Mapping[str, str] | None = None,
) -> BootstrapSettings:
    if not config_root.is_absolute():
        raise EnvironmentSelectionError("ABSOLUTE_CONFIG_ROOT_REQUIRED")
    return BootstrapSettings(
        environment=select_environment(
            cli_environment=cli_environment,
            process_env=process_env,
            runtime_kind=runtime_kind,
        ),
        environment_id=environment_id,
        runtime_kind=runtime_kind,
        config_root=config_root.resolve(),
    )


def load_test_resource_profile(*, process_env: Mapping[str, str] | None = None) -> TestResourceProfile:
    """Read only an explicitly selected, non-secret Test resource receipt."""
    source = os.environ if process_env is None else process_env
    if source.get("EDUAGENT_ENVIRONMENT") != EnvironmentName.TEST:
        raise ValueError("EXPLICIT_TEST_ENVIRONMENT_REQUIRED")
    name = source.get("EDUAGENT_TEST_RESOURCE_PROFILE")
    if not name:
        raise ValueError("TEST_RESOURCE_PROFILE_REQUIRED")
    path = Path(name)
    if not path.is_absolute() or path != path.resolve():
        raise ValueError("ABSOLUTE_TEST_PROFILE_REQUIRED")
    try:
        profile = TestResourceProfile.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise ValueError("INVALID_TEST_RESOURCE_PROFILE") from exc
    identity = profile.database_identity
    if (
        profile.environment_id.int == 0
        or identity.database != "eduagent_test"
        or identity.user != "eduagent_test_runner"
        or identity.schema_name != "public"
        or not identity.instance_id.strip()
        or not profile.root.is_absolute()
        or profile.root != profile.root.resolve()
    ):
        raise ValueError("UNVERIFIED_TEST_RESOURCE_PROFILE")
    maintenance = profile.maintenance_identity
    if maintenance is not None and (
        maintenance.host != identity.host
        or maintenance.port != identity.port
        or maintenance.database != "postgres"
        or maintenance.user == identity.user
        or maintenance.schema_name != "public"
        or maintenance.instance_id != identity.instance_id
    ):
        raise ValueError("INVALID_TEST_MAINTENANCE_IDENTITY")
    return profile


def authorize_cleanup(
    record: ResourceManifest,
    resource: OwnedResource,
    *,
    actual_database: DatabaseIdentity | None,
    actual_redis_instance: str | None,
) -> None:
    """Authorize one exact creation receipt against the current Test target."""
    if record.environment != EnvironmentName.TEST or record.cleanup_state == "completed":
        raise ValueError("TEST_MANIFEST_REQUIRED")
    if resource not in record.resources or resource.run_id != record.run_id or not resource.owner:
        raise ValueError("RESOURCE_NOT_OWNED_BY_RUN")
    if record.database_identity is not None:
        if actual_database is None or actual_database != record.database_identity:
            raise ValueError("DATABASE_IDENTITY_MISMATCH")
        if not actual_database.instance_id.strip():
            raise ValueError("DATABASE_INSTANCE_UNVERIFIED")
    if record.redis_instance_id is not None and actual_redis_instance != record.redis_instance_id:
        raise ValueError("REDIS_INSTANCE_MISMATCH")

    if resource.kind == "schema":
        expected = record.database_identity
        if expected is None or resource.owner != expected.user:
            raise ValueError("SCHEMA_OWNER_MISMATCH")
        if resource.target != expected.schema_name or resource.target != f"test_{record.run_id.hex}":
            raise ValueError("SCHEMA_NOT_OWNED_BY_RUN")
    elif resource.kind == "file":
        root = record.root
        if not root.is_absolute() or root != root.resolve():
            raise ValueError("TEST_ROOT_UNVERIFIED")
        target = Path(resource.target)
        if not target.is_absolute():
            raise ValueError("FILE_TARGET_NOT_ABSOLUTE")
        resolved = target.resolve()
        if resolved == root or not resolved.is_relative_to(root):
            raise ValueError("FILE_TARGET_OUTSIDE_RUN")
    elif resource.kind == "redis_key":
        if record.redis_instance_id is None:
            raise ValueError("REDIS_INSTANCE_UNVERIFIED")
        prefix = f"test:{record.environment_id}:{record.run_id}:"
        if not resource.target.startswith(prefix) or resource.target == prefix:
            raise ValueError("REDIS_KEY_NOT_OWNED_BY_RUN")


def cleanup_owned_resources(
    record: ResourceManifest,
    *,
    actual_database: DatabaseIdentity | None,
    actual_redis_instance: str | None,
    delete: Callable[[OwnedResource], None],
) -> ResourceManifest:
    """Stop at first failure; never extend deletion beyond recorded resources."""
    for resource in record.resources:
        authorize_cleanup(
            record, resource,
            actual_database=actual_database,
            actual_redis_instance=actual_redis_instance,
        )
    for index, resource in enumerate(record.resources):
        try:
            # Recheck immediately before a destructive callback.
            authorize_cleanup(
                record, resource,
                actual_database=actual_database,
                actual_redis_instance=actual_redis_instance,
            )
            delete(resource)
        except Exception as exc:  # noqa: BLE001 - deletion callback can fail with any operational error
            return record.model_copy(update={
                "cleanup_state": "failed",
                "residual_targets": tuple(item.target for item in record.resources[index:]),
                "cleanup_error_type": type(exc).__name__,
            })
    return record.model_copy(update={
        "cleanup_state": "completed",
        "residual_targets": (),
        "cleanup_error_type": None,
    })


def _verified_path(path: Path) -> Path:
    if not path.is_absolute() or path != path.resolve():
        raise EnvironmentSelectionError("ENVIRONMENT_PATH_UNVERIFIED")
    return path


def resolve_environment_profile(
    *, process_env: Mapping[str, str] | None = None, storage_root: Path | None = None,
    runtime_kind: RuntimeKind = RuntimeKind.PYTHON,
) -> EnvironmentProfile:
    """Select internal paths without creating directories, importing AI, or migrating.

    The legacy Development storage remains selected until explicit adoption. A
    deterministic legacy identity is only a label, NEVER a resource ownership grant.
    Test uses its T199 creation receipt; Demo's launcher supplies its registered ID.
    """
    source = os.environ if process_env is None else process_env
    runtime_kind = RuntimeKind(source.get("EDUAGENT_RUNTIME_KIND", runtime_kind))
    selected = select_environment(process_env=source, runtime_kind=runtime_kind)
    database: DatabaseLocator | None = None
    if selected == EnvironmentName.TEST:
        receipt = load_test_resource_profile(process_env=source)
        root = receipt.root
        identity = receipt.environment_id
        database = receipt.database_identity
        if source.get("EDUAGENT_DATA_ROOT") and Path(source["EDUAGENT_DATA_ROOT"]) != root:
            raise EnvironmentSelectionError("TEST_ROOT_MISMATCH")
    else:
        supplied = source.get("EDUAGENT_DATA_ROOT")
        if supplied:
            root = Path(supplied)
        elif runtime_kind == RuntimeKind.DOCKER:
            raise EnvironmentSelectionError("DOCKER_ENVIRONMENT_ROOT_REQUIRED")
        else:
            user = Path(source.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
            root = user / "EduAgent" / selected.value
        _verified_path(root)
        identifier = source.get("EDUAGENT_ENVIRONMENT_ID")
        internal = root / "config" / "environment-profile.json"
        if identifier:
            identity = UUID(identifier)
        elif internal.exists():
            _verified_path(internal)
            registered = EnvironmentProfile.model_validate_json(internal.read_bytes())
            if registered.environment != selected or registered.config_root.parent != root:
                raise EnvironmentSelectionError("ENVIRONMENT_PROFILE_MISMATCH")
            identity = registered.environment_id
        else:
            # Stable unadopted identity; it cannot authorize a database/volume or cleanup.
            identity = uuid5(NAMESPACE_URL, "eduagent:unadopted:" + os.path.normcase(str(root)))
    _verified_path(root)
    policy = "explicit"
    target = root / "storage"
    if selected == EnvironmentName.DEVELOPMENT and not source.get("EDUAGENT_DATA_ROOT"):
        if storage_root is None:
            if getattr(sys, "frozen", False):
                user = Path(source.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
                target = user / "EduAgent" / "storage"
            else:
                target = Path(__file__).resolve().parents[3] / "storage"
        else:
            target = storage_root
        policy = "legacy_compatibility"
    elif storage_root is not None and storage_root != target:
        raise EnvironmentSelectionError("STORAGE_ENVIRONMENT_MISMATCH")
    return EnvironmentProfile(
        environment=selected, environment_id=identity, runtime_kind=runtime_kind,
        config_root=root / "config", storage_root=_verified_path(target),
        cache_root=root / "cache", log_root=root / "logs", temp_root=root / "temp",
        results_root=root / "results", database_locator=database, source_policy=policy,
    )


def environment_path(profile: EnvironmentProfile, category: str, relative: str = "") -> Path:
    """Recheck canonical paths at the operation boundary, including later junctions."""
    profile.validate_paths()
    fields = {"config": "config_root", "storage": "storage_root", "cache": "cache_root",
              "logs": "log_root", "temp": "temp_root", "results": "results_root", "credentials": "credentials_root"}
    if category not in fields:
        raise EnvironmentSelectionError("UNKNOWN_ENVIRONMENT_PATH")
    _verified_path(profile.config_root.parent)
    base = _verified_path(getattr(profile, fields[category]))
    if not relative:
        return base
    if (":" in relative or "\\" in relative or PureWindowsPath(relative).drive
            or PurePosixPath(relative).is_absolute()
            or any(part in {"", ".", ".."} for part in relative.split("/"))):
        raise EnvironmentSelectionError("ENVIRONMENT_RELATIVE_PATH_INVALID")
    target = base / relative
    _verified_path(target)
    if target == base or not target.is_relative_to(base):
        raise EnvironmentSelectionError("ENVIRONMENT_PATH_ESCAPE")
    return target


def resolve_results_directory(
    directory: Path | None, *, profile: EnvironmentProfile | None = None,
    process_env: Mapping[str, str] | None = None,
) -> Path:
    source = os.environ if process_env is None else process_env
    selected = profile or resolve_environment_profile(process_env=source)
    base = environment_path(selected, "results")
    if directory is None:
        return base
    target = directory.resolve()
    # Existing synthetic callers may explicitly supply their own tmp_path; a
    # selected environment/profile cannot bypass its results boundary.
    controlled = profile is not None or "EDUAGENT_ENVIRONMENT" in source or "EDUAGENT_DATA_ROOT" in source
    if controlled and not target.is_relative_to(base):
        raise EnvironmentSelectionError("RESULTS_OUTSIDE_ENVIRONMENT")
    if directory.is_absolute() and directory != target:
        raise EnvironmentSelectionError("RESULTS_PATH_UNVERIFIED")
    return _verified_path(target)


def result_file_path(directory: Path, filename: str) -> Path:
    """Check each output target before writes, retaining regular-file semantics."""
    base = _verified_path(directory)
    if (not filename or filename in {".", ".."} or "/" in filename
            or "\\" in filename or ":" in filename or PureWindowsPath(filename).drive):
        raise EnvironmentSelectionError("RESULT_FILE_NAME_INVALID")
    return _verified_path(base / filename)


def validate_result_run_id(run_id: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run_id) or run_id in {".", ".."}:
        raise EnvironmentSelectionError("RESULT_RUN_ID_INVALID")


def preview_legacy_resources(
    *, legacy_database: DatabaseLocator | None, legacy_storage_root: Path,
    proposed_profile: EnvironmentProfile,
) -> dict[str, object]:
    """Read-only preview; no copying, SQL, implicit acceptance, or deletion."""
    _verified_path(legacy_storage_root)
    proposed = environment_path(proposed_profile, "storage")
    same_database = legacy_database is not None and legacy_database == proposed_profile.database_locator
    conflicts = []
    if same_database and proposed != legacy_storage_root:
        conflicts.append("LEGACY_DATABASE_STORAGE_MISMATCH")
    if not legacy_storage_root.is_dir():
        conflicts.append("LEGACY_STORAGE_UNAVAILABLE")
    return {
        "environment": proposed_profile.environment.value,
        "legacy_storage_root": str(legacy_storage_root), "proposed_storage_root": str(proposed),
        "same_database": same_database, "requires_confirmation": True, "conflicts": conflicts,
        "moves_files": False, "deletes_files": False,
    }


def prepare_runtime_paths(profile: EnvironmentProfile) -> None:
    """Prepare only owned operational directories, not an empty replacement storage.

    Called once by a process owner before Gradio/model imports, never to hot-switch
    running clients. Old caches/temp files are neither copied nor removed.
    """
    directories = {name: environment_path(profile, name) for name in ("cache", "logs", "temp")}
    for name, path in directories.items():
        path.mkdir(parents=True, exist_ok=True)
        environment_path(profile, name)
    cache, temp = directories["cache"], directories["temp"]
    os.environ.update({
        "HF_HOME": str(cache / "huggingface"), "HF_HUB_CACHE": str(cache / "huggingface" / "hub"),
        "SENTENCE_TRANSFORMERS_HOME": str(cache / "sentence-transformers"),
        "GRADIO_TEMP_DIR": str(temp / "gradio"),
        "XDG_CACHE_HOME": str(cache), "TORCH_HOME": str(cache / "torch"),
        "TMPDIR": str(temp), "TMP": str(temp), "TEMP": str(temp),
    })
    tempfile.tempdir = str(temp)

