"""Authenticated, offline maintenance; application configuration is never switched."""
from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Self
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from backend.app.core.environment import environment_path
from backend.app.core.settings_security import ManagementSession, SetupSecurityService
from backend.app.domain.enums import UserRole
from backend.app.models import User
from backend.app.schemas.settings import EnvironmentProfile

MARKER = ".maintenance.json"


def require_admin_user(session: Session, actor_id: UUID) -> User:
    from backend.app.core.security import get_user_roles
    from backend.app.services.file_storage_service import FileStorageError
    actor = session.get(User, actor_id)
    if actor is None or not actor.is_active or UserRole.ADMIN not in get_user_roles(actor):
        raise FileStorageError("MAINTENANCE_FORBIDDEN", "维护工具需要当前启用的管理员账户。", http_status=403)
    return actor


def authenticate_admin(session: Session, token: str) -> User:
    from backend.app.services.auth_service import AuthService
    actor = AuthService(session).get_current_user(token)
    return require_admin_user(session, actor.id)


def ensure_storage_writable(root: Path) -> None:
    from backend.app.services.file_storage_service import FileStorageError
    if (root / MARKER).exists():
        raise FileStorageError("STORAGE_MAINTENANCE", "文件处于维护停写窗口，稍后再试。", http_status=503)


def durable_json(path: Path, value: str, *, create: bool = False) -> None:
    """An acknowledged manifest/receipt must have actually reached durable storage."""
    temporary = path if create else path.with_name(uuid4().hex + ".pending")
    with temporary.open("xb") as stream:
        stream.write(value.encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())
    if not create:
        os.replace(temporary, path)


def authorize_local_maintenance(
    security: SetupSecurityService, token: str, profile: EnvironmentProfile,
) -> ManagementSession:
    """Local management authority is bound to the exact current environment."""
    profile.validate_paths()
    return security.authorize(token, environment_id=profile.environment_id,
                              instance_id=security.instance_id)


