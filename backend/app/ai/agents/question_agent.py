"""Question Agent：课程检索与结构化候选题目生成（T067）。

契约依据：FR-024~FR-027、`.specify/plan.md` §4.1 AI 出题完整链路与 §2 语义/Hybrid 检索、
`.specify/contracts/agent-workflow.md` 的 Question Agent 职责，以及宪章 IV 结构化输出铁律。

硬约束：

- **只走结构化输出**：候选题目必须通过
  :meth:`backend.app.ai.llm.base.BaseLLMProvider.generate_structured` 取得，Schema 为本模块的
  :class:`QuestionGenerationPayload`（内部为 T010 定义的 ``QuestionCandidate`` 列表），
  **不做自由文本解析**；Pydantic 校验失败的响应属于结构化失败。
- **上下文不足即失败**：零候选或零有效正文一律判定上下文不足，不调用模型、不编造课程内容
  （plan §4.1 步骤 3）。
- **来源白名单**：候选题只能引用**真正写入最终 Prompt** 的片段 ID；虚构、跨课程或被预算截断
  而未写入 Prompt 的引用显式失败，不做“整批检索 ID 回填”，也不允许自证循环。
- **禁止自动发布**：候选状态必须保持 ``Candidate Generation``；本模块只产出候选，不写数据库，
  也不设置 ``Pending Review``/``Approved``（审核状态转换属 T068 与 T075）。
- **同一装配口径**：LLM、Embedding 与 Hybrid 检索都使用同一个 ``AppSettings``；显式注入的组件
  优先，便于测试与替换，不在导入期实例化 Provider。
- **异步不嵌套**：``generate`` 与 ``build_generation_context`` 均为 ``async``，直接 ``await``
  检索与生成，不调用 ``asyncio.run``。
- **错误脱敏**：公开失败只保留平台错误码、脱敏说明与来源码，不回显 Prompt、片段正文或模型原文。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Final, Protocol, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from backend.app.ai.agents.state import (
    AgentError,
    AgentInput,
    AgentOutput,
    AgentStatus,
    AgentType,
    QuestionGenerationRequest,
)
from backend.app.ai.embedding.base import (
    BaseEmbeddingProvider,
    EmbeddingProviderError,
    EmbeddingProviderNotReadyError,
)
from backend.app.ai.embedding.factory import (
    create_embedding_provider,
    get_embedding_provider,
)
from backend.app.ai.llm.base import BaseLLMProvider, LLMMessage, LLMMessages
from backend.app.ai.llm.factory import create_llm_provider
from backend.app.ai.retrieval.base import (
    DEFAULT_TOP_K,
    BaseRetriever,
    RetrievalError,
    RetrievalFilters,
    RetrievalMode,
    RetrievalQuery,
    RetrievalUnsupportedDialectError,
    RetrievedChunk,
    get_retriever,
    normalize_mode,
    normalize_top_k,
)
from backend.app.ai.retrieval.hybrid_search import DEFAULT_CANDIDATE_K
from backend.app.core.config import AppSettings, get_settings
from backend.app.core.retry_policy import ProviderExecutionError
from backend.app.domain.enums import ValidationStatus
from backend.app.schemas.ai import QuestionCandidate

#: 出题条件非法或缺失。
QUESTION_INVALID_INPUT: Final[str] = "QUESTION_INVALID_INPUT"
#: 缺少整个出题条件；不得以默认条件生成题目。
QUESTION_MISSING_GENERATION_REQUEST: Final[str] = "QUESTION_MISSING_GENERATION_REQUEST"
#: 检索上下文不足；不得编造课程内容。
QUESTION_INSUFFICIENT_CONTEXT: Final[str] = "QUESTION_INSUFFICIENT_CONTEXT"
#: 候选引用了未写入生成 Prompt 的片段（虚构、跨课程或被预算截断）。
QUESTION_UNKNOWN_SOURCE_CONTEXT: Final[str] = "QUESTION_UNKNOWN_SOURCE_CONTEXT"
#: 候选数量与教师要求不一致。
QUESTION_CANDIDATE_COUNT_MISMATCH: Final[str] = "QUESTION_CANDIDATE_COUNT_MISMATCH"
#: 候选题型与教师要求不一致。
QUESTION_TYPE_MISMATCH: Final[str] = "QUESTION_TYPE_MISMATCH"
#: 候选状态被改成非候选生成状态；禁止自动发布。
QUESTION_NOT_CANDIDATE_STATUS: Final[str] = "QUESTION_NOT_CANDIDATE_STATUS"
#: LLM 响应未通过结构化校验（JSON 非法或违反 Schema）。
QUESTION_INVALID_LLM_RESPONSE: Final[str] = "QUESTION_INVALID_LLM_RESPONSE"
#: 出题 Provider 调用失败。
QUESTION_PROVIDER_FAILED: Final[str] = "QUESTION_PROVIDER_FAILED"
#: 出题 Provider 未配置或未就绪。
QUESTION_PROVIDER_NOT_READY: Final[str] = "QUESTION_PROVIDER_NOT_READY"
#: Embedding Provider 未就绪（缺少依赖或配置）。
QUESTION_EMBEDDING_PROVIDER_NOT_READY: Final[str] = "QUESTION_EMBEDDING_PROVIDER_NOT_READY"
#: Embedding 调用失败；可重试语义按来源错误保真传递。
QUESTION_EMBEDDING_PROVIDER_FAILED: Final[str] = "QUESTION_EMBEDDING_PROVIDER_FAILED"
#: 当前数据库方言不支持所需检索模式。
QUESTION_RETRIEVAL_UNSUPPORTED_DIALECT: Final[str] = "QUESTION_RETRIEVAL_UNSUPPORTED_DIALECT"
#: 检索输入或查询失败。
QUESTION_RETRIEVAL_FAILED: Final[str] = "QUESTION_RETRIEVAL_FAILED"

#: 提示版本；作为系统提示首行的稳定前缀。
QUESTION_GENERATION_PROMPT_VERSION: Final[str] = "question-generation-v1"

#: 候选题目必须保持的生成状态；任何其它取值都不得由本模块产出。
CANDIDATE_GENERATION_STATUS: Final[str] = "Candidate Generation"

#: Provider 结构化失败码：属于无效响应而不是调用故障。
STRUCTURED_FAILURE_CODES: Final[frozenset[str]] = frozenset(
    {"StructuredOutputFailed", "ProviderEmptyResponse"}
)

#: 检索查询文本上限；完整教师条件另行走生成 Prompt，不因该上限而丢失。
MAX_GENERATION_QUERY_CHARS: Final[int] = 1000
#: 查询各字段预算；实际额度还受总额限制。
QUERY_FIELD_BUDGETS: Final[Mapping[str, int]] = {
    "【课程】": 120,
    "【知识点】": 480,
    "【难度】": 120,
    "【题型】": 120,
    "【数量】": 40,
}
#: 单条片段写入生成 Prompt 的正文字符上限。
PER_CHUNK_CHARS: Final[int] = 800
#: 生成 Prompt 中课程片段的总字符上限。
MAX_FINAL_CONTEXT_CHARS: Final[int] = 6000
#: 截断标记；计入字段预算。
TRUNCATION_MARKER: Final[str] = "…（已截断）"

_SECTION_COURSE: Final[str] = "【课程】"
_SECTION_KNOWLEDGE: Final[str] = "【知识点】"
_SECTION_DIFFICULTY: Final[str] = "【难度】"
_SECTION_TYPE: Final[str] = "【题型】"
_SECTION_COUNT: Final[str] = "【数量】"
_UNSPECIFIED: Final[str] = "未指定"


class QuestionGenerationPayload(BaseModel):
    """模型必须返回的结构化出题 Payload。

    平台字段（来源片段标识、审核状态）不属于模型 Payload 的判定依据：``status`` 由
    ``QuestionCandidate`` 固定为 ``Candidate Generation``，``source_context_ids`` 由平台按
    生成 Prompt 白名单校验收紧。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    candidates: list[QuestionCandidate] = Field(
        min_length=1, description="候选题目列表，至少一道。"
    )


