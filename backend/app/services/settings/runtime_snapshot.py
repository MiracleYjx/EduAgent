"""不可变运行快照（T212）。

契约依据：``specs/001-local-settings-and-onboarding/plan.md`` §6、``data-model.md`` §6、
``contracts/runtime-configuration.md`` §3–§4。

职责边界：

- **只承载非敏感事实**：快照记录生效修订、解析后的任务绑定、Provider 身份、请求模型、
  Prompt/Schema 版本、检索与置信策略、Embedding 身份、凭据引用/版本与创建时间；任何秘密值、
  SDK 客户端或会话对象都不进入快照文件。
- **不可变**：写入 ``config/snapshots/<snapshot_id>.json`` 后不因配置变更重写；解析只读已保存值，
  **不**重新叠加当前 process-env、默认值或当前 active 修订。
- **旧运行不伪造身份**：状态里没有 ``snapshot_id`` 即 ``legacy_unknown``，解析不创建文件、不推断
  当前配置身份；状态指向缺失/损坏快照时报 ``SNAPSHOT_UNAVAILABLE``，Provider 凭据缺失或被撤销
  时报 ``SNAPSHOT_DEPENDENCY_UNAVAILABLE``，都不回落到当前配置。
- **客户端所有权**：同 ``(snapshot_id, task_kind)`` 复用同一客户端，跨快照（含同修订的新快照）
  不重用；释放幂等，``close()`` 释放全部租约。配置服务不维护会跨修订替换的全局客户端缓存。
- **备份承载**：只导出已存在且摘要可核验的快照条目；缺失即闭锁失败，不静默遗漏。

``SnapshotReferenceIndex`` 与 ``legacy_run_index`` 把可信的非终态运行事实（由
``backend.app.services.workflow_checkpoint.snapshot_bindings_from_rows`` 从运行行/载荷投影）
接入 T210 的 ``runtime_references`` 与 ``legacy_runs`` 接纳入口；事实源失败/缺项仍视为未知。
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.app.ai.workflows.state import WORKFLOW_STATE_PAYLOAD_VERSION
from backend.app.schemas.settings import CredentialReference, SettingsRevision
from backend.app.services.settings.config_service import ConfigService

#: 运行状态指向的快照不存在或不可解析。
SNAPSHOT_UNAVAILABLE: Final[str] = "SNAPSHOT_UNAVAILABLE"
#: 快照引用的 Provider 凭据缺失、被撤销或凭据后端不可用。
SNAPSHOT_DEPENDENCY_UNAVAILABLE: Final[str] = "SNAPSHOT_DEPENDENCY_UNAVAILABLE"
#: 同一 snapshot_id 已存在不同内容，拒绝重写。
SNAPSHOT_IMMUTABLE: Final[str] = "SNAPSHOT_IMMUTABLE"
#: 没有可用的 active 修订或缺少绑定解析所需的 Registry。
SNAPSHOT_CONFIGURATION_UNAVAILABLE: Final[str] = "SNAPSHOT_CONFIGURATION_UNAVAILABLE"
#: 请求的任务类型不在已知绑定范围内。
SNAPSHOT_TASK_UNSUPPORTED: Final[str] = "SNAPSHOT_TASK_UNSUPPORTED"
#: 快照中没有该任务的绑定。
SNAPSHOT_BINDING_UNAVAILABLE: Final[str] = "SNAPSHOT_BINDING_UNAVAILABLE"
#: 只读配置实例不得写快照。
SNAPSHOT_READ_ONLY: Final[str] = "SNAPSHOT_READ_ONLY"
#: 备份条目缺失或无法核验。
SNAPSHOT_BACKUP_UNAVAILABLE: Final[str] = "SNAPSHOT_BACKUP_UNAVAILABLE"
#: 备份条目与文件摘要不一致。
SNAPSHOT_BACKUP_CONTENT_CHANGED: Final[str] = "SNAPSHOT_BACKUP_CONTENT_CHANGED"

#: 快照来源：新运行创建；旧运行缺快照时由解析返回，不写库。
NATIVE_ORIGIN: Final[str] = "native"
LEGACY_UNKNOWN_ORIGIN: Final[str] = "legacy_unknown"

#: 允许请求快照的任务类型；与 Registry 的已知任务集合保持一致。
KNOWN_TASK_KINDS: Final[frozenset[str]] = frozenset(
    {
        "default_llm",
        "question_generation",
        "subjective_grading",
        "diagnosis",
        "content_validation",
        "paper_extraction",
        "embedding",
        "vision",
        "ocr",
        "rerank",
        "reviewer",
    }
)
#: Embedding 向量维度保持 1024；本批不重建、不改列。
EMBEDDING_DIMENSION: Final[int] = 1024


class RuntimeSnapshotError(RuntimeError):
    """运行快照不可用；只暴露安全码与阶段，不携秘密正文。"""

    def __init__(self, code: str, *, stage: str = "snapshot") -> None:
        super().__init__(f"{code}：运行快照不可用（{stage}）。")
        self.code = code
        self.stage = stage


class SnapshotBinding(BaseModel):
    """单个任务在本次运行中解析出的绑定事实。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    task_kind: str = Field(min_length=1)
    mode: str = Field(min_length=1)
    profile_id: UUID | None = None
    profile_revision: UUID | None = None
    adapter_id: str | None = None
    model_id: str | None = None
    base_url: str | None = None
    auth_mode: str | None = None
    capabilities: tuple[str, ...] = ()
    retrieval_mode: str | None = None
    credential_ref: CredentialReference | None = None


