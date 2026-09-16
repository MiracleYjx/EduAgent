"""Rerank 适配器契约与两条路线。

按 ``.specify/plan.md`` §3 保留两条可替换路线，由 Benchmark 结果决定采用哪一条：

- ``LLMRerankAdapter``：复用既有 ``BaseLLMProvider`` 抽象，优先降低本地部署成本。
- ``CrossEncoderRerankAdapter``：本地轻量 Cross Encoder（``sentence-transformers``），
  作为可选依赖安装；依赖缺失时在**初始化**阶段抛出 ``RERANK_PROVIDER_NOT_READY``。

统一约束：

- ``rerank(query, candidates, top_k)`` 只写 ``rerank_score`` 并重排 ``rank``，不修改
  ``content``、``metadata`` 与任何来源标识。
- 单次重排候选数量有上限（默认 20），LLM 路线超时可配置（默认 30 秒），用于控制成本。
- LLM 返回未知 ``chunk_id`` 视为失败；候选未被返回时记 0 分并排在末尾，而不是丢弃来源。
- 未就绪或未配置时不返回“未重排结果”冒充满分，而是抛出可识别的错误。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import math
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any, ClassVar, Final, Protocol, cast

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.app.ai.llm.base import BaseLLMProvider, LLMMessage
from backend.app.ai.llm.factory import LLMProviderError, create_llm_provider
from backend.app.ai.retrieval.base import (
    DEFAULT_TOP_K,
    BaseRetriever,
    RetrievalFilters,
    RetrievalInputError,
    RetrievalMode,
    RetrievalQuery,
    RetrievedChunk,
    get_retriever,
    normalize_query_text,
    normalize_top_k,
    resolve_filters,
)
from backend.app.ai.retrieval.hybrid_search import DEFAULT_CANDIDATE_K
from backend.app.core.config import get_settings

#: Rerank 相关失败码。
RERANK_INVALID_INPUT: Final[str] = "RERANK_INVALID_INPUT"
RERANK_PROVIDER_NOT_READY: Final[str] = "RERANK_PROVIDER_NOT_READY"
RERANK_FAILED: Final[str] = "RERANK_FAILED"

RERANK_ERROR_MESSAGES: Final[dict[str, str]] = {
    RERANK_INVALID_INPUT: "重排输入不合法，请检查查询文本或候选片段。",
    RERANK_PROVIDER_NOT_READY: "Rerank Provider 当前不可用，请检查依赖或配置。",
    RERANK_FAILED: "重排执行失败，请稍后重试或切换 Rerank Provider。",
}

#: 候选数量上限：控制单次 LLM 重排的 token 消耗。
DEFAULT_MAX_CANDIDATES: Final[int] = 5
#: 与 AppSettings.rerank_max_candidates 默认值保持一致（方案 A：20 -> 5）。
#: LLM 重排默认超时（秒）。
DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0
#: Cross Encoder 默认模型（本地部署时按 Benchmark 结果替换）。
DEFAULT_CROSS_ENCODER_MODEL: Final[str] = "BAAI/bge-reranker-base"

type CrossEncoderLoader = Callable[[str], Any]


class _HybridLike(Protocol):
    """Hybrid 检索的最小协议：接受 RetrievalQuery。"""

    def search(
        self,
        session: Session,
        query: RetrievalQuery,
        *,
        top_k: int,
        filters: RetrievalFilters | None,
    ) -> list[RetrievedChunk]: ...


class RerankError(RuntimeError):
    """重排失败基类。"""

    error_code: ClassVar[str] = RERANK_FAILED
    retryable: ClassVar[bool] = True

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.error_code}：{detail}")
        self.detail = detail

    @property
    def user_message(self) -> str:
        """返回可直接展示的失败提示。"""

        return RERANK_ERROR_MESSAGES.get(
            self.error_code, RERANK_ERROR_MESSAGES[RERANK_FAILED]
        )


class RerankInputError(RerankError):
    """查询文本或候选片段不合法。"""

    error_code: ClassVar[str] = RERANK_INVALID_INPUT
    retryable: ClassVar[bool] = False


class RerankProviderNotReadyError(RerankError):
    """Rerank Provider 未配置或依赖缺失；不得静默降级。"""

    error_code: ClassVar[str] = RERANK_PROVIDER_NOT_READY
    retryable: ClassVar[bool] = False


class RerankFailedError(RerankError):
    """Provider 调用失败、超时或返回非法结果。"""

    error_code: ClassVar[str] = RERANK_FAILED


class LLMRerankItem(BaseModel):
    """LLM 返回的单条重排结果。"""

    chunk_id: str = Field(min_length=1)
    score: float = Field(ge=0.0, le=1.0)


class LLMRerankResponse(BaseModel):
    """LLM 重排的结构化输出契约。"""

    rankings: list[LLMRerankItem]


_RERANK_SYSTEM_PROMPT = (
    "你是课程资料检索的重排模型。给定一个查询和候选知识片段，"
    "请为每个片段给出 0 到 1 的相关性分数（1 表示最相关）。"
    "只使用候选列表中出现过的 chunk_id，不得新增或改写片段内容。"
)


def sentence_transformers_available() -> bool:
    """判断本地 Cross Encoder 依赖是否已安装。"""

    return importlib.util.find_spec("sentence_transformers") is not None


def _sigmoid(value: float) -> float:
    """把 Cross Encoder 的原始 logit 映射到 [0, 1]。"""

    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exponent = math.exp(value)
    return exponent / (1.0 + exponent)


def _prepare_candidates(
    candidates: Sequence[RetrievedChunk],
    *,
    max_candidates: int,
) -> list[RetrievedChunk]:
    """校验候选并去重，同时按上限截断以控制重排成本。"""

    if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
        raise RerankInputError("重排候选必须是检索结果序列。")
    if not candidates:
        return []
    unique: dict[str, RetrievedChunk] = {}
    for candidate in candidates:
        if not isinstance(candidate, RetrievedChunk):
            raise RerankInputError("重排候选必须是 RetrievedChunk。")
        if not candidate.content.strip():
            raise RerankInputError("重排候选内容不能为空。")
        unique.setdefault(candidate.chunk_id, candidate)
    return list(unique.values())[:max_candidates]


def _apply_scores(
    candidates: Sequence[RetrievedChunk],
    scores: dict[str, float],
    top_k: int,
) -> list[RetrievedChunk]:
    """写入重排分数并重排 rank，保留全部来源字段。"""

    rescored = [
        replace(candidate, rerank_score=float(scores.get(candidate.chunk_id, 0.0)))
        for candidate in candidates
    ]
    rescored.sort(key=lambda item: (-float(item.rerank_score or 0.0), item.chunk_id))
    return [
        replace(candidate, rank=rank)
        for rank, candidate in enumerate(rescored[:top_k])
    ]


class BaseReranker(ABC):
    """所有 Rerank 实现共同遵守的最小接口。"""

    provider_name: ClassVar[str] = "unknown"
    #: 模型标识；允许具体实现按配置覆盖。
    model_name: str = "unknown"

    @abstractmethod
    def rerank(
        self,
        query: str,
        candidates: Sequence[RetrievedChunk],
        top_k: int = DEFAULT_TOP_K,
    ) -> list[RetrievedChunk]:
        """对候选重新打分并排序；无候选时返回空列表。"""

        raise NotImplementedError

    async def rerank_async(
        self,
        query: str,
        candidates: Sequence[RetrievedChunk],
        top_k: int = DEFAULT_TOP_K,
    ) -> list[RetrievedChunk]:
        """异步入口；既有同步实现在线程中执行，不阻塞调用方事件循环。"""

        return await asyncio.to_thread(self.rerank, query, candidates, top_k)

    def describe(self) -> dict[str, str | int | float | None]:
        """返回 Provider 元数据，供检索结果与 Benchmark 记录来源。"""

        return {
            "provider": self.provider_name,
            "model": self.model_name,
            "max_candidates": getattr(self, "max_candidates", None),
        }


class LLMRerankAdapter(BaseReranker):
    """基于 LLM 结构化输出的重排适配器。"""

    provider_name = "llm"

    def __init__(
        self,
        *,
        provider: BaseLLMProvider | None = None,
        model: str | None = None,
        max_candidates: int | None = None,
        timeout: float | None = None,
    ) -> None:
        self.model_name = model or "provider-default"
        self.max_candidates = _resolve_max_candidates(max_candidates)
        self.timeout = _resolve_timeout(timeout)
        self._model = model
        if provider is not None:
            self._provider = provider
        else:
            try:
                self._provider = create_llm_provider()
            except Exception as exc:  # 未就绪必须明确失败
                raise RerankProviderNotReadyError(
                    f"LLM Rerank 依赖的 LLM Provider 未就绪：{type(exc).__name__}。"
                ) from exc

    def rerank(
        self,
        query: str,
        candidates: Sequence[RetrievedChunk],
        top_k: int = DEFAULT_TOP_K,
    ) -> list[RetrievedChunk]:
        """同步兼容入口；事件循环中的调用方必须使用异步入口。"""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.rerank_async(query, candidates, top_k))
        raise RerankInputError("当前线程已有事件循环，请使用 await rerank_async(...)。")

    async def rerank_async(
        self,
        query: str,
        candidates: Sequence[RetrievedChunk],
        top_k: int = DEFAULT_TOP_K,
    ) -> list[RetrievedChunk]:
        """在调用方事件循环中等待 LLM，保留来源、超时与取消语义。"""

        text = normalize_query_text(query)
        limit = normalize_top_k(top_k)
        prepared = _prepare_candidates(candidates, max_candidates=self.max_candidates)
        if not prepared:
            return []

        response = await self._invoke(text, prepared)
        known_ids = {candidate.chunk_id for candidate in prepared}
        unknown_ids = [
            item.chunk_id for item in response.rankings if item.chunk_id not in known_ids
        ]
        if unknown_ids:
            raise RerankFailedError(
                f"LLM 返回了候选之外的 chunk_id：{unknown_ids[0]}。"
            )
        scores = {item.chunk_id: float(item.score) for item in response.rankings}
        return _apply_scores(prepared, scores, limit)

    async def _invoke(self, text: str, candidates: Sequence[RetrievedChunk]) -> LLMRerankResponse:
        """执行一次结构化重排调用，超时与 Provider 错误统一收敛。"""

        messages: list[LLMMessage] = [
            {
                "role": "system",
                "content": _RERANK_SYSTEM_PROMPT
                + "\n请严格按照以下 JSON Schema 返回 JSON 对象：\n"
                + json.dumps(LLMRerankResponse.model_json_schema(), ensure_ascii=False),
            },
            {"role": "user", "content": self._build_prompt(text, candidates)},
        ]
        try:
            result = await asyncio.wait_for(
                self._provider.generate_structured(
                    messages,
                    LLMRerankResponse,
                    model=self._model,
                ),
                timeout=self.timeout,
            )
        except TimeoutError as exc:
            raise RerankFailedError(
                f"LLM Rerank 超时（{self.timeout:g} 秒）。"
            ) from exc
        except LLMProviderError as exc:
            raise RerankFailedError(f"LLM Rerank 调用失败：{exc}") from exc
        if not isinstance(result, LLMRerankResponse):
            raise RerankFailedError("LLM 未返回约定的结构化重排结果。")
        return result

    @staticmethod
    def _build_prompt(text: str, candidates: Sequence[RetrievedChunk]) -> str:
        """构造包含查询与全部候选片段的中文提示。"""

        lines = [f"查询：{text}", "", "候选知识片段："]
        for candidate in candidates:
            lines.append(f"- chunk_id={candidate.chunk_id}")
            lines.append(f"  内容：{candidate.content[:400]}")
        lines.append("")
        lines.append("请返回每个 chunk_id 的相关性分数。")
        return "\n".join(lines)


class CrossEncoderRerankAdapter(BaseReranker):
    """基于本地 Cross Encoder 的重排适配器。"""

    provider_name = "cross_encoder"

    def __init__(
        self,
        *,
        model: str = DEFAULT_CROSS_ENCODER_MODEL,
        max_candidates: int | None = None,
        model_loader: CrossEncoderLoader | None = None,
    ) -> None:
        self.model_name = model
        self.max_candidates = _resolve_max_candidates(max_candidates)
        if model_loader is None and not sentence_transformers_available():
            raise RerankProviderNotReadyError(
                "Cross Encoder Rerank 依赖未安装，请安装可选依赖："
                "pip install \"eduagent[rerank]\"（sentence-transformers）。"
            )
        self._model_loader = model_loader or _load_cross_encoder
        self._model: Any | None = None

    def rerank(
        self,
        query: str,
        candidates: Sequence[RetrievedChunk],
        top_k: int = DEFAULT_TOP_K,
    ) -> list[RetrievedChunk]:
        """使用 Cross Encoder 为 (查询, 片段) 打分并重排。"""

        text = normalize_query_text(query)
        limit = normalize_top_k(top_k)
        prepared = _prepare_candidates(candidates, max_candidates=self.max_candidates)
        if not prepared:
            return []

        model = self._ensure_model()
        pairs = [(text, candidate.content) for candidate in prepared]
        try:
            logits = model.predict(pairs)
        except Exception as exc:  # 统一收敛为可识别的重排失败
            raise RerankFailedError(
                f"Cross Encoder 计算失败：{type(exc).__name__}。"
            ) from exc
        values = [float(value) for value in logits]
        if len(values) != len(prepared):
            raise RerankFailedError("Cross Encoder 返回的分数数量与候选数量不一致。")
        scores = {
            candidate.chunk_id: _sigmoid(value)
            for candidate, value in zip(prepared, values, strict=True)
        }
        return _apply_scores(prepared, scores, limit)

    def _ensure_model(self) -> Any:
        """首次调用时加载模型，避免导入配置阶段占用内存。"""

        if self._model is None:
            try:
                self._model = self._model_loader(self.model_name)
            except RerankError:
                raise
            except Exception as exc:
                raise RerankProviderNotReadyError(
                    f"Cross Encoder 模型加载失败：{type(exc).__name__}。"
                ) from exc
        return self._model


def _load_cross_encoder(model_name: str) -> Any:
    """加载 sentence-transformers CrossEncoder；依赖缺失时明确失败。"""

    if not sentence_transformers_available():
        raise RerankProviderNotReadyError(
            "Cross Encoder Rerank 依赖未安装，请安装可选依赖："
            "pip install \"eduagent[rerank]\"。"
        )
    from sentence_transformers import CrossEncoder  # type: ignore[import-not-found]

    return CrossEncoder(model_name)


def _resolve_max_candidates(value: int | None) -> int:
    """解析候选上限：显式参数优先，其次读取配置。"""

    raw: object = value
    if raw is None:
        raw = get_settings().rerank_max_candidates
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise RerankInputError("rerank_max_candidates 必须是整数。")
    if raw <= 0:
        raise RerankInputError("rerank_max_candidates 必须大于 0。")
    return raw


def _resolve_timeout(value: float | None) -> float:
    """解析 LLM 重排超时：显式参数优先，其次读取配置。"""

    raw: object = value
    if raw is None:
        raw = get_settings().rerank_timeout_seconds
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise RerankInputError("rerank_timeout_seconds 必须是数值。")
    if float(raw) <= 0:
        raise RerankInputError("rerank_timeout_seconds 必须大于 0。")
    return float(raw)


def build_reranker(name: str | None = None, **kwargs: Any) -> BaseReranker:
    """按 ``RERANK_PROVIDER`` 创建 Rerank 适配器；未知或未配置时明确失败。

    ``name`` 是 ``RERANK_PROVIDER`` 取值（未提供时读取配置）；其余参数透传给具体适配器。
    """

    resolved = name if name is not None else get_settings().rerank_provider
    normalized = (resolved or "").strip().lower()
    if normalized in {"llm", "openai_compatible", "cross_encoder"} and "model" not in kwargs:
        settings = get_settings()
        # 显式切换路线时不套用另一条路线的模型；显式 model 参数始终优先。
        configured_model = settings.rerank_model if normalized == settings.rerank_provider else None
        if configured_model:
            kwargs["model"] = configured_model
        elif normalized in {"llm", "openai_compatible"}:
            kwargs["model"] = settings.deepseek_model
    if normalized in {"llm", "openai_compatible"}:
        return LLMRerankAdapter(**kwargs)
    if normalized == "cross_encoder":
        return CrossEncoderRerankAdapter(**kwargs)
    if normalized == "none":
        raise RerankProviderNotReadyError(
            "RERANK_PROVIDER=none 表示未启用重排；Hybrid + Rerank 模式必须配置可用 reranker。"
        )
    raise RerankInputError(f"不支持的 RERANK_PROVIDER 取值：{normalized or '（空）'}。")


class HybridRerankRetriever(BaseRetriever):
    """Hybrid 融合后执行 Rerank 的检索实现（``hybrid_rerank`` 模式）。"""

    mode = RetrievalMode.HYBRID_RERANK

    def __init__(
        self,
        *,
        reranker: BaseReranker | None = None,
        hybrid_retriever: BaseRetriever | None = None,
        fusion_top_k: int = DEFAULT_CANDIDATE_K,
        candidate_k: int = DEFAULT_CANDIDATE_K,
        vector_weight: float | None = None,
    ) -> None:
        self.fusion_top_k = normalize_top_k(fusion_top_k)
        # 基础契约的查询参数型为 str | Sequence[float]，Hybrid 实现额外接受 RetrievalQuery；
        # 这里用 cast 保持 vector/keyword 实现不被修改。
        candidate = hybrid_retriever or get_retriever(
            RetrievalMode.HYBRID,
            vector_weight=vector_weight,
            candidate_k=candidate_k,
        )
        self._hybrid = cast("_HybridLike", candidate)
        self._reranker = reranker

    @property
    def reranker(self) -> BaseReranker:
        """返回生效的 Rerank 适配器；未就绪时抛出明确错误。"""

        if self._reranker is None:
            self._reranker = build_reranker()
        return self._reranker

    def search(
        self,
        session: Session,
        query: str | Sequence[float] | RetrievalQuery,
        *,
        top_k: int = DEFAULT_TOP_K,
        filters: RetrievalFilters | None = None,
    ) -> list[RetrievedChunk]:
        """先做 Hybrid 融合，再对融合后的 Top-K 候选重排。"""

        if not isinstance(query, RetrievalQuery):
            raise RetrievalInputError(
                "Hybrid + Rerank 需要同时提供查询文本与 query embedding，请使用 RetrievalQuery。"
            )
        limit = normalize_top_k(top_k)
        scope = resolve_filters(filters)
        fused = self._hybrid.search(
            session,
            query,
            top_k=self.fusion_top_k,
            filters=scope,
        )
        if not fused:
            # 空上下文直接返回空列表，不调用 Rerank，也不编造候选。
            return []
        return self.reranker.rerank(query.text, fused, limit)


__all__ = [
    "DEFAULT_CROSS_ENCODER_MODEL",
    "DEFAULT_MAX_CANDIDATES",
    "DEFAULT_TIMEOUT_SECONDS",
    "RERANK_ERROR_MESSAGES",
    "RERANK_FAILED",
    "RERANK_INVALID_INPUT",
    "RERANK_PROVIDER_NOT_READY",
    "BaseReranker",
    "CrossEncoderRerankAdapter",
    "HybridRerankRetriever",
    "LLMRerankAdapter",
    "LLMRerankItem",
    "LLMRerankResponse",
    "RerankError",
    "RerankFailedError",
    "RerankInputError",
    "RerankProviderNotReadyError",
    "build_reranker",
    "sentence_transformers_available",
]
