"""Offline backup / isolated restore; never changes .env or activates a target."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.core.config import get_settings
from backend.app.core.database import get_engine, get_session_factory
from backend.app.core.maintenance import authenticate_admin
from backend.app.services.auth_service import AuthenticationError
from backend.app.services.backup_restore_service import BackupRestoreService
from backend.app.services.file_storage_service import FileStorageError
from backend.app.services.postgres_backup import PostgresTools


def main() -> int:
    parser = argparse.ArgumentParser(description="离线维护备份；恢复仅创建隔离目标，生成报告后人工授权切换。")
    parser.add_argument("--token-env", default="EDUAGENT_MAINTENANCE_TOKEN")
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
    args = parser.parse_args()
    token = os.environ.get(args.token_env)
    if not token:
        parser.error("指定环境变量中没有管理员 JWT")
    try:
        with get_session_factory()() as session:
            actor_id = authenticate_admin(session, token).id
        engine = get_engine()
        service = BackupRestoreService(engine, root=get_settings().storage_root,
                    tools=PostgresTools(engine.url, client_dir=args.pg_client_dir,
                                        container=args.pg_container, container_port=args.container_port))
        if args.action == "backup":
            destination, manifest = service.backup(backup_root=args.backup_root, actor_id=actor_id,
                                                    writers_stopped=args.writers_stopped, timeout=args.timeout)
            print(f"{manifest.outcome}: {destination}")
            return 0 if manifest.outcome == "complete" else 1
        report = service.restore(backup_set=args.backup_set, target_database=args.target_database,
                                  target_root=args.target_root, report_path=args.report, actor_id=actor_id)
        print(f"{report.outcome}: {args.report}; 隔离目标仍停写，未切换配置。")
        return 0 if report.outcome == "verified" else 1
    except (AuthenticationError, FileStorageError, OSError) as exc:
        print(str(exc) if isinstance(exc, (AuthenticationError, FileStorageError)) else type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
