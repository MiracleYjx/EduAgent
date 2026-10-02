"""Explicit historical migration. Does not remove any old originals."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Match the repository's existing directly executable scripts.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.core.database import get_session_factory
from backend.app.core.maintenance import authenticate_admin
from backend.app.services.auth_service import AuthenticationError
from backend.app.services.file_storage_service import FileStorageError
from backend.app.services.storage_migration_service import StorageMigrationService


def main() -> int:
    parser = argparse.ArgumentParser(description="复制并核对旧原稿，事务更新全部引用；保留旧副本。")
    parser.add_argument("--token-env", default="EDUAGENT_MAINTENANCE_TOKEN", help="现有管理员 JWT 所在环境变量名")
    parser.add_argument("--report", type=Path, required=True, help="私有迁移报告的新文件路径")
    args = parser.parse_args()
    token = os.environ.get(args.token_env)
    if not token:
        parser.error("指定环境变量中没有管理员 JWT")
    if args.report.exists():
        parser.error("报告路径已存在，不能覆盖")
    try:
        with get_session_factory()() as session:
            actor = authenticate_admin(session, token)
            report = StorageMigrationService(session).run(actor_id=actor.id)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open("x", encoding="utf-8") as stream:
            stream.write(report.model_dump_json(indent=2))
        print(report.counts)
        return 1 if any(report.counts[key] for key in ("failed", "missing", "history_unknown")) else 0
    except (AuthenticationError, FileStorageError, OSError) as exc:
        print(str(exc) if isinstance(exc, (AuthenticationError, FileStorageError)) else type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
