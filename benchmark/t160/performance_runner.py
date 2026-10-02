"""T160 import-performance protocol runner; --plan-only never makes model calls.

Dedicated experiment outside production code. All outcomes occupy planned slots.
--execute is intentionally explicit; root coordinates quality before performance.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import importlib.metadata
import json
import math
import os
import re
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
RUN = REPO / ".cache/t160-performance"
SOURCE = REPO
OUT = REPO / "benchmark/results/v2/t160-replay/import-performance"
MODEL_DIR = REPO / ".cache/t155-ocr-20261002/rapid-models"
ISOLATION_FILE = RUN / "isolation.json"
SOURCE_COMMIT = "unrecorded"
sys.path.insert(0, str(SOURCE))
sys.path.insert(0, str(RUN))
from resource_sampler import (
    ResourceSampler,
    container_rss,
    verified_worker_chain,
    windows_working_set,
)


def utc():
    return datetime.now(UTC).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


def isolated_env():
    from dotenv import dotenv_values
    from sqlalchemy.engine import make_url

    from backend.app.core.config import get_settings

    env = os.environ.copy()
    env.update({k: v for k, v in dotenv_values(REPO / ".env").items() if v is not None})
    original = get_settings()
    isolation = json.loads(ISOLATION_FILE.read_text(encoding="utf-8"))
    scope = re.fullmatch(
        r"eduagent_(e2_acceptance|t160_performance)_([0-9a-f]{12})",
        isolation["database"],
    )
    assert (
        scope is not None
    ), "only explicitly named isolated T160 databases are accepted"
    expected_redis = "eduagent-" + scope[1].replace("_", "-") + "-redis-" + scope[2]
    assert (
        isolation["redis_container"] == expected_redis
    ), "Redis identity must belong to the same isolated run"
    assert (
        isinstance(isolation["redis_port"], int)
        and 1 <= isolation["redis_port"] <= 65535
    )
    assert re.fullmatch(r"[A-Za-z0-9_.-]+", isolation["pg_container"])
    env.update(
        DATABASE_URL=make_url(str(original.database_url))
        .set(database=isolation["database"])
        .render_as_string(hide_password=False),
        REDIS_URL="redis://127.0.0.1:" + str(isolation["redis_port"]) + "/0",
        STORAGE_ROOT=str(RUN / "business-files"),
        OCR_ENABLED="true",
        OCR_PROVIDER="rapidocr",
        OCR_MODEL="PP-OCRv5-mobile",
        OCR_MODEL_DIR=str(MODEL_DIR),
        PYTHONPATH=str(SOURCE),
        PYTHONIOENCODING="utf-8",
    )
    return env, isolation, original


def make_plan():
    from pypdf import PdfReader

    from backend.app.core.retry_policy import RetryPolicy

    _env, _isolation, settings = isolated_env()
    assert (
        settings.llm_provider == "deepseek"
        and settings.deepseek_model == "deepseek-chat"
    ), "model decision differs from current configuration"
    samples = []
    for filename, pages in (
        ("paper_text.pdf", 1),
        ("workload_10_pages.pdf", 10),
        ("workload_50_pages.pdf", 50),
    ):
        path = SOURCE / "benchmark/corpus/v2-draft-20261001/inputs" / filename
        raw = path.read_bytes()
        assert len(PdfReader(path).pages) == pages
        samples.append(
            {
                "case": path.stem,
                "filename": filename,
                "page_count": pages,
                "bytes": len(raw),
                "sha256": digest(raw),
                "planned_cold": 3,
                "planned_warmup": 1,
                "planned_warm": 5,
                "base_logical_requests_per_success": math.ceil(pages / 2),
            }
        )
    model_files = []
    expected = {
        "ch_PP-OCRv5_det_mobile.onnx": "4d97c44a20d30a81aad087d6a396b08f786c4635742afc391f6621f5c6ae78ae",
        "ch_PP-OCRv5_rec_mobile.onnx": "5825fc7ebf84ae7a412be049820b4d86d77620f204a041697b0494669b1742c5",
        "ch_ppocr_mobile_v2.0_cls_mobile.onnx": "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c",
    }
    for name, sha in expected.items():
        raw = (MODEL_DIR / name).read_bytes()
        assert digest(raw) == sha, name
        model_files.append({"filename": name, "bytes": len(raw), "sha256": sha})
    assert os.name == "nt"
    policy = RetryPolicy()
    return {
        "kind": "real-import-performance-plan",
        "planned_at": utc(),
        "cloud_calls_in_plan_only": 0,
        "teacher_annotation": False,
        "source_commit": SOURCE_COMMIT,
        "concurrency": 1,
        "cases": samples,
        "total_imports": 27,
        "measured_imports": 24,
        "warmup_imports": 3,
        "cold_processes": 9,
        "warm_processes": 3,
        "base_logical_calls_if_all_batches_reached": sum(
            9 * r["base_logical_requests_per_success"] for r in samples
        ),
        "model": {
            "provider": settings.llm_provider,
            "configured_model": settings.deepseek_model,
            "endpoint_host": settings.deepseek_base_url.host,
            "prompt_version": "paper-extraction-v1",
            "batch_size": 2,
            "retry_policy": {
                "max_retries": policy.max_retries,
                "backoff_seconds": policy.backoff_seconds,
                "max_retry_after": policy.max_retry_after,
                "jitter_seconds": policy.jitter_seconds,
            },
            "sdk_max_retries": 0,
            "timeout_seconds": 30,
            "fallback": None,
        },
        "ocr": {
            "provider": "rapidocr",
            "model": "PP-OCRv5-mobile",
            "model_files": model_files,
            "production_default_ocr_argument": None,
            "cross_import_ocr_instance_reused": False,
            "versions": {
                n: importlib.metadata.version(n)
                for n in ("rapidocr", "onnxruntime", "pypdf", "pypdfium2", "openai")
            },
        },
        "process_protocol": "Each cold trial starts a new actual create_app/full-lifespan process. Each warm group uses one process, same-scale warmup then five new imports. Production Runner OCR/provider factories, initialization and retries remain unchanged. OS/disk model caches are not cleared.",
        "timing_boundary": {
            "start": "Immediately before PaperImportService.upload receives an already-received in-memory file. Includes format validation, original file/receipt writes, database registration and all subsequent import stages.",
            "upload_stage": "upload entry through its returned Uploaded view; separately observe store_document start/end. store_document return precedes resource registration commit; upload return includes commit/receipt.",
            "end": "Pure after_commit observer records actual durable Pending Review or Failed transition. Owned provider cleanup and final DTO read are recorded separately, outside terminal elapsed.",
            "excluded": "Application startup, benchmark local input-file read before upload, client network transfer, teacher reading/correction/confirmation, and model resource downloads.",
            "failure": "Every planned run retained. No rerun, deleted failed slot or inferred success. Upload failures without a persisted import record are explicit operation failures.",
        },
        "resources": {
            "interval_seconds": 1,
            "windows": "application child working set; experiment controller excluded",
            "postgres": "shared container all postgres process RSS sum; includes other database workloads and may count shared pages repeatedly",
            "redis": "dedicated container all redis-server process RSS sum",
            "missing": "null and incomplete, never zero",
            "scope": "API backend application, minimal Gradio shell; no claim of full UI/EXE acceptance",
        },
        "budgets": {
            "import_terminal_seconds_strict_less_than": 300,
            "interpretation": "All 24 measured imports must reach Pending Review and individually satisfy duration. Warmups separate. Resource sampling is observed process RSS/workset, not unique physical memory or EXE budget proof.",
        },
        "historical_acceptance_environment": {
            "cpu": "AMD Ryzen 5 4600H",
            "physical_cores": 6,
            "logical_processors": 12,
            "total_ram_bytes": 16505966592,
            "disk": "SAMSUNG MZVLB512HBJQ-000L2 SSD NVMe",
            "postgres": "16.15 Debian",
            "pgvector": "0.8.6",
            "evidence": "root agent actual read-only host/container checks",
            "cpu_affinity_or_container_limits_set": False,
            "matches_2_to_4_vcpu_budget_machine": False,
            "sla_extrapolation_allowed": False,
        },
        "result_dir": str(OUT),
        "environment_preserved": ".env and production source unchanged",
        "observability": "Read-only wrappers call original provider and file-store methods unchanged. Count logical calls and Provider invocations separately from actual HTTP attempt hooks on the real AsyncOpenAI httpx client. Request hook entering is a transport attempt, not proof of server receipt; response hooks separately record status. Capture schema output and model/usage. No headers, complete Prompt or credentials retained.",
        "important_source_sha256": {
            name: digest((SOURCE / name).read_bytes())
            for name in (
                "backend/app/services/paper_import_service.py",
                "backend/app/ai/llm/deepseek.py",
                "backend/app/ai/ingestion/ocr/rapidocr.py",
                "backend/app/ai/paper_extraction/service.py",
            )
        },
    }


async def worker_async(job):
    # Executed only by --worker after root explicitly starts --execute.
    import gradio as gr
    from sqlalchemy import event, select
    from sqlalchemy.orm import Session

    import backend.app.services.paper_import_service as runner_module
    from backend.app.core.app import create_app
    from backend.app.core.config import get_settings
    from backend.app.core.database import (
        check_postgres_ready,
        create_database_engine,
        create_session_factory,
    )
    from backend.app.core.redis import check_redis_ready, create_redis_client
    from backend.app.core.retry_policy import classify_provider_exception
    from backend.app.domain.enums import UserRole
    from backend.app.models import Course, PaperImport, Role, User
    from backend.app.services.file_storage_service import FileStorageService
    from backend.app.services.paper_import_service import (
        PaperImportRunner,
        PaperImportService,
    )

    settings = get_settings()
    assert (
        settings.deepseek_model == "deepseek-chat"
        and settings.llm_provider == "deepseek"
    )
    assert (
        settings.ocr_enabled
        and settings.ocr_provider == "rapidocr"
        and settings.ocr_model == "PP-OCRv5-mobile"
    )
    assert settings.ocr_model_dir.resolve() == MODEL_DIR.resolve()
    # Direct --worker invocation must remain in the same private scope as its parent.
    from sqlalchemy.engine import make_url

    isolation = json.loads(ISOLATION_FILE.read_text(encoding="utf-8"))
    scope = re.fullmatch(
        r"eduagent_(e2_acceptance|t160_performance)_([0-9a-f]{12})",
        isolation["database"],
    )
    assert scope is not None
    assert (
        make_url(str(settings.database_url)).database == isolation["database"]
    ), "worker database differs from private isolation input"
    assert (
        str(settings.redis_url)
        == "redis://127.0.0.1:" + str(isolation["redis_port"]) + "/0"
    ), "worker Redis differs from private isolation input"
    assert (
        settings.storage_root.resolve() == (RUN / "business-files").resolve()
    ), "worker storage differs from the private run directory"
    assert (
        isolation["redis_container"]
        == "eduagent-" + scope[1].replace("_", "-") + "-redis-" + scope[2]
    )
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)
    check_postgres_ready(engine)
    redis = create_redis_client(settings)
    check_redis_ready(redis)
    redis.close()
    current = {}
    original_factory = runner_module.create_llm_provider
    original_store = FileStorageService.store_document

    def error_fact(exc):
        classified = classify_provider_exception(exc)
        return {
            "type": type(exc).__name__,
            "code": getattr(exc, "code", classified.code),
            "safe_message": getattr(exc, "safe_message", classified.safe_message),
            "retryable": getattr(exc, "retryable", classified.retryable),
            "attempt_count": getattr(exc, "attempt_count", None),
        }

    def observe_factory(cfg):
        provider = original_factory(cfg)
        assert type(provider).__name__ == "DeepSeekProvider"
        real_generate = provider.generate_structured
        real_request = provider._request_json
        real_sdk = provider._client.chat.completions.create
        active = {}

        async def request_hook(request):
            logical = active.get("logical")
            attempt = active.get("attempt")
            fact = {
                "http_attempt_no": len(current["record"]["http_requests"]) + 1,
                "logical_call_no": logical.get("logical_call_no") if logical else None,
                "provider_attempt_no": (
                    attempt.get("attempt_number") if attempt else None
                ),
                "started_at": utc(),
                "start_perf": time.perf_counter(),
                "method": request.method,
                "host": request.url.host,
                "path": request.url.path,
                "response_observed": False,
                "measurement": "httpx request event hook entered before transport; not proof server received bytes",
            }
            try:
                body = json.loads(request.content)
                value = body.get("model") if isinstance(body, dict) else None
                fact["requested_model"] = value if isinstance(value, str) else None
            except (ValueError, TypeError, RuntimeError) as exc:
                fact["requested_model"] = None
                fact["body_model_observation_error_type"] = type(exc).__name__
            current["record"]["http_requests"].append(fact)
            request.extensions["t160_http_observation"] = fact
            active.setdefault("http_attempts", []).append(fact)

        async def response_hook(response):
            fact = response.request.extensions.get("t160_http_observation")
            if fact is not None:
                fact.update(
                    response_observed=True,
                    status_code=response.status_code,
                    response_headers_received_at=utc(),
                    response_header_elapsed_seconds=time.perf_counter()
                    - fact["start_perf"],
                )

        transport = provider._client._client
        import httpx

        assert isinstance(transport, httpx.AsyncClient)
        transport.event_hooks.setdefault("request", []).append(request_hook)
        transport.event_hooks.setdefault("response", []).append(response_hook)

        async def sdk_create(*args, **kwargs):
            active["http_attempts"] = []
            try:
                response = await real_sdk(*args, **kwargs)
                attempt = active.get("attempt")
                if attempt is not None:
                    usage = getattr(response, "usage", None)
                    attempt["response_model"] = getattr(response, "model", None)
                    attempt["usage"] = {
                        name: getattr(usage, name, None)
                        for name in (
                            "prompt_tokens",
                            "completion_tokens",
                            "total_tokens",
                        )
                    }
                for fact in active.get("http_attempts", []):
                    if fact.get("response_observed"):
                        fact["response_model"] = getattr(response, "model", None)
                        fact["response_model_evidence"] = (
                            "actual SDK parsed HTTP response.model; distinct from configured/request body model"
                        )
                return response
            except Exception as exc:
                for fact in active.get("http_attempts", []):
                    fact["sdk_error"] = error_fact(exc)
                raise
            finally:
                for fact in active.pop("http_attempts", []):
                    fact["sdk_call_finished_at"] = utc()
                    fact["sdk_elapsed_seconds"] = (
                        time.perf_counter() - fact["start_perf"]
                    )

        provider._client.chat.completions.create = sdk_create

        async def request(messages, schema, model):
            fact = {
                "attempt_number": len(active["logical"]["attempts"]) + 1,
                "started_at": utc(),
                "start_perf": time.perf_counter(),
                "configured_model": model or provider._model,
            }
            active["logical"]["attempts"].append(fact)
            active["attempt"] = fact
            try:
                result = await real_request(messages, schema, model)
                fact["status"] = "success"
                return result
            except Exception as exc:
                fact["status"] = "failure"
                fact["error"] = error_fact(exc)
                raise
            finally:
                fact["ended_at"] = utc()
                fact["elapsed_seconds"] = time.perf_counter() - fact["start_perf"]
                active.pop("attempt", None)
                write_json(Path(current["raw_path"]), current["record"])

        async def generate(messages, schema, model=None):
            logical = {
                "logical_call_no": len(current["record"]["llm_calls"]) + 1,
                "schema": schema.__name__,
                "prompt_version": "paper-extraction-v1",
                "started_at": utc(),
                "start_perf": time.perf_counter(),
                "provider_metadata": provider.describe(
                    prompt_version="paper-extraction-v1"
                ),
                "attempts": [],
            }
            current["record"]["llm_calls"].append(logical)
            active["logical"] = logical
            try:
                result = await real_generate(messages, schema, model=model)
                logical["status"] = "success"
                logical["validated_output_fields"] = result.model_dump(mode="json")
                return result
            except Exception as exc:
                logical["status"] = "failure"
                logical["error"] = error_fact(exc)
                raise
            finally:
                logical["ended_at"] = utc()
                logical["elapsed_seconds"] = time.perf_counter() - logical["start_perf"]
                active.pop("logical", None)
                write_json(Path(current["raw_path"]), current["record"])

        provider._request_json = request
        provider.generate_structured = generate
        return provider

    def observed_store(self, *args, **kwargs):
        fact = {"started_at": utc(), "start_perf": time.perf_counter()}
        if current:
            current["record"]["original_file_store"] = fact
        try:
            result = original_store(self, *args, **kwargs)
            fact["status"] = "written_candidate"
            fact["bytes"] = result.metadata.size_bytes
            return result
        except Exception as exc:
            fact["status"] = "failure"
            fact["error_type"] = type(exc).__name__
            raise
        finally:
            fact["ended_at"] = utc()
            fact["elapsed_seconds"] = time.perf_counter() - fact["start_perf"]

    def terminal_before_commit(session):
        # Retain only facts before flush clears strong dirty references. The
        # production Runner may assign a temporary _record(...).status, so the
        # weak identity map alone is insufficient after commit.
        if (
            not current
            or "identity" not in current
            or "terminal_perf" in current["record"]
        ):
            return
        for obj in list(session.dirty):
            if (
                isinstance(obj, PaperImport)
                and obj.id == current["identity"]
                and obj.status.value in ("Pending Review", "Failed")
            ):
                session.info["t160_observed_terminal"] = (
                    obj.id,
                    obj.status.value,
                    obj.error_code,
                    obj.error_message,
                )
                break

    def terminal_committed(session):
        candidate = session.info.pop("t160_observed_terminal", None)
        if (
            candidate is None
            or not current
            or candidate[0] != current.get("identity")
            or "terminal_perf" in current["record"]
        ):
            return
        current["record"].update(
            terminal_perf=time.perf_counter(),
            terminal_at=utc(),
            status=candidate[1],
            error_code=candidate[2],
            error_message=candidate[3],
        )
        write_json(Path(current["raw_path"]), current["record"])

    event.listen(Session, "before_commit", terminal_before_commit)
    event.listen(Session, "after_commit", terminal_committed)
    runner_module.create_llm_provider = observe_factory
    FileStorageService.store_document = observed_store
    with gr.Blocks() as shell:
        gr.Markdown("T160 import performance API test shell")
    app = create_app(settings=settings, gradio_app=shell)
    try:
        async with app.router.lifespan_context(app):
            with factory() as session:
                role = session.scalar(select(Role).where(Role.name == UserRole.TEACHER))
                if role is None:
                    role = Role(name=UserRole.TEACHER)
                    session.add(role)
                suffix = uuid4().hex[:12]
                teacher = User(
                    username="performance_fixture_" + suffix,
                    email=suffix + "@fixture.invalid",
                    password_hash="synthetic-no-password",
                    is_active=True,
                )
                teacher.roles.append(role)
                session.add(teacher)
                session.flush()
                course = Course(
                    name="T160 import performance " + suffix, created_by=teacher.id
                )
                session.add(course)
                session.commit()
                teacher_id, course_id = teacher.id, course.id
            raw = (
                SOURCE / "benchmark/corpus/v2-draft-20261001/inputs" / job["filename"]
            ).read_bytes()
            assert digest(raw) == job["sha256"]
            for trial in job["trials"]:
                record = {
                    "kind": "real-import-performance-run",
                    "teacher_annotation": False,
                    "case": job["case"],
                    "mode": trial["mode"],
                    "repetition": trial["repetition"],
                    "pid": os.getpid(),
                    "source_commit": SOURCE_COMMIT,
                    "input_sha256": job["sha256"],
                    "input_bytes": len(raw),
                    "page_count": job["page_count"],
                    "initial_state": "new import; not an idempotent completed operation",
                    "llm_calls": [],
                    "http_requests": [],
                    "provider": "deepseek",
                    "configured_model": settings.deepseek_model,
                    "prompt_version": "paper-extraction-v1",
                    "ocr": {
                        "enabled": True,
                        "provider": "rapidocr",
                        "model": "PP-OCRv5-mobile",
                        "default_ocr_argument": None,
                        "instance_reuse_across_imports": False,
                    },
                }
                raw_path = (
                    Path(job["directory"])
                    / "runs"
                    / (trial["mode"] + "-" + str(trial["repetition"]) + ".json")
                )
                if raw_path.exists():
                    raise RuntimeError("refuse to overwrite existing trial evidence")
                current.clear()
                current.update(record=record, raw_path=str(raw_path))
                record["receive_started_at"] = utc()
                record["receive_start_perf"] = time.perf_counter()
                write_json(raw_path, record)
                identity = None
                try:
                    with factory() as session:
                        view = PaperImportService(
                            session, root=settings.storage_root
                        ).upload(
                            course_id,
                            filename=job["filename"],
                            content=raw,
                            actor_id=teacher_id,
                        )
                    identity = view.id
                    current["identity"] = identity
                    record.update(
                        paper_import_id=str(identity),
                        teacher_fixture_id=str(teacher_id),
                        course_id=str(course_id),
                        upload_returned_at=utc(),
                        upload_end_perf=time.perf_counter(),
                    )
                    record["upload_seconds"] = (
                        record["upload_end_perf"] - record["receive_start_perf"]
                    )
                    runner = PaperImportRunner(factory, settings=settings)
                    await runner._run(identity)
                    record["runner_returned_at"] = utc()
                    record["runner_end_perf"] = time.perf_counter()
                    with factory() as session:
                        final = PaperImportService(
                            session, root=settings.storage_root
                        ).get(identity, actor_id=teacher_id)
                        record["persisted_output_fields"] = final.model_dump(
                            mode="json"
                        )
                    record["status"] = final.status.value
                    record["error_code"] = final.error_code
                    record["error_message"] = final.error_message
                    record["terminal_observed_by_after_commit"] = (
                        "terminal_perf" in record
                    )
                    record["terminal_elapsed_seconds"] = (
                        record["terminal_perf"] - record["receive_start_perf"]
                        if "terminal_perf" in record
                        else None
                    )
                    record["runner_elapsed_seconds"] = (
                        record["runner_end_perf"] - record["receive_start_perf"]
                    )
                    record["post_terminal_cleanup_seconds"] = (
                        record["runner_end_perf"] - record["terminal_perf"]
                        if "terminal_perf" in record
                        else None
                    )
                except Exception as exc:  # noqa: BLE001
                    # Retain the actual failure in its planned slot.
                    record["operation_error"] = {
                        "type": type(exc).__name__,
                        "code": getattr(exc, "code", None),
                    }
                    record.setdefault(
                        "status",
                        "Upload Failed" if identity is None else "Runner Exception",
                    )
                    record.setdefault("terminal_elapsed_seconds", None)
                    record["failed_elapsed_seconds"] = (
                        time.perf_counter() - record["receive_start_perf"]
                    )
                finally:
                    record["completed_at"] = utc()
                    record["completed_perf"] = time.perf_counter()
                    record["logical_call_count"] = len(record["llm_calls"])
                    record["provider_attempt_count"] = sum(
                        len(r["attempts"]) for r in record["llm_calls"]
                    )
                    record["actual_request_attempt_count"] = len(
                        record["http_requests"]
                    )
                    record["requested_models"] = sorted(
                        {
                            r["requested_model"]
                            for r in record["http_requests"]
                            if r.get("requested_model") is not None
                        }
                    )
                    record["response_models"] = sorted(
                        {
                            r["response_model"]
                            for r in record["http_requests"]
                            if r.get("response_model") is not None
                        }
                    )
                    record["business_success"] = (
                        record["status"] == "Pending Review"
                        and "operation_error" not in record
                    )
                    record["within_import_target"] = (
                        record["business_success"]
                        and record.get("terminal_elapsed_seconds") is not None
                        and record["terminal_elapsed_seconds"] < 300
                    )
                    write_json(raw_path, record)
                    print(
                        json.dumps(
                            {
                                "case": record["case"],
                                "mode": record["mode"],
                                "repetition": record["repetition"],
                                "status": record["status"],
                                "logical_calls": record["logical_call_count"],
                                "requests": record["actual_request_attempt_count"],
                            }
                        ),
                        flush=True,
                    )
    finally:
        event.remove(Session, "before_commit", terminal_before_commit)
        event.remove(Session, "after_commit", terminal_committed)
        runner_module.create_llm_provider = original_factory
        FileStorageService.store_document = original_store
        engine.dispose()


def launch_job(job, env, isolation):
    directory = Path(job["directory"])
    directory.mkdir(parents=True, exist_ok=False)
    write_json(directory / "job.json", job)
    log = (directory / "process.log").open("xb")
    process = None
    sampler = None
    actual_worker = None
    worker_chain = None
    started = utc()
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--repo-root",
                str(REPO),
                "--source-root",
                str(SOURCE),
                "--run-root",
                str(RUN),
                "--output-root",
                str(OUT),
                "--model-dir",
                str(MODEL_DIR),
                "--isolation-file",
                str(ISOLATION_FILE),
                "--source-commit",
                SOURCE_COMMIT,
                "--worker",
                str(directory / "job.json"),
            ],
            env=env,
            cwd=SOURCE,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        # Windows venv python.exe can be a redirector, not the executing process.
        handshake = directory / "actual-worker.json"
        deadline = time.perf_counter() + 30
        while not handshake.exists():
            if process.poll() is not None or time.perf_counter() >= deadline:
                raise RuntimeError("Actual worker handshake was not received")
            time.sleep(0.02)
        actual_worker = json.loads(handshake.read_text(encoding="utf-8"))
        worker_chain = verified_worker_chain(actual_worker["pid"], process.pid)
        sampler = ResourceSampler(
            pid=actual_worker["pid"],
            pg_container=isolation["pg_container"],
            redis_container=isolation["redis_container"],
            output_path=directory / "resources.jsonl",
        )
        sampler.start()
        ready_candidate = directory / ".controller-ready.json"
        write_json(
            ready_candidate,
            {
                "verified_worker_pid": actual_worker["pid"],
                "controller_popen_pid": process.pid,
                "sample_started_at": utc(),
            },
        )
        os.replace(ready_candidate, directory / "controller-ready.json")
        code = process.wait()
        sampler.stop()
        facts = {
            "pid": actual_worker["pid"],
            "controller_popen_pid": process.pid,
            "verified_worker_parent_chain": worker_chain,
            "worker_parent_pid_reported": actual_worker["parent_pid"],
            "started_at": started,
            "ended_at": utc(),
            "exit_code": code,
            "resources": sampler.summary(),
        }
        for path in sorted((directory / "runs").glob("*.json")):
            result = json.loads(path.read_text(encoding="utf-8"))
            # Report observations in the timed interval; short intervals may be incomplete.
            end = result.get("terminal_perf") or result.get("completed_perf")
            result["resources"] = sampler.summary(
                start_perf=result["receive_start_perf"], end_perf=end
            )
            write_json(path, result)
        write_json(directory / "process.json", facts)
        return facts
    finally:
        if process is not None and process.poll() is None:
            assert process.args == [
                sys.executable,
                str(Path(__file__).resolve()),
                "--repo-root",
                str(REPO),
                "--source-root",
                str(SOURCE),
                "--run-root",
                str(RUN),
                "--output-root",
                str(OUT),
                "--model-dir",
                str(MODEL_DIR),
                "--isolation-file",
                str(ISOLATION_FILE),
                "--source-commit",
                SOURCE_COMMIT,
                "--worker",
                str(directory / "job.json"),
            ]
            if actual_worker is not None and worker_chain is not None:
                verified_worker_chain(actual_worker["pid"], process.pid)
                subprocess.run(
                    ["taskkill", "/PID", str(actual_worker["pid"]), "/T", "/F"],
                    check=False,
                    capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            process.terminate()
            process.wait(timeout=20)
        if sampler is not None:
            sampler.stop()
        log.close()


def summarize(plan):
    rows = []
    for path in sorted(OUT.glob("*/runs/*.json")):
        result = json.loads(path.read_text(encoding="utf-8"))
        complete = all(
            k in result
            for k in (
                "completed_at",
                "business_success",
                "within_import_target",
                "logical_call_count",
                "provider_attempt_count",
                "actual_request_attempt_count",
                "resources",
            )
        )
        resources = result.get("resources") or {
            "complete_at_requested_sampling_resolution": False,
            "peaks": {
                key: None
                for key in (
                    "app_working_set_bytes",
                    "postgres_container_process_rss_bytes",
                    "redis_container_process_rss_bytes",
                    "simultaneous_observed_sum_bytes",
                )
            },
        }
        rows.append(
            {
                "case": result["case"],
                "mode": result["mode"],
                "repetition": result["repetition"],
                "pid": result["pid"],
                "status": result.get("status", "Incomplete"),
                "record_complete": complete,
                "business_success": complete and result.get("business_success", False),
                "configured_model": result["configured_model"],
                "requested_models": json.dumps(
                    result.get("requested_models", []), ensure_ascii=False
                ),
                "response_models": json.dumps(
                    result.get("response_models", []), ensure_ascii=False
                ),
                "terminal_elapsed_seconds": result.get("terminal_elapsed_seconds"),
                "upload_seconds": result.get("upload_seconds"),
                "logical_calls": result.get(
                    "logical_call_count",
                    len(result["llm_calls"]) if "llm_calls" in result else None,
                ),
                "provider_attempts": result.get(
                    "provider_attempt_count",
                    (
                        sum(len(c.get("attempts", [])) for c in result["llm_calls"])
                        if "llm_calls" in result
                        else None
                    ),
                ),
                "request_attempts": result.get(
                    "actual_request_attempt_count",
                    len(result["http_requests"]) if "http_requests" in result else None,
                ),
                "error_code": result.get("error_code"),
                "within_import_target": complete
                and result.get("within_import_target", False),
                "resource_complete": complete
                and resources["complete_at_requested_sampling_resolution"],
                **resources["peaks"],
                "raw_result": str(path.relative_to(REPO)),
            }
        )
    with (OUT / "timings.csv").open("x", newline="", encoding="utf-8-sig") as stream:
        fields = list(rows[0]) if rows else ["case", "mode", "status"]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    groups = []
    for sample in plan["cases"]:
        for mode, planned in (("cold", 3), ("warm", 5), ("warmup", 1)):
            matching = [
                r for r in rows if r["case"] == sample["case"] and r["mode"] == mode
            ]
            good = [
                r["terminal_elapsed_seconds"]
                for r in matching
                if r["business_success"] and r["terminal_elapsed_seconds"] is not None
            ]
            groups.append(
                {
                    "case": sample["case"],
                    "mode": mode,
                    "planned": planned,
                    "observed": len(matching),
                    "success": sum(r["business_success"] for r in matching),
                    "failed": sum(not r["business_success"] for r in matching),
                    "target_passed": sum(r["within_import_target"] for r in matching),
                    "successful_seconds": {
                        "min": min(good) if good else None,
                        "median": statistics.median(good) if good else None,
                        "max": max(good) if good else None,
                    },
                    "complete_at_resource_sampling_resolution": len(matching) == planned
                    and all(r["resource_complete"] for r in matching),
                }
            )
    measured = [r for r in rows if r["mode"] != "warmup"]
    summary = {
        "kind": "real-import-performance-summary",
        "completed_at": utc(),
        "planned_total": 27,
        "observed_total": len(rows),
        "measured_count": len(measured),
        "all_planned_business_success": len(rows) == 27
        and all(r["business_success"] for r in rows),
        "all_measured_within_300_seconds": len(measured) == 24
        and all(r["within_import_target"] for r in measured),
        "all_measured_resource_complete": len(measured) == 24
        and all(r["resource_complete"] for r in measured),
        "request_attempts": sum(
            r["request_attempts"] for r in rows if r["request_attempts"] is not None
        ),
        "provider_attempts": sum(
            r["provider_attempts"] for r in rows if r["provider_attempts"] is not None
        ),
        "logical_calls": sum(
            r["logical_calls"] for r in rows if r["logical_calls"] is not None
        ),
        "configured_model": plan["model"]["configured_model"],
        "requested_models": sorted(
            {m for r in rows for m in json.loads(r["requested_models"])}
        ),
        "response_models": sorted(
            {m for r in rows for m in json.loads(r["response_models"])}
        ),
        "model_alias_reason": "not inferred; configuration/requested/response models are independently recorded without a model-equality gate",
        "unobserved_planned_slots": [
            {"case": c["case"], "mode": mode, "repetition": n}
            for c in plan["cases"]
            for mode, count in (("cold", 3), ("warm", 5), ("warmup", 1))
            for n in range(1, count + 1)
            if not any(
                r["case"] == c["case"] and r["mode"] == mode and r["repetition"] == n
                for r in rows
            )
        ],
        "incomplete_records": sum(not r["record_complete"] for r in rows),
        "unknown_count_records": sum(
            any(
                r[key] is None
                for key in ("request_attempts", "provider_attempts", "logical_calls")
            )
            for r in rows
        ),
        "groups": groups,
        "physical_memory_or_exe_budget_proven": False,
        "teacher_annotation": False,
        "input_quality_acceptance": False,
    }
    write_json(OUT / "summary.json", summary)
    return summary


def execute(plan):
    env, isolation, _settings = isolated_env()
    if OUT.exists():
        raise RuntimeError("refuse to overwrite existing import-performance evidence")
    OUT.mkdir(parents=True)
    write_json(OUT / "plan.json", plan)
    process_facts = []
    try:
        for sample in plan["cases"]:
            for number in (1, 2, 3):
                job = {
                    **sample,
                    "directory": str(OUT / (sample["case"] + "-cold-" + str(number))),
                    "trials": [{"mode": "cold", "repetition": number}],
                }
                process_facts.append(launch_job(job, env, isolation))
            job = {
                **sample,
                "directory": str(OUT / (sample["case"] + "-warm-group")),
                "trials": [
                    {"mode": "warmup", "repetition": 1},
                    *[{"mode": "warm", "repetition": n} for n in range(1, 6)],
                ],
            }
            process_facts.append(launch_job(job, env, isolation))
    finally:
        write_json(OUT / "processes.json", process_facts)
        summary = summarize(plan)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


def main():
    global RUN, REPO, SOURCE, OUT, MODEL_DIR, ISOLATION_FILE, SOURCE_COMMIT
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=REPO)
    parser.add_argument("--source-root", type=Path, default=SOURCE)
    parser.add_argument("--run-root", type=Path, default=RUN)
    parser.add_argument("--output-root", type=Path, default=OUT)
    parser.add_argument("--model-dir", type=Path, default=MODEL_DIR)
    parser.add_argument("--isolation-file", type=Path, default=ISOLATION_FILE)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--plan-only", action="store_true")
    group.add_argument("--execute", action="store_true")
    group.add_argument("--worker")
    args = parser.parse_args()
    REPO = args.repo_root.resolve()
    SOURCE = args.source_root.resolve()
    RUN = args.run_root.resolve()
    OUT = args.output_root.resolve()
    MODEL_DIR = args.model_dir.resolve()
    ISOLATION_FILE = args.isolation_file.resolve()
    SOURCE_COMMIT = args.source_commit
    assert (
        REPO / "backend/app"
    ).is_dir(), "repo-root must identify an EduAgent workspace"
    assert SOURCE.is_relative_to(REPO), "source-root must remain in the named workspace"
    assert RUN.is_relative_to(
        REPO / ".cache"
    ), "run-root must remain in workspace cache"
    assert ISOLATION_FILE.is_relative_to(
        REPO / ".cache"
    ), "private isolation input must remain in workspace cache"
    assert (
        OUT.is_relative_to(REPO / "benchmark/results")
        and OUT.name == "import-performance"
    ), "output must be a dedicated import-performance child of benchmark/results"
    sys.path.insert(0, str(SOURCE))
    if args.worker:
        os.chdir(SOURCE)
        job = json.loads(Path(args.worker).read_text(encoding="utf-8"))
        directory = Path(job["directory"]).resolve()
        assert directory.is_relative_to(OUT)
        if any((directory / "runs").glob("*.json")):
            raise RuntimeError("Refuse to repeat an existing business-run group")
        target = directory / "actual-worker.json"
        if target.exists():
            raise RuntimeError("Refuse to replace an existing worker handshake")
        candidate = directory / (".actual-worker-" + str(os.getpid()) + ".json")
        with candidate.open("x", encoding="utf-8") as stream:
            json.dump(
                {
                    "pid": os.getpid(),
                    "parent_pid": os.getppid(),
                    "started_at": utc(),
                    "start_perf": time.perf_counter(),
                },
                stream,
            )
        # Publish only a complete JSON document; the controller can observe existence immediately.
        os.replace(candidate, target)
        # No model or database work can begin before the controller verifies ownership
        # and starts sampling this real worker, not its venv redirector.
        ready = directory / "controller-ready.json"
        deadline = time.perf_counter() + 30
        while not ready.exists():
            if time.perf_counter() >= deadline:
                raise RuntimeError(
                    "Verified controller sampling handshake was not received"
                )
            time.sleep(0.02)
        assert (
            json.loads(ready.read_text(encoding="utf-8"))["verified_worker_pid"]
            == os.getpid()
        )
        asyncio.run(worker_async(job))
        return
    plan = make_plan()
    if args.plan_only:
        app, app_error = windows_working_set(os.getpid())
        _env, isolation, _settings = isolated_env()
        pg, _pg_rows, pg_error = container_rss(isolation["pg_container"], "postgres")
        redis, _redis_rows, redis_error = container_rss(
            isolation["redis_container"], "redis-server"
        )
        plan["read_only_resource_probe"] = {
            "app_bytes": app,
            "postgres_bytes": pg,
            "redis_bytes": redis,
            "errors": {
                k: v
                for k, v in (
                    ("app", app_error),
                    ("postgres", pg_error),
                    ("redis", redis_error),
                )
                if v is not None
            },
            "scope": "observer setup verification only, not timed workload evidence",
        }
        write_json(RUN / "performance-plan.json", plan)
        print(
            json.dumps(
                {
                    "plan_only": True,
                    "cloud_calls": 0,
                    "imports": plan["total_imports"],
                    "base_logical_calls": plan[
                        "base_logical_calls_if_all_batches_reached"
                    ],
                    "resource_probe_errors": plan["read_only_resource_probe"]["errors"],
                    "plan": str(RUN / "performance-plan.json"),
                }
            ),
            flush=True,
        )
    else:
        execute(plan)


if __name__ == "__main__":
    main()
