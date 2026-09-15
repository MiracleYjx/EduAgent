"""T051 结构化输出与分数范围失败优先测试（主观题评分）。

任务编号：T051（M3 AI 阅卷；实现见 T052）。
必要性：FR-034 要求主观题评分只有通过 Pydantic Validation 才能进入统一结果、成绩与诊断
（Constitution IV）；FR-035/FR-036 要求每个主观题结果都经过 Confidence Check。若评分器
假定 Provider 返回自由文本、把超范围分数静默修正、或把结构化失败并入普通调用失败，
错误结果就可能进入业务层或失去可复核性。
覆盖内容：
1. 合法 JSON 文本与 Mapping（含全对/全错时的合法空列表）→ 完整 `GradingResult`；
2. 非法 JSON、非对象、缺字段、多余字段、非数值、非有限值、空白理由/建议 → `GRADING_INVALID_LLM_RESPONSE`；
3. 分数范围与舍入顺序（先查原始范围，再 `ROUND_HALF_UP`，舍入后越界仍报错，不静默钳制）；
4. 置信度范围且不舍入；
5. 入口拒绝：客观题题型、缺少标准答案、非法满分（含 `Decimal` 异常与超大值）；
6. 上下文不足时不得调用评分 Provider；
7. Provider 返回类型与 `ProviderExecutionError` 分类映射（结构化失败 → 无效响应，调用故障 → Provider 失败）；
8. Prompt 合同：版本前缀、JSON 指令与 Schema、平台满分与五要素；不插入额外 system 消息；
9. 脱敏：公开异常只含已知字段名与错误类型，不回显响应正文、学生答案、未知字段名或 Pydantic 全文；
10. 默认置信度接线：未注入策略时低置信度结果仍进入 `Pending Review`。
执行方法（先红后绿）：``python -m pytest tests/unit/grading/test_structured_grading.py -q``；
仓库未安装 pytest-asyncio，异步用例沿用 `asyncio.run` 包装。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from decimal import Decimal
from functools import wraps
from typing import Any

import pytest
from pydantic import BaseModel

from backend.app.ai.embedding.base import BaseEmbeddingProvider
from backend.app.ai.llm.base import BaseLLMProvider, LLMMessages
from backend.app.ai.llm.deepseek import DeepSeekProvider
from backend.app.ai.retrieval.base import (
    DEFAULT_TOP_K,
    BaseRetriever,
    RetrievalFilters,
    RetrievalMode,
    RetrievedChunk,
)
from backend.app.ai.retrieval.reranker import BaseReranker
from backend.app.core.retry_policy import ProviderErrorInfo, ProviderExecutionError
from backend.app.domain.enums import QuestionType
from backend.app.schemas.ai import GradingResult
from backend.app.services.grading.grading_context import (
    GradingContext,
    SubjectiveGradingSource,
)
from backend.app.services.grading.subjective_grader import (
    GRADING_CONFIDENCE_OUT_OF_RANGE,
    GRADING_INVALID_LLM_RESPONSE,
    GRADING_INVALID_MAX_SCORE,
    GRADING_MISSING_CONTEXT,
    GRADING_MODE_MISMATCH,
    GRADING_PROVIDER_FAILED,
    GRADING_SCORE_OUT_OF_RANGE,
    SUBJECTIVE_GRADING_PROMPT_VERSION,
    ConfidenceOutOfRangeError,
    GradingModeMismatchError,
    InsufficientContextError,
    InvalidLLMResponseError,
    InvalidMaxScoreError,
    ProviderFailedError,
    ReferenceAnswerMissingError,
    ScoreOutOfRangeError,
    SubjectiveGrader,
    SubjectiveGradingError,
    SubjectiveGradingPayload,
    build_grading_messages,
    parse_subjective_payload,
)
from tests.unit.settings_helpers import build_test_settings

COURSE_ID = "3f1a8c2e-0d4f-4b7a-9c1e-2b8d5f6a7c90"
SESSION = object()
MAX_SCORE = 10.0
SENSITIVE_TEXT = "学生答案敏感串-ZZ9"


def async_test(function: Any) -> Any:
    """在未安装 pytest-asyncio 的仓库里运行异步用例。"""

    @wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(function(*args, **kwargs))

    return wrapper


def _payload(**overrides: Any) -> dict[str, Any]:
    """构造一份合法的结构化评分响应用于覆盖单点差异。"""

    values: dict[str, Any] = {
        "score": 7.0,
        "confidence": 0.9,
        "reason": "学生答出局部作用域，未覆盖全局作用域。",
        "correct_points": ["局部作用域"],
        "missing_knowledge_points": ["全局作用域"],
        "suggestions": ["补充全局作用域的作用范围。"],
    }
    values.update(overrides)
    return values


def _parse(raw: Any, **overrides: Any) -> GradingResult:
    """以默认平台参数调用解析器。"""

    params: dict[str, Any] = {
        "question_type": QuestionType.SHORT_ANSWER,
        "max_score": MAX_SCORE,
        "knowledge_points": ("变量作用域",),
        "retrieved_context_ids": ("chunk-1",),
        "answer_id": "3f1a8c2e-0d4f-4b7a-9c1e-2b8d5f6a7c91",
        "submission_id": "3f1a8c2e-0d4f-4b7a-9c1e-2b8d5f6a7c92",
    }
    params.update(overrides)
    return parse_subjective_payload(raw, **params)


def _chunk(chunk_id: str, *, content: str | None = None) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        course_id=COURSE_ID,
        document_id=f"document-{chunk_id}",
        content=content if content is not None else f"{chunk_id} 的课程片段内容。",
        metadata={"chunk_index": 0, "location": "第 1 段"},
        fusion_score=1.0,
        rank=0,
        source_mode="both",
    )


class StubEmbeddingProvider(BaseEmbeddingProvider):
    provider_name = "stub"
    model_name = "stub-model"
    dimension = 3

    async def embed_documents(self, documents: Sequence[str]) -> list[list[float]]:
        raise NotImplementedError("替身不提供文档向量。")

    async def embed_query(self, query: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class StubRetriever(BaseRetriever):
    mode = RetrievalMode.HYBRID

    def __init__(self, hits: Sequence[RetrievedChunk] = ()) -> None:
        self._hits = list(hits)
        self.calls = 0

    def search(
        self,
        session: Any,
        query: Any,
        *,
        top_k: int = DEFAULT_TOP_K,
        filters: RetrievalFilters | None = None,
    ) -> list[RetrievedChunk]:
        self.calls += 1
        return list(self._hits)


class StubReranker(BaseReranker):
    """重排替身：按已有分数排序并在截断，不调用任何 Provider。"""

    provider_name = "stub"

    def __init__(self) -> None:
        self.calls = 0

    def rerank(
        self,
        query: str,
        candidates: Sequence[RetrievedChunk],
        top_k: int = DEFAULT_TOP_K,
    ) -> list[RetrievedChunk]:
        self.calls += 1
        ordered = sorted(candidates, key=lambda item: item.score, reverse=True)
        return [
            replace(chunk, rank=index)
            for index, chunk in enumerate(ordered[:top_k])
        ]


class OtherPayload(BaseModel):
    """与被测 Schema 不同的模型，用于验证返回类型校核。"""

    value: int = 0


class FakeGradingProvider(BaseLLMProvider):
    """假评分 Provider：记录消息并按预设结果返回或抛出异常。"""

    provider_name = "fake"

    def __init__(self, result: Any) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    async def generate_structured(
        self,
        messages: LLMMessages,
        schema: type[Any],
        model: str | None = None,
    ) -> Any:
        self.calls.append({"messages": list(messages), "schema": schema})
        if isinstance(self._result, BaseException):
            raise self._result
        if isinstance(self._result, Mapping):
            return schema.model_validate(dict(self._result))
        return self._result


def _source(**overrides: Any) -> SubjectiveGradingSource:
    values: dict[str, Any] = {
        "question_type": QuestionType.SHORT_ANSWER,
        "course_id": COURSE_ID,
        "question_content": "请说明 Python 中变量作用域的区别。",
        "reference_answer": "局部作用域只在函数内可见，全局作用域在整个模块可见。",
        "student_answer": SENSITIVE_TEXT,
        "scoring_rubric": "答出局部与全局两点各得一半分值。",
        "knowledge_points": ("变量作用域",),
    }
    values.update(overrides)
    return SubjectiveGradingSource(**values)


def _grade(
    provider: Any,
    *,
    hits: Sequence[RetrievedChunk] | None = None,
    max_score: Any = MAX_SCORE,
    settings: Any = None,
    policy: Any = None,
    grader: Any = None,
) -> GradingResult:
    """同步包装入口调用。"""

    active = grader or SubjectiveGrader(provider=provider, policy=policy)
    return asyncio.run(
        active.grade(
            SESSION,
            _source(),
            max_score=max_score,
            retriever=StubRetriever(hits if hits is not None else [_chunk("chunk-1")]),
            reranker=StubReranker(),
            embedding_provider=StubEmbeddingProvider(),
            settings=settings
            if settings is not None
            else build_test_settings(confidence_threshold=0.5),
        )
    )


# --- 合法响应 ---


def test_valid_json_payload_returns_validated_result() -> None:
    """合法 JSON 文本经平台复核后返回完整 DTO，平台字段由平台回填。"""

    result = _parse(json.dumps(_payload(), ensure_ascii=False))

    assert isinstance(result, GradingResult)
    assert result.question_type is QuestionType.SHORT_ANSWER
    assert result.score == 7.0
    assert result.max_score == MAX_SCORE
    assert result.confidence == 0.9
    assert result.knowledge_points == ["变量作用域"]
    assert result.retrieved_context_ids == ["chunk-1"]
    assert result.validation_status == "Validated"
    assert result.review_status == "Not Required"
    assert result.answer_id is not None
    assert result.submission_id is not None


def test_valid_mapping_allows_empty_point_lists() -> None:
    """全对或全错时要点列表允许为空。"""

    result = _parse(
        _payload(
            score=MAX_SCORE,
            correct_points=[],
            missing_knowledge_points=[],
        )
    )

    assert result.score == MAX_SCORE
    assert result.correct_points == []
    assert result.missing_knowledge_points == []


# --- 无效响应 ---


@pytest.mark.parametrize(
    "raw",
    [
        "{不是 JSON",
        "[]",
        '"纯文本"',
        "null",
        "123",
        json.dumps({**_payload(), "extra_field": "多余字段"}, ensure_ascii=False),
        json.dumps({key: value for key, value in _payload().items() if key != "reason"}),
        json.dumps({**_payload(), "score": "7.0"}, ensure_ascii=False),
        json.dumps({**_payload(), "score": True}, ensure_ascii=False),
        json.dumps({**_payload(), "confidence": "0.9"}, ensure_ascii=False),
        json.dumps({**_payload(), "confidence": False}, ensure_ascii=False),
        json.dumps({**_payload(), "reason": "   "}, ensure_ascii=False),
        json.dumps({**_payload(), "suggestions": []}, ensure_ascii=False),
        json.dumps({**_payload(), "suggestions": ["   "]}, ensure_ascii=False),
        json.dumps({**_payload(), "correct_points": ["  "]}, ensure_ascii=False),
        json.dumps({**_payload(), "missing_knowledge_points": [""]}, ensure_ascii=False),
        (
            '{"score": NaN, "confidence": 0.5, "reason": "r", '
            '"correct_points": [], "missing_knowledge_points": [], "suggestions": ["s"]}'
        ),
        (
            '{"score": Infinity, "confidence": 0.5, "reason": "r", '
            '"correct_points": [], "missing_knowledge_points": [], "suggestions": ["s"]}'
        ),
    ],
)
def test_invalid_payload_maps_to_invalid_response(raw: str) -> None:
    """非法 JSON 与未通过 Schema 校验的响应统一为结构化失败。"""

    with pytest.raises(InvalidLLMResponseError) as excinfo:
        _parse(raw)

    assert excinfo.value.error_code == GRADING_INVALID_LLM_RESPONSE
    assert isinstance(excinfo.value, SubjectiveGradingError)


@pytest.mark.parametrize(
    "overrides",
    [
        {"score": -0.001},
        {"score": MAX_SCORE + 0.004},
        {"score": -1},
        {"score": MAX_SCORE + 5},
    ],
)
def test_score_out_of_range_is_rejected_before_rounding(
    overrides: dict[str, Any],
) -> None:
    """分数范围在舍入前检查，不得把超范围值舍入到范围内。"""

    with pytest.raises(ScoreOutOfRangeError) as excinfo:
        _parse(_payload(**overrides))

    assert excinfo.value.error_code == GRADING_SCORE_OUT_OF_RANGE


@pytest.mark.parametrize(
    ("score", "expected"),
    [(7.335, 7.34), (7.334, 7.33), (0.005, 0.01), (MAX_SCORE, MAX_SCORE)],
)
def test_score_is_rounded_half_up_to_two_decimals(
    score: float,
    expected: float,
) -> None:
    """合法分数按 `ROUND_HALF_UP` 规整到两位小数，对齐 Numeric(8, 2)。"""

    result = _parse(_payload(score=score))

    assert result.score == expected


def test_rounding_may_not_exceed_max_score() -> None:
    """舍入后超出满分时必须显式失败，不得静默钳制。"""

    with pytest.raises(ScoreOutOfRangeError):
        _parse(_payload(score=0.005), max_score=0.005)


@pytest.mark.parametrize("confidence", [-0.01, 1.01, 2.0])
def test_confidence_out_of_range_is_rejected(confidence: float) -> None:
    """置信度必须落在 [0, 1]。"""

    with pytest.raises(ConfidenceOutOfRangeError) as excinfo:
        _parse(_payload(confidence=confidence))

    assert excinfo.value.error_code == GRADING_CONFIDENCE_OUT_OF_RANGE


def test_confidence_is_not_rounded() -> None:
    """置信度不做四舍五入，保留模型给出的数值。"""

    result = _parse(_payload(confidence=0.1234567))

    assert result.confidence == 0.1234567


@pytest.mark.parametrize(
    "max_score",
    [0, -1, float("nan"), float("inf"), "10", True, Decimal("sNaN"), 10**400],
)
def test_invalid_max_score_is_rejected(max_score: Any) -> None:
    """满分必须为有限正数；非法值不得泄漏底层异常类型。"""

    with pytest.raises(InvalidMaxScoreError) as excinfo:
        _parse(_payload(), max_score=max_score)

    assert excinfo.value.error_code == GRADING_INVALID_MAX_SCORE
    assert not isinstance(excinfo.value, (ValueError, OverflowError))


# --- 入口行为 ---


def test_entry_rejects_objective_question_type() -> None:
    """客观题不得进入主观题评分链路，且不得调用评分 Provider。"""

    provider = FakeGradingProvider(_payload())

    with pytest.raises(GradingModeMismatchError) as excinfo:
        _source(question_type=QuestionType.SINGLE_CHOICE)

    assert excinfo.value.error_code == GRADING_MODE_MISMATCH
    assert provider.calls == []


def test_entry_rejects_missing_reference_answer() -> None:
    """标准答案缺失时无法组装评分输入，入口前即显式失败。"""

    provider = FakeGradingProvider(_payload())

    with pytest.raises(ReferenceAnswerMissingError):
        _source(reference_answer="   ")

    assert provider.calls == []


def test_entry_rejects_invalid_max_score() -> None:
    """入口对非法满分必须先失败再调用 Provider。"""

    provider = FakeGradingProvider(_payload())

    with pytest.raises(InvalidMaxScoreError):
        _grade(provider, max_score=0)

    assert provider.calls == []


def test_entry_without_context_does_not_call_provider() -> None:
    """上下文不足时必须显式失败，且不得调用评分 Provider。"""

    provider = FakeGradingProvider(_payload())

    with pytest.raises(InsufficientContextError) as excinfo:
        _grade(provider, hits=[])

    assert excinfo.value.error_code == GRADING_MISSING_CONTEXT
    assert provider.calls == []


def test_entry_rejects_empty_context_even_when_require_context_false() -> None:
    """`require_context=False` 仅供低层组装使用，正式入口仍须拒绝空上下文。"""

    provider = FakeGradingProvider(_payload())
    grader = SubjectiveGrader(provider=provider)

    with pytest.raises(InsufficientContextError) as excinfo:
        asyncio.run(
            grader.grade(
                SESSION,
                _source(),
                max_score=MAX_SCORE,
                retriever=StubRetriever([]),
                reranker=StubReranker(),
                embedding_provider=StubEmbeddingProvider(),
                require_context=False,
                settings=build_test_settings(confidence_threshold=0.5),
            )
        )

    assert excinfo.value.error_code == GRADING_MISSING_CONTEXT
    assert provider.calls == []


def test_entry_returns_validated_result_and_passes_message_contract() -> None:
    """完整入口：记录请求消息、返回 Validated 结果并按阈值回填复核状态。"""

    provider = FakeGradingProvider(_payload(confidence=0.9))

    result = _grade(provider)

    assert result.validation_status == "Validated"
    assert result.review_status == "Not Required"
    assert result.retrieved_context_ids == ["chunk-1"]
    assert len(provider.calls) == 1
    messages = provider.calls[0]["messages"]
    system_content = str(messages[0]["content"])
    user_content = str(messages[1]["content"])
    assert system_content.startswith(SUBJECTIVE_GRADING_PROMPT_VERSION)
    assert "JSON" in system_content
    assert "confidence" in system_content
    assert '"reason"' in system_content
    assert str(MAX_SCORE) in user_content
    assert "局部作用域只在函数内可见" in user_content
    assert "答出局部与全局两点各得一半分值。" in user_content
    assert SENSITIVE_TEXT in user_content
    assert "chunk_id=chunk-1" in user_content


def test_default_policy_marks_low_confidence_for_review() -> None:
    """未注入策略时，低置信度结果仍必须进入待人工复核。"""

    provider = FakeGradingProvider(_payload(confidence=0.3))

    result = _grade(
        provider,
        settings=build_test_settings(confidence_threshold=0.5),
    )

    assert result.review_status == "Pending Review"
    assert result.validation_status == "Validated"


def test_high_confidence_is_auto_accepted_with_custom_threshold() -> None:
    """阈值可配置：达到阈值即自动接受。"""

    provider = FakeGradingProvider(_payload(confidence=0.6))

    result = _grade(
        provider,
        settings=build_test_settings(confidence_threshold=0.5),
    )

    assert result.review_status == "Not Required"


def test_prepare_messages_keeps_versioned_prefix_as_first_system_message() -> None:
    """系统提示已含 JSON 合同，Provider 不再插入额外的 system 消息。"""

    provider = FakeGradingProvider(_payload())
    _grade(provider)

    messages = provider.calls[0]["messages"]
    prepared = DeepSeekProvider._prepare_messages(messages)

    assert len(prepared) == len(messages)
    assert prepared[0]["role"] == "system"


# --- Provider 分类与脱敏 ---


def test_provider_returning_other_schema_is_invalid_response() -> None:
    """Provider 返回其它模型对象时必须判为无效结构化响应。"""

    provider = FakeGradingProvider(OtherPayload(value=1))

    with pytest.raises(InvalidLLMResponseError):
        _grade(provider)


@pytest.mark.parametrize(
    "code",
    ["StructuredOutputFailed", "ProviderEmptyResponse"],
)
def test_structured_provider_failure_maps_to_invalid_response(code: str) -> None:
    """结构化失败与空响应属于无效响应，保留脱敏来源码。"""

    provider = FakeGradingProvider(
        ProviderExecutionError(
            ProviderErrorInfo(code=code, message="脱敏后的 Provider 失败。", attempt_count=2)
        )
    )

    with pytest.raises(InvalidLLMResponseError) as excinfo:
        _grade(provider)

    assert excinfo.value.error_code == GRADING_INVALID_LLM_RESPONSE
    assert code in str(excinfo.value)


@pytest.mark.parametrize("code", ["ProviderTimeout", "ProviderRateLimited", "ProviderFailed"])
def test_provider_call_failure_maps_to_provider_failed(code: str) -> None:
    """调用故障（超时、限流、服务失败）属于 Provider 失败。"""

    provider = FakeGradingProvider(
        ProviderExecutionError(
            ProviderErrorInfo(code=code, message="脱敏后的 Provider 失败。", attempt_count=3)
        )
    )

    with pytest.raises(ProviderFailedError) as excinfo:
        _grade(provider)

    assert excinfo.value.error_code == GRADING_PROVIDER_FAILED
    assert code in str(excinfo.value)


@pytest.mark.parametrize("retryable", [True, False])
def test_provider_failure_preserves_retryable_flag(retryable: bool) -> None:
    """Provider 失败映射必须保真传递可重试标记与来源元数据。"""

    provider = FakeGradingProvider(
        ProviderExecutionError(
            ProviderErrorInfo(
                code="ProviderFailed",
                message="脱敏后的 Provider 失败。",
                attempt_count=3,
                retryable=retryable,
                status="ProviderFailed",
            )
        )
    )

    with pytest.raises(ProviderFailedError) as excinfo:
        _grade(provider)

    assert excinfo.value.error_code == GRADING_PROVIDER_FAILED
    assert excinfo.value.retryable is retryable
    assert excinfo.value.source_code == "ProviderFailed"
    assert excinfo.value.attempt_count == 3
    assert excinfo.value.status == "ProviderFailed"


def test_structured_failure_preserves_source_metadata() -> None:
    """结构化失败同样保留来源码、尝试次数与可重试标记。"""

    provider = FakeGradingProvider(
        ProviderExecutionError(
            ProviderErrorInfo(
                code="StructuredOutputFailed",
                message="脱敏后的结构化失败。",
                attempt_count=2,
                retryable=True,
                status="StructuredOutputFailed",
            )
        )
    )

    with pytest.raises(InvalidLLMResponseError) as excinfo:
        _grade(provider)

    assert excinfo.value.retryable is True
    assert excinfo.value.source_code == "StructuredOutputFailed"
    assert excinfo.value.attempt_count == 2


@pytest.mark.parametrize(
    "question_type",
    [QuestionType.SINGLE_CHOICE, QuestionType.TRUE_FALSE, "MULTIPLE_CHOICE"],
)
def test_parse_rejects_objective_question_type(
    question_type: QuestionType | str,
) -> None:
    """解析器直接调用时也不得为客观题生成主观题结果。"""

    with pytest.raises(GradingModeMismatchError) as excinfo:
        _parse(_payload(), question_type=question_type)

    assert excinfo.value.error_code == GRADING_MODE_MISMATCH


def test_invalid_response_error_is_redacted() -> None:
    """公开异常只暴露已知字段名与错误类型，不回显响应正文或未知字段名。"""

    raw = json.dumps(
        {
            "score": SENSITIVE_TEXT,
            "confidence": 0.5,
            "reason": SENSITIVE_TEXT,
            "correct_points": [SENSITIVE_TEXT],
            "missing_knowledge_points": [],
            "suggestions": [SENSITIVE_TEXT],
            "secret_key": SENSITIVE_TEXT,
        },
        ensure_ascii=False,
    )

    with pytest.raises(InvalidLLMResponseError) as excinfo:
        _parse(raw)

    message = f"{excinfo.value}\n{excinfo.value.detail}"
    assert SENSITIVE_TEXT not in message
    assert "secret_key" not in message
    assert "ValidationError" not in message
    assert "extra_forbidden" in message or "score" in message


def test_prompt_schema_matches_payload_contract() -> None:
    """Prompt 中的 Schema 与用户消息覆盖 Payload 合同与五要素。"""

    context = GradingContext(
        source=_source(),
        query_text="查询文本",
        retrieval_mode=RetrievalMode.HYBRID_RERANK,
        filters=RetrievalFilters(),
        chunks=(_chunk("chunk-1"),),
        retrieved_context_ids=("chunk-1",),
        final_context="[片段 1] chunk_id=chunk-1\n课程片段正文",
        candidate_count=1,
    )

    messages = build_grading_messages(context, max_score=MAX_SCORE)

    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    schema_json = json.dumps(
        SubjectiveGradingPayload.model_json_schema(), ensure_ascii=False
    )
    assert "score" in schema_json and "confidence" in schema_json
    for field in ("reason", "correct_points", "missing_knowledge_points", "suggestions"):
        assert field in schema_json
