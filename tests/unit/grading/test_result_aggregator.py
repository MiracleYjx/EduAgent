"""T054 结果汇总单元测试：完整性、整卷状态判定、精度与无 LLM 约束。

TCR（2026-09-16，T054 / B02）：仓库此前没有 Objective 与 Subjective 结果的统一汇总服务，
无法证明 plan.md §5.2 的“只有全部题目可接受或全部低置信度结果完成复核才算最终结果”与
data-model.md 的 GradingResult 状态图已落地；同时需要固化完整性行为（缺题不补 0、重复、
额外、跨答卷、身份缺失、分数异常）与 ConfidenceDecision 的传递一致性。先新增失败用例，
再实现 ``backend/app/services/grading/result_aggregator.py``。

测试只检查业务合同（FR-033～FR-037、plan.md §5.1/§5.2、data-model.md 状态图），不镜像
实现细节、不放宽既有断言；汇总过程不得调用 LLM Provider。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from backend.app.domain.enums import QuestionType
from backend.app.schemas.ai import GradingResult
from backend.app.schemas.grading import (
    ExamResultStatus,
    ExpectedAnswer,
    SubmissionContext,
)
from backend.app.services.grading.confidence_policy import ConfidenceDecision
from backend.app.services.grading.result_aggregator import (
    GRADING_AGGREGATION_EMPTY_EXPECTED,
    GRADING_DUPLICATE_RESULT,
    GRADING_IDENTITY_MISSING,
    GRADING_QUESTION_TYPE_MISMATCH,
    GRADING_SCORE_INVALID,
    GRADING_SUBMISSION_MISMATCH,
    GRADING_UNEXPECTED_RESULT,
    GRADING_UNKNOWN_STATE,
    ResultAggregator,
)

SUBMISSION_ID = "submission-1"
EXAM_ID = "exam-1"
STUDENT_ID = "student-1"
OBJECTIVE = QuestionType.SINGLE_CHOICE
SUBJECTIVE = QuestionType.SHORT_ANSWER
KNOWLEDGE_POINT = "知识点 A"


def _expected(
    order: int,
    answer_id: str,
    *,
    question_type: QuestionType = OBJECTIVE,
    max_score: str = "10.00",
    knowledge_points: tuple[str, ...] = (KNOWLEDGE_POINT,),
) -> ExpectedAnswer:
    """构造服务端权威的预期题目与答案关联。"""

    return ExpectedAnswer(
        order=order,
        answer_id=answer_id,
        question_id=f"question-{order}",
        question_type=question_type,
        max_score=Decimal(max_score),
        knowledge_points=list(knowledge_points),
    )


def _context(*expected: ExpectedAnswer) -> SubmissionContext:
    """构造一份答卷的预期集合。"""

    return SubmissionContext(
        submission_id=SUBMISSION_ID,
        exam_id=EXAM_ID,
        student_id=STUDENT_ID,
        expected_answers=list(expected),
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
    submission_id: str | None = SUBMISSION_ID,
) -> GradingResult:
    """构造单题评分结果 DTO。"""

    return GradingResult(
        question_type=question_type,
        score=float(score),
        max_score=float(max_score),
        reason="评分理由。",
        correct_points=["要点一"],
        missing_knowledge_points=[],
        knowledge_points=[KNOWLEDGE_POINT],
        suggestions=["继续练习。"],
        confidence=confidence,
        validation_status=validation_status,
        review_status=review_status,
        answer_id=answer_id,
        submission_id=submission_id,
    )


def _accepted(answer_id: str, *, confidence: float = 0.9) -> ConfidenceDecision:
    """构造自动接受决策。"""

    return ConfidenceDecision(
        confidence=confidence,
        threshold=0.8,
        requires_review=False,
        review_status="Not Required",
        grading_status="Accepted",
        reason="置信度不低于阈值，自动接受。",
    )


def _pending(answer_id: str, *, confidence: float = 0.4) -> ConfidenceDecision:
    """构造待人工复核决策。"""

    return ConfidenceDecision(
        confidence=confidence,
        threshold=0.8,
        requires_review=True,
        review_status="Pending Review",
        grading_status="Pending Review",
        reason="置信度低于阈值，进入待人工复核。",
    )


def _aggregator() -> ResultAggregator:
    """构造被测汇总服务。"""

    return ResultAggregator()


def test_empty_expected_answers_are_rejected() -> None:
    """预期题目为空时不得返回 0 分成绩。"""

    with pytest.raises(Exception) as error:
        _aggregator().aggregate(
            SubmissionContext(
                submission_id=SUBMISSION_ID,
                exam_id=EXAM_ID,
                student_id=STUDENT_ID,
                expected_answers=[],
            ),
            results=[],
            decisions={},
        )

    assert getattr(error.value, "error_code", None) == GRADING_AGGREGATION_EMPTY_EXPECTED


def test_missing_result_stays_pending_without_zero_filling() -> None:
    """缺结果不得按 0 分补齐，也不得标成待人工复核。"""

    aggregated = _aggregator().aggregate(
        _context(_expected(1, "answer-1"), _expected(2, "answer-2")),
        results=[_result("answer-1", score="6", max_score="10")],
        decisions={},
    )

    assert aggregated.result_status is ExamResultStatus.PENDING
    assert aggregated.is_final is False
    assert aggregated.final_total_score is None
    assert aggregated.missing_answer_ids == ["answer-2"]
    assert aggregated.confirmed_subtotal == Decimal("6.00")
    assert aggregated.counted_answer_count == 1
    assert aggregated.expected_answer_count == 2


def test_duplicate_results_are_rejected() -> None:
    """同一题出现多条结果时来源有歧义，必须显式失败。"""

    with pytest.raises(Exception) as error:
        _aggregator().aggregate(
            _context(_expected(1, "answer-1")),
            results=[_result("answer-1"), _result("answer-1")],
            decisions={},
        )

    assert getattr(error.value, "error_code", None) == GRADING_DUPLICATE_RESULT


def test_duplicate_expected_answers_are_rejected() -> None:
    """预期集合本身重复答案标识时同样拒绝。"""

    with pytest.raises(Exception) as error:
        _aggregator().aggregate(
            _context(_expected(1, "answer-1"), _expected(2, "answer-1")),
            results=[_result("answer-1")],
            decisions={},
        )

    assert getattr(error.value, "error_code", None) == GRADING_DUPLICATE_RESULT


def test_unexpected_result_answer_is_rejected() -> None:
    """结果中出现预期集合之外的答案标识时拒绝，不静默忽略。"""

    with pytest.raises(Exception) as error:
        _aggregator().aggregate(
            _context(_expected(1, "answer-1")),
            results=[_result("answer-1"), _result("answer-2")],
            decisions={},
        )

    assert getattr(error.value, "error_code", None) == GRADING_UNEXPECTED_RESULT


def test_result_from_other_submission_is_rejected() -> None:
    """跨答卷结果不得混入本次汇总。"""

    with pytest.raises(Exception) as error:
        _aggregator().aggregate(
            _context(_expected(1, "answer-1")),
            results=[_result("answer-1", submission_id="submission-other")],
            decisions={},
        )

    assert getattr(error.value, "error_code", None) == GRADING_SUBMISSION_MISMATCH


def test_result_without_submission_identity_is_rejected() -> None:
    """缺少答卷关联的结果无法证明归属，必须显式失败。"""

    with pytest.raises(Exception) as error:
        _aggregator().aggregate(
            _context(_expected(1, "answer-1")),
            results=[_result("answer-1", submission_id=None)],
            decisions={},
        )

    assert getattr(error.value, "error_code", None) == GRADING_IDENTITY_MISSING


def test_max_score_mismatch_is_rejected() -> None:
    """单题满分与题目定义不一致时不得直接汇总。"""

    with pytest.raises(Exception) as error:
        _aggregator().aggregate(
            _context(_expected(1, "answer-1", max_score="10.00")),
            results=[_result("answer-1", score="8", max_score="12")],
            decisions={},
        )

    assert getattr(error.value, "error_code", None) == GRADING_SCORE_INVALID


def test_question_type_mismatch_is_rejected() -> None:
    """结果题型与题目定义不一致时拒绝，避免模型输出反向改变题型。"""

    with pytest.raises(Exception) as error:
        _aggregator().aggregate(
            _context(_expected(1, "answer-1", question_type=OBJECTIVE)),
            results=[_result("answer-1", question_type=SUBJECTIVE)],
            decisions={},
        )

    assert getattr(error.value, "error_code", None) == GRADING_QUESTION_TYPE_MISMATCH


def test_objective_results_finalize_without_decision() -> None:
    """客观题按确定性规则直接接受，无需置信度决策。"""

    aggregated = _aggregator().aggregate(
        _context(_expected(1, "answer-1", max_score="10.00")),
        results=[_result("answer-1", score="10", max_score="10")],
        decisions={},
    )

    assert aggregated.result_status is ExamResultStatus.FINAL
    assert aggregated.is_final is True
    assert aggregated.final_total_score == Decimal("10.00")
    assert aggregated.items[0].counted is True
    assert aggregated.items[0].decision is None


def test_subjective_result_requires_recorded_decision() -> None:
    """主观题的默认 Not Required 不能单独证明执行过 Confidence Check。"""

    with pytest.raises(Exception) as error:
        _aggregator().aggregate(
            _context(_expected(1, "answer-1", question_type=SUBJECTIVE)),
            results=[_result("answer-1", question_type=SUBJECTIVE, confidence=0.95)],
            decisions={},
        )

    assert getattr(error.value, "error_code", None) == GRADING_UNKNOWN_STATE


def test_subjective_accepted_decision_is_counted() -> None:
    """主观题自动接受决策计入最终成绩。"""

    aggregated = _aggregator().aggregate(
        _context(_expected(1, "answer-1", question_type=SUBJECTIVE, max_score="8.00")),
        results=[_result("answer-1", score="8", max_score="8", question_type=SUBJECTIVE)],
        decisions={"answer-1": _accepted("answer-1")},
    )

    assert aggregated.result_status is ExamResultStatus.FINAL
    assert aggregated.final_total_score == Decimal("8.00")
    assert aggregated.items[0].decision is not None
    assert aggregated.items[0].requires_review is False


def test_subjective_pending_review_decision_keeps_exam_pending_review() -> None:
    """低置信度主观题使整卷进入待复核，且不产生最终总分。"""

    aggregated = _aggregator().aggregate(
        _context(_expected(1, "answer-1", question_type=SUBJECTIVE, max_score="8.00")),
        results=[
            _result(
                "answer-1",
                score="5",
                max_score="8",
                question_type=SUBJECTIVE,
                confidence=0.4,
                review_status="Pending Review",
            )
        ],
        decisions={"answer-1": _pending("answer-1")},
    )

    assert aggregated.result_status is ExamResultStatus.PENDING_REVIEW
    assert aggregated.is_final is False
    assert aggregated.final_total_score is None
    assert aggregated.pending_review_answer_count == 1
    assert aggregated.confirmed_subtotal == Decimal("0.00")


def test_inconsistent_decision_is_rejected() -> None:
    """决策要求复核但结果仍是自动接受状态时视为状态不可信。"""

    with pytest.raises(Exception) as error:
        _aggregator().aggregate(
            _context(_expected(1, "answer-1", question_type=SUBJECTIVE)),
            results=[_result("answer-1", question_type=SUBJECTIVE, confidence=0.4)],
            decisions={"answer-1": _pending("answer-1")},
        )

    assert getattr(error.value, "error_code", None) == GRADING_UNKNOWN_STATE


def test_confirmed_human_review_wins_over_low_confidence_decision() -> None:
    """人工确认后旧的低置信度决策不得覆盖人工结论。"""

    aggregated = _aggregator().aggregate(
        _context(_expected(1, "answer-1", question_type=SUBJECTIVE, max_score="8.00")),
        results=[
            _result(
                "answer-1",
                score="7",
                max_score="8",
                question_type=SUBJECTIVE,
                confidence=0.4,
                review_status="Confirmed",
            )
        ],
        decisions={"answer-1": _pending("answer-1")},
    )

    assert aggregated.result_status is ExamResultStatus.FINAL
    assert aggregated.final_total_score == Decimal("7.00")
    assert aggregated.pending_review_answer_count == 0
    assert aggregated.items[0].counted is True


def test_modified_human_review_uses_updated_score() -> None:
    """人工修改后的正式得分参与最终成绩。"""

    aggregated = _aggregator().aggregate(
        _context(_expected(1, "answer-1", question_type=SUBJECTIVE, max_score="8.00")),
        results=[
            _result(
                "answer-1",
                score="6",
                max_score="8",
                question_type=SUBJECTIVE,
                confidence=0.4,
                review_status="Modified",
            )
        ],
        decisions={},
    )

    assert aggregated.result_status is ExamResultStatus.FINAL
    assert aggregated.final_total_score == Decimal("6.00")


def test_regrade_state_is_not_final() -> None:
    """Re-grade 表示等待重新评分，整卷仍是未完成而不是最终成绩。"""

    aggregated = _aggregator().aggregate(
        _context(_expected(1, "answer-1", question_type=SUBJECTIVE, max_score="8.00")),
        results=[
            _result(
                "answer-1",
                score="4",
                max_score="8",
                question_type=SUBJECTIVE,
                review_status="Re-grade",
            )
        ],
        decisions={},
    )

    assert aggregated.result_status is ExamResultStatus.PENDING
    assert aggregated.is_final is False
    assert aggregated.final_total_score is None
    assert aggregated.items[0].counted is False


def test_failed_validation_marks_exam_result_failed() -> None:
    """结构化校验失败的题目不得进入接受或最终状态。"""

    aggregated = _aggregator().aggregate(
        _context(
            _expected(1, "answer-1"),
            _expected(2, "answer-2", question_type=SUBJECTIVE),
        ),
        results=[
            _result("answer-1", score="10", max_score="10"),
            _result(
                "answer-2",
                score="0",
                max_score="10",
                question_type=SUBJECTIVE,
                validation_status="Failed",
            ),
        ],
        decisions={},
    )

    assert aggregated.result_status is ExamResultStatus.FAILED
    assert aggregated.is_final is False
    assert aggregated.final_total_score is None
    assert aggregated.failed_answer_ids == ["answer-2"]
    assert aggregated.not_validated_answer_ids == ["answer-2"]


def test_unknown_review_status_is_rejected() -> None:
    """未知复核状态无法映射到状态机，必须显式失败。"""

    with pytest.raises(Exception) as error:
        _aggregator().aggregate(
            _context(_expected(1, "answer-1")),
            results=[_result("answer-1", review_status="未知状态")],
            decisions={},
        )

    assert getattr(error.value, "error_code", None) == GRADING_UNKNOWN_STATE


def test_final_total_follows_exam_order_and_decimal_accumulation() -> None:
    """总分按考试题序累加，并用 Decimal 在出口统一舍入。"""

    aggregated = _aggregator().aggregate(
        _context(
            _expected(1, "answer-1", max_score="10.00"),
            _expected(2, "answer-2", max_score="10.00"),
            _expected(3, "answer-3", max_score="10.00"),
        ),
        results=[
            _result("answer-3", score="1.005", max_score="10"),
            _result("answer-1", score="0.1", max_score="10"),
            _result("answer-2", score="0.2", max_score="10"),
        ],
        decisions={},
    )

    assert [item.order for item in aggregated.items] == [1, 2, 3]
    assert [item.answer_id for item in aggregated.items] == [
        "answer-1",
        "answer-2",
        "answer-3",
    ]
    assert aggregated.total_max_score == Decimal("30.00")
    assert aggregated.final_total_score == Decimal("1.31")
    assert aggregated.confirmed_subtotal_label


def test_diagnosis_readiness_stays_false_when_results_are_incomplete() -> None:
    """未完成汇总的整卷不得被当作可生成诊断的最终结果。"""

    aggregated = _aggregator().aggregate(
        _context(
            _expected(1, "answer-1"),
            _expected(2, "answer-2", question_type=SUBJECTIVE),
        ),
        results=[_result("answer-1", score="10", max_score="10")],
        decisions={},
    )

    assert aggregated.result_status is ExamResultStatus.PENDING
    assert aggregated.graded_answer_count == 1
    assert aggregated.counted_answer_count == 1
    assert aggregated.missing_answer_ids == ["answer-2"]


def test_aggregation_ignores_confidence_threshold_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """汇总只消费已记录的决策，不按最新阈值重判历史结果。"""

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("汇总过程不得读取运行配置或重新判定置信度。")

    monkeypatch.setattr("backend.app.core.config.get_settings", _boom)

    aggregated = _aggregator().aggregate(
        _context(_expected(1, "answer-1", question_type=SUBJECTIVE, max_score="8.00")),
        results=[_result("answer-1", score="8", max_score="8", question_type=SUBJECTIVE)],
        decisions={"answer-1": _accepted("answer-1", confidence=0.5)},
    )

    assert aggregated.result_status is ExamResultStatus.FINAL
    assert aggregated.final_total_score == Decimal("8.00")


def test_aggregation_never_calls_llm_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """汇总路径不调用任何 LLM Provider。"""

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("汇总过程不得调用 LLM Provider。")

    monkeypatch.setattr("backend.app.ai.llm.factory.create_llm_provider", _boom)

    aggregated = _aggregator().aggregate(
        _context(
            _expected(1, "answer-1"),
            _expected(2, "answer-2", question_type=SUBJECTIVE, max_score="8.00"),
        ),
        results=[
            _result("answer-1", score="10", max_score="10"),
            _result("answer-2", score="6", max_score="8", question_type=SUBJECTIVE),
        ],
        decisions={"answer-2": _accepted("answer-2")},
    )

    assert aggregated.result_status is ExamResultStatus.FINAL
    assert aggregated.final_total_score == Decimal("16.00")
