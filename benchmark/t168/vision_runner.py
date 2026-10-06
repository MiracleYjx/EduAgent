"""T168 pixel baseline using the unchanged application Vision boundary.

Default mode only verifies local inputs and writes a run plan. --run is an
explicit cloud execution flag; no data labels are transmitted to the model.
The direct fixture run does not prove service authorization or persistence.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import httpx

from backend.app.ai.llm.base import describe_llm_provider
from backend.app.ai.vision.base import VisionFailure, VisionImage
from backend.app.ai.vision.provider import (
    VISION_PROMPT_VERSION,
    ProviderVisionUnderstanding,
    create_vision_provider,
)
from backend.app.core.config import get_settings
from backend.app.services.trace_service import (
    TraceService,
    bind_trace,
    trace_prompt_version,
)

DATASET = ROOT / "benchmark/corpus/t146-ai-authorized-20261003"
INPUT_PATH = DATASET / "image_annotations.json"
CASE_IDS = ("IMG-FIGURE", "IMG-TABLE", "IMG-DIAGRAM", "IMG-UNCLEAR")
TASK = "识别本题全部图片中的图示、表格与必要条件；不识别或补写答案。"
CONTEXT = {
    "content": "请读取所附题图中可见的条件，未知或不可靠的关键文字保留问题说明。",
    "options": None,
    "answer": None,
    "analysis": None,
    "caption": [None],
}


def utc() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class EvidenceTrace(TraceService):
    """Collect existing provider trace events without touching a database."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record(self, **kwargs: Any) -> bool:
        context = kwargs.pop("context")
        self.events.append({"recorded_at": utc(), "request_id": context.request_id, **kwargs})
        return True


def inputs() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    annotations = json.loads(INPUT_PATH.read_text(encoding="utf-8"))
    reference_index = json.loads((DATASET / "annotations.json").read_text(encoding="utf-8"))
    review = json.loads((DATASET / "developer_review.json").read_text(encoding="utf-8"))
    if reference_index.get("acceptance_decision") != "accepted_developer_review_ai_assisted":
        raise ValueError("Current reference index has not accepted the developer-reviewed AI-assisted baseline")
    if review.get("is_independent_teacher_truth") is not False:
        raise ValueError("This runner only supports the declared learning-project baseline")
    annotations["baseline_reference"] = {
        "reference_standard": reference_index["reference_standard"],
        "baseline_annotation_version": reference_index["annotation_version"],
        "annotation_authors": review["annotation_authors"],
        "decision_source": review["decision_source"],
        "decision_recorded_at_utc": review["recorded_at_utc"],
        "is_independent_teacher_truth": False,
        "source_sha256": {name: digest((DATASET / name).read_bytes()) for name in ("annotations.json", "developer_review.json", "manifest.json")},
    }
    cases = {item["case_id"]: item for item in annotations["entries"]}
    verified = []
    for case_id in CASE_IDS:
        entry = cases[case_id]
        images = []
        for index, item in enumerate(entry["image_inputs"], 1):
            path = (ROOT / item["source_file"]).resolve()
            path.relative_to(ROOT)
            data = path.read_bytes()
            image = VisionImage.from_bytes(data)
            if digest(data) != item["source_sha256"]:
                raise ValueError(f"Input digest changed: {case_id}/{index}")
            if [image.width, image.height] != item["actual_size_pixels"]:
                raise ValueError(f"Input pixels changed: {case_id}/{index}")
            images.append({
                "image_index": index,
                "fixture_asset_id": item["asset_id"],
                "identity_scope": "stable fixture only; no database asset/file identity",
                "path": item["source_file"],
                "sha256": digest(data),
                "size_bytes": len(data),
                "width": image.width,
                "height": image.height,
                "mime_type": image.mime_type,
            })
        verified.append({"case_id": case_id, "images": images})
    return annotations, verified


