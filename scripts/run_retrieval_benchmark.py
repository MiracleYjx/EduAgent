"""四模式检索 Benchmark 执行器（T045）。

按 ``.specify/plan.md`` §2 的规范分别运行 ``vector_only``、``keyword_only``、``hybrid`` 与
``hybrid_rerank``，把每次运行的完整结果写入 ``benchmark/results/``。默认加载
``benchmark/corpus/`` 下已摄取的 Python 教材、Query 和语义标注；保留旧合成数据入口供
既有契约测试使用：

- 单次运行：``retrieval_<run_id>_<config>.json``（含元数据、指标、逐查询结果与失败原因）。
- 横向比较：``retrieval_summary.csv``（每次运行一行，失败运行同样写入状态与错误码）。

诚实性约束：

- 运行失败（Provider 未就绪、方言不支持等）也写入记录，不伪造指标，不把失败当作 0 分。
- 新数据集使用 ``annotations.json`` 中的多正样本标注，并排除 ``out_of_scope``；旧合成
  数据没有 query-chunk 标签时，才按参考答案所在片段构造单一正样本。
- ``--self-test`` 使用确定性哈希替身 Embedding/Rerank，仅用于验证四模式管道能端到端
  跑通，记录中的 ``model_version`` 会标注为 stub，**不得当作模型质量对比结果**。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import platform
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from backend.app.ai.embedding.base import BaseEmbeddingProvider
from backend.app.ai.embedding.factory import create_embedding_provider
from backend.app.ai.retrieval.base import (
    RetrievalFilters,
    RetrievalMode,
    RetrievalQuery,
    RetrievedChunk,
    get_retriever,
)
from backend.app.ai.retrieval.reranker import BaseReranker, HybridRerankRetriever
from backend.app.core.config import get_settings
from backend.app.core.database import create_database_engine
from backend.app.domain.enums import DocumentStatus, UserRole
from backend.app.models import (
    Course,
    Document,
    DocumentChunk,
    KnowledgeBase,
    Role,
    User,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "benchmark" / "results"
DATASET_PATH = DEFAULT_OUTPUT_DIR / "synthetic_benchmark.json"
CORPUS_DIR = PROJECT_ROOT / "benchmark" / "corpus"
NEW_CHUNKS_PATH = CORPUS_DIR / "chunks.json"
NEW_QUERIES_PATH = CORPUS_DIR / "queries.json"
NEW_ANNOTATIONS_PATH = CORPUS_DIR / "annotations.json"
SUMMARY_NAME = "retrieval_summary_v2.csv"
#: 向量维度必须与 ``document_chunks.embedding`` 列一致。
EMBEDDING_DIMENSION = 1024
DEFAULT_QUERY_LIMIT = 12
RECALL_KS: tuple[int, ...] = (5, 10)


@dataclass(frozen=True, slots=True)
class RetrievalCase:
    """一条检索评测用例：查询、正样本片段与语料内容。"""

    query_id: str
    query: str
    corpus_text: str
    relevant_ids: tuple[str, ...] = ()
    out_of_scope: bool = False


@dataclass(slots=True)
class ConfigRun:
    """单个检索模式的运行结果。"""

    config: RetrievalMode
    per_query: list[dict[str, Any]]
    metrics: dict[str, float]
    status: str = "ok"
    error_code: str | None = None
    error_message: str | None = None


class StubHashEmbeddingProvider(BaseEmbeddingProvider):
    """确定性哈希替身 Embedding：仅供管道自检，不代表任何模型质量。"""

    provider_name = "stub"
    model_name = "stub-hash-v1"

    def __init__(self, dimension: int = EMBEDDING_DIMENSION) -> None:
        self.dimension = dimension

    async def embed_documents(self, documents: Sequence[str]) -> list[list[float]]:
        normalized = self.ensure_documents(documents)
        return self.validate_document_vectors(
            normalized, [self._vector(text_value) for text_value in normalized]
        )

    async def embed_query(self, query: str) -> list[float]:
        return self._vector(self.ensure_query(query))

    def _vector(self, text_value: str) -> list[float]:
        digest = hashlib.sha256(text_value.encode("utf-8")).digest()
        dimension = self.dimension
        if dimension is None:
            raise RuntimeError("替身 Embedding 未设置向量维度。")
        return [digest[index % len(digest)] / 255 for index in range(dimension)]


class IdentityReranker(BaseReranker):
    """确定性重排替身：只把候选顺序映射为递减分数，用于管道自检。"""

    provider_name = "stub"
    model_name = "identity-rerank-v1"

    def rerank(
        self,
        query: str,
        candidates: Sequence[RetrievedChunk],
        top_k: int = 5,
    ) -> list[RetrievedChunk]:
        rescored = [
            _replace_score(candidate, 1.0 / (index + 1))
            for index, candidate in enumerate(candidates)
        ]
        return _with_ranks(rescored[:top_k])


def _replace_score(candidate: RetrievedChunk, score: float) -> RetrievedChunk:
    """复制候选并写入重排分数。"""

    return replace(candidate, rerank_score=score)


def _with_ranks(candidates: Sequence[RetrievedChunk]) -> list[RetrievedChunk]:
    """按当前顺序重排 rank。"""

    return [
        replace(candidate, rank=rank) for rank, candidate in enumerate(candidates)
    ]


def build_retrieval_cases(
    dataset: Mapping[str, Any],
    *,
    limit: int = DEFAULT_QUERY_LIMIT,
) -> list[RetrievalCase]:
    """把新标注数据或旧合成样本转换为检索评测用例。"""

    if "queries" in dataset and "annotations" in dataset:
        annotations = {
            str(item.get("query_id")): item
            for item in dataset.get("annotations", [])
        }
        cases: list[RetrievalCase] = []
        for query in list(dataset.get("queries", []))[:limit]:
            query_id = str(query.get("query_id", ""))
            annotation = annotations.get(query_id, {})
            relevant_ids = tuple(
                str(chunk_id)
                for chunk_id in annotation.get("positive_chunk_ids", [])
            )
            if not query_id or not str(query.get("query", "")).strip():
                continue
            if annotation.get("out_of_scope") or not relevant_ids:
                continue
            cases.append(
                RetrievalCase(
                    query_id=query_id,
                    query=str(query["query"]).strip(),
                    corpus_text="",
                    relevant_ids=relevant_ids,
                    out_of_scope=False,
                )
            )
        return cases

    cases = []
    for case in list(dataset.get("cases", []))[:limit]:
        question = str(case.get("question", "")).strip()
        answer = str(case.get("reference_answer", "")).strip()
        knowledge_points = " ".join(
            str(point).strip() for point in case.get("knowledge_points", [])
        )
        if not question or not answer:
            continue
        # 正样本规则：参考答案所在片段作为唯一相关片段；同一片段包含题干文本，
        # 便于关键词路用术语做精确匹配。
        corpus_text = (
            f"题干：{question}\n参考答案：{answer}\n知识点：{knowledge_points}"
        )
        cases.append(
            RetrievalCase(
                query_id=str(case.get("case_id", f"case-{len(cases)}")),
                query=question,
                corpus_text=corpus_text,
            )
        )
    return cases


def percentile(values: Sequence[float], ratio: float) -> float:
    """返回给定分位的数值（最近秩法，避免插值引入平滑）。"""

    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    if ratio <= 0:
        return ordered[0]
    if ratio >= 1:
        return ordered[-1]
    index = min(len(ordered) - 1, math.ceil(ratio * len(ordered)) - 1)
    return ordered[max(0, index)]


def compute_metrics(
    per_query: Sequence[Mapping[str, Any]],
    *,
    ks: Sequence[int] = RECALL_KS,
) -> dict[str, float]:
    """计算 Recall@k、Precision@k、MRR 与延迟 p95。"""

    metrics: dict[str, float] = {}
    if not per_query:
        for k in ks:
            metrics[f"recall_at_{k}"] = 0.0
            metrics[f"precision_at_{k}"] = 0.0
        metrics["mrr"] = 0.0
        metrics["ndcg_at_10"] = 0.0
        metrics["latency_p95_ms"] = 0.0
        return metrics

    for k in ks:
        recalls: list[float] = []
        precisions: list[float] = []
        for record in per_query:
            retrieved = list(record.get("chunk_ids", []))[:k]
            relevant = set(record.get("relevant_ids", []))
            hits = len([item for item in retrieved if item in relevant])
            recalls.append(hits / len(relevant) if relevant else 0.0)
            precisions.append(hits / len(retrieved) if retrieved else 0.0)
        metrics[f"recall_at_{k}"] = sum(recalls) / len(recalls)
        metrics[f"precision_at_{k}"] = sum(precisions) / len(precisions)

    reciprocal_ranks: list[float] = []
    for record in per_query:
        relevant = set(record.get("relevant_ids", []))
        rank = 0.0
        for index, chunk_id in enumerate(record.get("chunk_ids", []), start=1):
            if chunk_id in relevant:
                rank = 1.0 / index
                break
        reciprocal_ranks.append(rank)
    metrics["mrr"] = sum(reciprocal_ranks) / len(reciprocal_ranks)
    ndcgs: list[float] = []
    for record in per_query:
        relevant = set(record.get("relevant_ids", []))
        retrieved = list(record.get("chunk_ids", []))[:10]
        dcg = sum(
            1.0 / math.log2(index + 2)
            for index, chunk_id in enumerate(retrieved)
            if chunk_id in relevant
        )
        ideal_hits = min(len(relevant), 10)
        idcg = sum(1.0 / math.log2(index + 2) for index in range(ideal_hits))
        ndcgs.append(dcg / idcg if idcg else 0.0)
    metrics["ndcg_at_10"] = sum(ndcgs) / len(ndcgs)
    metrics["latency_p95_ms"] = percentile(
        [float(record.get("latency_ms", 0.0)) for record in per_query], 0.95
    )
    return metrics


def load_dataset(path: Path = DATASET_PATH) -> dict[str, Any]:
    """读取合成数据集；不存在时按既有生成器重新生成（不写随机数据）。"""

    if (
        path == DATASET_PATH
        and NEW_CHUNKS_PATH.is_file()
        and NEW_QUERIES_PATH.is_file()
        and NEW_ANNOTATIONS_PATH.is_file()
    ):
        chunks = json.loads(NEW_CHUNKS_PATH.read_text(encoding="utf-8"))
        queries = json.loads(NEW_QUERIES_PATH.read_text(encoding="utf-8"))
        annotations = json.loads(NEW_ANNOTATIONS_PATH.read_text(encoding="utf-8"))
        return {
            "metadata": {
                "dataset_version": "synthetic-python-basics-v2",
                "model_version": "bge-large-zh-v1.5",
                "prompt_version": "llm-rerank-v1",
            },
            "corpus": chunks,
            "queries": queries,
            "annotations": annotations,
        }
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    from scripts.generate_synthetic_benchmark import generate_synthetic_benchmark

    return dict(generate_synthetic_benchmark())


def _make_embedding_provider(self_test: bool) -> BaseEmbeddingProvider:
    """构造 Embedding Provider：自检使用确定性替身，否则使用运行配置。"""

    if self_test:
        return StubHashEmbeddingProvider()
    return create_embedding_provider()


def _embed_texts(provider: BaseEmbeddingProvider, texts: Sequence[str]) -> list[list[float]]:
    """同步调用异步 Embedding Provider，批量编码语料文档。"""

    return asyncio.run(provider.embed_documents(list(texts)))


def _embed_queries(provider: BaseEmbeddingProvider, queries: Sequence[str]) -> list[list[float]]:
    """逐条调用查询编码接口，保留 Provider 的查询前缀和语义约定。"""

    async def encode() -> list[list[float]]:
        return [await provider.embed_query(query) for query in queries]

    return asyncio.run(encode())


def seed_corpus(
    session: Session,
    cases: Sequence[RetrievalCase],
    vectors: Sequence[Sequence[float]],
    *,
    run_id: str,
) -> dict[str, Any]:
    """写入完整评测语料后同事务设置 Ready，返回过滤与清理标识。"""

    teacher = User(
        username=f"benchmark-{run_id}",
        email=f"benchmark-{run_id}@example.com",
        password_hash="hashed-password",
    )
    existing_role = session.scalar(select(Role).where(Role.name == UserRole.TEACHER))
    teacher.roles.append(existing_role or Role(name=UserRole.TEACHER, description="教师"))
    course = Course(name=f"Benchmark 课程 {run_id}", creator=teacher)
    knowledge_base = KnowledgeBase(name=f"Benchmark 知识库 {run_id}", course=course)
    document = Document(
        original_filename=f"benchmark-{run_id}.txt",
        file_format="txt",
        course=course,
        knowledge_base=knowledge_base,
        uploader=teacher,
    )
    session.add(document)
    session.commit()

    chunks: list[DocumentChunk] = []
    for index, case in enumerate(cases):
        chunk = DocumentChunk(
            document_id=document.id,
            course_id=course.id,
            knowledge_base_id=knowledge_base.id,
            chunk_index=index,
            content=case.corpus_text,
            embedding=list(vectors[index]),
            chunk_metadata={
                "document_id": str(document.id),
                "course_id": str(course.id),
                "chunk_index": index,
                "query_id": case.query_id,
            },
        )
        chunk.search_vector = func.to_tsvector("simple", chunk.content)
        chunks.append(chunk)
    session.add_all(chunks)
    # 评测种子已有文档向量和全文数据，与 Ready 一起提交后才允许检索。
    document.status = DocumentStatus.READY
    session.commit()

    return {
        "user_id": teacher.id,
        "course_id": course.id,
        "knowledge_base_id": knowledge_base.id,
        "document_id": document.id,
        "chunk_ids": [chunk.id for chunk in chunks],
    }


def cleanup_corpus(session: Session, seed: Mapping[str, Any]) -> None:
    """删除本次运行写入的语料，避免污染开发库。"""

    course = session.get(Course, seed.get("course_id"))
    if course is not None:
        session.delete(course)
    teacher = session.get(User, seed.get("user_id"))
    if teacher is not None:
        session.delete(teacher)
    session.commit()


def _build_retriever(
    config: RetrievalMode,
    *,
    self_test: bool,
) -> Any:
    """按模式构造检索实现；hybrid_rerank 注入自检替身或运行配置的 Reranker。"""

    if config is RetrievalMode.HYBRID_RERANK:
        return HybridRerankRetriever(
            reranker=IdentityReranker() if self_test else None,
            fusion_top_k=20,
        )
    return get_retriever(config)


def run_config(
    session: Session,
    config: RetrievalMode,
    cases: Sequence[RetrievalCase],
    vectors: Sequence[Sequence[float]] | None,
    chunk_ids: Sequence[Any],
    scope: RetrievalFilters,
    *,
    top_k: int,
    self_test: bool,
) -> ConfigRun:
    """在给定模式上执行全部查询并汇总指标。"""

    needs_embedding = config is not RetrievalMode.KEYWORD_ONLY
    if needs_embedding and vectors is None:
        return ConfigRun(
            config=config,
            per_query=[],
            metrics=compute_metrics([]),
            status="failed",
            error_code="EMBEDDING_PROVIDER_NOT_READY",
            error_message="Embedding Provider 未就绪，无法构造查询向量。",
        )
    query_vectors: Sequence[Sequence[float]] = vectors or [() for _ in cases]
    per_query: list[dict[str, Any]] = []
    try:
        retriever = _build_retriever(config, self_test=self_test)
        for index, (case, vector) in enumerate(
            zip(cases, query_vectors, strict=False)
        ):
            # 单路召回只接受自己需要的形式；Hybrid 需要同时提供文本与向量。
            if config is RetrievalMode.VECTOR_ONLY:
                argument: Any = vector
            elif config is RetrievalMode.KEYWORD_ONLY:
                argument = case.query
            else:
                argument = RetrievalQuery(case.query, tuple(vector))
            started = time.perf_counter()
            results = retriever.search(
                session,
                argument,
                top_k=max(RECALL_KS, default=top_k),
                filters=scope,
            )
            latency_ms = (time.perf_counter() - started) * 1000.0
            per_query.append(
                {
                    "query_id": case.query_id,
                    "chunk_ids": [item.chunk_id for item in results],
                    "scores": [round(float(item.score), 6) for item in results],
                    "relevant_ids": list(
                        case.relevant_ids
                        or ((str(chunk_ids[index]),) if chunk_ids else ())
                    ),
                    "latency_ms": round(latency_ms, 3),
                }
            )
    except Exception as exc:  # noqa: BLE001  # 失败必须写入记录，不得伪造指标
        return ConfigRun(
            config=config,
            per_query=[],
            metrics=compute_metrics([]),
            status="failed",
            error_code=getattr(exc, "error_code", type(exc).__name__),
            error_message=f"{type(exc).__name__}: {exc}",
        )
    return ConfigRun(
        config=config,
        per_query=per_query,
        metrics=compute_metrics(per_query),
        status="ok",
    )


def write_run_records(
    run: ConfigRun,
    *,
    run_id: str,
    output_dir: Path,
    dataset_version: str,
    model_version: str,
    prompt_version: str | None,
    top_k: int,
    analysis: str,
) -> Path:
    """写入单次运行 JSON（失败运行同样写入错误状态）并追加汇总行。"""

    output_dir.mkdir(parents=True, exist_ok=True)
    run_at = datetime.now(UTC).isoformat()
    payload = {
        "run_id": run_id,
        "run_at": run_at,
        "config": run.config.value,
        "dataset_version": dataset_version,
        "model_version": model_version,
        "prompt_version": prompt_version,
        "status": run.status,
        "error_code": run.error_code,
        "error_message": run.error_message,
        "environment": {
            "cpu": platform.processor() or platform.machine(),
            "memory_mb": 0,
            "python": platform.python_version(),
            "top_k": top_k,
        },
        "metrics": run.metrics if run.status == "ok" else {},
        "results": run.per_query,
        "analysis": analysis,
    }
    result_path = output_dir / f"retrieval_{run_id}_{run.config.value}.json"
    result_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    summary_path = output_dir / SUMMARY_NAME
    header = (
        "run_id,run_at,config,dataset_version,model_version,prompt_version,status,"
        "error_code,recall_at_5,recall_at_10,precision_at_5,mrr,ndcg_at_10,"
        "latency_p95_ms,result_path\n"
    )
    if not summary_path.is_file():
        summary_path.write_text(header, encoding="utf-8")
    row = ",".join(
        [
            run_id,
            run_at,
            run.config.value,
            dataset_version,
            model_version,
            prompt_version or "",
            run.status,
            run.error_code or "",
            *[
                format(run.metrics[key], precision)
                if run.status == "ok" and key in run.metrics else ""
                for key, precision in (
                    ("recall_at_5", ".4f"),
                    ("recall_at_10", ".4f"),
                    ("precision_at_5", ".4f"),
                    ("mrr", ".4f"),
                    ("ndcg_at_10", ".4f"),
                    ("latency_p95_ms", ".3f"),
                )
            ],
            result_path.name,
        ]
    )
    with summary_path.open("a", encoding="utf-8") as handle:
        handle.write(row + "\n")
    return result_path


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(description="四模式检索 Benchmark 执行器")
    parser.add_argument("--run-id", default=None, help="运行标识，默认按时间生成。")
    parser.add_argument("--queries", type=int, default=DEFAULT_QUERY_LIMIT)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="使用确定性替身 Embedding/Rerank 验证四模式管道（结果标注为 stub）。",
    )
    parser.add_argument(
        "--configs",
        nargs="+",
        default=[mode.value for mode in RetrievalMode],
        choices=[mode.value for mode in RetrievalMode],
    )
    return parser.parse_args(argv)


def _load_ingested_corpus(
    session: Session,
    dataset: Mapping[str, Any],
) -> tuple[UUID, list[str]]:
    """校验新教材已摄取为 Ready，并返回资料范围与 chunk 标识。"""

    corpus = dataset.get("corpus")
    if not isinstance(corpus, Mapping):
        raise TypeError("新数据集缺少 corpus manifest。")
    document_id = UUID(str(corpus.get("document_id")))
    expected = [str(item["chunk_id"]) for item in corpus.get("chunks", [])]
    rows = list(
        session.scalars(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.chunk_index)
        )
    )
    actual = [str(row.id) for row in rows]
    if actual != expected:
        raise ValueError(
            f"摄取 chunk 与 manifest 不一致（数据库 {len(actual)}，manifest {len(expected)}）。"
        )
    document = session.get(Document, document_id)
    if document is None or document.status is not DocumentStatus.READY:
        raise ValueError("教材文档未处于 Ready 状态。")
    if any(row.embedding is None or row.search_vector is None for row in rows):
        raise ValueError("教材 chunk 缺少 embedding 或全文检索字段。")
    return document_id, actual


def main(argv: Sequence[str] | None = None) -> int:
    """执行四模式检索 Benchmark。"""

    args = parse_args(argv)
    run_id = args.run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dataset = load_dataset()
    is_ingested_dataset = "corpus" in dataset and "queries" in dataset
    cases = build_retrieval_cases(dataset, limit=args.queries)
    if not cases:
        print("未构造出任何检索用例，请检查合成数据集。", file=sys.stderr)
        return 1

    engine = create_database_engine(get_settings(), connect_timeout=5)
    failed_configs = 0
    session: Session | None = None
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            tables = {
                str(row[0])
                for row in connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public'"
                    )
                )
            }
        if "document_chunks" not in tables:
            print("document_chunks 表不存在，请先执行 alembic upgrade head。", file=sys.stderr)
            return 1

        corpus_vectors: list[list[float]] | None = None
        query_vectors: list[list[float]] | None = None
        embedding_error: Exception | None = None
        provider: BaseEmbeddingProvider | None = None
        try:
            provider = _make_embedding_provider(args.self_test)
            if not is_ingested_dataset:
                corpus_vectors = _embed_texts(
                    provider, [case.corpus_text for case in cases]
                )
            # 查询逐条使用 embed_query，保留模型查询前缀与 Provider 约定。
            query_vectors = _embed_queries(provider, [case.query for case in cases])
        except Exception as exc:  # noqa: BLE001  # 原始失败按模式写入记录
            embedding_error = exc
            print(f"Embedding 执行失败：{type(exc).__name__}: {exc}", file=sys.stderr)

        session = Session(bind=engine, expire_on_commit=False)
        seed: dict[str, Any] = {}
        seeded_temp = False
        if is_ingested_dataset:
            try:
                document_id, chunk_ids = _load_ingested_corpus(session, dataset)
                seed = {"document_id": document_id}
            except Exception as exc:  # noqa: BLE001
                embedding_error = embedding_error or exc
                chunk_ids = []
                print(f"教材校验失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        elif corpus_vectors is not None:
            seed = seed_corpus(session, cases, corpus_vectors, run_id=run_id)
            seeded_temp = True
        # 缺少文档向量时不能伪造 Ready；查询编码失败不影响已有完整语料的关键词检索。
        if not is_ingested_dataset:
            chunk_ids = list(seed.get("chunk_ids", []))
        scope = RetrievalFilters(
            document_ids=(seed["document_id"],) if seed else (),
        )

        model_version = (
            "stub-hash-v1"
            if args.self_test
            else (
                "bge-large-zh-v1.5"
                if is_ingested_dataset
                else (get_settings().embedding_model or "unknown")
            )
        )
        analysis = (
            "harness 自检运行：使用确定性替身 Embedding/Rerank 验证四模式管道，"
            "不得作为模型质量对比结果。"
            if args.self_test
            else "真实 Provider 运行：按教材标注计算指标，未就绪模式记录失败原因。"
        )

        for config_value in args.configs:
            config = RetrievalMode(config_value)
            if embedding_error is not None and (
                not seed or config is not RetrievalMode.KEYWORD_ONLY
            ):
                reason = (
                    "无法建立包含向量和全文索引的 Ready 评测语料"
                    if not seed else "无法生成查询向量"
                )
                run = ConfigRun(
                    config=config,
                    per_query=[],
                    metrics={},
                    status="failed",
                    error_code=getattr(embedding_error, "error_code", type(embedding_error).__name__),
                    error_message=f"{reason}：{type(embedding_error).__name__}: {embedding_error}",
                )
            else:
                run = run_config(
                    session,
                    config,
                    cases,
                    query_vectors,
                    chunk_ids,
                    scope,
                    top_k=args.top_k,
                    self_test=args.self_test,
                )
            path = write_run_records(
                run,
                run_id=run_id,
                output_dir=args.output_dir,
                dataset_version=str(
                    dataset.get("metadata", {}).get("dataset_version", "unknown")
                ),
                model_version=model_version,
                prompt_version=(
                    "llm-rerank-v1"
                    if is_ingested_dataset
                    else str(dataset.get("metadata", {}).get("prompt_version", ""))
                    or None
                ),
                top_k=args.top_k,
                analysis=analysis,
            )
            state = "OK" if run.status == "ok" else f"FAILED({run.error_code})"
            print(f"[{config.value}] {state} -> {path.name}")
            if run.status != "ok":
                failed_configs += 1

        if session is not None and seed and seeded_temp:
            cleanup_corpus(session, seed)
        return 1 if failed_configs else 0
    finally:
        if session is not None:
            session.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
