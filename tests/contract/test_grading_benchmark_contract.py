"""T059 阅卷 Benchmark 契约测试：三路策略、指标口径、诚实降级与失败记录。

TCR（2026-09-16，T059 / B06、B07）：Benchmark 需要独立的实验入口（不复用正式评分入口的
上下文充分性强制检查）、完整样本与明确指标口径，并在缺少教师 Ground Truth 时如实标注
指标不可计算；失败必须写入失败状态与错误码，不写假分数，也不记录学生答案原文。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from scripts import run_grading_benchmark as benchmark
from scripts.run_grading_benchmark import SelfTestScoringProvider


class OutOfRangeProvider(SelfTestScoringProvider):
    """返回超满分结果的自检 Provider，用于构造单样本失败记录。"""

    async def generate_structured(
        self,
        messages: Sequence[Mapping[str, Any]],
        schema: type[Any],
        **kwargs: Any,
    ) -> Any:
        payload = await super().generate_structured(messages, schema, **kwargs)
        return payload.model_copy(update={"score": float("1e6")})


class NotReadyProvider(SelfTestScoringProvider):
    """调用即失败的 Provider，用于验证整体失败记录。"""

    async def generate_structured(
        self,
        messages: Sequence[Mapping[str, Any]],
        schema: type[Any],
        **kwargs: Any,
    ) -> Any:
        raise RuntimeError("provider boom")


@pytest.fixture
def results_dir(tmp_path: Path) -> Path:
    """每次用例使用独立结果目录。"""

    return tmp_path / "results"


def _run(results_dir: Path, provider: Any = None) -> dict[str, Any]:
    record, _ = benchmark.run_benchmark(
        mode="selftest",
        results_dir=results_dir,
        run_id="t059-run",
        provider=provider if provider is not None else SelfTestScoringProvider(),
    )
    return record


def test_three_strategies_are_executed_with_complete_metric_fields(
    results_dir: Path,
) -> None:
    """三路策略均被执行，预测与指标字段完整，且预测分数在满分范围内。"""

    record = _run(results_dir)

    assert [run["strategy"] for run in record["runs"]] == [
        "zero_shot",
        "rag",
        "hybrid_rerank",
    ]
    assert record["status"] == "completed"
    for run in record["runs"]:
        assert run["status"] == "completed"
        assert run["provider_calls"] == record["dataset"]["sample_count"]
        metrics = run["metrics"]
        assert metrics["sample_total"] == record["dataset"]["sample_count"]
        assert metrics["scored_count"] == record["dataset"]["sample_count"]
        assert metrics["failure_count"] == 0
        assert metrics["failure_rate"] == 0.0
        for key in ("mae", "rmse", "agreement_rate", "effective_sample_count"):
            assert key in metrics
        for prediction in run["predictions"]:
            assert prediction["status"] == "completed"
            assert predicted_within_max(prediction)

    zero_shot = record["runs"][0]
    rag = record["runs"][1]
    hybrid = record["runs"][2]
    assert zero_shot["retrieval_calls"] == 0
    assert rag["retrieval_calls"] > 0
    assert rag["rerank_calls"] == 0
    assert hybrid["retrieval_calls"] > 0
    assert hybrid["rerank_calls"] > 0


def predicted_within_max(prediction: Mapping[str, Any]) -> bool:
    """预测分数必须落在 [0, 满分] 内。"""

    score = float(prediction["predicted_score"])
    maximum = float(prediction["max_score"])
    return 0.0 <= score <= maximum


def test_metrics_are_null_without_teacher_ground_truth(results_dir: Path) -> None:
    """数据集只有合成参考分时，质量指标为 null 并说明原因。"""

    record = _run(results_dir)

    assert record["ground_truth"]["teacher_labels_available"] is False
    assert record["ground_truth"]["reason"] == benchmark.NO_GROUND_TRUTH_REASON
    for run in record["runs"]:
        metrics = run["metrics"]
        assert metrics["mae"] is None
        assert metrics["rmse"] is None
        assert metrics["agreement_rate"] is None
        assert metrics["effective_sample_count"] == 0
        assert metrics["metrics_unavailable_reason"] == benchmark.NO_GROUND_TRUTH_REASON
        for prediction in run["predictions"]:
            assert prediction["teacher_score"] is None
            assert prediction["label_source"] == "synthetic_reference"


def test_teacher_labels_enable_quality_metrics(results_dir: Path, tmp_path: Path) -> None:
    """存在教师评分时按定义计算 MAE/RMSE 与一致率。"""

    dataset = json.loads(benchmark.DATASET_PATH.read_text(encoding="utf-8"))
    for item in dataset["samples"]:
        item["teacher_score"] = item["reference_score"]
        item["label_source"] = "teacher"
    dataset_path = tmp_path / "grading_samples_teacher.json"
    dataset_path.write_text(json.dumps(dataset, ensure_ascii=False), encoding="utf-8")

    record, _ = benchmark.run_benchmark(
        mode="selftest",
        dataset_path=dataset_path,
        results_dir=results_dir,
        run_id="t059-teacher",
        provider=SelfTestScoringProvider(),
    )

    assert record["ground_truth"]["teacher_labels_available"] is True
    for run in record["runs"]:
        metrics = run["metrics"]
        assert metrics["effective_sample_count"] == record["dataset"]["sample_count"]
        assert metrics["mae"] is not None
        assert metrics["rmse"] is not None
        assert metrics["agreement_rate"] is not None
        assert metrics["metrics_unavailable_reason"] is None


def test_invalid_payload_writes_failure_record_without_fake_score(
    results_dir: Path,
) -> None:
    """单样本失败写入失败状态与错误码，且不写假分数。"""

    record = _run(results_dir, provider=OutOfRangeProvider())

    run = record["runs"][0]
    assert run["metrics"]["failure_count"] == record["dataset"]["sample_count"]
    assert run["metrics"]["scored_count"] == 0
    assert run["metrics"]["metrics_unavailable_reason"] == benchmark.NO_GROUND_TRUTH_REASON
    for failure in run["failures"]:
        assert failure["status"] == "failed"
        assert failure["error_code"] == "GRADING_SCORE_OUT_OF_RANGE"
        assert "predicted_score" not in failure


def test_provider_failure_marks_run_failed_with_error_code(results_dir: Path) -> None:
    """Provider 调用失败时整体运行标记失败并保留错误码。"""

    record = _run(results_dir, provider=NotReadyProvider())

    assert record["status"] == "failed"
    for run in record["runs"]:
        assert run["status"] == "failed"
        assert run["error_code"] == "RuntimeError"
        assert run["predictions"] == []


def test_results_omit_prompts_and_student_answers(results_dir: Path) -> None:
    """结果文件不记录密钥、完整 Prompt 与学生答案原文。"""

    _run(results_dir)

    payload = (results_dir / "grading_t059-run.json").read_text(encoding="utf-8")
    dataset = json.loads(benchmark.DATASET_PATH.read_text(encoding="utf-8"))
    assert "api_key" not in payload.lower()
    for item in dataset["samples"]:
        assert item["student_answer"] not in payload
    assert "本题满分" not in payload


def test_repeated_runs_append_csv_summary(results_dir: Path) -> None:
    """重复运行生成独立 JSON 并追加 CSV 汇总行。"""

    _run(results_dir)
    record, run_id = benchmark.run_benchmark(
        mode="selftest",
        results_dir=results_dir,
        run_id="t059-run-2",
        provider=SelfTestScoringProvider(),
    )

    assert (results_dir / "grading_t059-run.json").exists()
    assert (results_dir / f"grading_{run_id}.json").exists()
    lines = (results_dir / benchmark.SUMMARY_FILENAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 + 2 * len(benchmark.STRATEGIES)
    assert record["runs"][0]["metrics"]["sample_total"] > 0
