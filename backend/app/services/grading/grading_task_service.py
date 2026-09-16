"""T054 阅卷应用层：置信度决策记录。

本模块是 M3 阅卷链路的应用/适配层，负责把既有评分核心的产出交给上层使用：

- :class:`DecisionRecordingPolicy`：实现 :class:`~backend.app.services.grading.subjective_grader.ConfidencePolicyLike`，
  复用既有 :meth:`ConfidencePolicy.evaluate` 的实际判定记录本次 :class:`ConfidenceDecision`，
  再把 ``review_status`` 交给既有 :meth:`ConfidencePolicy.apply` 回填。

约束（plan.md §5.4、FR-035）：

- 每次评分独享策略实例，不使用全局 ``last_decision``、不在汇总或查询阶段按最新阈值重判
  历史结果；阈值需求由 :class:`ConfidencePolicy` 自身负责。
- 不修改既有评分核心；已有人工结论的结果仍由 :meth:`ConfidencePolicy.apply` 拒绝覆盖。
- T056 将在同一模块扩展阅卷执行器与结果存储边界，本阶段只提供决策记录能力。
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.app.core.config import AppSettings
from backend.app.schemas.ai import GradingResult
from backend.app.services.grading.confidence_policy import (
    ConfidenceDecision,
    ConfidencePolicy,
)


@dataclass(frozen=True, slots=True)
class RecordedDecision:
    """一次评分产生的决策及其答案标识。"""

    answer_id: str | None
    decision: ConfidenceDecision


class DecisionRecordingPolicy:
    """记录置信度决策的策略包装，可注入 :class:`SubjectiveGrader`。

    :param policy: 既有置信度策略；``None`` 时按 ``settings`` 构造。
    :param settings: 运行配置；仅在未传入 ``policy`` 时用于解析阈值。
    """

    def __init__(
        self,
        policy: ConfidencePolicy | None = None,
        *,
        settings: AppSettings | None = None,
    ) -> None:
        self._policy = policy if policy is not None else ConfidencePolicy(settings=settings)
        self._records: list[RecordedDecision] = []

    @property
    def decisions(self) -> tuple[RecordedDecision, ...]:
        """按记录顺序返回本次实例产生的全部决策。"""

        return tuple(self._records)

    def decision_for(self, answer_id: str) -> ConfidenceDecision | None:
        """返回指定答案最近一次的决策；没有记录时返回 ``None``。"""

        for record in reversed(self._records):
            if record.answer_id == answer_id:
                return record.decision
        return None

    def decisions_by_answer_id(self) -> dict[str, ConfidenceDecision]:
        """返回 ``answer_id`` 到决策的映射，供汇总阶段消费。

        缺少 ``answer_id`` 的记录无法定位题目，只保留在 :attr:`decisions` 中，
        由汇总阶段按身份缺失处理。
        """

        resolved: dict[str, ConfidenceDecision] = {}
        for record in self._records:
            if record.answer_id is not None:
                resolved[record.answer_id] = record.decision
        return resolved

    def apply(self, result: GradingResult) -> GradingResult:
        """执行置信度检查、记录决策并返回回填后的结果。

        仅当既有策略接受该结果（已通过结构化校验且没有人工结论）时才记录决策，
        避免把被拒绝的输入当作已完成检查。
        """

        decision = self._policy.evaluate(
            result.confidence,
            question_type=result.question_type,
        )
        updated = self._policy.apply(result)
        self._records.append(
            RecordedDecision(answer_id=result.answer_id, decision=decision)
        )
        return updated


__all__ = [
    "DecisionRecordingPolicy",
    "RecordedDecision",
]
