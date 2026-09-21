"""复核服务（T074）：Pending Review 恢复、教师确认/修改、Reviewer 重评与最终结果持久化。

契约依据：``.specify/plan.md`` §5（人工复核可中断、可恢复）、§5.2（评分结果、考试结果与诊断
持久化）、``.specify/data-model.md``（GradeResult/ExamResult/ReviewRecord/WorkflowRun 状态转换）、
FR-035~FR-038 与 ``.specify/contracts/agent-workflow.md``（教师结论是唯一解除人工复核的事实）。

执行模型（两段式，避免数据库写锁跨越 LLM/检索/诊断）：

1. **短事务**（:meth:`ReviewService._apply_decision`）：校验 Teacher/操作者/答卷与答案归属 → 在同一事务内
   锁定并原子比较当前复核状态（``UPDATE ... WHERE review_status = <读到的状态>`` 且要求 ``rowcount == 1``）→
   写一条 ``ReviewRecord`` → 更新该题 ``GradingResult`` → 按 T054 重汇总并用 T060 写入器保存**当前**整卷
   结果（即使仍非最终状态，也要让待复核计数与当前分数可读）→ 同步工作流业务状态与检查点中的当前结果 → 提交。
2. **事务外恢复**：用原 ``workflow_id``/``thread_id``/``request_id`` 调用 T072
   ``apply_teacher_decision_async(..., resume=True)`` 恢复**原图**（不旁路伪造完成），随后重汇总并再次
   落库权威结果；只有最终成绩且图内没有可用诊断时才触发 T061 诊断生成（事务外）。

边界与不变量：

- **消费 T072 契约**：教师决策载荷一律使用
  :class:`~backend.app.ai.workflows.grading_workflow.TeacherReviewDecision`；本模块不重新定义该载荷、
  不改图实现，也不复制节点名（当前节点取状态或运行记录）。
- **权限与审计**：只有 ``Teacher`` 可提交教师结论，操作者写入 ``ReviewRecord.reviewer_id``；教师对答卷的
  课程归属由注入的 :class:`GradingSubmissionReader` 校验，不提供可省略的绕过分支。
- **并发**：``expected_review_status`` 不只是读取后的 Python 比较；真正的准入由事务内的条件更新
  （``rowcount == 1``）决定。两个请求同时处理同一答案时只有一个成功，另一个得到明确的状态冲突错误且
  不产生第二条有效复核记录。持有数据库锁期间不调用 LLM、检索、重评或诊断。
- **决定先落库、恢复可重试**：决定成功落库后恢复失败时，不重复写复核记录，运行状态显示为
  “教师结论已保存，等待恢复原工作流”，并可用 :meth:`resume_recorded_decision_async` 幂等重试。
- **重评语义**：``Re-grade`` 回到原图的该题评分节点（T072 在重评预算内调度），只重评目标答案，其他已完成
  答案不重新评分；最新评分与最新置信度决策替换当前事实，首次触发 ``Pending Review`` 的原始分数/理由由
  ``ReviewRecord.original_*`` 保留，人工复核要求不会因重评置信度升高而自动解除。
- **诊断失败**：最终成绩先可靠保存，再在事务外生成诊断。诊断失败时保留最终 ``GradingResult``/``ExamResult``，
  保留来源错误码与 ``retryable`` 事实：可重试且有持久 runtime 检查点与明确恢复节点时标记
  ``Paused + resumable=True``，否则标记 ``Failed``；不写空诊断，也不把失败返回成完成。
- **依赖齐全才执行**：检查点存储、会话工厂、复核记录存储、结果写入器、工作流恢复入口与诊断入口任一缺失时
  在产生副作用之前抛 ``REVIEW_SERVICE_NOT_READY``，不返回空成功。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Protocol, cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from backend.app.ai.agents.state import AgentError, confidence_decision_from_snapshot
from backend.app.ai.workflows.grading_handoff import (
    ACCEPTED_REVIEW_STATES,
    GENERATE_DIAGNOSIS,
)
from backend.app.ai.workflows.grading_workflow import (
    GRADING_WORKFLOW_DIAGNOSIS_FAILED,
    TEACHER_REVIEW_STATES,
    TEACHER_REVISION_STATE,
    TeacherReviewDecision,
)
from backend.app.ai.workflows.state import GradingWorkflowState
from backend.app.core.retry_policy import ProviderExecutionError
from backend.app.domain.enums import (
    GradingStatus,
    ReviewStatus,
    UserRole,
    ValidationStatus,
    WorkflowStatus,
)
from backend.app.models import GradingResult as GradingResultRow
from backend.app.models import ReviewRecord, WorkflowRun
from backend.app.schemas.ai import GradingResult as GradingResultDTO
from backend.app.schemas.grading import (
    ConfidenceDecisionDTO,
    DiagnosisReportDTO,
    ExamResultDTO,
)
from backend.app.services.grading.grading_task_service import (
    GradingSubmissionReader,
    GradingTargetAnswer,
    SubmissionSnapshot,
)
from backend.app.services.grading.result_aggregator import (
    GradingAggregationError,
    ResultAggregator,
)
from backend.app.services.workflow_checkpoint import (
    WorkflowCheckpointStore,
    checkpoint_thread_id,
)

#: 教师结论载荷非法（状态不可写、缺/多修订结果或标识不完整）。
REVIEW_SERVICE_INVALID_DECISION: Final[str] = "REVIEW_SERVICE_INVALID_DECISION"
#: ``Modified`` 未携带经校验的修订评分结果。
REVIEW_SERVICE_REVISION_REQUIRED: Final[str] = "REVIEW_SERVICE_REVISION_REQUIRED"
#: 非 Teacher 或缺少操作者标识。
REVIEW_SERVICE_PERMISSION_DENIED: Final[str] = "REVIEW_SERVICE_PERMISSION_DENIED"
#: 复核状态已变更，拒绝陈旧覆盖。
REVIEW_SERVICE_STALE_DECISION: Final[str] = "REVIEW_SERVICE_STALE_DECISION"
#: 复核轮次已改变；旧轮次请求不得消费新的 Pending Review。
REVIEW_SERVICE_STALE_ROUND: Final[str] = "REVIEW_SERVICE_STALE_ROUND"
#: 事务内的原子状态比较失败（并发复核冲突）。
REVIEW_SERVICE_CONFLICT: Final[str] = "REVIEW_SERVICE_CONFLICT"
#: 工作流/线程/答案身份不一致（含跨工作流写入）。
REVIEW_SERVICE_IDENTITY_MISMATCH: Final[str] = "REVIEW_SERVICE_IDENTITY_MISMATCH"
#: 目标答案不属于该答卷或该工作流状态，或还没有评分结果行。
REVIEW_SERVICE_ANSWER_NOT_FOUND: Final[str] = "REVIEW_SERVICE_ANSWER_NOT_FOUND"
#: 目标工作流没有检查点记录。
REVIEW_SERVICE_WORKFLOW_NOT_FOUND: Final[str] = "REVIEW_SERVICE_WORKFLOW_NOT_FOUND"
#: 复核依赖（检查点、会话、复核记录、结果写入、工作流恢复或诊断）未接线。
REVIEW_SERVICE_NOT_READY: Final[str] = "REVIEW_SERVICE_NOT_READY"
#: 整卷重汇总失败。
REVIEW_SERVICE_AGGREGATION_FAILED: Final[str] = "REVIEW_SERVICE_AGGREGATION_FAILED"
#: 当前线程已有事件循环，必须使用异步入口。
REVIEW_SERVICE_ASYNC_REQUIRED: Final[str] = "REVIEW_SERVICE_ASYNC_REQUIRED"

#: 决定已落库、等待恢复原工作流时的暂停原因（可识别的中间状态）。
DECISION_RECORDED_PAUSE_REASON: Final[str] = "教师结论已保存，等待恢复原工作流。"


class ReviewServiceError(RuntimeError):
    """复核服务边界错误；保留脱敏错误码，按不可重试的业务问题处理。"""

    error_code: str = REVIEW_SERVICE_INVALID_DECISION

    def __init__(self, detail: str, *, error_code: str | None = None) -> None:
        if error_code is not None:
            self.error_code = error_code
        super().__init__(f"{self.error_code}：{detail}")
        self.detail = detail


class ReviewPermissionError(ReviewServiceError):
    """调用者不是 Teacher 或缺少审计主体。"""

    error_code = REVIEW_SERVICE_PERMISSION_DENIED


class ReviewInvalidDecisionError(ReviewServiceError):
    """教师决策载荷非法。"""

    error_code = REVIEW_SERVICE_INVALID_DECISION


class ReviewRevisionRequiredError(ReviewServiceError):
    """``Modified`` 缺少经校验的修订结果。"""

    error_code = REVIEW_SERVICE_REVISION_REQUIRED


class ReviewStaleDecisionError(ReviewServiceError):
    """教师决策基于已变更的复核状态。"""

    error_code = REVIEW_SERVICE_STALE_DECISION


class ReviewStaleRoundError(ReviewStaleDecisionError):
    """请求轮次与当前待复核轮次不一致。"""

    error_code = REVIEW_SERVICE_STALE_ROUND


class ReviewConflictError(ReviewServiceError):
    """事务内的原子状态比较失败：另一请求已经修改该答案的复核状态。"""

    error_code = REVIEW_SERVICE_CONFLICT


class ReviewIdentityError(ReviewServiceError):
    """工作流、线程或答案身份不一致。"""

    error_code = REVIEW_SERVICE_IDENTITY_MISMATCH


class ReviewAnswerNotFoundError(ReviewServiceError):
    """目标答案不属于该答卷或该工作流状态，或没有评分结果行。"""

    error_code = REVIEW_SERVICE_ANSWER_NOT_FOUND


class ReviewWorkflowNotFoundError(ReviewServiceError):
    """目标工作流没有检查点记录。"""

    error_code = REVIEW_SERVICE_WORKFLOW_NOT_FOUND


class ReviewServiceNotReadyError(ReviewServiceError):
    """复核依赖未接线。"""

    error_code = REVIEW_SERVICE_NOT_READY


class ReviewAggregationError(ReviewServiceError):
    """整卷重汇总失败。"""

    error_code = REVIEW_SERVICE_AGGREGATION_FAILED


class ReviewRecordStoreError(ReviewServiceError):
    """复核记录无法写入（缺少对应评分行等）。"""

    error_code = REVIEW_SERVICE_ANSWER_NOT_FOUND


@dataclass(frozen=True, slots=True)
class ReviewRecordRequest:
    """一次复核记录的写入请求：操作者、操作类型与结论后的分数事实。

    ``final_*`` 只为 ``Confirmed``/``Modified`` 给出；``Re-grade`` 尚无新结论时保持 ``None``，
    不用 0 分冒充结论（0 分是合法的新结论，与空值可区分）。
    """

    submission_id: str
    answer_id: str
    reviewer_id: str
    decision: ReviewStatus
    final_score: Decimal | None = None
    final_reason: str | None = None
    final_knowledge_points: list[str] | None = None
    comment: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewOutcome:
    """一次教师复核的结果：复核记录、重汇总结果、恢复情况与真实运行状态。"""

    workflow_id: str
    submission_id: str
    answer_id: str
    decision: ReviewStatus
    review_record_id: str | None
    workflow_status: WorkflowStatus
    interrupted: bool
    pending_answer_ids: tuple[str, ...]
    resumable: bool
    resumed: bool
    resume_error_code: str | None
    exam_result: ExamResultDTO | None
    exam_result_persisted: bool
    diagnosis: DiagnosisReportDTO | None
    diagnosis_error_code: str | None
    review_round_id: UUID | None = None


class ReviewResultWriter(Protocol):
    """整卷结果写入合同（T060 边界）：提供事务内与独立事务两种写入方式。"""

    def save_exam_result(self, exam_result: ExamResultDTO) -> None: ...

    def save_exam_result_within(
        self, session: Session, exam_result: ExamResultDTO
    ) -> None: ...


class ReviewRecordWriter(Protocol):
    """复核记录写入合同（T074 边界）：在调用方事务内写入，不自行提交。"""

    def save_within(self, session: Session, request: ReviewRecordRequest) -> ReviewRecord: ...


class ReviewDiagnosisRecorder(Protocol):
    """诊断生成与落库合同（T061 边界）。"""

    def record(self, exam_result: ExamResultDTO) -> DiagnosisReportDTO | None: ...


class TeacherDecisionWorkflow(Protocol):
    """T072 工作流的教师决策入口（不重新定义，只消费）。"""

    async def apply_teacher_decision_async(
        self,
        decision: TeacherReviewDecision,
        *,
        resume: bool = False,
    ) -> Any: ...


def _as_uuid(value: object, label: str) -> UUID:
    """校验 UUID；非法取值显式失败，不静默丢弃身份。"""

    if isinstance(value, UUID):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return UUID(value.strip())
        except ValueError as error:
            raise ReviewServiceError(f"{label} 必须是 UUID，收到 {value!r}。") from error
    raise ReviewServiceError(f"缺少 {label}，拒绝写入不完整记录。")


def _required_text(value: object, label: str) -> str:
    """校验必填文本；空白或非文本显式失败。"""

    if not isinstance(value, str) or not value.strip():
        raise ReviewInvalidDecisionError(f"教师决策缺少 {label}，拒绝不完整结论。")
    return value.strip()


def _run_status(state: Mapping[str, Any]) -> WorkflowStatus | None:
    """解析复核后的流程状态；未知取值返回 ``None``（由运行记录的真实状态兜底）。"""

    value = state.get("status")
    if isinstance(value, WorkflowStatus):
        return value
    if isinstance(value, str):
        try:
            return WorkflowStatus(value)
        except ValueError:
            return None
    return None


def _current_node(state: Mapping[str, Any], run: WorkflowRun) -> str:
    """返回应写入检查点的当前节点：状态优先，其次运行记录；两者都缺失即拒绝。"""

    for candidate in (state.get("current_node"), run.current_node):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    raise ReviewServiceError("状态与运行记录都未给出当前节点，拒绝写入检查点。")


def _current_result(state: Mapping[str, Any], answer_id: str) -> GradingResultDTO | None:
    """返回状态中该答案的评分结果（修订结果校验与 ``Confirmed`` 结论的事实来源）。"""

    results = state.get("grading_results") or {}
    if not isinstance(results, Mapping):
        return None
    result = results.get(answer_id)
    return result if isinstance(result, GradingResultDTO) else None


def _authoritative_review_status(state: Mapping[str, Any], answer_id: str) -> str | None:
    """返回该答案的权威复核状态：决策快照优先，其次评分结果本身（与 T072 口径一致）。"""

    decisions = state.get("confidence_decisions") or {}
    decision = decisions.get(answer_id) if isinstance(decisions, Mapping) else None
    if isinstance(decision, ConfidenceDecisionDTO):
        return decision.review_status
    result = _current_result(state, answer_id)
    return result.review_status if result is not None else None


def _result_state(result: Any) -> dict[str, Any]:
    """取出 T072 恢复结果中的状态映射；缺失即显式失败，不伪造状态。"""

    raw = getattr(result, "state", None)
    if not isinstance(raw, Mapping):
        raise ReviewServiceError("工作流恢复结果缺少状态映射，拒绝继续写入。")
    return dict(raw)


def _pending_answer_ids(result: Any, state_after: Mapping[str, Any]) -> tuple[str, ...]:
    """返回复核后的待复核答案集合。

    工作流给出集合时以工作流为准；未给出时按 T071 ``ACCEPTED_REVIEW_STATES`` 从状态推导，
    不把“无法判定”当成“没有待复核题目”。
    """

    raw = getattr(result, "pending_answer_ids", None)
    if raw is not None:
        return tuple(str(item) for item in raw)
    decisions = state_after.get("confidence_decisions") or {}
    if not isinstance(decisions, Mapping):
        return ()
    return tuple(
        sorted(
            str(answer_id)
            for answer_id, decision in decisions.items()
            if isinstance(decision, ConfidenceDecisionDTO)
            and decision.review_status not in ACCEPTED_REVIEW_STATES
        )
    )


def _ready_diagnosis(state: Mapping[str, Any]) -> DiagnosisReportDTO | None:
    """返回图中已生成的可用诊断；非就绪或缺失时返回 ``None``。"""

    diagnosis = state.get("diagnosis")
    if not isinstance(diagnosis, DiagnosisReportDTO):
        return None
    return diagnosis if str(diagnosis.status) == "Ready" else None


def _retryable(error: BaseException) -> bool:
    """判断失败是否属于可重试的系统错误（复用 T009 Provider 契约的可重试语义）。"""

    if isinstance(error, ProviderExecutionError):
        return bool(error.info.retryable)
    return bool(getattr(error, "retryable", False))


class DatabaseReviewRecordStore:
    """基于 T062 ``ReviewRecord`` 的复核记录存储（T074 边界）。

    写入一律发生在调用方事务内：复核记录必须与单题结果、当前整卷结果同事务提交，
    否则会出现“有复核记录但没有对应评分事实”的中间态。
    """

    def save_within(self, session: Session, request: ReviewRecordRequest) -> ReviewRecord:
        """在调用方事务内写入一条复核记录。

        ``original_*`` 一律取评分行当前的落库事实（复核前的分数/理由/知识点），不取评分结果状态，
        因此不会把 AI 的新结论当成“修改前事实”；``final_*`` 由调用方给出的结论决定。
        """

        submission_id = _as_uuid(request.submission_id, "submission_id")
        answer_id = _as_uuid(request.answer_id, "answer_id")
        row = session.scalars(
            select(GradingResultRow).where(
                GradingResultRow.submission_id == submission_id,
                GradingResultRow.answer_id == answer_id,
            )
        ).one_or_none()
        if row is None:
            raise ReviewRecordStoreError("该题目还没有评分结果行，复核记录不得凭空创建。")
        record = ReviewRecord(
            grading_result_id=row.id,
            reviewer_id=_as_uuid(request.reviewer_id, "reviewer_id"),
            decision=request.decision,
            review_round_id=row.pending_review_round_id,
            original_score=row.score,
            original_reason=row.reason,
            original_knowledge_points=list(row.knowledge_points or []),
            final_score=request.final_score,
            final_reason=request.final_reason,
            final_knowledge_points=request.final_knowledge_points,
            comment=request.comment,
        )
        session.add(record)
        session.flush()
        return record


def find_matching_review_record(
    session: Session,
    *,
    run: WorkflowRun,
    decision: TeacherReviewDecision,
    actor_id: str,
    expected_review_round_id: UUID | None = None,
) -> ReviewRecord | None:
    """只复用当前仍生效的最新结论；本函数在调用方完成授权后使用。

    记录没有运行外键，创建时间仍用于排除旧运行记录。提供轮次时必须匹配
    最新记录的轮次；None 是旧客户端兼容模式，沿用 P3.1 的状态和内容校验。
    """

    if decision.workflow_id != run.workflow_id or decision.expected_review_status not in (
        None,
        ReviewStatus.PENDING_REVIEW.value,
    ):
        return None
    row = session.scalars(
        select(GradingResultRow).where(
            GradingResultRow.submission_id == run.submission_id,
            GradingResultRow.answer_id == _as_uuid(decision.answer_id, "answer_id"),
        )
    ).one_or_none()
    if row is None or row.review_status.value != decision.review_status:
        return None
    # 先取最新记录再匹配，不能从历史中挑出一个已被覆盖的同动作记录。
    record = session.scalars(
        select(ReviewRecord)
        .where(ReviewRecord.grading_result_id == row.id)
        .order_by(ReviewRecord.created_at.desc(), ReviewRecord.id.desc())
        .limit(1)
    ).first()
    if (
        record is None
        or record.created_at < run.created_at
        or str(record.reviewer_id) != actor_id
        or record.decision.value != decision.review_status
        or (
            expected_review_round_id is not None
            and record.review_round_id != expected_review_round_id
        )
    ):
        return None
    if record.decision is ReviewStatus.RE_GRADE:
        return record
    if (
        record.final_score != row.score
        or record.final_reason != row.reason
        or record.final_knowledge_points != row.knowledge_points
    ):
        return None
    if record.decision is ReviewStatus.MODIFIED:
        revised = decision.revised_result
        if (
            revised is None
            or record.final_score != Decimal(str(revised.score))
            or record.final_reason != revised.reason
        ):
            return None
    return record


@dataclass(frozen=True, slots=True)
class _PreparedDecision:
    """已完成校验的教师决策上下文（工作流、状态、答卷快照与当前复核状态）。"""

    run: WorkflowRun
    state: GradingWorkflowState
    snapshot: SubmissionSnapshot
    current_review_status: str | None
    recorded_status: str | None
    already_recorded: bool
    observed_review_round_id: UUID | None


@dataclass(frozen=True, slots=True)
class _DecisionResult:
    """短事务的产物：复核记录、投影后的整卷结果与落库情况。"""

    review_record_id: str
    review_round_id: UUID | None
    exam_result: ExamResultDTO | None
    exam_result_persisted: bool


class ReviewService:
    """复核应用服务：原子落库教师结论，并在事务外恢复原工作流。

    :param checkpoints: T073 检查点存储；同一事务内同步工作流业务状态。
    :param reader: 答卷快照读取（含教师课程归属校验），同时提供 T054 汇总所需的权威题目集合。
    :param session_factory: 会话工厂；复核记录、单题结果与当前整卷结果在同一事务内写入。
    :param workflow_provider: 由运行记录与状态构造 T072 工作流的工厂；``None`` 时报未就绪。
    :param result_writer: 整卷结果写入（T060 边界，事务内）；``None`` 时报未就绪。
    :param diagnosis: 诊断生成与落库入口（T061 边界）；``None`` 时报未就绪。
    :param review_records: 复核记录写入（T074 边界，事务内）；``None`` 时报未就绪。
    :param aggregator: T054 汇总器；``None`` 时使用默认实现。
    """

    def __init__(
        self,
        *,
        checkpoints: WorkflowCheckpointStore,
        reader: GradingSubmissionReader,
        session_factory: Callable[[], Session] | None = None,
        workflow_provider: (
            Callable[[WorkflowRun, Mapping[str, Any]], TeacherDecisionWorkflow] | None
        ) = None,
        result_writer: ReviewResultWriter | None = None,
        diagnosis: ReviewDiagnosisRecorder | None = None,
        review_records: ReviewRecordWriter | None = None,
        aggregator: Any | None = None,
    ) -> None:
        self._checkpoints = checkpoints
        self._reader = reader
        self._session_factory = session_factory
        self._workflow_provider = workflow_provider
        self._result_writer = result_writer
        self._diagnosis = diagnosis
        self._review_records = review_records
        self._aggregator = aggregator if aggregator is not None else ResultAggregator()

    # ------------------------------------------------------------------ 公开入口

    async def submit_decision_async(
        self,
        decision: TeacherReviewDecision,
        *,
        actor_id: str,
        actor_role: UserRole | str,
        comment: str | None = None,
        expected_review_round_id: UUID | None = None,
    ) -> ReviewOutcome:
        """提交教师结论：短事务落库，然后在事务外恢复原工作流。

        expected_review_round_id=None 保留旧客户端兼容语义，不能识别跨轮次迟到
        请求；无论是否提供轮次，都在持锁后核对预检观察到的轮次，防止事务间换轮。
        """

        self._ensure_ready()
        prepared = self._prepare(
            decision, actor_id=actor_id, actor_role=actor_role,
            expected_review_round_id=expected_review_round_id,
        )
        if prepared.already_recorded:
            decided: _DecisionResult | None = None
        else:
            decided = self._apply_decision(
                prepared,
                decision,
                actor_id=actor_id,
                comment=comment,
            )
        return await self._resume(prepared, decision, decided)

    def submit_decision(
        self,
        decision: TeacherReviewDecision,
        *,
        actor_id: str,
        actor_role: UserRole | str,
        comment: str | None = None,
        expected_review_round_id: UUID | None = None,
    ) -> ReviewOutcome:
        """同步入口：无运行中事件循环时包装 ``asyncio.run``，否则显式报错。"""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.submit_decision_async(
                    decision,
                    actor_id=actor_id,
                    actor_role=actor_role,
                    comment=comment,
                    expected_review_round_id=expected_review_round_id,
                )
            )
        raise ReviewServiceError(
            "当前线程已有事件循环，请使用 await submit_decision_async(...)。",
            error_code=REVIEW_SERVICE_ASYNC_REQUIRED,
        )

    async def resume_recorded_decision_async(
        self,
        decision: TeacherReviewDecision,
        *,
        actor_id: str,
        actor_role: UserRole | str,
        expected_review_round_id: UUID | None = None,
    ) -> ReviewOutcome:
        """重试入口：结论已落库但恢复未完成时幂等恢复，不重复写复核记录。"""

        self._ensure_ready()
        prepared = self._prepare(
            decision,
            actor_id=actor_id,
            actor_role=actor_role,
            allow_recorded=True,
            expected_review_round_id=expected_review_round_id,
        )
        if prepared.already_recorded:
            # 结论已生效：重试只恢复运行，不再要求 `expected_review_status`
            # （陈旧断言已在首次调用时完成，重试时图状态的权威值就是本次结论）。
            return await self._resume(
                prepared,
                replace(decision, expected_review_status=None),
                None,
            )
        decided = self._apply_decision(prepared, decision, actor_id=actor_id, comment=None)
        return await self._resume(prepared, decision, decided)

    def resume_recorded_decision(
        self,
        decision: TeacherReviewDecision,
        *,
        actor_id: str,
        actor_role: UserRole | str,
        expected_review_round_id: UUID | None = None,
    ) -> ReviewOutcome:
        """同步入口：无运行中事件循环时包装 ``asyncio.run``，否则显式报错。"""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.resume_recorded_decision_async(
                    decision,
                    actor_id=actor_id,
                    actor_role=actor_role,
                    expected_review_round_id=expected_review_round_id,
                )
            )
        raise ReviewServiceError(
            "当前线程已有事件循环，请使用 await resume_recorded_decision_async(...)。",
            error_code=REVIEW_SERVICE_ASYNC_REQUIRED,
        )

    async def request_regrade_async(
        self,
        workflow_id: str,
        thread_id: str,
        answer_id: str,
        *,
        actor_id: str,
        actor_role: UserRole | str,
        expected_review_status: str | None = None,
        expected_review_round_id: UUID | None = None,
        comment: str | None = None,
    ) -> ReviewOutcome:
        """把 T070 Reviewer 的 ``regrade`` 决策翻译为教师 ``Re-grade`` 结论并调度重评。

        Reviewer 的建议不解除人工复核（B01）：重评必须由教师显式请求；结论写入后由原图的
        ``Re-grade`` 节点在重评预算内回到该题的评分节点，其他已完成答案不重新评分。
        """

        decision = TeacherReviewDecision(
            workflow_id=workflow_id,
            thread_id=thread_id,
            answer_id=answer_id,
            review_status=ReviewStatus.RE_GRADE.value,
            revised_result=None,
            expected_review_status=expected_review_status,
        )
        return await self.submit_decision_async(
            decision,
            actor_id=actor_id,
            actor_role=actor_role,
            comment=comment,
            expected_review_round_id=expected_review_round_id,
        )

    def request_regrade(
        self,
        workflow_id: str,
        thread_id: str,
        answer_id: str,
        *,
        actor_id: str,
        actor_role: UserRole | str,
        expected_review_status: str | None = None,
        expected_review_round_id: UUID | None = None,
        comment: str | None = None,
    ) -> ReviewOutcome:
        """同步入口：无运行中事件循环时包装 ``asyncio.run``，否则显式报错。"""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.request_regrade_async(
                    workflow_id,
                    thread_id,
                    answer_id,
                    actor_id=actor_id,
                    actor_role=actor_role,
                    expected_review_status=expected_review_status,
                    expected_review_round_id=expected_review_round_id,
                    comment=comment,
                )
            )
        raise ReviewServiceError(
            "当前线程已有事件循环，请使用 await request_regrade_async(...)。",
            error_code=REVIEW_SERVICE_ASYNC_REQUIRED,
        )

    # ------------------------------------------------------------------ 依赖与校验

    def _ensure_ready(self) -> None:
        """依赖齐全才允许执行：缺任一依赖都在产生副作用之前显式报未就绪。"""

        missing: list[str] = []
        if self._session_factory is None:
            missing.append("会话工厂")
        if self._review_records is None:
            missing.append("复核记录存储")
        if self._result_writer is None:
            missing.append("整卷结果写入器")
        if self._workflow_provider is None:
            missing.append("工作流恢复入口")
        if self._diagnosis is None:
            missing.append("诊断生成入口")
        if missing:
            raise ReviewServiceNotReadyError(
                "复核依赖未接线：" + "、".join(missing) + "；拒绝返回空成功。"
            )

    def _require_teacher(self, actor_id: str, actor_role: UserRole | str) -> str:
        """只有 Teacher 可以提交教师结论；操作者标识必填（审计主体不可缺省）。"""

        try:
            role = actor_role if isinstance(actor_role, UserRole) else UserRole(actor_role)
        except (TypeError, ValueError) as error:
            raise ReviewPermissionError("无法识别调用者角色，拒绝复核写入。") from error
        if role is not UserRole.TEACHER:
            raise ReviewPermissionError(
                "只有 Teacher 可以提交教师复核结论，Admin 与学生不得替代教师。"
            )
        if not isinstance(actor_id, str) or not actor_id.strip():
            raise ReviewPermissionError("缺少操作者标识，拒绝无审计主体的复核写入。")
        return actor_id.strip()

    @staticmethod
    def _validate_decision_shape(decision: TeacherReviewDecision) -> None:
        """校验载荷形态：状态可写、修订结果只属于 ``Modified``、标识完整。"""

        if not isinstance(decision, TeacherReviewDecision):
            raise ReviewInvalidDecisionError("教师决策必须是 TeacherReviewDecision。")
        _required_text(decision.workflow_id, "workflow_id")
        _required_text(decision.thread_id, "thread_id")
        _required_text(decision.answer_id, "answer_id")
        if decision.review_status not in TEACHER_REVIEW_STATES:
            raise ReviewInvalidDecisionError(
                "教师结论只允许 Confirmed/Modified/Final/Re-grade。"
            )
        if decision.review_status == TEACHER_REVISION_STATE and decision.revised_result is None:
            raise ReviewRevisionRequiredError("Modified 必须携带经校验的修订评分结果。")
        if (
            decision.review_status != TEACHER_REVISION_STATE
            and decision.revised_result is not None
        ):
            raise ReviewInvalidDecisionError("只有 Modified 可以携带修订评分结果。")

    @staticmethod
    def _validate_revised_result(
        revised: GradingResultDTO,
        target: GradingTargetAnswer,
        submission_id: str,
    ) -> None:
        """修订结果必须属于该题、该答卷，且题型与满分与题目定义一致。"""

        if revised.answer_id != target.answer_id or revised.submission_id != submission_id:
            raise ReviewIdentityError(
                "修订结果与该题的答案/答卷不一致，拒绝写入其它题目的结论。"
            )
        if str(revised.question_type) != str(target.question_type):
            raise ReviewInvalidDecisionError("修订结果的题型与该题定义不一致。")
        try:
            same_max_score = Decimal(str(revised.max_score)) == Decimal(str(target.max_score))
        except (InvalidOperation, ValueError):
            same_max_score = False
        if not same_max_score:
            raise ReviewInvalidDecisionError("修订结果的满分与该题定义不一致。")

    def _prepare(
        self,
        decision: TeacherReviewDecision,
        *,
        actor_id: str,
        actor_role: UserRole | str,
        allow_recorded: bool = False,
        expected_review_round_id: UUID | None = None,
    ) -> _PreparedDecision:
        """完成权限、身份、防陈旧与修订结果校验，返回可直接写入的上下文。"""

        operator_id = self._require_teacher(actor_id, actor_role)
        self._validate_decision_shape(decision)
        run = self._checkpoints.load_checkpoint(decision.workflow_id)
        if run is None:
            raise ReviewWorkflowNotFoundError(
                f"工作流 {decision.workflow_id} 没有检查点记录，无法恢复复核。"
            )
        state = self._checkpoints.restore_state(run)
        if str(state.get("workflow_id") or "") != decision.workflow_id:
            raise ReviewIdentityError(
                "教师决策的 workflow_id 与检查点状态不一致，拒绝跨工作流写入。"
            )
        stored_thread = checkpoint_thread_id(run)
        if stored_thread is not None and stored_thread != decision.thread_id:
            raise ReviewIdentityError(
                "教师决策的 thread_id 与检查点记录的运行线程不一致。"
            )
        submission_id = str(run.submission_id)
        state_status = _authoritative_review_status(state, decision.answer_id)
        if state_status is None and _current_result(state, decision.answer_id) is None:
            raise ReviewAnswerNotFoundError(
                "该题目不属于本工作流状态的评分结果集合，拒绝跨工作流或凭空确认。"
            )
        snapshot = self._reader.load_for_teacher(submission_id, operator_id)
        if snapshot.submission_id != submission_id:
            raise ReviewIdentityError("教师可访问的答卷与检查点记录的答卷不一致。")
        target = next(
            (item for item in snapshot.answers if item.answer_id == decision.answer_id),
            None,
        )
        if target is None:
            raise ReviewAnswerNotFoundError("该答案不属于注入的答卷快照，拒绝写入复核结论。")
        if decision.revised_result is not None:
            self._validate_revised_result(decision.revised_result, target, submission_id)
        db_review = self._row_review_state(submission_id, decision.answer_id)
        if db_review is None:
            raise ReviewAnswerNotFoundError(
                "该题目还没有评分结果行，复核记录不得凭空创建。"
            )
        db_status, round_id = db_review
        already_recorded = decision.review_status == db_status
        if allow_recorded and already_recorded:
            with self._open_session() as session:
                # 在同一 Session 读取时间字段，避免不同数据库驱动的时区表示差异。
                stored_run = session.get(WorkflowRun, run.id)
                record = (
                    find_matching_review_record(
                        session, run=stored_run, decision=decision, actor_id=operator_id,
                        expected_review_round_id=expected_review_round_id,
                    )
                    if stored_run is not None
                    else None
                )
            if record is None:
                raise ReviewStaleDecisionError(
                    "该请求与当前生效的最新复核记录不一致，拒绝作为已保存决定恢复。"
                )
            return _PreparedDecision(
                run=run,
                state=state,
                snapshot=snapshot,
                current_review_status=state_status,
                recorded_status=db_status,
                already_recorded=True,
                observed_review_round_id=round_id,
            )
        if expected_review_round_id is not None and expected_review_round_id != round_id:
            raise ReviewStaleRoundError("复核轮次已变化，请刷新待复核详情后重试。")
        if allow_recorded and db_status != ReviewStatus.PENDING_REVIEW.value:
            raise ReviewStaleDecisionError(
                "当前评分已被其它决定覆盖，拒绝恢复旧结论。"
            )
        expected = decision.expected_review_status
        if expected is not None and expected not in {state_status, db_status}:
            raise ReviewStaleDecisionError(
                "复核状态已变化（评分行 "
                f"{db_status!r}、图中状态 {state_status!r}），拒绝陈旧覆盖既有结论。"
            )
        return _PreparedDecision(
            run=run,
            state=state,
            snapshot=snapshot,
            current_review_status=state_status,
            recorded_status=db_status,
            already_recorded=False,
            observed_review_round_id=round_id,
        )

    def _row_review_state(
        self, submission_id: str, answer_id: str
    ) -> tuple[str, UUID | None] | None:
        """一起读取评分状态和轮次，作为事务内原子比较的基准。"""

        with self._open_session() as session:
            row = self._grading_row(session, submission_id, answer_id)
            if row is None:
                return None
            return row.review_status.value, row.pending_review_round_id

    # ------------------------------------------------------------------ 短事务

    @contextmanager
    def _open_session(self) -> Iterator[Session]:
        """打开会话；未注入会话工厂时显式报未就绪。"""

        factory = self._session_factory
        if factory is None:
            raise ReviewServiceNotReadyError("复核会话工厂未接线：无法执行原子落库。")
        session = factory()
        try:
            yield session
        finally:
            session.close()

    @staticmethod
    def _grading_row(
        session: Session,
        submission_id: str,
        answer_id: str,
        *,
        lock: bool = False,
    ) -> GradingResultRow | None:
        """按答卷与答案取评分行；``lock=True`` 时请求行级写锁（无该能力的方言自动忽略）。"""

        statement = select(GradingResultRow).where(
            GradingResultRow.submission_id == _as_uuid(submission_id, "submission_id"),
            GradingResultRow.answer_id == _as_uuid(answer_id, "answer_id"),
        )
        if lock:
            statement = statement.with_for_update()
        return session.scalars(statement).one_or_none()

    def _apply_decision(
        self,
        prepared: _PreparedDecision,
        decision: TeacherReviewDecision,
        *,
        actor_id: str,
        comment: str | None,
    ) -> _DecisionResult:
        """短事务：原子校验 → 写复核记录 → 更新单题结果 → 保存当前整卷结果 → 同步状态。"""

        status = ReviewStatus(decision.review_status)
        submission_id = str(prepared.run.submission_id)
        projected = self._projected_state(prepared, decision)
        exam_result = self._aggregate(projected, prepared.snapshot)
        recorded = self._recorded_state(prepared)
        with self._open_session() as session:
            try:
                # 锁定运行记录与评分行：并发复核同一答案时由数据库串行化。
                self._require_locked_run(session, prepared.run.workflow_id)
                row = self._grading_row(
                    session, submission_id, decision.answer_id, lock=True
                )
                if row is None:
                    raise ReviewAnswerNotFoundError(
                        "该题目还没有评分结果行，复核记录不得凭空创建。"
                    )
                # 原子状态基础：必须是本次预检读到的权威状态，而不是事务内的新读取。
                observed = self._observed_status(prepared)
                if row.review_status is not observed:
                    raise ReviewConflictError(
                        "该题目的复核状态已被其它请求修改（当前 "
                        f"{row.review_status.value}），拒绝覆盖；请刷新后重试。"
                    )
                if row.pending_review_round_id != prepared.observed_review_round_id:
                    raise ReviewStaleRoundError("复核轮次已被其它请求更新，拒绝消费新的待复核轮次。")
                # 先写复核记录：存储件在更新前读取评分行，得到真正的“复核前事实”。
                # 若后面的原子更新失败，整个事务回滚，记录不会残留。
                record = self._require_records().save_within(
                    session,
                    self._record_request(
                        prepared=prepared,
                        decision=decision,
                        status=status,
                        actor_id=actor_id,
                        comment=comment,
                    ),
                )
                updated = cast(
                    CursorResult[Any],
                    session.execute(
                        update(GradingResultRow)
                        .where(
                            GradingResultRow.id == row.id,
                            GradingResultRow.review_status == observed,
                            GradingResultRow.pending_review_round_id
                            == prepared.observed_review_round_id,
                        )
                        .values(self._row_updates(row, decision, status))
                    ),
                )
                if updated.rowcount != 1:
                    raise ReviewConflictError(
                        "该题目的复核状态已被其它请求修改，拒绝覆盖；请刷新后重试。"
                    )
                persisted = False
                if exam_result is not None:
                    self._require_writer().save_exam_result_within(session, exam_result)
                    persisted = True
                self._checkpoints.save_checkpoint_within(
                    session,
                    prepared.run.workflow_id,
                    recorded,
                    _current_node(recorded, prepared.run),
                    pause_reason=DECISION_RECORDED_PAUSE_REASON,
                    thread_id=decision.thread_id,
                )
                session.commit()
            except Exception:
                session.rollback()
                raise
            return _DecisionResult(
                review_record_id=str(record.id),
                review_round_id=record.review_round_id,
                exam_result=exam_result,
                exam_result_persisted=persisted,
            )

    @staticmethod
    def _observed_status(prepared: _PreparedDecision) -> ReviewStatus:
        """返回本次结论的原子比较基准；缺失即显式失败（预检已保证存在）。"""

        if prepared.recorded_status is None:  # pragma: no cover - 预检已拒绝该情形
            raise ReviewAnswerNotFoundError("该题目还没有评分记录，拒绝复核写入。")
        return ReviewStatus(prepared.recorded_status)

    @staticmethod
    def _row_updates(
        row: GradingResultRow,
        decision: TeacherReviewDecision,
        status: ReviewStatus,
    ) -> dict[str, Any]:
        """构造评分行的更新字段：结论后的分数/理由/知识点与最新决策快照。

        决策快照六列必须同时为空或同时存在（T062 表级约束），因此只有在已有自动决策快照时才
        继承置信度与阈值并更新复核相关列；没有快照（如客观题）时不凭空造一个阈值，教师结论
        由 ``review_status`` 与复核记录表达。
        """

        revised = decision.revised_result
        updates: dict[str, Any] = {"review_status": status, "pending_review_round_id": None}
        carried_confidence = row.decision_confidence
        carried_threshold = row.decision_threshold
        if carried_confidence is not None and carried_threshold is not None:
            updates.update(
                {
                    "decision_confidence": (
                        float(revised.confidence)
                        if revised is not None
                        else carried_confidence
                    ),
                    "decision_threshold": carried_threshold,
                    "decision_requires_review": status not in ACCEPTED_REVIEW_STATES,
                    "decision_review_status": status,
                    "decision_grading_status": (
                        GradingStatus.FINAL
                        if status in {ReviewStatus.CONFIRMED, ReviewStatus.MODIFIED}
                        else GradingStatus.PENDING_REVIEW
                    ),
                    "decision_reason": f"教师复核结论：{status.value}。",
                }
            )
        if revised is not None:
            updates.update(
                {
                    "score": Decimal(str(revised.score)),
                    "max_score": Decimal(str(revised.max_score)),
                    "reason": revised.reason,
                    "knowledge_points": list(revised.knowledge_points),
                    "correct_points": list(revised.correct_points),
                    "missing_knowledge_points": list(revised.missing_knowledge_points),
                    "suggestions": list(revised.suggestions),
                    "confidence": float(revised.confidence),
                    "validation_status": ValidationStatus(revised.validation_status),
                }
            )
        return updates

    def _recorded_state(self, prepared: _PreparedDecision) -> dict[str, Any]:
        """短事务写入检查点的状态：保留图的状态事实，只标记“决定已保存、等待恢复”。

        不在这里改写 ``grading_results``/``confidence_decisions``/``review_status``：教师结论由 T072
        在恢复时按其契约写入图状态（并校验 ``expected_review_status``）；服务侧抢先改状态会让图的
        陈旧守位把合法结论当成陈旧覆盖，也会让“图的状态”与“教师结论”两份事实分叉。
        """

        state: dict[str, Any] = dict(prepared.state)
        state["status"] = WorkflowStatus.PAUSED
        state["pause_reason"] = DECISION_RECORDED_PAUSE_REASON
        return state

    def _projected_state(
        self,
        prepared: _PreparedDecision,
        decision: TeacherReviewDecision,
    ) -> dict[str, Any]:
        """把教师结论投影到状态上，**仅用于同事务的当前整卷结果重汇总**。

        投影让待复核计数与当前分数立即反映教师结论；它不写入检查点，图的权威状态由恢复过程产生。
        """

        status = ReviewStatus(decision.review_status)
        state: dict[str, Any] = dict(prepared.state)
        results = dict(state.get("grading_results") or {})
        current = results.get(decision.answer_id)
        if decision.revised_result is not None:
            results[decision.answer_id] = decision.revised_result.model_copy(
                update={"review_status": status}
            )
        elif isinstance(current, GradingResultDTO):
            results[decision.answer_id] = current.model_copy(
                update={"review_status": status}
            )
        else:
            raise ReviewAnswerNotFoundError(
                "状态中没有该题目的评分结果，拒绝写入不完整的复核结论。"
            )
        state["grading_results"] = results
        decisions = dict(state.get("confidence_decisions") or {})
        snapshot = decisions.get(decision.answer_id)
        if isinstance(snapshot, ConfidenceDecisionDTO):
            decisions[decision.answer_id] = snapshot.model_copy(
                update={
                    "requires_review": status not in ACCEPTED_REVIEW_STATES,
                    "review_status": status,
                    "reason": f"教师复核结论：{status.value}。",
                }
            )
            state["confidence_decisions"] = decisions
        state["review_status"] = status
        return state

    def _record_request(
        self,
        *,
        prepared: _PreparedDecision,
        decision: TeacherReviewDecision,
        status: ReviewStatus,
        actor_id: str,
        comment: str | None,
    ) -> ReviewRecordRequest:
        """构造复核记录请求：``Confirmed`` 沿用原结论事实，``Modified`` 用修订结果。"""

        final_score: Decimal | None = None
        final_reason: str | None = None
        final_points: list[str] | None = None
        if status is ReviewStatus.MODIFIED:
            revised = decision.revised_result
            if revised is None:  # pragma: no cover - 上方校验已拒绝该情形
                raise ReviewRevisionRequiredError("Modified 必须携带经校验的修订评分结果。")
            final_score = Decimal(str(revised.score))
            final_reason = revised.reason
            final_points = list(revised.knowledge_points)
        elif status is ReviewStatus.CONFIRMED:
            confirmed = _current_result(prepared.state, decision.answer_id)
            if confirmed is None:  # pragma: no cover - 上方校验已拒绝该情形
                raise ReviewAnswerNotFoundError("确认结论缺少可核对的评分结果。")
            final_score = Decimal(str(confirmed.score))
            final_reason = confirmed.reason
            final_points = list(confirmed.knowledge_points)
        # Re-grade / Final：尚无新结论时 final_* 保持为空，不用 0 分冒充结论。
        return ReviewRecordRequest(
            submission_id=str(prepared.run.submission_id),
            answer_id=decision.answer_id,
            reviewer_id=actor_id,
            decision=status,
            final_score=final_score,
            final_reason=final_reason,
            final_knowledge_points=final_points,
            comment=comment,
        )

    @staticmethod
    def _require_locked_run(session: Session, workflow_id: str) -> WorkflowRun:
        """在事务内锁定并返回运行记录；不存在即显式失败。"""

        row = session.scalars(
            select(WorkflowRun)
            .where(WorkflowRun.workflow_id == workflow_id)
            .with_for_update()
        ).one_or_none()
        if row is None:
            raise ReviewWorkflowNotFoundError(f"工作流 {workflow_id} 没有检查点记录。")
        return row

    def _require_records(self) -> ReviewRecordWriter:
        """返回复核记录写入器；未接线即显式报未就绪。"""

        writer = self._review_records
        if writer is None:
            raise ReviewServiceNotReadyError("复核记录存储未接线：无法提交教师结论。")
        return writer

    def _require_writer(self) -> ReviewResultWriter:
        """返回整卷结果写入器；未接线即显式报未就绪。"""

        writer = self._result_writer
        if writer is None:
            raise ReviewServiceNotReadyError("最终结果写入未接线：无法持久化整卷结果。")
        return writer

    # ------------------------------------------------------------------ 恢复与收尾

    async def _resume(
        self,
        prepared: _PreparedDecision,
        decision: TeacherReviewDecision,
        decided: _DecisionResult | None,
    ) -> ReviewOutcome:
        """事务外恢复原工作流，并在恢复后重新落库权威结果与生成诊断。"""

        workflow = self._workflow_for(prepared.run, prepared.state)
        try:
            result = await workflow.apply_teacher_decision_async(decision, resume=True)
        except Exception as error:  # noqa: BLE001 - 决定已落库，恢复失败必须如实报告
            return self._resume_failed(prepared, decision, decided, error)
        state_after = _result_state(result)
        pending = _pending_answer_ids(result, state_after)
        exam_result = self._aggregate(state_after, prepared.snapshot)
        persisted = False
        if exam_result is not None:
            self._require_writer().save_exam_result(exam_result)
            persisted = True
        diagnosis, diagnosis_error = self._resolve_diagnosis(state_after, exam_result, pending)
        if diagnosis_error is not None:
            state_final = self._diagnosis_failure_state(state_after, diagnosis_error)
        elif diagnosis is not None:
            state_final = self._completed_state(state_after, diagnosis)
        else:
            # 无最终成绩或仍待复核：沿用图状态（图只标记“诊断待生成”）。
            state_final = dict(state_after)
        run = self._sync_checkpoint(state_final, thread_id=decision.thread_id)
        return ReviewOutcome(
            workflow_id=prepared.run.workflow_id,
            submission_id=str(prepared.run.submission_id),
            answer_id=decision.answer_id,
            decision=ReviewStatus(decision.review_status),
            review_record_id=None if decided is None else decided.review_record_id,
            review_round_id=None if decided is None else decided.review_round_id,
            workflow_status=_run_status(state_final) or run.status,
            interrupted=bool(getattr(result, "interrupted", False)),
            pending_answer_ids=pending,
            resumable=bool(run.resumable),
            resumed=True,
            resume_error_code=None,
            exam_result=exam_result,
            exam_result_persisted=persisted or bool(decided and decided.exam_result_persisted),
            diagnosis=diagnosis,
            diagnosis_error_code=(
                None
                if diagnosis_error is None
                else str(getattr(diagnosis_error, "error_code", type(diagnosis_error).__name__))
            ),
        )

    def _resume_failed(
        self,
        prepared: _PreparedDecision,
        decision: TeacherReviewDecision,
        decided: _DecisionResult | None,
        error: BaseException,
    ) -> ReviewOutcome:
        """恢复失败：决定已落库，如实返回“已保存、待恢复”，不谎称成功。"""

        row = self._checkpoints.load_checkpoint(prepared.run.workflow_id)
        return ReviewOutcome(
            workflow_id=prepared.run.workflow_id,
            submission_id=str(prepared.run.submission_id),
            answer_id=decision.answer_id,
            decision=ReviewStatus(decision.review_status),
            review_record_id=None if decided is None else decided.review_record_id,
            review_round_id=None if decided is None else decided.review_round_id,
            workflow_status=(row.status if row is not None else WorkflowStatus.PAUSED),
            interrupted=False,
            pending_answer_ids=(decision.answer_id,),
            resumable=bool(row.resumable) if row is not None else False,
            resumed=False,
            resume_error_code=str(
                getattr(error, "error_code", type(error).__name__)
            ),
            exam_result=None if decided is None else decided.exam_result,
            exam_result_persisted=bool(decided and decided.exam_result_persisted),
            diagnosis=None,
            diagnosis_error_code=None,
        )

    def _workflow_for(
        self,
        run: WorkflowRun,
        state: GradingWorkflowState,
    ) -> TeacherDecisionWorkflow:
        """由运行记录构造 T072 工作流；未接线或接口不符时显式报未就绪。"""

        provider = self._workflow_provider
        if provider is None:
            raise ReviewServiceNotReadyError("工作流恢复未接线：无法提交教师结论。")
        workflow = provider(run, state)
        if workflow is None or not hasattr(workflow, "apply_teacher_decision_async"):
            raise ReviewServiceNotReadyError(
                "工作流恢复入口缺少 apply_teacher_decision_async 契约。"
            )
        return workflow

    def _resolve_diagnosis(
        self,
        state_after: Mapping[str, Any],
        exam_result: ExamResultDTO | None,
        pending: tuple[str, ...],
    ) -> tuple[DiagnosisReportDTO | None, BaseException | None]:
        """决定是否生成诊断：只有最终成绩且图内没有可用诊断时才触发 T061。

        图内诊断由恢复后的 ``Generate Diagnosis`` 节点产出（即教师结论触发的重新生成）；
        缺失时由 T061 记录器补齐一次，失败事实原样返回供调用方表达真实状态。
        """

        if exam_result is None or not exam_result.is_final or pending:
            return None, None
        in_graph = _ready_diagnosis(state_after)
        if in_graph is not None:
            return in_graph, None
        recorder = self._diagnosis
        if recorder is None:
            return None, ReviewServiceNotReadyError("诊断生成未接线：无法生成诊断报告。")
        try:
            return recorder.record(exam_result), None
        except Exception as error:  # noqa: BLE001 - 保留最终成绩并如实报告诊断失败
            return None, error

    def _completed_state(
        self,
        state_after: Mapping[str, Any],
        diagnosis: DiagnosisReportDTO,
    ) -> dict[str, Any]:
        """诊断成功后的终态（P1.3）：图内只标记“诊断待生成”，完成态由本服务落诊断后写。"""

        state: dict[str, Any] = dict(state_after)
        state["current_node"] = GENERATE_DIAGNOSIS
        state["diagnosis"] = diagnosis
        state["status"] = WorkflowStatus.COMPLETED
        state["pause_reason"] = None
        state["resumable"] = False
        state["error"] = None
        return state

    def _diagnosis_failure_state(
        self,
        state_after: Mapping[str, Any],
        error: BaseException,
    ) -> dict[str, Any]:
        """诊断失败后的状态：可重试且有持久检查点→Paused；否则 Failed；两者都保留成绩。"""

        failure = AgentError(
            error_code=GRADING_WORKFLOW_DIAGNOSIS_FAILED,
            message="诊断生成失败，已保留最终成绩。",
            retryable=_retryable(error),
            source_code=str(getattr(error, "error_code", type(error).__name__)),
        )
        state: dict[str, Any] = dict(state_after)
        state["diagnosis"] = None
        state["current_node"] = GENERATE_DIAGNOSIS
        if failure.retryable:
            state.update(
                {
                    "status": WorkflowStatus.PAUSED,
                    "error": None,
                    "pause_reason": (
                        "诊断生成失败且属可重试系统错误，已保留最终成绩，"
                        "等待从诊断节点恢复。"
                    ),
                    "resumable": True,
                }
            )
            return state
        state.update(
            {
                "status": WorkflowStatus.FAILED,
                "error": failure,
                "pause_reason": None,
                "resumable": False,
                "review_status": None,
            }
        )
        return state

    def _sync_checkpoint(self, state_final: Mapping[str, Any], *, thread_id: str) -> WorkflowRun:
        """把最终运行事实写入 T073 检查点；终态使用明确的完成/失败标记。"""

        workflow_id = self._workflow_state_workflow_id(state_final)
        run = self._checkpoints.load_checkpoint(workflow_id)
        if run is None:
            raise ReviewWorkflowNotFoundError(
                f"工作流 {workflow_id} 没有检查点记录，无法同步运行事实。"
            )
        node = _current_node(state_final, run)
        status = _run_status(state_final)
        if status is WorkflowStatus.FAILED:
            failure = state_final.get("error")
            self._checkpoints.save_checkpoint(
                workflow_id,
                state_final,
                node,
                thread_id=thread_id,
            )
            return self._checkpoints.mark_failed(
                workflow_id,
                failure
                if isinstance(failure, AgentError)
                else "工作流恢复后失败（状态未给出脱敏错误）。",
            )
        if status is WorkflowStatus.COMPLETED:
            self._checkpoints.save_checkpoint(
                workflow_id,
                {**state_final, "pause_reason": None},
                node,
                thread_id=thread_id,
            )
            return self._checkpoints.mark_completed(workflow_id)
        pause_reason = state_final.get("pause_reason")
        return self._checkpoints.save_checkpoint(
            workflow_id,
            state_final,
            node,
            pause_reason=pause_reason if isinstance(pause_reason, str) else None,
            thread_id=thread_id,
        )

    @staticmethod
    def _workflow_state_workflow_id(state: Mapping[str, Any]) -> str:
        """取状态中的工作流标识；缺失即显式失败。"""

        workflow_id = state.get("workflow_id")
        if not isinstance(workflow_id, str) or not workflow_id.strip():
            raise ReviewServiceError("恢复结果缺少 workflow_id，拒绝同步运行事实。")
        return workflow_id.strip()

    # ------------------------------------------------------------------ 重汇总

    def _aggregate(
        self,
        state: Mapping[str, Any],
        snapshot: SubmissionSnapshot,
    ) -> ExamResultDTO | None:
        """按 T054 汇总口径重算整卷结果；没有逐题结果时返回 ``None``（不伪造成绩）。"""

        raw_results = state.get("grading_results") or {}
        if not isinstance(raw_results, Mapping) or not raw_results:
            return None
        ordered: list[GradingResultDTO] = []
        for target in snapshot.answers:
            item = raw_results.get(target.answer_id)
            if isinstance(item, GradingResultDTO):
                ordered.append(item)
        if not ordered:
            return None
        raw_decisions = state.get("confidence_decisions") or {}
        decisions: dict[str, Any] = {}
        if isinstance(raw_decisions, Mapping):
            for answer_id, item in raw_decisions.items():
                if isinstance(item, ConfidenceDecisionDTO):
                    decisions[str(answer_id)] = confidence_decision_from_snapshot(item)
        try:
            return self._aggregator.aggregate(
                snapshot.to_context(),
                results=ordered,
                decisions=decisions,
            )
        except GradingAggregationError as error:
            raise ReviewAggregationError(f"整卷重汇总失败：{error}") from error


__all__ = [
    "DECISION_RECORDED_PAUSE_REASON",
    "REVIEW_SERVICE_AGGREGATION_FAILED",
    "REVIEW_SERVICE_ANSWER_NOT_FOUND",
    "REVIEW_SERVICE_ASYNC_REQUIRED",
    "REVIEW_SERVICE_CONFLICT",
    "REVIEW_SERVICE_IDENTITY_MISMATCH",
    "REVIEW_SERVICE_INVALID_DECISION",
    "REVIEW_SERVICE_NOT_READY",
    "REVIEW_SERVICE_PERMISSION_DENIED",
    "REVIEW_SERVICE_REVISION_REQUIRED",
    "REVIEW_SERVICE_STALE_DECISION",
    "REVIEW_SERVICE_STALE_ROUND",
    "REVIEW_SERVICE_WORKFLOW_NOT_FOUND",
    "DatabaseReviewRecordStore",
    "ReviewAggregationError",
    "ReviewAnswerNotFoundError",
    "ReviewConflictError",
    "ReviewDiagnosisRecorder",
    "ReviewIdentityError",
    "ReviewInvalidDecisionError",
    "ReviewOutcome",
    "ReviewPermissionError",
    "ReviewRecordRequest",
    "ReviewRecordStoreError",
    "ReviewRecordWriter",
    "ReviewResultWriter",
    "ReviewRevisionRequiredError",
    "ReviewService",
    "ReviewServiceError",
    "ReviewServiceNotReadyError",
    "ReviewStaleDecisionError",
    "ReviewStaleRoundError",
    "ReviewWorkflowNotFoundError",
    "TeacherDecisionWorkflow",
    "find_matching_review_record",
]
