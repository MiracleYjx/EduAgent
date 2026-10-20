"""Selection must preserve source/scope and disclose shortages, never manufacture a full set."""
import random
from uuid import uuid4

from backend.app.schemas.personal_practice import PracticeConditions
from backend.app.services.practice_selection import (
    SelectionCandidate,
    select_candidates,
)


def candidate(*, kp=None, attempted=False, wrong=False, source="uploaded", canonical=None):
    identity = uuid4()
    return SelectionCandidate(
        question_id=identity, canonical_id=canonical or identity, revision_id=uuid4(),
        knowledge_point_ids=frozenset(kp or []), attempted=attempted,
        original_wrong=wrong, source_kind=source,
    )


def test_twenty_eight_three_shortages_do_not_implicitly_repractice():
    rows = [candidate(attempted=i >= 3) for i in range(8)]
    result = select_candidates(rows, PracticeConditions(count=20), rng=random.Random(3))
    assert len(result.selected) == 3
    assert result.eligible_count == 8 and result.unseen_count == 3
    assert result.candidate_shortage == 12 and result.unseen_shortage == 17
    assert all(not row.attempted for row in result.selected)


def test_repractice_and_reduced_count_are_explicit_independent_choices():
    rows = [candidate(attempted=i >= 3) for i in range(8)]
    assert len(select_candidates(rows, PracticeConditions(count=8, allow_practiced=True)).selected) == 8
    assert len(select_candidates(rows, PracticeConditions(count=3)).selected) == 3
    assert len(select_candidates(rows, PracticeConditions(count=20)).selected) == 3


def test_balanced_multilabel_identity_takes_one_position_and_actual_coverage():
    a, b, c = uuid4(), uuid4(), uuid4()
    shared = candidate(kp=[a,b])
    rows = [shared, candidate(kp=[a,b], canonical=shared.canonical_id),
            candidate(kp=[c]), *[candidate(kp=[a]) for _ in range(6)]]
    result = select_candidates(rows, PracticeConditions(count=3, knowledge_point_ids=[a,b,c]), rng=random.Random(7))
    assert len(result.selected) == 3
    assert len({r.canonical_id for r in result.selected}) == 3
    assert set(result.coverage) == {a,b,c} and all(result.coverage.values())
    assert sum(result.coverage.values()) >= len(result.selected)


def test_uploaded_and_mixed_never_implicitly_include_ai():
    rows = [candidate(), candidate(source="ai")]
    for source in ("uploaded","mixed"):
        selected = select_candidates(rows, PracticeConditions(count=2, source=source)).selected
        assert [r.source_kind for r in selected] == ["uploaded"]
    chosen = select_candidates(rows, PracticeConditions(count=2, source="mixed"),
                               allowed_ai_ids={rows[1].question_id}).selected
    assert [r.source_kind for r in chosen] == ["uploaded","ai"]


def test_ai_source_does_not_include_uploaded_candidates():
    rows = [candidate(), candidate(source="ai")]
    result = select_candidates(rows, PracticeConditions(count=2,source="ai"))
    assert [r.source_kind for r in result.selected] == ["ai"]


def test_weak_mode_prefers_other_questions_on_weak_points_and_includes_original_wrong():
    weak, strong = uuid4(), uuid4()
    other = candidate(kp=[weak])
    wrong = candidate(kp=[weak],attempted=True,wrong=True)
    rows = [candidate(kp=[strong]), wrong, other, candidate(kp=[weak])]
    result = select_candidates(rows, PracticeConditions(count=3, mode="weak", allow_practiced=True),
                               weak_points={weak}, rng=random.Random(2))
    assert result.selected[0].original_wrong is False
    assert all(weak in row.knowledge_point_ids for row in result.selected)
    assert wrong in result.selected and result.original_wrong_count == 1


def test_empty_candidates_have_honest_zero_coverage_and_no_fallback():
    result = select_candidates([], PracticeConditions(count=20))
    assert result.selected == [] and result.eligible_count == 0
    assert result.candidate_shortage == result.unseen_shortage == 20


def test_balanced_without_explicit_points_covers_real_candidate_labels():
    a,b = uuid4(),uuid4()
    rows=[candidate(kp=[a]) for _ in range(6)]+[candidate(kp=[b])]
    result=select_candidates(rows,PracticeConditions(count=2),rng=random.Random(0))
    assert result.coverage=={a:1,b:1}

def test_knowledge_filter_precedes_canonical_representative_choice():
    a,b=uuid4(),uuid4()
    first=candidate(kp=[a])
    alias=candidate(kp=[b],canonical=first.canonical_id)
    result=select_candidates([first,alias],PracticeConditions(count=1,knowledge_point_ids=[b]),rng=random.Random(0))
    assert [row.question_id for row in result.selected]==[alias.question_id]
