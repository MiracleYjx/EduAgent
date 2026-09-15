"""T050 主观题 Query Construction 与检索上下文组装失败优先测试。

任务编号：T050（M3 AI 阅卷；实现见 `backend/app/services/grading/grading_context.py`）。
必要性：主观题评分必须按 FR-031/FR-032 与 plan.md §5.3 组装「题目、标准答案、评分标准、
学生答案、课程知识库检索结果」，且检索只能经 M2 的 `get_retriever`/`build_reranker` 抽象完成；
若 query 截断吞掉学生答案、Final Context 与实际溯源 ID 不一致、空上下文被当作充分依据，
或异步路径误用同步重排入口（已核实在事件循环内会报错），评分就会失去依据或直接失败。
覆盖内容：
1. 输入契约：题型/知识点纳入 Source，UUID 归一为文本，列表答案不 `str()` 化，
   错误优先级为 题型 → 标准答案缺失 → 其它必填；
2. Query Construction：固定拼接顺序、字段预算、学生答案预算非零、截断标记计入上限、
   截断不覆盖原始输入；
3. Final Context 与溯源：只记录实际写入片段且保持顺序，保留 course/document/metadata/重排分数；
4. 充分性：空白正文或零候选一律判不足，`require_context=True` 时显式失败；
5. 逐模式调用合同：Hybrid 收 `RetrievalQuery`、Vector 只收向量、Keyword 只收文本且不调用
   Embedding；默认候选数来自配置；
6. 真实 M2 Rerank 适配器（`LLMRerankAdapter` + 假 Provider）在事件循环内的异步用例；
7. 检索层异常直接传播，不静默降级。
执行方法（先红后绿）：``python -m pytest tests/unit/grading/test_grading_context.py -q``；
仓库未安装 pytest-asyncio，异步用例沿用 `asyncio.run` 包装。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from functools import wraps
from typing import Any
from uuid import UUID, uuid4

import pytest

from backend.app.ai.embedding.base import BaseEmbeddingProvider
from backend.app.ai.llm.base import BaseLLMProvider, LLMMessages
from backend.app.ai.retrieval.base import (
    DEFAULT_TOP_K,
    BaseRetriever,
    RetrievalFilters,
    RetrievalMode,
    RetrievalQuery,
    RetrievalUnsupportedDialectError,
    RetrievedChunk,
)
from backend.app.ai.retrieval.reranker import (
    LLMRerankAdapter,
    LLMRerankResponse,
    RerankFailedError,
)
from backend.app.domain.enums import QuestionType
from backend.app.services.grading import grading_context as grading_context_module
from backend.app.services.grading.grading_context import (
    GRADING_INVALID_INPUT,
    GRADING_MISSING_CONTEXT,
    GRADING_MISSING_REFERENCE_ANSWER,
    GRADING_MODE_MISMATCH,
    MAX_QUERY_CHARS,
    TRUNCATION_MARKER,
    GradingContextError,
    GradingInputError,
    GradingModeMismatchError,
    InsufficientContextError,
    ReferenceAnswerMissingError,
    SubjectiveGradingSource,
    build_grading_context,
    build_query_text,
)
from backend.app.services.grading.question_router import (
    QUESTION_TYPE_MISSING,
    QUESTION_TYPE_UNKNOWN,
    MissingQuestionTypeError,
    UnknownQuestionTypeError,
)
from tests.unit.settings_helpers import build_test_settings

#: 检索过滤条件要求 UUID；测试使用固定合法 UUID 以覆盖真实过滤链路。
COURSE_ID = "3f1a8c2e-0d4f-4b7a-9c1e-2b8d5f6a7c90"
KNOWLEDGE_BASE_ID = "8c7d6e5f-4a3b-4c2d-9e1f-0a1b2c3d4e5f"
OTHER_COURSE_ID = "1b2c3d4e-5f60-4718-9a2b-3c4d5e6f7081"
OTHER_KNOWLEDGE_BASE_ID = "2c3d4e5f-6071-4829-8b3c-4d5e6f708192"
DOCUMENT_ID = "3d4e5f60-7182-493a-9c4d-5e6f708192a3"
EMBEDDING_VECTOR = (0.1, 0.2, 0.3)
_SESSION = object()


def async_test(function: Any) -> Any:
    """在未安装 pytest-asyncio 的仓库里运行异步用例。"""

    @wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(function(*args, **kwargs))

    return wrapper


class StubEmbeddingProvider(BaseEmbeddingProvider):
    """记录查询向量调用的 Embedding 替身；可返回向量或抛出异常。"""

    provider_name = "stub"
    model_name = "stub-model"
    dimension = len(EMBEDDING_VECTOR)

    def __init__(self, error: Exception | None = None) -> None:
        self.queries: list[str] = []
        self._error = error

    async def embed_documents(self, documents: Sequence[str]) -> list[list[float]]:
        """测试不需要文档向量；显式失败避免被误用。"""

        raise NotImplementedError("替身不提供文档向量。")

    async def embed_query(self, query: str) -> list[float]:
        self.queries.append(query)
        if self._error is not None:
            raise self._error
        return list(EMBEDDING_VECTOR)


class StubRetriever(BaseRetriever):
    """记录调用参数的检索替身；不访问数据库。"""

    mode = RetrievalMode.HYBRID

    def __init__(
        self,
        hits: Sequence[RetrievedChunk] = (),
        error: Exception | None = None,
        mode: RetrievalMode = RetrievalMode.HYBRID,
    ) -> None:
        self.mode = mode
        self._hits = list(hits)
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def search(
        self,
        session: Any,
        query: Any,
        *,
        top_k: int = DEFAULT_TOP_K,
        filters: RetrievalFilters | None = None,
    ) -> list[RetrievedChunk]:
        self.calls.append({"query": query, "top_k": top_k, "filters": filters})
        if self._error is not None:
            raise self._error
        return list(self._hits)


class FakeRerankProvider(BaseLLMProvider):
    """假 LLM Provider：记录消息并按给定结构化结果返回。"""

    provider_name = "fake"

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.calls: list[dict[str, Any]] = []

    async def generate_structured(
        self,
        messages: LLMMessages,
        schema: type[Any],
        model: str | None = None,
    ) -> Any:
        self.calls.append({"messages": list(messages), "schema": schema, "model": model})
        return schema.model_validate(self._payload)


def _chunk(
    chunk_id: str,
    *,
    content: str | None = None,
    fusion_score: float | None = 1.0,
    rerank_score: float | None = None,
    rank: int = 0,
) -> RetrievedChunk:
    """构造一条带来源信息的检索候选。"""

    return RetrievedChunk(
        chunk_id=chunk_id,
        course_id=COURSE_ID,
        document_id=f"document-{chunk_id}",
        content=content if content is not None else f"{chunk_id} 的课程片段内容。",
        metadata={"chunk_index": 0, "location": "第 1 段"},
        fusion_score=fusion_score,
        rerank_score=rerank_score,
        rank=rank,
        source_mode="both",
    )


def _source(**overrides: Any) -> SubjectiveGradingSource:
    """构造主观题评分输入；默认值是合法的完整输入集合。"""

    values: dict[str, Any] = {
        "question_type": QuestionType.SHORT_ANSWER,
        "course_id": COURSE_ID,
        "question_content": "请说明 Python 中变量作用域的区别。",
        "reference_answer": "局部作用域只在函数内可见，全局作用域在整个模块可见。",
        "student_answer": "变量在函数内部定义时只在函数内可见。",
        "scoring_rubric": "答出局部与全局两点各得一半分值。",
        "knowledge_points": ("变量作用域",),
    }
    values.update(overrides)
    return SubjectiveGradingSource(**values)


# --- 输入契约 ---


def test_source_normalizes_uuid_type_and_list_answer() -> None:
    """UUID 归一为文本，列表答案用换行连接，题型归一为枚举。"""

    course_id = uuid4()
    source = _source(
        course_id=course_id,
        question_type="short_answer",
        student_answer=["第一点", "第二点"],
        question_id=uuid4(),
    )

    assert source.course_id == str(course_id)
    assert source.question_type is QuestionType.SHORT_ANSWER
    assert source.student_answer_text == "第一点\n第二点"
    assert source.question_id is not None
    assert len(source.question_id) == 36


def test_source_rejects_unknown_and_missing_question_type() -> None:
    """题型未知或缺失沿用 Router 错误码，不得推断或默认。"""

    with pytest.raises(UnknownQuestionTypeError) as unknown:
        _source(question_type="MATCHING")
    assert unknown.value.error_code == QUESTION_TYPE_UNKNOWN

    with pytest.raises(MissingQuestionTypeError) as missing:
        _source(question_type=None)
    assert missing.value.error_code == QUESTION_TYPE_MISSING


def test_source_rejects_objective_question_type() -> None:
    """客观题不得进入主观题上下文组装。"""

    with pytest.raises(GradingModeMismatchError) as excinfo:
        _source(question_type=QuestionType.SINGLE_CHOICE)

    assert excinfo.value.error_code == GRADING_MODE_MISMATCH


def test_missing_reference_answer_takes_priority_over_other_inputs() -> None:
    """标准答案缺失使用专用错误码，优先于其它必填项错误。"""

    with pytest.raises(ReferenceAnswerMissingError) as excinfo:
        _source(reference_answer="   ", question_content="", course_id=None)

    assert excinfo.value.error_code == GRADING_MISSING_REFERENCE_ANSWER
    assert excinfo.value.retryable is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"course_id": None},
        {"question_content": ""},
        {"question_content": "   "},
        {"student_answer": None},
        {"student_answer": ["正常", 3]},
        {"knowledge_points": ("变量作用域", 5)},
        {"knowledge_base_ids": ("kb-1", None)},
        {"question_id": 42},
    ],
)
def test_source_rejects_invalid_required_input(overrides: dict[str, Any]) -> None:
    """其余必填与类型错误统一使用 INVALID_INPUT，不做静默转换。"""

    with pytest.raises(GradingInputError) as excinfo:
        _source(**overrides)

    assert excinfo.value.error_code == GRADING_INVALID_INPUT
    assert isinstance(excinfo.value, GradingContextError)


@pytest.mark.parametrize(
    "overrides",
    [
        {"student_answer": ""},
        {"student_answer": "   "},
        {"student_answer": []},
        {"student_answer": ["  "]},
        {"scoring_rubric": None},
        {"scoring_rubric": ""},
        {"scoring_rubric": "   "},
        {"student_answer": {"blank_1": "答案"}},
    ],
)
def test_required_subjective_inputs_must_not_be_blank(
    overrides: dict[str, Any],
) -> None:
    """评分标准与学生答案均为必填非空白；字典答案缺乏键语义约定，显式拒绝。"""

    with pytest.raises(GradingInputError) as excinfo:
        _source(**overrides)

    assert excinfo.value.error_code == GRADING_INVALID_INPUT


def test_missing_constructor_arguments_raise_business_error() -> None:
    """漏传必填构造参数必须转为业务异常，不得泄漏 Python `TypeError`。"""

    with pytest.raises(GradingInputError) as excinfo:
        SubjectiveGradingSource(
            question_type=QuestionType.SHORT_ANSWER,
            course_id=COURSE_ID,
            question_content="题干",
            reference_answer="参考答案",
        )

    assert excinfo.value.error_code == GRADING_INVALID_INPUT
    assert not isinstance(excinfo.value, TypeError)


def test_mapping_student_answer_exposes_rejection_reason() -> None:
    """字典答案被拒绝时必须说明类型，避免被隐式转成文本。"""

    with pytest.raises(GradingInputError) as excinfo:
        _source(student_answer={"blank_1": "答案"})

    assert "dict" in excinfo.value.detail


# --- Query Construction ---


def test_query_text_uses_fixed_section_order() -> None:
    """Query 采用固定拼接顺序：题目 → 参考要点 → 评分标准 → 学生答案。"""

    query = build_query_text(_source())

    positions = [
        query.index("请说明 Python 中变量作用域的区别。"),
        query.index("局部作用域只在函数内可见"),
        query.index("答出局部与全局两点各得一半分值。"),
        query.index("变量在函数内部定义时只在函数内可见。"),
    ]

    assert positions == sorted(positions)


def test_query_text_keeps_student_budget_and_original_source() -> None:
    """长文本按字段截断：总额不超上限、学生答案仍有预算、原始输入不被覆盖。"""

    source = _source(
        question_content="题" * 5000,
        reference_answer="答" * 5000,
        scoring_rubric="标" * 5000,
        student_answer="学生要点" * 500,
    )

    query = build_query_text(source)

    assert len(query) <= MAX_QUERY_CHARS
    assert TRUNCATION_MARKER in query
    assert "学生要点" in query
    assert query.split("【学生答案】")[-1].strip()
    assert len(source.question_content) == 5000
    assert len(source.student_answer_text) == 2000


def test_query_text_always_includes_required_sections() -> None:
    """评分标准与学生答案均为必填，因此 Query 段落始终存在。"""

    query = build_query_text(_source())

    assert "【评分标准】" in query
    assert "【学生答案】" in query


# --- Final Context、溯源与充分性 ---


def test_context_records_only_chunks_written_to_final_context() -> None:
    """总预算只允许部分片段写入时，溯源 ID 必须与实际写入片段一致且有序。"""

    hits = [_chunk(f"c{index}", content="正文" * 400) for index in range(10)]
    retriever = StubRetriever(hits=hits)

    context = _build(retriever=retriever)

    written_ids = [chunk.chunk_id for chunk in context.chunks]
    assert 0 < len(written_ids) < len(hits)
    assert written_ids == list(context.retrieved_context_ids)
    assert len(set(written_ids)) == len(written_ids)
    for chunk_id in written_ids:
        assert f"chunk_id={chunk_id}" in context.final_context
    for chunk_id in {chunk.chunk_id for chunk in hits} - set(written_ids):
        assert f"chunk_id={chunk_id}" not in context.final_context
    assert context.candidate_count == len(hits)


def test_context_keeps_course_document_metadata_and_scores() -> None:
    """Final Context 保留课程、资料、metadata 与重排分数。"""

    hit = _chunk("c1", rerank_score=0.87, rank=0)
    context = _build(retriever=StubRetriever(hits=[hit]))

    assert context.chunks[0].document_id == "document-c1"
    assert context.chunks[0].metadata["location"] == "第 1 段"
    assert context.chunks[0].rerank_score == 0.87
    assert "document-c1" in context.final_context
    assert "0.87" in context.final_context


def test_caller_filters_cannot_widen_course_scope() -> None:
    """调用方过滤条件不得把检索范围扩到其它课程。"""

    retriever = StubRetriever(hits=[_chunk("c1")])

    with pytest.raises(GradingInputError) as excinfo:
        _build(
            retriever=retriever,
            filters=RetrievalFilters(course_ids=(UUID(OTHER_COURSE_ID),)),
        )

    assert excinfo.value.error_code == GRADING_INVALID_INPUT
    assert retriever.calls == []


def test_caller_filters_are_intersected_with_source_scope() -> None:
    """课程始终强制为题目课程；知识库取交集；资料过滤透传。"""

    retriever = StubRetriever(hits=[_chunk("c1")])

    context = _build(
        retriever=retriever,
        knowledge_base_ids=(KNOWLEDGE_BASE_ID,),
        filters=RetrievalFilters(
            course_ids=(UUID(COURSE_ID),),
            knowledge_base_ids=(
                UUID(KNOWLEDGE_BASE_ID),
                UUID(OTHER_KNOWLEDGE_BASE_ID),
            ),
            document_ids=(UUID(DOCUMENT_ID),),
        ),
    )

    scope = context.filters
    assert [str(value) for value in scope.course_ids] == [COURSE_ID]
    assert [str(value) for value in scope.knowledge_base_ids] == [KNOWLEDGE_BASE_ID]
    assert [str(value) for value in scope.document_ids] == [DOCUMENT_ID]


def test_conflicting_knowledge_base_filters_are_rejected() -> None:
    """题目知识库范围与调用方条件无交集时显式失败。"""

    retriever = StubRetriever(hits=[_chunk("c1")])

    with pytest.raises(GradingInputError):
        _build(
            retriever=retriever,
            knowledge_base_ids=(KNOWLEDGE_BASE_ID,),
            filters=RetrievalFilters(
                course_ids=(UUID(COURSE_ID),),
                knowledge_base_ids=(UUID(OTHER_KNOWLEDGE_BASE_ID),),
            ),
        )

    assert retriever.calls == []


@pytest.mark.parametrize("content", ["", "   ", "\n\t"])
def test_context_is_insufficient_for_blank_bodies(content: str) -> None:
    """仅剩来源头或零正文一律判不足，不得用“有返回项”代替充分性判断。"""

    retriever = StubRetriever(hits=[_chunk("c1", content=content)])

    with pytest.raises(InsufficientContextError) as excinfo:
        _build(retriever=retriever)

    assert excinfo.value.error_code == GRADING_MISSING_CONTEXT


def test_context_is_insufficient_when_retrieval_returns_nothing() -> None:
    """检索为空时必须显式失败，且不把空上下文当作充分依据。"""

    retriever = StubRetriever(hits=[])

    with pytest.raises(InsufficientContextError) as excinfo:
        _build(retriever=retriever)

    assert excinfo.value.error_code == GRADING_MISSING_CONTEXT
    assert "0" in excinfo.value.detail


def test_require_context_false_returns_insufficient_context() -> None:
    """允许空上下文时返回不足状态，供上游决定复核策略。"""

    context = _build(retriever=StubRetriever(hits=[]), require_context=False)

    assert context.is_sufficient is False
    assert context.retrieved_context_ids == ()
    assert context.final_context == ""


def test_sufficient_context_exposes_ensure_helper() -> None:
    """充分上下文可安全通过 ensure_sufficient。"""

    context = _build(retriever=StubRetriever(hits=[_chunk("c1")]))

    assert context.is_sufficient is True
    context.ensure_sufficient()


# --- 逐模式调用合同 ---


def test_keyword_mode_does_not_call_embedding() -> None:
    """关键词模式只传文本，不调用 Embedding。"""

    embedding = StubEmbeddingProvider()
    retriever = StubRetriever(hits=[_chunk("c1")], mode=RetrievalMode.KEYWORD_ONLY)

    context = _build(
        retriever=retriever,
        embedding=embedding,
        mode=RetrievalMode.KEYWORD_ONLY,
    )

    assert embedding.queries == []
    assert retriever.calls[0]["query"] == context.query_text


def test_vector_mode_receives_embedding_only() -> None:
    """向量模式只接收查询向量。"""

    embedding = StubEmbeddingProvider()
    retriever = StubRetriever(hits=[_chunk("c1")], mode=RetrievalMode.VECTOR_ONLY)

    _build(retriever=retriever, embedding=embedding, mode=RetrievalMode.VECTOR_ONLY)

    assert len(embedding.queries) == 1
    assert list(retriever.calls[0]["query"]) == list(EMBEDDING_VECTOR)


def test_hybrid_mode_receives_retrieval_query() -> None:
    """Hybrid 模式同时提供查询文本与向量。"""

    retriever = StubRetriever(hits=[_chunk("c1")])

    context = _build(retriever=retriever, mode=RetrievalMode.HYBRID)

    sent = retriever.calls[0]["query"]
    assert isinstance(sent, RetrievalQuery)
    assert sent.text == context.query_text
    assert list(sent.embedding) == list(EMBEDDING_VECTOR)


def test_default_candidate_count_comes_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认候选数读取 RERANK_MAX_CANDIDATES，不在代码中硬编码。"""

    captured: dict[str, Any] = {}
    retriever = StubRetriever(hits=[_chunk("c1")])

    def fake_get_retriever(mode: Any, **kwargs: Any) -> BaseRetriever:
        captured["mode"] = mode
        captured["kwargs"] = kwargs
        return retriever

    monkeypatch.setattr(grading_context_module, "get_retriever", fake_get_retriever)

    _build(
        retriever=None,
        mode=RetrievalMode.HYBRID,
        settings=build_test_settings(rerank_max_candidates=5),
    )

    assert captured["mode"] is RetrievalMode.HYBRID
    assert captured["kwargs"]["candidate_k"] == 5


