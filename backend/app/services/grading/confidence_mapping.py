"""置信度决策与序列化快照的字段转换。"""

from backend.app.schemas.grading import ConfidenceDecisionDTO
from backend.app.services.grading.confidence_policy import ConfidenceDecision


def confidence_decision_snapshot(decision: ConfidenceDecision) -> ConfidenceDecisionDTO:
    """保留当次决策的六个字段，不重新判定阈值或复核状态。"""

    return ConfidenceDecisionDTO(
        confidence=decision.confidence,
        threshold=decision.threshold,
        requires_review=decision.requires_review,
        review_status=decision.review_status,
        grading_status=decision.grading_status,
        reason=decision.reason,
    )
