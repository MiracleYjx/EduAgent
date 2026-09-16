"""T055 诊断服务单元测试：准入门槛、平台计算字段、LLM 边界与失效合同。

TCR（2026-09-16，T055 / B03）：仓库此前没有诊断服务，无法证明 FR-038 与 SC-007 的
“教师完成复核后，最终评分才能进入学生诊断报告”；同时需要固化“整卷未完成即未就绪且
不调用 LLM”“掌握度不得用评分 confidence 冒充”“缺知识点不填 0”“LLM 建议必须走
generate_structured”“ExamResult 更新后旧报告失效”等合同。先新增失败用例，再实现
``backend/app/services/diagnosis_service.py``。

掌握度算法与薄弱点阈值属**新增业务约定**：``.specify`` 文档未规定具体公式与阈值，
因此测试只固定本批声明的行为（分子分母累加、两位小数、默认阈值 0.6 可配置），不冒称
既有契约。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel

from backend.app.ai.llm.base import BaseLLMProvider, LLMMessage
from backend.app.core.retry_policy import ProviderErrorInfo, ProviderExecutionError
from backend.app.domain.enums import QuestionType
from backend.app.schemas.ai import GradingResult
from backend.app.schemas.grading import (
    DiagnosisStatus,
    ExamResultDTO,
    ExpectedAnswer,
    SubmissionContext,
)
from backend.app.services.diagnosis_service import (
    DEFAULT_WEAK_MASTERY_THRESHOLD,
    DIAGNOSIS_NOT_READY,
    DIAGNOSIS_PROVIDER_NOT_READY,
    DIAGNOSIS_SUGGESTION_FAILED,
    DiagnosisService,
)
from backend.app.services.grading.result_aggregator import ResultAggregator

SUBMISSION_ID = "submission-1"
EXAM_ID = "exam-1"
STUDENT_ID = "student-1"
OBJECTIVE = QuestionType.SINGLE_CHOICE
SUBJECTIVE = QuestionType.SHORT_ANSWER
KP_A = "知识点 A"
KP_B = "知识点 B"


class StubSuggestionProvider(BaseLLMProvider):
    """可控建议 Provider：返回结构化建议、抛错或记录调用次数。"""

    provider_name = "stub-diagnosis"

    def __init__(
        self,
        suggestions: Sequence[str] = ("建议一",),
        *,
        response: BaseModel | None = None,
        error: Exception | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._suggestions = list(suggestions)
        self._response = response
        self._error = error

    async def generate_structured(
        self,
        messages: Sequence[LLMMessage],
        schema: type[BaseModel],
        model: str | None = None,
    ) -> BaseModel:
        self.calls.append({"messages": list(messages), "schema": schema, "model": model})
        if self._error is not None:
            raise self._error
        if self._response is not None:
            return self._response
        return schema.model_validate({"suggestions": self._suggestions})


def _expected(
    order: int,
    answer_id: str,
    *,
    question_type: QuestionType = OBJECTIVE,
    max_score: str = "10.00",
    knowledge_points: tuple[str, ...] = (KP_A,),
) -> ExpectedAnswer:
    """构造服务端权威的预期题目。"""

    return ExpectedAnswer(
        order=order,
        answer_id=answer_id,
        question_id=f"question-{order}",
        question_type=question_type,
        max_score=Decimal(max_score),
        knowledge_points=list(knowledge_points),
    )


def _result(
    answer_id: str,
    *,
    score: str = "10",
    max_score: str = "10",
    question_type: QuestionType = OBJECTIVE,
    confidence: float = 0.9,
    review_status: str = "Not Required",
    validation_status: str = "Validated",
    missing_knowledge_points: Sequence[str] = (),
) -> GradingResult:
    """构造单题评分结果。"""

    return GradingResult(
        question_type=question_type,
        score=float(score),
        max_score=float(max_score),
        reason="评分理由。",
        correct_points=["要点一"],
        missing_knowledge_points=list(missing_knowledge_points),
        knowledge_points=[KP_A],
        suggestions=["继续练习。"],
        confidence=confidence,
        validation_status=validation_status,
        review_status=review_status,
        answer_id=answer_id,
        submission_id=SUBMISSION_ID,
    )


def _aggregate(
    expected: Sequence[ExpectedAnswer],
    results: Sequence[GradingResult],
    *,
    decisions: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> ExamResultDTO:
    """用 T054 汇总服务构造整卷结果，保证输入与生产一致。"""

    return ResultAggregator().aggregate(
        SubmissionContext(
            submission_id=SUBMISSION_ID,
            exam_id=EXAM_ID,
            student_id=STUDENT_ID,
            expected_answers=list(expected),
        ),
        results=list(results),
        decisions=decisions or {},
        **({"now": now} if now is not None else {}),
    )


def _mastery_lookup(report: Any) -> dict[str, Any]:
    """把掌握度列表转成按知识点索引的映射。"""

    return {
        entry.knowledge_point: entry for entry in report.mastery_by_knowledge_point
    }


def test_pending_review_exam_result_is_not_ready_without_llm_call() -> None:
    """存在待复核题目时整卷不是最终结果，诊断必须未就绪且不调用 LLM。"""

    exam_result = _aggregate(
        [_expected(1, "answer-1", question_type=SUBJECTIVE, max_score="8.00")],
        [
            _result(
                "answer-1",
                score="5",
                max_score="8",
                question_type=SUBJECTIVE,
                confidence=0.4,
                review_status="Pending Review",
            )
        ],
    )
    provider = StubSuggestionProvider()

    report = asyncio.run(DiagnosisService(provider=provider).generate(exam_result))

    assert report.status is DiagnosisStatus.NOT_READY
    assert report.error_code == DIAGNOSIS_NOT_READY
    assert report.learning_suggestions == []
    assert report.mastery_by_knowledge_point == []
    assert report.generated_at is None
    assert provider.calls == []


def test_failed_exam_result_is_not_ready() -> None:
    """评分失败的整卷不得生成诊断。"""

    exam_result = _aggregate(
        [_expected(1, "answer-1")],
        [_result("answer-1", validation_status="Failed")],
    )
    provider = StubSuggestionProvider()

    report = asyncio.run(DiagnosisService(provider=provider).generate(exam_result))

    assert report.status is DiagnosisStatus.NOT_READY
    assert provider.calls == []


def test_final_result_generates_platform_fields_and_llm_suggestions() -> None:
    """最终结果生成四类字段，建议来自结构化输出。"""

    exam_result = _aggregate(
        [
            _expected(1, "answer-1", knowledge_points=(KP_A,)),
            _expected(2, "answer-2", knowledge_points=(KP_B,)),
        ],
        [
            _result("answer-1", score="10", max_score="10"),
            _result(
                "answer-2",
                score="4",
                max_score="10",
                missing_knowledge_points=("要点二",),
            ),
        ],
    )
    provider = StubSuggestionProvider(suggestions=("建议一", "建议二"))

    report = asyncio.run(DiagnosisService(provider=provider).generate(exam_result))

    assert report.status is DiagnosisStatus.READY
    assert report.error_code is None
    assert report.learning_suggestions == ["建议一", "建议二"]
    assert report.mastery_by_knowledge_point
    assert report.weak_knowledge_points
    assert report.error_reasons
    assert report.generated_at is not None
    assert report.source_exam_result_updated_at == exam_result.aggregated_at
    assert len(provider.calls) == 1
    assert provider.calls[0]["schema"].__name__ == "LearningSuggestions"


def test_same_knowledge_point_across_questions_accumulates() -> None:
    """多题命中同一知识点时分子分母累加，不做平均。"""

    exam_result = _aggregate(
        [
            _expected(1, "answer-1", knowledge_points=(KP_A,)),
            _expected(2, "answer-2", knowledge_points=(KP_A,)),
        ],
        [
            _result("answer-1", score="10", max_score="10"),
            _result("answer-2", score="6", max_score="10"),
        ],
    )
    provider = StubSuggestionProvider()

    report = asyncio.run(DiagnosisService(provider=provider).generate(exam_result))
    entry = _mastery_lookup(report)[KP_A]

    assert entry.answered_count == 2
    assert entry.correct_count == 1
    assert entry.awarded_score == Decimal("16.00")
    assert entry.max_score == Decimal("20.00")
    assert entry.mastery == Decimal("0.80")


def test_question_with_multiple_knowledge_points_attributes_full_score() -> None:
    """一题覆盖多个知识点时，整题得分同时计入各知识点。"""

    exam_result = _aggregate(
        [_expected(1, "answer-1", knowledge_points=(KP_A, KP_B))],
        [_result("answer-1", score="10", max_score="10")],
    )
    provider = StubSuggestionProvider()

    report = asyncio.run(DiagnosisService(provider=provider).generate(exam_result))
    lookup = _mastery_lookup(report)

    assert set(lookup) == {KP_A, KP_B}
    assert lookup[KP_A].mastery == Decimal("1.00")
    assert lookup[KP_B].mastery == Decimal("1.00")


def test_all_correct_exam_has_empty_weak_points_and_error_reasons() -> None:
    """全对答卷不强迫生成错误原因，允许空集合。"""

    exam_result = _aggregate(
        [_expected(1, "answer-1", knowledge_points=(KP_A,))],
        [_result("answer-1", score="10", max_score="10")],
    )
    provider = StubSuggestionProvider(suggestions=())

    report = asyncio.run(DiagnosisService(provider=provider).generate(exam_result))

    assert report.weak_knowledge_points == []
    assert report.error_reasons == []
    assert report.learning_suggestions == []
    assert report.status is DiagnosisStatus.READY


def test_missing_knowledge_points_are_reported_as_insufficient_evidence() -> None:
    """题目未声明知识点时不填 0 冒充掌握度，而是标记依据不足。"""

    exam_result = _aggregate(
        [_expected(1, "answer-1", knowledge_points=())],
        [_result("answer-1", score="7", max_score="10")],
    )
    provider = StubSuggestionProvider()

    report = asyncio.run(DiagnosisService(provider=provider).generate(exam_result))

    assert report.mastery_by_knowledge_point == []
    assert report.insufficient_evidence_answer_ids == ["answer-1"]
    assert report.status is DiagnosisStatus.READY


def test_mastery_does_not_use_scoring_confidence() -> None:
    """掌握度来自得分比例，不使用评分 confidence。"""

    exam_result = _aggregate(
        [_expected(1, "answer-1", knowledge_points=(KP_A,))],
        [_result("answer-1", score="0", max_score="10", confidence=0.99)],
    )
    provider = StubSuggestionProvider()

    report = asyncio.run(DiagnosisService(provider=provider).generate(exam_result))
    entry = _mastery_lookup(report)[KP_A]

    assert entry.mastery == Decimal("0.00")
    assert entry.awarded_score == Decimal("0.00")


def test_llm_failure_marks_report_failed_without_fake_success() -> None:
    """结构化建议失败时保留来源错误码，不返回伪造的成功诊断。"""

    exam_result = _aggregate(
        [_expected(1, "answer-1", knowledge_points=(KP_A,))],
        [_result("answer-1", score="8", max_score="10")],
    )
    provider = StubSuggestionProvider(
        error=ProviderExecutionError(
            ProviderErrorInfo(
                code="PROVIDER_STRUCTURED_OUTPUT_INVALID",
                message="脱敏后的 Provider 失败。",
                attempt_count=2,
                retryable=True,
            )
        )
    )

    report = asyncio.run(DiagnosisService(provider=provider).generate(exam_result))

    assert report.status is DiagnosisStatus.FAILED
    assert report.error_code == DIAGNOSIS_SUGGESTION_FAILED
    assert report.retryable is True
    assert report.source_code == "PROVIDER_STRUCTURED_OUTPUT_INVALID"
    assert report.attempt_count == 2
    assert report.learning_suggestions == []
    assert report.mastery_by_knowledge_point
    assert len(provider.calls) == 1


def test_missing_provider_returns_provider_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider 未就绪时不返回伪造诊断。"""

    exam_result = _aggregate(
        [_expected(1, "answer-1", knowledge_points=(KP_A,))],
        [_result("answer-1", score="10", max_score="10")],
    )

    def _boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("provider 未就绪")

    monkeypatch.setattr("backend.app.services.diagnosis_service.create_llm_provider", _boom)

    report = asyncio.run(DiagnosisService().generate(exam_result))

    assert report.status is DiagnosisStatus.FAILED
    assert report.error_code == DIAGNOSIS_PROVIDER_NOT_READY
    assert report.retryable is False
    assert report.learning_suggestions == []


