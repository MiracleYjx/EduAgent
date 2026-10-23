"""Offline backup / isolated restore; never changes .env or activates a target."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from uuid import UUID

from sqlalchemy import create_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.services.backup_restore_service import BackupRestoreService
from backend.app.services.file_storage_service import FileStorageError
from backend.app.services.postgres_backup import PostgresTools


def local_maintenance(args) -> int:
    """Offline OS/container operator, using the same in-process Setup authority."""
    from backend.app.core.bootstrap import (
        build_bootstrap_security,
        resolve_bootstrap_context,
    )
    from backend.app.core.environment import resolve_environment_profile
    from backend.app.deployment.local_management import LocalManagementAuthority
    from backend.app.schemas.settings import EnvironmentProfile
    from backend.app.services.settings.management import build_settings_runtime
    from backend.app.services.settings.runtime_snapshot import RuntimeSnapshotService

    profile = resolve_environment_profile()
    security = build_bootstrap_security(resolve_bootstrap_context())
    issued = LocalManagementAuthority(security).request_one_time_code()
    token = security.exchange(issued.code).token
    runtime = build_settings_runtime(profile)
    target_runtime = None
    engine = None
    try:
        runtime.credential_store()
        url, _ = runtime.personal_learning_database()
        engine = create_engine(url)
        if args.action.startswith("cutover-"):
            from backend.app.schemas.settings import DatabaseIdentity
            from backend.app.services.personal_cutover_service import (
                PersonalCutoverService,
            )

            approval = json.loads(args.approved_target.read_bytes())
            cutover = PersonalCutoverService.for_local_management(engine, profile=profile,
                security=security, token=token,
                approved_database=DatabaseIdentity.model_validate(approval["database_identity"]),
                approved_root=Path(approval["file_root"]))
            result = (cutover.prepare(writers_stopped=args.writers_stopped,
                resume_operation_id=args.resume_operation_id) if args.action == "cutover-prepare"
                else cutover.execute(args.operation_id, args.digest, writers_stopped=args.writers_stopped))
            print(json.dumps(result, ensure_ascii=False))
            return 0
        service = BackupRestoreService.for_local_management(engine, profile=profile,
            security=security, token=token,
            tools=PostgresTools(engine.url, client_dir=args.pg_client_dir,
                container=args.pg_container, container_port=args.container_port),
            snapshots=RuntimeSnapshotService(runtime._require_config()))
        if args.action == "backup":
            destination, manifest = service.backup(backup_root=args.backup_root,
                writers_stopped=args.writers_stopped, timeout=args.timeout)
            print(f"{manifest.outcome}: {destination}")
            return 0 if manifest.outcome == "complete" else 1
        if args.target_profile is None:
            raise FileStorageError("RESTORE_SETTINGS_PROFILE_MISMATCH", "个人恢复需要 --target-profile 指定同空间的隔离目标配置。")
        target = EnvironmentProfile.model_validate_json(args.target_profile.read_bytes())
        if target.storage_root != args.target_root.resolve() or target.environment_id != profile.environment_id:
            raise FileStorageError("RESTORE_SETTINGS_PROFILE_MISMATCH", "恢复目标的空间与文件根不一致。")
        target_runtime = build_settings_runtime(target)
        target_runtime.credential_store()
        report = service.restore(backup_set=args.backup_set, target_database=args.target_database,
            target_root=args.target_root, report_path=args.report, target_profile=target,
            target_snapshots=RuntimeSnapshotService(target_runtime._require_config()))
        print(f"{report.outcome}: {args.report}; 隔离目标仍停写，未切换配置。")
        return 0 if report.outcome == "verified" else 1
    finally:
        if engine is not None:
            engine.dispose()
        if target_runtime is not None:
            asyncio.run(target_runtime.aclose())
        asyncio.run(runtime.aclose())


def main() -> int:
    parser = argparse.ArgumentParser(description="离线维护备份；恢复仅创建隔离目标，生成报告后人工授权切换。")
    parser.add_argument("--local-management", action="store_true",
        help="由启动本应用的OS用户/容器操作者离线维护，复用Setup管理授权；不使用业务JWT")
    clients = parser.add_mutually_exclusive_group()
    clients.add_argument("--pg-client-dir", type=Path, help="现有 pg_dump/pg_restore/psql 所在目录")
    clients.add_argument("--pg-container", help="显式现有 PostgreSQL 容器；校验实际实例，不自动猜测")
    parser.add_argument("--container-port", type=int, default=5432)
    subparsers = parser.add_subparsers(dest="action", required=True)
    backup = subparsers.add_parser("backup")
    backup.add_argument("--backup-root", type=Path, required=True)
    backup.add_argument("--writers-stopped", action="store_true", help="已经停止全部后端、工作进程和写入脚本")
    backup.add_argument("--timeout", type=float, default=10)
    restore = subparsers.add_parser("restore")
    restore.add_argument("--backup-set", type=Path, required=True)
    restore.add_argument("--target-database", required=True)
    restore.add_argument("--target-root", type=Path, required=True)
    restore.add_argument("--report", type=Path, required=True)
    restore.add_argument("--target-profile", type=Path, help="个人恢复的已核验隔离目标EnvironmentProfile JSON；不含凭据正文")
    for action in ("cutover-prepare", "cutover-execute"):
        cutover = subparsers.add_parser(action, help="仅对已逐对象批准的独立Test执行退役；普通迁移不触发")
        cutover.add_argument("--approved-target", type=Path, required=True,
            help="已批准的精确database_identity（含Schema）和file_root；不含连接密码")
        cutover.add_argument("--writers-stopped", action="store_true")
        if action == "cutover-prepare":
            cutover.add_argument("--resume-operation-id", type=UUID)
        else:
            cutover.add_argument("--operation-id", type=UUID, required=True)
            cutover.add_argument("--digest", required=True)
    args = parser.parse_args()
    if args.action.startswith("cutover-") and not args.local_management:
        parser.error("退役仅允许 --local-management 本地管理授权，不使用旧业务 JWT")
    if args.local_management:
        from backend.app.core.settings_security import SettingsSecurityError
        from backend.app.services.settings.management import SettingsManagementError
        try:
            return local_maintenance(args)
        except (FileStorageError, SettingsSecurityError, SettingsManagementError, OSError, ValueError) as exc:
            print(getattr(exc, "code", "MAINTENANCE_FAILED"), file=sys.stderr)
            return 1
    parser.error("Maintenance requires --local-management; business JWT is retired.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