class RuntimeSnapshot(BaseModel):
    """一次运行使用的不可变非敏感事实集合。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: int = Field(default=1, ge=1)
    snapshot_id: UUID
    environment_id: UUID
    workflow_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    settings_revision: UUID
    revision_status: str = Field(min_length=1)
    resolved_task_bindings: dict[str, SnapshotBinding]
    provider_identity: dict[str, dict[str, Any]]
    #: 参与本次运行的非秘密 Provider profile 快照；客户端只由此构造，不读最新 active 配置。
    provider_profiles: dict[str, dict[str, Any]] = Field(default_factory=dict)
    #: 文件存储根；仅非敏感路径事实，供图像/导入等边界复用。
    storage_root: str | None = None
    requested_model: dict[str, str]
    #: 返回模型在创建时未知；只允许结果生产者写入真实返回值。
    returned_model: dict[str, str] = Field(default_factory=dict)
    prompt_versions: dict[str, str]
    schema_versions: dict[str, str]
    retrieval_policy: dict[str, Any]
    confidence_policy: dict[str, float]
    embedding_identity: dict[str, Any]
    credential_refs: dict[str, CredentialReference]
    created_at: datetime
    legacy_origin: str = NATIVE_ORIGIN

    def credential_pairs(self) -> tuple[CredentialReference, ...]:
        """快照持有的 Provider 凭据引用（含版本），供引用索引比较。"""

        return tuple(self.credential_refs.values())


class SnapshotBackupEntry(BaseModel):
    """备份清单中的单个快照条目。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    snapshot_id: UUID
    relative_path: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)


class SnapshotClientLease:
    """按快照归属的客户端租约；释放幂等。"""

    def __init__(
        self,
        *,
        snapshot_id: UUID,
        task_kind: str,
        client: Any,
        on_release: Callable[[SnapshotClientLease], None],
    ) -> None:
        self.snapshot_id = snapshot_id
        self.task_kind = task_kind
        self.client = client
        self._on_release = on_release
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            close = getattr(self.client, "close", None)
            if callable(close):
                close()
        finally:
            self._on_release(self)

    async def arelease(self) -> None:
        """异步释放 Provider 客户端；``aclose`` 优先，其次同步 ``close``。"""

        if self._released:
            return
        self._released = True
        try:
            aclose = getattr(self.client, "aclose", None)
            if callable(aclose):
                await aclose()
            else:
                close = getattr(self.client, "close", None)
                if callable(close):
                    close()
        finally:
            self._on_release(self)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


@dataclass(frozen=True)
class SnapshotBackupBundle:
    """把一组快照当作可承载对象交给备份服务。"""

    service: RuntimeSnapshotService
    snapshot_ids: tuple[UUID, ...]

    def copy_entries(self, destination: Path) -> tuple[SnapshotBackupEntry, ...]:
        return self.service.copy_backup_entries(destination, self.snapshot_ids)


def prompt_versions() -> dict[str, str]:
    """记录本构建中真实的 Prompt 版本；没有版本常量时显式 ``unversioned``。"""

    from backend.app.ai.agents.question_agent import QUESTION_GENERATION_PROMPT_VERSION
    from backend.app.ai.vision.provider import VISION_PROMPT_VERSION
    from backend.app.services.grading.subjective_grader import (
        SUBJECTIVE_GRADING_PROMPT_VERSION,
    )

    return {
        "question_generation": QUESTION_GENERATION_PROMPT_VERSION,
        "subjective_grading": SUBJECTIVE_GRADING_PROMPT_VERSION,
        "vision": VISION_PROMPT_VERSION,
        "diagnosis": "unversioned",
        "teacher": "personal-teacher-v1",
        "content_validation": "unversioned",
        "paper_extraction": "unversioned",
        "rerank": "unversioned",
    }


def schema_versions() -> dict[str, str]:
    """快照自身可核验的 schema/载荷版本。"""

    return {
        "settings_revision": "1",
        "runtime_snapshot": "1",
        "workflow_state_payload": WORKFLOW_STATE_PAYLOAD_VERSION,
    }