def test_report_becomes_stale_after_exam_result_updates() -> None:
    """ExamResult 更新后旧报告必须失效，需要重新生成。"""

    expected = [_expected(1, "answer-1", knowledge_points=(KP_A,))]
    moment = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    first = _aggregate(
        expected,
        [_result("answer-1", score="10", max_score="10")],
        now=moment,
    )
    second = _aggregate(
        expected,
        [_result("answer-1", score="4", max_score="10")],
        now=moment + timedelta(minutes=5),
    )
    service = DiagnosisService(provider=StubSuggestionProvider())
    report = asyncio.run(service.generate(first))

    assert service.is_current(report, first) is True
    assert service.is_current(report, second) is False


def test_weak_threshold_is_configurable_new_convention() -> None:
    """薄弱知识点阈值是本批新增约定，可通过构造参数覆盖。"""

    exam_result = _aggregate(
        [
            _expected(1, "answer-1", knowledge_points=(KP_A,)),
            _expected(2, "answer-2", knowledge_points=(KP_A,)),
        ],
        [
            _result("answer-1", score="10", max_score="10"),
            _result("answer-2", score="6", max_score="10"),
        ],
    )
    strict_provider = StubSuggestionProvider()

    strict = asyncio.run(
        DiagnosisService(
            provider=strict_provider,
            weak_mastery_threshold=Decimal("0.90"),
        ).generate(exam_result)
    )
    default = asyncio.run(
        DiagnosisService(provider=StubSuggestionProvider()).generate(exam_result)
    )

    assert DEFAULT_WEAK_MASTERY_THRESHOLD == Decimal("0.60")
    assert [entry.knowledge_point for entry in strict.weak_knowledge_points] == [KP_A]
    assert default.weak_knowledge_points == []


def test_invalid_threshold_is_rejected() -> None:
    """阈值非法时显式失败，不静默回退默认值。"""

    with pytest.raises(Exception) as error:
        DiagnosisService(
            provider=StubSuggestionProvider(),
            weak_mastery_threshold=Decimal("1.50"),
        )

    assert getattr(error.value, "error_code", None) == "DIAGNOSIS_INVALID_THRESHOLD"
