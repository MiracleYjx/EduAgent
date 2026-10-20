"""T220 settings management runtime: the single seam for the settings API.

The runtime owns the *non-business* services that used to be disconnected:

- :class:`ConfigService` (T210) for drafts/revisions/activation;
- the credential backend (T205/T206), opened lazily so a Bootstrap process does
  not touch a vault it does not need;
- :class:`ProviderRegistry` (T208) and :class:`ProbeService` (T209);
- :class:`RuntimeSnapshotService` (T212) for snapshot/legacy reference facts;
- onboarding progress, operation records and initialization previews;
- the projection from a saved revision to the legacy ``AppSettings`` shape that
  the business factory still consumes.

Nothing here writes business data, calls a model, or reads a repository ``.env``.
Failures are reported as stable codes; secret values never appear in a view.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import quote
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import SecretStr, ValidationError

from backend.app.schemas.settings import (
    ActivationView,
    AiReadiness,
    BusinessSettings,
    ConfigurationValues,
    CredentialBindingRequest,
    CredentialReference,
    CredentialState,
    CredentialStatusView,
    InitializationPreview,
    OnboardingView,
    OperationView,
    ProviderProfile,
    SettingsView,
    ValidationReport,
)
from backend.app.services.settings import readiness
from backend.app.services.settings.ai_readiness import evaluate_ai_readiness
from backend.app.services.settings.config_service import (
    ConfigService,
    ConfigServiceError,
)
from backend.app.services.settings.database_initialization import (
    InitializationError,
    require_test_identity,
    run_alembic_upgrade,
    verify_database_url,
)
from backend.app.services.settings.index_identity import AdoptionPreviewStore
from backend.app.services.settings.mounted_secrets import (
    MountedSecretError,
)
from backend.app.services.settings.provider_registry import (
    ProviderRegistry,
    RegistryValidationError,
)
from backend.app.services.settings.runtime_snapshot import (
    RuntimeSnapshotService,
    SnapshotReferenceIndex,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from backend.app.core.config import AppSettings
    from backend.app.core.settings_security import ManagementSession
    from backend.app.services.settings.index_identity import IndexIdentityService

SECRET_BACKEND_ENV = "EDUAGENT_SECRET_BACKEND"
SECRET_MOUNTS_ENV = "EDUAGENT_SECRET_MOUNTS"
SECRET_BACKEND_DPAPI = "dpapi"
SECRET_BACKEND_MOUNTED = "mounted_secret"
_MOUNT_SECRET_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_MOUNT_PURPOSES = frozenset({"provider", "database", "redis", "jwt"})
ONBOARDING_FILE = "onboarding.json"
OPERATIONS_DIR = "operations"
PREVIEW_TTL = timedelta(minutes=10)
_NON_TERMINAL_WORKFLOW_STATES = ("Queued", "Running", "Paused")
_MAX_OPERATIONS = 128

#: 稳定错误码 → HTTP 状态；未列出的按输入错误 422。
_STATUS_BY_CODE: dict[str, int] = {
    "SETTINGS_STORAGE_UNAVAILABLE": 503,
    "CREDENTIAL_BACKEND_UNAVAILABLE": 503,
    "CREDENTIAL_RECOVERY_REQUIRED": 503,
    "INITIALIZATION_RUNNER_UNAVAILABLE": 503,
    "DEPENDENCY_UNAVAILABLE": 503,
    "REFERENCE_INDEX_UNAVAILABLE": 409,
    "LEGACY_RUN_INDEX_UNAVAILABLE": 409,
    "DRAFT_NOT_FOUND": 404,
    "PROFILE_NOT_FOUND": 404,
    "CREDENTIAL_NOT_FOUND": 404,
    "OPERATION_NOT_FOUND": 404,
    "PREVIEW_NOT_FOUND": 404,
    "INDEX_IDENTITY_NOT_FOUND": 404,
    "REVISION_CONFLICT": 409,
    "CONFIG_WRITER_LOCKED": 409,
    "IN_USE": 409,
    "PROBE_REQUIRED": 409,
    "PROBE_EXPIRED": 409,
    "ACTIVATION_GATE_REQUIRED": 409,
    "LEGACY_RUN_ACTIVATION_BLOCKED": 409,
    "PREVIEW_EXPIRED": 409,
    "IDENTITY_CONFLICT": 409,
    "INDEX_BUSY": 409,
    "INITIALIZATION_BUSY": 409,
    "PROBE_LIMIT_REACHED": 429,
    "EXTERNALLY_MANAGED": 405,
    "OPERATOR_REQUIRED": 403,
}


def status_for(code: str, default: int = 422) -> int:
    """把内部安全码映射到 HTTP 状态；未知码不退化成成功。"""

    return _STATUS_BY_CODE.get(code, default)


class SettingsManagementError(RuntimeError):
    """设置管理边界错误；只携带稳定安全码，不携带输入或秘密。"""

    def __init__(self, code: str, *, stage: str = "settings", status_code: int | None = None):
        super().__init__(code)
        self.code = code
        self.stage = stage
        self.status_code = status_code if status_code is not None else status_for(code)


def mounted_mounts_from_env(
    environment_id: UUID, environ: Mapping[str, str] | None = None
) -> tuple[dict[str, tuple[CredentialReference, Path]], dict[UUID, CredentialReference]]:
    """把服务端挂载配置转为只读引用目录；HTTP 请求不能新增挂载。"""

    source = os.environ if environ is None else environ
    raw = (source.get(SECRET_MOUNTS_ENV) or "").strip()
    if not raw:
        return {}, {}
    try:
        data = json.loads(raw)
    except ValueError:
        raise SettingsManagementError("INVALID_MOUNTED_MAPPING", stage="mounts") from None
    if not isinstance(data, dict) or len(data) > 32:
        raise SettingsManagementError("INVALID_MOUNTED_MAPPING", stage="mounts")
    mounts: dict[str, tuple[CredentialReference, Path]] = {}
    catalog: dict[UUID, CredentialReference] = {}
    for secret_id, item in data.items():
        if (
            not isinstance(secret_id, str)
            or _MOUNT_SECRET_ID.fullmatch(secret_id) is None
            or not isinstance(item, dict)
            or set(item) != {"path", "purpose"}
            or item["purpose"] not in _MOUNT_PURPOSES
            or not isinstance(item["path"], str)
        ):
            raise SettingsManagementError("INVALID_MOUNTED_MAPPING", stage="mounts")
        path = Path(item["path"])
        if not path.is_absolute() or path != path.resolve():
            raise SettingsManagementError("INVALID_MOUNTED_MAPPING", stage="mounts")
        credential_id = uuid5(
            NAMESPACE_URL, f"eduagent:mounted:{environment_id}:{secret_id}"
        )
        version = uuid5(
            NAMESPACE_URL, f"eduagent:mounted-version:{environment_id}:{secret_id}"
        )
        reference = CredentialReference(
            credential_id=credential_id,
            version=version,
            environment_id=environment_id,
            backend="mounted_secret",
            purpose=item["purpose"],
            external_secret_id=secret_id,
        )
        mounts[secret_id] = (reference, path)
        catalog[credential_id] = reference
    return mounts, catalog


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    """同目录临时文件 + fsync + 原子替换；不含秘密。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    content = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    with temporary.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# 向导进度
# ---------------------------------------------------------------------------


class OnboardingStore:
    """只保存流程事实；不因步骤前进而宣称依赖通过。"""

    def __init__(self, root: Path, *, clock: Callable[[], datetime] | None = None) -> None:
        self._root = root.resolve()
        self._path = self._root / "config" / ONBOARDING_FILE
        self._clock = clock or _utcnow
        self._mutex = threading.RLock()

    def _empty(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "revision": 0,
            "current_step": 0,
            "draft_revision": None,
            "updated_at": None,
            "completed_steps": [],
        }

    def _guard(self, path: Path) -> Path:
        if path.resolve() != path or not path.is_relative_to(self._root):
            raise SettingsManagementError("CONFIG_PATH_ESCAPE", stage="onboarding")
        return path

    def _read(self) -> dict[str, Any]:
        path = self._guard(self._path)
        if not path.exists():
            return self._empty()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            raise SettingsManagementError("CORRUPT_ONBOARDING", stage="onboarding") from None
        if (
            not isinstance(data, dict)
            or type(data.get("schema_version")) is not int
            or data["schema_version"] != 1
            or type(data.get("revision")) is not int
            or data["revision"] < 0
            or type(data.get("current_step")) is not int
            or not 0 <= data["current_step"] <= 32
            or not isinstance(data.get("completed_steps", []), list)
        ):
            raise SettingsManagementError("CORRUPT_ONBOARDING", stage="onboarding")
        return data

    def read(self) -> OnboardingView:
        with self._mutex:
            data = self._read()
        return self._view(data)

    @staticmethod
    def _view(data: Mapping[str, Any]) -> OnboardingView:
        draft = data.get("draft_revision")
        updated = data.get("updated_at")
        try:
            updated_at = datetime.fromisoformat(str(updated)) if updated else None
        except ValueError:
            updated_at = None
        return OnboardingView(
            revision=int(data.get("revision", 0)),
            current_step=int(data.get("current_step", 0)),
            draft_revision=UUID(str(draft)) if draft else None,
            updated_at=updated_at,
            completed_steps=tuple(int(step) for step in data.get("completed_steps", []) if type(step) is int),
        )

    def update(
        self, *, expected_revision: int | None, current_step: int, draft_revision: UUID | None
    ) -> OnboardingView:
        with self._mutex:
            data = self._read()
            current = int(data.get("revision", 0))
            if expected_revision is not None and expected_revision != current:
                raise SettingsManagementError("REVISION_CONFLICT", stage="onboarding")
            revision = current + 1
            payload = {
                "schema_version": 1,
                "revision": revision,
                "current_step": current_step,
                "draft_revision": str(draft_revision) if draft_revision else None,
                "updated_at": self._clock().isoformat(),
                "completed_steps": list(range(current_step)),
            }
            try:
                _atomic_json(self._guard(self._path), payload)
            except OSError:
                raise SettingsManagementError("CONFIG_IO_ERROR", stage="onboarding") from None
        return self._view(payload)