class RuntimeSnapshotService:
    """创建、持久化与解析运行快照；不执行任何模型请求。"""

    def __init__(
        self,
        config: ConfigService,
        *,
        store: Any = None,
        clock: Callable[[], datetime] | None = None,
        client_factory: Callable[[str, Mapping[str, Any]], Any] | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._client_factory = client_factory
        self._leases: dict[tuple[UUID, str], SnapshotClientLease] = {}

    @property
    def active_lease_count(self) -> int:
        """未释放的客户端租约数量；只用于诊断与测试。"""

        return sum(1 for lease in self._leases.values() if not lease.released)

    @property
    def root(self) -> Path:
        return self._config.config / "snapshots"

    @property
    def environment_id(self) -> UUID:
        return self._config.environment_id

    def backup_dependencies(self, snapshot: RuntimeSnapshot):
        """Validate original metadata and credential versions without acquiring a client."""
        if snapshot.environment_id != self.environment_id:
            raise RuntimeSnapshotError(SNAPSHOT_DEPENDENCY_UNAVAILABLE, stage="environment")
        revision = self._config.get_revision(snapshot.settings_revision)
        self._require_dependencies(snapshot)
        return revision

    # -- 创建与持久化 -----------------------------------------------------

    def create(
        self,
        *,
        workflow_id: str,
        request_id: str,
        task_kinds: Sequence[str],
        optional_task_kinds: Sequence[str] = (),
        storage_root: Path | str | None = None,
    ) -> RuntimeSnapshot:
        """从当前 active 修订创建不可变快照；失败不落盘。

        ``task_kinds`` 是本次运行必需任务，任一无法解析即失败；``optional_task_kinds`` 只在
        已配置且可解析时收入，供同一次运行后续按需使用。
        """

        if not self._config.writable:
            raise RuntimeSnapshotError(SNAPSHOT_READ_ONLY, stage="create")
        tasks = tuple(str(item) for item in task_kinds)
        optional = tuple(str(item) for item in optional_task_kinds)
        if not tasks:
            raise RuntimeSnapshotError(SNAPSHOT_TASK_UNSUPPORTED, stage="create")
        unknown = sorted((set(tasks) | set(optional)) - KNOWN_TASK_KINDS)
        if unknown:
            raise RuntimeSnapshotError(SNAPSHOT_TASK_UNSUPPORTED, stage="create")
        with self._config.reference_transaction():
            revision = self._config.active_revision()
            if revision is None:
                raise RuntimeSnapshotError(SNAPSHOT_CONFIGURATION_UNAVAILABLE, stage="revision")
            snapshot = self._build(
                revision,
                workflow_id=str(workflow_id),
                request_id=str(request_id),
                tasks=tasks,
                optional_tasks=optional,
                storage_root=None if storage_root is None else str(storage_root),
            )
            self._persist(snapshot)
        return snapshot

    def open_run(
        self,
        *,
        workflow_id: str,
        request_id: str,
        task_kinds: Sequence[str],
        optional_task_kinds: Sequence[str] = (),
        storage_root: Path | str | None = None,
    ) -> RunSnapshotContext:
        """创建快照并返回运行上下文；调用方在首次 AI 调用前调用一次。"""

        snapshot = self.create(
            workflow_id=workflow_id,
            request_id=request_id,
            task_kinds=task_kinds,
            optional_task_kinds=optional_task_kinds,
            storage_root=storage_root,
        )
        return self.context(snapshot)

    def context(self, snapshot: RuntimeSnapshot) -> RunSnapshotContext:
        """把已持久化快照包装成运行上下文；不重新读取配置。"""

        return RunSnapshotContext(self, snapshot)

    def _build(
        self,
        revision: SettingsRevision,
        *,
        workflow_id: str,
        request_id: str,
        tasks: tuple[str, ...],
        optional_tasks: tuple[str, ...] = (),
        storage_root: str | None = None,
    ) -> RuntimeSnapshot:
        registry = self._config.provider_registry
        if registry is None:
            raise RuntimeSnapshotError(SNAPSHOT_CONFIGURATION_UNAVAILABLE, stage="registry")
        values = revision.values
        bindings: dict[str, SnapshotBinding] = {}
        requested: dict[str, str] = {}
        identity: dict[str, dict[str, Any]] = {}
        profiles: dict[str, dict[str, Any]] = {}
        refs: dict[str, CredentialReference] = {}
        required = frozenset(tasks)
        for task_kind in dict.fromkeys((*tasks, *optional_tasks)):
            try:
                resolved = registry.resolve_binding(
                    task_kind=task_kind, profiles=values.providers, bindings=values.bindings
                )
            except Exception as error:  # noqa: BLE001 - Registry 已收敛为安全码。
                if task_kind not in required:
                    # 可选消费者未配置时保持缺失；需要时再以绑定不可用安全阻断。
                    continue
                code = getattr(error, "code", None)
                raise RuntimeSnapshotError(
                    code if isinstance(code, str) and code else SNAPSHOT_CONFIGURATION_UNAVAILABLE,
                    stage="bindings",
                ) from None
            binding = SnapshotBinding(
                task_kind=task_kind,
                mode=resolved.mode,
                profile_id=resolved.profile_id,
                profile_revision=resolved.profile_revision,
                capabilities=tuple(resolved.required_capabilities),
            )
            profile = (
                values.providers.get(str(resolved.profile_id))
                if resolved.profile_id is not None
                else None
            )
            if profile is not None:
                retrieval_mode = getattr(
                    values.bindings.get(task_kind), "retrieval_mode", None
                )
                if retrieval_mode is None:
                    retrieval_mode = getattr(
                        values.bindings.get("default_llm"), "retrieval_mode", None
                    )
                binding = binding.model_copy(
                    update={
                        "adapter_id": profile.adapter_id,
                        "model_id": profile.model_id,
                        "base_url": profile.base_url,
                        "auth_mode": profile.auth_mode,
                        "retrieval_mode": retrieval_mode,
                        "credential_ref": profile.credential_ref,
                    }
                )
                requested[task_kind] = profile.model_id
                profiles[str(profile.profile_id)] = profile.model_dump(mode="json")
                identity[str(profile.profile_id)] = {
                    "profile_id": str(profile.profile_id),
                    "profile_revision": str(profile.profile_revision),
                    "adapter_id": profile.adapter_id,
                    "model_id": profile.model_id,
                    "base_url": profile.base_url,
                }
                if profile.credential_ref is not None:
                    refs[task_kind] = profile.credential_ref
            bindings[task_kind] = binding
        return RuntimeSnapshot(
            snapshot_id=uuid4(),
            environment_id=self._config.environment_id,
            workflow_id=workflow_id,
            request_id=request_id,
            settings_revision=revision.revision_id,
            revision_status=revision.status,
            resolved_task_bindings=bindings,
            provider_identity=identity,
            provider_profiles=profiles,
            storage_root=storage_root,
            requested_model=requested,
            prompt_versions=prompt_versions(),
            schema_versions=schema_versions(),
            retrieval_policy={
                "hybrid_weight": values.policies.hybrid_weight,
                "rerank_max_candidates": values.policies.rerank_max_candidates,
                "rerank_timeout_seconds": values.policies.rerank_timeout_seconds,
                "task_modes": {
                    name: getattr(binding, "retrieval_mode", None)
                    for name, binding in values.bindings.items()
                },
            },
            confidence_policy={"confidence_threshold": values.policies.confidence_threshold},
            embedding_identity=self._embedding_identity(values, registry),
            credential_refs=refs,
            created_at=self._clock(),
        )

    @staticmethod
    def _embedding_identity(values: Any, registry: Any) -> dict[str, Any]:
        task = values.bindings.get("embedding")
        if task is None:
            return {"bound": False, "dimension": EMBEDDING_DIMENSION}
        try:
            resolved = registry.resolve_binding(
                task_kind="embedding", profiles=values.providers, bindings=values.bindings
            )
        except Exception:  # noqa: BLE001 - 未绑定/非法绑定时保持未绑定事实。
            return {"bound": False, "dimension": EMBEDDING_DIMENSION}
        profile = (
            values.providers.get(str(resolved.profile_id)) if resolved.profile_id is not None else None
        )
        if profile is None:
            return {"bound": False, "dimension": EMBEDDING_DIMENSION}
        return {
            "bound": True,
            "profile_id": str(profile.profile_id),
            "profile_revision": str(profile.profile_revision),
            "adapter_id": profile.adapter_id,
            "model_id": profile.model_id,
            "base_url": profile.base_url,
            "dimension": EMBEDDING_DIMENSION,
        }

    def _persist(self, snapshot: RuntimeSnapshot) -> None:
        target = self.path_for(snapshot.snapshot_id)
        content = self._canonical(snapshot)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise RuntimeSnapshotError(SNAPSHOT_UNAVAILABLE, stage="write") from None
        if target.exists():
            try:
                existing = target.read_bytes()
            except OSError:
                raise RuntimeSnapshotError(SNAPSHOT_UNAVAILABLE, stage="write") from None
            if existing == content:
                return
            raise RuntimeSnapshotError(SNAPSHOT_IMMUTABLE, stage="write")
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except OSError:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise RuntimeSnapshotError(SNAPSHOT_UNAVAILABLE, stage="write") from None

    @staticmethod
    def _canonical(snapshot: RuntimeSnapshot) -> bytes:
        return json.dumps(
            snapshot.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def path_for(self, snapshot_id: UUID | str) -> Path:
        """快照文件路径；越出 ``config/snapshots`` 即拒绝。"""

        try:
            identifier = snapshot_id if isinstance(snapshot_id, UUID) else UUID(str(snapshot_id))
        except (ValueError, TypeError):
            raise RuntimeSnapshotError(SNAPSHOT_UNAVAILABLE, stage="path") from None
        root = self.root
        path = (root / f"{identifier}.json").resolve()
        if path.parent != root.resolve() or not path.is_relative_to(self._config.root):
            raise RuntimeSnapshotError(SNAPSHOT_UNAVAILABLE, stage="path")
        return path

    # -- 解析 -------------------------------------------------------------

    def load(self, snapshot_id: UUID | str) -> RuntimeSnapshot:
        path = self.path_for(snapshot_id)
        try:
            data = json.loads(path.read_bytes())
        except (OSError, ValueError):
            raise RuntimeSnapshotError(SNAPSHOT_UNAVAILABLE, stage="read") from None
        try:
            snapshot = RuntimeSnapshot.model_validate(data)
        except ValidationError:
            raise RuntimeSnapshotError(SNAPSHOT_UNAVAILABLE, stage="read") from None
        if str(snapshot.snapshot_id) != path.stem or snapshot.environment_id != self._config.environment_id:
            raise RuntimeSnapshotError(SNAPSHOT_UNAVAILABLE, stage="environment")
        return snapshot

    def resolve_for_state(self, state: Mapping[str, Any] | Any) -> dict[str, Any]:
        """解析运行状态的快照边界：native 快照或显式 legacy_unknown。"""

        value = state.get("snapshot_id") if isinstance(state, Mapping) else getattr(
            state, "snapshot_id", None
        )
        if value is None:
            return {"snapshot": None, "origin": LEGACY_UNKNOWN_ORIGIN}
        snapshot = self.load(value)
        self._require_dependencies(snapshot)
        return {"snapshot": snapshot, "origin": NATIVE_ORIGIN}

    def context_for_state(
        self, state: Mapping[str, Any] | Any
    ) -> tuple[RunSnapshotContext | None, str]:
        """解析状态并返回运行上下文；legacy 状态返回 ``None`` 而不伪造快照。"""

        resolved = self.resolve_for_state(state)
        snapshot = resolved["snapshot"]
        if snapshot is None:
            return None, str(resolved["origin"])
        return self.context(snapshot), str(resolved["origin"])

    def _require_dependencies(self, snapshot: RuntimeSnapshot) -> None:
        references = snapshot.credential_pairs()
        if not references:
            # 未绑定 Provider 凭据的运行不依赖自有凭据库（例如本地 auth_mode=none）。
            return
        store = self._store if self._store is not None else self._config.credential_store
        if store is None:
            raise RuntimeSnapshotError(SNAPSHOT_DEPENDENCY_UNAVAILABLE, stage="credentials")
        for reference in references:
            if reference.environment_id != self._config.environment_id:
                raise RuntimeSnapshotError(SNAPSHOT_DEPENDENCY_UNAVAILABLE, stage="credentials")
            try:
                status = store.status(reference, reference.version)
                state = getattr(status, "status", None)
            except Exception:  # noqa: BLE001 - 凭据后端失败不得携正文逃出此边界。
                state = None
            if state != "available":
                raise RuntimeSnapshotError(SNAPSHOT_DEPENDENCY_UNAVAILABLE, stage="credentials")

    # -- 客户端租约 -------------------------------------------------------

    def acquire(
        self,
        *,
        snapshot_id: UUID | str,
        task_kind: str,
        factory: Callable[[SnapshotBinding], Any],
    ) -> SnapshotClientLease:
        """同快照同任务复用客户端；跨快照（含同修订的新快照）不复用。"""

        snapshot = self.load(snapshot_id)
        binding = snapshot.resolved_task_bindings.get(task_kind)
        if binding is None:
            raise RuntimeSnapshotError(SNAPSHOT_BINDING_UNAVAILABLE, stage="client")
        key = (snapshot.snapshot_id, task_kind)
        existing = self._leases.get(key)
        if existing is not None and not existing.released:
            return existing
        lease = SnapshotClientLease(
            snapshot_id=snapshot.snapshot_id,
            task_kind=task_kind,
            client=factory(binding),
            on_release=self._drop_lease,
        )
        self._leases[key] = lease
        return lease

    def acquire_client(self, snapshot_id: UUID | str, task_kind: str) -> SnapshotClientLease:
        """按快照内保存的完整 Profile 构造客户端并登记租约。

        客户端只使用快照中的 adapter/model/base_url/参数/凭据引用，不调用 ``get_settings``，
        也不重新解析当前 active 修订。
        """

        snapshot = self.load(snapshot_id)

        def factory(_binding: SnapshotBinding) -> Any:
            return self._build_client(snapshot, task_kind)

        return self.acquire(
            snapshot_id=snapshot.snapshot_id,
            task_kind=task_kind,
            factory=factory,
        )

    def _build_client(self, snapshot: RuntimeSnapshot, task_kind: str) -> Any:
        binding = snapshot.resolved_task_bindings.get(task_kind)
        if binding is None:
            raise RuntimeSnapshotError(SNAPSHOT_BINDING_UNAVAILABLE, stage="client")
        profile = (
            snapshot.provider_profiles.get(str(binding.profile_id))
            if binding.profile_id is not None
            else None
        )
        if profile is None:
            raise RuntimeSnapshotError(SNAPSHOT_DEPENDENCY_UNAVAILABLE, stage="client")
        if self._client_factory is not None:
            try:
                return self._client_factory(task_kind, dict(profile))
            except RuntimeSnapshotError:
                raise
            except Exception:  # noqa: BLE001 - 客户端构造失败不得携配置正文逃出。
                raise RuntimeSnapshotError(SNAPSHOT_DEPENDENCY_UNAVAILABLE, stage="client") from None
        store = self._store if self._store is not None else self._config.credential_store
        if binding.credential_ref is not None and store is None:
            raise RuntimeSnapshotError(SNAPSHOT_DEPENDENCY_UNAVAILABLE, stage="credentials")
        from backend.app.services.settings.provider_registry import ProviderRegistry

        registry = self._config.provider_registry
        if registry is None:
            registry = ProviderRegistry(environment_id=self._config.environment_id)
        try:
            return registry.create(profile, credential_store=store)
        except Exception:  # noqa: BLE001 - 只暴露安全依赖码。
            raise RuntimeSnapshotError(SNAPSHOT_DEPENDENCY_UNAVAILABLE, stage="client") from None

    def _drop_lease(self, lease: SnapshotClientLease) -> None:
        self._leases.pop((lease.snapshot_id, lease.task_kind), None)

    def release_all(self, snapshot_id: UUID | str) -> int:
        """释放某快照的全部租约，返回释放数量（幂等）。"""

        identifier = UUID(str(snapshot_id))
        leases = [
            lease
            for (owner, _task), lease in tuple(self._leases.items())
            if owner == identifier and not lease.released
        ]
        self._release_leases(leases)
        return len(leases)

    @staticmethod
    def _release_leases(leases: Sequence[SnapshotClientLease]) -> None:
        first_error: BaseException | None = None
        for lease in leases:
            try:
                lease.release()
            except BaseException as error:  # noqa: BLE001 - finish all owned releases before re-raising the original error.
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error

    @staticmethod
    async def _arelease_leases(leases: Sequence[SnapshotClientLease]) -> None:
        first_error: BaseException | None = None
        for lease in leases:
            try:
                await lease.arelease()
            except BaseException as error:  # noqa: BLE001 - finish all owned releases before re-raising the original error.
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error

    def close(self) -> None:
        """释放全部同步租约；不触碰配置服务或凭据库。"""

        self._release_leases(tuple(self._leases.values()))
        self._leases.clear()

    async def arelease_all(self, snapshot_id: UUID | str) -> int:
        """异步释放某快照的全部租约，返回释放数量（幂等）。"""

        identifier = UUID(str(snapshot_id))
        leases = [
            lease
            for (owner, _task), lease in tuple(self._leases.items())
            if owner == identifier and not lease.released
        ]
        await self._arelease_leases(leases)
        return len(leases)

    async def aclose(self) -> None:
        """异步释放全部租约；Provider 的 ``aclose`` 优先。"""

        await self._arelease_leases(tuple(self._leases.values()))
        self._leases.clear()

    # -- 备份承载 ---------------------------------------------------------

    def backup_bundle(self, snapshot_ids: Sequence[UUID | str]) -> SnapshotBackupBundle:
        return SnapshotBackupBundle(
            service=self,
            snapshot_ids=tuple(UUID(str(item)) for item in snapshot_ids),
        )

    def copy_backup_entries(
        self, destination: Path, snapshot_ids: Sequence[UUID | str]
    ) -> tuple[SnapshotBackupEntry, ...]:
        """把已核验快照复制到备份集的 ``runtime_snapshots/`` 并返回清单条目。"""

        destination = Path(destination).resolve()
        root = (destination / "runtime_snapshots").resolve()
        if not root.is_relative_to(destination):
            raise RuntimeSnapshotError(SNAPSHOT_BACKUP_UNAVAILABLE, stage="backup")
        entries: list[SnapshotBackupEntry] = []
        for identifier in sorted({UUID(str(item)) for item in snapshot_ids}, key=str):
            source = self.path_for(identifier)
            snapshot = self.load(identifier)  # 必须存在且环境一致；否则闭锁
            content = self._canonical(snapshot)
            try:
                current = source.read_bytes()
            except OSError:
                raise RuntimeSnapshotError(SNAPSHOT_BACKUP_UNAVAILABLE, stage="backup") from None
            if current != content:
                raise RuntimeSnapshotError(SNAPSHOT_BACKUP_CONTENT_CHANGED, stage="backup")
            target = root / f"{identifier}.json"
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError:
                raise RuntimeSnapshotError(SNAPSHOT_BACKUP_UNAVAILABLE, stage="backup") from None
            if target.read_bytes() != content:
                raise RuntimeSnapshotError(SNAPSHOT_BACKUP_CONTENT_CHANGED, stage="backup")
            entries.append(
                SnapshotBackupEntry(
                    snapshot_id=identifier,
                    relative_path=f"runtime_snapshots/{identifier}.json",
                    size_bytes=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                )
            )
        return tuple(entries)

    @staticmethod
    def verify_backup_entries(
        destination: Path, entries: Sequence[SnapshotBackupEntry]
    ) -> None:
        """核对备份集中的快照条目；缺失或摘要不符均闭锁。"""

        root = (Path(destination) / "runtime_snapshots").resolve()
        for entry in entries:
            path = (root / Path(entry.relative_path).name).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise RuntimeSnapshotError(SNAPSHOT_BACKUP_UNAVAILABLE, stage="backup")
            content = path.read_bytes()
            if (
                len(content) != entry.size_bytes
                or hashlib.sha256(content).hexdigest() != entry.sha256
            ):
                raise RuntimeSnapshotError(SNAPSHOT_BACKUP_CONTENT_CHANGED, stage="backup")


@dataclass(frozen=True, slots=True)
class SnapshotInvocation:
    """一次实际 AI 调用的来源记录；未知值保持 ``None``，不回填当前配置。"""

    task_kind: str
    snapshot_id: UUID
    settings_revision: UUID
    requested_model: str | None
    returned_model: str | None
    prompt_version: str | None
    prompt_source: str


def _optional_text(value: object) -> str | None:
    """只接受非空白文本；其它值（含 ``unknown`` 哨兵）保持未知。"""

    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


class RunSnapshotContext:
    """一次运行不可变快照的消费视图；不读取最新 active 配置。"""

    def __init__(self, service: RuntimeSnapshotService, snapshot: RuntimeSnapshot) -> None:
        self._service = service
        self._snapshot = snapshot
        self._invocations: list[SnapshotInvocation] = []

    @property
    def service(self) -> RuntimeSnapshotService:
        return self._service

    @property
    def snapshot(self) -> RuntimeSnapshot:
        return self._snapshot

    @property
    def snapshot_id(self) -> UUID:
        return self._snapshot.snapshot_id

    @property
    def settings_revision(self) -> UUID:
        return self._snapshot.settings_revision

    @property
    def storage_root(self) -> Path | None:
        value = self._snapshot.storage_root
        return None if value is None else Path(value)

    def binding(self, task_kind: str) -> SnapshotBinding:
        binding = self._snapshot.resolved_task_bindings.get(task_kind)
        if binding is None:
            raise RuntimeSnapshotError(SNAPSHOT_BINDING_UNAVAILABLE, stage="binding")
        return binding

    def requested_model(self, task_kind: str) -> str | None:
        return _optional_text(self._snapshot.requested_model.get(task_kind))

    def prompt_version(self, task_kind: str) -> str | None:
        return _optional_text(self._snapshot.prompt_versions.get(task_kind))

    def prompt_source(self, task_kind: str) -> str:
        version = self.prompt_version(task_kind)
        if version is None:
            return "unknown"
        if version == "unversioned":
            return "unversioned"
        return "runtime_snapshot"

    def retrieval_mode(self, task_kind: str) -> str | None:
        binding = self._snapshot.resolved_task_bindings.get(task_kind)
        if binding is None or binding.retrieval_mode is None:
            return None
        # TaskBinding 的持久词汇是 keyword/semantic；运行期检索枚举使用 *_only。
        return {
            "keyword": "keyword_only",
            "semantic": "vector_only",
        }.get(binding.retrieval_mode, binding.retrieval_mode)

    def _policy_number(self, name: str, *, default: float | None = None) -> float:
        section = (
            self._snapshot.retrieval_policy
            if name.startswith("rerank_") or name == "hybrid_weight"
            else self._snapshot.confidence_policy
        )
        value = section.get(name)
        if value is None:
            if default is not None:
                return default
            raise RuntimeSnapshotError(SNAPSHOT_DEPENDENCY_UNAVAILABLE, stage="policy")
        try:
            return float(value)
        except (TypeError, ValueError):
            raise RuntimeSnapshotError(SNAPSHOT_DEPENDENCY_UNAVAILABLE, stage="policy") from None

    @property
    def confidence_threshold(self) -> float:
        return self._policy_number("confidence_threshold")

    @property
    def hybrid_vector_weight(self) -> float:
        return self._policy_number("hybrid_weight")

    @property
    def rerank_max_candidates(self) -> int:
        return int(self._policy_number("rerank_max_candidates"))

    @property
    def rerank_timeout_seconds(self) -> float:
        return self._policy_number("rerank_timeout_seconds")

    def acquire(self, task_kind: str) -> SnapshotClientLease:
        """取得该运行该任务的客户端租约；同快照同任务复用。"""

        return self._service.acquire_client(self.snapshot_id, task_kind)

    def record_invocation(
        self,
        task_kind: str,
        *,
        returned_model: object = None,
        prompt_version: str | None = None,
    ) -> SnapshotInvocation:
        """记录实际请求/可确认返回模型与 Prompt 来源；未知不伪造。"""

        explicit_prompt = _optional_text(prompt_version)
        resolved_prompt = explicit_prompt or self.prompt_version(task_kind)
        source = (
            "consumer"
            if explicit_prompt is not None
            else self.prompt_source(task_kind)
        )
        invocation = SnapshotInvocation(
            task_kind=task_kind,
            snapshot_id=self.snapshot_id,
            settings_revision=self.settings_revision,
            requested_model=self.requested_model(task_kind),
            returned_model=_optional_text(returned_model),
            prompt_version=resolved_prompt,
            prompt_source=source,
        )
        self._invocations.append(invocation)
        return invocation

    def last_invocation(self, task_kind: str) -> SnapshotInvocation | None:
        for item in reversed(self._invocations):
            if item.task_kind == task_kind:
                return item
        return None

    @property
    def invocations(self) -> tuple[SnapshotInvocation, ...]:
        return tuple(self._invocations)

    def release_all(self) -> int:
        return self._service.release_all(self.snapshot_id)

    async def aclose(self) -> None:
        await self._service.arelease_all(self.snapshot_id)

    def __repr__(self) -> str:  # pragma: no cover - 仅诊断
        return f"RunSnapshotContext(snapshot_id={self.snapshot_id!s})"


class SnapshotReferenceIndex:
    """凭据引用事实：非终态运行的快照仍持有该引用即为 ``snapshot``。"""

    def __init__(
        self,
        snapshots: RuntimeSnapshotService,
        bindings: Callable[[], Sequence[Any]],
    ) -> None:
        self._snapshots = snapshots
        self._bindings = bindings

    def __call__(self, reference: CredentialReference, version: UUID) -> tuple[str, ...]:
        facts: list[str] = []
        for item in self._bindings():
            snapshot_id = getattr(item, "snapshot_id", None)
            if not snapshot_id:
                # 无快照的遗留非终态运行无法核实实际引用；未知不得当作无引用。
                if "legacy_run" not in facts:
                    facts.append("legacy_run")
                continue
            snapshot = self._snapshots.load(snapshot_id)
            matched = any(
                held == reference and held.version == version
                for held in snapshot.credential_pairs()
            )
            if matched and "snapshot" not in facts:
                facts.append("snapshot")
        return tuple(facts)


def legacy_run_index(bindings: Callable[[], Sequence[Any]]) -> Callable[[], tuple[dict[str, Any], ...]]:
    """把运行绑定投影成 T212 接纳门禁所需的遗留运行事实（无快照的非终态运行）。"""

    def source() -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "workflow_id": str(getattr(item, "workflow_id", "")),
                "status": str(getattr(item, "status", "")),
                "snapshot_id": getattr(item, "snapshot_id", None),
            }
            for item in bindings()
            if not getattr(item, "snapshot_id", None)
        )

    return source


__all__ = [
    "EMBEDDING_DIMENSION",
    "KNOWN_TASK_KINDS",
    "LEGACY_UNKNOWN_ORIGIN",
    "NATIVE_ORIGIN",
    "SNAPSHOT_BACKUP_CONTENT_CHANGED",
    "SNAPSHOT_BACKUP_UNAVAILABLE",
    "SNAPSHOT_BINDING_UNAVAILABLE",
    "SNAPSHOT_CONFIGURATION_UNAVAILABLE",
    "SNAPSHOT_DEPENDENCY_UNAVAILABLE",
    "SNAPSHOT_IMMUTABLE",
    "SNAPSHOT_READ_ONLY",
    "SNAPSHOT_TASK_UNSUPPORTED",
    "SNAPSHOT_UNAVAILABLE",
    "RunSnapshotContext",
    "RuntimeSnapshot",
    "RuntimeSnapshotError",
    "RuntimeSnapshotService",
    "SnapshotBackupBundle",
    "SnapshotBackupEntry",
    "SnapshotBinding",
    "SnapshotClientLease",
    "SnapshotInvocation",
    "SnapshotReferenceIndex",
    "legacy_run_index",
    "prompt_versions",
    "schema_versions",
]