def test_filters_limit_course_and_knowledge_base_scope() -> None:
    """检索范围限定到题目所属课程与指定知识库。"""

    retriever = StubRetriever(hits=[_chunk("c1")])

    context = _build(
        retriever=retriever,
        knowledge_base_ids=(KNOWLEDGE_BASE_ID,),
    )

    scope = retriever.calls[0]["filters"]
    assert isinstance(scope, RetrievalFilters)
    assert [str(value) for value in scope.course_ids] == [COURSE_ID]
    assert [str(value) for value in scope.knowledge_base_ids] == [KNOWLEDGE_BASE_ID]
    assert context.filters == scope


def test_retrieval_errors_propagate_without_fallback() -> None:
    """检索层异常直接传播，不静默降级为单路检索。"""

    error = RetrievalUnsupportedDialectError("当前方言不支持关键词检索。")
    retriever = StubRetriever(error=error)

    with pytest.raises(RetrievalUnsupportedDialectError):
        _build(retriever=retriever)


def test_embedding_errors_propagate() -> None:
    """Embedding 失败必须显式向上传播。"""

    embedding = StubEmbeddingProvider(error=RuntimeError("embedding 不可用"))

    with pytest.raises(RuntimeError):
        _build(embedding=embedding)


# --- 真实 M2 Rerank 适配器（异步路径） ---


