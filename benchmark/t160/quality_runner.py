"""T160 dedicated fixed real-provider acceptance runner; reference is AI-assisted."""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from uuid import UUID, uuid4

parser = argparse.ArgumentParser()
mode = parser.add_mutually_exclusive_group(required=True)
mode.add_argument("--plan-only", action="store_true")
mode.add_argument("--execute-confirmed", action="store_true")
for name in (
    "run-root",
    "repo-root",
    "source-root",
    "annotations",
    "input-root",
    "ocr-model-dir",
):
    parser.add_argument("--" + name, type=Path)
args = parser.parse_args()
RUN = (args.run_root or Path(__file__).resolve().parent).resolve()
REPO = (args.repo_root or RUN.parents[1]).resolve()
SOURCE = (args.source_root or REPO).resolve()
ANNOTATIONS = (
    args.annotations
    or REPO / "benchmark/corpus/t160-assisted-20261003/annotations.draft.json"
).resolve()
INPUTS = (args.input_root or REPO / "benchmark/corpus/v2-draft-20261001").resolve()
sys.path[:0] = [str(SOURCE), str(RUN)]
sys.path.append(str(SOURCE / "benchmark/t160"))
os.chdir(REPO)
from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.engine import make_url

from backend.app.ai.ingestion.ocr.factory import create_ocr_provider
from backend.app.ai.ingestion.ocr.rapidocr import _model_paths
from backend.app.ai.llm.factory import create_llm_provider
from backend.app.ai.paper_extraction import PaperExtractor
from backend.app.ai.paper_extraction.service import PROMPT_VERSION
from backend.app.core.config import get_settings
from backend.app.core.database import create_database_engine, create_session_factory
from backend.app.domain.enums import PaperImportStatus, QuestionType, UserRole
from backend.app.models import AgentRun, Course, PaperImport, Role, User
from backend.app.schemas.paper_import import CorrectionPayload
from backend.app.services.paper_import_service import (
    PaperImportRunner,
    PaperImportService,
)
from backend.app.services.question_correction_service import QuestionCorrectionService
from backend.app.services.trace_service import (
    TraceService,
    bind_trace,
    trace_prompt_version,
)

CASES = ("IMP-TEXT", "IMP-SCAN", "IMP-IMAGE", "IMP-MIXED", "IMP-CROSS")
FIELDS = (
    "question_number",
    "order_index",
    "source_page_numbers",
    "question_type",
    "content",
    "options",
    "reference_answer",
    "analysis",
    "score",
    "scoring_rubric",
    "knowledge_points",
    "source_regions",
    "assets",
)


def now():
    return datetime.now(UTC).isoformat()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def default(value):
    if isinstance(value, (UUID, Path, Decimal)):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(type(value).__name__)


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=default),
        encoding="utf-8",
    )


def error(exc):
    return {
        "type": type(exc).__name__,
        "code": getattr(exc, "code", None),
        "message": str(exc) if type(exc).__module__.startswith("backend.") else None,
        "cause_type": type(exc.__cause__).__name__ if exc.__cause__ else None,
    }


def normalized(value):
    if isinstance(value, str):
        return value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if isinstance(value, list):
        return [normalized(x) for x in value]
    if isinstance(value, dict):
        return [(normalized(k), normalized(v)) for k, v in value.items()]
    return value


def equal(field, expected, actual):
    if field == "score" and expected is not None and actual is not None:
        try:
            return Decimal(str(expected)) == Decimal(str(actual))
        except (ValueError, ArithmeticError):
            return False
    if field == "knowledge_points" and expected is not None and actual is not None:
        return {normalized(x) for x in expected} == {normalized(x) for x in actual}
    return normalized(expected) == normalized(actual)


def expected(label, field):
    if field == "question_type" and label[field] is not None:
        return QuestionType[label[field]].value
    if field == "assets":
        return [
            {
                "asset_type": a["kind"],
                "source_page_number": a["source_page_number"],
                "order_index": a["order_index"],
            }
            for a in label["assets"]
        ]
    return label[field]


