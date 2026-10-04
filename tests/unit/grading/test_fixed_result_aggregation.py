"""T177 fixed-result identity, Decimal sums and incomplete totals (TCR §32)."""

from decimal import Decimal

import pytest

from backend.app.domain.enums import QuestionType
from backend.app.schemas.ai import GradingResult
from backend.app.schemas.grading import ExpectedAnswer, SubmissionContext
from backend.app.services.grading.result_aggregator import (
    GradingAggregationError,
    ResultAggregator,
)


def context(*, fixed=True, count=1):
    return SubmissionContext(
        submission_id="submission",
        exam_id="exam",
        student_id="student",
        expected_answers=[
            ExpectedAnswer(
                order=i,
                answer_id=f"answer-{i}",
                question_id=f"question-{i}",
                exam_question_id=f"link-{i}" if fixed else None,
                question_type=QuestionType.SINGLE_CHOICE,
                max_score=Decimal("10.00"),
                knowledge_points=["published"],
            )
            for i in range(1, count + 1)
        ],
    )


def result(*, index=1, fixed=True, score="0.10", **changes):
    raw = {
        "question_type": QuestionType.SINGLE_CHOICE,
        "score": Decimal(score),
        "max_score": Decimal("10.00"),
        "reason": "Deterministic grading",
        "correct_points": [],
        "missing_knowledge_points": [],
        "knowledge_points": ["published"],
        "suggestions": [],
        "confidence": 1,
        "answer_id": f"answer-{index}",
        "submission_id": "submission",
        "exam_question_id": f"link-{index}" if fixed else None,
    }
    raw.update(changes)
    return GradingResult(**raw)


@pytest.mark.parametrize("actual", [None, "foreign-link"])
def test_fixed_result_requires_same_recorded_exam_question(actual):
    with pytest.raises(GradingAggregationError):
        ResultAggregator().aggregate(
            context(), results=[result(exam_question_id=actual)]
        )


def test_fixed_result_cannot_change_published_labels():
    with pytest.raises(GradingAggregationError):
        ResultAggregator().aggregate(
            context(), results=[result(knowledge_points=["bank-now"])]
        )


def test_fixed_result_decimal_sum_and_identity_stay_exact():
    actual = ResultAggregator().aggregate(
        context(count=3),
        results=[
            result(index=1, score="0.10"),
            result(index=2, score="0.20"),
            result(index=3, score="0.01"),
        ],
    )
    assert actual.final_total_score == Decimal("0.31")
    assert [item.score for item in actual.items] == [
        Decimal("0.10"),
        Decimal("0.20"),
        Decimal("0.01"),
    ]
    assert all(isinstance(item.score, Decimal) for item in actual.items)
    assert [item.exam_question_id for item in actual.items] == [
        "link-1",
        "link-2",
        "link-3",
    ]


def test_historical_result_missing_identity_is_not_backfilled():
    actual = ResultAggregator().aggregate(
        context(fixed=False), results=[result(fixed=False)]
    )
    assert actual.items[0].exam_question_id is None
    assert actual.final_total_score == Decimal("0.10")


@pytest.mark.parametrize("state", ["failed", "pending", "missing"])
def test_incomplete_results_are_excluded_from_final_total(state):
    second = (
        []
        if state == "missing"
        else [
            result(
                index=2,
                score="7.00",
                validation_status="Failed" if state == "failed" else "Validated",
                review_status=(
                    "Pending Review" if state == "pending" else "Not Required"
                ),
            )
        ]
    )
    actual = ResultAggregator().aggregate(
        context(count=2), results=[result(score="0.20"), *second]
    )
    assert actual.final_total_score is None and not actual.is_final
    assert actual.confirmed_subtotal == Decimal("0.20")
    assert actual.items[1].effective_score is None and not actual.items[1].counted
    if state == "missing":
        assert actual.items[1].score is None
