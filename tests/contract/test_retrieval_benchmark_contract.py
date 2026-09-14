"""H04 Benchmark 查询编码与 Ready 语料契约。

TCR（2026-09-14）：辅助函数测试不能证明执行器使用了正确入口，故在隔离 PostgreSQL
上运行主流程，断言 Provider 调用、四模式非空检索及清理。依赖缺失时明确失败记录，
查询单独失败时仅关键词模式仍可运行；不访问真实模型，不伪造可用 Provider。
"""

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.ai.embedding.base import (
    EmbeddingProviderError,
    EmbeddingProviderNotReadyError,
)
from backend.app.ai.retrieval.base import RetrievalMode
from backend.app.models import Document
from scripts import run_retrieval_benchmark as benchmark
from tests.postgres_helpers import isolated_postgres_engine
from tests.unit.retrieval.test_retrieval_benchmark import DATASET


@pytest.mark.parametrize("failure", [None, "provider", "query"])
def test_benchmark_main_preserves_query_operation_and_failure_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None,
) -> None:
    """执行器逐条查询编码，无法准备语料时四模式失败，原始失败码不丢失。"""

    class RecordingProvider(benchmark.StubHashEmbeddingProvider):
        def __init__(self) -> None:
            super().__init__()
            self.document_calls: list[list[str]] = []
            self.query_calls: list[str] = []

        async def embed_documents(self, documents: Sequence[str]) -> list[list[float]]:
            self.document_calls.append(list(documents))
            return await super().embed_documents(documents)

        async def embed_query(self, query: str) -> list[float]:
            self.query_calls.append(query)
            if failure == "query":
                raise EmbeddingProviderError("测试模拟查询编码失败")
            return await super().embed_query(query)

    provider = RecordingProvider()

    def make_provider(_self_test):
        if failure == "provider":
            raise EmbeddingProviderNotReadyError("测试模拟依赖未安装")
        return provider

    cases = benchmark.build_retrieval_cases(DATASET)
    monkeypatch.setattr(benchmark, "load_dataset", lambda: DATASET)
    monkeypatch.setattr(benchmark, "_make_embedding_provider", make_provider)
    with isolated_postgres_engine() as engine:
        monkeypatch.setattr(benchmark, "create_database_engine", lambda *_a, **_k: engine)
        result = benchmark.main([
            "--self-test", "--run-id", "h04-contract", "--output-dir", str(tmp_path),
        ])
        assert result == (0 if failure is None else 1)
        for mode in RetrievalMode:
            payload = json.loads(
                (tmp_path / f"retrieval_h04-contract_{mode.value}.json").read_text(encoding="utf-8")
            )
            succeeded = failure is None or (failure == "query" and mode is RetrievalMode.KEYWORD_ONLY)
            if succeeded:
                assert payload["status"] == "ok"
                assert len(payload["results"]) == len(cases)
                assert all(row["chunk_ids"] for row in payload["results"])
            else:
                assert payload["status"] == "failed"
                assert payload["results"] == []
                assert payload["metrics"] == {}
                expected_error = (
                    EmbeddingProviderNotReadyError.error_code
                    if failure == "provider" else EmbeddingProviderError.error_code
                )
                assert payload["error_code"] == expected_error
                assert "测试模拟" in payload["error_message"]
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(Document)) == 0

    if failure != "provider":
        assert provider.document_calls == [[case.corpus_text for case in cases]]
        expected_queries = cases[:1] if failure == "query" else cases
        assert provider.query_calls == [case.query for case in expected_queries]
