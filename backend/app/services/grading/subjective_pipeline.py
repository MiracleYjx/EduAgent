"""T056 主观题评分链路适配：把既有评分组件装配为可注入的主观题评分器。

本模块**不重写评分逻辑**，只做装配：复用 T050 的检索上下文组装
（:func:`~backend.app.services.grading.grading_context.build_grading_context`）、T052 的
:class:`~backend.app.services.grading.subjective_grader.SubjectiveGrader` 与 T053 的
:class:`~backend.app.services.grading.confidence_policy.ConfidencePolicy`，把
:class:`~backend.app.services.grading.grading_task_service.DefaultScoringPipeline` 缺少的
``subjective_scorer`` 接上。

关键约定：

- **决策记录**：包装既有的
  :class:`~backend.app.services.grading.grading_task_service.DecisionRecordingPolicy`，
  记录**当次实际**执行的置信度决策（阈值、置信度、是否复核、复核状态、评分状态、原因），
  供仓储写入决策快照；保存失败或未产生决策时显式失败，不按当前配置重算历史结论。
- **会话所有权**：每次评分自建并关闭会话，只用于读取评分上下文；评分调用期间不持有写事务，
  LLM 调用不阻塞其它写者。
- **错误保真**：既有 :class:`SubjectiveGradingError` / :class:`GradingContextError` 的业务
  错误码与 ``retryable`` 原样保留为 :class:`GradingTaskError`，不压成通用任务失败。
- **不伪造分数**：Provider 或 Embedding 未配置时按既有错误码显式失败。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Final

from sqlalchemy.orm import Session

from backend.app.ai.embedding.base import BaseEmbeddingProvider
from backend.app.ai.llm.base import BaseLLMProvider
from backend.app.ai.retrieval.base import DEFAULT_TOP_K, BaseRetriever
from backend.app.ai.retrieval.reranker import BaseReranker
from backend.app.core.config import AppSettings
from backend.app.schemas.ai import GradingResult
from backend.app.services.grading.confidence_policy import (
    ConfidenceDecision,
    ConfidencePolicy,
)
from backend.app.services.grading.grading_context import (
    GradingContextError,
    GradingInputError,
    SubjectiveGradingSource,
)
from backend.app.services.grading.grading_task_service import (
    GRADING_TASK_FAILED,
    DecisionRecordingPolicy,
    GradingExecutionNotReadyError,
    GradingTargetAnswer,
    GradingTaskError,
    SubmissionSnapshot,
)
from backend.app.services.grading.subjective_grader import (
    SubjectiveGrader,
    SubjectiveGradingError,
)

#: 主观题评分器签名：单题目标 + 答卷快照 → （评分结果, 当次决策）。
SubjectiveScorer = Callable[
    [SubmissionSnapshot, GradingTargetAnswer],
    tuple[GradingResult, ConfidenceDecision],
]

#: 未记录决策时的说明；不允许把“无法证明已执行置信度检查”的结果写入库。
MISSING_DECISION_MESSAGE: Final[str] = (
    "主观题评分未记录本次置信度决策，拒绝作为已复核结果入库。"
)


def build_subjective_scorer(
    *,
    session_factory: Callable[[], Session],
    settings: AppSettings | None = None,
    provider: BaseLLMProvider | None = None,
    policy: ConfidencePolicy | None = None,
    retriever: BaseRetriever | None = None,
    reranker: BaseReranker | None = None,
    embedding_provider: BaseEmbeddingProvider | None = None,
    top_k: int = DEFAULT_TOP_K,
) -> SubjectiveScorer:
    """构造 :class:`DefaultScoringPipeline` 使用的主观题评分器。

    :param session_factory: 会话工厂；每次评分自建并在结束后关闭，不复用请求作用域会话。
    :param settings: 运行配置；``None`` 时由既有组件按配置解析。
    :param provider: 评分 Provider；``None`` 时由既有工厂按配置解析，未就绪时显式失败。
    :param policy: 置信度策略；每次评分内部包一层记录策略，保证决策与结果一一对应。
    """

    def score(
        snapshot: SubmissionSnapshot,
        target: GradingTargetAnswer,
    ) -> tuple[GradingResult, ConfidenceDecision]:
        """执行一次主观题评分；内部使用同步事件循环，须在无事件循环的调用线程执行。"""

        recording = DecisionRecordingPolicy(policy=policy, settings=settings)
        grader = SubjectiveGrader(provider=provider, policy=recording)
        session = session_factory()
        try:
            source = build_subjective_source(snapshot, target)
            result = asyncio.run(
                grader.grade(
                    session,
                    source,
                    max_score=target.max_score,
                    top_k=top_k,
                    retriever=retriever,
                    reranker=reranker,
                    embedding_provider=embedding_provider,
                    settings=settings,
                )
            )
        except (SubjectiveGradingError, GradingContextError) as error:
            raise as_task_error(error) from None
        finally:
            session.close()
        decision = recording.decision_for(target.answer_id)
        if decision is None:
            raise GradingExecutionNotReadyError(MISSING_DECISION_MESSAGE)
        return result, decision

    return score


def build_subjective_source(
    snapshot: SubmissionSnapshot,
    target: GradingTargetAnswer,
) -> SubjectiveGradingSource:
    """由答卷快照与单题目标组装 §5.3 要求的主观题评分输入。

    题库与答卷字段缺失（标准答案、评分标准、学生作答）时构造器会按既有错误码显式失败，
    这里不做占位填充。
    """

    student_answer = target.student_answer
    if isinstance(student_answer, Mapping):
        raise GradingInputError(
            "字典形态的学生答案缺少键语义与顺序约定，不能用于主观题评分。"
        )
    return SubjectiveGradingSource(
        question_type=target.question_type,
        course_id=snapshot.course_id,
        question_content=target.content,
        reference_answer=target.reference_answer or "",
        student_answer=student_answer,
        scoring_rubric=target.scoring_rubric,
        knowledge_points=target.knowledge_points,
        question_id=target.question_id,
        answer_id=target.answer_id,
        submission_id=snapshot.submission_id,
    )


def as_task_error(error: Exception) -> GradingTaskError:
    """把评分组件的业务错误映射为任务错误，保留错误码与 ``retryable``。"""

    code = str(getattr(error, "error_code", GRADING_TASK_FAILED))
    retryable = bool(getattr(error, "retryable", False))
    source_code = getattr(error, "source_code", None)
    mapped = GradingTaskError(
        f"主观题评分失败（来源码 {code}）。",
        retryable=retryable,
        source_code=str(source_code) if source_code else code,
    )
    mapped.error_code = code
    return mapped


__all__ = [
    "MISSING_DECISION_MESSAGE",
    "SubjectiveScorer",
    "as_task_error",
    "build_subjective_scorer",
    "build_subjective_source",
]
