"""T059 阅卷 Benchmark 契约测试：三路策略、指标口径、诚实降级与失败记录。

TCR（2026-09-16，T059 / B06、B07）：Benchmark 需要独立的实验入口（不复用正式评分入口的
上下文充分性强制检查）、完整样本与明确指标口径，并在缺少教师 Ground Truth 时如实标注
指标不可计算；失败必须写入失败状态与错误码，不写假分数，也不记录学生答案原文。

TCR（B03）：真实模式不得使用自检检索/Embedding/重排替身；新增组件类型、未就绪失败、
共享 Provider 事件循环测试。外部模型调用用替身验证，不以此声明模型效果。

TCR（B04）：补齐全失败、部分失败、调用中断与未执行样本的统计断言，验证来源错误码、
持久化报告和命令行退出码一致，避免失败被报告为成功。
"""

from __future__ import annotations

import asyncio
import csv
import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from backend.app.ai.embedding import factory as embedding_factory
from backend.app.ai.embedding.base import EmbeddingProviderNotReadyError
from backend.app.ai.embedding.providers.bge import BgeEmbeddingProvider
from backend.app.ai.retrieval.hybrid_search import HybridSearchRetriever
from backend.app.ai.retrieval.reranker import (
    LLMRerankAdapter,
    RerankProviderNotReadyError,
)
from backend.app.ai.retrieval.vector_search import VectorSearchRetriever
from backend.app.core.retry_policy import ProviderErrorInfo, ProviderExecutionError
from scripts import run_grading_benchmark as benchmark
from scripts.run_grading_benchmark import SelfTestScoringProvider
from tests.unit.settings_helpers import build_test_settings


@pytest.mark.parametrize("injected", [False, True])
@pytest.mark.parametrize("model_name", ["actual-stub-model-v2", ""])
def test_benchmark_metadata_comes_from_resolved_provider(
    results_dir: Path, monkeypatch: pytest.MonkeyPatch, injected: bool, model_name: str,
) -> None:
    """P4.2 TCR：real 模式也不得用配置模型冒充实际替身，JSON/CSV 保持同源。"""

    provider = SelfTestScoringProvider()
    provider.model_name = model_name
    settings = build_test_settings(deepseek_model="configured-model-must-not-appear")
    monkeypatch.setattr(
        embedding_factory, "create_embedding_provider", lambda _: benchmark.StubEmbeddingProvider(),
    )
    resolutions = []

    def resolve(mode, passed):
        resolutions.append((mode, passed))
        return provider

    monkeypatch.setattr(benchmark, "build_provider", resolve)
    record, run_id = benchmark.run_benchmark(
        mode="real", settings=settings, provider=provider if injected else None,
        strategies=("zero_shot",), limit=1, results_dir=results_dir,
    )
    assert record["status"] == "completed"
    assert provider.calls == 1
    assert resolutions == ([] if injected else [("real", settings)])
    assert record["provider"] == "stub"
    assert record["model"] == (model_name or "unknown")
    assert record["prompt_version"] == benchmark.SUBJECTIVE_GRADING_PROMPT_VERSION
    assert record["call_path"] == (
        "scripts.run_grading_benchmark.SelfTestScoringProvider.generate_structured"
    )
    saved = json.loads((results_dir / f"grading_{run_id}.json").read_text(encoding="utf-8"))
    assert saved == record
    with (results_dir / benchmark.SUMMARY_FILENAME).open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["model"] == record["model"]
    assert rows[0]["prompt_version"] == record["prompt_version"]
    assert "configured-model-must-not-appear" not in json.dumps(saved)


def test_selftest_default_provider_is_explicit_stub(results_dir: Path) -> None:
    record, _ = benchmark.run_benchmark(
        mode="selftest", settings=build_test_settings(llm_provider="openai_compatible"),
        strategies=("zero_shot",), limit=1, results_dir=results_dir,
    )
    assert record["status"] == "completed"
    assert record["provider"] == "stub"
    assert record["model"] == "selftest-stub"
    assert "SelfTestScoringProvider.generate_structured" in record["call_path"]


def test_unresolved_benchmark_provider_has_unknown_identity(
    results_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        embedding_factory, "create_embedding_provider", lambda _: benchmark.StubEmbeddingProvider(),
    )
    record, _ = benchmark.run_benchmark(
        mode="real", settings=build_test_settings(llm_provider="openai_compatible"),
        strategies=("zero_shot",), limit=1, results_dir=results_dir,
    )
    assert record["status"] == "failed"
    assert record["error_code"] == "GRADING_PROVIDER_NOT_READY_UnsupportedLLMProviderError"
    assert record["provider"] == record["model"] == record["call_path"] == "unknown"
    assert record["runs"] == []


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