def comparison(labels, paper, run_id, case, repeat, stage):
    pages = {str(p.id): p.page_number for p in paper.pages}
    actual = []
    for q in paper.questions:
        row = q.model_dump(mode="json")
        row["source_page_numbers"] = [pages.get(str(p)) for p in q.source_page_ids]
        row["assets"] = (
            None
            if q.assets is None
            else [
                {
                    "asset_type": a.asset_type,
                    "source_page_number": pages.get(str(a.source_page_id)),
                    "order_index": i,
                }
                for i, a in enumerate(q.assets, 1)
            ]
        )
        actual.append(row)
    grouped = {}
    for q in actual:
        grouped.setdefault(
            (normalized(q["question_number"]), tuple(q["source_page_numbers"])), []
        ).append(q)
    rows, matches = [], []
    for label in labels:
        found = grouped.get(
            (normalized(label["question_number"]), tuple(label["source_page_numbers"])),
            [],
        )
        matched = found[0] if len(found) == 1 else None
        reason = (
            "matched"
            if matched
            else "duplicate_identity" if found else "missing_identity"
        )
        matches.append(
            {
                "reference_identity": label["question_identity"],
                "actual_id": matched["id"] if matched else None,
                "reason": reason,
            }
        )
        for field in FIELDS:
            unknown = (
                field in label.get("unknown", [])
                or label.get("field_states", {}).get(field) == "unknown"
            )
            target, value = expected(label, field), (
                matched.get(field) if matched else None
            )
            rows.append(
                {
                    "run_id": run_id,
                    "case_id": case,
                    "repeat_index": repeat,
                    "stage": stage,
                    "reference_question_identity": label["question_identity"],
                    "extracted_question_id": matched["id"] if matched else None,
                    "field": field,
                    "reference_state": label.get("field_states", {}).get(
                        field, "source_provided"
                    ),
                    "evaluable": not unknown,
                    "correct": (
                        None
                        if unknown
                        else bool(matched and equal(field, target, value))
                    ),
                    "expected": target,
                    "actual": value,
                    "reason": "reference_unknown" if unknown else reason,
                }
            )
    metrics = []
    for field in FIELDS:
        valid = [r for r in rows if r["field"] == field and r["evaluable"]]
        numerator, denominator = sum(r["correct"] for r in valid), len(valid)
        metrics.append(
            {
                "run_id": run_id,
                "case_id": case,
                "repeat_index": repeat,
                "stage": stage,
                "metric": field + "_assisted_reference_match",
                "unit": "ratio",
                "numerator": numerator,
                "denominator": denominator,
                "value": numerator / denominator if denominator else None,
                "status": (
                    "observed_reference_comparison"
                    if denominator
                    else "no_evaluable_reference"
                ),
                "reason": "user-confirmed AI-assisted reference; independent teacher denominator 0",
            }
        )
    return {
        "identity_matches": matches,
        "fields": rows,
        "metrics": metrics,
        "reference_question_count": len(labels),
        "actual_question_count": len(actual),
        "unknown_field_count": sum(not r["evaluable"] for r in rows),
        "matched_identities": sum(m["actual_id"] is not None for m in matches),
        "duplicate_actual_identities": sum(
            max(0, len(v) - 1) for v in grouped.values()
        ),
    }


