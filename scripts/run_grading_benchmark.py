"""T059 阅卷策略 Benchmark：Zero-shot / RAG / Hybrid + Rerank 三路对比。

契约依据：T059、FR-032、plan §7（Benchmark 输出与记录边界）、宪章 V（诚实证据）。

设计要点：

- **独立实验入口**（B06）：三路策略各自组装输入并复用**公开**消息构造
  （:func:`backend.app.services.grading.subjective_grader.build_grading_messages`）、
  Payload Schema（``SubjectiveGradingPayload``）、``generate_structured`` 与结果校验
  （:func:`parse_subjective_payload`），不直接复用正式评分入口的上下文充分性强制检查，
  也不修改正式评分约束。
  - ``zero_shot``：不检索，Final Context 为空；
  - ``rag``：``VECTOR_ONLY`` 检索，不重排；
  - ``hybrid_rerank``：正式检索组合（Hybrid + Rerank）。
- **样本与指标**（B07）：数据集 ``benchmark/corpus/grading_samples.json`` 提供题型、题目、
  标准答案、rubric、学生答案、满分、知识点与标注来源；``teacher_score``（教师评分）与
  ``reference_score``（合成参考分）分开记录。**没有教师 Ground Truth 时** MAE/RMSE/一致率
  一律为 ``null`` 并说明原因，只报告三路的调用次数、耗时与实际输出差异，不据此判断评分质量。
- **输出边界**（plan §7）：结果写入 ``benchmark/results/grading_<run_id>.json`` 并追加
  ``grading_summary.csv``；只记录数据集、模型、Prompt 版本、策略、指标与耗时，
  **不写密钥、不写完整 Prompt、不写学生答案原文**。
- **失败也如实落盘**：单样本失败记录 ``status=failed`` 与脱敏错误码，不写假分数；
  未执行样本单独记录，不参与失败率。全失败为 ``failed``，部分失败为 ``partial_failed``，
  两者均非零退出。调用数统计实际发起的评分请求，不包含 Provider 内部重试。

用法（自检，无需外部服务）::

    python scripts/run_grading_benchmark.py --mode selftest

真实模型评测（需要 LLM、Embedding 配置，以及按 M2 流程摄取为 Ready 的评测语料）::

    python scripts/run_grading_benchmark.py --mode real
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

from sqlalchemy.orm import Session

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.ai.embedding import factory as embedding_factory
from backend.app.ai.embedding.base import (
    BaseEmbeddingProvider,
    EmbeddingProviderError,
    EmbeddingProviderNotReadyError,
)
from backend.app.ai.llm.base import BaseLLMProvider
from backend.app.ai.llm.factory import create_llm_provider
from backend.app.ai.retrieval.base import (
    DEFAULT_TOP_K,
    BaseRetriever,
    RetrievalFilters,
    RetrievalMode,
    RetrievedChunk,
    get_retriever,
)
from backend.app.ai.retrieval.reranker import (
    DEFAULT_CROSS_ENCODER_MODEL,
    BaseReranker,
    RerankProviderNotReadyError,
    build_reranker,
)
from backend.app.core.config import AppSettings, get_settings
from backend.app.core.database import create_database_engine
from backend.app.domain.enums import QuestionType
from backend.app.models import Document
from backend.app.services.grading.grading_context import (
    GradingContext,
    GradingContextError,
    SubjectiveGradingSource,
    build_grading_context,
)
from backend.app.services.grading.question_router import normalize_question_type
from backend.app.services.grading.subjective_grader import (
    SUBJECTIVE_GRADING_PROMPT_VERSION,
    SubjectiveGradingError,
    SubjectiveGradingPayload,
    build_grading_messages,
    parse_subjective_payload,
)
from scripts.run_retrieval_benchmark import _load_ingested_corpus

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
DATASET_PATH: Final[Path] = REPO_ROOT / "benchmark" / "corpus" / "grading_samples.json"
CORPUS_PATH: Final[Path] = REPO_ROOT / "benchmark" / "corpus" / "chunks.json"
RESULTS_DIR: Final[Path] = REPO_ROOT / "benchmark" / "results"
SUMMARY_FILENAME: Final[str] = "grading_summary.csv"

#: 三路策略标识；顺序即对比顺序。
STRATEGIES: Final[tuple[str, ...]] = ("zero_shot", "rag", "hybrid_rerank")

#: 未提供教师评分时的指标不可计算说明。
NO_GROUND_TRUTH_REASON: Final[str] = (
    "缺少教师人工评分 Ground Truth：MAE/RMSE/一致率不可计算，"
    "此处只报告三路相对调用与耗时表现。"
)

#: 一致率判定容差（分数单位：分）。
AGREEMENT_TOLERANCE: Final[Decimal] = Decimal("1.00")

#: 仅自检使用固定课程标识；真实模式从已摄取资料读取所属课程。
BENCHMARK_COURSE_ID: Final[str] = "00000000-0000-0000-0000-000000000059"

#: 自检语料：供 RAG / Hybrid 路径检索的最小片段集合。
SELFTEST_CHUNKS: Final[tuple[tuple[str, str], ...]] = (
    ("chunk-1", "变量用于保存数据，并可在后续语句中引用。"),
    ("chunk-2", "函数参数默认值让调用方可以省略该参数，未传参时使用默认值。"),
    ("chunk-3", "可变对象作为默认参数会在多次调用间共享，导致状态意外累积。"),
    ("chunk-4", "列表推导式在单个表达式内构建列表，通常更快且更简洁。"),
)


@dataclass(frozen=True, slots=True)
class GradingCase:
    """一条阅卷评测样本（含标注来源，教师评分与合成参考分分开）。"""

    sample_id: str
    question_type: QuestionType
    question: str
    reference_answer: str
    scoring_rubric: str
    student_answer: str
    max_score: Decimal
    knowledge_points: tuple[str, ...]
    reference_score: Decimal | None
    teacher_score: Decimal | None
    label_source: str


@dataclass(slots=True)
class StrategyRun:
    """一路策略的运行结果。"""

    strategy: str
    status: str = "completed"
    error_code: str | None = None
    provider_calls: int = 0
    retrieval_calls: int = 0
    rerank_calls: int = 0
    elapsed_ms: float = 0.0
    predictions: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    not_executed: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    fatal_error: Exception | None = None


class StubEmbeddingProvider(BaseEmbeddingProvider):
    """自检用确定性 Embedding；不加载外部模型。"""

    def __init__(self, dimension: int = 8) -> None:
        self.dimension = dimension

    async def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        seed = sum(ord(char) for char in text) or 1
        return [((seed + index) % 97) / 97 for index in range(self.dimension)]


class StubRetriever(BaseRetriever):
    """自检用固定候选检索；按查询词命中数排序，保证可重复。"""

    def __init__(self, chunks: Sequence[tuple[str, str]] = SELFTEST_CHUNKS) -> None:
        self._chunks = list(chunks)
        self.calls = 0

    def search(
        self,
        session: Any,
        query: Any,
        *,
        top_k: int = DEFAULT_TOP_K,
        filters: Any = None,
    ) -> list[RetrievedChunk]:
        self.calls += 1
        text = query.text if hasattr(query, "text") else str(query)
        ordered = sorted(
            self._chunks,
            key=lambda item: (-_hit_count(text, item[1]), item[0]),
        )
        return [
            RetrievedChunk(
                chunk_id=chunk_id,
                course_id="benchmark-course",
                document_id="benchmark-document",
                content=content,
                rank=rank,
                semantic_score=0.5,
            )
            for rank, (chunk_id, content) in enumerate(ordered[:top_k])
        ]


class IdentityReranker(BaseReranker):
    """自检用重排：保持检索顺序，仅记录调用次数。"""

    provider_name = "selftest"
    model_name = "identity"

    def __init__(self) -> None:
        self.calls = 0

    def rerank(
        self,
        query: str,
        candidates: Sequence[RetrievedChunk],
        top_k: int = DEFAULT_TOP_K,
    ) -> list[RetrievedChunk]:
        """同步重排入口（自检用）：按输入顺序截断，仅记录调用次数。"""

        self.calls += 1
        return list(candidates)[:top_k]

    async def rerank_async(
        self,
        query: str,
        candidates: Sequence[RetrievedChunk],
        top_k: int = DEFAULT_TOP_K,
    ) -> list[RetrievedChunk]:
        self.calls += 1
        return list(candidates)[:top_k]


class SelfTestScoringProvider(BaseLLMProvider):
    """自检用评分 Provider：按参考分与关键词命中给出确定性分数。"""

    def __init__(self) -> None:
        self.calls = 0

    async def generate_structured(
        self,
        messages: Sequence[Mapping[str, Any]],
        schema: type[SubjectiveGradingPayload],
        **kwargs: Any,
    ) -> SubjectiveGradingPayload:
        self.calls += 1
        text = "\n".join(str(message.get("content", "")) for message in messages)
        max_score = _extract_max_score(text)
        hits = _hit_count(text, "引用") + _hit_count(text, "保存")
        score = min(max_score, max(0.0, max_score * 0.6 + hits))
        return schema(
            score=round(score, 2),
            confidence=0.8,
            reason="自检评分：依据命中要点数量给出确定性分数。",
            correct_points=["命中要点"] if hits else [],
            missing_knowledge_points=[] if hits else ["未命中要点"],
            suggestions=["自检模式仅用于验证脚本可重复执行。"],
        )


def _hit_count(text: str, keyword: str) -> int:
    """统计关键词命中次数。"""

    return text.count(keyword)


def _extract_max_score(text: str) -> float:
    """从消息文本中读取「本题满分」行；缺失时回退为 10 分。"""

    for line in text.splitlines():
        if "本题满分" in line:
            digits = "".join(
                char for char in line if char.isdigit() or char == "."
            )
            try:
                return float(digits) or 10.0
            except ValueError:
                return 10.0
    return 10.0


def load_dataset(path: Path = DATASET_PATH) -> dict[str, Any]:
    """读取阅卷样本数据集；结构非法时显式失败。"""

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("samples"), list):
        raise TypeError(f"数据集结构非法：{path}")
    return raw


def build_cases(dataset: Mapping[str, Any]) -> list[GradingCase]:
    """把数据集条目转换为评测样本；缺失字段显式失败。"""

    cases: list[GradingCase] = []
    for item in dataset["samples"]:
        cases.append(
            GradingCase(
                sample_id=str(item["sample_id"]),
                question_type=normalize_question_type(item["question_type"]),
                question=str(item["question"]),
                reference_answer=str(item["reference_answer"]),
                scoring_rubric=str(item["scoring_rubric"]),
                student_answer=str(item["student_answer"]),
                max_score=Decimal(str(item["max_score"])),
                knowledge_points=tuple(item.get("knowledge_points") or ()),
                reference_score=_optional_decimal(item.get("reference_score")),
                teacher_score=_optional_decimal(item.get("teacher_score")),
                label_source=str(item.get("label_source") or "unknown"),
            )
        )
    return cases


def _optional_decimal(value: Any) -> Decimal | None:
    """把可选分数转换为 Decimal；缺失时返回 None。"""

    if value is None:
        return None
    return Decimal(str(value))


def _source_for(
    case: GradingCase, *, course_id: str = BENCHMARK_COURSE_ID,
) -> SubjectiveGradingSource:
    """构造评分输入（学生答案不写入结果文件）。"""

    return SubjectiveGradingSource(
        question_type=case.question_type,
        course_id=course_id,
        question_content=case.question,
        reference_answer=case.reference_answer,
        student_answer=case.student_answer,
        scoring_rubric=case.scoring_rubric,
        knowledge_points=case.knowledge_points,
        question_id=case.sample_id,
        answer_id=case.sample_id,
        submission_id=case.sample_id,
    )


async def _build_context(
    strategy: str,
    case: GradingCase,
    *,
    retriever: BaseRetriever | None,
    reranker: BaseReranker | None,
    embedding: BaseEmbeddingProvider,
    settings: AppSettings,
    session: Session | None = None,
    course_id: str = BENCHMARK_COURSE_ID,
    filters: RetrievalFilters | None = None,
) -> GradingContext:
    """按策略组装输入上下文；Zero-shot 不检索。"""

    source = _source_for(case, course_id=course_id)
    if strategy == "zero_shot":
        return GradingContext(
            source=source,
            query_text="",
            retrieval_mode=RetrievalMode.VECTOR_ONLY,
            filters=_empty_filters(),
            chunks=(),
            retrieved_context_ids=(),
            final_context="",
            candidate_count=0,
        )
    mode = RetrievalMode.VECTOR_ONLY if strategy == "rag" else RetrievalMode.HYBRID_RERANK
    return await build_grading_context(
        session,  # type: ignore[arg-type] - 仅自检允许不提供数据库会话
        source,
        mode=mode,
        retriever=retriever,
        reranker=reranker,
        embedding_provider=embedding,
        filters=filters,
        settings=settings,
        require_context=False,
    )


def _empty_filters() -> Any:
    """返回空检索过滤器。"""

    from backend.app.ai.retrieval.base import RetrievalFilters

    return RetrievalFilters()


async def run_strategy(
    strategy: str,
    cases: Sequence[GradingCase],
    *,
    provider: BaseLLMProvider,
    retriever: BaseRetriever | None,
    reranker: BaseReranker | None,
    embedding: BaseEmbeddingProvider,
    settings: AppSettings,
    session: Session | None = None,
    course_id: str = BENCHMARK_COURSE_ID,
    filters: RetrievalFilters | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> StrategyRun:
    """执行一路策略；逐样本记录预测或失败，不写假分数。"""

    run = StrategyRun(strategy=strategy)
    started = clock()
    for case in cases:
        try:
            context = await _build_context(
                strategy,
                case,
                retriever=retriever,
                reranker=reranker,
                embedding=embedding,
                settings=settings,
                session=session,
                course_id=course_id,
                filters=filters,
            )
            if strategy != "zero_shot":
                run.retrieval_calls += 1
            if strategy == "hybrid_rerank" and context.candidate_count:
                run.rerank_calls += 1
            messages = build_grading_messages(context, max_score=float(case.max_score))
            run.provider_calls += 1
            payload = await _generate(provider, messages)
            result = parse_subjective_payload(
                payload.model_dump(),
                question_type=case.question_type,
                max_score=float(case.max_score),
                knowledge_points=case.knowledge_points,
                retrieved_context_ids=context.retrieved_context_ids,
                answer_id=case.sample_id,
                submission_id=case.sample_id,
            )
        except (SubjectiveGradingError, GradingContextError) as error:
            run.failures.append(_failure_record(case, getattr(error, "error_code", "GRADING_FAILED")))
            continue
        except ProviderNotReady as error:
            run.error_code = str(error)
            run.failures.append(_failure_record(case, run.error_code))
            break
        except (EmbeddingProviderNotReadyError, RerankProviderNotReadyError) as error:
            # 先保留失败样本，再维持 B03 的整批中止语义。
            run.error_code = error.error_code
            run.failures.append(_failure_record(case, run.error_code))
            run.fatal_error = error
            break
        except EmbeddingProviderError as error:
            run.error_code = error.error_code
            run.failures.append(_failure_record(case, run.error_code))
            break
        except Exception as error:  # noqa: BLE001 - 统一收敛为脱敏失败
            run.failures.append(_failure_record(case, _error_code(error)))
            continue
        run.predictions.append(
            {
                "sample_id": case.sample_id,
                "status": "completed",
                "predicted_score": str(result.score),
                "max_score": str(case.max_score),
                "teacher_score": (
                    None if case.teacher_score is None else str(case.teacher_score)
                ),
                "reference_score": (
                    None if case.reference_score is None else str(case.reference_score)
                ),
                "label_source": case.label_source,
                "confidence": result.confidence,
                "knowledge_points": list(result.knowledge_points),
                "retrieved_context_ids": list(result.retrieved_context_ids),
            }
        )
    attempted = len(run.predictions) + len(run.failures)
    run.not_executed = [
        {"sample_id": case.sample_id, "status": "not_executed"}
        for case in cases[attempted:]
    ]
    run.status = (
        "failed" if not run.predictions
        else "partial_failed" if run.failures or run.not_executed else "completed"
    )
    if run.error_code is None and run.failures:
        run.error_code = run.failures[0]["error_code"]
    run.elapsed_ms = round((clock() - started) * 1000, 3)
    run.metrics = compute_metrics(run.predictions, total=len(cases), failures=len(run.failures))
    return run


async def _generate(
    provider: BaseLLMProvider,
    messages: Sequence[Mapping[str, Any]],
) -> SubjectiveGradingPayload:
    """调用 Provider 获取结构化评分 Payload。"""

    try:
        return await provider.generate_structured(messages, SubjectiveGradingPayload)
    except Exception as error:  # noqa: BLE001 - 未就绪按显式失败处理
        raise ProviderNotReady(_error_code(error)) from None


def _error_code(error: Exception) -> str:
    """保留来源业务错误码；未知异常仅记录类型，不记录敏感正文。"""

    return str(getattr(error, "error_code", None) or getattr(error, "code", None)
               or type(error).__name__)


class ProviderNotReady(RuntimeError):
    """Provider 未就绪；整体运行标记失败，不伪造分数。"""


def _failure_record(case: GradingCase, error_code: str) -> dict[str, Any]:
    """构造失败记录；只含标识与脱敏错误码。"""

    return {
        "sample_id": case.sample_id,
        "status": "failed",
        "error_code": error_code,
    }


def compute_metrics(
    predictions: Sequence[Mapping[str, Any]],
    *,
    total: int,
    failures: int,
) -> dict[str, Any]:
    """计算质量指标；缺教师 Ground Truth 时明确不可计算。

    MAE/RMSE 的量纲是与教师评分一致的分数量纲（数据集 score_scale），一致率定义为
    预测分与教师评分绝对差不超过 ``AGREEMENT_TOLERANCE``（1 分）的样本占比；
    有效样本集合为同时具备教师评分与预测分的样本。
    失败率分母为已成功或失败的样本数，不包含中止后未执行的样本。
    """

    paired = [
        (Decimal(str(item["predicted_score"])), Decimal(str(item["teacher_score"])))
        for item in predictions
        if item.get("teacher_score") not in (None, "")
    ]
    metrics: dict[str, Any] = {
        "sample_total": total,
        "scored_count": len(predictions),
        "failure_count": failures,
        "attempted_count": len(predictions) + failures,
        "not_executed_count": total - len(predictions) - failures,
        "failure_rate": (
            round(failures / (len(predictions) + failures), 4)
            if predictions or failures else None
        ),
        "effective_sample_count": len(paired),
        "mae": None,
        "rmse": None,
        "agreement_rate": None,
        "metrics_unavailable_reason": None,
    }
    if not paired:
        metrics["metrics_unavailable_reason"] = NO_GROUND_TRUTH_REASON
        return metrics
    errors = [predicted - actual for predicted, actual in paired]
    metrics["mae"] = str(
        (sum(abs(error) for error in errors) / Decimal(len(errors))).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    )
    metrics["rmse"] = str(
        Decimal(
            math.sqrt(sum(float(error) ** 2 for error in errors) / len(errors))
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )
    agreed = sum(
        1 for predicted, actual in paired if abs(predicted - actual) <= AGREEMENT_TOLERANCE
    )
    metrics["agreement_rate"] = str(
        (Decimal(agreed) / Decimal(len(paired))).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )
    )
    return metrics


def write_run_records(
    record: Mapping[str, Any],
    *,
    results_dir: Path = RESULTS_DIR,
    run_id: str,
) -> tuple[Path, Path]:
    """写入单次 JSON 结果并追加 CSV 汇总；返回两个文件路径。"""

    results_dir.mkdir(parents=True, exist_ok=True)
    json_path = results_dir / f"grading_{run_id}.json"
    json_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    csv_path = results_dir / SUMMARY_FILENAME
    new_file = not csv_path.exists()
    with csv_path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        if new_file:
            writer.writerow(
                [
                    "run_id",
                    "created_at",
                    "mode",
                    "dataset_version",
                    "model",
                    "prompt_version",
                    "strategy",
                    "status",
                    "error_code",
                    "sample_total",
                    "scored_count",
                    "failure_count",
                    "failure_rate",
                    "effective_sample_count",
                    "mae",
                    "rmse",
                    "agreement_rate",
                    "provider_calls",
                    "retrieval_calls",
                    "rerank_calls",
                    "elapsed_ms",
                ]
            )
        for run in record["runs"]:
            metrics = run["metrics"]
            writer.writerow(
                [
                    run_id,
                    record["created_at"],
                    record["mode"],
                    record["dataset"]["version"],
                    record["model"],
                    record["prompt_version"],
                    run["strategy"],
                    run["status"],
                    run["error_code"] or "",
                    metrics["sample_total"],
                    metrics["scored_count"],
                    metrics["failure_count"],
                    metrics["failure_rate"],
                    metrics["effective_sample_count"],
                    metrics["mae"] or "",
                    metrics["rmse"] or "",
                    metrics["agreement_rate"] or "",
                    run["provider_calls"],
                    run["retrieval_calls"],
                    run["rerank_calls"],
                    run["elapsed_ms"],
                ]
            )
    return json_path, csv_path


def build_provider(mode: str, settings: AppSettings | None = None) -> BaseLLMProvider:
    """按模式构造 Provider：真实模式使用既有工厂，未就绪时显式失败。"""

    if mode == "selftest":
        return SelfTestScoringProvider()
    return create_llm_provider(settings)


def build_embedding(mode: str, settings: AppSettings) -> BaseEmbeddingProvider:
    """替身只用于自检；真实模式复用 T035 工厂并保留未就绪错误码。"""

    if mode == "selftest":
        return StubEmbeddingProvider()
    return embedding_factory.create_embedding_provider(settings)


def build_retrieval_components(
    mode: str, strategy: str, *, settings: AppSettings, provider: BaseLLMProvider,
) -> tuple[BaseRetriever | None, BaseReranker | None]:
    """复用 M2 检索与重排；异步上下文组装负责调用 rerank_async。"""

    if mode == "selftest":
        return StubRetriever(), IdentityReranker()
    if strategy == "zero_shot":
        return None, None
    if strategy == "rag":
        return get_retriever(RetrievalMode.VECTOR_ONLY), None
    kwargs: dict[str, Any] = {"max_candidates": settings.rerank_max_candidates}
    if settings.rerank_provider in {"llm", "openai_compatible"}:
        kwargs.update(
            provider=provider,
            model=settings.rerank_model or settings.deepseek_model,
            timeout=settings.rerank_timeout_seconds,
        )
    else:
        kwargs["model"] = settings.rerank_model or DEFAULT_CROSS_ENCODER_MODEL
    return (
        get_retriever(
            RetrievalMode.HYBRID,
            candidate_k=settings.rerank_max_candidates,
            vector_weight=settings.hybrid_vector_weight,
        ),
        build_reranker(settings.rerank_provider, **kwargs),
    )


class BenchmarkCorpusNotReady(RuntimeError):
    """评测资料未按 M2 摄取完成，不能以空语料冒充真实检索。"""


def load_corpus_scope(
    session: Session, corpus_path: Path,
) -> tuple[str, RetrievalFilters, dict[str, Any]]:
    """复用 M2 manifest/Ready 校验，只读既有资料并限定检索范围。"""

    try:
        manifest = json.loads(corpus_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, Mapping) or not manifest.get("chunks"):
            raise ValueError("评测语料为空")
        document_id, chunk_ids = _load_ingested_corpus(session, {"corpus": manifest})
        document = session.get(Document, document_id)
        assert document is not None
        return (
            str(document.course_id),
            RetrievalFilters(document_ids=(document_id,)),
            {"version": manifest.get("dataset_version"),
             "document_id": str(document_id), "chunk_count": len(chunk_ids)},
        )
    except Exception as error:  # noqa: BLE001 - 只保留失败类型，不输出连接信息或正文
        raise BenchmarkCorpusNotReady(type(error).__name__) from None


async def _run_strategies(
    record: dict[str, Any], cases: Sequence[GradingCase], *, mode: str,
    settings: AppSettings, strategies: Sequence[str], corpus_path: Path,
    provider: BaseLLMProvider | None,
) -> None:
    """工厂创建、全部查询、重排和评分共享一次事件循环；数据库资源在结束时释放。"""

    embedding = build_embedding(mode, settings)
    record["embedding"] = embedding.describe()
    record["retrieval_config"] = {
        "candidate_k": settings.rerank_max_candidates,
        "vector_weight": settings.hybrid_vector_weight,
        "rerank_provider": settings.rerank_provider if mode == "real" else "selftest-identity",
    }
    with ExitStack() as resources:
        session = None
        course_id = BENCHMARK_COURSE_ID
        filters = None
        if mode == "real" and any(strategy != "zero_shot" for strategy in strategies):
            engine = create_database_engine(settings)
            resources.callback(engine.dispose)
            session = resources.enter_context(Session(engine))
            course_id, filters, record["corpus"] = load_corpus_scope(session, corpus_path)
        try:
            active_provider = provider if provider is not None else build_provider(mode, settings)
        except Exception as error:  # noqa: BLE001 - 未配置时明确失败
            raise ProviderNotReady(f"GRADING_PROVIDER_NOT_READY_{type(error).__name__}") from None
        for strategy in strategies:
            retriever, reranker = build_retrieval_components(
                mode, strategy, settings=settings, provider=active_provider,
            )
            run = await run_strategy(
                strategy, cases, provider=active_provider, retriever=retriever,
                reranker=reranker, embedding=embedding, settings=settings,
                session=session, course_id=course_id, filters=filters,
            )
            record["runs"].append({
                "strategy": strategy,
                "status": run.status,
                "error_code": run.error_code,
                "provider_calls": run.provider_calls,
                "retrieval_calls": run.retrieval_calls,
                "rerank_calls": run.rerank_calls,
                "elapsed_ms": run.elapsed_ms,
                "predictions": run.predictions,
                "failures": run.failures,
                "not_executed": run.not_executed,
                "metrics": run.metrics,
                "retriever": type(retriever).__name__ if retriever is not None else None,
                "reranker": reranker.describe() if reranker is not None else None,
            })
            if run.fatal_error is not None:
                raise run.fatal_error


def run_benchmark(
    *,
    mode: str = "selftest",
    dataset_path: Path = DATASET_PATH,
    corpus_path: Path = CORPUS_PATH,
    results_dir: Path = RESULTS_DIR,
    run_id: str | None = None,
    provider: BaseLLMProvider | None = None,
    settings: AppSettings | None = None,
    strategies: Sequence[str] = STRATEGIES,
    limit: int | None = None,
) -> tuple[dict[str, Any], str]:
    """执行三路对比并落盘；返回 (结果记录, run_id)。"""

    if mode not in {"selftest", "real"} or any(s not in STRATEGIES for s in strategies):
        raise ValueError("不支持的评测模式或策略")
    dataset = load_dataset(dataset_path)
    cases = build_cases(dataset)
    if limit is not None:
        cases = cases[:limit]
    resolved_settings = settings if settings is not None else get_settings()
    resolved_run_id = run_id or f"{mode}-{uuid4().hex[:8]}"
    record: dict[str, Any] = {
        "run_id": resolved_run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "mode": mode,
        "dataset": {
            "name": dataset.get("metadata", {}).get("name", dataset_path.stem),
            "version": dataset.get("metadata", {}).get("version", "unknown"),
            "label_source": dataset.get("metadata", {}).get("label_source", "unknown"),
            "score_scale": dataset.get("metadata", {}).get("score_scale", "unknown"),
            "sample_count": len(cases),
        },
        "model": _model_name(mode, resolved_settings),
        "prompt_version": SUBJECTIVE_GRADING_PROMPT_VERSION,
        "ground_truth": _ground_truth_note(cases),
        "runs": [],
        "note": "不记录密钥、完整 Prompt 与学生答案原文；策略间对比不代表评分质量优劣。",
    }
    try:
        asyncio.run(_run_strategies(
            record, cases, mode=mode, settings=resolved_settings, strategies=strategies,
            corpus_path=corpus_path, provider=provider,
        ))
    except Exception as error:  # noqa: BLE001 - 装配失败仍保存脱敏报告，不降级为替身
        record["status"] = "failed"
        if isinstance(error, BenchmarkCorpusNotReady):
            record["error_code"] = "GRADING_BENCHMARK_CORPUS_NOT_READY"
        elif isinstance(error, ProviderNotReady):
            record["error_code"] = str(error)
        else:
            record["error_code"] = getattr(error, "error_code", f"GRADING_BENCHMARK_SETUP_FAILED_{type(error).__name__}")
        write_run_records(record, results_dir=results_dir, run_id=resolved_run_id)
        return record, resolved_run_id
    runs = record["runs"]
    record["status"] = (
        "failed" if not any(run["predictions"] for run in runs)
        else "completed" if all(run["status"] == "completed" for run in runs)
        else "partial_failed"
    )
    write_run_records(record, results_dir=results_dir, run_id=resolved_run_id)
    return record, resolved_run_id


def _model_name(mode: str, settings: AppSettings) -> str:
    """返回本次运行的模型标识；自检模式如实标注为 stub。"""

    if mode == "selftest":
        return "selftest-stub"
    return str(getattr(settings, "deepseek_model", "unknown"))


def _ground_truth_note(cases: Iterable[GradingCase]) -> dict[str, Any]:
    """说明教师 Ground Truth 与合成参考分的可用情况。"""

    case_list = list(cases)
    teacher_labeled = [case for case in case_list if case.teacher_score is not None]
    return {
        "teacher_labeled_count": len(teacher_labeled),
        "synthetic_reference_count": sum(
            1 for case in case_list if case.reference_score is not None
        ),
        "teacher_labels_available": bool(teacher_labeled),
        "reason": None if teacher_labeled else NO_GROUND_TRUTH_REASON,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(description="阅卷策略 Benchmark（T059）")
    parser.add_argument("--mode", choices=("selftest", "real"), default="selftest")
    parser.add_argument("--dataset", type=Path, default=DATASET_PATH)
    parser.add_argument("--corpus", type=Path, default=CORPUS_PATH, help="已摄取的 M2 语料清单")
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--strategies",
        default=",".join(STRATEGIES),
        help="逗号分隔的策略列表",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口；失败时返回非零退出码并保留失败记录。"""

    args = parse_args(argv)
    strategies = tuple(
        item.strip() for item in str(args.strategies).split(",") if item.strip()
    )
    record, run_id = run_benchmark(
        mode=args.mode,
        dataset_path=args.dataset,
        corpus_path=args.corpus,
        results_dir=args.results_dir,
        run_id=args.run_id,
        strategies=strategies,
        limit=args.limit,
    )
    print(f"run_id={run_id}")
    for run in record["runs"]:
        print(
            f"{run['strategy']}: status={run['status']} calls={run['provider_calls']} "
            f"failures={run['metrics']['failure_count']} "
            f"mae={run['metrics']['mae']} agreement={run['metrics']['agreement_rate']}"
        )
    if record.get("ground_truth", {}).get("teacher_labels_available") is False:
        print(f"提示：{NO_GROUND_TRUTH_REASON}")
    return 0 if record["status"] == "completed" else 1


if __name__ == "__main__":  # pragma: no cover - 命令行入口
    sys.exit(main())
