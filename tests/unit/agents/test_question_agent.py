"""T067 Question Agent 检索与结构化候选生成的失败优先测试。

任务编号：T067（M4；实现见 `backend/app/ai/agents/question_agent.py`）。

必要性：`plan.md` §4.1 规定 AI 出题必须按“教师输入需求 → Query Construction → 知识库检索 →
Question Agent → Pydantic Schema → Question Validator → 教师审核 → 题库”执行，且无足够
上下文时不得静默编造；宪章 IV（结构化输出铁律）要求核心结果必须经 `generate_structured`
与 Pydantic 校验后才能进入业务层；FR-026 要求生成结果只能标记 `Candidate Generation`。
若 Agent 允许自由文本解析、以空上下文继续生成、把整批检索片段回填成候选来源，或让候选状态
直接变成可发布状态，则 T068 校验与教师审核都会失去意义。

覆盖内容：
1. Query Construction 保留课程、知识点、难度、题型与数量约束，且完整原始条件独立进入生成 Prompt；
2. 检索只经注入的（或按同一 `AppSettings` 解析的）Hybrid 检索，课程范围被强制约束，
   Embedding 走同一装配口径，且不嵌套 `asyncio.run`；
3. 上下文不足（零候选或零有效正文）即显式失败，不调用模型、不返回空成功；
4. LLM 调用只经 `generate_structured(messages, QuestionGenerationPayload)`，不解析自由文本；
5. 真实 `DeepSeekProvider` 遇到非法 JSON 或违反 Schema 的响应时映射为
   `QUESTION_INVALID_LLM_RESPONSE`；Provider 调用故障保留来源码与可重试语义；
6. 来源白名单为真正写入最终 Prompt 的片段：虚构、跨课程、被预算截断的引用一律拒绝，
   有效引用去重保序，缺失引用留空交由 T068 判定；
7. 候选数量与题型必须符合教师条件，候选状态必须保持 `Candidate Generation`（禁止自动发布）；
8. `model` 追踪字段只在 Provider 能提供标识时记录，不从全局配置猜测。

执行方法（先红后绿）：``python -m pytest tests/unit/agents -q``；实现前本文件因模块缺失而失败。
本仓库测试约定：异步入口在同步用例中用 ``asyncio.run`` 驱动，不启用 pytest-asyncio 标记。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Coroutine
from typing import Any
from uuid import UUID

from backend.app.ai.agents.question_agent import (
    QUESTION_CANDIDATE_COUNT_MISMATCH,
    QUESTION_GENERATION_PROMPT_VERSION,
    QUESTION_INSUFFICIENT_CONTEXT,
    QUESTION_INVALID_INPUT,
    QUESTION_INVALID_LLM_RESPONSE,
    QUESTION_MISSING_GENERATION_REQUEST,
    QUESTION_PROVIDER_FAILED,
    QUESTION_TYPE_MISMATCH,
    QUESTION_UNKNOWN_SOURCE_CONTEXT,
    QuestionAgent,
    QuestionGenerationContext,
    QuestionGenerationPayload,
    build_generation_context,
    build_generation_messages,
    build_generation_query,
    resolve_generation_retriever,
)
from backend.app.ai.agents.state import (
    AgentInput,
    AgentStatus,
    AgentType,
    QuestionGenerationRequest,
)
from backend.app.ai.llm.deepseek import DeepSeekProvider
from backend.app.ai.retrieval.base import RetrievalMode, RetrievalQuery, RetrievedChunk
from backend.app.ai.retrieval.hybrid_search import HybridSearchRetriever
from backend.app.core.retry_policy import (
    ProviderErrorInfo,
    ProviderExecutionError,
    RetryPolicy,
)
from backend.app.domain.enums import QuestionType, ValidationStatus
from tests.support.question_generation_doubles import (
    COURSE_UUID,
    StubEmbeddingProvider,
    StubQuestionProvider,
    StubRetriever,
    make_candidate,
    make_chunk,
)
from tests.unit.settings_helpers import build_test_settings


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """在同步用例中驱动异步入口；与仓库既有测试约定保持一致。"""

    return asyncio.run(coroutine)


def _request(**overrides: Any) -> QuestionGenerationRequest:
    """构造出题条件；默认单选题 1 道。"""

    payload: dict[str, Any] = {
        "course_id": COURSE_UUID,
        "knowledge_points": ["变量"],
        "difficulty": "中等",
        "question_type": QuestionType.SINGLE_CHOICE,
        "count": 1,
    }
    payload.update(overrides)
    return QuestionGenerationRequest(**payload)


def _input(**overrides: Any) -> AgentInput:
    """构造 Question Agent 输入信封；独立调用时 workflow_id 可以为空。"""

    payload: dict[str, Any] = {
        "agent_type": AgentType.QUESTION,
        "request_id": "request-question-1",
        "generation_request": _request(),
    }
    payload.update(overrides)
    return AgentInput(**payload)


def _generate(
    provider: StubQuestionProvider | DeepSeekProvider,
    *,
    agent_input: AgentInput | None = None,
    chunks: list[RetrievedChunk] | None = None,
    settings: Any | None = None,
) -> Any:
    """用替身检索与 Embedding 驱动一次生成。"""

    return _run(
        QuestionAgent(provider=provider).generate(
            None,  # type: ignore[arg-type] - 替身检索不需要真实会话
            agent_input if agent_input is not None else _input(),
            retriever=StubRetriever(chunks if chunks is not None else [make_chunk("chunk-1")]),
            embedding_provider=StubEmbeddingProvider(),
            settings=settings if settings is not None else build_test_settings(),
        )
    )


class _FakeMessage:
    """模拟 SDK 响应消息。"""

    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    """模拟 SDK 响应选项。"""

    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    """模拟 SDK 聊天补全响应。"""

    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    """记录请求并返回固定内容的补全接口替身。"""

    def __init__(self, content: str) -> None:
        self.content = content
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _FakeResponse:
        self.requests.append(kwargs)
        return _FakeResponse(self.content)


class _FakeChat:
    """模拟 SDK 聊天命名空间。"""

    def __init__(self, content: str) -> None:
        self.completions = _FakeCompletions(content)


class _FakeClient:
    """模拟 AsyncOpenAI 客户端；不发任何网络请求。"""

    def __init__(self, content: str) -> None:
        self.chat = _FakeChat(content)


def test_build_generation_query_keeps_teacher_constraints() -> None:
    """Query Construction 必须保留课程、知识点、难度、题型与数量。"""

    query = build_generation_query(
        _request(knowledge_points=["变量", "作用域"], difficulty="困难", count=3)
    )

    assert COURSE_UUID in query
    assert "变量" in query and "作用域" in query
    assert "困难" in query
    assert QuestionType.SINGLE_CHOICE.value in query
    assert "3" in query


def test_generation_query_is_bounded_but_conditions_are_not_lost() -> None:
    """检索查询有长度上限，但完整条件另行走 Prompt，不因截断而丢失。"""

    request = _request(knowledge_points=["知识点" * 400], difficulty="困难")
    query = build_generation_query(request)
    chunks = [make_chunk("chunk-1")]
    context = QuestionGenerationContext(
        request=request,
        query_text=query,
        retrieval_mode=RetrievalMode.HYBRID,
        chunks=tuple(chunks),
        retrieved_context_ids=("chunk-1",),
        final_context="[片段 1] chunk_id=chunk-1\n变量用于保存数据。",
        candidate_count=1,
    )

    assert len(query) <= 1200
    user_message = str(build_generation_messages(request, context)[1]["content"])
    assert request.knowledge_points[0] in user_message


def test_generation_messages_keep_version_and_citation_rule() -> None:
    """系统提示首行为版本标识并写入 Schema；用户消息要求只引用实际使用的片段。"""

    request = _request(knowledge_points=["变量", "作用域"], difficulty="困难")
    chunks = [make_chunk("chunk-1")]
    context = QuestionGenerationContext(
        request=request,
        query_text="查询文本",
        retrieval_mode=RetrievalMode.HYBRID,
        chunks=tuple(chunks),
        retrieved_context_ids=("chunk-1",),
        final_context="[片段 1] chunk_id=chunk-1\n变量用于保存数据。",
        candidate_count=1,
    )

    messages = build_generation_messages(request, context)

    assert messages[0]["role"] == "system"
    assert str(messages[0]["content"]).splitlines()[0] == QUESTION_GENERATION_PROMPT_VERSION
    assert "json" in str(messages[0]["content"]).lower()
    assert "source_context_ids" in str(messages[0]["content"])
    user_message = str(messages[1]["content"])
    assert "变量" in user_message and "作用域" in user_message
    assert "困难" in user_message
    assert "chunk-1" in user_message
    assert "source_context_ids" in user_message


def test_generation_context_forces_course_filter_and_uses_injected_components() -> None:
    """检索必须使用同一课程范围与注入的 Embedding/检索组件。"""

    embedding = StubEmbeddingProvider()
    retriever = StubRetriever([make_chunk("chunk-1")])

    context = _run(
        build_generation_context(
            None,  # type: ignore[arg-type] - 替身检索不需要真实会话
            _request(),
            retriever=retriever,
            embedding_provider=embedding,
            settings=build_test_settings(),
        )
    )

    call = retriever.calls[0]
    filters = call["filters"]
    assert filters.course_ids == (UUID(COURSE_UUID),)
    assert isinstance(call["query"], RetrievalQuery)
    assert call["query"].embedding == tuple(embedding.vector)
    assert embedding.queries == [call["query"].text]
    assert context.retrieved_context_ids == ("chunk-1",)
    assert context.candidate_count == 1


def test_empty_retrieval_fails_without_calling_model() -> None:
    """零候选即上下文不足：不得调用模型，也不得返回空成功结果。"""

    provider = StubQuestionProvider(candidates=[make_candidate()])
    output = _generate(provider, chunks=[])

    assert output.status is AgentStatus.FAILURE
    assert output.error is not None
    assert output.error.error_code == QUESTION_INSUFFICIENT_CONTEXT
    assert output.question_candidates == []
    assert provider.calls == []


def test_blank_chunk_content_is_not_a_valid_context() -> None:
    """零有效正文同样视为上下文不足，不得把空片段计入来源。"""

    output = _generate(
        StubQuestionProvider(),
        chunks=[make_chunk("chunk-1", content="   ")],
    )

    assert output.status is AgentStatus.FAILURE
    assert output.error is not None
    assert output.error.error_code == QUESTION_INSUFFICIENT_CONTEXT


def test_generate_uses_structured_output_and_keeps_candidate_status() -> None:
    """LLM 调用只经结构化输出，候选必须保持 Candidate Generation（禁止自动发布）。"""

    provider = StubQuestionProvider(candidates=[make_candidate()])
    output = _generate(provider)

    assert output.status is AgentStatus.SUCCESS
    assert output.validation_status is ValidationStatus.VALIDATED
    assert output.prompt_version == QUESTION_GENERATION_PROMPT_VERSION
    assert len(provider.calls) == 1
    assert provider.calls[0]["schema"] is QuestionGenerationPayload
    assert output.retrieved_context_ids == ["chunk-1"]
    assert [candidate.status for candidate in output.question_candidates] == [
        "Candidate Generation"
    ]
    assert output.supervisor_decision is None
    assert output.grading_result is None


def test_source_context_ids_keep_only_prompt_whitelist() -> None:
    """来源只保留真正写入 Prompt 的有效引用，去重保序且不被整批检索 ID 覆盖。"""

    chunks = [make_chunk("chunk-1"), make_chunk("chunk-2")]
    candidate = make_candidate(source_context_ids=["chunk-2", "chunk-1", "chunk-2"])
    output = _generate(
        StubQuestionProvider(candidates=[candidate]),
        chunks=chunks,
    )

    assert output.status is AgentStatus.SUCCESS
    assert output.question_candidates[0].source_context_ids == ["chunk-2", "chunk-1"]


def test_fabricated_source_citation_is_rejected() -> None:
    """虚构来源（不在 Prompt 白名单）必须显式失败，而不是静默改写。"""

    candidate = make_candidate(source_context_ids=["chunk-fabricated"])
    output = _generate(StubQuestionProvider(candidates=[candidate]))

    assert output.status is AgentStatus.FAILURE
    assert output.error is not None
    assert output.error.error_code == QUESTION_UNKNOWN_SOURCE_CONTEXT
    assert output.question_candidates == []


def test_truncated_out_chunk_citation_is_rejected() -> None:
    """被总预算截断而未写入 Prompt 的片段不算有效来源（避免自证循环）。"""

    chunks = [
        make_chunk(f"chunk-{index}", content="超长内容。" * 200) for index in range(1, 11)
    ]
    context = _run(
        build_generation_context(
            None,  # type: ignore[arg-type]
            _request(),
            top_k=10,
            candidate_k=10,
            retriever=StubRetriever(chunks),
            embedding_provider=StubEmbeddingProvider(),
            settings=build_test_settings(),
        )
    )
    assert "chunk-10" not in context.retrieved_context_ids
    assert len(context.retrieved_context_ids) < len(chunks)

    candidate = make_candidate(source_context_ids=["chunk-10"])
    output = _generate(StubQuestionProvider(candidates=[candidate]), chunks=chunks)

    assert output.status is AgentStatus.FAILURE
    assert output.error is not None
    assert output.error.error_code == QUESTION_UNKNOWN_SOURCE_CONTEXT


def test_empty_citation_survives_until_validator() -> None:
    """缺少引用时保留空来源，交由 T068 判定为 Needs Revision，不在 Agent 内伪造来源。"""

    candidate = make_candidate(source_context_ids=[])
    output = _generate(StubQuestionProvider(candidates=[candidate]))

    assert output.status is AgentStatus.SUCCESS
    assert output.question_candidates[0].source_context_ids == []


def test_candidate_count_mismatch_fails() -> None:
    """候选数量必须与教师要求一致，缺题或多题都显式失败。"""

    output = _generate(
        StubQuestionProvider(candidates=[make_candidate(), make_candidate()])
    )

    assert output.status is AgentStatus.FAILURE
    assert output.error is not None
    assert output.error.error_code == QUESTION_CANDIDATE_COUNT_MISMATCH


def test_question_type_mismatch_fails() -> None:
    """候选题型必须符合教师要求，避免出题条件被静默忽略。"""

    candidate = make_candidate(
        question_type=QuestionType.SHORT_ANSWER,
        options=None,
        reference_answer="变量用于保存数据。",
    )
    output = _generate(StubQuestionProvider(candidates=[candidate]))

    assert output.status is AgentStatus.FAILURE
    assert output.error is not None
    assert output.error.error_code == QUESTION_TYPE_MISMATCH


def test_missing_generation_request_fails() -> None:
    """缺少整个出题条件时不得以默认条件生成题目。"""

    output = _generate(
        StubQuestionProvider(),
        agent_input=_input(generation_request=None),
    )

    assert output.status is AgentStatus.FAILURE
    assert output.error is not None
    assert output.error.error_code == QUESTION_MISSING_GENERATION_REQUEST


def test_invalid_input_type_fails() -> None:
    """输入信封类型非法时返回脱敏失败，而不是抛出 Python 异常。"""

    output = _generate(
        StubQuestionProvider(),
        agent_input="not-an-agent-input",  # type: ignore[arg-type]
    )

    assert output.status is AgentStatus.FAILURE
    assert output.error is not None
    assert output.error.error_code == QUESTION_INVALID_INPUT


def test_provider_structured_failure_maps_to_invalid_response() -> None:
    """Provider 报结构化失败时映射为结构化失败码，并保留脱敏来源信息。"""

    provider = StubQuestionProvider(
        error=ProviderExecutionError(
            ProviderErrorInfo(
                code="StructuredOutputFailed",
                message="LLM Provider 返回未通过结构化校验。",
                attempt_count=2,
                retryable=True,
                status="StructuredOutputFailed",
            )
        )
    )
    output = _generate(provider)

    assert output.status is AgentStatus.FAILURE
    assert output.error is not None
    assert output.error.error_code == QUESTION_INVALID_LLM_RESPONSE
    assert output.error.source_code == "StructuredOutputFailed"
    assert output.error.attempt_count == 2


def test_provider_call_failure_keeps_retryable_semantics() -> None:
    """Provider 调用故障必须保留来源码与可重试语义，不静默降级。"""

    provider = StubQuestionProvider(
        error=ProviderExecutionError(
            ProviderErrorInfo(
                code="ProviderTimeout",
                message="LLM Provider 请求超时。",
                attempt_count=3,
                retryable=True,
                status="ProviderTimeout",
            )
        )
    )
    output = _generate(provider)

    assert output.status is AgentStatus.FAILURE
    assert output.error is not None
    assert output.error.error_code == QUESTION_PROVIDER_FAILED
    assert output.error.retryable is True
    assert output.error.source_code == "ProviderTimeout"
    assert output.error.attempt_count == 3


def test_real_provider_rejects_free_text_and_schema_violations() -> None:
    """真实 DeepSeekProvider 遇到非 JSON 或违反 Schema 的响应都必须映射为结构化失败。"""

    settings = build_test_settings()
    invalid_json = DeepSeekProvider(
        settings,
        client=_FakeClient("这不是 JSON"),
        retry_policy=RetryPolicy(max_retries=0),
    )
    output = _generate(invalid_json, settings=settings)
    assert output.status is AgentStatus.FAILURE
    assert output.error is not None
    assert output.error.error_code == QUESTION_INVALID_LLM_RESPONSE

    candidate_payload = make_candidate().model_dump(mode="json")
    candidate_payload["unexpected_field"] = 1
    violating = DeepSeekProvider(
        settings,
        client=_FakeClient(json.dumps({"candidates": [candidate_payload]})),
        retry_policy=RetryPolicy(max_retries=0),
    )
    output = _generate(violating, settings=settings)
    assert output.status is AgentStatus.FAILURE
    assert output.error is not None
    assert output.error.error_code == QUESTION_INVALID_LLM_RESPONSE

    accepted = DeepSeekProvider(
        settings,
        client=_FakeClient(
            json.dumps({"candidates": [make_candidate().model_dump(mode="json")]})
        ),
        retry_policy=RetryPolicy(max_retries=0),
    )
    output = _generate(accepted, settings=settings)
    assert output.status is AgentStatus.SUCCESS
    assert len(output.question_candidates) == 1


def test_model_field_only_recorded_when_provider_exposes_identifier() -> None:
    """`model` 只记录 Provider 实际提供的标识，注入替身无法提供时保持 None。"""

    output = _generate(StubQuestionProvider(candidates=[make_candidate()]))
    assert output.model is None

    output = _generate(
        StubQuestionProvider(candidates=[make_candidate()], model_name="stub-model-v1")
    )
    assert output.model == "stub-model-v1"


def test_retriever_resolution_uses_same_settings_and_prefers_injection() -> None:
    """同一 `AppSettings` 决定 Hybrid 权重与候选数；显式注入的检索实现优先。"""

    settings = build_test_settings(hybrid_vector_weight=0.25)
    resolved = resolve_generation_retriever(
        RetrievalMode.HYBRID,
        candidate_k=3,
        retriever=None,
        settings=settings,
    )

    assert isinstance(resolved, HybridSearchRetriever)
    assert resolved.vector_weight == 0.25
    assert resolved.candidate_k == 3

    injected = StubRetriever([make_chunk("chunk-1")])
    assert (
        resolve_generation_retriever(
            RetrievalMode.HYBRID,
            candidate_k=3,
            retriever=injected,
            settings=settings,
        )
        is injected
    )


def test_embedding_provider_factory_receives_same_settings(monkeypatch: Any) -> None:
    """未注入 Embedding 时必须用同一 `AppSettings` 装配，而不是读取全局配置。"""

    import backend.app.ai.agents.question_agent as module

    captured: list[Any] = []
    stub = StubEmbeddingProvider()

    def _record(settings: Any) -> StubEmbeddingProvider:
        captured.append(settings)
        return stub

    monkeypatch.setattr(module, "create_embedding_provider", _record)
    settings = build_test_settings(hybrid_vector_weight=0.4)
    _run(
        build_generation_context(
            None,  # type: ignore[arg-type]
            _request(),
            retriever=StubRetriever([make_chunk("chunk-1")]),
            settings=settings,
        )
    )

    assert captured == [settings]
