"""M0 Benchmark 生成器的单元测试。"""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from backend.app.schemas.ai import QuestionType
from scripts.generate_synthetic_benchmark import (
    SyntheticBenchmarkDataset,
    generate_synthetic_benchmark,
)


def test_benchmark_seed_cases_are_reproducible_and_complete() -> None:
    """验证固定种子可以稳定生成至少十条完整的 Benchmark 案例。"""

    first = generate_synthetic_benchmark(count=10, seed=20260904)
    second = generate_synthetic_benchmark(count=10, seed=20260904)

    assert first == second

    dataset = SyntheticBenchmarkDataset.model_validate(first)

    assert dataset.metadata.record_count >= 10
    assert len(dataset.cases) >= 10
    assert len({case.case_id for case in dataset.cases}) == len(dataset.cases)
    assert {case.question_type for case in dataset.cases} == {
        QuestionType.SINGLE_CHOICE,
        QuestionType.TRUE_FALSE,
        QuestionType.SHORT_ANSWER,
    }

    for case in dataset.cases:
        assert case.case_id
        assert case.question
        assert case.scoring_criteria
        assert case.reference_answer
        assert case.student_answer
        assert case.reason
        assert case.knowledge_points
        assert case.validation_status
        assert 0 <= case.score <= case.max_score


@pytest.mark.parametrize(
    "field_name",
    (
        "case_id",
        "question_type",
        "question",
        "scoring_criteria",
        "reference_answer",
        "student_answer",
        "reason",
        "knowledge_points",
        "validation_status",
    ),
)
def test_benchmark_case_rejects_missing_required_field(field_name: str) -> None:
    """验证 Benchmark 案例缺少任一必填字段时会被拒绝。"""

    dataset = generate_synthetic_benchmark(count=10, seed=20260904)
    invalid_dataset = deepcopy(dataset)
    invalid_dataset["cases"][0].pop(field_name)

    with pytest.raises(ValidationError):
        SyntheticBenchmarkDataset.model_validate(invalid_dataset)


def test_benchmark_case_rejects_score_above_maximum() -> None:
    """验证 Benchmark 案例得分超过满分时会被拒绝。"""

    dataset = generate_synthetic_benchmark(count=10, seed=20260904)
    invalid_dataset = deepcopy(dataset)
    invalid_dataset["cases"][0]["score"] = invalid_dataset["cases"][0]["max_score"] + 1

    with pytest.raises(ValidationError, match="得分不能超过"):
        SyntheticBenchmarkDataset.model_validate(invalid_dataset)
