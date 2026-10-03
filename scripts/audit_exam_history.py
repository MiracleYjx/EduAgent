"""Read historical exam facts; explicitly reconcile only complete reviewed evidence."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from backend.app.core.database import get_session_factory
from backend.app.core.maintenance import authenticate_admin, durable_json
from backend.app.services.auth_service import AuthenticationError
from backend.app.services.exam_history_service import (
    EXAM_HISTORY_COMMIT_UNKNOWN,
    ExamHistoryError,
    ExamHistoryManifest,
    ExamHistoryService,
)
from backend.app.services.file_storage_service import FileStorageError


def main() -> int:
    parser = argparse.ArgumentParser(
        description="历史考试只读盘点；--apply 显式核对完整历史依据，不迁移业务库结构、不重评。"
    )
    parser.add_argument(
        "--token-env",
        default="EDUAGENT_MAINTENANCE_TOKEN",
        help="现有管理员 JWT 环境变量名",
    )
    parser.add_argument(
        "--report", type=Path, required=True, help="新的私有报告文件；已有文件拒绝覆盖"
    )
    parser.add_argument(
        "--exam-id", type=UUID, action="append", help="只读审计目标考试，可重复指定"
    )
    parser.add_argument(
        "--apply",
        type=Path,
        metavar="MANIFEST",
        help="显式应用完整证据清单；需管理员兼课程教师",
    )
    args = parser.parse_args()
    if args.apply is not None and args.exam_id:
        parser.error("--apply 使用清单中的完整考试集合，不能同时传 --exam-id")
    token = os.environ.get(args.token_env)
    if not token:
        parser.error("指定环境变量中没有管理员 JWT")
    if args.report.exists():
        parser.error("报告路径已存在，不能覆盖")
    report_path = args.report.resolve()
    operation_id = uuid4()
    pending = {
        "operation_id": str(operation_id),
        "operation": "apply" if args.apply else "audit",
        "status": "started",
        "started_at": datetime.now(UTC).isoformat(),
    }
    reserved = False
    database_committed: bool | None = False
    try:
        with get_session_factory()() as session:
            actor = authenticate_admin(session, token)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            durable_json(
                report_path,
                json.dumps(pending, ensure_ascii=False, indent=2),
                create=True,
            )
            reserved = True
            service = ExamHistoryService(session)
            if args.apply:
                source = args.apply.resolve()
                manifest = ExamHistoryManifest.model_validate_json(
                    source.read_text(encoding="utf-8")
                )
                # Relative evidence locations belong to the manifest, never to the shell cwd.
                for evidence in manifest.evidence_files:
                    evidence.path = (source.parent / evidence.path).resolve()
                report = service.apply(actor_id=actor.id, manifest=manifest)
                database_committed = True
            else:
                report = service.audit(actor_id=actor.id, exam_ids=args.exam_id)
            report.operation_id = operation_id
            durable_json(report_path, report.model_dump_json(indent=2))
            print(
                json.dumps(
                    {
                        "operation": report.operation,
                        "exam_count": len(report.exams),
                        "status_counts": {
                            status: sum(row.status == status for row in report.exams)
                            for status in sorted({row.status for row in report.exams})
                        },
                    },
                    ensure_ascii=False,
                )
            )
            return (
                0
                if all(
                    row.status not in {"history_unknown", "needs_review"}
                    for row in report.exams
                )
                else 1
            )
    except (
        AuthenticationError,
        FileStorageError,
        ExamHistoryError,
        ValidationError,
        SQLAlchemyError,
        OSError,
        RuntimeError,
    ) as error:
        if EXAM_HISTORY_COMMIT_UNKNOWN in getattr(error, "__notes__", ()):
            database_committed = None
            code = EXAM_HISTORY_COMMIT_UNKNOWN
            detail = "数据库 COMMIT 已开始但未收到可靠回执，结果未知；请使用新报告路径重新执行只读审计后再决定是否重试。"
        elif isinstance(
            error, (ExamHistoryError, AuthenticationError, FileStorageError)
        ):
            code = getattr(
                error, "code", getattr(error, "error_code", type(error).__name__)
            )
            detail = str(error)
        else:
            code, detail = (
                type(error).__name__,
                "核对未完成；检查输入和私有报告，不能据此声明回填成功。",
            )
        failure = {
            **pending,
            "status": (
                "commit_outcome_unknown"
                if database_committed is None
                else "report_failed_after_commit" if database_committed else "failed"
            ),
            "database_committed": database_committed,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": {"code": code, "message": detail},
        }
        if reserved:
            try:
                durable_json(
                    report_path, json.dumps(failure, ensure_ascii=False, indent=2)
                )
            except OSError:
                pass  # The original durable started receipt remains for explicit inspection.
        print(json.dumps(failure, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
