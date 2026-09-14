"""T045 检索 Benchmark 执行器单元测试：用例构造、指标与记录写入。

不访问数据库与外部模型：只验证规则化标注、指标计算与 JSON/CSV 记录格式（含失败记录）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app.ai.retrieval.base import RetrievalMode
from scripts.run_retrieval_benchmark import (
    SUMMARY_NAME,
    ConfigRun,
    build_retrieval_cases,
    compute_metrics,
    percentile,
    write_run_records,
)

DATASET = {
    "metadata": {
        "dataset_version": "m0-seed-v1",
        "model_version": "seed-model",
        "prompt_version": "synthetic-prompt-v1",
        "scoring_criteria": "按参考答案满分。",
        "seed": 7,
        "record_count": 2,
    },
    "cases": [
        {
            "case_id": "m0-case-a",
            "question_type": "SINGLE_CHOICE",
            "question": "2 加 2 等于多少？",
            "scoring_criteria": "答案正确得满分。",
            "reference_answer": "4",
            "student_answer": "4",
            "score": 5,
            "max_score": 5,
            "reason": "一致。",
            "knowledge_points": ["整数加法"],
        },
        {
            "case_id": "m0-case-b",
            "question_type": "TRUE_FALSE",
            "question": "力可以改变物体的运动状态。",
            "scoring_criteria": "判断正确得满分。",
            "reference_answer": "正确",
            "student_answer": "正确",
            "score": 5,
            "max_score": 5,
            "reason": "一致。",
            "knowledge_points": ["力的作用效果"],
        },
    ],
}


def test_build_retrieval_cases_uses_rule_based_labels() -> None:
    """用例按规则构造：题干作 query，语料包含参考答案与知识点。"""

    cases = build_retrieval_cases(DATASET)

    assert [case.query_id for case in cases] == ["m0-case-a", "m0-case-b"]
    assert cases[0].query == "2 加 2 等于多少？"
    assert "参考答案：4" in cases[0].corpus_text
    assert "知识点：整数加法" in cases[0].corpus_text
    assert "参考答案：正确" in cases[1].corpus_text


def test_build_retrieval_cases_respects_limit_and_skips_incomplete() -> None:
    """限制查询数量，且缺少题干或参考答案的样本被跳过。"""

    dataset = {
        "cases": DATASET["cases"]
        + [{"case_id": "m0-case-c", "question": "", "reference_answer": ""}],
    }

    assert len(build_retrieval_cases(dataset, limit=1)) == 1
    assert len(build_retrieval_cases(dataset)) == 2


def test_percentile_uses_nearest_rank() -> None:
    """分位数使用最近秩法，保持可解释性。"""

    assert percentile([], 0.95) == 0.0
    assert percentile([5.0], 0.95) == 5.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.95) == 4.0


def test_compute_metrics_matches_manual_calculation() -> None:
    """Recall@k、Precision@k、MRR 与延迟 p95 按定义计算。"""

    per_query = [
        {
            "chunk_ids": ["a", "b", "c"],
            "relevant_ids": ["a"],
            "latency_ms": 10.0,
        },
        {
            "chunk_ids": ["x", "y", "b"],
            "relevant_ids": ["b"],
            "latency_ms": 30.0,
        },
    ]

    metrics = compute_metrics(per_query, ks=(3,))

    assert metrics["recall_at_3"] == pytest.approx(1.0)
    assert metrics["precision_at_3"] == pytest.approx((1 / 3 + 1 / 3) / 2)
    assert metrics["mrr"] == pytest.approx((1.0 + 1 / 3) / 2)
    assert metrics["latency_p95_ms"] == pytest.approx(30.0)


def test_compute_metrics_handles_empty_results() -> None:
    """没有任何候选时指标为 0，而不是抛异常。"""

    metrics = compute_metrics([])

    assert metrics["recall_at_5"] == 0.0
    assert metrics["mrr"] == 0.0
    assert metrics["latency_p95_ms"] == 0.0


def test_write_run_records_persists_schema_and_summary(tmp_path: Path) -> None:
    """成功运行写入规范 JSON 字段，并追加汇总 CSV 行。"""

    run = ConfigRun(
        config=RetrievalMode.KEYWORD_ONLY,
        per_query=[
            {"query_id": "m0-case-a", "chunk_ids": ["c1"], "scores": [0.9], "relevant_ids": ["c1"], "latency_ms": 5.0},
        ],
        metrics=compute_metrics(
            [
                {"chunk_ids": ["c1"], "relevant_ids": ["c1"], "latency_ms": 5.0},
            ]
        ),
    )

    path = write_run_records(
        run,
        run_id="unit-run",
        output_dir=tmp_path,
        dataset_version="m0-seed-v1",
        model_version="stub-hash-v1",
        prompt_version=None,
        top_k=5,
        analysis="harness 自检运行。",
    )

    assert path.name == "retrieval_unit-run_keyword_only.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["config"] == "keyword_only"
    assert payload["status"] == "ok"
    assert payload["metrics"]["recall_at_5"] == pytest.approx(1.0)
    assert payload["results"][0]["chunk_ids"] == ["c1"]
    assert payload["environment"]["top_k"] == 5
    assert "自检" in payload["analysis"]

    summary = (tmp_path / SUMMARY_NAME).read_text(encoding="utf-8").splitlines()
    assert summary[0].startswith("run_id,run_at,config")
    assert summary[1].split(",")[0] == "unit-run"
    assert summary[1].split(",")[6] == "ok"


def test_write_run_records_marks_failures_without_metrics(tmp_path: Path) -> None:
    """失败运行写入错误码，不伪造指标。"""

    run = ConfigRun(
        config=RetrievalMode.VECTOR_ONLY,
        per_query=[],
        metrics=compute_metrics([]),
        status="failed",
        error_code="EMBEDDING_PROVIDER_NOT_READY",
        error_message="Embedding Provider 未就绪。",
    )

    path = write_run_records(
        run,
        run_id="unit-failed",
        output_dir=tmp_path,
        dataset_version="m0-seed-v1",
        model_version="BAAI/bge-large-zh-v1.5",
        prompt_version="synthetic-prompt-v1",
        top_k=5,
        analysis="真实 Provider 运行。",
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["error_code"] == "EMBEDDING_PROVIDER_NOT_READY"
    assert payload["results"] == []

    summary_row = (tmp_path / SUMMARY_NAME).read_text(encoding="utf-8").splitlines()[1]
    assert "failed" in summary_row
    assert "EMBEDDING_PROVIDER_NOT_READY" in summary_row
