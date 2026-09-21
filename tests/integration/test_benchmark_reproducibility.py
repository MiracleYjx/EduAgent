"""P4A.3 TCR：真实 PostgreSQL、生产摄取/检索、运行时 manifest；仅模型使用 stub。"""

import asyncio
import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from backend.app.ai.retrieval.reranker import LLMRerankAdapter
from backend.app.domain.enums import DocumentStatus
from backend.app.models import Document, DocumentChunk
from scripts import benchmark_corpus as corpus
from scripts import run_grading_benchmark as grading
from scripts import run_retrieval_benchmark as retrieval
from scripts import setup_benchmark_corpus as setup
from tests.postgres_helpers import isolated_postgres_engine
from tests.unit.settings_helpers import build_test_settings


@pytest.fixture(scope="module")
def manifest_path(tmp_path_factory):
    # 既有 helper 负责 PostgreSQL/pgvector 可用性检查，不用 SQLite 冒充。
    with isolated_postgres_engine():
        output = tmp_path_factory.mktemp("p4a3") / "corpus"
        assert setup.main(["--self-test", "--output-dir", str(output)]) == 0
        path = output / "manifest.json"
        try:
            yield path
        finally:
            corpus.cleanup_manifest(path)


def _run(path, output, run_id, *extra):
    return retrieval.main([
        "--manifest", str(path), "--output-dir", str(output), "--self-test",
        "--run-id", run_id, *extra,
    ])


def _report(output, run_id, mode="vector_only"):
    return json.loads((output / run_id / f"retrieval_{run_id}_{mode}.json").read_text(encoding="utf-8"))


def test_setup_ingests_production_corpus_and_maps_actual_uuids(manifest_path):
    manifest = corpus.load_manifest(manifest_path)
    reference = json.loads((corpus.CORPUS_DIR / "chunks.json").read_text(encoding="utf-8"))
    chunks = manifest["corpus"]["chunks"]
    current_ids = {item["chunk_id"] for item in chunks}
    assert manifest["corpus"]["document_id"] != reference["document_id"]
    assert current_ids.isdisjoint(item["chunk_id"] for item in reference["chunks"])
    assert len(current_ids) == manifest["corpus"]["chunk_count"] == 21
    assert manifest["embedding"]["provider"] == "stub"
    assert manifest["input_fingerprint"] == corpus.fingerprint(manifest["inputs"])
    old_by_id = {item["chunk_id"]: item["content"] for item in reference["chunks"]}
    new_by_id = {item["chunk_id"]: item["content"] for item in chunks}
    old_annotations = json.loads((corpus.CORPUS_DIR / "annotations.json").read_text(encoding="utf-8"))
    for before, after in zip(old_annotations, manifest["annotations"], strict=True):
        assert [old_by_id[key] for key in before["positive_chunk_ids"]] == [
            new_by_id[key] for key in after["positive_chunk_ids"]
        ]
    engine = corpus.schema_engine(manifest["schema"])
    try:
        with Session(engine) as session:
            assert session.scalar(text("SELECT current_schema()")) == manifest["schema"]
            document = session.get(Document, UUID(manifest["corpus"]["document_id"]))
            assert document.status is DocumentStatus.READY
            rows = session.scalars(select(DocumentChunk).order_by(DocumentChunk.chunk_index)).all()
            assert [str(row.id) for row in rows] == [item["chunk_id"] for item in chunks]
            assert all(row.embedding is not None and row.search_vector for row in rows)
            assert set(session.scalars(select(func.vector_dims(DocumentChunk.embedding)))) == {1024}
    finally:
        engine.dispose()