def plan():
    baseline = get_settings()
    isolation = json.loads((RUN / "isolation.json").read_text(encoding="utf-8"))
    assert isolation["database"].startswith("eduagent_e2_acceptance_")
    url = make_url(str(baseline.database_url))
    settings = baseline.model_copy(
        update={
            "database_url": type(baseline.database_url)(
                url.set(database=isolation["database"]).render_as_string(
                    hide_password=False
                )
            ),
            "redis_url": type(baseline.redis_url)(
                f"redis://127.0.0.1:{isolation['redis_port']}/0"
            ),
            "storage_root": RUN / "business-files",
            "ocr_enabled": True,
            "ocr_provider": "rapidocr",
            "ocr_model": "PP-OCRv5-mobile",
            "ocr_model_dir": args.ocr_model_dir or baseline.ocr_model_dir,
        }
    )
    assert (
        settings.llm_provider == "deepseek"
        and settings.deepseek_model == "deepseek-chat"
    )
    raw = ANNOTATIONS.read_bytes()
    labels = json.loads(raw)
    assert (
        labels["status"] == "user_confirmed_ai_assisted"
        and labels["teacher_id"] is None
    )
    assert not labels["is_independent_teacher_truth"]
    entries = {e["case_id"]: e for e in labels["entries"]}
    assert set(entries) == set(CASES)
    assert (
        settings.ocr_model_dir is not None
    ), "Supply --ocr-model-dir or configured OCR_MODEL_DIR"
    model_paths = _model_paths(settings.ocr_model_dir)
    planned = []
    for case in CASES:
        entry = entries[case]
        assert entry["human_verified"] is True and len(entry["source_refs"]) == 1
        source_ref = entry["source_refs"][0]
        source_sha = sha((INPUTS / source_ref).read_bytes())
        identities = []
        for q in entry["labels"]["questions"]:
            assert q["source_sha256"] == source_sha
            if q["question_type"] is not None:
                assert isinstance(QuestionType[q["question_type"]], QuestionType)
            identities.append((q["question_number"], tuple(q["source_page_numbers"])))
        assert len(identities) == len(set(identities))
        for repeat in range(1, 4):
            planned.append(
                {
                    "case_id": case,
                    "repeat_index": repeat,
                    "source_ref": source_ref,
                    "source_sha256": source_sha,
                    "reference_questions": len(identities),
                }
            )
    assert len(planned) == 15
    versions = {}
    for name in (
        "rapidocr",
        "onnxruntime",
        "pypdf",
        "pypdfium2",
        "openai",
        "httpx",
        "sqlalchemy",
    ):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    commit = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    manifest = {
        "protocol_version": "v2-evaluation-1",
        "source_commit": commit,
        "source_root": str(SOURCE),
        "runner_sha256": sha(Path(__file__).read_bytes()),
        "dataset_version": labels["input_dataset_version"],
        "annotation_version": labels["annotation_version"],
        "annotation_sha256": sha(raw),
        "annotation_status": labels["status"],
        "reference_origin": "user_confirmed_ai_assisted",
        "independent_teacher_labeled_cases": 0,
        "user_review_confirmation": labels.get("user_review_confirmation"),
        "reviewed_by": labels.get("reviewed_by"),
        "reviewed_at": labels.get("reviewed_at"),
        "quality_thresholds": None,
        "threshold_status": "pending_baseline",
        "environment": {
            "os": platform.platform(),
            "cpu": platform.processor(),
            "logical_cpus": os.cpu_count(),
            "python": sys.version,
            "packages": versions,
        },
        "config": {
            "provider": settings.llm_provider,
            "model": settings.deepseek_model,
            "provider_host": settings.deepseek_base_url.host,
            "prompt": PROMPT_VERSION,
            "batch_size": 2,
            "provider_timeout_seconds": 30,
            "sdk_retries": 0,
            "retry_policy": "unchanged production RetryPolicy",
            "concurrency": 1,
            "ocr_enabled": True,
            "ocr_provider": "rapidocr",
            "ocr_model": settings.ocr_model,
            "model_files": [
                {"file": n, "sha256": sha(p.read_bytes())}
                for n, p in model_paths.items()
            ],
            "render_dpi": 150,
            "autoflush": False,
            "database": isolation["database"],
            "application_state": "quality repetitions; new provider/OCR per import; no cold/warm claim",
        },
        "planned_runs": planned,
        "planned_imports": 15,
        "distinct_reference_questions": sum(
            len(entries[c]["labels"]["questions"]) for c in CASES
        ),
        "policy": {
            "automatic_identity": "original number + real ordered source pages, unique one-to-one",
            "correction_identity": "explicit unique original number; no guessed content match",
            "normalize": "only line endings/trim; preserve option keys/order; Decimal score; exact label set",
            "unknown": "exclude and count; known missing values and known image requirements remain evaluable",
            "images": "ordered type/source-page association; not pixel quality or independent teacher truth",
        },
        "planned_at": now(),
        "cloud_calls_executed": 0,
    }
    return settings, isolation, entries, manifest