@async_test
async def test_rerank_uses_real_adapter_with_fake_provider() -> None:
    """真实 `LLMRerankAdapter` + 假 Provider 在事件循环内完成重排，不使用同步入口。"""

    hits = [
        _chunk("c1", fusion_score=0.9, rank=0),
        _chunk("c2", fusion_score=0.8, rank=1),
        _chunk("c3", fusion_score=0.7, rank=2),
    ]
    provider = FakeRerankProvider(
        {
            "rankings": [
                {"chunk_id": "c3", "score": 0.95},
                {"chunk_id": "c1", "score": 0.4},
                {"chunk_id": "c2", "score": 0.2},
            ]
        }
    )
    reranker = LLMRerankAdapter(provider=provider, max_candidates=20)
    retriever = StubRetriever(hits=hits)
    source = _source()

    context = await build_grading_context(
        _SESSION,
        source,
        mode=RetrievalMode.HYBRID_RERANK,
        top_k=2,
        retriever=retriever,
        reranker=reranker,
        embedding_provider=StubEmbeddingProvider(),
    )

    assert [chunk.chunk_id for chunk in context.chunks] == ["c3", "c1"]
    assert context.chunks[0].rerank_score == pytest.approx(0.95)
    assert [chunk.rank for chunk in context.chunks] == [0, 1]
    assert context.retrieval_mode is RetrievalMode.HYBRID_RERANK
    assert provider.calls[0]["schema"] is LLMRerankResponse
    prompt = str(provider.calls[0]["messages"][1]["content"])
    assert all(chunk.chunk_id in prompt for chunk in hits)


