"""Approved diagnosis boundaries, independent of ORM and model providers."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from backend.app.services.personal_learning_facts import Observation, summarize

POINTS = (UUID(int=1000),)


def sample(
    n, ratio="0", *, question=None, points=POINTS, excluded=None, source="uploaded"
):
    return Observation(
        answer_id=UUID(int=n),
        item_id=UUID(int=n),
        practice_id=UUID(int=n),
        question_id=UUID(int=question or n),
        revision_id=UUID(int=n),
        attempt_id=UUID(int=n),
        submitted_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=n),
        knowledge_point_ids=points,
        question_type="single_choice",
        source_kind=source,
        answer_origin="paper",
        score=Decimal(ratio) * 10 if excluded is None else None,
        max_score=Decimal(10),
        exclusion=excluded,
    )


@pytest.mark.parametrize(
    "count,ratio,status",
    [
        (4, "0", "insufficient_evidence"),
        (5, "0.59", "reinforce"),
        (5, "0.60", "observed"),
    ],
)
def test_five_and_sixty_percent_boundaries(count, ratio, status):
    result = summarize([sample(n + 1, ratio) for n in range(count)])
    assert result.status == status and result.recent.count == count


def test_twenty_distinct_first_answers_and_previous_twenty():
    rows = [sample(n + 1, "0" if n < 20 else "1") for n in range(40)]
    rows += [sample(41, "0", question=40), sample(42, excluded="unanswered")]
    result = summarize(rows)
    assert result.recent.count == result.previous.count == 20
    assert result.recent.score_rate == 1 and result.previous.score_rate == 0
    assert result.first.count == 40 and result.repractice.count == 1
    assert result.unanswered_count == 1 and result.valid_count == 41


def test_equal_question_weight_and_valid_zero_not_missing():
    a = sample(1, "0.5")
    b = sample(2, "1")
    b = b.__class__(**{**b.__dict__, "score": Decimal(20), "max_score": Decimal(20)})
    result = summarize([a, b])
    assert result.first.score_rate == Decimal("0.75")
    assert summarize([sample(1)]).first.score_rate == 0
    assert summarize([]).first.score_rate is None


def test_multiple_points_exclusions_repractice_and_source_filters():
    rows = [
        sample(1, points=(UUID(int=1000), UUID(int=1001))),
        sample(2, "1", question=1, source="ai"),
        sample(3, excluded="failed"),
        sample(4, excluded="disputed"),
        sample(5, excluded="unanswered"),
    ]
    result = summarize(rows)
    assert result.first.count == 1 and result.repractice.count == 1
    assert result.excluded_count == 2 and result.unanswered_count == 1
    assert summarize(rows, source_kind="ai").first.count == 0
    assert summarize(rows, source_kind="ai").repractice.count == 1
    assert (
        summarize(rows, knowledge_point_id=UUID(int=1001)).status
        == "insufficient_evidence"
    )


def test_recent_not_cumulative_controls_reinforcement():
    rows = [sample(n + 1, "0" if n < 20 else "1") for n in range(40)]
    assert summarize(rows).status == "observed"
    assert summarize(rows).first.score_rate == Decimal("0.5")


def test_unanswered_does_not_consume_first_identity_and_repeated_errors_need_repractice():
    rows = [
        sample(1, excluded="unanswered"),
        sample(2, question=1),
        sample(3, question=1),
    ]
    result = summarize(rows)
    assert result.first.count == 1 and result.repractice.count == 1
    assert result.repeated_wrong_question_ids == [UUID(int=1)]
