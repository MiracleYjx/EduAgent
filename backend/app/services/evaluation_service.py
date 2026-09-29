"""Authorized, read-only projection of saved retrieval and grading Benchmarks."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite
from pathlib import Path
from typing import Any
from uuid import UUID

RETRIEVAL_UNITS = {
    "recall_at_5": "比例", "recall_at_10": "比例",
    "precision_at_5": "比例", "precision_at_10": "比例",
    "mrr": "比例", "ndcg_at_10": "比例", "latency_p95_ms": "毫秒",
}
GRADING_UNITS = {
    "mae": "分", "rmse": "分", "agreement_rate": "比例",
    "failure_rate": "比例", "elapsed_ms": "毫秒",
}
QUALITY_METRICS = frozenset({"mae", "rmse", "agreement_rate"})
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9_./:@+-]{1,128}$")
_SAFE_ERROR = re.compile(r"^[A-Z][A-Z0-9_]{2,95}$")
_SENSITIVE_MARKERS = ("authorization", "bearer", "secret", "password", "api_key")


@dataclass(frozen=True)
class EvaluationRecord:
    """A small, sanitized comparison row; never carries predictions or answers."""

    run_id: str
    experiment: str
    model: str
    prompt: str
    dataset: str
    retrieval_mode: str
    status: str
    metrics: Mapping[str, float | None] = field(default_factory=dict)
    metric_units: Mapping[str, str] = field(default_factory=dict)
    sample_count: int | None = None
    metric_sample_counts: Mapping[str, int | None] = field(default_factory=dict)
    model_version: str = ""
    prompt_version: str = ""
    dataset_version: str = ""
    configuration: str = ""
    run_at: str = ""
    result_path: str = ""
    failure_reason: str = ""
    comparison_condition: str = ""
    evidence_kind: str = "unknown"


def _label(value: object) -> str:
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    lowered = candidate.lower()
    if lowered.startswith("sk-") or any(marker in lowered for marker in _SENSITIVE_MARKERS):
        return ""
    return candidate if _SAFE_LABEL.fullmatch(candidate) else ""


def _error_code(value: object) -> str:
    return value if isinstance(value, str) and _SAFE_ERROR.fullmatch(value) else "BENCHMARK_FAILED"


def _status(value: object) -> str:
    return {"ok": "completed", "completed": "completed", "failed": "failed",
            "partial_failed": "failed", "running": "running"}.get(value, "unknown") if isinstance(value, str) else "unknown"


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)) or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _count(value: object) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number >= 0 and number.is_integer() else None


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def _timestamp(value: object) -> str:
    if not isinstance(value, str):
        return ""
    try:
        return datetime.fromisoformat(value).isoformat()
    except ValueError:
        return ""


def _evidence(raw: Mapping[str, Any], *, model: str, run_id: str) -> str:
    declared = raw.get("evidence_kind")
    if declared is None:
        declared = _mapping(raw.get("reproducibility")).get("evidence_kind")
    if (declared == "pipeline_selftest" or raw.get("mode") == "selftest"
            or "stub" in model.lower() or "selftest" in run_id.lower()):
        return "pipeline_selftest"
    return "provider_run" if declared == "provider_run" else "unknown"


def _query_signature(value: object) -> str:
    if not isinstance(value, list) or not value:
        return ""
    identifiers = [_label(item) for item in value]
    if not all(identifiers):
        return ""
    packed = json.dumps(sorted(identifiers), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(packed).hexdigest()[:12]


def _metrics(raw: Mapping[str, Any], units: Mapping[str, str], *, status: str) -> dict[str, float | None]:
    return {
        name: _number(raw.get(name)) if status == "completed" else None
        for name in units
    }


def _retrieval_record(raw: Mapping[str, Any], *, result_path: str) -> EvaluationRecord:
    run_id = _label(raw.get("run_id"))
    model = _label(raw.get("model_version"))
    mode = _label(raw.get("config"))
    status = _status(raw.get("status"))
    results = raw.get("results")
    count = len(results) if isinstance(results, list) else None
    environment = _mapping(raw.get("environment"))
    top_k = _count(environment.get("top_k"))
    reproducibility = _mapping(raw.get("reproducibility"))
    comparison = _mapping(reproducibility.get("comparison"))
    fingerprint = _label(comparison.get("input_fingerprint"))
    query_ids = comparison.get("query_ids")
    if query_ids is None and isinstance(results, list):
        query_ids = [_mapping(item).get("query_id") for item in results]
    query_signature = _query_signature(query_ids)
    condition = (
        f"同语料 {fingerprint[:12]} / 查询集 {query_signature} / Top-K {top_k}"
        if fingerprint and query_signature and top_k else
        f"同批次 {run_id} / 查询集 {query_signature} / Top-K {top_k}（历史记录）"
        if run_id and query_signature and top_k else ""
    )
    metrics = _metrics(_mapping(raw.get("metrics")), RETRIEVAL_UNITS, status=status)
    return EvaluationRecord(
        run_id=run_id, experiment=f"retrieval/{run_id or 'unknown'}",
        model=model, prompt=_label(raw.get("prompt_version")),
        dataset=_label(raw.get("dataset_version")), retrieval_mode=mode, status=status,
        metrics=metrics, metric_units=RETRIEVAL_UNITS, sample_count=count,
        metric_sample_counts={name: count for name in metrics},
        model_version=model, prompt_version=_label(raw.get("prompt_version")),
        dataset_version=_label(raw.get("dataset_version")),
        configuration=f"Top-K: {top_k}" if top_k else "",
        run_at=_timestamp(raw.get("run_at")), result_path=result_path,
        failure_reason=_error_code(raw.get("error_code")) if status == "failed" else "",
        comparison_condition=condition,
        evidence_kind=_evidence(raw, model=model, run_id=run_id),
    )


def _grading_records(raw: Mapping[str, Any], *, result_path: str) -> list[EvaluationRecord]:
    run_id = _label(raw.get("run_id"))
    model = _label(raw.get("model"))
    dataset = _mapping(raw.get("dataset"))
    dataset_name = _label(dataset.get("name"))
    dataset_version = _label(dataset.get("version"))
    total = _count(dataset.get("sample_count"))
    ground_truth = _mapping(raw.get("ground_truth"))
    evidence = _evidence(raw, model=model, run_id=run_id)
    runs = raw.get("runs")
    if not isinstance(runs, list) or not runs:
        runs = [{"strategy": "setup", "status": raw.get("status", "failed"),
                 "error_code": raw.get("error_code"), "metrics": {}}]
    projected: list[EvaluationRecord] = []
    for item in runs:
        if not isinstance(item, dict):
            continue
        strategy = _label(item.get("strategy"))
        status = _status(item.get("status"))
        raw_metrics = _mapping(item.get("metrics"))
        effective = _count(raw_metrics.get("effective_sample_count"))
        sample_total = _count(raw_metrics.get("sample_total"))
        if sample_total is None:
            sample_total = total
        metrics = _metrics(raw_metrics, GRADING_UNITS, status=status)
        if evidence != "provider_run" or ground_truth.get("teacher_labels_available") is not True or not effective:
            for name in QUALITY_METRICS:
                metrics[name] = None
        metrics["elapsed_ms"] = _number(item.get("elapsed_ms")) if status == "completed" else None
        counts = {name: (effective if name in QUALITY_METRICS else sample_total) for name in metrics}
        projected.append(EvaluationRecord(
            run_id=run_id, experiment=f"grading/{run_id or 'unknown'}", model=model,
            prompt=_label(raw.get("prompt_version")), dataset=dataset_name,
            retrieval_mode=strategy, status=status, metrics=metrics,
            metric_units=GRADING_UNITS, sample_count=sample_total,
            metric_sample_counts=counts, model_version=_label(raw.get("model_version")),
            prompt_version=_label(raw.get("prompt_version")), dataset_version=dataset_version,
            configuration=f"评分策略: {strategy}; 标注来源: {_label(dataset.get('label_source')) or 'unknown'}",
            run_at=_timestamp(raw.get("created_at")), result_path=result_path,
            failure_reason=_error_code(item.get("error_code") or raw.get("error_code")) if status == "failed" else "",
            comparison_condition=(f"同批次 {run_id} / {dataset_version} / 教师标签" if run_id and dataset_version and ground_truth.get("teacher_labels_available") is True else ""),
            evidence_kind=evidence,
        ))
    return projected


class EvaluationService:
    """Read fixed Benchmark files only after a trusted caller explicitly authorizes an actor.

    No role implicitly grants access. The production Gradio mount deliberately
    does not supply an authorizer or actor until a separate read contract exists.
    """

    def __init__(
        self, results_dir: Path, *, authorize: Callable[[UUID], bool] | None = None,
    ) -> None:
        self._results_dir = results_dir.resolve()
        self._authorize = authorize

    def query(
        self, *, actor_id: UUID | None = None, experiment: str = "", dataset: str = "",
        model: str = "", prompt: str = "", retrieval_mode: str = "",
    ) -> list[EvaluationRecord]:
        """Return sanitized comparisons; unauthorized calls never touch the filesystem."""

        if not self.is_authorized(actor_id):
            return []
        if not self._results_dir.is_dir():
            return []
        records = self._load_records()
        return [
            row for row in records
            if (not experiment or row.experiment == experiment)
            and (not dataset or row.dataset == dataset)
            and (not model or row.model == model)
            and (not prompt or row.prompt == prompt)
            and (not retrieval_mode or row.retrieval_mode == retrieval_mode)
        ]

    def is_authorized(self, actor_id: UUID | None) -> bool:
        """Fail closed; a role name alone is not a Benchmark read grant."""

        if actor_id is None or self._authorize is None:
            return False
        try:
            return self._authorize(actor_id)
        except Exception:  # noqa: BLE001 - authorization fails closed
            return False

    def _load_records(self) -> list[EvaluationRecord]:
        indexed: dict[tuple[str, str, str], EvaluationRecord] = {}
        for path in sorted(self._results_dir.rglob("retrieval_*.json")):
            if not self._is_local_file(path):
                continue
            for record in self._load_json(path, experiment="retrieval"):
                indexed[record.experiment, record.run_id, record.retrieval_mode] = record
        for path in sorted(self._results_dir.rglob("grading_*.json")):
            if not self._is_local_file(path):
                continue
            for record in self._load_json(path, experiment="grading"):
                indexed[record.experiment, record.run_id, record.retrieval_mode] = record
        for path in sorted(self._results_dir.rglob("retrieval_summary*.csv")):
            if not self._is_local_file(path):
                continue
            for record in self._load_csv(path, experiment="retrieval"):
                indexed.setdefault((record.experiment, record.run_id, record.retrieval_mode), record)
        for path in sorted(self._results_dir.rglob("grading_summary*.csv")):
            if not self._is_local_file(path):
                continue
            for record in self._load_csv(path, experiment="grading"):
                indexed.setdefault((record.experiment, record.run_id, record.retrieval_mode), record)
        return sorted(indexed.values(), key=lambda row: (row.experiment, row.run_id, row.retrieval_mode))

    def _is_local_file(self, path: Path) -> bool:
        return path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(self._results_dir)

    def _relative(self, path: Path) -> str:
        return path.relative_to(self._results_dir).as_posix()

    def _load_json(self, path: Path, *, experiment: str) -> list[EvaluationRecord]:
        result_path = self._relative(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise TypeError("Benchmark record must be an object")
        except (OSError, UnicodeError, ValueError, TypeError):
            return [EvaluationRecord(
                run_id=_label(path.stem), experiment=f"{experiment}/{_label(path.stem)}",
                model="", prompt="",
                dataset="", retrieval_mode="", status="failed",
                result_path=result_path, failure_reason="BENCHMARK_RECORD_INVALID",
            )]
        if experiment == "retrieval":
            return [_retrieval_record(raw, result_path=result_path)]
        return _grading_records(raw, result_path=result_path)

    def _load_csv(self, path: Path, *, experiment: str) -> list[EvaluationRecord]:
        try:
            with path.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
        except (OSError, UnicodeError, csv.Error):
            return []
        projected: list[EvaluationRecord] = []
        for raw in rows:
            # CSV is a fallback summary, never a source of unproven teacher labels.
            result_path = Path(raw.get("result_path") or path.name).name
            if experiment == "retrieval":
                projected.append(_retrieval_record({
                    **raw, "metrics": raw, "results": None,
                }, result_path=result_path))
            else:
                projected.extend(_grading_records({
                    "run_id": raw.get("run_id"), "created_at": raw.get("created_at"),
                    "mode": raw.get("mode"), "model": raw.get("model"),
                    "prompt_version": raw.get("prompt_version"),
                    "dataset": {"name": raw.get("dataset_version"), "version": raw.get("dataset_version"),
                                "sample_count": raw.get("sample_total")},
                    "ground_truth": {"teacher_labels_available": False},
                    "runs": [{"strategy": raw.get("strategy"), "status": raw.get("status"),
                              "error_code": raw.get("error_code"), "metrics": raw,
                              "elapsed_ms": raw.get("elapsed_ms")}],
                }, result_path=result_path))
        return projected
