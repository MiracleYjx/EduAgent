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
from typing import TYPE_CHECKING, Any, ClassVar, Final, Literal, Protocol, cast
from uuid import UUID

from sqlalchemy.orm import Session

from backend.app.ai.embedding.base import BaseEmbeddingProvider
from backend.app.ai.embedding.factory import (
    create_embedding_provider,
    get_embedding_provider,
)
from backend.app.ai.retrieval._filters import resolve_retrieval_scope
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
from backend.app.schemas.grading import ScoringInput, ScoringSourceReference
from backend.app.schemas.retrieval_scope import RetrievalScope
from backend.app.services.grading.question_router import (
    QuestionRouter,
    normalize_question_type,
)

if TYPE_CHECKING:
    from backend.app.services.settings.runtime_snapshot import RunSnapshotContext

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
    question_content: str
    reference_answer: str
    student_answer: str | Sequence[str] | None | _MissingArgument = _MISSING
    #: 旧考试链按课程隔离；个人练习链按科目隔离，二者恰好提供一个。
    course_id: UUID | str | None = None
    subject_id: UUID | str | None = None
    scoring_rubric: str | None = None
    knowledge_points: tuple[str, ...] = ()
    knowledge_base_ids: tuple[str, ...] = ()
    material_version_ids: tuple[str, ...] = ()
    question_id: UUID | str | None = None
    answer_id: UUID | str | None = None
    submission_id: UUID | str | None = None
    retrieval_scope: RetrievalScope = field(default_factory=RetrievalScope)
    scoring_input: ScoringInput | None = None
    teacher_id: UUID | str | None = None
    #: 通用知识题不检索教材，也不伪造空片段引用。
    grounding: Literal["uploaded_material", "general_knowledge"] = "uploaded_material"
    #: 平台侧的评分模式校验器；默认使用无状态 :class:`QuestionRouter`。
    _router: QuestionRouter = field(
        default_factory=QuestionRouter, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        router = self._router if self._router is not None else QuestionRouter()
        normalized_type = normalize_question_type(self.question_type)
        if router.route_type(normalized_type) is not GradingMode.SUBJECTIVE:
            raise GradingModeMismatchError(
                f"题型 {normalized_type} 属于客观题路径，不能组装主观题评分上下文。"
            )
        object.__setattr__(self, "question_type", normalized_type)

        if (
            not isinstance(self.reference_answer, str)
            or not self.reference_answer.strip()
        ):
            raise ReferenceAnswerMissingError("题目缺少标准答案，无法组装评分依据。")

        if not isinstance(self.scoring_rubric, str) or not self.scoring_rubric.strip():
            raise GradingInputError("题目缺少非空的评分标准，无法按规则评分。")
        object.__setattr__(self, "scoring_rubric", self.scoring_rubric.strip())

        if (self.course_id is None) == (self.subject_id is None):
            raise GradingInputError("课程标识与个人科目标识必须恰好提供一个。")
        if self.course_id is not None:
            object.__setattr__(
                self, "course_id", _as_uuid_text(self.course_id, label="课程标识")
            )
        if self.subject_id is not None:
            object.__setattr__(
                self, "subject_id", _as_uuid_text(self.subject_id, label="个人科目标识")
            )
        if self.grounding not in {"uploaded_material", "general_knowledge"}:
            raise GradingInputError("评分依据必须是上传资料或通用知识。")
        if self.grounding == "general_knowledge" and (
            self.material_version_ids or not self.retrieval_scope.is_empty
        ):
            raise GradingInputError("通用知识评分不得携带教材检索范围。")
        object.__setattr__(
            self,
            "material_version_ids",
            _as_uuid_text_tuple(
                self.material_version_ids, label="资料版本标识"
            ),
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
        fixed = self.scoring_input
        if fixed is not None:
            if self.subject_id is not None:
                raise GradingInputError("个人练习不沿用旧考试评分输入。")
            expected_answer = fixed.student_answer
            if isinstance(expected_answer, list):
                expected_answer = "\n".join(expected_answer)
            if (
                str(self.question_id) != fixed.question_id
                or str(self.answer_id) != fixed.answer_id
                or str(self.submission_id) != fixed.submission_id
                or str(self.course_id) != fixed.course_id
                or self.question_type != fixed.question_type
                or self.question_content != fixed.question_content
                or self.reference_answer != fixed.reference_answer
                or self.scoring_rubric != (fixed.source_rubric or "").strip()
                or self.student_answer != expected_answer
                or self.knowledge_points != tuple(fixed.published_knowledge_points)
            ):
                raise GradingInputError("主观题来源与真实固定本场输入不一致。")
            object.__setattr__(self, "scoring_input", fixed.model_copy(deep=True))

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


def _query_fields(source: SubjectiveGradingSource) -> list[tuple[str, str, int]]:
    """Allocate one field budget shared by trace text and keyword facts."""
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

    return [(label, text, budgets[label]) for label, text in sections]


def build_query_text(source: SubjectiveGradingSource) -> str:
    """Preserve the ordered, labelled, bounded query used for trace/semantic input."""

    return "\n".join(
        f"{label}\n{_truncate(text, budget)}"
        for label, text, budget in _query_fields(source)
    )


def build_keyword_query_text(source: SubjectiveGradingSource) -> str:
    """Search the same bounded facts without generated labels/truncation terms.

    Only this producer's injected marker is excluded. Identical text actually
    supplied by the user remains part of the query; the general retriever does
    no stripping or fallback.
    """

    facts = []
    for _label, text, budget in _query_fields(source):
        bounded = _truncate(text, budget)
        if len(text) > budget > len(TRUNCATION_MARKER):
            bounded = bounded[:-len(TRUNCATION_MARKER)]
        facts.append(bounded)
    return "\n".join(facts)


@dataclass(frozen=True, slots=True)
class GradingContext:
    """主观题评分使用的检索上下文与溯源信息。"""

    source: SubjectiveGradingSource
    query_text: str
    retrieval_mode: RetrievalMode | None
    filters: RetrievalFilters
    chunks: tuple[RetrievedChunk, ...]
    retrieved_context_ids: tuple[str, ...]
    final_context: str
    candidate_count: int

    @property
    def is_sufficient(self) -> bool:
        """返回上下文是否足以支撑评分（至少一条片段写入且正文非空）。"""

        return bool(self.chunks)

    def enriched_scoring_input(self) -> ScoringInput | None:
        """Record only the actual context and chunks written into this model request."""
        fixed = self.source.scoring_input
        if fixed is None:
            return None
        references = [
            ScoringSourceReference(
                chunk_id=chunk.chunk_id,
                course_id=chunk.course_id,
                document_id=chunk.document_id,
                metadata=dict(chunk.metadata),
            )
            for chunk in self.chunks
        ]
        return fixed.model_copy(
            deep=True,
            update={
                "course_context": self.final_context,
                "source_references": references,
            },
        )

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
    """合并调用方过滤条件，只允许收窄既有评分范围。

    旧考试链强制课程范围；个人练习链强制科目范围，并以练习开始时冻结的
    资料版本/资料标识为上限。调用方提供任何交集为空的过滤条件时显式失败，
    不得静默退回到更大范围。
    """

    if source.subject_id is not None:
        return _merge_personal_filters(source, filters)

    course_id = UUID(str(source.course_id))
    source_knowledge_bases = tuple(UUID(value) for value in source.knowledge_base_ids)
    document_ids = source.retrieval_scope.document_ids
    if filters is None:
        return RetrievalFilters(
            course_ids=(course_id,),
            knowledge_base_ids=source_knowledge_bases,
            document_ids=document_ids,
        )
    resolved = resolve_filters(filters)
    if resolved.subject_ids or resolved.material_version_ids:
        raise GradingInputError(
            "Course grading does not support subject or material-version filters."
        )
    if resolved.course_ids and course_id not in resolved.course_ids:
        raise GradingInputError("调用方过滤条件与题目所属课程不一致，拒绝跨课程检索。")
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
    if document_ids and resolved.document_ids:
        allowed_documents = set(document_ids)
        document_ids = tuple(
            value for value in resolved.document_ids if value in allowed_documents
        )
        if not document_ids:
            raise GradingInputError(
                "调用方资料过滤与显式检索范围无交集，拒绝扩大范围。"
            )
    else:
        document_ids = document_ids or resolved.document_ids
    return RetrievalFilters(
        course_ids=(course_id,),
        knowledge_base_ids=knowledge_base_ids,
        document_ids=document_ids,
    )


def _merge_personal_filters(
    source: SubjectiveGradingSource,
    filters: RetrievalFilters | None,
) -> RetrievalFilters:
    """个人科目范围；资料版本与资料标识只能收窄，不能扩大到其他科目。"""

    subject_id = UUID(str(source.subject_id))
    allowed_documents = source.retrieval_scope.document_ids
    allowed_versions = tuple(UUID(value) for value in source.material_version_ids)
    if filters is None:
        return RetrievalFilters(
            subject_ids=(subject_id,),
            document_ids=allowed_documents,
            material_version_ids=allowed_versions,
        )
    resolved = resolve_filters(filters)
    if resolved.course_ids or resolved.knowledge_base_ids:
        raise GradingInputError(
            "个人评分不接受旧课程或知识库过滤条件。"
        )
    if resolved.subject_ids and resolved.subject_ids != (subject_id,):
        raise GradingInputError("调用方科目范围与题目所属科目不一致，拒绝跨科目检索。")
    document_ids = allowed_documents
    if resolved.document_ids:
        if not set(resolved.document_ids) <= set(allowed_documents):
            raise GradingInputError("调用方资料范围超出本场冻结依据，拒绝扩大检索。")
        document_ids = resolved.document_ids
    material_version_ids = allowed_versions
    if resolved.material_version_ids:
        if not set(resolved.material_version_ids) <= set(allowed_versions):
            raise GradingInputError("调用方资料版本超出本场冻结依据，拒绝扩大检索。")
        material_version_ids = resolved.material_version_ids
    return RetrievalFilters(
        subject_ids=(subject_id,),
        document_ids=document_ids,
        material_version_ids=material_version_ids,
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
    vector_weight: float | None = None,
    index_identity: Mapping[str, Any] | None = None,
) -> BaseRetriever:
    """按模式解析检索实现；只传各构造器支持的参数。"""

    if retriever is not None:
        return retriever
    if mode in {RetrievalMode.HYBRID, RetrievalMode.HYBRID_RERANK}:
        kwargs: dict[str, Any] = {"candidate_k": candidate_k}
        if vector_weight is not None:
            kwargs["vector_weight"] = vector_weight
        elif settings is not None:
            kwargs["vector_weight"] = settings.hybrid_vector_weight
        if index_identity is not None:
            kwargs["index_identity"] = index_identity
        return get_retriever(RetrievalMode.HYBRID, **kwargs)
    # vector_only / keyword_only 的构造器不接受候选数参数。
    if index_identity is not None and mode is RetrievalMode.VECTOR_ONLY:
        return get_retriever(mode, index_identity=index_identity)
    return get_retriever(mode)


def _build_search_query(
    mode: RetrievalMode,
    *,
    query_text: str,
    embedding: Sequence[float] | None,
    retrieval_scope: RetrievalScope,
) -> str | Sequence[float] | RetrievalQuery:
    """显式范围通过 Query 贯通所有模式；缺省范围保留原调用形式。"""

    if not retrieval_scope.is_empty:
        return RetrievalQuery(
            text=query_text,
            embedding=tuple(embedding or ()),
            chapter_ids=retrieval_scope.chapter_ids,
            section_range=retrieval_scope.section_range,
            knowledge_points=retrieval_scope.knowledge_points,
        )
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
    mode: RetrievalMode | str | None = None,
    top_k: int = DEFAULT_TOP_K,
    candidate_k: int | None = None,
    fusion_top_k: int | None = None,
    filters: RetrievalFilters | None = None,
    retriever: BaseRetriever | None = None,
    reranker: BaseReranker | None = None,
    embedding_provider: BaseEmbeddingProvider | None = None,
    require_context: bool = True,
    settings: AppSettings | None = None,
    runtime: RunSnapshotContext | None = None,
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

    query_text = build_query_text(source)
    if source.grounding == "general_knowledge":
        # 通用知识路径不检索教材，也不把空结果冒充为资料依据。
        return GradingContext(
            source=source,
            query_text=query_text,
            retrieval_mode=None,
            filters=RetrievalFilters(),
            chunks=(),
            retrieved_context_ids=(),
            final_context="",
            candidate_count=0,
        )
    if mode is None:
        configured_mode = (
            runtime.retrieval_mode("subjective_grading") if runtime is not None else None
        )
        mode = configured_mode or RetrievalMode.HYBRID_RERANK
    resolved_mode = normalize_mode(mode)
    resolved_settings = (
        settings
        if settings is not None
        else (None if runtime is not None else get_settings())
    )
    configured_candidate_k = (
        runtime.rerank_max_candidates
        if runtime is not None
        else (
            resolved_settings.rerank_max_candidates
            if resolved_settings is not None
            else None
        )
    )
    resolved_candidate_k = _resolve_positive_int(
        candidate_k if candidate_k is not None else configured_candidate_k,
        label="candidate_k",
    )
    resolved_fusion_top_k = _resolve_positive_int(
        fusion_top_k if fusion_top_k is not None else resolved_candidate_k,
        label="fusion_top_k",
    )
    limit = normalize_top_k(top_k)

    scope = _merge_filters(source, filters)
    if not source.retrieval_scope.is_empty:
        scope = resolve_retrieval_scope(
            session,
            RetrievalQuery(
                text=query_text,
                embedding=(),
                chapter_ids=source.retrieval_scope.chapter_ids,
                section_range=source.retrieval_scope.section_range,
                knowledge_points=source.retrieval_scope.knowledge_points,
            ),
            scope,
        )

    embedding: list[float] | None = None
    embedding_lease = None
    query_identity: Mapping[str, Any] | None = None
    if resolved_mode in {
        RetrievalMode.HYBRID,
        RetrievalMode.HYBRID_RERANK,
        RetrievalMode.VECTOR_ONLY,
    }:
        if embedding_provider is not None:
            provider = embedding_provider
        elif runtime is not None:
            embedding_lease = runtime.acquire("embedding")
            provider = embedding_lease.client
        elif settings is not None:
            provider = create_embedding_provider(resolved_settings)
        else:
            provider = get_embedding_provider()
        try:
            embedding = await provider.embed_query(query_text)
        finally:
            if embedding_lease is not None:
                await embedding_lease.arelease()

    if runtime is not None and embedding is not None:
        query_identity = provider.index_identity(dimension=len(embedding))
    active_retriever = _resolve_retriever(
        resolved_mode,
        candidate_k=resolved_candidate_k,
        retriever=retriever,
        settings=settings,
        vector_weight=runtime.hybrid_vector_weight if runtime is not None else None,
        index_identity=query_identity,
    )
    search_query = _build_search_query(
        resolved_mode,
        query_text=(query_text if resolved_mode is RetrievalMode.VECTOR_ONLY
                    else build_keyword_query_text(source)),
        embedding=embedding,
        retrieval_scope=source.retrieval_scope,
    )
    candidate_limit = (
        resolved_fusion_top_k if resolved_mode is RetrievalMode.HYBRID_RERANK else limit
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
            cast("str | Sequence[float] | RetrievalQuery", search_query),
            top_k=candidate_limit,
            filters=scope,
        )

    ranked: list[RetrievedChunk] = list(candidates)
    if resolved_mode is RetrievalMode.HYBRID_RERANK and ranked:
        reranker_lease = None
        if reranker is not None:
            active_reranker = reranker
        elif runtime is not None:
            reranker_lease = runtime.acquire("rerank")
            active_reranker = reranker_lease.client
        elif settings is not None:
            active_reranker = build_reranker(settings=resolved_settings)
        else:
            active_reranker = build_reranker()
        try:
            ranked = await active_reranker.rerank_async(query_text, ranked, limit)
        finally:
            if reranker_lease is not None:
                await reranker_lease.arelease()

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
