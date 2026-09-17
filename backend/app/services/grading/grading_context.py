"""主观题评分输入组装：Query Construction、Hybrid 检索、Rerank 与 Final Context。

契约依据：FR-031、FR-032、plan.md §5.3（主观题评分输入契约、输入组装顺序）与
``.specify/contracts/agent-workflow.md`` 的 ``Retrieve Context`` 节点。

设计要点：

- **输入集合**：题目、标准答案、评分标准、学生答案与课程范围全部来自 `Question`/`Answer`
  实体，本模块只做校验与组装，不推断缺失信息，也不让模型补齐。
- **检索抽象**：只通过 M2 的 :func:`backend.app.ai.retrieval.base.get_retriever` 与
  :func:`backend.app.ai.retrieval.reranker.build_reranker` 访问检索能力，**不写原始 SQL**，
  也不复制融合或重排算法。
- **异步与同步的衔接**：正式评分路径（``HYBRID_RERANK``）在调用方事件循环内先执行 Hybrid
  候选检索，再 ``await reranker.rerank_async(...)``。不得调用 ``HybridRerankRetriever.search``
  或 ``LLMRerankAdapter.rerank``：两者在已有事件循环时会显式报错。
- **溯源一致性**：``retrieved_context_ids`` 只包含实际写入 Final Context 的片段且保持顺序；
  被预算截断而未写入的片段不会被声明为已使用。
- **充分性**：零候选或零有效正文一律判为上下文不足，``require_context=True`` 时显式失败，
  不允许把空上下文当作充分依据。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, ClassVar, Final, Protocol, cast
from uuid import UUID

from sqlalchemy.orm import Session

from backend.app.ai.embedding.base import BaseEmbeddingProvider
from backend.app.ai.embedding.factory import (
    create_embedding_provider,
    get_embedding_provider,
)
from backend.app.ai.retrieval.base import (
    DEFAULT_TOP_K,
    BaseRetriever,
    RetrievalFilters,
    RetrievalMode,
    RetrievalQuery,
    RetrievedChunk,
    get_retriever,
    normalize_mode,
    normalize_top_k,
    resolve_filters,
)
from backend.app.ai.retrieval.reranker import BaseReranker, build_reranker
from backend.app.core.config import AppSettings, get_settings
from backend.app.domain.enums import GradingMode, QuestionType
from backend.app.services.grading.question_router import (
    QuestionRouter,
    normalize_question_type,
)

#: 必填输入非法或缺失（题型与标准答案有各自专用错误码）。
GRADING_INVALID_INPUT: Final[str] = "GRADING_INVALID_INPUT"
#: 标准答案缺失，无法组装评分依据。
GRADING_MISSING_REFERENCE_ANSWER: Final[str] = "GRADING_MISSING_REFERENCE_ANSWER"
#: 客观题误入主观题上下文组装。
GRADING_MODE_MISMATCH: Final[str] = "GRADING_MODE_MISMATCH"
#: 检索上下文不足；不得把空上下文当作充分依据。
GRADING_MISSING_CONTEXT: Final[str] = "GRADING_MISSING_CONTEXT"

#: Query 总长度上限；字段预算、段落标签与截断标记都计入该上限。
MAX_QUERY_CHARS: Final[int] = 1500
#: 各字段的 Query 预算基线；实际额度还会受总额限制。
QUERY_FIELD_BUDGETS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "【题目】": 400,
        "【参考要点】": 400,
        "【评分标准】": 300,
    }
)
#: 学生答案的预留预算；必须非零，避免长题目挤掉学生作答。
STUDENT_QUERY_BUDGET: Final[int] = 400
#: 截断标记；计入字段预算。
TRUNCATION_MARKER: Final[str] = "…（已截断）"
#: 单条片段写入 Final Context 的正文字符上限。
PER_CHUNK_CHARS: Final[int] = 800
#: Final Context 的总字符上限。
MAX_FINAL_CONTEXT_CHARS: Final[int] = 6000


class _MissingArgument:
    """哨兵类型：区分「未传参」与显式传入的空值。"""

    def __repr__(self) -> str:  # pragma: no cover - 仅用于诊断输出
        return "<未传参>"


#: 未传参哨兵；用于把漏传参数转为业务异常，而不是 Python ``TypeError``。
_MISSING: Final[_MissingArgument] = _MissingArgument()

SECTION_QUESTION: Final[str] = "【题目】"
SECTION_REFERENCE: Final[str] = "【参考要点】"
SECTION_RUBRIC: Final[str] = "【评分标准】"
SECTION_STUDENT: Final[str] = "【学生答案】"


class GradingContextError(RuntimeError):
    """主观题上下文组装失败基类；默认按不可重试的输入或数据问题处理。"""

    error_code: ClassVar[str] = GRADING_INVALID_INPUT
    retryable: ClassVar[bool] = False

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.error_code}：{detail}")
        self.detail = detail


class GradingInputError(GradingContextError):
    """必填输入缺失或类型非法；必须先修正输入。"""

    error_code: ClassVar[str] = GRADING_INVALID_INPUT


class ReferenceAnswerMissingError(GradingContextError):
    """标准答案缺失或为空白；无法组装评分依据。"""

    error_code: ClassVar[str] = GRADING_MISSING_REFERENCE_ANSWER


class GradingModeMismatchError(GradingContextError):
    """客观题不得进入主观题评分上下文。"""

    error_code: ClassVar[str] = GRADING_MODE_MISMATCH


class InsufficientContextError(GradingContextError):
    """检索上下文不足；不得用空上下文继续评分。"""

    error_code: ClassVar[str] = GRADING_MISSING_CONTEXT


def _as_text(value: Any, *, label: str) -> str:
    """把 `UUID` 或文本归一为文本；其它类型显式失败。"""

    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise GradingInputError(f"{label}不能为空。")
        return text
    raise GradingInputError(
        f"{label}必须是文本或 UUID，收到类型 {type(value).__name__}。"
    )


def _as_uuid_text(value: Any, *, label: str) -> str:
    """把 `UUID` 或 UUID 文本归一为文本；其它值显式失败。

    课程标识与知识库标识要求 UUID，因为检索过滤条件最终需要 UUID。
    """

    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise GradingInputError(f"{label}不能为空。")
        try:
            return str(UUID(text))
        except ValueError:
            raise GradingInputError(f"{label}必须是合法 UUID。") from None
    raise GradingInputError(
        f"{label}必须是 UUID 或 UUID 文本，收到类型 {type(value).__name__}。"
    )


def _as_uuid_text_tuple(value: Any, *, label: str) -> tuple[str, ...]:
    """校验 UUID 标识序列，拒绝隐式类型转换。"""

    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise GradingInputError(f"{label}必须是标识序列。")
    return tuple(_as_uuid_text(item, label=label) for item in value)


def _as_text_tuple(value: Any, *, label: str) -> tuple[str, ...]:
    """校验字符串序列，拒绝隐式类型转换。"""

    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise GradingInputError(f"{label}必须是字符串序列。")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise GradingInputError(
                f"{label}只接受字符串，收到类型 {type(item).__name__}。"
            )
        text = item.strip()
        if not text:
            raise GradingInputError(f"{label}不能包含空白项。")
        items.append(text)
    return tuple(items)


@dataclass(frozen=True, slots=True)
class SubjectiveGradingSource:
    """§5.3 规定的主观题评分输入集合，字段来源均为题库与答卷实体。

    字段来源与校验顺序（错误码优先级固定）：

    1. ``question_type``：来自 ``Question.type``，交由 :class:`QuestionRouter` 判定；
       缺失或未知沿用 Router 错误码，客观题报 ``GRADING_MODE_MISMATCH``；
    2. ``reference_answer``：来自 ``Question.reference_answer``；缺失/空白报
       ``GRADING_MISSING_REFERENCE_ANSWER``；
    3. 其余必填项（``scoring_rubric``、``student_answer``、``course_id``、``question_content``
       等）缺失、类型非法或全空白报 ``GRADING_INVALID_INPUT``；漏传构造参数同样转为本错误码，
       不向上层泄漏 Python ``TypeError``。

    **评分标准与学生答案均为必填且非空白**：缺少评分标准时「按规则给分」没有依据，
    空白作答也不得送入模型猜测（FR-031）。

    ``student_answer`` 接受文本或字符串列表（列表用换行连接）。``Answer.content`` 虽然允许
    字典，但主观题没有键语义与顺序约定，因此本模块**显式拒绝字典答案**，不提供字典转文本的
    隐式规则，避免把无意义的拼接结果当作学生作答。
    """

    question_type: QuestionType | str | None
    course_id: UUID | str
    question_content: str
    reference_answer: str
    student_answer: str | Sequence[str] | None | _MissingArgument = _MISSING
    scoring_rubric: str | None = None
    knowledge_points: tuple[str, ...] = ()
    knowledge_base_ids: tuple[str, ...] = ()
    question_id: UUID | str | None = None
    answer_id: UUID | str | None = None
    submission_id: UUID | str | None = None
    #: 平台侧的评分模式校验器；默认使用无状态 :class:`QuestionRouter`。
    _router: QuestionRouter = field(default_factory=QuestionRouter, repr=False, compare=False)

    def __post_init__(self) -> None:
        router = self._router if self._router is not None else QuestionRouter()
        normalized_type = normalize_question_type(self.question_type)
        if router.route_type(normalized_type) is not GradingMode.SUBJECTIVE:
            raise GradingModeMismatchError(
                f"题型 {normalized_type} 属于客观题路径，不能组装主观题评分上下文。"
            )
        object.__setattr__(self, "question_type", normalized_type)

        if not isinstance(self.reference_answer, str) or not self.reference_answer.strip():
            raise ReferenceAnswerMissingError("题目缺少标准答案，无法组装评分依据。")

        if not isinstance(self.scoring_rubric, str) or not self.scoring_rubric.strip():
            raise GradingInputError(
                "题目缺少非空的评分标准，无法按规则评分。"
            )
        object.__setattr__(self, "scoring_rubric", self.scoring_rubric.strip())

        object.__setattr__(
            self, "course_id", _as_uuid_text(self.course_id, label="课程标识")
        )
        object.__setattr__(
            self,
            "question_content",
            _as_text(self.question_content, label="题目内容"),
        )
        object.__setattr__(
            self,
            "knowledge_points",
            _as_text_tuple(self.knowledge_points, label="知识点"),
        )
        object.__setattr__(
            self,
            "knowledge_base_ids",
            _as_uuid_text_tuple(self.knowledge_base_ids, label="知识库标识"),
        )
        for name in ("question_id", "answer_id", "submission_id"):
            raw = getattr(self, name)
            if raw is not None:
                object.__setattr__(self, name, _as_text(raw, label=name))
        object.__setattr__(self, "student_answer", self._normalize_answer())

    def _normalize_answer(self) -> str:
        """校验学生答案形态；必须为文本或字符串列表，且不得为空白。

        列表答案用换行连接；**不做 ``str()`` 化**，字典形态在本模块被明确拒绝。
        """

        raw = self.student_answer
        if isinstance(raw, _MissingArgument) or raw is None:
            raise GradingInputError("缺少学生答案，无法组装评分输入。")
        if isinstance(raw, str):
            if not raw.strip():
                raise GradingInputError("学生答案为空白，无法组装评分输入。")
            return raw
        if isinstance(raw, (bytes, bytearray)) or not isinstance(raw, Sequence):
            raise GradingInputError(
                f"学生答案必须是文本或字符串列表，收到类型 {type(raw).__name__}。"
            )
        items: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                raise GradingInputError(
                    f"学生答案列表只接受字符串，收到类型 {type(item).__name__}。"
                )
            items.append(item)
        joined = "\n".join(items)
        if not joined.strip():
            raise GradingInputError("学生答案为空白，无法组装评分输入。")
        return joined

    @property
    def student_answer_text(self) -> str:
        """返回去除首尾空白的作答文本；空串表示学生未作答。"""

        return str(self.student_answer).strip()


def _truncate(text: str, budget: int) -> str:
    """按预算截断文本；截断标记计入预算。"""

    if budget <= 0:
        return ""
    if len(text) <= budget:
        return text
    if budget <= len(TRUNCATION_MARKER):
        return text[:budget]
    return text[: budget - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


def build_query_text(source: SubjectiveGradingSource) -> str:
    """构造检索查询文本：题目 → 参考要点 → 评分标准 → 学生答案（固定顺序）。

    采用固定拼接顺序，不声称某种顺序已优化检索效果（顺序对比留给后续评测）。
    每个字段使用独立预算，学生答案预算非零；段落标签与截断标记都计入
    :data:`MAX_QUERY_CHARS`。截断只作用于查询文本，不修改 ``source`` 中的原始输入。
    """

    rubric = (source.scoring_rubric or "").strip()
    sections: list[tuple[str, str]] = [
        (SECTION_QUESTION, source.question_content),
        (SECTION_REFERENCE, source.reference_answer),
        (SECTION_RUBRIC, rubric),
        (SECTION_STUDENT, source.student_answer_text),
    ]

    overhead = sum(len(label) + 1 for label, _ in sections) + (len(sections) - 1)
    budgets: dict[str, int] = {label: 0 for label, _ in sections}
    budgets[SECTION_STUDENT] = STUDENT_QUERY_BUDGET
    remaining = MAX_QUERY_CHARS - overhead - STUDENT_QUERY_BUDGET
    for label, _ in sections:
        if label == SECTION_STUDENT:
            continue
        granted = max(0, min(QUERY_FIELD_BUDGETS.get(label, 0), remaining))
        budgets[label] = granted
        remaining -= granted

    return "\n".join(
        f"{label}\n{_truncate(text, budgets[label])}" for label, text in sections
    )


@dataclass(frozen=True, slots=True)
class GradingContext:
    """主观题评分使用的检索上下文与溯源信息。"""

    source: SubjectiveGradingSource
    query_text: str
    retrieval_mode: RetrievalMode
    filters: RetrievalFilters
    chunks: tuple[RetrievedChunk, ...]
    retrieved_context_ids: tuple[str, ...]
    final_context: str
    candidate_count: int

    @property
    def is_sufficient(self) -> bool:
        """返回上下文是否足以支撑评分（至少一条片段写入且正文非空）。"""

        return bool(self.chunks)

    def ensure_sufficient(self) -> None:
        """上下文不足时显式失败；供评分入口在调用模型前调用。"""

        if not self.is_sufficient:
            raise InsufficientContextError(
                f"检索候选 {self.candidate_count} 条，写入 Final Context "
                f"{len(self.chunks)} 条，上下文不足。"
            )


class _HybridLikeRetriever(Protocol):
    """Hybrid 检索的最小协议：接受 :class:`RetrievalQuery` 的同步入口。

    基础契约只声明 ``str | Sequence[float]``；Hybrid 实现额外接受 ``RetrievalQuery``。
    本模块用协议收窄类型，而不修改 M2 的检索实现。
    """

    def search(
        self,
        session: Session,
        query: RetrievalQuery,
        *,
        top_k: int = DEFAULT_TOP_K,
        filters: RetrievalFilters | None = None,
    ) -> list[RetrievedChunk]: ...


def _merge_filters(
    source: SubjectiveGradingSource,
    filters: RetrievalFilters | None,
) -> RetrievalFilters:
    """合并调用方过滤条件：课程范围始终强制为题目所属课程。

    - ``course_ids`` 永远只包含题目课程；调用方限定其它课程时显式失败，
      防止主观题评分检索到其它课程内容；
    - ``knowledge_base_ids`` 与题目知识库范围取交集，交集为空时显式失败；
    - ``document_ids`` 由调用方提供并透传，但仍受强制课程过滤约束。
      （不查库无法验证资料归属，因此依赖课程过滤与上游数据一致性。）
    """

    course_id = UUID(str(source.course_id))
    source_knowledge_bases = tuple(
        UUID(value) for value in source.knowledge_base_ids
    )
    if filters is None:
        return RetrievalFilters(
            course_ids=(course_id,),
            knowledge_base_ids=source_knowledge_bases,
        )
    resolved = resolve_filters(filters)
    if resolved.course_ids and course_id not in resolved.course_ids:
        raise GradingInputError(
            "调用方过滤条件与题目所属课程不一致，拒绝跨课程检索。"
        )
    knowledge_base_ids = resolved.knowledge_base_ids or source_knowledge_bases
    if resolved.knowledge_base_ids and source_knowledge_bases:
        allowed = set(source_knowledge_bases)
        merged = tuple(
            value for value in resolved.knowledge_base_ids if value in allowed
        )
        if not merged:
            raise GradingInputError(
                "调用方知识库过滤条件与题目知识库范围无交集，拒绝跨知识库检索。"
            )
        knowledge_base_ids = merged
    return RetrievalFilters(
        course_ids=(course_id,),
        knowledge_base_ids=knowledge_base_ids,
        document_ids=resolved.document_ids,
    )


def _resolve_positive_int(value: Any, *, label: str) -> int:
    """校验正整数配置；非法时显式失败。"""

    if isinstance(value, bool) or not isinstance(value, int):
        raise GradingInputError(f"{label} 必须是正整数。")
    if value <= 0:
        raise GradingInputError(f"{label} 必须大于 0。")
    return value


def _resolve_retriever(
    mode: RetrievalMode,
    *,
    candidate_k: int,
    retriever: BaseRetriever | None,
    settings: AppSettings | None,
) -> BaseRetriever:
    """按模式解析检索实现；只传各构造器支持的参数。"""

    if retriever is not None:
        return retriever
    if mode in {RetrievalMode.HYBRID, RetrievalMode.HYBRID_RERANK}:
        kwargs: dict[str, Any] = {"candidate_k": candidate_k}
        if settings is not None:
            kwargs["vector_weight"] = settings.hybrid_vector_weight
        return get_retriever(RetrievalMode.HYBRID, **kwargs)
    # vector_only / keyword_only 的构造器不接受候选数参数。
    return get_retriever(mode)


def _build_search_query(
    mode: RetrievalMode,
    *,
    query_text: str,
    embedding: Sequence[float] | None,
) -> str | Sequence[float] | RetrievalQuery:
    """按模式构造检索入参：Hybrid 需要文本 + 向量，Vector 只要向量，Keyword 只要文本。"""

    if mode in {RetrievalMode.HYBRID, RetrievalMode.HYBRID_RERANK}:
        if embedding is None:  # pragma: no cover - 由上层保证已计算
            raise GradingInputError("Hybrid 检索需要查询向量。")
        return RetrievalQuery(text=query_text, embedding=tuple(embedding))
    if mode is RetrievalMode.VECTOR_ONLY:
        if embedding is None:  # pragma: no cover - 由上层保证已计算
            raise GradingInputError("向量检索需要查询向量。")
        return tuple(embedding)
    return query_text


def _compose_final_context(
    chunks: Sequence[RetrievedChunk],
) -> tuple[tuple[RetrievedChunk, ...], str]:
    """按片段与总预算组装 Final Context，只返回实际写入的片段。"""

    written: list[RetrievedChunk] = []
    entries: list[str] = []
    for chunk in chunks:
        if not str(chunk.content).strip():
            # 零有效正文不写入，也不计入溯源。
            continue
        header = (
            f"[片段 {len(written) + 1}] chunk_id={chunk.chunk_id} "
            f"document_id={chunk.document_id} course_id={chunk.course_id} "
            f"rank={chunk.rank} score={chunk.score:g}"
        )
        if chunk.semantic_score is not None:
            header += f" semantic_score={chunk.semantic_score:g}"
        if chunk.keyword_score is not None:
            header += f" keyword_score={chunk.keyword_score:g}"
        if chunk.fusion_score is not None:
            header += f" fusion_score={chunk.fusion_score:g}"
        if chunk.rerank_score is not None:
            header += f" rerank_score={chunk.rerank_score:g}"
        entry = f"{header}\n{_truncate(str(chunk.content), PER_CHUNK_CHARS)}"
        if len("\n".join([*entries, entry])) > MAX_FINAL_CONTEXT_CHARS:
            # 预算不足以完整写入该片段时停止，避免声明未真正使用的来源。
            break
        written.append(chunk)
        entries.append(entry)
    return tuple(written), "\n".join(entries)


async def build_grading_context(
    session: Session,
    source: SubjectiveGradingSource,
    *,
    mode: RetrievalMode | str = RetrievalMode.HYBRID_RERANK,
    top_k: int = DEFAULT_TOP_K,
    candidate_k: int | None = None,
    fusion_top_k: int | None = None,
    filters: RetrievalFilters | None = None,
    retriever: BaseRetriever | None = None,
    reranker: BaseReranker | None = None,
    embedding_provider: BaseEmbeddingProvider | None = None,
    require_context: bool = True,
    settings: AppSettings | None = None,
) -> GradingContext:
    """组装主观题评分上下文（Query → Hybrid 检索 → Rerank → Final Context）。

    参数语义：

    - ``candidate_k``：Hybrid 每路召回数，默认读取 ``RERANK_MAX_CANDIDATES``；
    - ``fusion_top_k``：送入重排的融合候选数，默认与 ``candidate_k`` 相同；
    - ``top_k``：最终返回并写入 Final Context 的片段数。

    ``HYBRID_RERANK`` 是 FR-032 规定的正式评分路径；``hybrid`` / ``vector_only`` /
    ``keyword_only`` 仅供测试与 Benchmark 使用，**不属于正式评分路径**。

    ``session`` 由调用方提供与管理，本函数不创建会话、不跨线程传递，也不嵌套
    ``asyncio.run``。
    """

    resolved_mode = normalize_mode(mode)
    resolved_settings = settings if settings is not None else get_settings()
    resolved_candidate_k = _resolve_positive_int(
        candidate_k
        if candidate_k is not None
        else resolved_settings.rerank_max_candidates,
        label="candidate_k",
    )
    resolved_fusion_top_k = _resolve_positive_int(
        fusion_top_k if fusion_top_k is not None else resolved_candidate_k,
        label="fusion_top_k",
    )
    limit = normalize_top_k(top_k)

    query_text = build_query_text(source)
    scope = _merge_filters(source, filters)

    embedding: list[float] | None = None
    if resolved_mode in {
        RetrievalMode.HYBRID,
        RetrievalMode.HYBRID_RERANK,
        RetrievalMode.VECTOR_ONLY,
    }:
        if embedding_provider is not None:
            provider = embedding_provider
        elif settings is not None:
            provider = create_embedding_provider(resolved_settings)
        else:
            provider = get_embedding_provider()
        embedding = await provider.embed_query(query_text)

    active_retriever = _resolve_retriever(
        resolved_mode,
        candidate_k=resolved_candidate_k,
        retriever=retriever,
        settings=settings,
    )
    search_query = _build_search_query(
        resolved_mode,
        query_text=query_text,
        embedding=embedding,
    )
    candidate_limit = (
        resolved_fusion_top_k
        if resolved_mode is RetrievalMode.HYBRID_RERANK
        else limit
    )
    if resolved_mode in {RetrievalMode.HYBRID, RetrievalMode.HYBRID_RERANK}:
        candidates = cast("_HybridLikeRetriever", active_retriever).search(
            session,
            cast("RetrievalQuery", search_query),
            top_k=candidate_limit,
            filters=scope,
        )
    else:
        candidates = active_retriever.search(
            session,
            cast("str | Sequence[float]", search_query),
            top_k=candidate_limit,
            filters=scope,
        )

    ranked: list[RetrievedChunk] = list(candidates)
    if resolved_mode is RetrievalMode.HYBRID_RERANK and ranked:
        if reranker is not None:
            active_reranker = reranker
        elif settings is not None:
            active_reranker = build_reranker(settings=resolved_settings)
        else:
            active_reranker = build_reranker()
        ranked = await active_reranker.rerank_async(query_text, ranked, limit)

    written, final_context = _compose_final_context(ranked)
    context = GradingContext(
        source=source,
        query_text=query_text,
        retrieval_mode=resolved_mode,
        filters=scope,
        chunks=written,
        retrieved_context_ids=tuple(chunk.chunk_id for chunk in written),
        final_context=final_context,
        candidate_count=len(ranked),
    )
    if require_context:
        context.ensure_sufficient()
    return context


__all__ = [
    "GRADING_INVALID_INPUT",
    "GRADING_MISSING_CONTEXT",
    "GRADING_MISSING_REFERENCE_ANSWER",
    "GRADING_MODE_MISMATCH",
    "MAX_FINAL_CONTEXT_CHARS",
    "MAX_QUERY_CHARS",
    "PER_CHUNK_CHARS",
    "QUERY_FIELD_BUDGETS",
    "STUDENT_QUERY_BUDGET",
    "TRUNCATION_MARKER",
    "GradingContext",
    "GradingContextError",
    "GradingInputError",
    "GradingModeMismatchError",
    "InsufficientContextError",
    "ReferenceAnswerMissingError",
    "SubjectiveGradingSource",
    "build_grading_context",
    "build_query_text",
]
