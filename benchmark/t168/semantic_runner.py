"""T168 real semantic provider evaluation, no business writes.

Default preflight has no provider initialization/network requests. --execute is
required for real calls. Unknown/not-applicable labels and abstentions are never
TN. Source fixtures are not database objects or independent teacher references.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import httpx
from pydantic import BaseModel, ValidationError

from backend.app.ai.llm.base import BaseLLMProvider, LLMMessages, LLMProviderMetadata
from backend.app.ai.llm.factory import create_llm_provider
from backend.app.ai.semantic_validation import (
    SEMANTIC_PROMPT_VERSION,
    ProviderSemanticValidator,
    SemanticExecutionFailure,
)
from backend.app.schemas.content_validation import (
    CheckKind,
    SemanticMachineOutput,
    SemanticQuestionFields,
    SemanticValidationInput,
    ValidationEvidence,
)
from backend.app.services.trace_service import bind_trace

KINDS: tuple[CheckKind, ...] = (
    "answer_correctness",
    "condition_sufficiency",
    "option_ambiguity",
    "rubric_clarity",
)
LABELS_DEFAULT = (
    ROOT / "benchmark/corpus/t146-ai-authorized-20261003/semantic_annotations.json"
)


def utc() -> str:
    return datetime.now(UTC).isoformat()


def save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
    )


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture_uuid(key: str) -> UUID:
    return uuid5(NAMESPACE_URL, "eduagent/t168/synthetic-fixture/" + key)


def build_input(
    entry: dict[str, Any], repeat: int, overrides: dict[str, Any]
) -> SemanticValidationInput:
    case_id = entry["case_id"]
    if case_id in overrides:
        # Explicit runtime projection supplied by operator; never inferred from labels.
        context = overrides[case_id]
        if context["input"] is None:
            raise ValueError(context["input_block_reason"])
        runtime = SemanticValidationInput.model_validate(context["input"])
        source = entry["input_snapshot"]
        if (
            runtime.fields.content != source.get("content", source.get("question"))
            or runtime.fields.reference_answer != source["reference_answer"]
            or runtime.fields.scoring_rubric != source["scoring_rubric"]
        ):
            raise ValueError("RUNTIME_CHANGED_SOURCE_CONTENT_ANSWER_OR_RUBRIC")
        return runtime.model_copy(update={"run_no": repeat})
    source = entry["input_snapshot"]
    required = ("question_type", "score", "options", "analysis")
    missing = [name for name in required if name not in source]
    if missing:
        raise ValueError("SOURCE_FIELDS_MISSING:" + ",".join(missing))
    fields = SemanticQuestionFields.model_validate(
        {
            "type": source["question_type"],
            "content": source.get("content", source.get("question")),
            "options": source["options"],
            "reference_answer": source["reference_answer"],
            "scoring_rubric": source["scoring_rubric"],
            "analysis": source["analysis"],
            "score": source["score"],
        }
    )
    basis_ref = next(
        (ref for ref in entry["source_refs"] if ref.endswith("teaching_basis.md")), None
    )
    evidence = []
    if basis_ref is not None:
        basis_path = (ROOT / basis_ref).resolve()
        basis_path.relative_to(ROOT)
        source_id = fixture_uuid(basis_ref)
        evidence = [
            ValidationEvidence(
                evidence_id=fixture_uuid(case_id + "/teaching-evidence"),
                kind="chunk",
                source_id=source_id,
                source_data={
                    "chunk_id": str(source_id),
                    "document_id": str(fixture_uuid(basis_ref + "/document")),
                    "course_id": str(fixture_uuid("synthetic-course")),
                    "source_file": basis_ref,
                    "location": {
                        "kind": "synthetic_whole_fixture",
                        "database_record": False,
                    },
                    "content_snapshot": basis_path.read_text(encoding="utf8"),
                },
            )
        ]
    return SemanticValidationInput(
        question_id=fixture_uuid(case_id),
        input_revision=0,
        run_no=repeat,
        fields=fields,
        evidence=evidence,
        manual_context=[],
    )


class MemoryTrace:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record(self, **kwargs: Any) -> bool:
        # record_trace already exposes only application metadata, never credentials.
        event = {key: value for key, value in kwargs.items() if key != "context"}
        event["observed_at_utc"] = utc()
        self.events.append(event)
        return True


class RecordingProvider(BaseLLMProvider):
    def __init__(self, actual: BaseLLMProvider) -> None:
        self.actual = actual
        self.messages: list[dict[str, Any]] = []
        self.raw_responses: list[dict[str, Any]] = []
        prepare = getattr(actual, "_request_client", None)
        if callable(prepare):
            prepare()  # Bind observation before the first authorized request.
        sdk = getattr(actual, "_client", None)
        http_client = getattr(sdk, "_client", None)
        secret = getattr(sdk, "api_key", None)
        self._secret = secret if isinstance(secret, str) and len(secret) >= 8 else None
        self.raw_capture_status = "unavailable_for_this_adapter"
        if isinstance(http_client, httpx.AsyncClient):
            http_client.event_hooks.setdefault("response", []).append(
                self._capture_response
            )
            self.raw_capture_status = "read_only_http_response_hook"

    def _redact(self, value: Any) -> Any:
        if isinstance(value, str):
            return value.replace(self._secret, "[REDACTED]") if self._secret else value
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, dict):
            return {key: self._redact(item) for key, item in value.items()}
        return value

    async def _capture_response(self, response: httpx.Response) -> None:
        record: dict[str, Any] = {
            "received_at_utc": utc(),
            "status_code": response.status_code,
            "capture_scope": "response identity, usage, actual choices only; no headers/URL/credentials",
        }
        try:
            # httpx caches the read body; the actual SDK consumes the same bytes.
            await response.aread()
            body = response.json()
            if isinstance(body, dict):
                for key in ("id", "model", "system_fingerprint", "usage", "choices"):
                    if key in body:
                        record[key] = self._redact(body[key])
        except Exception as error:  # noqa: BLE001 -- Evidence capture never changes provider behavior.
            record["capture_error_kind"] = type(error).__name__
        self.raw_responses.append(record)

    def describe(self, *, prompt_version: str | None = None) -> LLMProviderMetadata:
        return self.actual.describe(prompt_version=prompt_version)

    async def generate_structured(
        self, messages: LLMMessages, schema: type[BaseModel], model: str | None = None
    ) -> BaseModel:
        self.messages = [dict(message) for message in messages]
        self.raw_responses = []
        return await self.actual.generate_structured(messages, schema, model=model)

    async def aclose(self) -> None:
        await self.actual.aclose()


def confusion(records: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for repeat in range(1, 4):
        per_round: dict[str, Any] = {}
        for kind in KINDS:
            selected = [
                row
                for row in records
                if row["repeat_index"] == repeat and row["check_kind"] == kind
            ]
            eligible = [
                row
                for row in selected
                if row["applicable"] and row["expected_problem"] is not None
            ]
            assessed = [row for row in eligible if row["predicted_problem"] is not None]
            counts = {name: 0 for name in ("TP", "FP", "TN", "FN")}
            for row in assessed:
                key = (
                    ("TP" if row["expected_problem"] else "FP")
                    if row["predicted_problem"]
                    else ("FN" if row["expected_problem"] else "TN")
                )
                counts[key] += 1
            tp, fp, tn, fn = (counts[key] for key in ("TP", "FP", "TN", "FN"))
            ratio = lambda n, d: n / d if d else None
            per_round[kind] = {
                **counts,
                "planned_checks": len(selected),
                "known_applicable_labels": len(eligible),
                "assessed": len(assessed),
                "not_applicable": sum(not row["applicable"] for row in selected),
                "unknown_labels": sum(
                    row["applicable"] and row["expected_problem"] is None
                    for row in selected
                ),
                "input_not_ready": sum(
                    row["status"] == "input_not_ready" for row in eligible
                ),
                "technical_failed": sum(
                    row["status"] == "technical_error" for row in eligible
                ),
                "abstained": sum(row["status"] == "abstained" for row in eligible),
                "not_executed": sum(
                    row["status"] == "not_executed" for row in eligible
                ),
                "coverage": ratio(len(assessed), len(eligible)),
                "recall": ratio(tp, tp + fn),
                "false_negative_rate": ratio(fn, tp + fn),
                "false_positive_rate": ratio(fp, fp + tn),
                "accuracy_on_assessed": ratio(tp + tn, len(assessed)),
            }
        result[str(repeat)] = per_round
    return result


def validate_context_sources(
    labels: dict[str, Any], index: dict[str, Any], runtime: dict[str, Any]
) -> None:
    """Pin this reference dataset, never enforce unrelated release lockstep."""
    if index.get("acceptance_decision") != "accepted_developer_review_ai_assisted":
        raise ValueError("REFERENCE_STANDARD_NOT_ACCEPTED")
    if index.get("is_independent_teacher_truth") is not False:
        raise ValueError("REFERENCE_PROVENANCE_CHANGED")
    if runtime.get("root_annotation_version") != index["annotation_version"]:
        raise ValueError("RUNTIME_ROOT_REFERENCE_VERSION_CHANGED")
    if runtime.get("semantic_annotation_version") != labels["annotation_version"]:
        raise ValueError("RUNTIME_COMPONENT_REFERENCE_VERSION_CHANGED")
    if runtime.get("schema_version") != "t168-semantic-runtime-context-1":
        raise ValueError("RUNTIME_CONTEXT_SCHEMA_UNSUPPORTED")
    if not runtime.get("source_snapshots"):
        raise ValueError("RUNTIME_CONTEXT_SOURCE_SNAPSHOTS_MISSING")
    for snapshot in runtime["source_snapshots"]:
        path = (ROOT / snapshot["path"]).resolve()
        path.relative_to(ROOT)
        if digest(path) != snapshot["sha256"]:
            raise ValueError("RUNTIME_CONTEXT_SOURCE_CHANGED:" + snapshot["path"])
    case_ids = {entry["case_id"] for entry in labels["entries"]}
    if set(runtime["cases"]) != case_ids:
        raise ValueError("RUNTIME_CONTEXT_CASE_SET_CHANGED")


def create_new_output(path: str) -> Path:
    output = Path(path).resolve()
    allowed = ((ROOT / ".cache").resolve(), (ROOT / "benchmark/results").resolve())
    if not any(output.is_relative_to(parent) for parent in allowed):
        raise ValueError("OUTPUT_MUST_BE_WITHIN_CACHE_OR_BENCHMARK_RESULTS")
    # A pre-existing slot is historical evidence and must never be replaced.
    output.mkdir(parents=True, exist_ok=False)
    return output


async def main(args: argparse.Namespace) -> None:
    labels_path = Path(args.labels).resolve()
    data = json.loads(labels_path.read_text(encoding="utf8"))
    entries = data["entries"]
    git_snapshot = await asyncio.to_thread(
        subprocess.run,
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    runtime_file = (
        json.loads(Path(args.runtime_inputs).read_text(encoding="utf8"))
        if args.runtime_inputs
        else {}
    )
    overrides = runtime_file.get("cases", {})
    root_annotations_path = labels_path.parent / "annotations.json"
    root_annotations = json.loads(root_annotations_path.read_text(encoding="utf8"))
    validate_context_sources(data, root_annotations, runtime_file)
    output = create_new_output(args.output)
    provider: RecordingProvider | None = None
    rows: list[dict[str, Any]] = []
    try:
        for repeat in range(1, 4):
            for entry in entries:
                case_id = entry["case_id"]
                run_id = f"semantic-r{repeat}-{case_id}"
                run: dict[str, Any] = {
                    "run_id": run_id,
                    "repeat_index": repeat,
                    "case_id": case_id,
                    "stage": "automatic",
                    "evidence_kind": "real_provider"
                    if args.execute
                    else "input_preflight",
                    "baseline_origin": "AI-assisted + developer review",
                    "is_independent_teacher_truth": False,
                    "source_labels": str(labels_path.relative_to(ROOT)).replace(
                        "\\", "/"
                    ),
                    "labels_sha256": digest(labels_path),
                    "annotation_version": root_annotations["annotation_version"],
                    "component_annotation_version": data["annotation_version"],
                    "developer_review_ref": root_annotations.get(
                        "developer_review_ref"
                    ),
                    "runtime_context_provenance": overrides.get(case_id, {}).get(
                        "context_provenance"
                    ),
                    "generation_path": entry["input_snapshot"]["generation_path"],
                    "synthetic_identities_not_persisted": True,
                    "started_at_utc": utc(),
                    "model_output": None,
                    "technical_error": None,
                    "trace_attempts": [],
                    "actual_request_count": 0,
                }
                started = perf_counter()
                machine: SemanticMachineOutput | None = None
                try:
                    value = build_input(entry, repeat, overrides)
                    run["model_input"] = value.model_dump(mode="json")
                except (ValueError, KeyError, ValidationError) as error:
                    run["status"] = "input_not_ready"
                    run["input_error"] = (
                        error.errors(include_input=False)
                        if isinstance(error, ValidationError)
                        else str(error)
                    )
                else:
                    if not args.execute:
                        run["status"] = "not_executed"
                    else:
                        if provider is None:
                            provider = RecordingProvider(create_llm_provider())
                        sink = MemoryTrace()
                        validator = ProviderSemanticValidator(provider)
                        with bind_trace(
                            request_id=run_id,
                            user_id=None,
                            workflow_id=None,
                            service=cast(Any, sink),
                        ):
                            try:
                                machine = await validator.validate(value)
                                run["status"] = "completed"
                                run["model_output"] = machine.model_dump(mode="json")
                            except SemanticExecutionFailure as error:
                                run["status"] = "technical_error"
                                run["technical_error"] = error.error.model_dump(
                                    mode="json"
                                )
                        run["actual_messages"] = provider.messages
                        run["actual_raw_responses"] = provider.raw_responses
                        run["raw_capture_status"] = provider.raw_capture_status
                        run["provider_provenance"] = (
                            validator.call_provenance.model_dump(mode="json")
                            if validator.call_provenance
                            else None
                        )
                        run["trace_attempts"] = sink.events
                        run["actual_request_count"] = sum(
                            event["agent_type"] == "llm" for event in sink.events
                        )
                run["completed_at_utc"] = utc()
                run["elapsed_ms"] = (perf_counter() - started) * 1000
                save(output / "runs" / f"{run_id}.json", run)
                checks = (
                    {check.kind: check for check in machine.checks} if machine else {}
                )
                for kind in KINDS:
                    annotation = entry["checks"][kind]
                    check = checks.get(kind)
                    predicted = (
                        (check.verdict == "fail")
                        if check and check.verdict in {"pass", "fail"}
                        else None
                    )
                    rows.append(
                        {
                            "run_id": run_id,
                            "repeat_index": repeat,
                            "case_id": case_id,
                            "stage": "automatic",
                            "check_kind": kind,
                            "applicable": annotation["applicable"],
                            "expected_problem": entry["problem_labels"][kind],
                            "predicted_problem": predicted,
                            "model_verdict": check.verdict if check else None,
                            "status": "abstained"
                            if check and predicted is None
                            else run["status"],
                            "reason": check.reason if check else None,
                            "evidence_ref": f"runs/{run_id}.json",
                        }
                    )
                print(
                    json.dumps(
                        {
                            "run_id": run_id,
                            "status": run["status"],
                            "actual_request_count": run["actual_request_count"],
                        }
                    ),
                    flush=True,
                )
    finally:
        if provider is not None:
            await provider.aclose()
    save(output / "semantic_checks.json", rows)
    save(output / "semantic_metrics.json", confusion(rows))
    with (output / "semantic_checks.csv").open(
        "w", encoding="utf8", newline=""
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    save(
        output / "semantic_manifest.json",
        {
            "task": "T168",
            "protocol_version": "v2-evaluation-1",
            "recorded_at_utc": utc(),
            "planned_runs": len(entries) * 3,
            "rounds": 3,
            "case_count": len(entries),
            "labels_path": str(labels_path),
            "labels_sha256": digest(labels_path),
            "annotation_version": root_annotations["annotation_version"],
            "component_annotation_version": data["annotation_version"],
            "root_annotation_sha256": digest(root_annotations_path),
            "developer_review_ref": root_annotations.get("developer_review_ref"),
            "runtime_version": runtime_file.get("runtime_version"),
            "runner_sha256": digest(Path(__file__)),
            "code_git_commit": git_snapshot.stdout.strip(),
            "runtime_inputs_path": args.runtime_inputs,
            "runtime_inputs_sha256": digest(Path(args.runtime_inputs))
            if args.runtime_inputs
            else None,
            "prompt_version": SEMANTIC_PROMPT_VERSION,
            "semantic_module_sha256": digest(
                ROOT / "backend/app/ai/semantic_validation.py"
            ),
            "source_file_sha256": {
                path: digest(ROOT / path)
                for path in (
                    "backend/app/ai/semantic_validation.py",
                    "backend/app/schemas/content_validation.py",
                    "backend/app/ai/llm/factory.py",
                    "backend/app/ai/llm/deepseek.py",
                    "backend/app/core/retry_policy.py",
                )
            },
            "quality_thresholds": None,
            "threshold_status": "pending_confirmation",
            "run_performed": args.execute,
            "metric_rule": "fail=positive;pass=negative;needs_review/insufficient_evidence=abstain;NA/unknown labels excluded;technical/input failures never TN",
            "acceptance_scope": "provider semantic baseline only; no business persistence/approval",
            "model_identity_note": "actual instance metadata, remote model revision unknown unless returned",
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--labels", default=str(LABELS_DEFAULT))
    parser.add_argument("--output", required=True)
    parser.add_argument("--runtime-inputs", required=True)
    parser.add_argument("--execute", action="store_true")
    asyncio.run(main(parser.parse_args()))
