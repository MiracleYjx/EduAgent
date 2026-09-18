"""复核服务（T074）：Pending Review 恢复、教师确认/修改、Reviewer 重评与最终结果持久化。

契约依据：``.specify/plan.md`` §5（人工复核可中断、可恢复）、§5.2（评分结果、考试结果与诊断
持久化）、``.specify/data-model.md``（ReviewRecord、WorkflowRun 状态转换与关键校验规则）、
FR-035~FR-038 与 ``.specify/contracts/agent-workflow.md``（教师结论是唯一解除人工复核的事实）。

职责与边界：

- **消费 T072 已固定的契约**：教师决策载荷一律使用
  :class:`~backend.app.ai.workflows.grading_workflow.TeacherReviewDecision`，写入与恢复一律经
  ``apply_teacher_decision_async(..., resume=True)``；本模块不重新定义该载荷、不改图实现，也不复制
  节点名（当前节点取状态或运行记录，不为检查点编造节点）。
- **只做应用编排**：校验权限与身份 → 防陈旧 → 写复核记录 → 触发 T054 整卷重汇总 → 委托 T060/T061
  落库整卷结果与诊断 → 同步 T073 检查点。本模块不直接写 ``ExamResult``/``DiagnosisReport`` 行
  （结果与诊断落库仍属 T060/T061 边界），也不在 T072 模块中做持久化。
- **权限边界不放松**：只有 ``Teacher`` 可提交教师结论，操作者写入 ``ReviewRecord.reviewer_id``；
  教师对答卷的课程归属由注入的 :class:`GradingSubmissionReader` 校验，本模块不提供可省略的绕过分支。
- **Reviewer 不得自动放行（B01）**：T070 Reviewer 的 ``regrade`` 决策必须由教师显式请求；本模块把它
  翻译为 ``Re-grade`` 教师结论并回到图的重评节点，不替教师确认分数。
- **防陈旧**：给出 ``expected_review_status`` 时必须与检查点中的权威复核状态一致，不一致即拒绝，
  不覆盖已有结论；未给出时不宣称已做陈旧保护。
- **不假声明**：工作流未返回状态映射、最终结果写入未接线、诊断未接线时都显式报未就绪，不静默跳过，
  也不在未形成最终成绩时落库部分成绩。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.ai.agents.state import AgentError, confidence_decision_from_snapshot
from backend.app.ai.workflows.grading_handoff import (
    ACCEPTED_REVIEW_STATES,
    diagnosis_allowed,
)
from backend.app.ai.workflows.grading_workflow import (
    TEACHER_REVIEW_STATES,
    TEACHER_REVISION_STATE,
    TeacherReviewDecision,
)
from backend.app.ai.workflows.state import GradingWorkflowState
from backend.app.domain.enums import ReviewStatus, UserRole, WorkflowStatus
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
#: 工作流/线程/答案身份不一致（含跨工作流写入）。
REVIEW_SERVICE_IDENTITY_MISMATCH: Final[str] = "REVIEW_SERVICE_IDENTITY_MISMATCH"
#: 目标答案不属于该答卷或该工作流状态。
REVIEW_SERVICE_ANSWER_NOT_FOUND: Final[str] = "REVIEW_SERVICE_ANSWER_NOT_FOUND"
#: 目标工作流没有检查点记录。
REVIEW_SERVICE_WORKFLOW_NOT_FOUND: Final[str] = "REVIEW_SERVICE_WORKFLOW_NOT_FOUND"
#: 复核依赖（工作流恢复、最终结果写入、诊断生成或记录存储）未接线。
REVIEW_SERVICE_NOT_READY: Final[str] = "REVIEW_SERVICE_NOT_READY"
#: 整卷重汇总失败。
REVIEW_SERVICE_AGGREGATION_FAILED: Final[str] = "REVIEW_SERVICE_AGGREGATION_FAILED"
#: 复核记录无法写入（缺少对应评分行等）。
REVIEW_SERVICE_RECORD_INVALID: Final[str] = "REVIEW_SERVICE_RECORD_INVALID"
#: 当前线程已有事件循环，必须使用异步入口。
REVIEW_SERVICE_ASYNC_REQUIRED: Final[str] = "REVIEW_SERVICE_ASYNC_REQUIRED"


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


class ReviewIdentityError(ReviewServiceError):
    """工作流、线程或答案身份不一致。"""

    error_code = REVIEW_SERVICE_IDENTITY_MISMATCH


class ReviewAnswerNotFoundError(ReviewServiceError):
    """目标答案不属于该答卷或该工作流状态。"""

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
    """复核记录无法写入。"""

    error_code = REVIEW_SERVICE_RECORD_INVALID


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
    """一次教师复核的结果：复核记录、复核后的运行事实与最终结果落库情况。"""

    workflow_id: str
    submission_id: str
    answer_id: str
    decision: ReviewStatus
    review_record_id: str
    workflow_status: WorkflowStatus
    interrupted: bool
    pending_answer_ids: tuple[str, ...]
    resumable: bool
    exam_result: ExamResultDTO | None
    exam_result_persisted: bool
    diagnosis: DiagnosisReportDTO | None


class ReviewResultWriter(Protocol):
    """最终结果写入合同（T060 边界；``DatabaseGradingRepository`` 满足该结构）。"""

    def save_exam_result(self, exam_result: ExamResultDTO) -> None: ...


class ReviewDiagnosisRecorder(Protocol):
    """诊断生成与落库合同（T061 边界；``DiagnosisRecorder`` 满足该结构）。"""

    def record(self, exam_result: ExamResultDTO) -> DiagnosisReportDTO | None: ...


class ReviewRecordWriter(Protocol):
    """复核记录写入合同（T074 边界）。"""

    def save(self, request: ReviewRecordRequest) -> ReviewRecord: ...


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


class DatabaseReviewRecordStore:
    """基于 T062 ``ReviewRecord`` 的复核记录存储（T074 边界）。

    :param session_factory: 会话工厂；每次调用自建并在使用后关闭。
    """

    def __init__(self, *, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    @contextmanager
    def _use_session(self) -> Iterator[Session]:
        """自建并关闭会话；不跨线程复用、不嵌套请求事务。"""

        session = self._session_factory()
        try:
            yield session
        finally:
            session.close()

    def save(self, request: ReviewRecordRequest) -> ReviewRecord:
        """写入一条复核记录。

        ``original_*`` 一律取评分行当前的落库事实（复核前的分数/理由/知识点），不取评分结果状态，
        因此不会把 AI 的新结论当成“修改前事实”；``final_*`` 由调用方给出的结论决定。
        """

        submission_id = _as_uuid(request.submission_id, "submission_id")
        answer_id = _as_uuid(request.answer_id, "answer_id")
        with self._use_session() as session:
            row = session.scalars(
                select(GradingResultRow).where(
                    GradingResultRow.submission_id == submission_id,
                    GradingResultRow.answer_id == answer_id,
                )
            ).one_or_none()
            if row is None:
                raise ReviewRecordStoreError(
                    "该题目还没有评分结果行，复核记录不得凭空创建。"
                )
            record = ReviewRecord(
                grading_result_id=row.id,
                reviewer_id=_as_uuid(request.reviewer_id, "reviewer_id"),
                decision=request.decision,
                original_score=row.score,
                original_reason=row.reason,
                original_knowledge_points=list(row.knowledge_points or []),
                final_score=request.final_score,
                final_reason=request.final_reason,
                final_knowledge_points=request.final_knowledge_points,
                comment=request.comment,
            )
            session.add(record)
            try:
                session.commit()
            except Exception:
                session.rollback()
                raise
            session.refresh(record)
            return record


@dataclass(frozen=True, slots=True)
class _PreparedDecision:
    """已完成校验的教师决策上下文（工作流、状态、答卷快照与当前复核状态）。"""

    run: WorkflowRun
    state: GradingWorkflowState
    snapshot: SubmissionSnapshot
    current_review_status: str | None


class ReviewService:
    """复核应用服务：教师结论写入、工作流恢复与最终结果落库编排。

    :param checkpoints: T073 检查点存储；教师结论落库后同步运行事实。
    :param reader: 答卷快照读取（含教师课程归属校验），同时提供 T054 汇总所需的权威题目集合。
    :param workflow_provider: 由运行记录与状态构造 T072 工作流的工厂；``None`` 时报未就绪。
    :param result_writer: 最终整卷结果写入（T060 边界）；``None`` 时报未就绪。
    :param diagnosis: 诊断生成与落库入口（T061 边界）；``None`` 时报未就绪。
    :param review_records: 复核记录写入（T074 边界）；``None`` 时报未就绪。
    :param aggregator: T054 汇总器；``None`` 时使用默认实现。
    """

    def __init__(
        self,
        *,
        checkpoints: WorkflowCheckpointStore,
        reader: GradingSubmissionReader,
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
    ) -> ReviewOutcome:
        """提交教师结论并恢复工作流，随后触发重汇总与诊断。

        T072 抛出的 ``GradingWorkflowError`` 原样传播（保留其 ``error_code``），本模块不把它改写为
        成功或另一种结论；本模块自身的校验错误在调用工作流之前抛出，避免产生副作用。
        """

        prepared = self._prepare(decision, actor_id=actor_id, actor_role=actor_role)
        workflow = self._workflow_for(prepared.run, prepared.state)
        result = await workflow.apply_teacher_decision_async(decision, resume=True)
        return self._finish(
            prepared=prepared,
            decision=decision,
            result=result,
            actor_id=actor_id,
            comment=comment,
        )

    def submit_decision(
        self,
        decision: TeacherReviewDecision,
        *,
        actor_id: str,
        actor_role: UserRole | str,
        comment: str | None = None,
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
                )
            )
        raise ReviewServiceError(
            "当前线程已有事件循环，请使用 await submit_decision_async(...)。",
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
        comment: str | None = None,
    ) -> ReviewOutcome:
        """把 T070 Reviewer 的 ``regrade`` 决策翻译为教师 ``Re-grade`` 结论并调度重评。

        Reviewer 的建议不解除人工复核（B01）：重评必须由教师显式请求；结论写入后由图的
        ``Re-grade`` 节点在重评预算内回到该题的评分节点。
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
                    comment=comment,
                )
            )
        raise ReviewServiceError(
            "当前线程已有事件循环，请使用 await request_regrade_async(...)。",
            error_code=REVIEW_SERVICE_ASYNC_REQUIRED,
        )

    # ------------------------------------------------------------------ 校验

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
        if (
            decision.review_status == TEACHER_REVISION_STATE
            and decision.revised_result is None
        ):
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
        result = _current_result(state, decision.answer_id)
        if result is None:
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
        current_status = _authoritative_review_status(state, decision.answer_id)
        expected = decision.expected_review_status
        if expected is not None and expected != current_status:
            raise ReviewStaleDecisionError(
                f"复核状态已变化（当前 {current_status!r}），拒绝陈旧覆盖既有结论。"
            )
        return _PreparedDecision(
            run=run,
            state=state,
            snapshot=snapshot,
            current_review_status=current_status,
        )

    # ------------------------------------------------------------------ 执行

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

    def _finish(
        self,
        *,
        prepared: _PreparedDecision,
        decision: TeacherReviewDecision,
        result: Any,
        actor_id: str,
        comment: str | None,
    ) -> ReviewOutcome:
        """恢复完成后：写复核记录 → 重汇总 → 落库结果与诊断 → 同步检查点。"""

        state_after = _result_state(result)
        interrupted = bool(getattr(result, "interrupted", False))
        pending = _pending_answer_ids(result, state_after)
        record = self._save_review_record(
            prepared=prepared,
            decision=decision,
            actor_id=actor_id,
            comment=comment,
        )
        # 仍有待复核题目时不重汇总：T071 要求待复核结果不得进入最终成绩，
        # 用未经确认的整卷结果覆盖已落库成绩会把“未最终确认”伪装成“已汇总”。
        exam_result = None if pending else self._reaggregate(prepared.snapshot, state_after)
        persisted = False
        report: DiagnosisReportDTO | None = None
        if exam_result is not None and exam_result.is_final:
            writer = self._result_writer
            if writer is None:
                raise ReviewServiceNotReadyError("最终结果写入未接线：无法持久化整卷结果。")
            writer.save_exam_result(exam_result)
            persisted = True
            if diagnosis_allowed(
                {
                    **state_after,
                    "exam_result": exam_result,
                    "final_results": list(exam_result.items),
                }
            ):
                recorder = self._diagnosis
                if recorder is None:
                    raise ReviewServiceNotReadyError("诊断生成未接线：无法重新生成诊断报告。")
                report = recorder.record(exam_result)
        run = self._sync_checkpoint(prepared.run, state_after, thread_id=decision.thread_id)
        return ReviewOutcome(
            workflow_id=prepared.run.workflow_id,
            submission_id=str(prepared.run.submission_id),
            answer_id=decision.answer_id,
            decision=ReviewStatus(decision.review_status),
            review_record_id=str(record.id),
            workflow_status=_run_status(state_after) or run.status,
            interrupted=interrupted,
            pending_answer_ids=pending,
            resumable=bool(run.resumable),
            exam_result=exam_result,
            exam_result_persisted=persisted,
            diagnosis=report,
        )

    def _save_review_record(
        self,
        *,
        prepared: _PreparedDecision,
        decision: TeacherReviewDecision,
        actor_id: str,
        comment: str | None,
    ) -> ReviewRecord:
        """写入复核记录：``Confirmed`` 沿用原结论事实，``Modified`` 用修订结果，``Re-grade`` 留空。"""

        store = self._review_records
        if store is None:
            raise ReviewServiceNotReadyError("复核记录存储未接线：无法提交教师结论。")
        status = ReviewStatus(decision.review_status)
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
        return store.save(
            ReviewRecordRequest(
                submission_id=str(prepared.run.submission_id),
                answer_id=decision.answer_id,
                reviewer_id=actor_id,
                decision=status,
                final_score=final_score,
                final_reason=final_reason,
                final_knowledge_points=final_points,
                comment=comment,
            )
        )

    def _reaggregate(
        self,
        snapshot: SubmissionSnapshot,
        state_after: Mapping[str, Any],
    ) -> ExamResultDTO | None:
        """按 T054 汇总口径重算整卷结果；没有逐题结果时返回 ``None``（不伪造成绩）。

        调用方在仍存在待复核题目时不会调用本方法：待复核结果不得进入最终成绩。
        """

        raw_results = state_after.get("grading_results") or {}
        if not isinstance(raw_results, Mapping) or not raw_results:
            return None
        ordered: list[GradingResultDTO] = []
        for target in snapshot.answers:
            item = raw_results.get(target.answer_id)
            if isinstance(item, GradingResultDTO):
                ordered.append(item)
        if not ordered:
            return None
        raw_decisions = state_after.get("confidence_decisions") or {}
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

    def _sync_checkpoint(
        self,
        run: WorkflowRun,
        state_after: Mapping[str, Any],
        *,
        thread_id: str,
    ) -> WorkflowRun:
        """把复核后的运行事实写入 T073 检查点；终态使用明确的完成/失败标记。"""

        workflow_id = run.workflow_id
        node = _current_node(state_after, run)
        status = _run_status(state_after)
        if status is WorkflowStatus.FAILED:
            failure = state_after.get("error")
            self._checkpoints.save_checkpoint(
                workflow_id,
                state_after,
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
            # 完成态不保留暂停原因，与 T073 `mark_completed` 的终态口径一致。
            self._checkpoints.save_checkpoint(
                workflow_id,
                {**state_after, "pause_reason": None},
                node,
                thread_id=thread_id,
            )
            return self._checkpoints.mark_completed(workflow_id)
        pause_reason = state_after.get("pause_reason")
        return self._checkpoints.save_checkpoint(
            workflow_id,
            state_after,
            node,
            pause_reason=pause_reason if isinstance(pause_reason, str) else None,
            thread_id=thread_id,
        )


__all__ = [
    "REVIEW_SERVICE_AGGREGATION_FAILED",
    "REVIEW_SERVICE_ANSWER_NOT_FOUND",
    "REVIEW_SERVICE_ASYNC_REQUIRED",
    "REVIEW_SERVICE_IDENTITY_MISMATCH",
    "REVIEW_SERVICE_INVALID_DECISION",
    "REVIEW_SERVICE_NOT_READY",
    "REVIEW_SERVICE_PERMISSION_DENIED",
    "REVIEW_SERVICE_RECORD_INVALID",
    "REVIEW_SERVICE_REVISION_REQUIRED",
    "REVIEW_SERVICE_STALE_DECISION",
    "REVIEW_SERVICE_WORKFLOW_NOT_FOUND",
    "DatabaseReviewRecordStore",
    "ReviewAggregationError",
    "ReviewAnswerNotFoundError",
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
    "ReviewWorkflowNotFoundError",
    "TeacherDecisionWorkflow",
]