class FailureSource(Protocol):
    """失败输出的来源错误最小协议；Provider、Embedding 与检索错误都满足。"""

    @property
    def error_code(self) -> str: ...

    @property
    def retryable(self) -> bool: ...


class QuestionGenerationError(RuntimeError):
    """出题失败基类；默认按不可重试的业务错误处理。"""

    error_code: ClassVar[str] = QUESTION_INVALID_INPUT
    #: 是否可重试；Provider 或检索失败时按来源错误保真覆盖。
    retryable: bool = False

    def __init__(
        self,
        detail: str,
        *,
        retryable: bool | None = None,
        source_code: str | None = None,
        attempt_count: int | None = None,
    ) -> None:
        super().__init__(f"{self.error_code}：{detail}")
        self.detail = detail
        if retryable is not None:
            self.retryable = retryable
        self.source_code = source_code
        self.attempt_count = attempt_count


class InsufficientGenerationContextError(QuestionGenerationError):
    """检索上下文不足；不得用空上下文继续生成。"""

    error_code: ClassVar[str] = QUESTION_INSUFFICIENT_CONTEXT


class UnknownSourceContextError(QuestionGenerationError):
    """候选引用了未写入生成 Prompt 的片段。"""

    error_code: ClassVar[str] = QUESTION_UNKNOWN_SOURCE_CONTEXT


