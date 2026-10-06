"""Actual offline CLI backup and isolated restore of an owned synthetic course."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from uuid import UUID

from dotenv import dotenv_values
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from backend.app.core.maintenance import MARKER
from backend.app.services.auth_service import AuthService
from backend.app.services.file_storage_service import (
    FileStorageError,
    FileStorageService,
)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--tokens", type=Path, required=True)
    p.add_argument("--prior", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--backup-root", type=Path, required=True)
    p.add_argument("--target-root", type=Path, required=True)
    p.add_argument("--target-database", required=True)
    p.add_argument("--pg-container", required=True)
    p.add_argument("--verify-existing-storage", type=Path)
    a = p.parse_args()
    root = Path.cwd()
    out = a.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    v = dotenv_values(a.config)
    env = os.environ.copy()
    env.update({k: value for k, value in v.items() if value is not None})
    env.update(PYTHONIOENCODING="utf-8", PYTHONPATH=str(root))
    tokens = json.loads(a.tokens.read_text("utf8"))
    prior = json.loads(a.prior.read_text("utf8"))
    env["EDUAGENT_MAINTENANCE_TOKEN"] = tokens["Admin"]
    source = create_engine(v["DATABASE_URL"])
    facts = {
        "provenance": "synthetic course and answers, AI session delegated; no independent teacher",
        "activated": False,
        "commands": [],
    }

    def save():
        (out / "receipt.json").write_text(
            json.dumps(facts, ensure_ascii=False, indent=2), "utf8"
        )

    def snapshot(engine):
        result = {}
        with engine.connect() as c:
            for name in inspect(c).get_table_names(schema="public"):
                quoted = c.dialect.identifier_preparer.quote(name)
                rows = sorted(
                    c.execute(
                        text("SELECT row_to_json(t)::text FROM public." + quoted + " t")
                    )
                    .scalars()
                    .all()
                )
                result[name] = {
                    "rows": len(rows),
                    "sha256": hashlib.sha256(
                        json.dumps(rows, ensure_ascii=False).encode()
                    ).hexdigest(),
                }
        return result

    def run(args, label):
        r = subprocess.run(
            [
                os.sys.executable,
                "scripts/backup_restore.py",
                "--pg-container",
                a.pg_container,
            ]
            + args,
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf8",
            errors="replace",
            timeout=240,
            check=False,
        )
        safe = r.stdout + "\n" + r.stderr
        for k, value in env.items():
            if value and any(x in k for x in ("KEY", "TOKEN", "PASSWORD", "SECRET")):
                safe = safe.replace(value, "[REDACTED]")
        (out / (label + ".log")).write_text(safe, "utf8")
        facts["commands"].append({"label": label, "returncode": r.returncode})
        save()
        print(label, r.returncode, flush=True)
        if r.returncode != 0:
            raise RuntimeError(label + " failed; original evidence retained")

    target = None
    try:
        with Session(source) as session:
            admin = AuthService(
                session, secret_key=v["JWT_SECRET_KEY"]
            ).get_current_user(tokens["Admin"])
            assert admin.is_active and any(str(r.name) == "Admin" for r in admin.roles)
        source.dispose()
        facts["source_tables"] = snapshot(source)
        source.dispose()
        save()
        if a.verify_existing_storage:
            old = json.loads(
                (a.verify_existing_storage / "receipt.json").read_text("utf8")
            )
            manifest = json.loads(
                (a.verify_existing_storage / "manifest.json").read_text("utf8")
            )
            report = json.loads(
                (a.verify_existing_storage / "restore-report.json").read_text("utf8")
            )
            assert (
                old["source_tables"] == facts["source_tables"]
                and manifest["outcome"] == "complete"
                and report["outcome"] == "verified"
            )
            facts["prior_storage_receipt"] = str(a.verify_existing_storage.resolve())
            facts["commands"] = old["commands"]
            facts["backup_set_path"] = old["backup_set_path"]
            facts["backup_outcome"] = manifest["outcome"]
            facts["file_count"] = len(manifest["files"])
            facts["reference_counts"] = dict(
                Counter(r["resource_type"] for r in manifest["references"])
            )
            shutil.copy2(
                a.verify_existing_storage / "manifest.json", out / "manifest.json"
            )
            shutil.copy2(
                a.verify_existing_storage / "restore-report.json",
                out / "restore-report.json",
            )
        else:
            run(
                [
                    "backup",
                    "--backup-root",
                    str(a.backup_root.resolve()),
                    "--writers-stopped",
                    "--timeout",
                    "10",
                ],
                "actual_backup_cli",
            )
            sets = list(a.backup_root.resolve().glob("backup_set_*/manifest.json"))
            assert len(sets) == 1, "Fresh backup root must have exactly one set"
            manifest = json.loads(sets[0].read_text("utf8"))
            shutil.copy2(sets[0], out / "manifest.json")
            facts["backup_outcome"] = manifest["outcome"]
            facts["file_count"] = len(manifest["files"])
            facts["reference_counts"] = dict(
                Counter(r["resource_type"] for r in manifest["references"])
            )
            facts["backup_set_path"] = str(sets[0].parent)
            save()
            run(
                [
                    "restore",
                    "--backup-set",
                    str(sets[0].parent),
                    "--target-database",
                    a.target_database,
                    "--target-root",
                    str(a.target_root.resolve()),
                    "--report",
                    str(out / "restore-report.json"),
                ],
                "actual_restore_cli",
            )
        target = create_engine(source.url.set(database=a.target_database))
        facts["target_tables"] = snapshot(target)
        facts["all_table_rows_identical"] = (
            facts["source_tables"] == facts["target_tables"]
        )
        assert facts["all_table_rows_identical"]
        checks = []
        for f in manifest["files"]:
            file = a.target_root.resolve() / f["relative_path"]
            checks.append(
                {
                    "relative_path": f["relative_path"],
                    "size_matches": file.stat().st_size == f["size_bytes"],
                    "sha256_matches": hashlib.sha256(file.read_bytes()).hexdigest()
                    == f["sha256"],
                }
            )
        assert all(f["size_matches"] and f["sha256_matches"] for f in checks)
        facts["restored_file_checks"] = checks
        with Session(target) as session:
            actor = AuthService(
                session, secret_key=v["JWT_SECRET_KEY"]
            ).get_current_user(tokens["Teacher"])
            service = FileStorageService(session, root=a.target_root.resolve())
            reads = []
            for ref in manifest["references"]:
                file, view = service.download(ref["file_id"], actor_id=actor.id)
                reads.append(
                    {
                        "file_id": ref["file_id"],
                        "availability": view.availability,
                        "bytes": file.stat().st_size,
                    }
                )
            facts["restored_teacher_authorized_reads"] = reads
            student_id = UUID(prior["synthetic_student"]["id"])
            picture_reads = []
            page_denied = []
            for ref in manifest["references"]:
                if ref["resource_type"] == "source_page":
                    try:
                        service.download(ref["file_id"], actor_id=student_id)
                    except FileStorageError as exc:
                        page_denied.append(
                            {
                                "file_id": ref["file_id"],
                                "http_status": exc.http_status,
                                "code": exc.code,
                            }
                        )
                    else:
                        raise RuntimeError(
                            "Restored private page became student visible"
                        )
                if (
                    ref["resource_type"] == "question_asset"
                    and ref["owner"].get("question_id")
                    in prior["actual_student_answers"]
                ):
                    file, view = service.download(ref["file_id"], actor_id=student_id)
                    picture_reads.append(
                        {"file_id": ref["file_id"], "bytes": file.stat().st_size}
                    )
            assert picture_reads and page_denied
            facts["student_restored_pictures"] = picture_reads
            facts["source_pages_still_private"] = page_denied
        with target.connect() as c:
            sid = prior["submission_id"]
            result = json.loads(
                c.execute(
                    text(
                        "SELECT row_to_json(t)::text FROM exam_results t WHERE submission_id=:sid"
                    ),
                    {"sid": sid},
                ).scalar_one()
            )
            assert result["is_final"] and float(result["final_total_score"]) == 2
            facts["restored_result"] = {
                k: result[k]
                for k in [
                    "submission_id",
                    "exam_id",
                    "is_final",
                    "final_total_score",
                    "total_max_score",
                ]
            }
            exams = (
                c.execute(
                    text(
                        "SELECT e.id,e.status,sum(eq.score) AS total_score FROM exams e JOIN exam_questions eq ON eq.exam_id=e.id WHERE e.id IN (:first,:second) GROUP BY e.id,e.status"
                    ),
                    {"first": prior["exam_id"], "second": prior["second_exam_id"]},
                )
                .mappings()
                .all()
            )
            facts["restored_exam_totals"] = [
                {k: str(value) for k, value in row.items()} for row in exams
            ]
            assert sorted(float(row["total_score"]) for row in exams) == [5, 10]
            facts["restored_option_orders"] = {
                str(row[0]): (
                    list(json.loads(row[1]).keys())
                    if isinstance(json.loads(row[1]), dict)
                    else json.loads(row[1])
                )
                for row in c.execute(
                    text(
                        "SELECT id,options::text FROM questions WHERE options IS NOT NULL"
                    )
                )
            }
            assert ["C", "A", "B", "D"] in facts["restored_option_orders"].values()
        facts["target_remains_maintenance_blocked"] = (
            a.target_root.resolve() / MARKER
        ).is_file()
        assert facts["target_remains_maintenance_blocked"]
        facts["status"] = "verified"
        save()
        print(
            "all tables, owned bytes, permissions and actual 2/10 restored; no activation",
            flush=True,
        )
    except Exception as exc:
        facts.update(status="failed", failure_type=type(exc).__name__, failure=str(exc))
        save()
        raise
    finally:
        source.dispose()
        if target is not None:
            target.dispose()


if __name__ == "__main__":
    main()