def test_manifest_runs_four_production_modes_without_fixed_ids(manifest_path, tmp_path, monkeypatch):
    def forbid_legacy_dataset():
        pytest.fail("manifest 模式不能读取旧 UUID 数据集")

    monkeypatch.setattr(retrieval, "load_dataset", forbid_legacy_dataset)
    assert _run(manifest_path, tmp_path, "all") == 0
    manifest = corpus.load_manifest(manifest_path)
    ids = {item["chunk_id"] for item in manifest["corpus"]["chunks"]}
    for mode in retrieval.RetrievalMode:
        report = _report(tmp_path, "all", mode.value)
        assert report["status"] == "ok"
        assert report["results"]
        assert report["reproducibility"]["manifest"] == manifest
        assert report["reproducibility"]["evidence_kind"] == "pipeline_selftest"
        assert report["model_version"] == "stub-hash-v1"
        for result in report["results"]:
            assert set(result["chunk_ids"]) <= ids
            assert set(result["relevant_ids"]) <= ids
            assert len(result["stable_chunk_ids"]) == len(result["chunk_ids"])
    with (tmp_path / "all" / retrieval.SUMMARY_NAME).open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 4
    assert all(row["status"] == "ok" for row in rows)


def test_same_manifest_repeated_run_is_comparable_and_does_not_overwrite(manifest_path, tmp_path):
    for run_id in ("first", "second"):
        assert _run(manifest_path, tmp_path, run_id) == 0
    for mode in retrieval.RetrievalMode:
        first = _report(tmp_path, "first", mode.value)
        second = _report(tmp_path, "second", mode.value)
        assert first["reproducibility"] == second["reproducibility"]
        assert {key: value for key, value in first["metrics"].items() if key != "latency_p95_ms"} == {
            key: value for key, value in second["metrics"].items() if key != "latency_p95_ms"
        }
        assert [item["stable_chunk_ids"] for item in first["results"]] == [
            item["stable_chunk_ids"] for item in second["results"]
        ]
    before = {path.name: path.read_bytes() for path in (tmp_path / "first").iterdir()}
    assert _run(manifest_path, tmp_path, "first") == 1
    assert before == {path.name: path.read_bytes() for path in (tmp_path / "first").iterdir()}


def test_fresh_ingestion_has_new_uuids_but_same_comparison_identity(manifest_path, tmp_path):
    path = corpus.setup_corpus(tmp_path / "fresh", provider=retrieval.StubHashEmbeddingProvider())
    try:
        first = corpus.load_manifest(manifest_path)
        second = corpus.load_manifest(path)
        assert first["schema"] != second["schema"]
        assert first["corpus"]["document_id"] != second["corpus"]["document_id"]
        assert first["input_fingerprint"] == second["input_fingerprint"]
        assert first["embedding"] == second["embedding"]
        for file, run_id in ((manifest_path, "old"), (path, "new")):
            assert _run(file, tmp_path, run_id, "--configs", "vector_only") == 0
        old, new = _report(tmp_path, "old"), _report(tmp_path, "new")
        assert old["reproducibility"]["comparison_fingerprint"] == new["reproducibility"]["comparison_fingerprint"]
        assert [item["stable_relevant_ids"] for item in old["results"]] == [
            item["stable_relevant_ids"] for item in new["results"]
        ]
    finally:
        corpus.cleanup_manifest(path)


def test_changed_queries_change_comparison_identity(manifest_path, tmp_path):
    assert _run(manifest_path, tmp_path, "short", "--queries", "3", "--configs", "vector_only") == 0
    assert _run(manifest_path, tmp_path, "long", "--queries", "6", "--configs", "vector_only") == 0
    assert _report(tmp_path, "short")["reproducibility"]["comparison_fingerprint"] != _report(
        tmp_path, "long",
    )["reproducibility"]["comparison_fingerprint"]