@pytest.mark.parametrize("teacher_labels", [False, True])
def test_p4a3_persisted_label_metrics_and_selftest_evidence(
    results_dir: Path, tmp_path: Path, teacher_labels: bool,
) -> None:
    """TCR P4A.3：已知误差的人工标签夹具验证公式；合成参考分绝不冒充人工分。"""
    dataset = json.loads(benchmark.DATASET_PATH.read_text(encoding="utf-8"))
    dataset["samples"] = dataset["samples"][:2]
    for item, score in zip(dataset["samples"], [6, 2], strict=True):
        item["teacher_score"] = score if teacher_labels else None
        item["label_source"] = "teacher" if teacher_labels else "synthetic_reference"
        item["reference_score"] = "5.00"  # 与预测相同也不能用来算质量指标。
    path = tmp_path / "labels.json"
    path.write_text(json.dumps(dataset, ensure_ascii=False), encoding="utf-8")

    class FivePointStub(SelfTestScoringProvider):
        async def generate_structured(self, messages, schema, **kwargs):
            payload = await super().generate_structured(messages, schema, **kwargs)
            return payload.model_copy(update={"score": 5.0})

    record, run_id = benchmark.run_benchmark(
        mode="selftest", dataset_path=path, results_dir=results_dir, provider=FivePointStub(),
    )
    saved = json.loads((results_dir / f"grading_{run_id}.json").read_text(encoding="utf-8"))
    assert saved == record
    assert saved["evidence_kind"] == "pipeline_selftest"
    assert "不是模型质量结论" in saved["note"]
    assert saved["ground_truth"]["label_sources"] == {
        "teacher" if teacher_labels else "synthetic_reference": 2,
    }
    for run in saved["runs"]:
        assert run["status"] == "completed"
        metrics = run["metrics"]
        # errors = [-1, 3] -> MAE 2, RMSE sqrt(5), agreement (<=1) 1/2。
        assert [metrics[key] for key in ("mae", "rmse", "agreement_rate")] == (
            ["2.00", "2.24", "0.5000"] if teacher_labels else [None, None, None]
        )
        assert metrics["effective_sample_count"] == (2 if teacher_labels else 0)


@pytest.mark.parametrize("missing", [True, False])
def test_p4a3_legacy_corpus_failure_keeps_original_error_code(
    results_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: bool,
) -> None:
    """旧清单缺失或损坏仍使用原错误合同，不误报为 Provider 装配问题。"""
    path = tmp_path / "broken.json"
    if not missing:
        path.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(benchmark, "build_embedding", lambda *_: benchmark.StubEmbeddingProvider())
    record, _ = benchmark.run_benchmark(
        mode="real", corpus_path=path, results_dir=results_dir, strategies=("rag",),
        settings=build_test_settings(), provider=SelfTestScoringProvider(),
    )
    assert record["status"] == "failed"
    assert record["error_code"] == "GRADING_BENCHMARK_CORPUS_NOT_READY"
    assert record["runs"] == []


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