@async_test
async def test_rerank_response_must_only_reference_known_candidates() -> None:
    """LLM 返回候选之外的 chunk_id 时必须显式失败，不伪造来源。"""

    provider = FakeRerankProvider({"rankings": [{"chunk_id": "unknown", "score": 1.0}]})
    reranker = LLMRerankAdapter(provider=provider, max_candidates=20)

    with pytest.raises(RerankFailedError) as excinfo:
        await build_grading_context(
            _SESSION,
            _source(),
            mode=RetrievalMode.HYBRID_RERANK,
            retriever=StubRetriever(hits=[_chunk("c1")]),
            reranker=reranker,
            embedding_provider=StubEmbeddingProvider(),
        )

    assert "unknown" in str(excinfo.value)


@async_test
async def test_default_reranker_is_resolved_once_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未注入 reranker 时按 RERANK_PROVIDER 构建适配器（真实 LLM 路线 + 假 Provider）。"""

    provider = FakeRerankProvider({"rankings": [{"chunk_id": "c1", "score": 0.9}]})
    built: list[Any] = []

    def fake_build_reranker(**kwargs: Any) -> LLMRerankAdapter:
        adapter = LLMRerankAdapter(provider=provider, **kwargs)
        built.append(adapter)
        return adapter

    monkeypatch.setattr(
        grading_context_module, "build_reranker", fake_build_reranker
    )

    context = await build_grading_context(
        _SESSION,
        _source(),
        mode=RetrievalMode.HYBRID_RERANK,
        retriever=StubRetriever(hits=[_chunk("c1")]),
        embedding_provider=StubEmbeddingProvider(),
    )

    assert len(built) == 1
    assert context.chunks[0].rerank_score == pytest.approx(0.9)


def _build(
    *,
    retriever: Any = None,
    embedding: Any = None,
    mode: RetrievalMode = RetrievalMode.HYBRID,
    top_k: int = DEFAULT_TOP_K,
    require_context: bool = True,
    knowledge_base_ids: Sequence[str] = (),
    filters: Any = None,
    settings: Any = None,
) -> Any:
    """同步包装：测试统一通过事件循环调用异步组装入口。"""

    return asyncio.run(
        build_grading_context(
            _SESSION,
            _source(knowledge_base_ids=tuple(knowledge_base_ids)),
            mode=mode,
            top_k=top_k,
            filters=filters,
            retriever=retriever,
            embedding_provider=embedding or StubEmbeddingProvider(),
            require_context=require_context,
            settings=settings,
        )
    )
