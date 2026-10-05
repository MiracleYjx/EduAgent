"""T181 deterministic current-final observations, TCR §36."""

from decimal import Decimal

from backend.app.services.diagnosis_service import DiagnosisService
from tests.unit.services.test_diagnosis_service import (
    KP_A,
    KP_B,
    StubSuggestionProvider,
    _aggregate,
    _expected,
    _result,
)


def test_same_formula_uses_only_verified_published_items_without_model():
    result = _aggregate(
        [
            _expected(1, "a", knowledge_points=(KP_A, KP_B)),
            _expected(2, "b", knowledge_points=(KP_A,)),
        ],
        [_result("a", score="4"), _result("b", score="10")],
    )
    provider = StubSuggestionProvider(error=RuntimeError("must not call a model"))
    entries, weak, unknown = DiagnosisService(
        provider=provider
    ).final_learning_observations(result, [result.items[0]])
    assert [e.knowledge_point for e in entries] == [KP_A, KP_B]
    assert all(
        e.awarded_score == Decimal(4)
        and e.max_score == Decimal(10)
        and e.mastery == Decimal("0.40")
        for e in entries
    )
    assert [e.knowledge_point for e in weak] == [KP_A, KP_B] and unknown == []
    assert provider.calls == []
    assert result.final_total_score == Decimal(
        14
    )  # read projection does not rewrite stored totals


def test_whole_exam_not_final_never_infers_weakness_from_partial_acceptance():
    result = _aggregate(
        [_expected(1, "a"), _expected(2, "b")], [_result("a", score="0")]
    )
    provider = StubSuggestionProvider()
    assert DiagnosisService(provider=provider).final_learning_observations(
        result, [result.items[0]]
    ) == ([], [], [])
    assert provider.calls == []


def test_final_unknown_tags_remain_insufficient_not_zero_mastery():
    result = _aggregate(
        [_expected(1, "a", knowledge_points=())], [_result("a", score="0")]
    )
    entries, weak, unknown = DiagnosisService().final_learning_observations(
        result, result.items
    )
    assert entries == [] and weak == [] and unknown == ["a"]