@pytest.mark.parametrize("kind", ["invalid", "provider", "partial", "mixed"])
def test_failure_status_counts_and_cli_agree(
    results_dir: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    """真实入口生成报告：失败率仅统计执行过的样本，失败退出码不可为零。"""

    class FailingProvider(SelfTestScoringProvider):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        async def generate_structured(self, messages, schema, **kwargs):
            self.attempts += 1
            if kind == "provider":
                raise ProviderExecutionError(ProviderErrorInfo(
                    code="ProviderTimeout", message="敏感内容不得写入结果", attempt_count=3,
                ))
            payload = await super().generate_structured(messages, schema, **kwargs)
            if kind == "invalid" or (kind == "partial" and self.attempts % 4 == 1) or (
                kind == "mixed" and self.attempts <= 4
            ):
                return payload.model_copy(update={"score": 1e6})
            return payload

    provider = FailingProvider()
    monkeypatch.setattr(benchmark, "build_provider", lambda *args: provider)
    exit_code = benchmark.main([
        "--mode", "selftest", "--results-dir", str(results_dir), "--run-id", "b04",
    ])
    record = json.loads((results_dir / "grading_b04.json").read_text(encoding="utf-8"))
    assert exit_code == 1
    assert record["status"] == ("partial_failed" if kind in {"partial", "mixed"} else "failed")
    assert sum(run["provider_calls"] for run in record["runs"]) == provider.attempts
    for index, run in enumerate(record["runs"]):
        metrics = run["metrics"]
        success = 3 if kind == "partial" else (4 if kind == "mixed" and index > 0 else 0)
        failed = 1 if kind in {"provider", "partial"} else (4 - success)
        skipped = 3 if kind == "provider" else 0
        assert metrics["sample_total"] == success + failed + skipped == 4
        assert metrics["scored_count"] == len(run["predictions"]) == success
        assert metrics["failure_count"] == len(run["failures"]) == failed
        assert metrics["not_executed_count"] == len(run["not_executed"]) == skipped
        assert metrics["attempted_count"] == success + failed
        assert metrics["failure_rate"] == failed / (success + failed)
        assert run["provider_calls"] == success + failed
        expected = "completed" if not failed else ("partial_failed" if success else "failed")
        assert run["status"] == expected
        for failure in run["failures"]:
            assert failure["error_code"] == (
                "ProviderTimeout" if kind == "provider" else "GRADING_SCORE_OUT_OF_RANGE"
            )
        sample_ids = [item["sample_id"] for group in (
            "predictions", "failures", "not_executed",
        ) for item in run[group]]
        assert len(set(sample_ids)) == 4
        assert all(item["status"] == "not_executed" for item in run["not_executed"])
    assert "敏感内容不得写入结果" not in json.dumps(record, ensure_ascii=False)


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


def test_shared_provider_uses_one_loop_for_all_samples_and_strategies(results_dir: Path) -> None:
    loops = []

    class LoopBoundProvider(SelfTestScoringProvider):
        async def generate_structured(self, messages, schema, **kwargs):
            loops.append(asyncio.get_running_loop())
            if loops[-1] is not loops[0]:
                raise RuntimeError("共享客户端跨事件循环调用")
            return await super().generate_structured(messages, schema, **kwargs)

    record = _run(results_dir, LoopBoundProvider())
    assert all(run["status"] == "completed" for run in record["runs"])
    assert len(loops) == 3 * record["dataset"]["sample_count"]
    assert len(set(loops)) == 1


def test_real_mode_embedding_not_ready_fails_without_stub_fallback(
    results_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def not_ready(settings=None):
        calls.append(settings)
        raise EmbeddingProviderNotReadyError("测试环境未安装模型")

    monkeypatch.setattr(embedding_factory, "create_embedding_provider", not_ready)
    record, _ = benchmark.run_benchmark(
        mode="real", provider=SelfTestScoringProvider(), results_dir=results_dir,
        settings=build_test_settings(),
    )
    assert record["status"] == "failed"
    assert record["error_code"] == "EMBEDDING_PROVIDER_NOT_READY"
    assert len(calls) == 1
    assert record["runs"] == []


@pytest.mark.parametrize("strategy", ["zero_shot", "rag", "hybrid_rerank"])
def test_real_mode_selects_production_retrievers_and_reranker(strategy: str) -> None:
    settings = build_test_settings(rerank_provider="llm", rerank_max_candidates=5)
    retriever, reranker = benchmark.build_retrieval_components(
        "real", strategy, settings=settings, provider=SelfTestScoringProvider(),
    )
    assert not isinstance(retriever, benchmark.StubRetriever)
    assert not isinstance(reranker, benchmark.IdentityReranker)
    if strategy == "zero_shot":
        assert retriever is None and reranker is None
    elif strategy == "rag":
        assert isinstance(retriever, VectorSearchRetriever)
        assert reranker is None
    else:
        assert isinstance(retriever, HybridSearchRetriever)
        assert retriever.candidate_k == 5
        assert isinstance(reranker, LLMRerankAdapter)
        assert reranker.max_candidates == 5


def test_embedding_modes_use_factory_or_selftest_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = build_test_settings(embedding_provider="bge", embedding_dimension=1024)
    calls = []
    bge = BgeEmbeddingProvider(dimension=1024, model_loader=lambda _: object())

    def create(actual_settings):
        calls.append(actual_settings)
        return bge

    monkeypatch.setattr(embedding_factory, "create_embedding_provider", create)
    assert benchmark.build_embedding("real", settings) is bge
    assert calls == [settings]
    assert isinstance(benchmark.build_embedding("selftest", settings), benchmark.StubEmbeddingProvider)
    retriever, reranker = benchmark.build_retrieval_components(
        "selftest", "hybrid_rerank", settings=settings, provider=SelfTestScoringProvider(),
    )
    assert isinstance(retriever, benchmark.StubRetriever)
    assert isinstance(reranker, benchmark.IdentityReranker)


@pytest.mark.parametrize("component", ["embedding", "reranker"])
def test_lazy_component_not_ready_cannot_be_hidden_by_zero_shot_success(
    results_dir: Path, monkeypatch: pytest.MonkeyPatch, component: str,
) -> None:
    """TCR（B03）：工厂返回后才发现未就绪时，同样不能报告整批成功。"""
    error_type = (EmbeddingProviderNotReadyError if component == "embedding"
                  else RerankProviderNotReadyError)

    async def not_ready(*args, **kwargs):
        raise error_type("延迟初始化未就绪")

    if component == "embedding":
        monkeypatch.setattr(benchmark.StubEmbeddingProvider, "embed_query", not_ready)
    else:
        monkeypatch.setattr(benchmark.IdentityReranker, "rerank_async", not_ready)
    record = _run(results_dir)
    assert record["status"] == "failed"
    assert record["error_code"] == error_type.error_code


def test_script_entrypoint_runs_selftest(results_dir: Path) -> None:
    """TCR（B03）：直接执行脚本也必须能导入复用的 M2 语料工具。"""
    completed = subprocess.run(
        [sys.executable, str(benchmark.REPO_ROOT / "scripts/run_grading_benchmark.py"),
         "--mode", "selftest", "--results-dir", str(results_dir),
         "--run-id", "b03-cli", "--limit", "1"],
        cwd=benchmark.REPO_ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    record = json.loads((results_dir / "grading_b03-cli.json").read_text(encoding="utf-8"))
    assert record["status"] == "completed"
    assert len(record["runs"]) == 3
    assert all(run["provider_calls"] == 1 for run in record["runs"])
