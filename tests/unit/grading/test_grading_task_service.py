"""T054 阅卷应用层单元测试：置信度决策记录与每次评分独享状态。

TCR（2026-09-16，T054 / B02）：``ConfidencePolicy.apply()`` 只回填 ``review_status``，
不把 :class:`ConfidenceDecision` 交给调用方，导致汇总阶段无法区分“已执行置信度检查且
自动接受”与“默认 Not Required”。修复方式是在应用层提供实现 ``ConfidencePolicyLike``
的记录策略，复用既有 ``evaluate()`` 判定并保存本次决策；先新增失败用例，再实现
``backend/app/services/grading/grading_task_service.py`` 中的 ``DecisionRecordingPolicy``。

测试只检查业务合同（FR-035、plan.md §5.4、data-model.md 状态图），不修改既有评分核心，
不使用全局 last_decision 或跨评分共享状态。
"""

from __future__ import annotations

import pytest

from backend.app.domain.enums import QuestionType
from backend.app.schemas.ai import GradingResult
from backend.app.services.grading.confidence_policy import (
    ConfidencePolicy,
    ManualReviewStateError,
    NotValidatedResultError,
)
from backend.app.services.grading.grading_task_service import (
    DecisionRecordingPolicy,
)
from backend.app.services.grading.subjective_grader import SubjectiveGrader
from tests.unit.settings_helpers import build_test_settings


def _result(
    answer_id: str | None = "answer-1",
    *,
    question_type: QuestionType = QuestionType.SHORT_ANSWER,
    confidence: float = 0.9,
    review_status: str = "Not Required",
    validation_status: str = "Validated",
) -> GradingResult:
    """构造单题评分结果 DTO。"""

    return GradingResult(
        question_type=question_type,
        score=6.0,
        max_score=10.0,
        reason="评分理由。",
        correct_points=["要点一"],
        missing_knowledge_points=[],
        knowledge_points=["知识点 A"],
        suggestions=["继续练习。"],
        confidence=confidence,
        validation_status=validation_status,
        review_status=review_status,
        answer_id=answer_id,
        submission_id="submission-1",
    )


def _settings() -> object:
    """构造阈值 0.8 的测试配置。"""

    return build_test_settings(confidence_threshold=0.8)


def test_policy_records_decision_and_returns_policy_result() -> None:
    """策略回填 review_status 的同时保存本次决策。"""

    settings = _settings()
    policy = DecisionRecordingPolicy(
        ConfidencePolicy(threshold=0.9),
        settings=settings,  # type: ignore[arg-type]
    )

    updated = policy.apply(_result(confidence=0.95))

    assert updated.review_status == "Not Required"
    recorded = policy.decision_for("answer-1")
    assert recorded is not None
    assert recorded.threshold == 0.9
    assert recorded.requires_review is False
    assert recorded.grading_status == "Accepted"


def test_low_confidence_decision_marks_pending_review() -> None:
    """低置信度主观题记录待复核决策并回填 Pending Review。"""

    settings = _settings()
    policy = DecisionRecordingPolicy(settings=settings)  # type: ignore[arg-type]

    updated = policy.apply(_result(confidence=0.4))

    assert updated.review_status == "Pending Review"
    recorded = policy.decision_for("answer-1")
    assert recorded is not None
    assert recorded.requires_review is True
    assert recorded.threshold == 0.8


def test_objective_result_is_accepted_without_review() -> None:
    """客观题按状态图直接接受，不进入主观题置信度复核。"""

    settings = _settings()
    policy = DecisionRecordingPolicy(settings=settings)  # type: ignore[arg-type]

    policy.apply(
        _result(
            question_type=QuestionType.SINGLE_CHOICE,
            confidence=0.1,
        )
    )

    recorded = policy.decision_for("answer-1")
    assert recorded is not None
    assert recorded.requires_review is False
    assert recorded.review_status == "Not Required"


def test_recorded_decisions_are_isolated_per_instance() -> None:
    """每次评分独享策略实例，决策不跨实例共享。"""

    settings = _settings()
    first = DecisionRecordingPolicy(settings=settings)  # type: ignore[arg-type]
    second = DecisionRecordingPolicy(settings=settings)  # type: ignore[arg-type]

    first.apply(_result("answer-1", confidence=0.95))

    assert first.decision_for("answer-1") is not None
    assert second.decision_for("answer-1") is None
    assert len(second.decisions) == 0
    assert second.decisions_by_answer_id() == {}


def test_human_decided_result_is_rejected_and_not_recorded() -> None:
    """已有人工结论的结果不得被自动策略覆盖，也不产生新记录。"""

    settings = _settings()
    policy = DecisionRecordingPolicy(settings=settings)  # type: ignore[arg-type]

    with pytest.raises(ManualReviewStateError):
        policy.apply(_result(review_status="Modified"))

    assert policy.decisions == ()


def test_unvalidated_result_is_rejected() -> None:
    """未通过结构化校验的结果不得记录决策。"""

    settings = _settings()
    policy = DecisionRecordingPolicy(settings=settings)  # type: ignore[arg-type]

    with pytest.raises(NotValidatedResultError):
        policy.apply(_result(validation_status="Pending"))

    assert policy.decisions == ()


def test_missing_answer_identity_is_recorded_as_none() -> None:
    """结果缺少 answer_id 时仍如实记录，交由汇总阶段判定身份缺失。"""

    settings = _settings()
    policy = DecisionRecordingPolicy(settings=settings)  # type: ignore[arg-type]

    policy.apply(_result(None, confidence=0.95))

    assert len(policy.decisions) == 1
    assert policy.decisions[0].answer_id is None
    assert policy.decisions_by_answer_id() == {}


def test_policy_satisfies_subjective_grader_injection_point() -> None:
    """记录策略可作为 ``ConfidencePolicyLike`` 注入既有评分器。"""

    settings = _settings()
    policy = DecisionRecordingPolicy(settings=settings)  # type: ignore[arg-type]
    grader = SubjectiveGrader(policy=policy)

    assert callable(policy.apply)
    assert grader is not None
