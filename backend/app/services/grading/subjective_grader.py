"""Subjective Grader：基于 RAG 上下文与结构化输出的主观题评分。

契约依据：FR-031、FR-032、FR-034、FR-035，plan.md §5.3 与 §5.4，
``.specify/contracts/agent-workflow.md`` 的 ``Grade`` 与 ``Structured Validation`` 节点。

硬约束：

- **只走结构化输出**：评分结果必须通过
  :meth:`backend.app.ai.llm.base.BaseLLMProvider.generate_structured` 取得，再经
  Pydantic 校验后才能进入业务层；本模块**不做自由文本解析**（Constitution IV、FR-034）。
- **Prompt 合同**：系统提示首行为版本标识，随后是角色约束、JSON 输出指令与
  ``SubjectiveGradingPayload`` 的 JSON Schema（当前 Provider 只使用 JSON Object Mode，
  Schema 用于返回后校验，因此必须显式写入提示）。用户消息给出平台满分与 §5.3 的五要素。
- **数值口径**：先做类型/有限性检查，再做**原始**分数范围检查，然后 ``ROUND_HALF_UP``
  保留两位小数（对齐 ``Numeric(8, 2)``），最后再次检查范围；任何越界都显式失败，
  **不静默修正**。置信度不做舍入。
- **Provider 分类**：``StructuredOutputFailed`` / ``ProviderEmptyResponse`` 属于无效结构化响应；
  其它调用故障属于 Provider 失败；两者都只保留脱敏后的来源码与尝试次数，且不新增重试循环
  （重试由 M2 的 ``RetryPolicy`` 负责）。
- **错误脱敏**：公开异常只包含已知 Payload 字段名与错误类型，不回显响应正文、学生答案、
  未知额外字段名或 Pydantic ``ValidationError`` 全文。
- **置信度检查不可跳过**：入口返回前必执行置信度检查；未注入策略时使用本模块的默认检查
  （读取 ``AppSettings.confidence_threshold``），低置信度结果标记为 ``Pending Review``。
  T053 会以可配置的 ``ConfidencePolicy`` 替换该默认检查。
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, ClassVar, Final, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)
from sqlalchemy.orm import Session

from backend.app.ai.embedding.base import BaseEmbeddingProvider
from backend.app.ai.llm.base import (
    BaseLLMProvider,
    LLMMessage,
    LLMMessages,
    LLMProviderMetadata,
    describe_llm_provider,
)
from backend.app.ai.llm.factory import create_llm_provider
from backend.app.ai.retrieval.base import DEFAULT_TOP_K, BaseRetriever
from backend.app.ai.retrieval.reranker import BaseReranker
from backend.app.core.config import AppSettings, get_settings
from backend.app.core.retry_policy import ProviderExecutionError
from backend.app.domain.enums import (
    GradingMode,
    QuestionType,
    ReviewStatus,
    ValidationStatus,
)
from backend.app.schemas.ai import GradingResult, NonEmptyText
from backend.app.services.grading.confidence_policy import ConfidencePolicy
from backend.app.services.grading.grading_context import (
    GradingContext,
    GradingModeMismatchError,
    InsufficientContextError,
    ReferenceAnswerMissingError,
    SubjectiveGradingSource,
    build_grading_context,
)
from backend.app.services.grading.question_router import (
    QuestionRouter,
    normalize_question_type,
)

#: LLM 响应不是合法 JSON、不是对象或未通过 Schema 校验。
GRADING_INVALID_LLM_RESPONSE: Final[str] = "GRADING_INVALID_LLM_RESPONSE"
#: 模型给出的分数超出 [0, 满分] 区间；不得静默钳制。
GRADING_SCORE_OUT_OF_RANGE: Final[str] = "GRADING_SCORE_OUT_OF_RANGE"
#: 模型给出的置信度超出 [0, 1] 区间。
GRADING_CONFIDENCE_OUT_OF_RANGE: Final[str] = "GRADING_CONFIDENCE_OUT_OF_RANGE"
#: 题目满分非法或无法转换。
GRADING_INVALID_MAX_SCORE: Final[str] = "GRADING_INVALID_MAX_SCORE"
#: Provider 调用故障（超时、限流、服务失败）。
GRADING_PROVIDER_FAILED: Final[str] = "GRADING_PROVIDER_FAILED"
#: 评分 Provider 未配置或未就绪。
GRADING_PROVIDER_NOT_READY: Final[str] = "GRADING_PROVIDER_NOT_READY"

#: 提示版本；作为系统提示首行的稳定前缀，供后续前缀缓存任务复用。
SUBJECTIVE_GRADING_PROMPT_VERSION: Final[str] = "subjective-grading-v1"

#: 分数最小量化单位，对齐题目分值的 ``Numeric(8, 2)``。
SCORE_QUANTUM: Final[Decimal] = Decimal("0.01")

#: Provider 结构化失败码：属于无效响应而非调用故障。
STRUCTURED_FAILURE_CODES: Final[frozenset[str]] = frozenset(
    {"StructuredOutputFailed", "ProviderEmptyResponse"}
)


class SubjectiveGradingError(RuntimeError):
    """主观题评分失败基类；默认按不可重试的业务错误处理。

    ``retryable`` 默认不可重试；当失败来自 Provider 时，映射会按来源错误的
    ``info.retryable`` 保真覆盖，避免把不可重试错误当成可重试错误重复提交。
    """

    error_code: ClassVar[str] = GRADING_INVALID_LLM_RESPONSE
    #: 是否可重试；Provider 分类时按来源错误保真覆盖。
    retryable: bool = False

    def __init__(
        self,
        detail: str,
        *,
        retryable: bool | None = None,
        source_code: str | None = None,
        status: str | None = None,
        attempt_count: int | None = None,
    ) -> None:
        super().__init__(f"{self.error_code}：{detail}")
        self.detail = detail
        if retryable is not None:
            self.retryable = retryable
        #: Provider 来源错误码（脱敏后），便于诊断与重试决策。
        self.source_code = source_code
        #: Provider 脱敏状态值。
        self.status = status
        #: Provider 实际尝试次数。
        self.attempt_count = attempt_count


class InvalidLLMResponseError(SubjectiveGradingError):
    """模型响应不是合法结构化结果；不得进入业务层。"""

    error_code: ClassVar[str] = GRADING_INVALID_LLM_RESPONSE


class ScoreOutOfRangeError(SubjectiveGradingError):
    """分数超出 [0, 满分]；不做静默修正。"""

    error_code: ClassVar[str] = GRADING_SCORE_OUT_OF_RANGE


class ConfidenceOutOfRangeError(SubjectiveGradingError):
    """置信度超出 [0, 1]。"""

    error_code: ClassVar[str] = GRADING_CONFIDENCE_OUT_OF_RANGE


class InvalidMaxScoreError(SubjectiveGradingError):
    """题目满分非法；无法计算合法分数。"""

    error_code: ClassVar[str] = GRADING_INVALID_MAX_SCORE


class ProviderFailedError(SubjectiveGradingError):
    """评分 Provider 调用失败；可重试性由来源错误的 ``info.retryable`` 决定。"""

    error_code: ClassVar[str] = GRADING_PROVIDER_FAILED


class ProviderNotReadyError(SubjectiveGradingError):
    """评分 Provider 未配置或未就绪；不得静默降级。"""

    error_code: ClassVar[str] = GRADING_PROVIDER_NOT_READY


class SubjectiveGradingPayload(BaseModel):
    """模型必须返回的结构化评分 Payload。

    平台字段（``question_type``、``max_score``、``knowledge_points``、
    ``retrieved_context_ids``、``answer_id``、``submission_id``、``validation_status``、
    ``review_status``）**不属于**模型 Payload：它们由平台回填，模型只能给出以下字段。

    ``score`` 与 ``confidence`` 保持无约束数值类型，以便由平台给出更精确的范围错误码；
    ``correct_points`` 与 ``missing_knowledge_points`` 允许为空列表（全对或全错时合法）。
    """

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    score: float = Field(description="本题得分，平台会再做范围与量化校验。")
    confidence: float = Field(description="评分置信度，范围 0 到 1。")
    reason: NonEmptyText = Field(description="评分理由。")
    correct_points: list[NonEmptyText] = Field(
        description="命中的正确知识点或要点；全错时可为空列表。"
    )
    missing_knowledge_points: list[NonEmptyText] = Field(
        description="缺失的知识点或要点；全对时可为空列表。"
    )
    suggestions: list[NonEmptyText] = Field(
        min_length=1,
        description="面向学生的学习建议，至少一项。",
    )

    @field_validator("score", "confidence", mode="before")
    @classmethod
    def _reject_non_numeric_values(cls, value: Any) -> Any:
        """拒绝字符串与布尔值，并要求有限数值。"""

        if isinstance(value, (bool, str, bytes, bytearray)):
            # Pydantic 只把 ValueError/AssertionError 转换为校验失败，因此这里不用 TypeError。
            raise ValueError("必须是 JSON 数值")  # noqa: TRY004
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("必须是有限数值")
        return value


#: 允许出现在公开错误信息中的 Payload 字段名。
KNOWN_PAYLOAD_FIELDS: Final[frozenset[str]] = frozenset(
    SubjectiveGradingPayload.model_fields
)

#: 解析器共用的题型校验器：只接受 Subjective，避免直接调用时生成客观题结果。
_SUBJECTIVE_ONLY_ROUTER: Final[QuestionRouter] = QuestionRouter()

_SUBJECTIVE_GRADING_SYSTEM_PROMPT: Final[str] = (
    f"{SUBJECTIVE_GRADING_PROMPT_VERSION}\n"
    "你是课程主观题评分模型。请依据题目、标准答案、评分标准、学生答案与课程知识库上下文，"
    "给出不少于 0 且不超过本题满分的分数，以及 0 到 1 之间的置信度，并说明评分理由、"
    "命中的正确要点、缺失要点与学习建议。只能使用题干或上下文中的依据，不得编造课程内容；"
    "上下文不足时降低置信度而不是猜测。\n"
    "请仅返回合法的 JSON 对象，不要输出 Markdown、解释或其它文本；JSON 必须符合以下 Schema：\n"
    + json.dumps(SubjectiveGradingPayload.model_json_schema(), ensure_ascii=False)
)


class ConfidencePolicyLike(Protocol):
    """置信度策略的最小协议（由 T053 的 ``ConfidencePolicy`` 实现）。"""

    def apply(self, result: GradingResult) -> GradingResult: ...


def _resolve_max_score(value: Any) -> float:
    """校验并转换题目满分；只接受有限正数。

    非数值、``bool``、非有限数（NaN/±inf、``Decimal`` signaling NaN）、超出浮点范围的值与
    非正数统一转为 :class:`InvalidMaxScoreError`，不泄漏 ``ValueError`` / ``OverflowError``。
    """

    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise InvalidMaxScoreError(
            f"题目满分必须是有限正数，收到类型 {type(value).__name__}。"
        )
    number: float | None = None
    failed = False
    try:
        number = float(value)
    except (ValueError, OverflowError, InvalidOperation):
        failed = True
    if failed or number is None:
        raise InvalidMaxScoreError(
            f"题目满分必须是有限正数，收到 {value!r}；该数值无法转换为可比较的有限浮点数。"
        )
    if not math.isfinite(number) or number <= 0:
        raise InvalidMaxScoreError(f"题目满分必须是有限正数，收到 {value!r}。")
    return number


def _reject_json_constant(value: str) -> Any:
    """拒绝 JSON 中的 ``NaN`` / ``Infinity`` 字面量。"""

    raise ValueError(f"不接受的 JSON 常量：{value}")


def _load_payload(raw: Any) -> Mapping[str, Any]:
    """把响应文本或映射归一为映射；非对象与非法 JSON 显式失败。"""

    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="strict")
    if not isinstance(raw, str):
        raise InvalidLLMResponseError(
            "评分响应必须是 JSON 文本或对象。"
        )
    try:
        payload = json.loads(raw, parse_constant=_reject_json_constant)
    except (ValueError, TypeError):
        raise InvalidLLMResponseError("评分响应不是合法 JSON 对象。") from None
    if not isinstance(payload, Mapping):
        raise InvalidLLMResponseError("评分响应必须是 JSON 对象。")
    return dict(payload)


def _describe_validation_error(error: ValidationError) -> str:
    """把 Pydantic 校验失败脱敏为「已知字段名 + 错误类型」。"""

    descriptions: list[str] = []
    for item in error.errors():
        loc = [
            str(part)
            for part in item.get("loc", ())
            if isinstance(part, str) and str(part) in KNOWN_PAYLOAD_FIELDS
        ]
        field = loc[0] if loc else "响应"
        descriptions.append(f"{field}：{item.get('type', 'invalid')}")
    return "；".join(sorted(set(descriptions))) or "响应不符合结构化契约"


def parse_subjective_payload(
    raw: Any,
    *,
    question_type: QuestionType | str | None,
    max_score: Any,
    knowledge_points: Sequence[str] = (),
    retrieved_context_ids: Sequence[str] = (),
    answer_id: str | None = None,
    submission_id: str | None = None,
) -> GradingResult:
    """把模型响应校验并转换为 :class:`GradingResult`。

    这是**已校验的中间结果**：``review_status`` 先回填 ``Not Required``，最终复核状态由入口的
    置信度检查决定，本函数不代表 ``Accepted``。
    """

    resolved_max_score = _resolve_max_score(max_score)
    normalized_type = normalize_question_type(question_type)
    if _SUBJECTIVE_ONLY_ROUTER.route_type(normalized_type) is not GradingMode.SUBJECTIVE:
        raise GradingModeMismatchError(
            f"题型 {normalized_type} 属于客观题路径，"
            "不得使用主观题结构化评分解析器。"
        )
    payload_map = _load_payload(raw)
    try:
        payload = SubjectiveGradingPayload.model_validate(payload_map)
    except ValidationError as exc:
        raise InvalidLLMResponseError(
            f"评分响应未通过结构化校验：{_describe_validation_error(exc)}。"
        ) from None

    if payload.score < 0 or payload.score > resolved_max_score:
        raise ScoreOutOfRangeError(
            f"得分 {payload.score} 超出 [0, {resolved_max_score}] 区间。"
        )
    try:
        quantized = Decimal(str(payload.score)).quantize(
            SCORE_QUANTUM, rounding=ROUND_HALF_UP
        )
    except InvalidOperation:  # pragma: no cover - 防御性分支
        raise ScoreOutOfRangeError("得分无法量化为两位小数。") from None
    if quantized < 0 or quantized > Decimal(str(resolved_max_score)):
        raise ScoreOutOfRangeError(
            f"得分 {payload.score} 量化后超出 [0, {resolved_max_score}] 区间，拒绝钳制。"
        )

    if not math.isfinite(payload.confidence) or not 0.0 <= payload.confidence <= 1.0:
        raise ConfidenceOutOfRangeError(
            f"置信度 {payload.confidence} 超出 [0, 1] 区间。"
        )

    return GradingResult(
        question_type=normalized_type,
        score=float(quantized),
        max_score=resolved_max_score,
        reason=payload.reason,
        correct_points=list(payload.correct_points),
        missing_knowledge_points=list(payload.missing_knowledge_points),
        knowledge_points=list(knowledge_points),
        suggestions=list(payload.suggestions),
        confidence=payload.confidence,
        validation_status=ValidationStatus.VALIDATED.value,
        review_status=ReviewStatus.NOT_REQUIRED.value,
        retrieved_context_ids=list(retrieved_context_ids),
        answer_id=answer_id,
        submission_id=submission_id,
    )


def _build_user_message(context: GradingContext, *, max_score: float) -> str:
    """构造用户消息：平台满分与 §5.3 的全部评分输入。

    评分标准与学生答案在 :class:`SubjectiveGradingSource` 构造阶段已保证非空白，
    因此这里不再提供「未作答」等占位文案，避免用占位内容代替真实作答。
    """

    source = context.source
    knowledge_points = "、".join(source.knowledge_points) or "（题目未标注知识点）"
    return "\n".join(
        [
            f"本题满分：{max_score}",
            "【题目】",
            source.question_content,
            "【标准答案】",
            source.reference_answer,
            "【评分标准】",
            str(source.scoring_rubric).strip(),
            "【学生答案】",
            source.student_answer_text,
            "【知识点】",
            knowledge_points,
            "【课程检索上下文】",
            context.final_context or "（无可用检索上下文）",
        ]
    )


def build_grading_messages(
    context: GradingContext,
    *,
    max_score: float,
) -> list[LLMMessage]:
    """构造评分消息：版本化系统提示 + 含五要素与 Final Context 的用户消息。"""

    return [
        {"role": "system", "content": _SUBJECTIVE_GRADING_SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_message(context, max_score=max_score)},
    ]


class SubjectiveGrader:
    """主观题评分编排：上下文组装 → 结构化生成 → 校验 → 置信度检查。

    :param provider: 评分 Provider；``None`` 表示在调用时通过既有工厂按配置解析，未就绪时
        显式失败，不在导入期实例化。
    :param policy: 置信度策略；``None`` 时使用 :class:`ConfidencePolicy`（读取
        ``AppSettings.confidence_threshold``）。入口返回前必执行置信度检查。
    :param on_provider_metadata: 可选的调用元数据接收方，只回传本次实际解析的 Provider
        来源，不在评分器实例缓存最后一次模型身份。
    """

    def __init__(
        self,
        *,
        provider: BaseLLMProvider | None = None,
        policy: ConfidencePolicyLike | None = None,
        on_provider_metadata: Callable[[LLMProviderMetadata], None] | None = None,
    ) -> None:
        self._provider = provider
        self._policy = policy
        self._on_provider_metadata = on_provider_metadata

    async def grade(
        self,
        session: Session,
        source: SubjectiveGradingSource,
        *,
        max_score: Any,
        top_k: int = DEFAULT_TOP_K,
        retriever: BaseRetriever | None = None,
        reranker: BaseReranker | None = None,
        embedding_provider: BaseEmbeddingProvider | None = None,
        require_context: bool = True,
        settings: AppSettings | None = None,
    ) -> GradingResult:
        """返回经过结构化校验与置信度检查的 :class:`GradingResult`。

        ``require_context`` 只影响低层上下文组装是否立即报错；正式评分入口**无论该参数取值
        如何都会强制校验上下文充分性**，绝不在空上下文下调用评分模型。
        """

        resolved_settings = settings if settings is not None else get_settings()
        resolved_max_score = _resolve_max_score(max_score)
        context = await build_grading_context(
            session,
            source,
            top_k=top_k,
            retriever=retriever,
            reranker=reranker,
            embedding_provider=embedding_provider,
            require_context=require_context,
            settings=settings,
        )
        context.ensure_sufficient()
        messages = build_grading_messages(context, max_score=resolved_max_score)
        payload = await self._generate_payload(messages, settings=resolved_settings)
        answer_id = (
            str(source.answer_id) if source.answer_id is not None else None
        )
        submission_id = (
            str(source.submission_id) if source.submission_id is not None else None
        )
        result = parse_subjective_payload(
            payload.model_dump(),
            question_type=source.question_type,
            max_score=resolved_max_score,
            knowledge_points=source.knowledge_points,
            retrieved_context_ids=context.retrieved_context_ids,
            answer_id=answer_id,
            submission_id=submission_id,
        )
        return self._apply_confidence_check(result, resolved_settings)

    async def _generate_payload(
        self,
        messages: LLMMessages,
        *,
        settings: AppSettings,
    ) -> SubjectiveGradingPayload:
        """调用 Provider 获取结构化评分 Payload，并把失败分类为业务错误码。"""

        provider = self._provider
        if provider is None:
            try:
                provider = create_llm_provider(settings)
            except Exception:  # noqa: BLE001 - 未就绪不得静默降级
                raise ProviderNotReadyError(
                    "评分 Provider 未就绪，请检查 LLM_PROVIDER 与模型配置。"
                ) from None
        try:
            result = await provider.generate_structured(
                messages, SubjectiveGradingPayload
            )
        except ProviderExecutionError as exc:
            info = exc.info
            code = str(info.code)
            failure_detail: dict[str, Any] = {
                "retryable": bool(info.retryable),
                "source_code": code,
                "status": str(info.status),
                "attempt_count": int(info.attempt_count),
            }
            if code in STRUCTURED_FAILURE_CODES:
                raise InvalidLLMResponseError(
                    f"评分响应未通过结构化校验（来源码 {code}，尝试 "
                    f"{info.attempt_count} 次）。",
                    **failure_detail,
                ) from None
            raise ProviderFailedError(
                f"评分 Provider 调用失败（来源码 {code}，尝试 "
                f"{info.attempt_count} 次）。",
                **failure_detail,
            ) from None
        except SubjectiveGradingError:
            raise
        except Exception:  # noqa: BLE001 - 统一收敛为脱敏 Provider 失败
            raise ProviderFailedError("评分 Provider 调用失败。") from None
        if not isinstance(result, SubjectiveGradingPayload):
            raise InvalidLLMResponseError("评分 Provider 未返回约定的结构化结果。")
        if self._on_provider_metadata is not None:
            self._on_provider_metadata(describe_llm_provider(
                provider, prompt_version=SUBJECTIVE_GRADING_PROMPT_VERSION,
            ))
        return result

    def _apply_confidence_check(
        self,
        result: GradingResult,
        settings: AppSettings,
    ) -> GradingResult:
        """置信度检查：未注入策略时使用读取运行配置的默认策略。

        入口返回前必执行本检查，否则低置信度结果会绕过 FR-035/FR-036 的人工复核。
        """

        policy = self._policy
        if policy is None:
            policy = ConfidencePolicy(settings=settings)
        return policy.apply(result)


__all__ = [
    "GRADING_CONFIDENCE_OUT_OF_RANGE",
    "GRADING_INVALID_LLM_RESPONSE",
    "GRADING_INVALID_MAX_SCORE",
    "GRADING_MISSING_CONTEXT",
    "GRADING_MODE_MISMATCH",
    "GRADING_PROVIDER_FAILED",
    "GRADING_PROVIDER_NOT_READY",
    "GRADING_SCORE_OUT_OF_RANGE",
    "KNOWN_PAYLOAD_FIELDS",
    "SCORE_QUANTUM",
    "STRUCTURED_FAILURE_CODES",
    "SUBJECTIVE_GRADING_PROMPT_VERSION",
    "ConfidenceOutOfRangeError",
    "ConfidencePolicyLike",
    "GradingModeMismatchError",
    "InsufficientContextError",
    "InvalidLLMResponseError",
    "InvalidMaxScoreError",
    "ProviderFailedError",
    "ProviderNotReadyError",
    "ReferenceAnswerMissingError",
    "ScoreOutOfRangeError",
    "SubjectiveGrader",
    "SubjectiveGradingError",
    "SubjectiveGradingPayload",
    "build_grading_messages",
    "parse_subjective_payload",
]

#: 从上下文模块重导出的错误码常量，便于评分入口单点导入。
GRADING_MISSING_CONTEXT = InsufficientContextError.error_code
GRADING_MODE_MISMATCH = GradingModeMismatchError.error_code