# ---------------------------------------------------------------------------
# 操作记录（初始化/重启）
# ---------------------------------------------------------------------------


class OperationRegistry:
    """持久化操作步骤；其他进程实例遗留的 running 记录读回为 interrupted。"""

    def __init__(self, root: Path, *, clock: Callable[[], datetime] | None = None) -> None:
        self._root = root.resolve()
        self._directory = self._root / "config" / OPERATIONS_DIR
        self._clock = clock or _utcnow
        self._instance = uuid4()
        self._mutex = threading.RLock()

    def _path(self, operation_id: UUID) -> Path:
        path = self._directory / f"{operation_id}.json"
        if not path.is_relative_to(self._root):
            raise SettingsManagementError("CONFIG_PATH_ESCAPE", stage="operation")
        return path

    def _prune(self) -> None:
        try:
            files = sorted(self._directory.glob("*.json"), key=lambda item: item.stat().st_mtime)
        except OSError:
            return
        for extra in files[:-_MAX_OPERATIONS]:
            try:
                extra.unlink()
            except OSError:
                pass

    def create(
        self, kind: str, steps: Sequence[str], *,
        draft_revision: UUID | None = None, confirmation_id: UUID | None = None,
    ) -> OperationView:
        operation_kind: Literal["initialization", "restart"] = (
            "initialization" if kind == "initialization" else "restart"
        )
        operation_status: Literal["queued"] = "queued"
        operation = OperationView(
            operation_id=uuid4(),
            kind=operation_kind,
            status=operation_status,
            steps=tuple(steps),
            started_at=self._clock(),
        )
        with self._mutex:
            self._write(operation, status="queued", association={
                "draft_revision": str(draft_revision) if draft_revision else None,
                "confirmation_id": str(confirmation_id) if confirmation_id else None,
            })
            self._prune()
        return operation

    def _write(
        self,
        operation: OperationView,
        *,
        status: Literal["queued", "running", "succeeded", "failed", "interrupted"] | None = None,
        association: Mapping[str, Any] | None = None,
    ) -> None:
        if association is None:
            try:
                association = json.loads(self._path(operation.operation_id).read_text(encoding="utf-8"))
            except FileNotFoundError:
                association = {}
            except (OSError, ValueError, UnicodeError):
                raise SettingsManagementError("CORRUPT_OPERATION", stage="operation") from None
        if not isinstance(association, Mapping):
            raise SettingsManagementError("CORRUPT_OPERATION", stage="operation")
        payload = {
            "schema_version": 1,
            "instance": str(self._instance),
            "operation_id": str(operation.operation_id),
            "kind": operation.kind,
            "status": status or operation.status,
            "steps": list(operation.steps),
            "error_code": operation.error_code,
            "started_at": operation.started_at.isoformat(),
            "finished_at": operation.finished_at.isoformat() if operation.finished_at else None,
            "draft_revision": association.get("draft_revision"),
            "confirmation_id": association.get("confirmation_id"),
        }
        try:
            _atomic_json(self._path(operation.operation_id), payload)
        except OSError:
            raise SettingsManagementError("CONFIG_IO_ERROR", stage="operation") from None

    def mark(
        self,
        operation_id: UUID,
        status: Literal["queued", "running", "succeeded", "failed", "interrupted"],
        *,
        error_code: str | None = None,
    ) -> OperationView:
        with self._mutex:
            current = self.get(operation_id)
            finished = self._clock() if status in {"succeeded", "failed", "interrupted"} else None
            updated = current.model_copy(
                update={"status": status, "error_code": error_code, "finished_at": finished}
            )
            self._write(updated, status=status)
        return updated

    def latest_initialization(
        self, draft_revision: UUID | None, *, confirmation_id: UUID | None = None,
    ) -> OperationView | None:
        """只读已有操作关联；不新建、恢复执行或重写 interrupted 记录。"""
        if draft_revision is None:
            return None
        found: list[OperationView] = []
        with self._mutex:
            try:
                paths = tuple(self._directory.glob("*.json"))
                for path in paths:
                    if path.resolve() != path:
                        raise SettingsManagementError("CONFIG_PATH_ESCAPE", stage="operation")
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        raise TypeError
                    if data.get("kind") != "initialization" or data.get("draft_revision") != str(draft_revision):
                        continue
                    if confirmation_id is not None and data.get("confirmation_id") != str(confirmation_id):
                        continue
                    found.append(self.get(UUID(path.stem)))
            except (OSError, TypeError, ValueError, UnicodeError):
                raise SettingsManagementError("CORRUPT_OPERATION", stage="operation") from None
        return max(found, key=lambda item: item.started_at) if found else None

    def get(self, operation_id: UUID) -> OperationView:
        path = self._path(operation_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise SettingsManagementError("OPERATION_NOT_FOUND", status_code=404) from None
        except (OSError, ValueError, UnicodeError):
            raise SettingsManagementError("CORRUPT_OPERATION", stage="operation") from None
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise SettingsManagementError("CORRUPT_OPERATION", stage="operation")
        stored_status = str(data.get("status", "failed"))
        if stored_status not in {"queued", "running", "succeeded", "failed", "interrupted"}:
            raise SettingsManagementError("CORRUPT_OPERATION", stage="operation")
        status = cast(
            "Literal['queued', 'running', 'succeeded', 'failed', 'interrupted']",
            stored_status,
        )
        if status in {"queued", "running"} and data.get("instance") != str(self._instance):
            # 进程重启/中断后不谎报仍在执行；实际 DB 版本由重新检查确认。
            status = "interrupted"
        stored_kind = str(data.get("kind", "initialization"))
        if stored_kind not in {"initialization", "restart"}:
            raise SettingsManagementError("CORRUPT_OPERATION", stage="operation")
        kind = cast("Literal['initialization', 'restart']", stored_kind)
        try:
            operation = OperationView(
                operation_id=UUID(str(data["operation_id"])),
                kind=kind,
                status=status,
                steps=tuple(str(step) for step in data.get("steps", [])),
                error_code=data.get("error_code"),
                started_at=datetime.fromisoformat(str(data["started_at"])),
                finished_at=(
                    datetime.fromisoformat(str(data["finished_at"])) if data.get("finished_at") else None
                ),
            )
        except (KeyError, TypeError, ValueError):
            raise SettingsManagementError("CORRUPT_OPERATION", stage="operation") from None
        return operation


# ---------------------------------------------------------------------------
# 初始化预览/应用
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _InitializationPlan:
    preview_id: UUID
    draft_revision: UUID
    environment: str
    target_identity: str
    current_head: str | None
    target_head: str | None
    database_url: str
    locator: Any
    expires_at: datetime
    allowed: bool


class InitializationService:
    """非破坏性迁移预览与应用；默认没有部署级执行器时明确拒绝。"""

    def __init__(
        self,
        *,
        config: ConfigService,
        environment: str,
        database_url: Callable[[ConfigurationValues], str],
        clock: Callable[[], datetime] | None = None,
        migrator: Callable[[str], None] | None = None,
        read_head: Callable[[str], str | None] | None = None,
        operations: OperationRegistry | None = None,
        verify_database: Callable[[str, Mapping[str, Any]], None] | None = None,
    ) -> None:
        self._config = config
        self._environment = environment
        self._database_url = database_url
        self._clock = clock or _utcnow
        self._migrator = migrator
        self._read_head = read_head or read_current_alembic_head
        self._operations = operations or OperationRegistry(config.root)
        # 默认目标核验始终启用；测试只能显式注入受控替身。
        self._verify_database = verify_database or (
            lambda url, expected: verify_database_url(url, expected=expected)
        )
        self._plans: dict[UUID, _InitializationPlan] = {}
        self._lock = threading.RLock()
        self._active_operation: UUID | None = None

    def _plan(self, preview_id: UUID) -> _InitializationPlan:
        with self._lock:
            plan = self._plans.get(preview_id)
        if plan is None:
            raise SettingsManagementError("PREVIEW_NOT_FOUND", status_code=404)
        if self._clock() > plan.expires_at:
            with self._lock:
                self._plans.pop(preview_id, None)
            raise SettingsManagementError("PREVIEW_EXPIRED")
        return plan

    def preview(self, draft_revision: UUID) -> InitializationPreview:
        draft = self._config.get_draft(draft_revision)
        locator = draft.values.business.database_locator
        if locator is None:
            raise SettingsManagementError("BUSINESS_CONFIGURATION_INCOMPLETE")
        try:
            url = self._database_url(draft.values)
        except SettingsManagementError:
            url = ""
        target_head = readiness.discover_migration_head()
        current_head = self._read_head(url) if url else None
        target_identity = (
            f"{self._environment}:{locator.host}:{locator.port}/{locator.database}#{locator.schema_name}"
        )
        allowed = bool(url) and target_head is not None
        steps = (
            ("inspect_current_head", "upgrade_to_target_head")
            if current_head != target_head
            else ("inspect_current_head", "already_current")
        )
        if not allowed:
            steps = ("inspect_current_head", "dependency_unavailable")
        expires_at = self._clock() + PREVIEW_TTL
        preview_id = uuid4()
        with self._lock:
            self._plans[preview_id] = _InitializationPlan(
                preview_id=preview_id,
                draft_revision=draft_revision,
                environment=self._environment,
                target_identity=target_identity,
                current_head=current_head,
                target_head=target_head,
                database_url=url,
                locator=locator,
                expires_at=expires_at,
                allowed=allowed,
            )
        return InitializationPreview(
            preview_id=preview_id,
            draft_revision=draft_revision,
            environment=self._environment,
            target_identity=target_identity,
            current_head=current_head,
            target_head=target_head,
            allowed=allowed,
            destructive=False,
            steps=steps,
            expires_at=expires_at,
        )

    def apply(self, confirmation_id: UUID, expected_revision: UUID) -> OperationView:
        with self._lock:
            previous = self._operations.latest_initialization(
                expected_revision, confirmation_id=confirmation_id,
            )
            if previous is not None:
                return previous
            plan = self._plan(confirmation_id)
            if plan.draft_revision != expected_revision:
                raise SettingsManagementError("REVISION_CONFLICT")
            if not plan.allowed or not plan.database_url or plan.target_head is None:
                raise SettingsManagementError("DEPENDENCY_UNAVAILABLE", stage="initialization")
            if readiness.discover_migration_head() != plan.target_head:
                raise SettingsManagementError("REVISION_CONFLICT", stage="target_head")
            draft = self._config.get_draft(expected_revision)
            if self._database_url(draft.values) != plan.database_url:
                raise SettingsManagementError("REVISION_CONFLICT", stage="target_identity")
            if self._read_head(plan.database_url) != plan.current_head:
                raise SettingsManagementError("REVISION_CONFLICT", stage="current_head")
            if self._active_operation is not None:
                raise SettingsManagementError("INITIALIZATION_BUSY", stage="initialization")
            if self._migrator is None:
                raise SettingsManagementError(
                    "INITIALIZATION_RUNNER_UNAVAILABLE", stage="initialization", status_code=503,
                )
            operation = self._operations.create(
                "initialization", ("inspect_current_head", f"upgrade_to:{plan.target_head}"),
                draft_revision=expected_revision, confirmation_id=confirmation_id,
            )
            self._operations.mark(operation.operation_id, "running")
            self._active_operation = operation.operation_id
            worker = threading.Thread(
                target=self._run, args=(operation.operation_id, plan),
                name="eduagent-settings-initialization", daemon=True,
            )
            worker.start()
            return self._operations.get(operation.operation_id)

    def _run(self, operation_id: UUID, plan: _InitializationPlan) -> None:
        try:
            try:
                if readiness.discover_migration_head() != plan.target_head:
                    self._operations.mark(operation_id, "failed", error_code="REVISION_CONFLICT")
                    return
                if self._verify_database is not None:
                    self._verify_database(
                        plan.database_url,
                        {"database": plan.locator.database, "user": plan.locator.user,
                         "schema": plan.locator.schema_name},
                    )
                assert self._migrator is not None
                self._migrator(plan.database_url)
                reached = self._read_head(plan.database_url)
                if reached != plan.target_head:
                    self._operations.mark(operation_id, "failed", error_code="MIGRATION_HEAD_MISMATCH")
                    return
            except InitializationError as error:
                self._operations.mark(operation_id, "failed", error_code=error.code)
                return
            except Exception:  # noqa: BLE001 - 迁移正文不得逃出管理边界
                self._operations.mark(operation_id, "failed", error_code="MIGRATION_FAILED")
                return
            self._operations.mark(operation_id, "succeeded")
        finally:
            with self._lock:
                if self._active_operation == operation_id:
                    self._active_operation = None


def read_current_alembic_head(database_url: str) -> str | None:
    """只读当前 ``alembic_version``；连接失败返回 None（不猜版本）。"""

    if not database_url:
        return None
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import SQLAlchemyError

    engine = None
    try:
        engine = create_engine(database_url, connect_args={"connect_timeout": 2})
        with engine.connect() as connection:
            schema = connection.scalar(text("SELECT current_schema()"))
            if not schema:
                return None
            qualified = connection.dialect.identifier_preparer.quote_schema(str(schema))
            row = connection.execute(text(f"SELECT version_num FROM {qualified}.alembic_version")).first()
    except (SQLAlchemyError, ValueError):
        return None
    finally:
        if engine is not None:
            engine.dispose()
    return str(row[0]) if row is not None else None


# ---------------------------------------------------------------------------
# 业务运行事实（快照/遗留运行）
# ---------------------------------------------------------------------------


class BusinessRunFacts:
    """只读非终态工作流行；事实不可得时抛安全码而不是“无遗留运行”。"""

    def __init__(
        self,
        *,
        settings: Callable[[], AppSettings | None],
        configured: Callable[[], bool],
        connect_timeout: int = 2,
    ) -> None:
        self._settings = settings
        self._configured = configured
        self._connect_timeout = connect_timeout

    def bindings(self) -> tuple[Any, ...]:
        settings = self._settings()
        if settings is None:
            if not self._configured():
                # 尚未存在已保存业务配置：没有可核实的运行事实，也没有可遗留的运行。
                return ()
            raise SettingsManagementError("LEGACY_RUN_INDEX_UNAVAILABLE", stage="runs")
        from sqlalchemy import select
        from sqlalchemy.exc import SQLAlchemyError

        from backend.app.core.database import create_database_engine
        from backend.app.domain.enums import WorkflowStatus
        from backend.app.models.workflow_run import WorkflowRun
        from backend.app.services.workflow_checkpoint import (
            run_kind_criteria,
            snapshot_bindings_from_rows,
        )

        engine = create_database_engine(settings, connect_timeout=self._connect_timeout)
        try:
            with engine.connect() as connection:
                rows = connection.execute(
                    select(
                        WorkflowRun.workflow_id,
                        WorkflowRun.status,
                        WorkflowRun.checkpoint,
                    )
                    .where(
                        WorkflowRun.status.in_(
                            [
                                WorkflowStatus.QUEUED,
                                WorkflowStatus.RUNNING,
                                WorkflowStatus.PAUSED,
                            ]
                        )
                    )
                    .where(run_kind_criteria())
                ).all()
                from sqlalchemy import inspect

                from backend.app.models.operation_run import OperationRun
                from backend.app.models.practice import GradeAttempt
                from backend.app.services.workflow_checkpoint import RunSnapshotBinding
                tables = set(inspect(connection).get_table_names())
                personal: list[RunSnapshotBinding] = []
                if "operation_runs" in tables:
                    personal.extend(RunSnapshotBinding(str(r[0]), str(r[1]), str(r[2])) for r in connection.execute(select(OperationRun.id, OperationRun.state, OperationRun.runtime_snapshot_id)))
                if "grade_attempts" in tables:
                    personal.extend(RunSnapshotBinding(str(r[0]), "grade_history", str(r[1])) for r in connection.execute(select(GradeAttempt.id, GradeAttempt.runtime_snapshot_id).where(GradeAttempt.runtime_snapshot_id.is_not(None))))
        except SQLAlchemyError:
            raise SettingsManagementError("LEGACY_RUN_INDEX_UNAVAILABLE", stage="runs") from None
        finally:
            engine.dispose()
        return (*snapshot_bindings_from_rows(
            (str(row[0]), row[1], row[2]) for row in rows
        ), *personal)


# ---------------------------------------------------------------------------
# 设置运行时
# ---------------------------------------------------------------------------


class SettingsRuntime:
    """以环境为边界持有设置服务；失败只暴露安全码。"""

    def __init__(
        self,
        *,
        profile: Any,
        config: ConfigService | None,
        failure: str | None = None,
        credential_backend: str = SECRET_BACKEND_MOUNTED,
        protector: Any = None,
        file_security: Any = None,
        mounts: Any = None,
        registry: ProviderRegistry | None = None,
        clock: Callable[[], datetime] | None = None,
        migrator: Callable[[str], None] | None = None,
        read_head: Callable[[str], str | None] | None = None,
        initialization_verify: Callable[[str, Mapping[str, Any]], None] | None = None,
        reference_catalog: Mapping[UUID, CredentialReference] | None = None,
        mount_failure: str | None = None,
        restart_channel: Callable[[], str] | None = None,
    ) -> None:
        self.profile = profile
        self.config = config
        self.failure = failure
        self.registry = registry or ProviderRegistry(
            environment_id=getattr(profile, "environment_id", None)
        )
        self._credential_backend = credential_backend
        self._protector = protector
        self._file_security = file_security
        self._mounts = mounts
        self._reference_catalog = dict(reference_catalog or {})
        self._mount_failure = mount_failure
        self._clock = clock or _utcnow
        self._store: Any = None
        self._store_error: str | None = None
        self._store_lock = threading.RLock()
        self._admission_configured = False
        self._probes: Any = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._snapshots: RuntimeSnapshotService | None = None
        self._index_previews = AdoptionPreviewStore()
        self._legacy_importer: Any = None
        self._closed = False
        self.environment = str(getattr(profile, "environment", "") or "")
        self.environment_id = getattr(profile, "environment_id", None)
        self._onboarding = OnboardingStore(Path(config.root) if config else Path("."), clock=self._clock)
        self._operations = OperationRegistry(Path(config.root) if config else Path("."), clock=self._clock)
        self.run_facts = BusinessRunFacts(
            settings=self._settings_for_runs,
            configured=self._has_active_revision,
        )
        self._initialization: InitializationService | None = None
        self._migrator = migrator
        self._read_head = read_head
        self._initialization_verify = initialization_verify
        # T221：只有本机 Launcher 注入的控制通道才能把“重启”交给自有 worker。
        self._restart_channel = restart_channel

    # -- 基础设施 ---------------------------------------------------------

    def _require_config(self) -> ConfigService:
        if self.config is None:
            raise SettingsManagementError(
                "SETTINGS_STORAGE_UNAVAILABLE",
                stage="config",
                status_code=503,
            )
        return self.config

    def _require_profile(self) -> Any:
        if self.profile is None:
            raise SettingsManagementError("ENVIRONMENT_PROFILE_UNAVAILABLE", status_code=503)
        return self.profile

    @property
    def snapshots(self) -> RuntimeSnapshotService:
        if self._snapshots is None:
            self._snapshots = RuntimeSnapshotService(self._require_config(), store=self._store)
        return self._snapshots

    def _has_active_revision(self) -> bool:
        if self.config is None:
            return False
        try:
            return self.config.read_manifest().active_revision is not None
        except ConfigServiceError:
            return True

    def _settings_for_runs(self) -> AppSettings | None:
        settings, _reason = self.effective_business_settings()
        return settings

    def _configure_admission(self, store: Any = None) -> None:
        """只有可信服务齐备时才打开激活门禁；缺任一事实保持闭锁。"""

        if self.config is None or self._admission_configured:
            return
        store = store if store is not None else self._store
        if store is None:
            return
        from backend.app.services.settings.probe_service import ProbeService

        self._probes = ProbeService(
            environment_id=self._require_profile().environment_id,
            registry=self.registry,
            credential_store=store,
            clock=self._clock,
        )

        def business_guard(_old: Any, new: ConfigurationValues) -> bool:
            business = new.business
            return (
                business.database_locator is not None
                and business.redis_locator is not None
                and business.storage_root is not None
            )

        def references(reference: CredentialReference, version: UUID) -> tuple[str, ...]:
            index = SnapshotReferenceIndex(self.snapshots, self.run_facts.bindings)
            return index(reference, version)

        self.config.configure_admission(
            registry=self.registry,
            probe_service=self._probes,
            business_guard=business_guard,
            runtime_references=references,
            legacy_runs=self.run_facts.bindings,
            clock=self._clock,
        )
        self._admission_configured = True

    def _open_store(self) -> Any:
        with self._store_lock:
            if self._store is not None:
                return self._store
            config = self.config
            if config is None:
                return None
            try:
                backend = self._credential_backend
                self._store = config.open_credentials(
                    backend=backend,
                    protector=self._protector,
                    file_security=self._file_security,
                    mounts=self._mounts,
                )
            except (ConfigServiceError, MountedSecretError, RuntimeError, ValueError) as error:
                self._store_error = getattr(error, "code", None) or "CREDENTIAL_BACKEND_UNAVAILABLE"
                return None
            self._configure_admission(self._store)
            return self._store

    def credential_store(self) -> Any:
        store = self._open_store()
        if store is None:
            raise SettingsManagementError(
                self._store_error or "CREDENTIAL_BACKEND_UNAVAILABLE",
                stage="credentials",
                status_code=503,
            )
        return store

    def credential_backend_health(self) -> bool | None:
        """仅在确有凭据引用时探测后端；无引用时保持“未评估”。"""

        if self._mount_failure is not None:
            return False
        values = self._current_values()
        if values is None or not _credential_refs(values):
            return None
        return self._open_store() is not None

    def _credentials_backend(self) -> str:
        return self._credential_backend

    def _current_values(self) -> ConfigurationValues | None:
        if self.config is None:
            return None
        try:
            manifest = self.config.read_manifest()
        except ConfigServiceError:
            return None
        revision_id = manifest.active_revision or manifest.pending_restart_revision
        if revision_id is None:
            return None
        try:
            return self.config.get_revision(revision_id).values
        except ConfigServiceError:
            return None

    def _secret(self, reference: CredentialReference | None) -> SecretStr | None:
        if reference is None:
            return None
        store = self.credential_store()
        try:
            return store.get(reference, reference.version)
        except (MountedSecretError, RuntimeError, ValueError) as error:
            raise SettingsManagementError(
                getattr(error, "code", None) or "CREDENTIAL_UNAVAILABLE",
                stage="credentials",
            ) from None

    # -- 视图 -------------------------------------------------------------

    def view(
        self, *, assembly: str, static_resources: str, app: FastAPI | None = None
    ) -> SettingsView:
        external_warnings = ("EXTERNALLY_MANAGED",) if self._credential_backend == SECRET_BACKEND_MOUNTED else ()
        if self.config is None:
            return SettingsView(
                environment=self.environment or None,
                environment_id=str(self.environment_id) if self.environment_id else None,
                configuration="invalid",
                warnings=(self.failure or "SETTINGS_STORAGE_UNAVAILABLE", *external_warnings),
                business_assembly=readiness.assembly_state(assembly),
                static_resources=static_resources,
            )
        try:
            manifest = self.config.read_manifest()
        except ConfigServiceError as error:
            return SettingsView(
                environment=self.environment or None,
                environment_id=str(self.environment_id) if self.environment_id else None,
                configuration="invalid",
                warnings=(error.code, *external_warnings),
                business_assembly=readiness.assembly_state(assembly),
                static_resources=static_resources,
            )
        values: ConfigurationValues | None = None
        warnings: list[str] = list(external_warnings)
        for revision_id in (manifest.active_revision, manifest.pending_restart_revision):
            if revision_id is None:
                continue
            try:
                revision = self.config.get_revision(revision_id)
            except ConfigServiceError as error:
                warnings.append(error.code)
                continue
            if values is None:
                values = revision.values
        from backend.app.core.config import resolve_configuration

        # Resolve only declared non-secret names, with no implicit dotenv read.
        resolved = resolve_configuration(saved=values.model_dump(mode="json") if values else None)
        effective = None if resolved.errors else ConfigurationValues.model_validate(resolved.segments)
        pending_values = self.config.get_revision(manifest.pending_restart_revision).values if manifest.pending_restart_revision else None
        statuses = self._credential_statuses(values, additional_values=pending_values)
        return SettingsView(
            environment=self.environment or None,
            environment_id=str(self.environment_id) if self.environment_id else None,
            configuration="available" if values is not None else "not_configured",
            active_revision=str(manifest.active_revision) if manifest.active_revision else None,
            pending_revision=(
                str(manifest.pending_restart_revision) if manifest.pending_restart_revision else None
            ),
            restart_required=manifest.pending_restart_revision is not None,
            values=values,
            pending_values=pending_values,
            effective_values=effective,
            effective_errors=resolved.errors,
            sources=resolved.sources,
            credential_statuses=statuses,
            warnings=tuple(warnings),
            business_assembly=readiness.assembly_state(assembly),
            static_resources=static_resources,
        )

    def _credential_statuses(self, values: ConfigurationValues | None, *,
                             additional_values: ConfigurationValues | None = None) -> tuple[CredentialStatusView, ...]:
        refs = list(_credential_refs(values)) if values is not None else []
        identities = {(reference.credential_id, reference.version) for reference in refs}
        if additional_values is not None:
            for reference in _credential_refs(additional_values):
                key = (reference.credential_id, reference.version)
                if key not in identities:
                    refs.append(reference)
                    identities.add(key)
        refs.extend(reference for reference in self._reference_catalog.values()
                    if (reference.credential_id, reference.version) not in identities)
        if not refs:
            return ()
        # GET 只投影已有后端事实；首次 catalog 尚未登记时保守 unavailable。
        # 登记/完整性核验仅由显式凭据/初始化/激活/Probe 写请求或启动执行。
        store = self._store
        if store is None:
            return tuple(
                CredentialStatusView(
                    credential_ref=item.credential_id,
                    version=item.version,
                    purpose=item.purpose,
                    status="unavailable",
                    environment_id=item.environment_id,
                    backend=item.backend,
                    external_secret_id=item.external_secret_id,
                )
                for item in refs
            )
        result: list[CredentialStatusView] = []
        for item in refs:
            try:
                status: CredentialState = store.status(item, item.version).status
            except (MountedSecretError, RuntimeError, ValueError):
                status = "unavailable"
            result.append(
                CredentialStatusView(
                    credential_ref=item.credential_id,
                    version=item.version,
                    purpose=item.purpose,
                    status=status,
                    environment_id=item.environment_id,
                    backend=item.backend,
                    external_secret_id=item.external_secret_id,
                )
            )
        return tuple(result)

    # -- 就绪 -------------------------------------------------------------

    def readiness_inputs(self, app: FastAPI) -> readiness.ReadinessInputs:
        candidate = getattr(app.state, "settings", None) or getattr(app.state, "business_candidate", None)
        if candidate is None:
            candidate, _reason = self.effective_business_settings()
        inputs = readiness.inputs_for_app(app, business_settings=candidate)
        if self.config is None:
            return inputs
        try:
            manifest = self.config.read_manifest()
        except ConfigServiceError:
            return replace(
                inputs,
                configuration_revision=None,
                pending_revision=None,
                expected_migration_head=readiness.discover_migration_head(),
                credential_backend_available=self.credential_backend_health(),
            )
        return replace(
            inputs,
            configuration_revision=(
                str(manifest.active_revision) if manifest.active_revision else None
            ),
            pending_revision=(
                str(manifest.pending_restart_revision) if manifest.pending_restart_revision else None
            ),
            expected_migration_head=readiness.discover_migration_head(),
            credential_backend_available=self.credential_backend_health(),
        )

    def prerequisites_failure(
        self, business: AppSettings, *, probes: readiness.DependencyProbes | None = None
    ) -> tuple[str, str] | None:
        report = readiness.evaluate_readiness(
            self._inputs_for(business),
            probes,
        )
        return readiness.prerequisites_failure(report)

    def _inputs_for(self, business: AppSettings) -> readiness.ReadinessInputs:
        return readiness.ReadinessInputs(
            environment=self.environment or None,
            environment_id=str(self.environment_id) if self.environment_id else None,
            business_settings=business,
            setup_ready=True,
            static_resources="not_bundled",
            assembly="not_assembled",
            recovery="not_started",
            configuration_revision=self._active_revision_text(),
            pending_revision=self._pending_revision_text(),
            expected_migration_head=readiness.discover_migration_head(),
            credential_backend_available=self.credential_backend_health(),
        )

    def _active_revision_text(self) -> str | None:
        if self.config is None:
            return None
        try:
            manifest = self.config.read_manifest()
        except ConfigServiceError:
            return None
        return str(manifest.active_revision) if manifest.active_revision else None

    def _pending_revision_text(self) -> str | None:
        if self.config is None:
            return None
        try:
            manifest = self.config.read_manifest()
        except ConfigServiceError:
            return None
        return (
            str(manifest.pending_restart_revision)
            if manifest.pending_restart_revision
            else None
        )

    def deployment_facts(self) -> dict[str, Any]:
        """启动装配写入 app.state 的既有事实；不新建第二个状态源。"""

        return {
            "configuration_revision": self._active_revision_text(),
            "pending_revision": self._pending_revision_text(),
            "migration_head": readiness.discover_migration_head(),
            "credential_backend_available": self.credential_backend_health(),
        }

    # -- 业务设置投影 ------------------------------------------------------

    def personal_learning_database(self, *, values: ConfigurationValues | None = None):
        """Resolve only current-space DB facts; legacy JWT/AI are separate consumers."""
        from backend.app.core.config import resolve_configuration
        from backend.app.core.environment import environment_path
        from backend.app.schemas.settings import EnvironmentProfile

        profile = self._require_profile()
        config = self._require_config()
        if not isinstance(profile, EnvironmentProfile):
            raise SettingsManagementError("ENVIRONMENT_PROFILE_UNAVAILABLE", status_code=503)
        try:
            profile.validate_paths()
        except ValueError:
            raise SettingsManagementError("ENVIRONMENT_PROFILE_UNAVAILABLE", status_code=503) from None
        if config.environment_id != profile.environment_id or config.root != profile.config_root.parent:
            raise SettingsManagementError("ENVIRONMENT_MISMATCH", status_code=503)
        if values is None:
            active = config.active_revision()
            if active is None:
                raise SettingsManagementError("LEARNING_CONFIGURATION_UNAVAILABLE", status_code=503)
            values = active.values
        resolved = resolve_configuration(saved=values.model_dump(mode="json"), process_env=dict(os.environ))
        business = resolved.segments.get("business")
        if resolved.errors or not isinstance(business, BusinessSettings) or business.database_locator is None:
            raise SettingsManagementError("LEARNING_CONFIGURATION_UNAVAILABLE", status_code=503)
        if business.storage_root != environment_path(profile, "storage"):
            raise SettingsManagementError("FILE_STORAGE_ENVIRONMENT_MISMATCH", status_code=503)
        if not profile.storage_root.is_dir():
            raise SettingsManagementError("LEARNING_STORAGE_UNAVAILABLE", status_code=503)
        return _database_url(business.database_locator, self._secret(business.database_password_ref)), profile

    def pending_startup_settings(self) -> tuple[AppSettings | None, UUID | None, str | None]:
        """新工厂使用的候选；校验失败保留 active/pending，不回退冒充已生效。"""
        if self.config is None:
            return None, None, None
        pending: UUID | None = None
        try:
            pending = self.config.read_manifest().pending_restart_revision
            if pending is None:
                return None, None, None
            self.credential_store()
            revision = self.config.check_pending_restart(
                expected_revision=pending, process_env=dict(os.environ),
            )
            if revision.values.business.jwt_secret_ref is None:
                # A personal-only candidate never assembles legacy consumers.
                # Verify its actual database/schema before committing activation.
                from sqlalchemy import create_engine, text
                from sqlalchemy.exc import SQLAlchemyError

                url, _profile = self.personal_learning_database(values=revision.values)
                engine = create_engine(url, connect_args={"connect_timeout": 3})
                try:
                    with engine.connect() as connection:
                        if connection.scalar(text("SELECT version_num FROM alembic_version")) != readiness.discover_migration_head():
                            raise SettingsManagementError("LEARNING_DATABASE_NOT_INITIALIZED", status_code=503)
                except SQLAlchemyError:
                    raise SettingsManagementError("LEARNING_DATABASE_UNAVAILABLE", status_code=503) from None
                finally:
                    engine.dispose()
                return None, pending, None
            return self.app_settings_for(revision.values), pending, None
        except (ConfigServiceError, SettingsManagementError) as error:
            return None, pending, error.code

    def accept_pending_startup(self, expected_revision: UUID) -> None:
        try:
            self._require_config().accept_pending_restart(
                expected_revision=expected_revision, process_env=dict(os.environ),
            )
        except ConfigServiceError as error:
            raise SettingsManagementError(error.code, stage="startup") from None

    def effective_business_settings(self) -> tuple[AppSettings | None, str | None]:
        """优先已保存 active 修订；没有修订时沿用显式进程环境。"""

        if self.config is not None:
            try:
                manifest = self.config.read_manifest()
            except ConfigServiceError as error:
                return None, error.code
            if manifest.active_revision is not None:
                try:
                    revision = self.config.get_revision(manifest.active_revision)
                    return self.app_settings_for(revision.values), None
                except ConfigServiceError as error:
                    return None, error.code
                except SettingsManagementError as error:
                    return None, error.code
        from backend.app.core.bootstrap import load_business_settings

        settings = load_business_settings()
        if settings is None:
            return None, "BUSINESS_CONFIG_NOT_CONFIGURED"
        return settings, None

    def ai_readiness(self) -> AiReadiness:
        """只读 active 修订的逐任务事实；不触发 Probe、模型或外部服务。"""

        if self.config is None:
            return AiReadiness(
                evaluated=True,
                status="unavailable",
                reason="configuration_unavailable",
                tasks={},
            )
        try:
            manifest = self.config.read_manifest()
        except ConfigServiceError as error:
            return AiReadiness(
                evaluated=True,
                status="unavailable",
                reason=error.code,
                tasks={},
            )
        if manifest.active_revision is None:
            return AiReadiness(
                evaluated=True,
                status="not_configured",
                reason="no_active_configuration",
                tasks={},
            )
        try:
            revision = self.config.get_revision(manifest.active_revision)
        except ConfigServiceError as error:
            return AiReadiness(
                evaluated=True,
                status="unavailable",
                reason=error.code,
                tasks={},
            )
        # 无 AI 凭据引用时不打开/初始化凭据后端，保持 GET 只读语义。
        has_references = any(
            profile.credential_ref is not None
            for profile in revision.values.providers.values()
        )
        store = self._open_store() if has_references else None

        def status_of(reference: CredentialReference) -> CredentialState | None:
            if store is None:
                return None
            try:
                return store.status(reference, reference.version).status
            except Exception:  # noqa: BLE001 - 凭据状态失败不得变成“可用”
                return "unavailable"

        return evaluate_ai_readiness(
            values=revision.values,
            registry=self.registry,
            activation_validation=revision.activation_validation,
            credential_status=status_of,
            resource_exists=lambda path: path.is_dir(),
        )

    def app_settings_for(self, values: ConfigurationValues) -> AppSettings:
        from backend.app.core.config import (
            EMBEDDING_DIMENSION_DEFAULT,
            AppSettings,
            resolve_configuration,
        )

        self._require_config()
        # 显式候选必须贯穿业务/策略投影；不能从旧 active 偷取 business。
        resolved = resolve_configuration(saved=values.model_dump(mode="json"), process_env=dict(os.environ))
        if resolved.errors:
            raise SettingsManagementError("INVALID_CONFIGURATION", stage="resolve")
        business = resolved.segments.get("business")
        policies = resolved.segments.get("policies")
        if not isinstance(business, BusinessSettings):
            raise SettingsManagementError("INVALID_CONFIGURATION", stage="resolve")
        if (
            business.database_locator is None
            or business.redis_locator is None
            or business.storage_root is None
            or business.jwt_secret_ref is None
        ):
            raise SettingsManagementError("BUSINESS_CONFIGURATION_INCOMPLETE", stage="resolve")
        policies = policies if hasattr(policies, "confidence_threshold") else values.policies
        llm = self._profile_for("default_llm", values)
        embedding = self._profile_for("embedding", values)
        llm_key = self._safe_secret(llm.credential_ref) if llm is not None else None
        # 缺 Key/凭据的 AI 不是装配失败：显式降为 none，AI 入口单独门禁。
        llm_ready = llm is not None and (
            llm_key is not None or (llm.auth_mode == "none" and llm.adapter_id != "deepseek")
        )
        embedding_key = (
            self._safe_secret(embedding.credential_ref) if embedding is not None else None
        )
        embedding_ready = embedding is not None and (
            embedding.adapter_id != "openai_compatible" or embedding_key is not None
        )
        rerank = self._profile_for("rerank", values)
        rerank_ready = rerank is not None and (
            rerank.adapter_id == "cross_encoder" or llm_ready
        )
        vision = self._profile_for("vision", values)
        ocr = self._profile_for("ocr", values)
        payload: dict[str, Any] = {
            "database_url": _database_url(
                business.database_locator, self._secret(business.database_password_ref)
            ),
            "redis_url": _redis_url(
                business.redis_locator, self._secret(business.redis_password_ref)
            ),
            "storage_root": business.storage_root,
            "JWT_SECRET_KEY": self._secret(business.jwt_secret_ref),
            "JWT_ALGORITHM": business.jwt_algorithm,
            "JWT_EXPIRE_MINUTES": business.jwt_expire_minutes,
            "DEV_MODE": business.dev_mode,
            "llm_provider": llm.adapter_id if llm_ready and llm is not None else "none",
            "llm_auth_mode": llm.auth_mode if llm_ready and llm is not None else "api_key",
            "deepseek_api_key": (
                llm_key if llm_ready and llm_key is not None else SecretStr("")
            ),
            "deepseek_base_url": (
                (llm.base_url or "https://api.deepseek.com")
                if llm_ready and llm is not None
                else None
            ),
            "deepseek_model": llm.model_id if llm_ready and llm is not None else "",
            "embedding_provider": (
                embedding.adapter_id if embedding_ready and embedding is not None else "none"
            ),
            "embedding_model": (
                embedding.model_id if embedding_ready and embedding is not None else None
            ),
            "embedding_base_url": (
                embedding.base_url if embedding_ready and embedding is not None else None
            ),
            "embedding_api_key": embedding_key if embedding_ready else None,
            "embedding_dimension": EMBEDDING_DIMENSION_DEFAULT,
            "rerank_provider": (
                rerank.adapter_id if rerank_ready and rerank is not None else "none"
            ),
            "rerank_model": rerank.model_id if rerank_ready and rerank is not None else None,
            "vision_model": (
                vision.model_id if vision is not None and llm_ready else None
            ),
            "confidence_threshold": getattr(policies, "confidence_threshold", 0.8),
            "hybrid_vector_weight": getattr(policies, "hybrid_weight", 0.5),
            "rerank_max_candidates": getattr(policies, "rerank_max_candidates", 5),
            "rerank_timeout_seconds": getattr(policies, "rerank_timeout_seconds", 30.0),
            "ocr_enabled": bool(ocr is not None and ocr.enabled),
            "ocr_provider": ocr.adapter_id if ocr is not None else None,
            "ocr_model": ocr.model_id if ocr is not None else None,
            "ocr_model_dir": ocr.resource_directory if ocr is not None else None,
        }
        try:
            # `_env_file=None`：已激活配置不得被仓库 `.env` 的默认 AI 值补充/覆盖。
            return AppSettings(_env_file=None, **payload)  # type: ignore[call-arg]
        except ValidationError:
            raise SettingsManagementError("INVALID_CONFIGURATION", stage="assembly") from None

    def _safe_secret(self, reference: CredentialReference | None) -> SecretStr | None:
        """AI 装配用：凭据不可用时降为未配置，而不是阻断业务装配。"""

        if reference is None:
            return None
        try:
            return self._secret(reference)
        except SettingsManagementError:
            return None

    def _profile_for(self, task_kind: str, values: ConfigurationValues) -> ProviderProfile | None:
        try:
            resolved = self.registry.resolve_binding(
                task_kind=task_kind,
                profiles=values.providers,
                bindings=values.bindings,
            )
        except RegistryValidationError:
            return None
        if resolved.mode == "rule_based" or resolved.profile_id is None:
            return None
        profile = values.providers.get(str(resolved.profile_id))
        if profile is None or not profile.enabled:
            return None
        return profile

    # -- 凭据 -------------------------------------------------------------

    def _reference_for(self, credential_id: UUID, version: UUID, purpose: str) -> CredentialReference:
        backend = self._credentials_backend()
        if backend == SECRET_BACKEND_MOUNTED:
            reference = self._reference_catalog.get(credential_id)
            if reference is None or reference.version != version or reference.purpose != purpose:
                raise SettingsManagementError(
                    "SECRET_REFERENCE_NOT_ALLOWED", stage="credentials", status_code=403
                )
            return reference
        return CredentialReference(
            credential_id=credential_id,
            version=version,
            environment_id=self._require_profile().environment_id,
            backend="dpapi",
            purpose=purpose,
        )

    def create_credential(self, purpose: str, secret: SecretStr) -> CredentialReference:
        store = self.credential_store()
        try:
            return store.put(purpose=purpose, secret_value=secret)
        except (MountedSecretError, RuntimeError, ValueError) as error:
            raise SettingsManagementError(
                getattr(error, "code", None) or "INVALID_SECRET_WRITE",
                stage="credentials",
            ) from None

    def add_credential_version(self, credential_id: UUID, secret: SecretStr) -> CredentialReference:
        reference = self._find_reference(credential_id)
        store = self.credential_store()
        try:
            return store.put(
                purpose=reference.purpose,
                secret_value=secret,
                credential_id=credential_id,
            )
        except (MountedSecretError, RuntimeError, ValueError) as error:
            raise SettingsManagementError(
                getattr(error, "code", None) or "INVALID_SECRET_WRITE",
                stage="credentials",
            ) from None

    def delete_credential_version(self, credential_id: UUID, version: UUID) -> None:
        found = self._find_reference(credential_id)
        # 存储层要求引用的 version 与待删版本一致；用途/后端沿用同一凭据身份。
        reference = found.model_copy(update={"version": version})
        store = self.credential_store()
        try:
            with self._require_config().reference_transaction():
                store.delete(reference, version)
        except (MountedSecretError, RuntimeError, ValueError) as error:
            raise SettingsManagementError(
                getattr(error, "code", None) or "CREDENTIAL_DELETE_FAILED",
                stage="credentials",
            ) from None

    def _find_reference(self, credential_id: UUID) -> CredentialReference:
        if self.config is None:
            raise SettingsManagementError("CREDENTIAL_BACKEND_UNAVAILABLE", status_code=503)
        values = self._current_values()
        if values is not None:
            for item in _credential_refs(values):
                if item.credential_id == credential_id:
                    return item
        raise SettingsManagementError("CREDENTIAL_NOT_FOUND", status_code=404)

    # -- 草稿与绑定 --------------------------------------------------------

    def create_draft(
        self,
        *,
        base_revision: UUID | None,
        changes: Mapping[str, Any],
        acknowledge_override: bool = False,
    ):
        """从默认值叠加部分非敏感 changes；秘密字段/未知字段一律 422。"""

        document = _deep_merge(
            ConfigurationValues().model_dump(mode="json"), dict(changes)
        )
        try:
            values = ConfigurationValues.model_validate(document)
        except ValidationError:
            raise SettingsManagementError("INVALID_CONFIGURATION", stage="draft") from None
        return self._require_config().create_draft(
            values,
            expected_revision=base_revision,
            process_env=dict(os.environ),
            acknowledge_override=acknowledge_override,
        )

    def update_draft(
        self,
        draft_id: UUID,
        *,
        expected_revision: UUID | None,
        changes: Mapping[str, Any],
        acknowledge_override: bool = False,
    ):
        """PATCH 草稿 = 新草稿修订；旧草稿保留为证据，不原地改写。"""

        config = self._require_config()
        with config.reference_transaction():
            previous = config.get_draft(draft_id)
            if previous.parent_revision != expected_revision:
                raise SettingsManagementError("REVISION_CONFLICT", stage="draft")
            try:
                merged = ConfigurationValues.model_validate(
                    _deep_merge(previous.values.model_dump(mode="json"), dict(changes))
                )
            except ValidationError:
                raise SettingsManagementError("INVALID_CONFIGURATION", stage="draft") from None
            return config.create_draft(
                merged,
                expected_revision=expected_revision,
                process_env=dict(os.environ),
                acknowledge_override=acknowledge_override,
            )

    def bind_credential(
        self,
        draft_id: UUID,
        *,
        binding: CredentialBindingRequest,
    ):
        config = self._require_config()
        with config.reference_transaction():
            previous = config.get_draft(draft_id)
            document = previous.values.model_dump(mode="json")
            providers = document.get("providers") or {}
            profile = providers.get(str(binding.profile_id))
            if not isinstance(profile, dict):
                raise SettingsManagementError("PROFILE_NOT_FOUND", status_code=404)
            reference = self._reference_for(
                binding.credential_ref, binding.version, "provider"
            )
            status = self.credential_store().status(reference, binding.version)
            if status.status != "available":
                raise SettingsManagementError("CREDENTIAL_UNAVAILABLE", stage="binding")
            profile["credential_ref"] = reference.model_dump(mode="json")
            try:
                merged = ConfigurationValues.model_validate(document)
            except ValidationError:
                raise SettingsManagementError("INVALID_CONFIGURATION", stage="draft") from None
            return config.create_draft(
                merged,
                expected_revision=previous.parent_revision,
                process_env=dict(os.environ),
                acknowledge_override=True,
            )

    def validate_draft(
        self, draft_id: UUID, *, expected_revision: UUID | None
    ) -> ValidationReport:
        config = self._require_config()
        try:
            validated = config.validate(
                draft_id, expected_revision=expected_revision, process_env=dict(os.environ)
            )
        except ConfigServiceError as error:
            raise SettingsManagementError(error.code, stage=error.stage) from None
        return ValidationReport(
            valid=True,
            revision_id=validated.revision_id,
            affected_tasks=tuple(
                sorted(
                    task
                    for task, binding in validated.values.bindings.items()
                    if task != "reviewer" and (binding.inherit_default or binding.profile_id is not None)
                )
            ),
            restart_required=False,
        )

    def activate(
        self,
        draft_id: UUID,
        *,
        probe_ids: Sequence[UUID],
        acknowledge_override: bool = False,
        expected_active_revision: UUID | None = None,
    ) -> ActivationView:
        config = self._require_config()
        manifest = config.read_manifest()
        if expected_active_revision is not None and manifest.active_revision != expected_active_revision:
            raise SettingsManagementError("REVISION_CONFLICT", stage="activation")
        # 带凭据的候选必须先证明后端可用；不因缺少后端而跳过凭据检查。
        if _credential_refs(config.get_draft(draft_id).values):
            self.credential_store()
        self._open_store()
        try:
            # 并发仍由配置服务的最外层期望修订检查兜底：这里用服务器读到的 head。
            revision = config.activate(
                draft_id,
                expected_revision=manifest.head_revision,
                process_env=dict(os.environ),
                acknowledge_override=acknowledge_override,
                probe_ids=tuple(probe_ids),
            )
        except ConfigServiceError as error:
            raise SettingsManagementError(error.code, stage=error.stage) from None
        return ActivationView(
            revision_id=revision.revision_id,
            status="pending_restart" if revision.status == "pending_restart" else "active",
            restart_required=revision.status == "pending_restart",
            activation_keys=tuple(sorted(revision.activation_validation)),
        )

    # -- 索引身份 ----------------------------------------------------------

    @contextmanager
    def index_identity(
        self, session: ManagementSession
    ) -> Iterator[tuple[IndexIdentityService, Any]]:
        """按请求持有业务 DB 会话；未装配/依赖不可用时 503，不要求 Business Ready。"""

        settings, reason = self.effective_business_settings()
        if settings is None:
            raise SettingsManagementError(
                reason or "DEPENDENCY_UNAVAILABLE", stage="index_identity", status_code=503
            )
        from backend.app.core.database import (
            create_database_engine,
            create_session_factory,
        )
        from backend.app.services.settings.index_identity import (
            IndexIdentityService,
            SqlAlchemyIndexRepository,
        )

        engine = create_database_engine(settings, connect_timeout=3)
        factory = create_session_factory(engine)
        db = factory()
        principal = {
            "principal_kind": session.operator.kind.value,
            "principal_id": session.operator.principal_id,
            "environment_id": str(session.environment_id),
            "instance_id": str(session.instance_id),
            "management_session_id": str(session.session_id),
        }
        try:
            service = IndexIdentityService(
                repository=SqlAlchemyIndexRepository(db),
                principal=lambda: principal,
                clock=self._clock, preview_store=self._index_previews,
            )
            yield service, db
        finally:
            try:
                db.close()
            finally:
                engine.dispose()

    # -- 初始化/重启/导出 --------------------------------------------------

    def initialization(self) -> InitializationService:
        if self._initialization is None:
            config = self._require_config()

            def database_url(values: ConfigurationValues) -> str:
                locator = values.business.database_locator
                if locator is None:
                    raise SettingsManagementError("BUSINESS_CONFIGURATION_INCOMPLETE")
                return _database_url(
                    locator, self._secret(values.business.database_password_ref)
                )

            profile = self._require_profile()
            test_instance_id = None
            if (
                str(getattr(profile, "environment", "")) == "test"
                and getattr(profile, "database_locator", None) is not None
            ):
                test_instance_id = profile.database_locator.instance_id

            def verify(url: str, expected: Mapping[str, Any]) -> None:
                require_test_identity(self.environment, test_instance_id)
                verify_database_url(
                    url, expected=expected, test_instance_id=test_instance_id
                )

            self._initialization = InitializationService(
                config=config,
                environment=self.environment,
                database_url=database_url,
                clock=self._clock,
                migrator=self._migrator or run_alembic_upgrade,
                read_head=self._read_head,
                operations=self._operations,
                verify_database=self._initialization_verify or verify,
            )
        return self._initialization

    def restart(self, *, expected_revision: UUID) -> tuple[str, str, str | None]:
        config = self._require_config()
        manifest = config.read_manifest()
        head = manifest.pending_restart_revision or manifest.active_revision
        if head != expected_revision:
            raise SettingsManagementError("REVISION_CONFLICT", stage="restart")
        pending = str(manifest.pending_restart_revision) if manifest.pending_restart_revision else None
        if self._restart_channel is not None:
            outcome = self._restart_channel()
            if outcome == "accepted":
                return (
                    "restart_requested",
                    "已请求受控重启；应用将在当前任务排空后由本机 Launcher 重启。",
                    pending,
                )
        return (
            "manual_restart_required",
            "本进程不含受控重启通道；请按运行方式重启（Launcher、容器或源码启动入口）以应用待生效配置。",
            pending,
        )

    def export_sections(self, app: FastAPI, include: Sequence[str]) -> dict[str, Any]:
        manifest = self.config.read_manifest() if self.config is not None else None
        values = self._current_values()
        sections: dict[str, Any] = {}
        if "configuration" in include:
            sections["configuration"] = _export_configuration(values, manifest)
        if "readiness" in include:
            report = readiness.evaluate_readiness(
                self.readiness_inputs(app), ai=self.ai_readiness()
            )
            sections["readiness"] = report.model_dump(mode="json")
        if "versions" in include:
            sections["versions"] = {
                "environment": self.environment or None,
                "environment_id": str(self.environment_id) if self.environment_id else None,
                "python": sys.version.split()[0],
                "migration_head": readiness.discover_migration_head(),
            }
        return sections

    def onboarding(self) -> OnboardingView:
        return self._onboarding.read()

    def onboarding_draft(self, draft_revision: UUID | None) -> Any:
        if draft_revision is None:
            return None
        config = self._require_config()
        if not (config.config / "drafts" / f"{draft_revision}.json").exists():
            return None
        return config.get_draft(draft_revision)

    def existing_probes(self, draft_revision: UUID | None) -> tuple[Any, ...]:
        if draft_revision is None or self._probes is None:
            return ()
        return self._probes.for_candidate(draft_revision)

    def provider_catalog(self) -> tuple[dict[str, Any], ...]:
        """实际 Registry 条目 + 参数 Schema；不返回 Key，不推能力。"""

        result: list[dict[str, Any]] = []
        for kind in ("llm", "embedding", "rerank", "vision", "ocr"):
            for entry in self.registry.entries(kind=kind):
                result.append(
                    {
                        "kind": kind,
                        "adapter_id": entry.adapter_id,
                        "capabilities": sorted(entry.supported_capabilities),
                        "lifecycle": entry.lifecycle,
                        "canonical_id": entry.canonical_id,
                        "parameters": entry.config_schema.model_json_schema(),
                    }
                )
        return tuple(result)

    def legacy_importer(self) -> Any:
        """只接受服务端许可来源 ID 的显式旧配置导入；不改删源文件。"""

        config = self._require_config()
        from backend.app.deployment.configuration_editor import LegacyConfigImporter

        sources: dict[str, Path] = {}
        raw = (os.environ.get("EDUAGENT_LEGACY_IMPORT_SOURCES") or "").strip()
        if raw:
            try:
                data = json.loads(raw)
            except ValueError:
                raise SettingsManagementError("INVALID_LEGACY_IMPORT_SOURCE") from None
            if not isinstance(data, dict):
                raise SettingsManagementError("INVALID_LEGACY_IMPORT_SOURCE")
            for source_id, value in data.items():
                if not isinstance(source_id, str) or not isinstance(value, str):
                    raise SettingsManagementError("INVALID_LEGACY_IMPORT_SOURCE")
                path = Path(value)
                if not path.is_absolute() or path != path.resolve():
                    raise SettingsManagementError("INVALID_LEGACY_IMPORT_SOURCE")
                sources[source_id] = path
        try:
            if self._legacy_importer is None or self._legacy_importer.sources != sources:
                self._legacy_importer = LegacyConfigImporter(service=config, sources=sources, clock=self._clock)
            return self._legacy_importer
        except ConfigServiceError as error:
            raise SettingsManagementError(error.code, stage=error.stage) from None

    def update_onboarding(
        self, *, expected_revision: int | None, current_step: int, draft_revision: UUID | None
    ) -> OnboardingView:
        return self._onboarding.update(
            expected_revision=expected_revision,
            current_step=current_step,
            draft_revision=draft_revision,
        )

    def operations(self) -> OperationRegistry:
        return self._operations

    # -- Probe ------------------------------------------------------------

    def probes(self) -> Any:
        if self._probes is None:
            raise SettingsManagementError("PROBE_SERVICE_UNAVAILABLE", status_code=503)
        return self._probes

    async def start_probe(
        self,
        *,
        candidate_revision: UUID,
        profile: ProviderProfile,
        capability: str,
        allow_billable: bool,
        timeout_seconds: float,
    ) -> Any:
        service = self.probes()
        probe_id = uuid4()
        task = asyncio.create_task(
            service.run(
                candidate_revision=candidate_revision,
                profile=profile,
                capability=capability,
                allow_billable=allow_billable,
                timeout_seconds=timeout_seconds,
                probe_id=probe_id,
            )
        )
        self._tasks.add(task)

        def finished(completed: asyncio.Task[Any]) -> None:
            self._tasks.discard(completed)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(finished)
        # run 的本地前置与 running 记录先建立，再把 ID 交给浏览器；
        # 后续网络与释放仍在持有的任务中执行，创建请求不等待检测终态。
        await asyncio.sleep(0)
        if task.done():
            return task.result()
        return service.get(probe_id)

    # -- 生命周期 ----------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._index_previews.lock:
            self._index_previews.previews.clear()
        self._legacy_importer = None
        for task in tuple(self._tasks):
            task.cancel()
        self._tasks.clear()
        store, self._store = self._store, None
        try:
            if store is not None:
                store.close()
        finally:
            if self.config is not None:
                self.config.close()

    async def aclose(self) -> None:
        probes, self._probes = self._probes, None
        try:
            if probes is not None:
                await probes.aclose()
        finally:
            self.close()


def _credential_refs(values: ConfigurationValues) -> tuple[CredentialReference, ...]:
    business = values.business
    found = [
        business.database_password_ref,
        business.redis_password_ref,
        business.jwt_secret_ref,
    ]
    found.extend(profile.credential_ref for profile in values.providers.values())
    return tuple(item for item in found if item is not None)


def _deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in overlay.items():
        # 模型内部使用 schema_name，HTTP 输出使用 schema；合并时去掉双名
        # 歧义，否则合法 HTTP round-trip 会遗留一个 extra 字段。
        if key == "schema" and "schema_name" in result:
            key = "schema_name"
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _database_url(locator: Any, secret: SecretStr | None) -> str:
    password = quote(secret.get_secret_value(), safe="") if secret is not None else ""
    auth = quote(locator.user, safe="") + (f":{password}" if password else "")
    schema = locator.schema_name or "public"
    # Keep writes in the selected schema while resolving shared pgvector types.
    search_path = schema if schema == "public" else f"{schema},public"
    return (
        f"postgresql+psycopg://{auth}@{locator.host}:{locator.port}/{locator.database}"
        f"?options=-csearch_path%3D{quote(search_path, safe='')}"
    )


def _redis_url(locator: Any, secret: SecretStr | None) -> str:
    password = quote(secret.get_secret_value(), safe="") if secret is not None else ""
    auth = f":{password}@" if password else ""
    return f"redis://{auth}{locator.host}:{locator.port}/{locator.database}"


def _export_configuration(values: ConfigurationValues | None, manifest: Any) -> dict[str, Any]:
    if values is None:
        return {
            "active_revision": None,
            "pending_revision": None,
            "configured": False,
        }
    business = values.business
    locator = business.database_locator
    redis = business.redis_locator
    return {
        "active_revision": str(manifest.active_revision) if manifest and manifest.active_revision else None,
        "pending_revision": (
            str(manifest.pending_restart_revision)
            if manifest and manifest.pending_restart_revision
            else None
        ),
        "configured": True,
        "database": (
            {
                "host": locator.host,
                "port": locator.port,
                "database": locator.database,
                "user": locator.user,
                "schema": locator.schema_name,
            }
            if locator is not None
            else None
        ),
        "redis": (
            {"host": redis.host, "port": redis.port, "database": redis.database}
            if redis is not None
            else None
        ),
        "storage_root": "<configured>" if business.storage_root is not None else None,
        "providers": [
            {
                "display_name": profile.display_name,
                "adapter_id": profile.adapter_id,
                "model_id": profile.model_id,
                "base_url": profile.base_url,
                "enabled": profile.enabled,
                "capabilities": list(profile.declared_capabilities),
            }
            for profile in values.providers.values()
        ],
        "bindings": {
            task: {
                "inherit_default": binding.inherit_default,
                "profile_id": str(binding.profile_id) if binding.profile_id else None,
                "retrieval_mode": binding.retrieval_mode,
            }
            for task, binding in values.bindings.items()
        },
        "policies": values.policies.model_dump(mode="json"),
    }


def build_settings_runtime(
    profile: Any,
    *,
    credential_backend: str | None = None,
    protector: Any = None,
    file_security: Any = None,
    mounts: Any = None,
    registry: ProviderRegistry | None = None,
    clock: Callable[[], datetime] | None = None,
    migrator: Callable[[str], None] | None = None,
    read_head: Callable[[str], str | None] | None = None,
    initialization_verify: Callable[[str, Mapping[str, Any]], None] | None = None,
    restart_channel: Callable[[], str] | None = None,
) -> SettingsRuntime:
    """构造运行时；目录/锁失败降级为安全码，不阻断引导应用启动。"""

    if credential_backend is None:
        configured = (os.environ.get(SECRET_BACKEND_ENV) or "").strip().lower()
        if configured in {SECRET_BACKEND_DPAPI, "mounted", SECRET_BACKEND_MOUNTED}:
            credential_backend = (
                SECRET_BACKEND_DPAPI if configured == SECRET_BACKEND_DPAPI else SECRET_BACKEND_MOUNTED
            )
        else:
            credential_backend = (
                SECRET_BACKEND_DPAPI if sys.platform == "win32" else SECRET_BACKEND_MOUNTED
            )
    if credential_backend not in {SECRET_BACKEND_DPAPI, SECRET_BACKEND_MOUNTED}:
        return SettingsRuntime(
            profile=profile,
            config=None,
            failure="CREDENTIAL_BACKEND_UNSUPPORTED",
            credential_backend=SECRET_BACKEND_MOUNTED,
        )
    reference_catalog: dict[UUID, CredentialReference] = {}
    mount_failure: str | None = None
    if credential_backend == SECRET_BACKEND_MOUNTED:
        if mounts is None and profile is not None:
            try:
                mounts, reference_catalog = mounted_mounts_from_env(profile.environment_id)
            except SettingsManagementError:
                mounts, reference_catalog = {}, {}
                mount_failure = "INVALID_MOUNTED_MAPPING"
        elif mounts is not None:
            try:
                reference_catalog = {
                    reference.credential_id: reference
                    for reference, _path in mounts.values()
                }
            except (AttributeError, TypeError, ValueError):
                mounts, reference_catalog = {}, {}
                mount_failure = "INVALID_MOUNTED_MAPPING"
    config: ConfigService | None = None
    failure = None
    if profile is not None:
        try:
            config = ConfigService(profile.config_root.parent, profile.environment_id)
        except ConfigServiceError as error:
            failure = error.code
        except (OSError, ValueError):
            failure = "SETTINGS_STORAGE_UNAVAILABLE"
    else:
        failure = "ENVIRONMENT_PROFILE_UNAVAILABLE"
    return SettingsRuntime(
        profile=profile,
        config=config,
        failure=failure,
        credential_backend=credential_backend,
        protector=protector,
        file_security=file_security,
        mounts=mounts,
        registry=registry,
        clock=clock,
        migrator=migrator,
        read_head=read_head,
        initialization_verify=initialization_verify,
        reference_catalog=reference_catalog,
        mount_failure=mount_failure,
        restart_channel=restart_channel,
    )


__all__ = [
    "SECRET_BACKEND_ENV",
    "BusinessRunFacts",
    "InitializationService",
    "OnboardingStore",
    "OperationRegistry",
    "SettingsManagementError",
    "SettingsRuntime",
    "build_settings_runtime",
    "read_current_alembic_head",
    "status_for",
]