class CandidateCountMismatchError(QuestionGenerationError):
    """候选数量与教师要求不一致。"""

    error_code: ClassVar[str] = QUESTION_CANDIDATE_COUNT_MISMATCH


class QuestionTypeMismatchError(QuestionGenerationError):
    """候选题型与教师要求不一致。"""

    error_code: ClassVar[str] = QUESTION_TYPE_MISMATCH


class NotCandidateGenerationError(QuestionGenerationError):
    """候选状态被改成可发布状态；禁止自动发布。"""

    error_code: ClassVar[str] = QUESTION_NOT_CANDIDATE_STATUS


class InvalidGenerationResponseError(QuestionGenerationError):
    """模型响应不是约定的结构化结果。"""

    error_code: ClassVar[str] = QUESTION_INVALID_LLM_RESPONSE


class ProviderFailedError(QuestionGenerationError):
    """出题 Provider 调用失败。"""

    error_code: ClassVar[str] = QUESTION_PROVIDER_FAILED


class ProviderNotReadyError(QuestionGenerationError):
    """出题 Provider 未配置或未就绪；不得静默降级。"""

    error_code: ClassVar[str] = QUESTION_PROVIDER_NOT_READY


def _truncate(text: str, budget: int) -> str:
    """按预算截断文本；截断标记计入预算。"""

    if budget <= 0:
        return ""
    if len(text) <= budget:
        return text
    if budget <= len(TRUNCATION_MARKER):
        return text[:budget]
    return text[: budget - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


def build_generation_query(request: QuestionGenerationRequest) -> str:
    """按教师条件构造检索查询文本（课程 → 知识点 → 难度 → 题型 → 数量）。"""

    sections: list[tuple[str, str]] = [
        (_SECTION_COURSE, request.course_id),
        (_SECTION_KNOWLEDGE, "、".join(request.knowledge_points) or _UNSPECIFIED),
        (_SECTION_DIFFICULTY, request.difficulty or _UNSPECIFIED),
        (
            _SECTION_TYPE,
            request.question_type.value if request.question_type is not None else _UNSPECIFIED,
        ),
        (_SECTION_COUNT, str(request.count)),
    ]
    overhead = sum(len(label) + 1 for label, _ in sections) + (len(sections) - 1)
    remaining = MAX_GENERATION_QUERY_CHARS - overhead
    lines: list[str] = []
    for label, text in sections:
        granted = max(0, min(QUERY_FIELD_BUDGETS.get(label, 0), remaining))
        remaining -= granted
        lines.append(f"{label}\n{_truncate(text, granted)}")
    return "\n".join(lines)


def _compose_final_context(
    chunks: Sequence[RetrievedChunk],
) -> tuple[tuple[RetrievedChunk, ...], str]:
    """按片段与总预算组装生成用课程片段，只返回实际写入的部分。"""

    written: list[RetrievedChunk] = []
    entries: list[str] = []
    for chunk in chunks:
        if not str(chunk.content).strip():
            # 零有效正文不写入，也不计入来源白名单。
            continue
        header = f"[片段 {len(written) + 1}] chunk_id={chunk.chunk_id}"
        if chunk.document_id:
            header += f" document_id={chunk.document_id}"
        if chunk.score:
            header += f" score={chunk.score:g}"
        entry = f"{header}\n{_truncate(str(chunk.content), PER_CHUNK_CHARS)}"
        if len("\n".join([*entries, entry])) > MAX_FINAL_CONTEXT_CHARS:
            # 预算不足以完整写入该片段时停止，避免声明未真正使用的来源。
            break
        written.append(chunk)
        entries.append(entry)
    return tuple(written), "\n".join(entries)


@dataclass(frozen=True, slots=True)
class QuestionGenerationContext:
    """出题使用的课程片段与来源白名单。"""

    request: QuestionGenerationRequest
    query_text: str
    retrieval_mode: RetrievalMode
    chunks: tuple[RetrievedChunk, ...]
    retrieved_context_ids: tuple[str, ...]
    final_context: str
    candidate_count: int

    @property
    def is_sufficient(self) -> bool:
        """返回上下文是否足以支撑出题（至少一条片段写入且正文非空）。"""

        return bool(self.chunks)

    def ensure_sufficient(self) -> None:
        """上下文不足时显式失败；本模块不提供“无上下文生成”的降级路径。"""

        if not self.is_sufficient:
            raise InsufficientGenerationContextError(
                f"检索候选 {self.candidate_count} 条，写入生成上下文 "
                f"{len(self.chunks)} 条，上下文不足。"
            )


def build_generation_messages(
    request: QuestionGenerationRequest,
    context: QuestionGenerationContext,
) -> list[LLMMessage]:
    """构造生成消息：版本化系统提示 + 完整教师条件与来源白名单。"""

    return [
        {"role": "system", "content": _GENERATION_SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_message(request, context)},
    ]


_GENERATION_SYSTEM_PROMPT: Final[str] = (
    f"{QUESTION_GENERATION_PROMPT_VERSION}\n"
    "你是课程出题模型。请依据教师给出的课程、知识点、难度、题型与数量，结合课程知识片段"
    "生成候选题，并为每道题给出参考答案、评分标准、难度、知识点与分值。只能使用给定片段中"
    "的课程依据，不得编造课程内容；片段不足以支撑出题时不要勉强生成。\n"
    "每道候选题的 source_context_ids 只能填写你实际依据的片段标识（必须与本条消息中出现的 "
    "chunk_id 完全一致），不得填写其它标识。\n"
    "候选题只属于候选生成（Candidate Generation）状态，绝不能标记为已审核或已发布。\n"
    "请仅返回合法的 JSON 对象，不要输出 Markdown、解释或其它文本；JSON 必须符合以下 Schema：\n"
    + json.dumps(QuestionGenerationPayload.model_json_schema(), ensure_ascii=False)
)


def _build_user_message(
    request: QuestionGenerationRequest,
    context: QuestionGenerationContext,
) -> str:
    """构造用户消息：完整教师条件 + 课程片段 + 来源与输出要求。"""

    knowledge = "、".join(request.knowledge_points) or _UNSPECIFIED
    question_type = (
        request.question_type.value if request.question_type is not None else _UNSPECIFIED
    )
    return "\n".join(
        [
            "【教师出题条件（完整条件，不得忽略或替换）】",
            f"课程：{request.course_id}",
            f"知识点：{knowledge}",
            f"难度：{request.difficulty or _UNSPECIFIED}",
            f"题型：{question_type}",
            f"数量：{request.count}",
            "",
            "【检索查询（用于召回，可能被截断）】",
            context.query_text,
            "",
            "【课程知识片段】",
            context.final_context or "（无可用片段）",
            "",
            "【来源引用要求】",
            "每道候选题的 source_context_ids 只能填写上面出现过的 chunk_id，且必须是该题实际",
            "依据的片段；不得填写未出现的标识，也不得为了凑数引用未使用的片段。",
            "",
            "【输出要求】",
            f"必须返回 JSON 对象 {{\"candidates\": [...]}}，候选数量必须等于 {request.count}，",
            (
                f"题型必须为 {question_type}；参考答案必须与学生可提交的取值一致"
                "（字典选项填写选项键，列表选项填写选项全文，判断题无选项时使用 True/False），"
                "多选题参考答案填写选项键；评分标准必须写明可分配的分值数字（如每个要点 2 分）。"
            ),
            f"每道题的 status 只能是 {CANDIDATE_GENERATION_STATUS}。",
        ]
    )


def resolve_generation_retriever(
    mode: RetrievalMode | str,
    *,
    candidate_k: int | None = None,
    retriever: BaseRetriever | None = None,
    settings: AppSettings | None = None,
) -> BaseRetriever:
    """解析出题检索实现：显式注入优先，否则按同一 ``AppSettings`` 装配 Hybrid 检索。"""

    if retriever is not None:
        return retriever
    resolved_mode = normalize_mode(mode)
    if resolved_mode is RetrievalMode.HYBRID:
        kwargs: dict[str, Any] = {
            "candidate_k": normalize_top_k(
                candidate_k if candidate_k is not None else DEFAULT_CANDIDATE_K
            )
        }
        if settings is not None:
            kwargs["vector_weight"] = settings.hybrid_vector_weight
        return get_retriever(resolved_mode, **kwargs)
    # 其它模式（vector_only/keyword_only）的构造器不接受候选数参数。
    return get_retriever(resolved_mode)


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


async def build_generation_context(
    session: Session,
    request: QuestionGenerationRequest,
    *,
    mode: RetrievalMode | str = RetrievalMode.HYBRID,
    top_k: int = DEFAULT_TOP_K,
    candidate_k: int | None = None,
    retriever: BaseRetriever | None = None,
    embedding_provider: BaseEmbeddingProvider | None = None,
    settings: AppSettings | None = None,
) -> QuestionGenerationContext:
    """组装出题上下文（Query → Hybrid 检索 → 课程片段），不足时显式失败。

    ``session`` 由调用方提供与管理；本函数不创建会话、不调用 ``asyncio.run``，也不写数据库。
    """

    resolved_mode = normalize_mode(mode)
    resolved_settings = settings if settings is not None else get_settings()
    limit = normalize_top_k(top_k)
    query_text = build_generation_query(request)
    filters = RetrievalFilters(course_ids=(_as_course_uuid(request.course_id),))

    if embedding_provider is not None:
        provider = embedding_provider
    elif settings is not None:
        provider = create_embedding_provider(resolved_settings)
    else:
        provider = get_embedding_provider()
    embedding = await provider.embed_query(query_text)

    active_retriever = resolve_generation_retriever(
        resolved_mode,
        candidate_k=candidate_k,
        retriever=retriever,
        settings=settings,
    )
    if resolved_mode is RetrievalMode.HYBRID:
        candidates = cast("_HybridLikeRetriever", active_retriever).search(
            session,
            RetrievalQuery(text=query_text, embedding=tuple(embedding)),
            top_k=limit,
            filters=filters,
        )
    elif resolved_mode is RetrievalMode.VECTOR_ONLY:
        candidates = active_retriever.search(
            session,
            tuple(embedding),
            top_k=limit,
            filters=filters,
        )
    else:
        candidates = active_retriever.search(
            session,
            query_text,
            top_k=limit,
            filters=filters,
        )
    written, final_context = _compose_final_context(candidates)
    context = QuestionGenerationContext(
        request=request,
        query_text=query_text,
        retrieval_mode=resolved_mode,
        chunks=written,
        retrieved_context_ids=tuple(chunk.chunk_id for chunk in written),
        final_context=final_context,
        candidate_count=len(candidates),
    )
    context.ensure_sufficient()
    return context


def _as_course_uuid(value: str) -> UUID:
    """把课程标识规范化为 UUID；非法标识显式失败。"""

    try:
        return UUID(str(value).strip())
    except (AttributeError, TypeError, ValueError) as exc:
        raise QuestionGenerationError("课程标识无效，无法限定检索范围。") from exc


def _dedupe_preserving_order(values: Sequence[str]) -> list[str]:
    """去重并保留首次出现顺序。"""

    seen: set[str] = set()
    kept: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            kept.append(value)
    return kept


def _resolve_provider_model(provider: BaseLLMProvider) -> str | None:
    """读取 Provider 实际提供的模型标识；无法获取时返回 ``None``，不从全局配置猜测。"""

    for attribute in ("model_name", "model"):
        value = getattr(provider, attribute, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


class QuestionAgent:
    """Question Agent：检索课程资料、生成候选题目、输出结构化候选。

    :param provider: 出题 Provider；``None`` 表示调用时按配置解析，未就绪时显式失败。
    """

    def __init__(self, *, provider: BaseLLMProvider | None = None) -> None:
        self._provider = provider

    async def generate(
        self,
        session: Session,
        agent_input: AgentInput,
        *,
        top_k: int = DEFAULT_TOP_K,
        candidate_k: int | None = None,
        retriever: BaseRetriever | None = None,
        embedding_provider: BaseEmbeddingProvider | None = None,
        settings: AppSettings | None = None,
    ) -> AgentOutput:
        """返回结构化候选题目；候选始终保持 ``Candidate Generation``，不写数据库。"""

        if not isinstance(agent_input, AgentInput):
            return self._failure(QUESTION_INVALID_INPUT, "出题输入信封不合法。")
        request = agent_input.generation_request
        if request is None:
            return self._failure(
                QUESTION_MISSING_GENERATION_REQUEST,
                "缺少出题条件，无法生成候选题目。",
            )

        resolved_settings = settings if settings is not None else get_settings()
        try:
            context = await build_generation_context(
                session,
                request,
                top_k=top_k,
                candidate_k=candidate_k,
                retriever=retriever,
                embedding_provider=embedding_provider,
                settings=settings,
            )
        except QuestionGenerationError as exc:
            return self._failure(exc.error_code, exc.detail, error=exc)
        except EmbeddingProviderNotReadyError as exc:
            return self._failure(
                QUESTION_EMBEDDING_PROVIDER_NOT_READY,
                "Embedding Provider 未就绪，请检查 EMBEDDING_PROVIDER 与模型配置。",
                error=exc,
            )
        except EmbeddingProviderError as exc:
            return self._failure(
                QUESTION_EMBEDDING_PROVIDER_FAILED,
                "课程查询向量生成失败，无法检索出题依据。",
                error=exc,
            )
        except RetrievalUnsupportedDialectError as exc:
            return self._failure(
                QUESTION_RETRIEVAL_UNSUPPORTED_DIALECT,
                "当前数据库不支持所需检索模式，无法获取出题依据。",
                error=exc,
            )
        except RetrievalError as exc:
            return self._failure(
                QUESTION_RETRIEVAL_FAILED,
                "课程检索失败，无法获取出题依据。",
                error=exc,
            )

        messages = build_generation_messages(request, context)
        try:
            payload = await self._generate_payload(messages, settings=resolved_settings)
            candidates = self._validate_candidates(payload, request, context)
        except QuestionGenerationError as exc:
            return self._failure(exc.error_code, exc.detail, error=exc)

        provider = self._provider
        return AgentOutput(
            agent_type=AgentType.QUESTION,
            status=AgentStatus.SUCCESS,
            summary=(
                f"已生成 {len(candidates)} 道候选题目，保持候选生成状态，等待校验与教师审核。"
            ),
            validation_status=ValidationStatus.VALIDATED,
            question_candidates=candidates,
            retrieved_context_ids=list(context.retrieved_context_ids),
            model=_resolve_provider_model(provider) if provider is not None else None,
            prompt_version=QUESTION_GENERATION_PROMPT_VERSION,
        )

    async def _generate_payload(
        self,
        messages: LLMMessages,
        *,
        settings: AppSettings,
    ) -> QuestionGenerationPayload:
        """调用 Provider 获取结构化候选；失败按结构化失败/调用故障分类。"""

        provider = self._provider
        if provider is None:
            try:
                provider = create_llm_provider(settings)
            except Exception:  # noqa: BLE001 - 未就绪不得静默降级
                raise ProviderNotReadyError(
                    "出题 Provider 未就绪，请检查 LLM_PROVIDER 与模型配置。"
                ) from None
        try:
            result = await provider.generate_structured(
                messages, QuestionGenerationPayload
            )
        except ProviderExecutionError as exc:
            info = exc.info
            code = str(info.code)
            detail = (
                f"出题响应未通过结构化校验（来源码 {code}，尝试 {info.attempt_count} 次）。"
                if code in STRUCTURED_FAILURE_CODES
                else f"出题 Provider 调用失败（来源码 {code}，尝试 {info.attempt_count} 次）。"
            )
            error_type = (
                InvalidGenerationResponseError
                if code in STRUCTURED_FAILURE_CODES
                else ProviderFailedError
            )
            raise error_type(
                detail,
                retryable=bool(info.retryable),
                source_code=code,
                attempt_count=int(info.attempt_count),
            ) from None
        except QuestionGenerationError:
            raise
        except Exception:  # noqa: BLE001 - 统一收敛为脱敏 Provider 失败
            raise ProviderFailedError("出题 Provider 调用失败。") from None
        if not isinstance(result, QuestionGenerationPayload):
            raise InvalidGenerationResponseError("出题 Provider 未返回约定的结构化结果。")
        return result

    def _validate_candidates(
        self,
        payload: QuestionGenerationPayload,
        request: QuestionGenerationRequest,
        context: QuestionGenerationContext,
    ) -> list[QuestionCandidate]:
        """平台侧校验：数量、题型、来源白名单与候选状态。"""

        candidates = list(payload.candidates)
        if len(candidates) != request.count:
            raise CandidateCountMismatchError(
                f"候选数量 {len(candidates)} 与要求的 {request.count} 不一致。"
            )
        whitelist = set(context.retrieved_context_ids)
        validated: list[QuestionCandidate] = []
        for candidate in candidates:
            if request.question_type is not None and candidate.question_type != (
                request.question_type
            ):
                raise QuestionTypeMismatchError("候选题型与教师要求不一致。")
            if candidate.status != CANDIDATE_GENERATION_STATUS:
                raise NotCandidateGenerationError("候选题必须保持候选生成状态，不得自动发布。")
            cited = list(candidate.source_context_ids)
            unknown = [chunk_id for chunk_id in cited if chunk_id not in whitelist]
            if unknown:
                raise UnknownSourceContextError(
                    "候选题引用了未写入生成上下文的片段，已拒绝该结果。"
                )
            validated.append(
                candidate.model_copy(
                    update={"source_context_ids": _dedupe_preserving_order(cited)}
                )
            )
        return validated

    def _failure(
        self,
        code: str,
        detail: str,
        *,
        error: FailureSource | None = None,
    ) -> AgentOutput:
        """构造脱敏失败输出；失败不得同时声明需要人工复核。"""

        source_code = getattr(error, "source_code", None) if error is not None else None
        attempt_count = (
            getattr(error, "attempt_count", None) if error is not None else None
        )
        return AgentOutput(
            agent_type=AgentType.QUESTION,
            status=AgentStatus.FAILURE,
            error=AgentError(
                error_code=code,
                message=detail,
                retryable=bool(error.retryable) if error is not None else False,
                source_code=source_code,
                attempt_count=attempt_count,
            ),
        )


__all__ = [
    "CANDIDATE_GENERATION_STATUS",
    "MAX_FINAL_CONTEXT_CHARS",
    "MAX_GENERATION_QUERY_CHARS",
    "PER_CHUNK_CHARS",
    "QUESTION_CANDIDATE_COUNT_MISMATCH",
    "QUESTION_EMBEDDING_PROVIDER_FAILED",
    "QUESTION_EMBEDDING_PROVIDER_NOT_READY",
    "QUESTION_GENERATION_PROMPT_VERSION",
    "QUESTION_INSUFFICIENT_CONTEXT",
    "QUESTION_INVALID_INPUT",
    "QUESTION_INVALID_LLM_RESPONSE",
    "QUESTION_MISSING_GENERATION_REQUEST",
    "QUESTION_NOT_CANDIDATE_STATUS",
    "QUESTION_PROVIDER_FAILED",
    "QUESTION_PROVIDER_NOT_READY",
    "QUESTION_RETRIEVAL_FAILED",
    "QUESTION_RETRIEVAL_UNSUPPORTED_DIALECT",
    "QUESTION_TYPE_MISMATCH",
    "QUESTION_UNKNOWN_SOURCE_CONTEXT",
    "STRUCTURED_FAILURE_CODES",
    "CandidateCountMismatchError",
    "FailureSource",
    "InsufficientGenerationContextError",
    "InvalidGenerationResponseError",
    "NotCandidateGenerationError",
    "ProviderFailedError",
    "ProviderNotReadyError",
    "QuestionAgent",
    "QuestionGenerationContext",
    "QuestionGenerationError",
    "QuestionGenerationPayload",
    "QuestionTypeMismatchError",
    "UnknownSourceContextError",
    "build_generation_context",
    "build_generation_messages",
    "build_generation_query",
    "resolve_generation_retriever",
]