def test_wrong_embedding_fails_without_fabricated_metrics(manifest_path, tmp_path, monkeypatch):
    provider = retrieval.StubHashEmbeddingProvider()
    provider.model_name = "other-model"
    monkeypatch.setattr(retrieval, "_make_embedding_provider", lambda _: provider)
    assert _run(manifest_path, tmp_path, "mismatch") == 1
    report = json.loads((tmp_path / "mismatch" / "failure.json").read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert "不匹配" in report["detail"]
    assert "metrics" not in report


def test_missing_schema_never_falls_back_to_public(manifest_path, tmp_path):
    manifest = corpus.load_manifest(manifest_path)
    manifest["schema"] = f"benchmark_{uuid4().hex}"
    path = tmp_path / "missing.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert _run(path, tmp_path, "missing") == 1
    report = json.loads((tmp_path / "missing" / "failure.json").read_text(encoding="utf-8"))
    assert "禁止回退 public" in report["detail"]


def test_cleanup_rejects_non_benchmark_schema(manifest_path, tmp_path):
    manifest = corpus.load_manifest(manifest_path)
    manifest["schema"] = "public"
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert setup.main(["--cleanup", str(path)]) == 1


def test_changed_source_fails_and_cleans_only_new_schema(manifest_path, tmp_path):
    source_dir = tmp_path / "changed"
    shutil.copytree(corpus.CORPUS_DIR, source_dir)
    source = source_dir / "python_basics.md"
    source.write_text(source.read_text(encoding="utf-8") + "\n\n新增未标注内容。", encoding="utf-8")
    engine = corpus.schema_engine(corpus.load_manifest(manifest_path)["schema"])
    try:
        with engine.connect() as connection:
            before = set(connection.scalars(text("SELECT nspname FROM pg_namespace")))
        assert setup.main([
            "--self-test", "--corpus-dir", str(source_dir), "--output-dir", str(tmp_path / "failed"),
        ]) == 1
        with engine.connect() as connection:
            assert set(connection.scalars(text("SELECT nspname FROM pg_namespace"))) == before
        assert not (tmp_path / "failed" / "manifest.json").exists()
    finally:
        engine.dispose()


def test_setup_refuses_existing_output(manifest_path):
    before = manifest_path.read_bytes()
    assert setup.main(["--self-test", "--output-dir", str(manifest_path.parent)]) == 1
    assert manifest_path.read_bytes() == before


def test_retrieval_cli_consumes_manifest(manifest_path, tmp_path):
    result = subprocess.run([
        sys.executable, str(Path(retrieval.__file__)), "--manifest", str(manifest_path),
        "--self-test", "--configs", "vector_only", "--output-dir", str(tmp_path), "--run-id", "cli",
    ], capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    assert _report(tmp_path, "cli")["status"] == "ok"


def test_grading_real_retrieval_can_consume_same_manifest(manifest_path, tmp_path, monkeypatch):
    monkeypatch.setattr(grading, "build_embedding", lambda *_: retrieval.StubHashEmbeddingProvider())
    record, _ = grading.run_benchmark(
        mode="real", corpus_path=manifest_path, results_dir=tmp_path,
        strategies=("rag",), provider=grading.SelfTestScoringProvider(),
        settings=build_test_settings(database_url=corpus.get_settings().database_url),
    )
    assert record["status"] == "completed"
    assert record["corpus_manifest"] == corpus.load_manifest(manifest_path)
    assert record["runs"][0]["retriever"] == "VectorSearchRetriever"
    assert record["evidence_kind"] == "pipeline_selftest"
    ids = {item["chunk_id"] for item in record["corpus_manifest"]["corpus"]["chunks"]}
    assert all(set(item["retrieved_context_ids"]) <= ids for item in record["runs"][0]["predictions"])


def test_llm_rerank_reuses_loop_and_records_actual_provider(manifest_path, tmp_path, monkeypatch):
    """受控异步 Provider 模拟 SDK 的循环亲和性；不修改实际重排算法。"""
    from backend.app.ai.retrieval import reranker as rerank_module

    loops = []

    class LoopBoundStub(grading.SelfTestScoringProvider):
        model_name = "actual-rerank-stub"

        async def generate_structured(self, messages, schema, **kwargs):
            loops.append(asyncio.get_running_loop())
            if any(loop is not loops[0] for loop in loops):
                raise RuntimeError("Provider client reused across closed event loops")
            ids = [line.removeprefix("- chunk_id=") for line in messages[-1]["content"].splitlines()
                   if line.startswith("- chunk_id=")]
            return schema(rankings=[{"chunk_id": key, "score": 0.9} for key in ids])

    active = LoopBoundStub()
    monkeypatch.setattr(retrieval, "_make_embedding_provider", lambda _: retrieval.StubHashEmbeddingProvider())
    monkeypatch.setattr(rerank_module, "build_reranker", lambda: LLMRerankAdapter(provider=active))
    assert retrieval.main([
        "--manifest", str(manifest_path), "--output-dir", str(tmp_path), "--run-id", "llm",
        "--configs", "hybrid_rerank", "--queries", "3",
    ]) == 0
    report = _report(tmp_path, "llm", "hybrid_rerank")
    assert len(loops) == 3
    assert len(set(loops)) == 1
    metadata = report["reproducibility"]["comparison"]["retrieval"]["reranker"]
    assert metadata["model"] == "actual-rerank-stub"
    assert metadata["llm"]["provider"] == "stub"
    assert report["reproducibility"]["evidence_kind"] == "pipeline_selftest"


@pytest.mark.parametrize("changed", ["query", "label"])
def test_modified_snapshot_does_not_reuse_comparison_fingerprint(manifest_path, tmp_path, changed):
    manifest = corpus.load_manifest(manifest_path)
    if changed == "query":
        manifest["queries"][0]["query"] = "changed query"
    else:
        manifest["annotations"][0]["positive_chunk_ids"] = []
    path = tmp_path / "changed.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert _run(path, tmp_path, "changed") == 1
    report = json.loads((tmp_path / "changed" / "failure.json").read_text(encoding="utf-8"))
    assert "输入摘要不一致" in report["detail"]


def test_actual_hybrid_config_is_part_of_comparison(manifest_path, tmp_path, monkeypatch):
    from backend.app.ai.retrieval import hybrid_search

    for weight, run_id in ((0.2, "low"), (0.8, "high")):
        settings = build_test_settings(hybrid_vector_weight=weight)
        monkeypatch.setattr(hybrid_search, "get_settings", lambda current=settings: current)
        assert _run(manifest_path, tmp_path, run_id, "--configs", "hybrid") == 0
        report = _report(tmp_path, run_id, "hybrid")
        assert report["reproducibility"]["comparison"]["retrieval"]["vector_weight"] == weight
    assert _report(tmp_path, "low", "hybrid")["reproducibility"]["comparison_fingerprint"] != _report(
        tmp_path, "high", "hybrid",
    )["reproducibility"]["comparison_fingerprint"]


def test_setup_cli_accepts_arbitrary_annotation_keys_and_cleanup(manifest_path, tmp_path):
    """旧 UUID 只是文件内键：全部换成非 UUID 后，正式摄取和评测仍成功。"""
    source_dir = tmp_path / "corpus"
    shutil.copytree(corpus.CORPUS_DIR, source_dir)
    path = source_dir / "chunks.json"
    reference = json.loads(path.read_text(encoding="utf-8"))
    mapping = {item["chunk_id"]: f"label-{index}" for index, item in enumerate(reference["chunks"])}
    for item in reference["chunks"]:
        item["chunk_id"] = mapping[item["chunk_id"]]
    reference["document_id"] = "not-a-database-id"
    path.write_text(json.dumps(reference), encoding="utf-8")
    path = source_dir / "annotations.json"
    annotations = json.loads(path.read_text(encoding="utf-8"))
    for item in annotations:
        item["positive_chunk_ids"] = [mapping[key] for key in item["positive_chunk_ids"]]
        item["selection_methods"] = {mapping[key]: value for key, value in item["selection_methods"].items()}
    path.write_text(json.dumps(annotations), encoding="utf-8")
    output = tmp_path / "runtime"
    initialized = subprocess.run([
        sys.executable, str(Path(setup.__file__)), "--self-test", "--output-dir", str(output),
        "--corpus-dir", str(source_dir),
    ], capture_output=True, text=True, timeout=30, check=False)
    assert initialized.returncode == 0, initialized.stderr
    manifest = output / "manifest.json"
    try:
        current = corpus.load_manifest(manifest)
        assert current["input_fingerprint"] == corpus.load_manifest(manifest_path)["input_fingerprint"]
        assert _run(manifest, tmp_path, "arbitrary", "--configs", "vector_only") == 0
    finally:
        assert setup.main(["--cleanup", str(manifest)]) == 0
    assert _run(manifest, tmp_path, "deleted") == 1
    assert manifest.exists()  # cleanup 保留证据，只删除自有 schema。
