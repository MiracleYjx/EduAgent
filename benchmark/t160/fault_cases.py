"""T160 fixed pipeline faults; injected OCR error is not quality evidence."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent


def sha(data):
    return hashlib.sha256(data).hexdigest()


def utc():
    return datetime.now(UTC).isoformat()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


async def execute(args, cases):
    from dotenv import dotenv_values
    from sqlalchemy import func, select
    from sqlalchemy.engine import make_url

    import backend.app.services.paper_import_service as module
    from backend.app.ai.ingestion.ocr.base import BaseOCRProvider, OCRProviderError
    from backend.app.ai.ingestion.ocr.schemas import OCRProviderInfo
    from backend.app.ai.ingestion.paper_pipeline import PaperInputError
    from backend.app.core.config import get_settings
    from backend.app.core.database import create_database_engine, create_session_factory
    from backend.app.domain.enums import UserRole
    from backend.app.models import Course, Document, PaperImport, Role, SourcePage, User
    from backend.app.services.file_storage_service import FileStorageService
    from backend.app.services.paper_import_service import (
        PaperImportRunner,
        PaperImportService,
    )

    os.environ.update(
        {
            k: v
            for k, v in dotenv_values(args.repo_root / ".env").items()
            if v is not None
        }
    )
    settings = get_settings()
    isolation = json.loads(args.isolation_file.read_text(encoding="utf-8"))
    assert re.fullmatch(
        r"eduagent_(e2_acceptance|t160_performance)_[0-9a-f]{12}", isolation["database"]
    )
    settings = settings.model_copy(
        update={
            "database_url": make_url(str(settings.database_url))
            .set(database=isolation["database"])
            .render_as_string(hide_password=False),
            "storage_root": args.run_root / "business-files",
        }
    )
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)
    report = {
        "kind": "pipeline_business_acceptance",
        "not_quality_truth": True,
        "teacher_annotation": False,
        "source_commit": args.source_commit,
        "started_at": utc(),
        "cases": [],
        "provider_boundary_entries": 0,
        "http_requests": 0,
        "cloud_guard": "Runner LLM factory guarded: unexpected entry is recorded and raises before any SDK/client creation",
        "passed": False,
    }
    original = module.create_llm_provider

    def forbid_cloud(*_a, **_kw):
        report["provider_boundary_entries"] += 1
        raise RuntimeError(
            "fault acceptance unexpectedly reached LLM boundary; no cloud SDK was created"
        )

    module.create_llm_provider = forbid_cloud

    class InjectedOCRFailure(BaseOCRProvider):
        async def extract_text(self, image_path):
            assert image_path.is_file()
            injected.append(
                {
                    "stage": "OCR",
                    "code": "OCR_CALL_FAILED",
                    "page_bytes": image_path.stat().st_size,
                }
            )
            raise OCRProviderError(
                "OCR_CALL_FAILED",
                "Explicit T160 pipeline acceptance fault injection; not a model-quality result.",
            )

        def describe(self):
            return OCRProviderInfo(
                provider="explicit_acceptance_fault",
                model=None,
                ready=True,
                reason=None,
            )

    output_created = False
    try:
        args.output_root.mkdir(parents=True, exist_ok=False)
        output_created = True
        with factory() as session:
            role = session.scalar(select(Role).where(Role.name == UserRole.TEACHER))
            if role is None:
                role = Role(name=UserRole.TEACHER)
                session.add(role)
            token = uuid4().hex[:12]
            teacher = User(
                username="fault_fixture_" + token,
                email=token + "@fixture.invalid",
                password_hash="synthetic-no-password",
                is_active=True,
            )
            teacher.roles.append(role)
            session.add(teacher)
            session.flush()
            course = Course(
                name="T160 fixed fault acceptance " + token, created_by=teacher.id
            )
            session.add(course)
            session.commit()
            actor, course_id = teacher.id, course.id
            report["actor_fixture"] = {
                "id": str(actor),
                "roles": [r.name.value for r in teacher.roles],
                "is_active": teacher.is_active,
                "course_id": str(course_id),
                "human_teacher_annotation": False,
            }
        for entry in cases:
            injected = []
            row = {
                **entry,
                "started_at": utc(),
                "steps": [],
                "bytechecks": [],
                "injected_errors": injected,
            }
            raw = (
                args.source_root
                / "benchmark/corpus/v2-draft-20261001/inputs"
                / entry["filename"]
            ).read_bytes()
            with factory() as session:
                counts = lambda: [
                    session.scalar(
                        select(func.count())
                        .select_from(cls)
                        .where(cls.course_id == course_id)
                    )
                    for cls in (PaperImport, Document)
                ]
                before = counts()
                service = PaperImportService(session, root=settings.storage_root)
                try:
                    imported = service.upload(
                        course_id,
                        filename=entry["filename"],
                        content=raw,
                        actor_id=actor,
                    )
                    row["steps"].append(
                        {"stage": "upload", "status": imported.status.value}
                    )
                    row["paper_import_id"] = str(imported.id)
                except PaperInputError as exc:
                    row.update(
                        status="Upload Rejected",
                        error_code=exc.code,
                        error_message=str(exc),
                        records_created=[a - b for a, b in zip(counts(), before)],
                    )
                    row["steps"].append(
                        {
                            "stage": "format_validation",
                            "status": "rejected",
                            "error_code": exc.code,
                        }
                    )
                    imported = None
            if imported is not None:
                configured = settings.model_copy(
                    update={"ocr_enabled": entry["case"] != "IMP-OCR-OFF"}
                )
                runner = PaperImportRunner(
                    factory,
                    settings=configured,
                    ocr=(
                        InjectedOCRFailure()
                        if entry["case"] == "IMP-OCR-FAIL"
                        else None
                    ),
                )
                await runner._run(imported.id)
                with factory() as session:
                    result = PaperImportService(
                        session, root=settings.storage_root
                    ).get(imported.id, actor_id=actor)
                    row.update(
                        status=result.status.value,
                        error_code=result.error_code,
                        error_message=result.error_message,
                        page_count=result.page_count,
                        saved_page_count=len(result.pages),
                        question_count=len(result.questions),
                    )
                    row["steps"].append(
                        {
                            "stage": "runner_terminal",
                            "status": result.status.value,
                            "error_code": result.error_code,
                        }
                    )
                    files = FileStorageService(session, root=settings.storage_root)
                    for fid in [
                        result.original_file_id,
                        *[p.file_id for p in result.pages],
                    ]:
                        path, _ = files.download(fid, actor_id=actor)
                        actual = path.read_bytes()
                        actual_sha = sha(actual)
                        expected = (
                            entry["sha256"]
                            if fid == result.original_file_id
                            else session.get(
                                SourcePage,
                                next(p.id for p in result.pages if p.file_id == fid),
                            ).file_metadata["sha256"]
                        )
                        row["bytechecks"].append(
                            {
                                "file_id": fid,
                                "bytes": len(actual),
                                "sha256": actual_sha,
                                "matches_registered_or_input_sha256": actual_sha
                                == expected,
                            }
                        )
            row["passed"] = (
                row["status"] == entry["expected_status"]
                and row["error_code"] == entry["expected_error"]
                and (
                    row.get("records_created") == [0, 0]
                    if imported is None
                    else row["saved_page_count"] > 0
                    and all(
                        r["matches_registered_or_input_sha256"]
                        for r in row["bytechecks"]
                    )
                )
            )
            row["passed"] = row["passed"] and (
                entry["fault_injection"] is None or bool(injected)
            )
            row["completed_at"] = utc()
            report["cases"].append(row)
            write(args.output_root / (entry["case"] + ".json"), row)
        report["passed"] = (
            all(r["passed"] for r in report["cases"])
            and report["provider_boundary_entries"] == 0
        )
    finally:
        module.create_llm_provider = original
        engine.dispose()
        report["completed_at"] = utc()
        if output_created:
            write(args.output_root / "summary.json", report)
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "cases": len(report["cases"]),
                "provider_boundary_entries": report["provider_boundary_entries"],
                "cloud_requests": report["http_requests"],
            }
        ),
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser()
    for flag, default in (
        ("repo-root", REPO),
        (
            "source-root",
            HERE / "source-head" if (HERE / "source-head").exists() else REPO,
        ),
        ("run-root", REPO / ".cache/t160-faults"),
        ("isolation-file", None),
        (
            "output-root",
            REPO / "benchmark/results/v2/t160-replay/technical/fault-cases",
        ),
    ):
        parser.add_argument("--" + flag, type=Path, default=default)
    parser.add_argument("--source-commit", default="unrecorded")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--plan-only", action="store_true")
    group.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.isolation_file is None:
        args.isolation_file = args.run_root / "isolation.json"
    for name in (
        "repo_root",
        "source_root",
        "run_root",
        "isolation_file",
        "output_root",
    ):
        setattr(args, name, getattr(args, name).resolve())
    assert args.run_root.is_relative_to(
        args.repo_root / ".cache"
    ) and args.isolation_file.is_relative_to(args.repo_root / ".cache")
    assert (
        args.source_root.is_relative_to(args.repo_root)
        and args.output_root.is_relative_to(args.repo_root / "benchmark/results")
        and args.output_root.name == "fault-cases"
    )
    sys.path.insert(0, str(args.source_root))
    cases = []
    for case, filename, status, error, injection in (
        ("IMP-DAMAGED", "damaged.pdf", "Upload Rejected", "PAPER_PARSE_FAILED", None),
        (
            "IMP-OVERLIMIT",
            "workload_51_pages.pdf",
            "Upload Rejected",
            "PAPER_TOO_MANY_PAGES",
            None,
        ),
        ("IMP-OCR-OFF", "paper_scan.pdf", "Failed", "OCR_PROVIDER_NOT_READY", None),
        (
            "IMP-OCR-FAIL",
            "paper_scan.pdf",
            "Failed",
            "PAPER_OCR_FAILED",
            "OCR provider raises OCR_CALL_FAILED explicitly",
        ),
    ):
        raw = (
            args.source_root / "benchmark/corpus/v2-draft-20261001/inputs" / filename
        ).read_bytes()
        cases.append(
            {
                "case": case,
                "filename": filename,
                "bytes": len(raw),
                "sha256": sha(raw),
                "expected_status": status,
                "expected_error": error,
                "fault_injection": injection,
            }
        )
    if args.plan_only:
        write(
            args.run_root / "fault-plan.json",
            {
                "kind": "pipeline_business_acceptance_plan",
                "cloud_requests": 0,
                "database_writes": 0,
                "teacher_annotation": False,
                "cases": cases,
            },
        )
        print(
            json.dumps(
                {
                    "plan_only": True,
                    "cases": len(cases),
                    "cloud_requests": 0,
                    "database_writes": 0,
                }
            ),
            flush=True,
        )
    else:
        asyncio.run(execute(args, cases))


if __name__ == "__main__":
    main()