class HTTPObservation:
    def __init__(self, directory):
        self.directory, self.attempts = directory, []

    async def request(self, request):
        assert request.method == "POST" and request.url.path.endswith(
            "/chat/completions"
        )
        body = json.loads(request.content)
        attempt = {
            "attempt_index": len(self.attempts) + 1,
            "started_at": now(),
            "start_perf": time.perf_counter(),
            "route": request.url.path,
            "status": "request_started",
            "request": {
                k: body.get(k) for k in ("model", "messages", "response_format")
            },
        }
        request.extensions["t160_attempt"] = attempt["attempt_index"]
        self.attempts.append(attempt)
        save(self.directory / f"http-{attempt['attempt_index']:03d}.json", attempt)

    async def response(self, response):
        attempt = self.attempts[response.request.extensions["t160_attempt"] - 1]
        await response.aread()
        attempt.update(
            completed_at=now(),
            elapsed_ms=(time.perf_counter() - attempt["start_perf"]) * 1000,
            status="response_received",
            http_status=response.status_code,
        )
        try:
            body = response.json()
            if response.status_code == 200:
                attempt.update(
                    response=body,
                    actual_response_model=body.get("model"),
                    usage=body.get("usage"),
                )
            else:
                attempt["error"] = {
                    k: body.get("error", {}).get(k) for k in ("type", "code")
                }
        except (ValueError, AttributeError):
            attempt["response_parse_status"] = "not_json"
        save(self.directory / f"http-{attempt['attempt_index']:03d}.json", attempt)


async def actual_import(factory, settings, actor, identity, run_id, directory, report):
    provider = create_llm_provider(settings)
    provider._request_client()  # Prepare the actual SDK solely to bind read-only observation.
    assert isinstance(provider._client, AsyncOpenAI)
    observer = HTTPObservation(directory)
    provider._client._client.event_hooks["request"].append(observer.request)
    provider._client._client.event_hooks["response"].append(observer.response)
    report["provenance"] = provider.describe(prompt_version=PROMPT_VERSION)
    assert report["provenance"]["model"] == "deepseek-chat"
    try:
        with (
            bind_trace(
                request_id=run_id,
                user_id=actor,
                workflow_id=None,
                service=TraceService(factory),
            ),
            trace_prompt_version(PROMPT_VERSION),
        ):
            await PaperImportRunner(
                factory,
                settings=settings,
                extractor=PaperExtractor(provider),
                ocr=create_ocr_provider(settings),
            )._run(identity)
    finally:
        report["http_attempts"] = observer.attempts
        report["actual_http_attempts"] = len(observer.attempts)
        try:
            await provider.aclose()
        except Exception as exc:  # noqa: BLE001 - Keep failed cases.
            report["cleanup_error"] = error(exc)


def correct(factory, settings, actor, paper, labels):
    counts = Counter(normalized(q.question_number) for q in paper.questions)
    by_number = {
        normalized(q.question_number): q
        for q in paper.questions
        if counts[normalized(q.question_number)] == 1
    }
    pages = {p.page_number: p.id for p in paper.pages}
    actions = []
    for label in labels:
        q = by_number.get(normalized(label["question_number"]))
        action = {
            "reference_identity": label["question_identity"],
            "mapping": "unique_original_number",
            "actor_kind": "synthetic Teacher-role executor of user-approved assisted reference",
            "independent_teacher_review": False,
            "started_at": now(),
        }
        actions.append(action)
        if q is None or any(n not in pages for n in label["source_page_numbers"]):
            action.update(
                status="not_executed",
                reason="missing_or_duplicate_number_or_missing_real_page",
            )
            continue
        payload = {
            "question_number": label["question_number"],
            "order_index": label["order_index"],
            "source_page_ids": [pages[n] for n in label["source_page_numbers"]],
            "correction_notes": "T160 approved AI-assisted reference; not independent teacher annotation.",
        }
        for field in (
            "question_type",
            "content",
            "options",
            "reference_answer",
            "analysis",
            "score",
            "scoring_rubric",
            "knowledge_points",
        ):
            if field not in label.get("unknown", []):
                payload[field] = (
                    QuestionType[label[field]].value
                    if field == "question_type" and label[field] is not None
                    else label[field]
                )
        if not label["assets"]:
            payload["assets"] = []
            action["asset_operation"] = "reference_explicit_no_images"
        else:
            action["asset_operation"] = (
                "pending_actual_source_crop_or_verified_existing_asset"
            )
            action["pending_assets"] = label["assets"]
        clock = time.perf_counter()
        try:
            parsed = CorrectionPayload.model_validate(payload)
            with factory() as session:
                saved = QuestionCorrectionService(
                    session, root=settings.storage_root
                ).patch(paper.id, q.id, parsed, actor_id=actor)
            action.update(
                status="persisted",
                extracted_question_id=str(saved.id),
                payload=parsed.model_dump(mode="json", exclude_unset=True),
            )
        except Exception as exc:  # noqa: BLE001 - Keep failed cases.
            action.update(status="failed", error=error(exc))
        action.update(
            completed_at=now(), elapsed_ms=(time.perf_counter() - clock) * 1000
        )
    with factory() as session:
        return (
            PaperImportService(session, root=settings.storage_root).get(
                paper.id, actor_id=actor
            ),
            actions,
        )