class MaintenanceWindow:
    """Operator stops writers first; connection checks and locks enforce the window."""
    def __init__(self, engine: Engine, root: Path, *, actor_id: UUID | None, timeout: float = 10):
        self.engine = engine
        self.root = root.resolve()
        self.actor_id = actor_id
        self.timeout = max(0.1, min(timeout, 60))
        self.operation_id = uuid4()
        self.connection: Connection | None = None
        self.started_at: datetime | None = None
        self.marker_created = False
        self.keep_marker_on_exit = False
        self.management_session: ManagementSession | None = None
        self.purpose = "offline_backup"
        self.resume_operation_id: UUID | None = None
        self.session_lock_key: int | None = None
        self.session_lock_acquired = False

    @classmethod
    def for_local_management(
        cls, engine: Engine, *, profile: EnvironmentProfile,
        security: SetupSecurityService, token: str, timeout: float = 10,
    ) -> Self:
        session = authorize_local_maintenance(security, token, profile)
        # Keep the management principal distinct from a legacy User identity.
        window = cls(engine, environment_path(profile, "storage"),
                     actor_id=None, timeout=timeout)
        window.management_session = session
        return window

    def __enter__(self) -> Self:
        from backend.app.services.file_storage_service import FileStorageError
        if self.actor_id is None and self.management_session is None:
            raise FileStorageError("MAINTENANCE_FORBIDDEN", "维护工具需要管理授权。", http_status=403)
        if self.engine.dialect.name != "postgresql":
            raise FileStorageError("MAINTENANCE_DATABASE_UNSUPPORTED", "一致备份必须使用 PostgreSQL。")
        resuming = self.resume_operation_id is not None
        if resuming:
            marker = json.loads((self.root / MARKER).read_text(encoding="utf-8"))
            if (self.management_session is None
                    or marker.get("operation_id") != str(self.resume_operation_id)
                    or marker.get("purpose") != self.purpose
                    or marker.get("environment_id") != str(self.management_session.environment_id)):
                raise FileStorageError("MAINTENANCE_FORBIDDEN", "维护重入身份与原计划不一致。", http_status=403)
        else:
            ensure_storage_writable(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        principal = ({"management_session_id": str(self.management_session.session_id),
                      "environment_id": str(self.management_session.environment_id),
                      "principal_id": self.management_session.operator.principal_id,
                      "principal_kind": self.management_session.operator.kind.value}
                     if self.management_session is not None else {"actor_id": str(self.actor_id)})
        if not resuming:
            durable_json(self.root / MARKER, json.dumps({"operation_id": str(self.operation_id), **principal,
                         "purpose": self.purpose, "started_at": datetime.now(UTC).isoformat()}), create=True)
        self.marker_created = True
        try:
            self.engine.dispose()
            self.connection = self.engine.connect()
            if self.session_lock_key is not None:
                self.session_lock_acquired = bool(self.connection.scalar(text(
                    "SELECT pg_try_advisory_lock(:key)"), {"key": self.session_lock_key}))
                if not self.session_lock_acquired:
                    raise FileStorageError("MAINTENANCE_IN_PROGRESS", "同一维护操作正在执行，禁止并发重入。", http_status=409)
            deadline = time.monotonic() + self.timeout
            while self.other_connections():
                if time.monotonic() >= deadline:
                    raise FileStorageError("MAINTENANCE_NOT_DRAINED", "仍有数据库连接；请停止全部应用/工作进程及写入脚本后重试。")
                time.sleep(0.05)
            self.connection.execute(text("SELECT set_config('lock_timeout', :value, true)"), {"value": f"{int(self.timeout * 1000)}ms"})
            tables = self.connection.execute(text("""
                SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE c.relkind IN ('r','p') AND n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'
                ORDER BY n.nspname,c.relname
            """)).all()
            quote = self.connection.dialect.identifier_preparer.quote
            for schema, table in tables:
                self.connection.execute(text(f"LOCK TABLE {quote(schema)}.{quote(table)} IN SHARE MODE"))
            self.assert_quiet()
            self.started_at = datetime.now(UTC)
            return self
        except BaseException:
            self._release(keep_marker=self.management_session is not None)
            raise

    def other_connections(self) -> int:
        assert self.connection is not None
        # pg_stat_activity snapshots must be refreshed while draining.
        self.connection.execute(text("SELECT pg_stat_clear_snapshot()"))
        return int(self.connection.scalar(text("""
            SELECT count(*) FROM pg_stat_activity
            WHERE datname=current_database() AND pid<>pg_backend_pid() AND backend_type='client backend'
        """)) or 0)

    def assert_quiet(self) -> None:
        from backend.app.services.file_storage_service import FileStorageError
        if self.other_connections():
            raise FileStorageError("MAINTENANCE_NOT_DRAINED", "停写窗口内出现其他数据库连接，不能确认一致备份。")

    def _release(self, *, keep_marker: bool = False) -> None:
        if self.connection is not None:
            self.connection.rollback()
            if self.session_lock_acquired:
                try:
                    self.connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": self.session_lock_key})
                    self.connection.commit()
                except BaseException:
                    self.connection.invalidate()
                    raise
                finally:
                    self.session_lock_acquired = False
            # Offline maintenance owns this connection. Do not leave an idle
            # pooled client (or a session lock) behind for another operator.
            self.connection.invalidate()
            self.connection.close()
            self.connection = None
        if self.marker_created and not keep_marker:
            marker = self.root / MARKER
            # Never clear somebody else's marker or an unrecognizable replacement.
            if json.loads(marker.read_text(encoding="utf-8")).get("operation_id") != str(self.operation_id):
                raise RuntimeError("maintenance marker ownership changed; keep writers stopped")
            marker.unlink()
            self.marker_created = False

    def __exit__(self, exc_type, _exc, _traceback) -> None:
        self._release(keep_marker=self.keep_marker_on_exit or self.management_session is not None and exc_type is not None)
