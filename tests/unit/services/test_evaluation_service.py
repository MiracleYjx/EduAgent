"""T086: authorized, truthful projections of saved Benchmark artifacts."""

import csv
import json
from pathlib import Path
from uuid import UUID, uuid4

from backend.app.services.evaluation_service import EvaluationService


def _retrieval(
    directory: Path, *, run_id: str = "run-a", mode: str = "hybrid",
    status: str = "ok", evidence: str = "provider_run",
    metrics: dict[str, float | None] | None = None,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id, "run_at": "2026-09-20T12:00:00+00:00",
        "config": mode, "dataset_version": "dataset-v1",
        "model_version": "embedding-a", "prompt_version": "prompt-v1",
        "status": status, "error_code": "EMBEDDING_PROVIDER_NOT_READY" if status == "failed" else None,
        "environment": {"top_k": 10},
        "metrics": metrics if metrics is not None else {"recall_at_5": 0.8, "latency_p95_ms": 12.5},
        "results": [{"query_id": "q1"}, {"query_id": "q2"}],
        "reproducibility": {
            "evidence_kind": evidence,
            "comparison": {"input_fingerprint": "input-abc", "query_ids": ["q1", "q2"]},
        },
    }
    (directory / f"retrieval_{run_id}_{mode}.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )


def _grading(directory: Path, *, mode: str = "real", teacher_labels: bool = False) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": "grade-a", "created_at": "2026-09-20T12:00:00+00:00",
        "mode": mode, "evidence_kind": "pipeline_selftest" if mode == "selftest" else "provider_run",
        "provider": "stub" if mode == "selftest" else "deepseek",
        "model": "stub-model" if mode == "selftest" else "deepseek-chat",
        "prompt_version": "grading-v1",
        "dataset": {"name": "rubric-set", "version": "v1", "sample_count": 4, "label_source": "teacher" if teacher_labels else "synthetic_reference"},
        "ground_truth": {"teacher_labels_available": teacher_labels},
        "runs": [{
            "strategy": "rag", "status": "completed", "error_code": None,
            "metrics": {
                "sample_total": 4, "effective_sample_count": 3 if teacher_labels else 0,
                "mae": 0.5, "rmse": 0.6, "agreement_rate": 0.75,
                "failure_rate": 0.25,
            },
        }],
    }
    (directory / "grading_grade-a.json").write_text(json.dumps(payload), encoding="utf-8")


def _service(directory: Path, actor: UUID) -> EvaluationService:
    return EvaluationService(directory, authorize=lambda current: current == actor)


def test_unauthorized_query_does_not_read_files_or_grant_admin_by_role(tmp_path: Path) -> None:
    (tmp_path / "retrieval_broken_hybrid.json").write_text("not json", encoding="utf-8")
    actor = uuid4()
    assert EvaluationService(tmp_path).query(actor_id=actor) == []
    assert _service(tmp_path, actor).query(actor_id=uuid4()) == []


def test_json_is_canonical_csv_only_adds_missing_runs_and_filters(tmp_path: Path) -> None:
    actor = uuid4()
    _retrieval(tmp_path)
    with (tmp_path / "retrieval_summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "run_id", "config", "dataset_version", "model_version", "prompt_version",
            "status", "error_code", "recall_at_5", "result_path",
        ])
        writer.writeheader()
        writer.writerow({
            "run_id": "run-a", "config": "hybrid", "dataset_version": "dataset-v1",
            "model_version": "embedding-a", "prompt_version": "prompt-v1",
            "status": "ok", "recall_at_5": "0.1", "result_path": "retrieval_run-a_hybrid.json",
        })
        writer.writerow({
            "run_id": "run-b", "config": "keyword_only", "dataset_version": "dataset-v1",
            "model_version": "embedding-b", "prompt_version": "prompt-v2",
            "status": "failed", "error_code": "EMBEDDING_PROVIDER_NOT_READY",
            "recall_at_5": "0.0", "result_path": "missing.json",
        })
    service = _service(tmp_path, actor)
    rows = service.query(actor_id=actor)
    assert len(rows) == 2
    canonical = service.query(
        actor_id=actor, experiment="retrieval/run-a", dataset="dataset-v1",
        model="embedding-a", prompt="prompt-v1", retrieval_mode="hybrid",
    )
    assert len(canonical) == 1
    assert canonical[0].metrics["recall_at_5"] == 0.8
    assert canonical[0].sample_count == 2
    assert canonical[0].metric_units["recall_at_5"] == "比例"
    _retrieval(tmp_path, run_id="task-v1")
    assert service.query(actor_id=actor, experiment="retrieval/task-v1")[0].run_id == "task-v1"
    failed = service.query(actor_id=actor, model="embedding-b")
    assert len(failed) == 1 and failed[0].status == "failed"
    assert failed[0].metrics["recall_at_5"] is None
    assert failed[0].failure_reason == "EMBEDDING_PROVIDER_NOT_READY"


def test_grading_without_teacher_labels_and_selftest_never_claim_quality(tmp_path: Path) -> None:
    actor = uuid4()
    _grading(tmp_path)
    record = _service(tmp_path, actor).query(actor_id=actor)[0]
    assert record.dataset == "rubric-set"
    assert record.metrics["mae"] is None
    assert record.metrics["rmse"] is None
    assert record.metrics["agreement_rate"] is None
    assert record.metrics["failure_rate"] == 0.25
    assert record.evidence_kind == "provider_run"
    _grading(tmp_path, mode="selftest", teacher_labels=True)
    selftest = _service(tmp_path, actor).query(actor_id=actor)[0]
    assert selftest.evidence_kind == "pipeline_selftest"
    assert selftest.metrics["mae"] is None
    assert selftest.metrics["agreement_rate"] is None
    assert selftest.model == "stub-model"


def test_grading_csv_only_has_counts_but_no_unverified_quality(tmp_path: Path) -> None:
    actor = uuid4()
    with (tmp_path / "grading_summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "run_id", "created_at", "mode", "dataset_version", "model",
            "prompt_version", "strategy", "status", "sample_total",
            "effective_sample_count", "mae", "rmse", "agreement_rate", "failure_rate",
        ])
        writer.writeheader()
        writer.writerow({
            "run_id": "grade-csv", "created_at": "2026-09-20T12:00:00+00:00",
            "mode": "real", "dataset_version": "v1", "model": "model-a",
            "prompt_version": "grading-v1", "strategy": "rag", "status": "completed",
            "sample_total": "4", "effective_sample_count": "3", "mae": "0.2",
            "rmse": "0.3", "agreement_rate": "1.0", "failure_rate": "0.25",
        })
    row = _service(tmp_path, actor).query(actor_id=actor)[0]
    assert row.experiment == "grading/grade-csv"
    assert row.metrics["failure_rate"] == 0.25
    assert row.sample_count == 4
    assert all(row.metrics[name] is None for name in ("mae", "rmse", "agreement_rate"))


def test_corrupt_record_is_failed_without_exposing_raw_file_content(tmp_path: Path) -> None:
    actor = uuid4()
    (tmp_path / "retrieval_bad_hybrid.json").write_text(
        "Authorization Bearer private-key", encoding="utf-8",
    )
    rows = _service(tmp_path, actor).query(actor_id=actor)
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert rows[0].failure_reason == "BENCHMARK_RECORD_INVALID"
    assert "private-key" not in repr(rows[0])