def csv_rows(path, rows, columns):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    k: (
                        json.dumps(v, ensure_ascii=False, default=default)
                        if isinstance(v, (dict, list))
                        else v
                    )
                    for k, v in row.items()
                }
            )


def execute(settings, isolation, entries, manifest):
    from resource_sampler import ResourceSampler

    with (RUN / "quality-execution-started.json").open("x", encoding="utf-8") as stream:
        json.dump(
            {
                "started_at": now(),
                "planned_imports": 15,
                "paid_execution_explicit": True,
                "annotation_sha256": manifest["annotation_sha256"],
            },
            stream,
        )
    batch = (
        "t160-quality-"
        + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid4().hex[:8]
    )
    output = RUN / batch
    output.mkdir()
    manifest.update(batch_id=batch, started_at=now(), evidence_kind="real_provider")
    save(output / "manifest.json", manifest)
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)
    with factory() as session:
        role = session.scalar(select(Role).where(Role.name == UserRole.TEACHER))
        if role is None:
            role = Role(name=UserRole.TEACHER)
            session.add(role)
        actor = User(
            username=batch,
            email=batch + "@synthetic.invalid",
            password_hash="synthetic-fixture-no-password",
            is_active=True,
            roles=[role],
        )
        session.add(actor)
        session.flush()
        course = Course(
            name="T160 actual import/reference batch " + batch, created_by=actor.id
        )
        session.add(course)
        session.commit()
        actor_id, course_id = actor.id, course.id
    reports, field_rows, metrics, timings, failures = [], [], [], [], []
    try:
        for position, item in enumerate(manifest["planned_runs"], 1):
            case, repeat = item["case_id"], item["repeat_index"]
            labels = entries[case]["labels"]["questions"]
            run_id = f"{batch}-{position:02d}-{case}-r{repeat}"
            directory = output / "runs" / run_id
            directory.mkdir(parents=True)
            report = {
                **item,
                "run_id": run_id,
                "evidence_kind": "real_provider",
                "reference_origin": "user_confirmed_ai_assisted",
                "independent_teacher_labeled_cases": 0,
                "started_at": now(),
                "actual_http_attempts": 0,
                "provenance": None,
                "fee": None,
                "fee_reason": "provider fee not returned",
            }
            sampler = ResourceSampler(
                pid=os.getpid(),
                pg_container=isolation["pg_container"],
                redis_container=isolation["redis_container"],
                output_path=directory / "resource-samples.jsonl",
                interval_seconds=1.0,
            )
            overall = time.perf_counter()
            import_start = import_end = None
            sampler.start()
            try:
                source = INPUTS / item["source_ref"]
                assert sha(source.read_bytes()) == item["source_sha256"]
                upload_clock = time.perf_counter()
                with factory() as session:
                    received = PaperImportService(
                        session, root=settings.storage_root
                    ).upload(
                        course_id,
                        filename=source.name,
                        content=source.read_bytes(),
                        actor_id=actor_id,
                    )
                report.update(
                    upload_receipt=received.model_dump(mode="json"),
                    uploaded_at=now(),
                    upload_elapsed_ms=(time.perf_counter() - upload_clock) * 1000,
                )
                import_start = time.perf_counter()
                asyncio.run(
                    actual_import(
                        factory,
                        settings,
                        actor_id,
                        received.id,
                        run_id,
                        directory,
                        report,
                    )
                )
                with factory() as session:
                    paper = PaperImportService(session, root=settings.storage_root).get(
                        received.id, actor_id=actor_id
                    )
                    imported = session.get(PaperImport, received.id)
                    report["original_document"] = {
                        k: getattr(imported.document, k)
                        for k in (
                            "id",
                            "purpose",
                            "status",
                            "file_metadata",
                            "original_filename",
                        )
                    }
                    report["provider_traces"] = [
                        {
                            k: getattr(row, k)
                            for k in (
                                "id",
                                "status",
                                "model",
                                "prompt_version",
                                "input_tokens",
                                "output_tokens",
                                "total_tokens",
                                "latency_ms",
                                "error_code",
                                "error_retryable",
                                "created_at",
                            )
                        }
                        for row in session.scalars(
                            select(AgentRun).where(AgentRun.request_id == run_id)
                        )
                    ]
                import_end = time.perf_counter()
                report.update(
                    automatic_snapshot=paper.model_dump(mode="json"),
                    automatic_status=paper.status.value,
                    automatic_completed_at=now(),
                    import_elapsed_ms=(import_end - import_start) * 1000,
                )
                automatic = comparison(labels, paper, run_id, case, repeat, "automatic")
                report["automatic_comparison"] = automatic
                field_rows.extend(automatic["fields"])
                metrics.extend(automatic["metrics"])
                if paper.status == PaperImportStatus.PENDING_REVIEW:
                    corrected, actions = correct(
                        factory, settings, actor_id, paper, labels
                    )
                    report.update(
                        corrected_snapshot=corrected.model_dump(mode="json"),
                        correction_actions=actions,
                    )
                    comparison_after = comparison(
                        labels, corrected, run_id, case, repeat, "assisted_corrected"
                    )
                    report["assisted_corrected_comparison"] = comparison_after
                    field_rows.extend(comparison_after["fields"])
                    metrics.extend(comparison_after["metrics"])
                else:
                    report["corrected_status"] = "not_executed_due_to_import_failure"
                    comparison_after = comparison(
                        labels, paper, run_id, case, repeat, "assisted_corrected"
                    )
                    for row in comparison_after["fields"]:
                        row.update(
                            actual=None,
                            extracted_question_id=None,
                            correct=False if row["evaluable"] else None,
                            reason="not_executed_due_to_import_failure",
                        )
                    for metric in comparison_after["metrics"]:
                        metric.update(
                            numerator=0,
                            value=0 if metric["denominator"] else None,
                            reason="Not executed after import failure; fixed planned denominator retained.",
                        )
                    for identity in comparison_after["identity_matches"]:
                        identity.update(
                            actual_id=None, reason="not_executed_due_to_import_failure"
                        )
                    comparison_after.update(
                        matched_identities=0, actual_question_count=0
                    )
                    report["assisted_corrected_comparison"] = comparison_after
                    field_rows.extend(comparison_after["fields"])
                    metrics.extend(comparison_after["metrics"])
                    failures.append(
                        {
                            "run_id": run_id,
                            "case_id": case,
                            "stage": "automatic",
                            "code": paper.error_code,
                            "message": paper.error_message,
                            "evidence_ref": str(
                                directory.relative_to(output) / "result.json"
                            ),
                        }
                    )
            except Exception as exc:  # noqa: BLE001 - Keep failed cases.
                report["runner_error"] = error(exc)
                failures.append(
                    {
                        "run_id": run_id,
                        "case_id": case,
                        "stage": "runner",
                        "error": error(exc),
                    }
                )
            finally:
                sampler.sample_now()
                sampler.stop()
                report.update(
                    resource_summary=sampler.summary(
                        start_perf=import_start, end_perf=import_end
                    ),
                    resource_samples_ref=str(
                        directory.relative_to(output) / "resource-samples.jsonl"
                    ),
                    completed_at=now(),
                    overall_elapsed_ms=(time.perf_counter() - overall) * 1000,
                )
                reports.append(report)
                save(directory / "result.json", report)
                save(
                    output / "progress.json",
                    {
                        "planned": 15,
                        "attempted": len(reports),
                        "last_run": run_id,
                        "last_status": report.get("automatic_status", "runner_error"),
                        "actual_http_attempts": sum(
                            r["actual_http_attempts"] for r in reports
                        ),
                        "utc": now(),
                    },
                )
                timing = {
                    "run_id": run_id,
                    "case_id": case,
                    "repeat_index": repeat,
                    "stage": "automatic",
                    "start_event": "original_uploaded_durably",
                    "end_event": "actual_pending_review_or_failed_read",
                    "elapsed_ms": report.get("import_elapsed_ms"),
                    "measurement_source": "same-process perf_counter",
                    "status": report.get("automatic_status", "runner_error"),
                    "target_ms": 300000,
                    "target_met": report.get("automatic_status") == "Pending Review"
                    and report.get("import_elapsed_ms", float("inf")) < 300000,
                    "resource_complete": report["resource_summary"].get(
                        "complete_at_requested_sampling_resolution"
                    ),
                    "evidence_ref": str(directory.relative_to(output) / "result.json"),
                }
                timings.append(timing)
                print(
                    json.dumps(
                        {
                            "position": position,
                            "planned": 15,
                            "run_id": run_id,
                            "status": report.get("automatic_status", "runner_error"),
                            "http_attempts": report["actual_http_attempts"],
                            "elapsed_ms": report.get("import_elapsed_ms"),
                            "utc": now(),
                        }
                    ),
                    flush=True,
                )
        csv_rows(
            output / "cases.csv",
            field_rows,
            (
                "run_id",
                "case_id",
                "repeat_index",
                "stage",
                "reference_question_identity",
                "extracted_question_id",
                "field",
                "reference_state",
                "evaluable",
                "correct",
                "expected",
                "actual",
                "reason",
            ),
        )
        csv_rows(
            output / "metrics.csv",
            metrics,
            (
                "run_id",
                "case_id",
                "repeat_index",
                "stage",
                "metric",
                "unit",
                "numerator",
                "denominator",
                "value",
                "status",
                "reason",
            ),
        )
        csv_rows(
            output / "timings.csv",
            timings,
            (
                "run_id",
                "case_id",
                "repeat_index",
                "stage",
                "start_event",
                "end_event",
                "elapsed_ms",
                "measurement_source",
                "status",
                "target_ms",
                "target_met",
                "resource_complete",
                "evidence_ref",
            ),
        )
        with (output / "failures.jsonl").open("w", encoding="utf-8") as stream:
            for failure in failures:
                stream.write(
                    json.dumps(failure, ensure_ascii=False, default=default) + "\n"
                )
        summary = {
            "batch_id": batch,
            "planned": 15,
            "attempted": len(reports),
            "completed_pending_review": sum(
                r.get("automatic_status") == "Pending Review" for r in reports
            ),
            "failed_imports": sum(
                r.get("automatic_status") == "Failed" for r in reports
            ),
            "runner_errors": sum("runner_error" in r for r in reports),
            "actual_http_attempts": sum(r["actual_http_attempts"] for r in reports),
            "independent_teacher_labeled_cases": 0,
            "reference_origin": "user_confirmed_ai_assisted",
            "quality_thresholds": None,
            "quality_acceptance": "not_claimed",
            "completed_at": now(),
            "output": str(output),
        }
        save(output / "summary.json", summary)
        save(RUN / "quality-output-pointer.json", summary)
        print(json.dumps(summary), flush=True)
        return 0 if len(reports) == 15 and not summary["runner_errors"] else 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    settings, isolation, entries, manifest = plan()
    if args.plan_only:
        save(RUN / "quality-plan.json", manifest)
        print(
            json.dumps(
                {
                    "status": "plan_only",
                    "planned_imports": 15,
                    "case_order": list(CASES),
                    "repeats": 3,
                    "reference_questions": manifest["distinct_reference_questions"],
                    "model": settings.deepseek_model,
                    "annotation_status": manifest["annotation_status"],
                    "annotation_sha256": manifest["annotation_sha256"],
                    "cloud_calls_executed": 0,
                    "database_mutations": 0,
                    "plan": str(RUN / "quality-plan.json"),
                }
            ),
            flush=True,
        )
    else:
        raise SystemExit(execute(settings, isolation, entries, manifest))
