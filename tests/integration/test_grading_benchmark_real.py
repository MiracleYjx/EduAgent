"""TCR（B03）：真实模式必须使用 M2 语料和实际检索/重排实现。

使用独立 PostgreSQL schema，BGE 编码器和 LLM 响应替换为本地测试输入；保留真实
BGE Provider、向量检索、关键词检索、Hybrid、LLM Reranker 和评分校验，验证语料范围、
组件装配与同一事件循环。缺少摄取片段时必须明确失败，不伪造真实评测结果。
"""

import asyncio
import json
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.app.ai.embedding import factory as embedding_factory
from backend.app.ai.embedding.providers.bge import BgeEmbeddingProvider
from backend.app.ai.retrieval.reranker import LLMRerankResponse
from backend.app.domain.enums import DocumentStatus
from backend.app.models import Document, DocumentChunk, KnowledgeBase
from scripts import run_grading_benchmark as benchmark
from tests.postgres_helpers import isolated_postgres_engine
from tests.unit.models.sqlite_support import seed_submission
from tests.unit.settings_helpers import build_test_settings


@pytest.mark.parametrize("complete_corpus", [True, False])
def test_real_benchmark_uses_ingested_corpus_and_production_components(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, complete_corpus: bool,
) -> None:
    loops = []

    class Encoder:
        def encode(self, texts, **kwargs):
            return [[1.0] + [0.0] * 1023 for _ in texts]

    class RecordingBge(BgeEmbeddingProvider):
        async def embed_query(self, text):
            loops.append(asyncio.get_running_loop())
            return await super().embed_query(text)

    class Provider(benchmark.SelfTestScoringProvider):
        rerank_calls = 0

        async def generate_structured(self, messages, schema, **kwargs):
            loops.append(asyncio.get_running_loop())
            if schema is LLMRerankResponse:
                self.rerank_calls += 1
                ids = [line.removeprefix("- chunk_id=")
                       for line in messages[-1]["content"].splitlines()
                       if line.startswith("- chunk_id=")]
                return schema(rankings=[{"chunk_id": key, "score": 0.9} for key in ids])
            return await super().generate_structured(messages, schema, **kwargs)

    embedding = RecordingBge(dimension=1024, model_loader=lambda _: Encoder())
    monkeypatch.setattr(embedding_factory, "create_embedding_provider", lambda _: embedding)
    provider = Provider()
    corpus = json.loads(benchmark.CORPUS_PATH.read_text(encoding="utf-8"))
    with isolated_postgres_engine() as engine:
        with Session(engine) as session:
            fixture = seed_submission(session)
            knowledge_base = KnowledgeBase(name="评测语料", course_id=fixture.course_id)
            session.add(knowledge_base)
            session.flush()
            document = Document(
                id=UUID(corpus["document_id"]), original_filename="python_basics.md",
                file_format="markdown", course_id=fixture.course_id,
                knowledge_base_id=knowledge_base.id, uploaded_by=fixture.teacher_id,
                status=DocumentStatus.READY,
            )
            session.add(document)
            session.flush()
            items = corpus["chunks"] if complete_corpus else corpus["chunks"][:-1]
            for item in items:
                session.add(DocumentChunk(
                    id=UUID(item["chunk_id"]), document_id=document.id,
                    course_id=fixture.course_id, knowledge_base_id=knowledge_base.id,
                    chunk_index=item["chunk_index"], content=item["content"],
                    embedding=[1.0] + [0.0] * 1023,
                    search_vector=func.to_tsvector("simple", item["content"]),
                    chunk_metadata=item["metadata"],
                ))
            session.commit()
        monkeypatch.setattr(benchmark, "create_database_engine", lambda _: engine)
        record, _ = benchmark.run_benchmark(
            mode="real", results_dir=tmp_path, provider=provider,
            settings=build_test_settings(
                embedding_provider="bge", embedding_dimension=1024,
                rerank_provider="llm", rerank_max_candidates=5,
            ),
        )

        if not complete_corpus:
            assert record["status"] == "failed"
            assert record["error_code"] == "GRADING_BENCHMARK_CORPUS_NOT_READY"
            assert record["runs"] == []
            assert loops == []
            return
        assert record["status"] == "completed"
        zero, rag, hybrid = record["runs"]
        assert rag["retriever"] == "VectorSearchRetriever"
        assert hybrid["retriever"] == "HybridSearchRetriever"
        assert hybrid["reranker"]["provider"] == "llm"
        assert record["embedding"]["model"] == "BAAI/bge-large-zh-v1.5"
        assert record["corpus"]["chunk_count"] == len(corpus["chunks"])
        expected_ids = {item["chunk_id"] for item in corpus["chunks"]}
        for run in (rag, hybrid):
            assert len(run["predictions"]) == 4
            assert run["retrieval_calls"] == 4
            for prediction in run["predictions"]:
                assert prediction["retrieved_context_ids"]
                assert set(prediction["retrieved_context_ids"]) <= expected_ids
        assert all(p["retrieved_context_ids"] == [] for p in zero["predictions"])
        assert provider.rerank_calls == hybrid["rerank_calls"] == 4
        assert len(set(loops)) == 1
