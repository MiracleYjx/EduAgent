"""Atomic, non-secret settings storage with a single process writer.

No business connections, provider construction, probe, migration or restart here.
The manifest alone names active/pending revisions; reads never repair corruption.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import sys
import threading
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Literal, Self
from uuid import UUID, uuid4

from pydantic import SecretStr, ValidationError

from backend.app.core.config import resolve_configuration
from backend.app.schemas.settings import (
    ActivationValidation,
    ConfigurationManifest,
    ConfigurationValues,
    CredentialReference,
    FieldSource,
    RecoveryRevisionView,
    RecoveryView,
    ResolvedConfiguration,
    SettingsRevision,
)
from backend.app.services.settings.credential_store import (
    CredentialStore,
    CredentialStoreError,
)
from backend.app.services.settings.mounted_secrets import (
    MountedSecretError,
    MountedSecretStore,
)
from backend.app.services.settings.provider_registry import RegistryValidationError


class ConfigServiceError(ValueError):
    def __init__(self, code: str, *, stage: str = "read", revision: UUID | None = None):
        super().__init__(code)
        self.code = code
        self.stage = stage
        self.revision = revision
        self.status_code = 409 if code in {"REVISION_CONFLICT", "CONFIG_WRITER_LOCKED"} else 422


class ConfigService:
    def __init__(self, root: Path, environment_id: UUID, *, writable: bool = True):
        if not root.is_absolute():
            raise ConfigServiceError("ABSOLUTE_CONFIG_ROOT_REQUIRED")
        self.root = root.resolve()
        self.environment_id = environment_id
        self.writable = writable
        self.config = self.root / "config"
        self._mutex = threading.RLock()
        self._lock_file: BinaryIO | None = None
        self._closed = False
        self._credential_store: CredentialStore | MountedSecretStore | None = None
        self._credential_backend: str | None = None
        self._registry: Any = None
        self._probes: Any = None
        self._business_guard: Any = None
        self._runtime_references: Any = None
        self._legacy_runs: Any = None
        self._clock: Any = lambda: datetime.now(UTC)
        if writable:
            try:
                for directory in (self.config, self.config / "drafts", self.config / "revisions"):
                    self._guard_path(directory).mkdir(parents=True, exist_ok=True)
                self._acquire_writer()
            except OSError:
                self.close()
                raise ConfigServiceError("CONFIG_IO_ERROR", stage="writer_lock") from None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_) -> None:
        self.close()

    @staticmethod
    def _canonical(value: object) -> bytes:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")

    @contextmanager
    def reference_transaction(self):
        """Outermost transaction shared by config publication and deletion."""
        with self._mutex:
            yield

    @property
    def credential_store(self) -> CredentialStore | MountedSecretStore | None:
        """Owned backend for trusted internal services; None until opened."""
        return self._credential_store

    @property
    def provider_registry(self) -> Any:
        """Admission registry, when an owner wired one; never constructs providers."""
        return self._registry

    def _require_store(self) -> CredentialStore | MountedSecretStore:
        if self._credential_store is None:
            raise ConfigServiceError("CREDENTIAL_BACKEND_UNAVAILABLE", stage="credentials")
        return self._credential_store

    def _receipt_path(self) -> Path:
        return self._guard_path(self.config / "credential-backend.json")

    def _read_receipt(self) -> dict[str, Any] | None:
        """The initialization record lives outside credentials/ on purpose."""
        path = self._receipt_path()
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or set(data) != {
                "schema_version", "environment_id", "backend", "instance", "created_at", "integrity",
            } or type(data["schema_version"]) is not int or data["schema_version"] != 1):
                raise ValueError
            integrity = data.pop("integrity")
            if (not isinstance(integrity, str)
                    or not hmac.compare_digest(integrity, hashlib.sha256(self._canonical(data)).hexdigest())):
                raise ValueError
            if data["backend"] not in {"dpapi", "mounted_secret"} or not isinstance(data["instance"], str):
                raise ValueError
        except (OSError, ValueError, KeyError):
            raise ConfigServiceError("CREDENTIAL_RECOVERY_REQUIRED", stage="credentials") from None
        if data["environment_id"] != str(self.environment_id):
            raise ConfigServiceError("ENVIRONMENT_MISMATCH", stage="credentials")
        return data

    def _write_receipt(self, backend: str) -> None:
        target = self._receipt_path()
        if target.exists():
            raise ConfigServiceError("CREDENTIAL_RECOVERY_REQUIRED", stage="credentials")
        payload = {"schema_version": 1, "environment_id": str(self.environment_id), "backend": backend,
                   "instance": uuid4().hex, "created_at": datetime.now(UTC).isoformat()}
        payload["integrity"] = hashlib.sha256(self._canonical(payload)).hexdigest()
        self._persist(target, payload, "credential_receipt")

    def _vault_has_entries(self) -> bool:
        vault = self._guard_path(self.root / "credentials")
        try:
            return any(item.name != "writer.lock" for item in vault.iterdir())
        except FileNotFoundError:
            return False
        except OSError:
            raise ConfigServiceError("CREDENTIAL_RECOVERY_REQUIRED", stage="credentials") from None

    def _verify_vault(self, backend: str) -> None:
        vault = self._guard_path(self.root / "credentials")
        if not vault.is_dir():
            raise ConfigServiceError("CREDENTIAL_RECOVERY_REQUIRED", stage="credentials")
        name = "environment.json" if backend == "dpapi" else "mounted-environment.json"
        path = self._guard_path(vault / name)
        if not path.is_file():
            raise ConfigServiceError("CREDENTIAL_RECOVERY_REQUIRED", stage="credentials")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or set(data) != {"schema_version", "environment_id"}
                    or type(data["schema_version"]) is not int or data["schema_version"] != 1):
                raise ValueError
        except (OSError, ValueError, KeyError):
            raise ConfigServiceError("CREDENTIAL_RECOVERY_REQUIRED", stage="credentials") from None
        if data["environment_id"] != str(self.environment_id):
            raise ConfigServiceError("ENVIRONMENT_MISMATCH", stage="credentials")

    def open_credentials(self, *, backend: str = "dpapi", protector: Any = None, file_security: Any = None,
                         mounts: Any = None) -> CredentialStore | MountedSecretStore:
        """Open the owned backend once; a lost vault is never silently re-initialized."""
        with self._mutex:
            self._require_writer()
            if backend not in {"dpapi", "mounted_secret"}:
                raise ConfigServiceError("CREDENTIAL_BACKEND_UNSUPPORTED", stage="credentials")
            if self._credential_store is not None:
                if self._credential_backend != backend:
                    raise ConfigServiceError("CREDENTIAL_BACKEND_MISMATCH", stage="credentials")
                return self._credential_store
            receipt = self._read_receipt()
            if receipt is None:
                if self._vault_has_entries():
                    # A vault without this environment's initialization record is
                    # never treated as a fresh bootstrap.
                    raise ConfigServiceError("CREDENTIAL_RECOVERY_REQUIRED", stage="credentials")
                fresh = True
            else:
                if receipt["backend"] != backend:
                    raise ConfigServiceError("CREDENTIAL_BACKEND_MISMATCH", stage="credentials")
                self._verify_vault(backend)
                fresh = False
            try:
                store: CredentialStore | MountedSecretStore
                if backend == "dpapi":
                    store = CredentialStore(self.root, self.environment_id, protector=protector,
                                            reference_index=self, file_security=file_security, mutex=self._mutex)
                else:
                    store = MountedSecretStore(self.root, self.environment_id,
                                               mounts=mounts if mounts is not None else {},
                                               file_security=file_security)
            except CredentialStoreError as error:
                raise ConfigServiceError(error.code, stage=error.stage) from None
            except MountedSecretError as error:
                raise ConfigServiceError(error.code, stage=error.stage) from None
            self._credential_store = store
            self._credential_backend = backend
            if fresh:
                try:
                    self._write_receipt(backend)
                except BaseException:
                    self._credential_store = None
                    self._credential_backend = None
                    store.close()
                    raise
            return store

    def configure_admission(self, *, registry: Any, probe_service: Any, business_guard: Any = None,
                            runtime_references: Any = None, legacy_runs: Any = None,
                            clock: Any = None) -> None:
        """Wire trusted server-side facts; without them admission stays closed."""
        with self._mutex:
            if (not callable(getattr(registry, "resolve_binding", None))
                    or not callable(getattr(probe_service, "get", None))
                    or not callable(getattr(probe_service, "assess_evidence", None))
                    or getattr(probe_service, "environment_id", self.environment_id) != self.environment_id):
                raise ConfigServiceError("INVALID_ACTIVATION_DEPENDENCIES", stage="admission")
            if business_guard is not None and not callable(business_guard):
                raise ConfigServiceError("INVALID_ACTIVATION_DEPENDENCIES", stage="admission")
            if runtime_references is not None and not callable(runtime_references):
                raise ConfigServiceError("INVALID_ACTIVATION_DEPENDENCIES", stage="admission")
            if legacy_runs is not None and not callable(legacy_runs):
                raise ConfigServiceError("INVALID_ACTIVATION_DEPENDENCIES", stage="admission")
            self._registry = registry
            self._probes = probe_service
            self._business_guard = business_guard
            self._runtime_references = runtime_references
            self._legacy_runs = legacy_runs
            self._clock = clock if callable(clock) else (lambda: datetime.now(UTC))

    @staticmethod
    def _revision_references(revision: SettingsRevision) -> tuple[CredentialReference, ...]:
        values = revision.values
        found = [values.business.database_password_ref, values.business.redis_password_ref,
                 values.business.jwt_secret_ref]
        found.extend(profile.credential_ref for profile in values.providers.values())
        return tuple(item for item in found if item is not None)

    def references(self, reference: CredentialReference, version: UUID) -> tuple[str, ...]:
        """Facts for deletion; incomplete runtime coverage is unknown, not empty."""
        with self._mutex:
            facts: list[str] = []
            manifest = self.read_manifest()
            for attribute, fact in (("active_revision", "active"), ("pending_restart_revision", "pending_restart")):
                revision_id = getattr(manifest, attribute)
                if revision_id is None:
                    continue
                revision = self.get_revision(revision_id)
                if any(item == reference and version == item.version
                       for item in self._revision_references(revision)):
                    facts.append(fact)
            if facts:
                return tuple(facts)
            source = self._runtime_references
            if source is None:
                raise ConfigServiceError("REFERENCE_INDEX_UNAVAILABLE", stage="references")
            try:
                extra = source(reference, version)
            except ConfigServiceError:
                raise
            except Exception:  # noqa: BLE001 - 非终态事实源失败不得降为“无引用”。
                raise ConfigServiceError("REFERENCE_INDEX_UNAVAILABLE", stage="references") from None
            if not isinstance(extra, (tuple, list)) or any(not isinstance(fact, str) for fact in extra):
                raise ConfigServiceError("REFERENCE_INDEX_UNAVAILABLE", stage="references")
            return tuple(extra)

    @staticmethod
    def _expected_purpose(path: tuple) -> str | None:
        if len(path) == 2 and path[0] == "business":
            return {"database_password_ref": "database", "redis_password_ref": "redis",
                    "jwt_secret_ref": "jwt"}.get(path[1])
        if len(path) == 3 and path[0] == "providers" and path[2] == "credential_ref":
            return "provider"
        return None

    def create_draft_with_secrets(self, values: ConfigurationValues | dict,
                                  writes: Mapping[tuple, tuple[str, SecretStr]], *,
                                  expected_revision: UUID | None,
                                  process_env: dict[str, str] | None = None,
                                  acknowledge_override: bool = False) -> SettingsRevision:
        """Write candidate credentials first, then the draft; never the pointer."""
        with self._mutex:
            self._require_writer()
            self._expected(expected_revision)
            document = (values.model_dump(mode="json") if isinstance(values, ConfigurationValues)
                        else copy.deepcopy(dict(values)))
            prepared: list[tuple[tuple, str, SecretStr]] = []
            for path, item in dict(writes or {}).items():
                if (not isinstance(path, tuple) or not 2 <= len(path) <= 3
                        or not isinstance(item, tuple) or len(item) != 2):
                    raise ConfigServiceError("INVALID_SECRET_WRITE_PATH", stage="credentials")
                purpose, secret = item
                node: Any = document
                for key in path[:-1]:
                    if not isinstance(node, dict) or not isinstance(node.get(key), dict):
                        raise ConfigServiceError("INVALID_SECRET_WRITE_PATH", stage="credentials")
                    node = node[key]
                if not isinstance(node, dict) or path[-1] not in node or node[path[-1]] is not None:
                    raise ConfigServiceError("INVALID_SECRET_WRITE_PATH", stage="credentials")
                if purpose != self._expected_purpose(path):
                    raise ConfigServiceError("INVALID_SECRET_WRITE_PATH", stage="credentials")
                if not isinstance(secret, SecretStr) or not secret.get_secret_value().strip():
                    raise ConfigServiceError("INVALID_SECRET_WRITE", stage="credentials")
                prepared.append((path, purpose, secret))
            self._values(document)
            if not prepared:
                return self.create_draft(document, expected_revision=expected_revision,
                                         process_env=process_env, acknowledge_override=acknowledge_override)
            store = self._require_store()
            for path, purpose, secret in prepared:
                reference = store.put(purpose=purpose, secret_value=secret)
                node = document
                for key in path[:-1]:
                    node = node[key]
                node[path[-1]] = reference.model_dump(mode="json")
            # A later failure keeps written candidates as isolated evidence; the
            # previous active revision and its credentials stay untouched.
            return self.create_draft(document, expected_revision=expected_revision,
                                     process_env=process_env, acknowledge_override=acknowledge_override)

    def _check_credentials(self, values: ConfigurationValues, provider_ids: set) -> None:
        store = self._credential_store
        if store is None:
            raise ConfigServiceError("CREDENTIAL_BACKEND_UNAVAILABLE", stage="credentials")
        references = []
        business = values.business
        for field, purpose in (("database_password_ref", "database"), ("redis_password_ref", "redis"),
                               ("jwt_secret_ref", "jwt")):
            reference = getattr(business, field)
            if reference is None:
                continue
            if reference.purpose != purpose or reference.environment_id != self.environment_id:
                raise ConfigServiceError("INVALID_CREDENTIAL_REFERENCE", stage="credentials")
            references.append(reference)
        for profile in values.providers.values():
            reference = profile.credential_ref
            if reference is None or profile.profile_id not in provider_ids:
                continue
            if reference.purpose != "provider" or reference.environment_id != self.environment_id:
                raise ConfigServiceError("INVALID_CREDENTIAL_REFERENCE", stage="credentials")
            references.append(reference)
        for reference in references:
            try:
                status = store.status(reference, reference.version)
                state = getattr(status, "status", None)
            except Exception:  # noqa: BLE001 - 凭据后端失败不得携秘密逃出此边界。
                state = None
            if state != "available":
                raise ConfigServiceError("CREDENTIAL_UNAVAILABLE", stage="credentials")
            if reference.purpose == "jwt":
                # 值只在内存中读取一次用于强度判断；错误只给稳定码，不含秘密。
                try:
                    value = store.get(reference, reference.version).get_secret_value()
                except Exception:  # noqa: BLE001 - 读取失败同样收敛为不可用。
                    raise ConfigServiceError("CREDENTIAL_UNAVAILABLE", stage="credentials") from None
                if len(value.strip()) < 32:
                    raise ConfigServiceError("JWT_SECRET_TOO_WEAK", stage="credentials")

    def _ai_targets(self, values: ConfigurationValues, candidate_revision: UUID | None) -> dict[str, tuple]:
        result: dict[str, tuple] = {}
        for task_kind in sorted(values.bindings):
            if task_kind == "reviewer":
                continue
            binding = values.bindings[task_kind]
            if binding.profile_id is None and not binding.inherit_default:
                # 显式空引用表示未配置（例如向导“跳过 AI”）；不制造 disabled
                # Provider，也不让深合并遗留的旧绑定继续生效。
                continue
            try:
                resolved = self._registry.resolve_binding(task_kind=task_kind, profiles=values.providers,
                                                          bindings=values.bindings)
            except RegistryValidationError as error:
                code = getattr(error, "code", None)
                raise ConfigServiceError(code if isinstance(code, str) and code else "INVALID_PROVIDER_CONFIGURATION",
                                         stage="activation") from None
            if resolved.mode == "rule_based" or resolved.profile_id is None or resolved.profile_revision is None:
                continue
            profile = values.providers.get(str(resolved.profile_id))
            if profile is None:
                raise ConfigServiceError("PROFILE_BINDING_MISSING", stage="activation")
            credential = profile.credential_ref
            retrieval = getattr(values.bindings.get(task_kind), "retrieval_mode", None) or ""
            parameters = json.dumps(profile.parameters, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), default=str)
            for capability in resolved.required_capabilities:
                identity = (profile.profile_id, profile.profile_revision, profile.adapter_id, profile.model_id,
                            profile.base_url or "", parameters,
                            credential.backend if credential else "",
                            credential.credential_id if credential else None,
                            credential.version if credential else None,
                            retrieval, tuple(resolved.required_capabilities), capability)
                target = {
                    "candidate_revision": candidate_revision, "profile_id": profile.profile_id,
                    "profile_revision": profile.profile_revision, "model_id": profile.model_id,
                    "capability": capability, "environment_id": self.environment_id,
                    "credential_id": credential.credential_id if credential else None,
                    "credential_version": credential.version if credential else None,
                }
                result[f"{task_kind}:{capability}"] = (identity, target, task_kind, capability)
        return result

    def _acceptance_facts(self, revision: SettingsRevision, record: ActivationValidation,
                          identity: tuple) -> dict[str, Any]:
        """Project persisted acceptance into the pure evidence shape; no new facts."""
        return {
            "status": revision.status, "environment_id": self.environment_id,
            "profile_id": identity[0], "profile_revision": identity[1],
            "credential_id": identity[7], "credential_version": identity[8],
            "model_id": identity[3], "capability": identity[11],
            "probe_id": record.probe_id, "tested_at": record.tested_at, "accepted_at": record.accepted_at,
        }

    def _admission_acceptance(self, values: ConfigurationValues, old: SettingsRevision | None,
                              candidate_revision: UUID,
                              probe_ids: Sequence[UUID]) -> dict[str, ActivationValidation]:
        guard = self._business_guard
        if not callable(guard) or guard(old.values if old is not None else None, values) is not True:
            raise ConfigServiceError("ACTIVATION_GATE_REQUIRED", stage="activation")
        probes = self._probes
        if probes is None:
            raise ConfigServiceError("ACTIVATION_GATE_REQUIRED", stage="activation")
        targets = self._ai_targets(values, candidate_revision)
        self._check_credentials(values, {item[0][0] for item in targets.values()})
        if not targets:
            return {}
        accepted: dict[str, ActivationValidation] = {}
        unresolved: list[tuple[str, dict, str, str]] = []
        old_targets = self._ai_targets(old.values, None) if old is not None else {}
        for key, (identity, target, task_kind, capability) in targets.items():
            record = old.activation_validation.get(key) if old is not None else None
            if record is not None and old is not None and identity == old_targets.get(key, (None,))[0]:
                outcome = probes.assess_evidence(
                    target=target, probe=record,
                    activation_validation=self._acceptance_facts(old, record, identity))
                if isinstance(outcome, dict) and outcome.get("valid") is True:
                    accepted[key] = record
                    continue
            unresolved.append((key, target, task_kind, capability))
        if not unresolved:
            return accepted
        if not probe_ids:
            raise ConfigServiceError("PROBE_REQUIRED", stage="activation")
        evidence = []
        for probe_id in probe_ids:
            if not isinstance(probe_id, UUID):
                raise ConfigServiceError("INVALID_PROBE_REQUEST", stage="activation")
            try:
                evidence.append(probes.get(probe_id))
            except Exception as error:  # noqa: BLE001 - Probe边界已收敛为安全码。
                code = getattr(error, "code", None)
                raise ConfigServiceError(code if isinstance(code, str) and code else "PROBE_NOT_FOUND",
                                         stage="activation") from None
        for key, target, task_kind, capability in unresolved:
            chosen = None
            expired = False
            for result in evidence:
                outcome = probes.assess_evidence(target=target, probe=result)
                if isinstance(outcome, dict) and outcome.get("valid") is True:
                    chosen = result
                    break
                expired = expired or (isinstance(outcome, dict) and outcome.get("code") == "PROBE_EXPIRED")
            if chosen is None:
                raise ConfigServiceError("PROBE_EXPIRED" if expired else "PROBE_REQUIRED", stage="activation")
            accepted[key] = ActivationValidation(
                task_kind=task_kind, profile_revision=chosen.profile_revision,
                credential_version=chosen.credential_version, probe_id=chosen.probe_id,
                capability=capability, tested_at=chosen.tested_at, accepted_at=self._clock())
        return accepted

    def _changes_ai_targets(self, old: SettingsRevision | None, values: ConfigurationValues) -> bool:
        """模型/策略相关的激活面；业务与引导段变更不属于本门禁。"""
        if old is None:
            return True
        return (old.values.providers != values.providers
                or old.values.bindings != values.bindings
                or old.values.policies != values.policies)

    def _require_legacy_gate(self, old: SettingsRevision | None, values: ConfigurationValues) -> None:
        """无快照非终态运行存在时，拒绝相关模型/策略激活；事实未知不降为放行。"""
        source = self._legacy_runs
        if source is None:
            return
        # 只有会影响遗留运行的模型/策略变更才需要该事实；纯业务/引导段变更不得因
        # 暂时无法读取运行索引而被误伤（T212 门禁语义本身不变）。
        if not self._changes_ai_targets(old, values):
            return
        try:
            facts = source()
        except ConfigServiceError:
            raise
        except Exception:  # noqa: BLE001 - 事实源失败不得当作“无遗留运行”。
            raise ConfigServiceError("LEGACY_RUN_INDEX_UNAVAILABLE", stage="activation") from None
        if not isinstance(facts, (tuple, list)):
            raise ConfigServiceError("LEGACY_RUN_INDEX_UNAVAILABLE", stage="activation")
        legacy = 0
        for fact in facts:
            if not isinstance(fact, Mapping):
                raise ConfigServiceError("LEGACY_RUN_INDEX_UNAVAILABLE", stage="activation")
            if not fact.get("snapshot_id"):
                legacy += 1
        if legacy == 0:
            return
        raise ConfigServiceError("LEGACY_RUN_ACTIVATION_BLOCKED", stage="activation")

    def _guard_path(self, path: Path) -> Path:
        if not path.resolve().is_relative_to(self.root) or path.resolve() == self.root:
            raise ConfigServiceError("CONFIG_PATH_ESCAPE")
        return path

    def _acquire_writer(self) -> None:
        lock_path = self._guard_path(self.config / "writer.lock")
        handle = lock_path.open("a+b")
        try:
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise ConfigServiceError("CONFIG_WRITER_LOCKED", stage="writer_lock") from None
        self._lock_file = handle

    def close(self) -> None:
        pending_store, self._credential_store = self._credential_store, None
        self._credential_backend = None
        try:
            if pending_store is not None:
                pending_store.close()
        finally:
            self._close_writer()

    def _close_writer(self) -> None:
        with self._mutex:
            handle = self._lock_file
            if handle is not None:
                try:
                    handle.seek(0)
                    if sys.platform == "win32":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                finally:
                    handle.close()
                    self._lock_file = None
            self._closed = True

    def _require_writer(self) -> None:
        if self._closed or not self.writable or self._lock_file is None:
            raise ConfigServiceError("CONFIG_READ_ONLY", stage="write")

    def _read(self, path: Path, model):
        self._guard_path(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError):
            raise ConfigServiceError("CORRUPT_CONFIGURATION") from None
        except OSError:
            raise ConfigServiceError("CONFIG_READ_ERROR") from None
        if not isinstance(data, dict):
            raise ConfigServiceError("CORRUPT_CONFIGURATION")
        if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
            raise ConfigServiceError("UNSUPPORTED_SCHEMA")
        try:
            parsed = model.model_validate(data)
        except ValidationError:
            raise ConfigServiceError("CORRUPT_CONFIGURATION") from None
        if parsed.environment_id != self.environment_id:
            raise ConfigServiceError("ENVIRONMENT_MISMATCH")
        return parsed

    def read_manifest(self) -> ConfigurationManifest:
        path = self._guard_path(self.config / "manifest.json")
        if not path.exists():
            revisions = self._guard_path(self.config / "revisions")
            if revisions.exists() and any(revisions.glob("*.json")):
                raise ConfigServiceError("MANIFEST_MISSING")
            return ConfigurationManifest(environment_id=self.environment_id)
        result = self._read(path, ConfigurationManifest)
        # A valid pointer to a damaged/missing revision is also unavailable.
        for revision in (result.active_revision, result.pending_restart_revision):
            if revision is not None:
                self.get_revision(revision)
        return result

    def get_revision(self, revision_id: UUID) -> SettingsRevision:
        revision_id = UUID(str(revision_id))
        result = self._read(self.config / "revisions" / f"{revision_id}.json", SettingsRevision)
        if result.revision_id != revision_id:
            raise ConfigServiceError("REVISION_ID_MISMATCH")
        if result.activated_at is None or result.status not in {"active", "pending_restart"}:
            raise ConfigServiceError("CORRUPT_CONFIGURATION")
        self._values(result.values)
        return result

    def get_draft(self, revision_id: UUID) -> SettingsRevision:
        revision_id = UUID(str(revision_id))
        result = self._read(self.config / "drafts" / f"{revision_id}.json", SettingsRevision)
        if result.revision_id != revision_id:
            raise ConfigServiceError("REVISION_ID_MISMATCH")
        return result

    def active_revision(self) -> SettingsRevision | None:
        manifest = self.read_manifest()
        return self.get_revision(manifest.active_revision) if manifest.active_revision else None

    def _expected(self, expected_revision: UUID | None) -> ConfigurationManifest:
        current = self.read_manifest()
        if current.head_revision != expected_revision:
            raise ConfigServiceError("REVISION_CONFLICT", stage="concurrency", revision=current.head_revision)
        return current

    def _atomic_write(self, path: Path, payload: Any, stage: str) -> None:
        self._guard_path(path)
        content = payload if isinstance(payload, bytes) else (
            json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        ).encode("utf-8")
        temporary = self._guard_path(path.with_name(f".{path.name}.{uuid4().hex}.tmp"))
        # An interrupted temporary/orphan revision stays as evidence. No automatic cleanup.
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def _persist(self, path: Path, payload: Any, stage: str) -> None:
        try:
            self._atomic_write(path, payload, stage)
        except OSError:
            raise ConfigServiceError("CONFIG_IO_ERROR", stage=stage) from None

    def resolve(
        self, *, mounted_config: dict | None = None, dotenv_path: Path | None = None,
        process_env: dict[str, str] | None = None, defaults: dict | None = None,
    ) -> ResolvedConfiguration:
        active = self.active_revision() if mounted_config is None else None
        saved = mounted_config if mounted_config is not None else (
            active.values.model_dump(mode="json") if active is not None else None
        )
        return resolve_configuration(
            defaults=defaults, saved=saved, dotenv_path=dotenv_path, process_env=process_env,
            saved_source="mounted_config" if mounted_config is not None else "saved",
        )

    def _values(self, values: ConfigurationValues | dict) -> ConfigurationValues:
        try:
            parsed = ConfigurationValues.model_validate(
                values.model_dump(mode="json") if isinstance(values, ConfigurationValues) else values
            )
        except ValidationError:
            raise ConfigServiceError("INVALID_CONFIGURATION", stage="schema") from None
        refs = [
            parsed.business.database_password_ref, parsed.business.redis_password_ref,
            parsed.business.jwt_secret_ref,
            *(profile.credential_ref for profile in parsed.providers.values()),
        ]
        if any(ref is not None and ref.environment_id != self.environment_id for ref in refs):
            raise ConfigServiceError("ENVIRONMENT_MISMATCH", stage="schema")
        return parsed

    def create_draft(
        self, values: ConfigurationValues | dict, *, expected_revision: UUID | None,
        process_env: dict[str, str] | None = None, acknowledge_override: bool = False,
    ) -> SettingsRevision:
        with self._mutex:
            self._require_writer()
            self._expected(expected_revision)
            parsed = self._values(values)
            resolved = resolve_configuration(saved=parsed.model_dump(mode="json"), process_env=process_env)
            overridden = {field for field, source in resolved.sources.items() if source.source == "process_env"}
            if overridden and not acknowledge_override:
                raise ConfigServiceError("OVERRIDE_ACKNOWLEDGEMENT_REQUIRED", stage="draft")
            sources = dict(resolved.sources)
            for field in overridden:
                sources[field] = FieldSource(source="process_env", effective=False)
            draft = SettingsRevision(
                revision_id=uuid4(), parent_revision=expected_revision, environment_id=self.environment_id,
                values=parsed, created_at=datetime.now(UTC), source_metadata=sources,
            )
            self._persist(self.config / "drafts" / f"{draft.revision_id}.json",
                          draft.model_dump(mode="json"), "draft")
            return draft

    def _validate_business(self, values: ConfigurationValues) -> None:
        business = values.business
        if (
            business.database_locator is None or business.redis_locator is None
            or business.storage_root is None
        ):
            raise ConfigServiceError("BUSINESS_CONFIGURATION_INCOMPLETE", stage="validation")
        # Personal configuration needs no business signer. Legacy assembly still
        # requires JWT in SettingsRuntime.app_settings_for and AppSettings;
        # configured JWT references retain the existing credential validation.

    def validate(
        self, draft_id: UUID, *, expected_revision: UUID | None,
        process_env: dict[str, str] | None = None,
    ) -> SettingsRevision:
        with self._mutex:
            self._require_writer()
            self._expected(expected_revision)
            draft = self.get_draft(draft_id)
            if draft.parent_revision != expected_revision:
                raise ConfigServiceError("REVISION_CONFLICT", stage="validation")
            parsed = self._values(draft.values)
            resolved = resolve_configuration(saved=parsed.model_dump(mode="json"), process_env=process_env)
            if resolved.errors:
                raise ConfigServiceError("INVALID_CONFIGURATION", stage="validation")
            self._validate_business(parsed)
            validated = draft.model_copy(update={"status": "validated"})
            self._persist(self.config / "drafts" / f"{draft_id}.json",
                          validated.model_dump(mode="json"), "validation")
            return validated

    def _check_ai_gate(self, values: ConfigurationValues) -> None:
        # Probe/index/runtime admission arrives in T210/T215/T220. Until then
        # this repository cannot manufacture an accepted enabled AI binding.
        if any(binding.task_kind != "reviewer" or binding.profile_id is not None or binding.inherit_default for binding in values.bindings.values()):
            raise ConfigServiceError("ACTIVATION_GATE_REQUIRED", stage="activation")

    def activate(
        self, draft_id: UUID, *, expected_revision: UUID | None,
        process_env: dict[str, str] | None = None,
        acknowledge_override: bool = False,
        probe_ids: Sequence[UUID] = (),
    ) -> SettingsRevision:
        with self._mutex:
            self._require_writer()
            manifest = self._expected(expected_revision)
            draft = self.get_draft(draft_id)
            if draft.parent_revision != expected_revision:
                raise ConfigServiceError("REVISION_CONFLICT", stage="activation")
            if draft.status != "validated":
                raise ConfigServiceError("VALIDATION_REQUIRED", stage="activation")
            values = self._values(draft.values)
            resolved = resolve_configuration(saved=values.model_dump(mode="json"), process_env=process_env)
            if resolved.errors:
                raise ConfigServiceError("INVALID_CONFIGURATION", stage="activation")
            if any(source.source == "process_env" for source in resolved.sources.values()) and not acknowledge_override:
                raise ConfigServiceError("OVERRIDE_ACKNOWLEDGEMENT_REQUIRED", stage="activation")
            self._validate_business(values)
            old = self.get_revision(manifest.active_revision) if manifest.active_revision else None
            self._require_legacy_gate(old, values)
            if self._registry is None:
                self._check_ai_gate(values)
                acceptance: dict[str, ActivationValidation] = {}
            else:
                acceptance = self._admission_acceptance(values, old, draft.revision_id, tuple(probe_ids))
            restart = manifest.pending_restart_revision is not None or (
                old is not None and (
                    old.values.business != values.business or old.values.bootstrap != values.bootstrap
                )
            )
            status: Literal["pending_restart", "active"] = "pending_restart" if restart else "active"
            revision = SettingsRevision(
                revision_id=uuid4(), parent_revision=manifest.head_revision,
                environment_id=self.environment_id, values=values, created_at=draft.created_at,
                activated_at=datetime.now(UTC), source_metadata=draft.source_metadata,
                activation_validation=acceptance,
                status=status,
            )
            target = self.config / "revisions" / f"{revision.revision_id}.json"
            if target.exists():
                raise ConfigServiceError("IMMUTABLE_REVISION_EXISTS", stage="revision")
            self._persist(target, revision.model_dump(mode="json"), "revision")
            updated = ConfigurationManifest(
                environment_id=self.environment_id,
                active_revision=manifest.active_revision if restart else revision.revision_id,
                pending_restart_revision=revision.revision_id if restart else None,
            )
            # Nothing that can fail or write follows this sole commit point.
            self._persist(self.config / "manifest.json", updated.model_dump(mode="json"), "manifest")
            return revision

    def check_pending_restart(
        self, *, expected_revision: UUID, process_env: dict[str, str] | None = None,
    ) -> SettingsRevision:
        """启动本地门禁：只检查 schema/凭据/既有接纳，绝不再次执行 Probe。"""
        with self._mutex:
            self._require_writer()
            manifest = self._expected(expected_revision)
            if manifest.pending_restart_revision != expected_revision:
                raise ConfigServiceError("REVISION_CONFLICT", stage="startup")
            revision = self.get_revision(expected_revision)
            values = self._values(revision.values)
            resolved = resolve_configuration(saved=values.model_dump(mode="json"), process_env=process_env)
            if resolved.errors:
                raise ConfigServiceError("INVALID_CONFIGURATION", stage="startup")
            self._validate_business(values)
            if self._registry is None:
                raise ConfigServiceError("ACTIVATION_GATE_REQUIRED", stage="startup")
            # old=该已接纳修订，使持久 acceptance 走沿用分支；缺证据仍闭锁，
            # 绝不拿过期的未接纳结果补齐，也不访问工作流/外部数据库。
            self._admission_acceptance(values, revision, revision.revision_id, ())
            return revision

    def accept_pending_restart(
        self, *, expected_revision: UUID, process_env: dict[str, str] | None = None,
    ) -> ConfigurationManifest:
        """仅由新应用工厂在前置/装配成功后调用，原子提升现有不可变修订。"""
        with self._mutex:
            revision = self.check_pending_restart(
                expected_revision=expected_revision, process_env=process_env,
            )
            updated = ConfigurationManifest(
                environment_id=self.environment_id, active_revision=revision.revision_id,
                pending_restart_revision=None,
            )
            self._persist(self.config / "manifest.json", updated.model_dump(mode="json"), "startup_manifest")
            return updated

    def recovery_view(self) -> RecoveryView:
        """Read owned revision candidates and a damaged-pointer concurrency token."""
        with self._mutex:
            damaged = False
            current = None
            fingerprint = None
            try:
                current = self.read_manifest().head_revision
            except ConfigServiceError as error:
                if error.code not in {"CORRUPT_CONFIGURATION", "UNSUPPORTED_SCHEMA", "CONFIG_READ_ERROR", "MANIFEST_MISSING"}:
                    raise
                damaged = True
                path = self._guard_path(self.config / "manifest.json")
                try:
                    fingerprint = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "MISSING"
                except OSError:
                    raise ConfigServiceError("CONFIG_READ_ERROR", stage="restore") from None
            revisions = []
            directory = self._guard_path(self.config / "revisions")
            for path in sorted(directory.glob("*.json")):
                try:
                    identifier = UUID(path.stem)
                except ValueError:
                    continue
                try:
                    revision = self.get_revision(identifier)
                except ConfigServiceError as error:
                    revisions.append(RecoveryRevisionView(revision_id=identifier, available=False, error_code=error.code))
                else:
                    revisions.append(RecoveryRevisionView(revision_id=identifier, available=True,
                        created_at=revision.created_at, status=revision.status, values=revision.values))
            return RecoveryView(current_revision=current, damaged=damaged,
                manifest_fingerprint=fingerprint, revisions=tuple(revisions))

    def restore_revision(
        self, revision_id: UUID, *, expected_revision: UUID | None,
        expected_manifest_fingerprint: str | None = None,
    ) -> ConfigurationManifest:
        with self._mutex:
            self._require_writer()
            revision = self.get_revision(revision_id)
            self._values(revision.values)
            self._validate_business(revision.values)
            self._require_legacy_gate(None, revision.values)
            if self._registry is None:
                self._check_ai_gate(revision.values)
            else:
                guard = self._business_guard
                if not callable(guard) or guard(None, revision.values) is not True:
                    raise ConfigServiceError("ACTIVATION_GATE_REQUIRED", stage="restore")
                targets = self._ai_targets(revision.values, None)
                self._check_credentials(revision.values, {item[0][0] for item in targets.values()})
            path = self._guard_path(self.config / "manifest.json")
            damaged = False
            try:
                current = self._expected(expected_revision)
            except ConfigServiceError as exc:
                if exc.code not in {"CORRUPT_CONFIGURATION", "UNSUPPORTED_SCHEMA", "CONFIG_READ_ERROR", "MANIFEST_MISSING"}:
                    raise
                if expected_manifest_fingerprint is None:
                    raise ConfigServiceError("RESTORE_CONFIRMATION_REQUIRED", stage="restore") from None
                if exc.code == "MANIFEST_MISSING":
                    if expected_manifest_fingerprint != "MISSING":
                        raise ConfigServiceError("RESTORE_CONFIRMATION_REQUIRED", stage="restore") from None
                else:
                    try:
                        original = path.read_bytes()
                    except OSError:
                        raise ConfigServiceError("CONFIG_READ_ERROR", stage="restore") from None
                    if hashlib.sha256(original).hexdigest() != expected_manifest_fingerprint:
                        raise ConfigServiceError("REVISION_CONFLICT", stage="restore") from None
                    self._persist(self.config / f"manifest-recovery-{uuid4()}.json", original, "restore_evidence")
                current = ConfigurationManifest(environment_id=self.environment_id)
                damaged = True
            updated = ConfigurationManifest(
                environment_id=self.environment_id,
                active_revision=None if damaged else current.active_revision,
                pending_restart_revision=revision.revision_id,
            )
            # Recovery is explicitly staged for restart, never a live business swap.
            self._persist(path, updated.model_dump(mode="json"), "restore_manifest")
            return updated

