"""AI 阅卷服务包。

本包承载 M3「AI 阅卷」阶段的服务边界：

- :mod:`backend.app.services.grading.question_router`：按题目自身题型分流 Objective 与
  Subjective；
- :mod:`backend.app.services.grading.objective_grader`：不调用 LLM 的客观题确定性规则评分。

本阶段只提供纯逻辑服务，不引入 RAG、Embedding、Rerank、LLM Provider 或数据库依赖。
"""

from __future__ import annotations

from backend.app.schemas.grading import (
    ConfidenceDecisionDTO,
    ExamResultDTO,
    ExamResultStatus,
    ExpectedAnswer,
    QuestionResultDTO,
    SubmissionContext,
)
from backend.app.services.grading.grading_task_service import (
    DecisionRecordingPolicy,
    RecordedDecision,
)
from backend.app.services.grading.result_aggregator import (
    GradingAggregationError,
    ResultAggregator,
)

__all__: list[str] = [
    "ConfidenceDecisionDTO",
    "DecisionRecordingPolicy",
    "ExamResultDTO",
    "ExamResultStatus",
    "ExpectedAnswer",
    "GradingAggregationError",
    "QuestionResultDTO",
    "RecordedDecision",
    "ResultAggregator",
    "SubmissionContext",
]
