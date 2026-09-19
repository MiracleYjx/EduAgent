"""Grading Agent：阅卷编排（T069）。

契约依据：``.specify/plan.md`` §4 Agent 分工与 §5 图节点（``Classify Question``、
``Rule Grade``、``Retrieve Context``、``Grade``、``Structured Validation``、``Confidence Check``）、
``.specify/contracts/agent-workflow.md``，以及 FR-030（客观题确定性、不调用 LLM）、
FR-031/FR-032（主观题必须带检索上下文并结构化输出）、FR-035/FR-036（置信度阈值与待复核）。

职责边界：

- **只编排，不重算**：客观题调用 M3 :class:`ObjectiveGrader`；主观题只组装
  :class:`SubjectiveGradingSource` 并调用一次 :class:`SubjectiveGrader`，课程上下文、Hybrid 检索
  与 Rerank 全部由该评分器内部完成（本模块不重复构建上下文）；整卷汇总调用 M3
  :class:`ResultAggregator`。本模块不包含任何分数、舍入或比例计算逻辑。
- **唯一循环与整卷合同**：``score_async`` 是唯一的整卷循环实现，``aggregate()`` 只调用一次，
  返回非空 ``exam_result``，任一题失败即抛出 :class:`GradingAgentError`，**不返回部分成功**；
  ``score`` 是 M3 ``ScoringPipeline`` 同步合同的包装（无运行中事件循环时用 ``asyncio.run``）。
  不修改 M3 ``DefaultScoringPipeline``：它仍是 T056 装配路径，本模块是 Agent 级、可异步的实现。
- **配置装配与优先级（H02）**：优先级固定为“显式组件 > 逐次 ``settings`` > 构造期 ``settings`` >
  全局配置”。四个公开入口（``grade_answer``/``grade_answer_async``/``score``/``score_async``）都接受
  可选 ``settings``（不传时行为不变）；生效的同一个 ``AppSettings`` 同时用于决策策略与
  ``SubjectiveGrader.grade``，Provider/Embedding/Retriever/Reranker 由 M3 既有链路按该配置解析，
  本模块不再另建第二套装配。
- **逐题入口是 Graph 合同（H01）**：T072 的图节点必须逐题调用 :meth:`GradingAgent.grade_answer_async`
  （或同步 :meth:`GradingAgent.grade_answer`）并自行推进题序；``score_async`` 只是既有
  ``ScoringPipeline`` 的整卷兼容入口，节点内不得调用它后再自行逐题循环。逐题入口每题只调用一次
  评分服务，``current_answer_id`` 恒取该题 ``grading_result.answer_id``；重复 ``answer_id`` 由 M3
  ``ResultAggregator`` 拒绝（``DuplicateResultError``），本模块不另立第二套去重规则。
- **异步边界**：``*_async`` 直接 ``await SubjectiveGrader.grade()``；不调用内含 ``asyncio.run``
  的 ``build_subjective_scorer()``。同步入口在已有事件循环时抛
  ``GRADING_AGENT_ASYNC_REQUIRED``，绝不嵌套 ``asyncio.run``（与 T044 的做法一致）。
- **置信度语义**：客观题按状态图直接接受，**不**调用 ``ConfidencePolicy``、**不**进入主观题
  决策映射；只有主观题经 ``DecisionRecordingPolicy`` 记录当次实际阈值与决策，并生成 T065 决策快照。
- **专属一致性校验**：只接受 ``Validated`` 结果，且答案标识、答卷标识、题型与满分必须与阅卷
  目标一致；``confidence`` 与决策快照必须一致，不一致显式失败而不静默修正。
- **配置装配**：优先级为“显式组件 > 显式 settings 工厂 > 全局配置”，同一个 ``AppSettings``
  贯穿 Provider、Embedding、Retriever、Reranker、置信度策略与 ``SubjectiveGrader.grade``。
  会话所有权：``grade_answer*`` 使用调用方会话（不关闭）；``score*`` 需要 ``session_factory``，
  每题自建并在结束后关闭（与 T056 一致）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from sqlalchemy.orm import Session

from backend.app.ai.agents.invocation import AgentInvocation, normalize_trace_context
from backend.app.ai.agents.state import (
    AgentError,
    AgentOutput,
    AgentStatus,
    AgentType,
    confidence_decision_snapshot,
)
from backend.app.ai.embedding.base import BaseEmbeddingProvider
from backend.app.ai.llm.base import BaseLLMProvider
from backend.app.ai.retrieval.base import DEFAULT_TOP_K, BaseRetriever
from backend.app.ai.retrieval.reranker import BaseReranker
from backend.app.core.config import AppSettings
from backend.app.core.retry_policy import ProviderExecutionError
from backend.app.domain.enums import GradingMode, QuestionType, ValidationStatus
from backend.app.schemas.ai import GradingResult
from backend.app.schemas.grading import ExamResultDTO
from backend.app.services.grading.confidence_policy import (
    ConfidenceDecision,
    ConfidencePolicy,
    ConfidencePolicyError,
)
from backend.app.services.grading.grading_context import (
    GradingContextError,
    SubjectiveGradingSource,
)
from backend.app.services.grading.grading_task_service import (
    DecisionRecordingPolicy,
    GradingOutcome,
    GradingTargetAnswer,
    SubmissionSnapshot,
)
from backend.app.services.grading.objective_grader import (
    ObjectiveGrader,
    ObjectiveGradingError,
)
from backend.app.services.grading.question_router import (
    QuestionRouter,
    normalize_question_type,
)
from backend.app.services.grading.result_aggregator import ResultAggregator
from backend.app.services.grading.subjective_grader import (
    SUBJECTIVE_GRADING_PROMPT_VERSION,
    SubjectiveGrader,
    SubjectiveGradingError,
)
from backend.app.services.grading.subjective_pipeline import build_subjective_source

#: 输入不是可阅卷的答卷快照或单题目标。
GRADING_AGENT_INVALID_INPUT: Final[str] = "GRADING_AGENT_INVALID_INPUT"
#: 当前线程已有事件循环，必须改用异步入口。
GRADING_AGENT_ASYNC_REQUIRED: Final[str] = "GRADING_AGENT_ASYNC_REQUIRED"
#: 主观题评分需要只读会话，但既未传入会话也没有会话工厂。
GRADING_AGENT_MISSING_SESSION: Final[str] = "GRADING_AGENT_MISSING_SESSION"
#: 题型无法分流（未知或不在支持范围）。
GRADING_AGENT_UNSUPPORTED_QUESTION_TYPE: Final[str] = "GRADING_AGENT_UNSUPPORTED_QUESTION_TYPE"
#: 评分结果未通过结构化校验。
GRADING_AGENT_RESULT_NOT_VALIDATED: Final[str] = "GRADING_AGENT_RESULT_NOT_VALIDATED"
#: 评分结果的答案/答卷/题型/满分与阅卷目标不一致。
GRADING_AGENT_RESULT_MISMATCH: Final[str] = "GRADING_AGENT_RESULT_MISMATCH"
#: 置信度事实与决策快照不一致。
GRADING_AGENT_DECISION_MISMATCH: Final[str] = "GRADING_AGENT_DECISION_MISMATCH"
#: 主观题评分服务调用失败（未映射到既有业务错误码的异常）。
GRADING_AGENT_SUBJECTIVE_FAILED: Final[str] = "GRADING_AGENT_SUBJECTIVE_FAILED"
#: 主观题评分完成但未记录本次置信度决策。
GRADING_AGENT_MISSING_DECISION: Final[str] = "GRADING_AGENT_MISSING_DECISION"


class GradingAgentError(RuntimeError):
    """阅卷编排失败；保留脱敏错误码、来源码与可重试语义。"""

    #: 脱敏错误码；默认非法输入，可按来源错误保真覆盖。
    error_code: str = GRADING_AGENT_INVALID_INPUT
    #: 是否可重试；默认不可重试，仅按来源错误保真覆盖。
    retryable: bool = False
    #: 上游来源错误码；无来源错误时保持 ``None``。
    source_code: str | None = None

    def __init__(
        self,
        detail: str,
        *,
        error_code: str | None = None,
        retryable: bool | None = None,
        source_code: str | None = None,
    ) -> None:
        if error_code is not None:
            self.error_code = error_code
        super().__init__(f"{self.error_code}：{detail}")
        self.detail = detail
        if retryable is not None:
            self.retryable = retryable
        self.source_code = source_code


class ObjectiveGraderLike(Protocol):
    """客观题评分服务的最小协议（M3 ``ObjectiveGrader`` 满足）。"""

    def grade(self, **kwargs: Any) -> GradingResult: ...


class SubjectiveGraderLike(Protocol):
    """主观题评分服务的最小协议（M3 ``SubjectiveGrader`` 满足，异步入口）。

    签名按 M3 ``SubjectiveGrader.grade`` 逐项固定：课程上下文、Hybrid 检索与 Rerank 由该服务
    内部完成，Agent 只组装 ``SubjectiveGradingSource`` 并通过这些关键字传入同一个 ``AppSettings``
    与检索组件；本协议不带 ``**kwargs``，避免 Agent 传入未被既有服务实现的参数。
    """

    async def grade(
        self,
        session: Session,
        source: SubjectiveGradingSource,
        *,
        max_score: Any,
        top_k: int = DEFAULT_TOP_K,
        retriever: BaseRetriever | None = None,
        reranker: BaseReranker | None = None,
        embedding_provider: BaseEmbeddingProvider | None = None,
        require_context: bool = True,
        settings: AppSettings | None = None,
    ) -> GradingResult: ...


class ResultAggregatorLike(Protocol):
    """整卷汇总服务的最小协议（M3 ``ResultAggregator`` 满足）。"""

    def aggregate(
        self,
        context: Any,
        *,
        results: Sequence[GradingResult],
        decisions: Mapping[str, ConfidenceDecision] | None = None,
    ) -> ExamResultDTO: ...


@dataclass(frozen=True, slots=True)
class _AnswerOutcome:
    """单题编排的内部结果：Agent 输出 + 既有服务产物 + 当次决策。"""

    output: AgentOutput
    result: GradingResult | None = None
    decision: ConfidenceDecision | None = None


def _ensure_no_running_loop() -> None:
    """同步入口的循环守卫：事件循环内必须改用异步入口。"""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise GradingAgentError(
        "当前线程已有事件循环，请使用 await score_async(...) / grade_answer_async(...)。",
        error_code=GRADING_AGENT_ASYNC_REQUIRED,
    )


def _resolve_provider_model(provider: BaseLLMProvider | None) -> str | None:
    """读取 Provider 实际提供的模型标识；无法获取时返回 ``None``，不伪造。"""

    if provider is None:
        return None
    for attribute in ("model_name", "model"):
        value = getattr(provider, attribute, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


class GradingAgent:
    """阅卷编排 Agent：分流题型、调用既有评分服务、执行置信度检查。

    :param objective_grader: 客观题评分服务；``None`` 时使用 M3 :class:`ObjectiveGrader`。
    :param subjective_grader: 主观题评分服务；``None`` 时按同一个 ``AppSettings`` 构造
        :class:`SubjectiveGrader`（Provider 未就绪时由既有错误码显式失败）。
    :param aggregator: 整卷汇总服务；``None`` 时使用 M3 :class:`ResultAggregator`。
    :param policy: 置信度策略；``None`` 时按同一个 ``AppSettings`` 构造。
    :param session_factory: 整卷编排使用的会话工厂；每题自建并关闭。
    """

    def __init__(
        self,
        *,
        objective_grader: ObjectiveGraderLike | None = None,
        subjective_grader: SubjectiveGraderLike | None = None,
        aggregator: ResultAggregatorLike | None = None,
        router: QuestionRouter | None = None,
        policy: ConfidencePolicy | None = None,
        settings: AppSettings | None = None,
        provider: BaseLLMProvider | None = None,
        retriever: BaseRetriever | None = None,
        reranker: BaseReranker | None = None,
        embedding_provider: BaseEmbeddingProvider | None = None,
        top_k: int = DEFAULT_TOP_K,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        self._objective_grader = objective_grader if objective_grader is not None else ObjectiveGrader()
        self._subjective_grader = subjective_grader
        self._aggregator = aggregator if aggregator is not None else ResultAggregator()
        self._router = router if router is not None else QuestionRouter()
        self._policy = policy
        self._settings = settings
        self._provider = provider
        self._retriever = retriever
        self._reranker = reranker
        self._embedding_provider = embedding_provider
        self._top_k = top_k
        self._session_factory = session_factory

    # ------------------------------------------------------------------ 公开入口

    def grade_answer(
        self,
        snapshot: SubmissionSnapshot,
        target: GradingTargetAnswer,
        *,
        request_id: str,
        workflow_id: str | None = None,
        session: Session | None = None,
        settings: AppSettings | None = None,
    ) -> AgentInvocation:
        """同步单题入口；事件循环内必须使用 :meth:`grade_answer_async`。

        本入口是 T072 图节点的逐题合同：一次调用只处理一道题，调用方自行维护题序；
        不接受整卷快照的批量语义（批量兼容入口是 :meth:`score`）。
        ``settings`` 为本次调用的可选配置覆盖，优先于构造期配置。
        """

        request_id, workflow_id = normalize_trace_context(request_id, workflow_id)
        _ensure_no_running_loop()
        outcome = asyncio.run(
            self._grade_answer(snapshot, target, session=session, settings=settings)
        )
        return AgentInvocation(request_id=request_id, workflow_id=workflow_id, output=outcome.output)

    async def grade_answer_async(
        self,
        snapshot: SubmissionSnapshot,
        target: GradingTargetAnswer,
        *,
        request_id: str,
        workflow_id: str | None = None,
        session: Session | None = None,
        settings: AppSettings | None = None,
    ) -> AgentInvocation:
        """异步单题入口：直接 ``await`` 主观题评分，不嵌套 ``asyncio.run``。

        与 :meth:`grade_answer` 同为 T072 图节点的逐题合同：每题调用一次，调用方负责推进题序
        与下一次迭代；图节点不得改用整卷 :meth:`score_async` 后再自行逐题循环。
        ``settings`` 为本次调用的可选配置覆盖，优先于构造期配置。
        """

        request_id, workflow_id = normalize_trace_context(request_id, workflow_id)
        outcome = await self._grade_answer(snapshot, target, session=session, settings=settings)
        return AgentInvocation(request_id=request_id, workflow_id=workflow_id, output=outcome.output)

    def score(
        self,
        snapshot: SubmissionSnapshot,
        *,
        settings: AppSettings | None = None,
    ) -> GradingOutcome:
        """既有的 ``ScoringPipeline`` 同步合同；整卷编排的同步包装。

        仅供整体阅卷调用（批量兼容入口），不是 T072 图节点接口。
        ``settings`` 为本次调用的可选配置覆盖，优先于构造期配置。
        """

        _ensure_no_running_loop()
        return asyncio.run(self.score_async(snapshot, settings=settings))

    async def score_async(
        self,
        snapshot: SubmissionSnapshot,
        *,
        settings: AppSettings | None = None,
    ) -> GradingOutcome:
        """整卷编排：逐题分流 → 既有评分服务 → 汇总一次；任一步失败即抛出。

        不返回部分成功：任一题失败时抛出 :class:`GradingAgentError`，调用方不得把不完整结果
        当作整卷成绩；汇总仍由 M3 :class:`ResultAggregator` 完成（含低置信度不计分规则）。

        本入口是整卷兼容层（实现既有 ``ScoringPipeline`` 合同）：T072 图节点逐题调用
        :meth:`grade_answer_async`，不得先调用本入口再自行逐题循环，否则同一题会被评分两次。
        答案集合重复时由 M3 :class:`ResultAggregator` 报 ``DuplicateResultError``，不在此另设去重。
        """

        if not isinstance(snapshot, SubmissionSnapshot):
            raise GradingAgentError(
                "整卷编排需要 SubmissionSnapshot。",
                error_code=GRADING_AGENT_INVALID_INPUT,
            )
        results: list[GradingResult] = []
        decisions: dict[str, ConfidenceDecision] = {}
        for target in snapshot.answers:
            outcome = await self._grade_answer(snapshot, target, session=None, settings=settings)
            if outcome.result is None:
                raise self._as_error(outcome.output)
            results.append(outcome.result)
            if outcome.decision is not None:
                decisions[target.answer_id] = outcome.decision
        exam_result = self._aggregator.aggregate(
            snapshot.to_context(),
            results=results,
            decisions=decisions,
        )
        return GradingOutcome(
            results=tuple(results),
            decisions=decisions,
            exam_result=exam_result,
        )

    # ------------------------------------------------------------------ 单题编排

    async def _grade_answer(
        self,
        snapshot: SubmissionSnapshot,
        target: GradingTargetAnswer,
        *,
        session: Session | None,
        settings: AppSettings | None,
    ) -> _AnswerOutcome:
        """单题编排：分流 → 既有评分服务 → 专属一致性校验 → 置信度决策。"""

        if not isinstance(snapshot, SubmissionSnapshot) or not isinstance(
            target, GradingTargetAnswer
        ):
            return self._failure(
                GRADING_AGENT_INVALID_INPUT,
                "阅卷输入必须是答卷快照与单题目标。",
            )
        try:
            question_type = normalize_question_type(target.question_type)
            mode = self._router.route_type(question_type)
        except (TypeError, ValueError):
            return self._failure(
                GRADING_AGENT_UNSUPPORTED_QUESTION_TYPE,
                "题型无法分流，已停止阅卷。",
            )
        if mode is GradingMode.OBJECTIVE:
            return self._grade_objective(snapshot, target, question_type=question_type)
        return await self._grade_subjective(
            snapshot,
            target,
            question_type=question_type,
            session=session,
            settings=settings,
        )

    def _grade_objective(
        self,
        snapshot: SubmissionSnapshot,
        target: GradingTargetAnswer,
        *,
        question_type: QuestionType,
    ) -> _AnswerOutcome:
        """客观题：只调用确定性规则评分器，不调用置信度策略、不产生决策快照。"""

        student_answer = target.student_answer
        if isinstance(student_answer, Mapping):
            return self._failure(
                "GRADING_INVALID_ANSWER_FORMAT",
                "字典形态的学生答案缺少键语义与顺序约定，不能用于客观题评分。",
            )
        try:
            result = self._objective_grader.grade(
                question_type=target.question_type,
                reference_answer=target.reference_answer,
                student_answer=student_answer,
                max_score=float(target.max_score),
                knowledge_points=target.knowledge_points,
                answer_id=target.answer_id,
                submission_id=snapshot.submission_id,
            )
        except ObjectiveGradingError as error:
            return self._failure_from_exception(error)
        checked = self._validate_result(result, target, snapshot)
        if isinstance(checked, _AnswerOutcome):
            return checked
        return _AnswerOutcome(
            output=self._success_output(checked, None, question_type=question_type),
            result=checked,
        )

    async def _grade_subjective(
        self,
        snapshot: SubmissionSnapshot,
        target: GradingTargetAnswer,
        *,
        question_type: QuestionType,
        session: Session | None,
        settings: AppSettings | None,
    ) -> _AnswerOutcome:
        """主观题：组装来源后调用一次既有评分器；上下文与检索由评分器内部完成。"""

        active_session = session
        owned_session = False
        if active_session is None:
            if self._session_factory is None:
                return self._failure(
                    GRADING_AGENT_MISSING_SESSION,
                    "主观题评分需要只读会话，请传入 session 或 session_factory。",
                )
            active_session = self._session_factory()
            owned_session = True
        try:
            try:
                source = build_subjective_source(snapshot, target)
            except (GradingContextError, ValueError) as error:
                return self._failure_from_exception(error)
            resolved_settings = settings if settings is not None else self._settings
            # 仅默认评分器与默认策略共用评分器内产生的决策记录。
            reuse_recorded_decision = self._subjective_grader is None and self._policy is None
            policy = None if reuse_recorded_decision else self._resolved_policy(resolved_settings)
            recording = DecisionRecordingPolicy(policy=policy, settings=resolved_settings)
            grader: SubjectiveGraderLike = (
                self._subjective_grader
                if self._subjective_grader is not None
                else SubjectiveGrader(
                    provider=self._provider,
                    policy=recording if reuse_recorded_decision else policy,
                )
            )
            try:
                result = await grader.grade(
                    active_session,
                    source,
                    max_score=target.max_score,
                    top_k=self._top_k,
                    retriever=self._retriever,
                    reranker=self._reranker,
                    embedding_provider=self._embedding_provider,
                    settings=resolved_settings,
                )
            except (SubjectiveGradingError, GradingContextError) as error:
                return self._failure_from_exception(error)
            except ProviderExecutionError as error:
                return self._failure_from_provider(error)
            except Exception:  # noqa: BLE001 - 统一收敛为脱敏失败，不泄露原始异常
                return self._failure(
                    GRADING_AGENT_SUBJECTIVE_FAILED,
                    "主观题评分服务调用失败，已停止该题阅卷。",
                )
            checked = self._validate_result(result, target, snapshot)
            if isinstance(checked, _AnswerOutcome):
                return checked
            try:
                # 注入路径仍由 Agent 执行原有末端确认，不假设评分器已记录决策。
                applied = checked if reuse_recorded_decision else recording.apply(checked)
            except ConfidencePolicyError as error:
                return self._failure_from_exception(error)
            decision = recording.decision_for(target.answer_id)
            if decision is None:
                return self._failure(
                    GRADING_AGENT_MISSING_DECISION,
                    "主观题评分未记录本次置信度决策，拒绝作为已完成结果。",
                )
            mismatch = self._decision_mismatch(applied, decision)
            if mismatch is not None:
                return _AnswerOutcome(output=mismatch)
            return _AnswerOutcome(
                output=self._success_output(applied, decision, question_type=question_type),
                result=applied,
                decision=decision,
            )
        finally:
            if owned_session and active_session is not None:
                close = getattr(active_session, "close", None)
                if callable(close):
                    close()

    # ------------------------------------------------------------------ 校验与输出

    def _validate_result(
        self,
        result: object,
        target: GradingTargetAnswer,
        snapshot: SubmissionSnapshot,
    ) -> GradingResult | _AnswerOutcome:
        """专属一致性校验：校验状态、身份、题型与满分必须与阅卷目标一致。"""

        if not isinstance(result, GradingResult):
            return self._failure(
                GRADING_AGENT_INVALID_INPUT,
                "评分服务未返回约定的结构化结果。",
            )
        if result.validation_status != ValidationStatus.VALIDATED.value:
            return self._failure(
                GRADING_AGENT_RESULT_NOT_VALIDATED,
                f"评分结果未通过结构化校验（validation_status={result.validation_status}）。",
            )
        if (
            result.answer_id != target.answer_id
            or result.submission_id != snapshot.submission_id
            or result.question_type != target.question_type
            or result.max_score != float(target.max_score)
        ):
            return self._failure(
                GRADING_AGENT_RESULT_MISMATCH,
                "评分结果的答案、答卷、题型或满分与阅卷目标不一致，已拒绝该结果。",
            )
        return result

    @staticmethod
    def _decision_mismatch(
        result: GradingResult,
        decision: ConfidenceDecision,
    ) -> AgentOutput | None:
        """置信度事实与决策必须一致；不一致显式失败。"""

        if (
            result.confidence != decision.confidence
            or result.review_status != decision.review_status
        ):
            return AgentOutput(
                agent_type=AgentType.GRADING,
                status=AgentStatus.FAILURE,
                error=AgentError(
                    error_code=GRADING_AGENT_DECISION_MISMATCH,
                    message="评分结果的置信度或复核状态与本次决策不一致，已停止该题阅卷。",
                    retryable=False,
                ),
            )
        return None

    def _success_output(
        self,
        result: GradingResult,
        decision: ConfidenceDecision | None,
        *,
        question_type: QuestionType,
    ) -> AgentOutput:
        """构造成功输出；客观题不带决策快照与模型追踪字段。"""

        requires_review = bool(decision.requires_review) if decision is not None else False
        return AgentOutput(
            agent_type=AgentType.GRADING,
            status=AgentStatus.PENDING_REVIEW if requires_review else AgentStatus.SUCCESS,
            summary=(
                f"第 {result.answer_id} 题评分完成并进入待人工复核。"
                if requires_review
                else f"第 {result.answer_id} 题评分完成。"
            ),
            validation_status=ValidationStatus(result.validation_status),
            question_type=question_type,
            grading_result=result,
            confidence=result.confidence,
            confidence_decision=(
                confidence_decision_snapshot(decision) if decision is not None else None
            ),
            requires_review=requires_review,
            retrieved_context_ids=list(result.retrieved_context_ids),
            model=_resolve_provider_model(self._provider) if decision is not None else None,
            prompt_version=SUBJECTIVE_GRADING_PROMPT_VERSION if decision is not None else None,
        )

    def _failure(self, code: str, detail: str) -> _AnswerOutcome:
        """构造脱敏失败输出；失败不得同时声明需要人工复核。"""

        return _AnswerOutcome(
            output=AgentOutput(
                agent_type=AgentType.GRADING,
                status=AgentStatus.FAILURE,
                error=AgentError(error_code=code, message=detail, retryable=False),
            )
        )

    def _failure_from_exception(self, error: Exception) -> _AnswerOutcome:
        """把 M3 业务错误映射为脱敏失败，保留来源码与可重试语义。"""

        code = str(getattr(error, "error_code", GRADING_AGENT_INVALID_INPUT))
        retryable = bool(getattr(error, "retryable", False))
        source_code = getattr(error, "source_code", None)
        detail = getattr(error, "detail", None)
        message = (
            str(detail)
            if isinstance(detail, str) and detail.strip()
            else f"评分服务返回失败（来源码 {code}）。"
        )
        return _AnswerOutcome(
            output=AgentOutput(
                agent_type=AgentType.GRADING,
                status=AgentStatus.FAILURE,
                error=AgentError(
                    error_code=code,
                    message=message,
                    retryable=retryable,
                    source_code=str(source_code) if source_code else code,
                ),
            )
        )

    def _failure_from_provider(self, error: ProviderExecutionError) -> _AnswerOutcome:
        """把 Provider 执行错误映射为脱敏失败，保留来源码与可重试语义。"""

        info = error.info
        return _AnswerOutcome(
            output=AgentOutput(
                agent_type=AgentType.GRADING,
                status=AgentStatus.FAILURE,
                error=AgentError(
                    error_code="GRADING_PROVIDER_FAILED",
                    message=(
                        f"评分 Provider 调用失败（来源码 {info.code}，"
                        f"尝试 {info.attempt_count} 次）。"
                    ),
                    retryable=bool(info.retryable),
                    source_code=str(info.code),
                    attempt_count=int(info.attempt_count),
                ),
            )
        )

    @staticmethod
    def _as_error(output: AgentOutput) -> GradingAgentError:
        """把失败输出转为异常，供整卷编排拒绝部分成功。"""

        error = output.error
        if error is None:  # pragma: no cover - 失败输出必定带 error
            return GradingAgentError("阅卷失败。")
        return GradingAgentError(
            error.message,
            error_code=error.error_code,
            retryable=error.retryable,
            source_code=error.source_code,
        )

    def _resolved_policy(self, settings: AppSettings | None) -> ConfidencePolicy:
        """解析置信度策略：显式组件 > 显式 settings 工厂 > 全局配置。"""

        if self._policy is not None:
            return self._policy
        return ConfidencePolicy(settings=settings) if settings is not None else ConfidencePolicy()


def grading_output_to_state_patch(
    invocation: AgentInvocation,
    *,
    current_answer_order: int | None = None,
) -> dict[str, object]:
    """把阅卷 Agent 输出映射为 T065 ``GradingWorkflowState`` 的逐题槽位补丁。

    只写逐题槽位（``ANSWER_SLOT_FIELDS``）与流程控制字段 ``pause_reason``/``resumable``：
    待复核时暂停且可恢复，但**不写**教师人工结论（``review_status`` 只填自动决策值）。
    ``GradingWorkflowState`` 的写入与检查点持久化属 T072/T073。
    """

    output = invocation.output
    patch: dict[str, object] = {
        "question_type": output.question_type,
        "validation_status": output.validation_status,
        "confidence": output.confidence,
        "confidence_decision": output.confidence_decision,
        "retrieved_context_ids": list(output.retrieved_context_ids),
    }
    if current_answer_order is not None:
        patch["current_answer_order"] = current_answer_order
    if output.grading_result is not None:
        patch["grading_result"] = output.grading_result
        patch["current_answer_id"] = output.grading_result.answer_id
        patch["review_status"] = output.grading_result.review_status
    if output.error is not None:
        patch["error"] = output.error
    if output.requires_review:
        patch["pause_reason"] = "存在待人工复核的评分结果，等待教师复核。"
        patch["resumable"] = True
    return patch


__all__ = [
    "GRADING_AGENT_ASYNC_REQUIRED",
    "GRADING_AGENT_DECISION_MISMATCH",
    "GRADING_AGENT_INVALID_INPUT",
    "GRADING_AGENT_MISSING_DECISION",
    "GRADING_AGENT_MISSING_SESSION",
    "GRADING_AGENT_RESULT_MISMATCH",
    "GRADING_AGENT_RESULT_NOT_VALIDATED",
    "GRADING_AGENT_SUBJECTIVE_FAILED",
    "GRADING_AGENT_UNSUPPORTED_QUESTION_TYPE",
    "GradingAgent",
    "GradingAgentError",
    "ObjectiveGraderLike",
    "ResultAggregatorLike",
    "SubjectiveGraderLike",
    "grading_output_to_state_patch",
]