async def execute(output: Path, plan: dict[str, Any], annotations: dict[str, Any]) -> None:
    settings = get_settings()
    provider = create_vision_provider(settings)
    vision = ProviderVisionUnderstanding(provider)
    trace = EvidenceTrace()
    metadata = describe_llm_provider(provider, prompt_version=VISION_PROMPT_VERSION)
    entries = {row["case_id"]: row for row in annotations["entries"]}
    rows: list[dict[str, Any]] = []
    raw_responses: list[dict[str, Any]] = []
    prepare = getattr(provider, "_request_client", None)
    if callable(prepare):
        prepare()  # Bind observation before the first authorized request.
    sdk = getattr(provider, "_client", None)
    http_client = getattr(sdk, "_client", None)
    secret_value = getattr(sdk, "api_key", None)
    secret = secret_value if isinstance(secret_value, str) and len(secret_value) >= 8 else None

    def redact(value: Any) -> Any:
        if isinstance(value, str):
            return value.replace(secret, "[REDACTED]") if secret else value
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, dict):
            return {key: redact(item) for key, item in value.items()}
        return value

    async def capture_response(response: httpx.Response) -> None:
        captured: dict[str, Any] = {
            "received_at_utc": utc(),
            "status_code": response.status_code,
            "capture_scope": "response identity, usage, actual choices only; no headers/URL/credentials",
        }
        try:
            # Read-only httpx hook: cached bytes are then consumed by the unchanged SDK.
            await response.aread()
            body = response.json()
            if isinstance(body, dict):
                for key in ("id", "model", "system_fingerprint", "usage", "choices"):
                    if key in body:
                        captured[key] = redact(body[key])
        except Exception as error:  # noqa: BLE001 -- Evidence cannot change provider behavior.
            captured["capture_error_kind"] = type(error).__name__
        raw_responses.append(captured)

    raw_capture_status = "unavailable_for_this_adapter"
    if isinstance(http_client, httpx.AsyncClient):
        http_client.event_hooks.setdefault("response", []).append(capture_response)
        raw_capture_status = "read_only_http_response_hook"
    save(output / "actual_provider.json", {
        **metadata,
        "supports_vision": provider.supports_vision(),
        "call_boundary": "ProviderVisionUnderstanding.understand",
        "default_timeout_seconds": 30,
        "max_network_retries": 2,
        "max_schema_retries": 1,
        "quality_reference_origin": "AI-assisted + developer review (learning project)",
        "teacher_metrics": None,
        "service_authorization_and_persistence_evaluated": False,
        "raw_capture_status": raw_capture_status,
    })
    try:
        for call in plan["calls"]:
            case_id, round_no, run_id = call["case_id"], call["round"], call["run_id"]
            images = [VisionImage.from_bytes((ROOT / row["path"]).read_bytes()) for row in call["images"]]
            record: dict[str, Any] = {
                "case_id": case_id,
                "round": round_no,
                "run_id": run_id,
                "started_at": utc(),
                "images": call["images"],
                "context": CONTEXT,
                "task": TASK,
                "output": None,
                "error": None,
                "provenance": None,
                "assessment_status": "pending_semantic_review",
                "reference_condition_count": len(entries[case_id]["labels"].get("key_conditions") or []),
                "is_independent_teacher_truth": False,
                "baseline_reference": annotations["baseline_reference"],
            }
            started = perf_counter()
            trace_start = len(trace.events)
            raw_start = len(raw_responses)
            try:
                with (
                    bind_trace(request_id=run_id, user_id=None, workflow_id=None, service=trace),
                    trace_prompt_version(VISION_PROMPT_VERSION),
                ):
                    result = await vision.understand(context=CONTEXT, images=images, task=TASK)
                record["output"] = result.model_dump(mode="json")
                record["outcome"] = "completed"
                record["provenance"] = result.provenance.model_dump(mode="json") if result.provenance else None
            except VisionFailure as error:
                record["outcome"] = "technical_error"
                record["error"] = {
                    "code": error.code,
                    "message": error.message,
                    "stage": error.stage,
                    "cause": error.cause,
                    "retryable": error.retryable,
                }
                record["provenance"] = error.provenance.model_dump(mode="json") if error.provenance else None
            except asyncio.CancelledError:
                record["outcome"] = "cancelled"
                record["error"] = {"code": "VISION_CANCELLED", "stage": "call", "cause": "CancelledError"}
                raise
            except Exception as error:
                record["outcome"] = "runner_error"
                record["error"] = {"code": "VISION_RUNNER_ERROR", "stage": "runner", "cause": type(error).__name__}
                raise
            finally:
                record["completed_at"] = utc()
                record["elapsed_ms"] = round((perf_counter() - started) * 1000, 3)
                record["provider_trace_events"] = trace.events[trace_start:]
                record["observed_provider_attempts"] = len(record["provider_trace_events"])
                record["actual_raw_responses"] = raw_responses[raw_start:]
                record["raw_capture_status"] = raw_capture_status
                save(output / f"{run_id}.json", record)
                rows.append(record)
                save(output / "runs.json", rows)
            print(json.dumps({"case_id": case_id, "round": round_no, "outcome": record["outcome"], "elapsed_ms": record["elapsed_ms"]}), flush=True)
    finally:
        if isinstance(http_client, httpx.AsyncClient):
            hooks = http_client.event_hooks.get("response", [])
            if capture_response in hooks:
                hooks.remove(capture_response)
        await provider.aclose()
        save(output / "provider_trace_events.json", trace.events)
        save(output / "provider_raw_responses.json", raw_responses)

    save(output / "execution_summary.json", {
        "planned_calls": len(plan["calls"]),
        "completed_calls": sum(row["outcome"] == "completed" for row in rows),
        "failed_calls": sum(row["outcome"] == "technical_error" for row in rows),
        "observed_provider_attempts": len(trace.events),
        "semantic_comparison_status": "pending_review",
        "correct_condition_count": None,
        "condition_precision": None,
        "condition_completeness": None,
        "clear_reference_condition_denominator": 33,
        "unclear_condition_reference": None,
        "unclear_handoff_review_status": "pending_review",
        "teacher_post_correction_metrics": None,
        "fault_cases_evaluated": [],
    })
    worksheet = []
    for row in rows:
        labels = entries[row["case_id"]]["labels"]
        for condition in labels.get("key_conditions") or []:
            worksheet.append({
                "run_id": row["run_id"], "case_id": row["case_id"], "round": row["round"],
                "reference_key": condition["annotation_condition_key"], "reference_text": condition["text"],
                "matched_output_condition_index": None, "correct": None, "reason": None,
            })
    save(output / "condition_review.pending.json", worksheet)
    with (output / "runs.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["run_id", "case_id", "round", "outcome", "elapsed_ms", "observed_provider_attempts"])
        writer.writeheader()
        writer.writerows({name: row[name] for name in writer.fieldnames} for row in rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", "--execute", dest="run", action="store_true", help="Perform exactly the frozen 12 application calls, including existing policy retries.")
    parser.add_argument("--output-dir", "--output", dest="output_dir", type=Path, required=True)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    allowed = (ROOT / ".cache", ROOT / "benchmark/results")
    if not any(output.is_relative_to(base) for base in allowed):
        raise ValueError("Evidence output must be within .cache or benchmark/results")
    output.mkdir(parents=True, exist_ok=False)
    annotations, verified = inputs()
    annotation_bytes = INPUT_PATH.read_bytes()
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    plan = {
        "schema_version": "t168-vision-pixel-1",
        "dataset_path": "benchmark/corpus/t146-ai-authorized-20261003",
        "annotation_version": annotations["annotation_version"],
        "annotation_sha256": digest(annotation_bytes),
        "baseline_reference": annotations["baseline_reference"],
        "annotation_origin": annotations["baseline_reference"]["reference_standard"],
        "revision": revision,
        "created_at": utc(),
        "runner_sha256": digest(Path(__file__).read_bytes()),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "prompt_version": VISION_PROMPT_VERSION,
        "context": CONTEXT,
        "task": TASK,
        "rounds": 3,
        "cloud_execution_requested": args.run,
        "clear_condition_denominator": 33,
        "unknown_condition_reference": None,
        "condition_matching_rule": "Reviewed semantic equivalence plus correct image_index; strict one-to-one. Composite outputs cannot match multiple reference conditions; duplicate outputs cannot repeat a match.",
        "scope": "Application Vision entry point + actual fixture pixels; no service authorization/persistence or independent teacher quality claim.",
        "calls": [
            {"round": round_no, "case_id": case["case_id"], "run_id": f"t168-vision-r{round_no}-{case['case_id'].lower()}-{uuid4().hex[:12]}", "images": case["images"]}
            for round_no in range(1, 4) for case in verified
        ],
        "source_digests": {
            name: digest((ROOT / name).read_bytes())
            for name in (
                "backend/app/ai/vision/provider.py", "backend/app/ai/vision/base.py",
                "backend/app/ai/llm/deepseek.py", "backend/app/core/retry_policy.py",
            )
        },
    }
    save(output / "plan.json", plan)
    if args.run:
        asyncio.run(execute(output, plan, annotations))
    else:
        print(json.dumps({"mode": "dry_run", "planned_calls": len(plan["calls"]), "clear_condition_denominator": 33, "cloud_calls_performed": 0}, ensure_ascii=True))


if __name__ == "__main__":
    main()
