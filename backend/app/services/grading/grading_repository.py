"""T056 结果与任务状态的生产持久化实现（T060～T064 模型）。

本模块实现 :class:`~backend.app.services.grading.grading_task_service.GradingRepository`
的生产版本，负责把阅卷产出与任务状态落到真实表：

- **单题结果与决策快照**（``grading_results``）：按 ``answer_id`` **就地更新**既有行，
  保留主键与既有 ``ReviewRecord`` 的级联关联；只有首次评分才插入。写入前校验该答案确实
  属于目标答卷，跨答卷写入显式失败。缺结果的题目不写 0 分占位行。
- **整卷结果**（``exam_results``）：``total_max_score``、``confirmed_subtotal``、
  ``final_total_score``、``aggregated_at`` 作为**当次汇总事实**落库，读回时直接使用，
  不按当前题目定义重算，也不刷新 ``aggregated_at``。
- **任务状态**（``workflow_runs``）：``workflow_id`` 复用任务标识，``request_id`` 由触发
  入口生成并落库，``checkpoint`` 保存还原任务查询 DTO 所需的时间、计数、脱敏错误信息与
  **本次快照的答案顺序**（题序 + ``answer_id`` 序列），读回时复用该顺序，不把数据库自然
  返回顺序当作原卷题序。
- **事务边界**：:meth:`DatabaseGradingRepository.save_outcome` 在一次事务内提交单题结果、
  决策快照、整卷结果与答卷进度；提交失败整体回滚，不留下“最终整卷 + 缺失单题”的中间态。
  ``save_task`` 同样是单事务写入，任务进入失败状态时同事务把答卷答案标记为失败。

状态映射（B04）：API 的 ``GradingTaskStatus`` 与 ``WorkflowStatus`` 不是同一个概念。
``Completed`` 只表示“本轮评分已执行完成”，当存在待人工复核题目时工作流状态写为
``Paused`` 并记录暂停原因，此时成绩仍未最终确认。检查点类型固定为普通后台任务检查点，
**不具备 LangGraph 检查点恢复能力**，进程中断后的遗留任务只能被收敛为中断失败并由教师
显式请求重评。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.domain.enums import (
    AnswerStatus,
    GradingStatus,
    ReviewStatus,
    SubmissionStatus,
    ValidationStatus,
    WorkflowStatus,
)
from backend.app.models import (
    Answer,
    Exam,
    ExamResult,
    GradingResult,
    Submission,
    WorkflowRun,
)
from backend.app.schemas.ai import GradingResult as GradingResultPayload
from backend.app.schemas.grading import (
    ConfidenceDecisionDTO,
    ExamResultDTO,
    ExamResultStatus,
    ExpectedAnswer,
    GradingTaskStatus,
    GradingTaskStatusDTO,
    QuestionResultDTO,
    SubmissionContext,
)
from backend.app.services.grading.confidence_policy import ConfidenceDecision
from backend.app.services.grading.grading_task_service import (
    GradingOutcome,
    GradingResultOwnershipError,
    GradingStoreNotReadyError,
    GradingSubmissionNotFoundError,
    GradingTaskError,
    GradingTaskTraceMissingError,
)
from backend.app.services.grading.result_aggregator import ResultAggregator

#: 需要就绪检查的持久化表；缺表说明迁移未执行。
REQUIRED_TABLES: Final[tuple[str, ...]] = (
    "grading_results",
    "exam_results",
    "workflow_runs",
)

#: 任务状态到工作流状态的默认映射；待复核在 :meth:`_apply_task` 中改写为 ``Paused``。
TASK_TO_WORKFLOW_STATUS: Final[Mapping[GradingTaskStatus, WorkflowStatus]] = {
    GradingTaskStatus.QUEUED: WorkflowStatus.QUEUED,
    GradingTaskStatus.RUNNING: WorkflowStatus.RUNNING,
    GradingTaskStatus.COMPLETED: WorkflowStatus.COMPLETED,
    GradingTaskStatus.FAILED: WorkflowStatus.FAILED,
}

#: 工作流状态回读为任务状态：``Paused`` 表示“本轮评分完成、成绩待复核”。
WORKFLOW_TO_TASK_STATUS: Final[Mapping[WorkflowStatus, GradingTaskStatus]] = {
    WorkflowStatus.QUEUED: GradingTaskStatus.QUEUED,
    WorkflowStatus.RUNNING: GradingTaskStatus.RUNNING,
    WorkflowStatus.PAUSED: GradingTaskStatus.COMPLETED,
    WorkflowStatus.COMPLETED: GradingTaskStatus.COMPLETED,
    WorkflowStatus.FAILED: GradingTaskStatus.FAILED,
}

#: 检查点类型标识：普通后台任务检查点，不宣称 LangGraph 恢复能力。
CHECKPOINT_KIND: Final[str] = "background-task-checkpoint"

#: 存在待人工复核题目时的暂停原因。
PENDING_REVIEW_PAUSE_REASON: Final[str] = (
    "存在待人工复核题目：本轮评分已完成，成绩尚未最终确认。"
)

#: 进程中断任务的失败说明（脱敏）。
INTERRUPTED_TASK_MESSAGE: Final[str] = "进程中断导致任务未完成，请显式请求重评。"

#: 进程中断任务的错误码。
GRADING_TASK_INTERRUPTED: Final[str] = "GRADING_TASK_INTERRUPTED"


class DatabaseGradingRepository:
    """基于 PostgreSQL 的阅卷结果与任务状态存储。

    :param session_factory: 会话工厂；每次调用自建并在使用后关闭，绝不复用请求作用域会话。
    :param clock: 时间来源，便于测试固定时间。
    """

    #: 任务状态持久化在 ``workflow_runs``，可跨进程重启查询。
    durable: bool = True

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._aggregator = ResultAggregator()

    # ------------------------------------------------------------------ 会话
    @contextmanager
    def _use_session(self) -> Iterator[Session]:
        """自建并关闭会话；不跨线程复用、不嵌套请求事务。"""

        session = self._session_factory()
        try:
            yield session
        finally:
            session.close()

    def _probe(self, session: Session) -> None:
        """探测结果与任务表是否可用；缺表或连接失败即视为未就绪。"""

        for table in REQUIRED_TABLES:
            session.execute(text(f"SELECT 1 FROM {table} LIMIT 1"))

    def ensure_ready(self) -> None:
        """最小就绪判断；未迁移或不可连接时抛可诊断的未就绪错误。"""

        try:
            with self._use_session() as session:
                self._probe(session)
        except SQLAlchemyError as error:
            raise GradingStoreNotReadyError(
                "结果存储未就绪："
                f"{type(error).__name__}；请确认数据库可连接且迁移已执行到 head。"
            ) from None

    # ------------------------------------------------------- 并发与中断收敛
    @contextmanager
    def lock_submission(self, submission_id: str) -> Iterator[None]:
        """在短事务内锁定答卷行，供“检查已有任务—创建任务”互斥执行。

        仅 PostgreSQL 使用 ``SELECT ... FOR NO KEY UPDATE``：该锁与插入 ``workflow_runs`` 时对
        ``submissions`` 的外键 ``FOR KEY SHARE`` 检查兼容，同时仍能阻止并发触发同时创建任务
        （使用强 ``FOR UPDATE`` 会与子表插入互相阻塞）。其它方言（测试用 SQLite）退化为普通
        读取，不伪装成已加锁。
        """

        with self._use_session() as session:
            submission = session.get(Submission, _as_uuid(submission_id))
            if submission is None:
                raise GradingSubmissionNotFoundError(
                    f"答卷 {submission_id} 不存在。"
                )
            if session.get_bind().dialect.name == "postgresql":
                session.execute(
                    select(Submission.id)
                    .where(Submission.id == submission.id)
                    .with_for_update(key_share=True)
                )
            try:
                yield
            except Exception:
                session.rollback()
                raise
            session.commit()

    def mark_interrupted_tasks_failed(self) -> int:
        """把遗留的 ``Queued``/``Running`` 任务收敛为中断失败，返回处理条数。

        单进程部署下进程重启不可能存在仍在运行的任务；这里如实标记为中断失败并把对应
        答案标记为失败，由教师显式请求重评重新执行。不实现自动恢复队列。
        """

        with self._use_session() as session, session.begin():
            rows = list(
                session.scalars(
                    select(WorkflowRun).where(
                        WorkflowRun.status.in_(
                            (WorkflowStatus.QUEUED, WorkflowStatus.RUNNING)
                        )
                    )
                )
            )
            for row in rows:
                self._write_failure(
                    row,
                    error_code=GRADING_TASK_INTERRUPTED,
                    error_message=INTERRUPTED_TASK_MESSAGE,
                    retryable=False,
                )
                self._mark_answers_failed(session, row.submission_id)
            return len(rows)

    # ------------------------------------------------------------ 任务状态
    def get_task(self, task_id: str) -> GradingTaskStatusDTO | None:
        """按任务标识读取任务状态；不存在时返回 ``None``。"""

        with self._use_session() as session:
            row = session.scalars(
                select(WorkflowRun).where(WorkflowRun.workflow_id == task_id)
            ).one_or_none()
            if row is None:
                return None
            return self._task_dto(row)

    def find_task_for_submission(
        self,
        submission_id: str,
    ) -> GradingTaskStatusDTO | None:
        """返回该答卷最近一次任务（含重评产生的历史任务）。"""

        with self._use_session() as session:
            rows = list(
                session.scalars(
                    select(WorkflowRun).where(
                        WorkflowRun.submission_id == _as_uuid(submission_id)
                    )
                )
            )
            if not rows:
                return None
            latest = max(rows, key=lambda item: (item.created_at, item.updated_at))
            return self._task_dto(latest)

    def save_task(
        self,
        task: GradingTaskStatusDTO,
        *,
        request_id: str | None = None,
    ) -> None:
        """单事务写入任务状态、检查点与一致的答卷进度。

        创建任务时必须提供 ``request_id``（plan §7），更新已有任务时可省略。
        任务进入失败状态时，同一事务把该答卷的答案标记为失败。
        """

        with self._use_session() as session, session.begin():
            row = session.scalars(
                select(WorkflowRun).where(
                    WorkflowRun.workflow_id == task.task_id
                )
            ).one_or_none()
            if row is None:
                if request_id is None:
                    raise GradingTaskTraceMissingError(
                        "创建任务必须提供 request_id（plan §7 要求的追踪标识）。"
                    )
                submission = session.get(Submission, _as_uuid(task.submission_id))
                if submission is None:
                    raise GradingSubmissionNotFoundError(
                        f"答卷 {task.submission_id} 不存在。"
                    )
                row = WorkflowRun(
                    workflow_id=task.task_id,
                    request_id=request_id,
                    submission_id=submission.id,
                )
                session.add(row)
                session.flush()
            elif request_id is not None:
                row.request_id = request_id
            self._apply_task(row, task)
            if task.status is GradingTaskStatus.FAILED:
                self._mark_answers_failed(session, row.submission_id)

    # ------------------------------------------------------------ 整卷结果
    def get_exam_result(self, submission_id: str) -> ExamResultDTO | None:
        """读回整卷结果：汇总事实取落库值，逐题明细由已保存评分行重建。"""

        with self._use_session() as session:
            exam_result = session.scalars(
                select(ExamResult).where(
                    ExamResult.submission_id == _as_uuid(submission_id)
                )
            ).one_or_none()
            if exam_result is None:
                return None
            return self._rebuild_exam_result(session, exam_result)

    def save_exam_result(self, exam_result: ExamResultDTO) -> None:
        """保存整卷结果与其携带的单题结果（不涉及任务检查点）。

        生产执行路径使用 :meth:`save_outcome`；本方法是结果读模型与测试可复用的显式入口，
        同样按 ``answer_id`` 就地更新，不做批量删除。
        """

        payloads: list[GradingResultPayload] = []
        decisions: dict[str, ConfidenceDecision] = {}
        for item in exam_result.items:
            if item.score is None:
                continue
            payloads.append(_payload_from_item(item))
            if item.decision is not None:
                decisions[item.answer_id] = _decision_from_dto(item.decision)
        self.save_outcome(
            exam_result.submission_id,
            GradingOutcome(
                results=tuple(payloads),
                decisions=decisions,
                exam_result=exam_result,
            ),
        )

    def save_outcome(
        self,
        submission_id: str,
        outcome: GradingOutcome,
        *,
        task_id: str | None = None,
        answer_order: Sequence[str] | None = None,
    ) -> None:
        """在一次事务内提交单题结果、决策快照、整卷结果、答卷进度与检查点。

        任何校验失败或写入异常都会回滚整个事务：不会出现“整卷结果已落库但单题结果缺失”
        的中间态。 ``answer_order`` 是本次快照的答案顺序（题序 + ``answer_id`` 序列），
        写入检查点供读回时复用。
        """

        with self._use_session() as session, session.begin():
            submission_uuid = _as_uuid(submission_id)
            submission = session.get(Submission, submission_uuid)
            if submission is None:
                raise GradingSubmissionNotFoundError(
                    f"答卷 {submission_id} 不存在。"
                )
            exam_result = outcome.exam_result
            if exam_result is None:
                raise GradingTaskError("整批保存缺少整卷结果，拒绝写入。")
            if _as_uuid(exam_result.submission_id) != submission_uuid:
                raise GradingResultOwnershipError(
                    "整卷结果与目标答卷不一致，拒绝写入。"
                )
            for payload in outcome.results:
                self._upsert_result(session, submission, payload, outcome.decisions)
            self._upsert_exam_result(session, submission, exam_result)
            self._mark_graded(session, submission)
            if task_id is not None:
                self._write_checkpoint(
                    session,
                    task_id,
                    answer_order=answer_order,
                    exam_result=exam_result,
                )

    # ------------------------------------------------------------ 单题结果
    def get_single_result(
        self,
        submission_id: str,
        answer_id: str,
    ) -> QuestionResultDTO | None:
        """读回单题结构化结果；缺少评分行时返回 ``None``（不补 0 分）。

        题目状态与分数由落库评分行重建；决策快照直接取已保存的阈值、置信度与原因，
        **不按当前配置重新执行置信度判断**。
        """

        with self._use_session() as session:
            row = session.scalars(
                select(GradingResult).where(
                    GradingResult.answer_id == _as_uuid(answer_id),
                    GradingResult.submission_id == _as_uuid(submission_id),
                )
            ).one_or_none()
            if row is None:
                return None
            entry = self._entry_from_row(session, row, order=1)
            payload = _payload_from_row(row)
            decisions = _decisions_from_row(row)
            aggregated = self._aggregator.aggregate(
                SubmissionContext(
                    submission_id=str(row.submission_id),
                    exam_id=str(_exam_id_of(session, row.submission_id)),
                    student_id=str(_student_id_of(session, row.submission_id)),
                    expected_answers=[entry],
                ),
                results=[payload],
                decisions=decisions,
            )
            item = _attach_decision(
                aggregated.items[0], decisions.get(str(row.answer_id))
            )
            return item.model_copy(update={"submission_id": str(row.submission_id)})

    def save_single_result(
        self,
        submission_id: str,
        result: QuestionResultDTO,
    ) -> None:
        """就地写入单题结果（按 ``answer_id`` 更新，保留主键与复核记录关联）。"""

        with self._use_session() as session, session.begin():
            submission = session.get(Submission, _as_uuid(submission_id))
            if submission is None:
                raise GradingSubmissionNotFoundError(
                    f"答卷 {submission_id} 不存在。"
                )
            if result.score is None:
                return
            decisions: Mapping[str, ConfidenceDecision] = (
                {result.answer_id: _decision_from_dto(result.decision)}
                if result.decision is not None
                else {}
            )
            self._upsert_result(
                session, submission, _payload_from_item(result), decisions
            )

    # ------------------------------------------------------------ 内部实现
    def _upsert_result(
        self,
        session: Session,
        submission: Submission,
        payload: GradingResultPayload,
        decisions: Mapping[str, ConfidenceDecision],
    ) -> None:
        """按 ``answer_id`` 就地写入单题结果与当次决策快照。"""

        if payload.answer_id is None:
            raise GradingTaskError("评分结果缺少答案标识，无法归属到答卷。")
        answer_uuid = _as_uuid(payload.answer_id)
        answer = session.get(Answer, answer_uuid)
        if answer is None or answer.submission_id != submission.id:
            raise GradingResultOwnershipError(
                f"答案 {payload.answer_id} 不属于目标答卷，拒绝写入评分结果。"
            )
        decision = decisions.get(payload.answer_id)
        row = session.scalars(
            select(GradingResult).where(GradingResult.answer_id == answer_uuid)
        ).one_or_none()
        if row is None:
            row = GradingResult(
                answer_id=answer_uuid,
                submission_id=submission.id,
                question_type=payload.question_type,
                score=_as_decimal(payload.score),
                max_score=_as_decimal(payload.max_score),
                reason=payload.reason,
                confidence=float(payload.confidence),
            )
            session.add(row)
        elif row.submission_id != submission.id:
            raise GradingResultOwnershipError(
                f"答案 {payload.answer_id} 已有属于其它答卷的评分结果，拒绝覆盖。"
            )
        row.question_type = payload.question_type
        row.score = _as_decimal(payload.score)
        row.max_score = _as_decimal(payload.max_score)
        row.reason = payload.reason
        row.correct_points = list(payload.correct_points)
        row.missing_knowledge_points = list(payload.missing_knowledge_points)
        row.knowledge_points = list(payload.knowledge_points)
        row.suggestions = list(payload.suggestions)
        row.retrieved_context_ids = list(payload.retrieved_context_ids)
        row.confidence = float(payload.confidence)
        row.validation_status = ValidationStatus(payload.validation_status)
        row.review_status = ReviewStatus(payload.review_status)
        if decision is None:
            row.decision_confidence = None
            row.decision_threshold = None
            row.decision_requires_review = None
            row.decision_review_status = None
            row.decision_grading_status = None
            row.decision_reason = None
        else:
            row.decision_confidence = float(decision.confidence)
            row.decision_threshold = float(decision.threshold)
            row.decision_requires_review = bool(decision.requires_review)
            row.decision_review_status = ReviewStatus(decision.review_status)
            row.decision_grading_status = _grading_status_of(decision)
            row.decision_reason = decision.reason

    def _upsert_exam_result(
        self,
        session: Session,
        submission: Submission,
        exam_result: ExamResultDTO,
    ) -> None:
        """按 ``submission_id`` 就地写入整卷结果与当次汇总事实。"""

        row = session.scalars(
            select(ExamResult).where(ExamResult.submission_id == submission.id)
        ).one_or_none()
        if row is None:
            row = ExamResult(
                submission_id=submission.id,
                exam_id=_as_uuid(exam_result.exam_id),
                student_id=_as_uuid(exam_result.student_id),
                result_status=exam_result.result_status,
                confirmed_subtotal=_as_decimal(exam_result.confirmed_subtotal),
                total_max_score=_as_decimal(exam_result.total_max_score),
                aggregated_at=exam_result.aggregated_at,
            )
            session.add(row)
        row.exam_id = _as_uuid(exam_result.exam_id)
        row.student_id = _as_uuid(exam_result.student_id)
        row.result_status = exam_result.result_status
        row.is_final = bool(exam_result.is_final)
        row.final_total_score = (
            None
            if exam_result.final_total_score is None
            else _as_decimal(exam_result.final_total_score)
        )
        row.confirmed_subtotal = _as_decimal(exam_result.confirmed_subtotal)
        row.total_max_score = _as_decimal(exam_result.total_max_score)
        row.aggregated_at = exam_result.aggregated_at

    def _mark_graded(self, session: Session, submission: Submission) -> None:
        """在同一事务内更新答卷与答案进度（不属于任何评分结果重算）。"""

        for answer in submission.answers:
            answer.status = AnswerStatus.GRADED
        submission.graded_at = self._clock()
        if submission.status is SubmissionStatus.SUBMITTED:
            submission.status = SubmissionStatus.GRADED

    def _mark_answers_failed(self, session: Session, submission_id: UUID) -> None:
        """把该答卷的全部答案标记为失败（与任务失败状态同事务）。"""

        submission = session.get(Submission, submission_id)
        if submission is None:
            return
        for answer in submission.answers:
            answer.status = AnswerStatus.FAILED

    def _write_checkpoint(
        self,
        session: Session,
        task_id: str,
        *,
        answer_order: Sequence[str] | None,
        exam_result: ExamResultDTO,
    ) -> None:
        """把答案顺序与整卷结果指针写入检查点，状态留给完成发布步骤。"""

        row = session.scalars(
            select(WorkflowRun).where(WorkflowRun.workflow_id == task_id)
        ).one_or_none()
        if row is None:
            return
        checkpoint = dict(row.checkpoint or {})
        checkpoint["kind"] = CHECKPOINT_KIND
        if answer_order is not None:
            checkpoint["answer_order"] = [str(item) for item in answer_order]
        checkpoint["current_node"] = "aggregate"
        row.current_node = "aggregate"
        row.exam_result_id = session.scalars(
            select(ExamResult.id).where(
                ExamResult.submission_id == _as_uuid(exam_result.submission_id)
            )
        ).one_or_none()
        row.checkpoint = checkpoint

    def _apply_task(self, row: WorkflowRun, task: GradingTaskStatusDTO) -> None:
        """把任务 DTO 写入工作流行，并按待复核语义决定 ``Paused``。"""

        pending_review = task.pending_review_answer_count or 0
        status = TASK_TO_WORKFLOW_STATUS[task.status]
        pause_reason: str | None = None
        if (
            task.status is GradingTaskStatus.COMPLETED
            and pending_review > 0
        ):
            status = WorkflowStatus.PAUSED
            pause_reason = PENDING_REVIEW_PAUSE_REASON
        row.status = status
        row.pause_reason = pause_reason
        row.resumable = False
        row.current_node = row.current_node or "score"
        row.checkpoint = self._checkpoint_for(row, task)

    def _checkpoint_for(
        self,
        row: WorkflowRun,
        task: GradingTaskStatusDTO,
    ) -> dict[str, Any]:
        """构造检查点：保留答案顺序，更新可还原任务查询 DTO 的时间与计数。"""

        checkpoint = dict(row.checkpoint or {})
        checkpoint["kind"] = CHECKPOINT_KIND
        snapshot = {
            "created_at": _isoformat(task.created_at),
            "started_at": _isoformat(task.started_at),
            "finished_at": _isoformat(task.finished_at),
            "expected_answer_count": task.expected_answer_count,
            "graded_answer_count": task.graded_answer_count,
            "pending_review_answer_count": task.pending_review_answer_count,
            "exam_result_status": (
                None if task.exam_result_status is None else task.exam_result_status.value
            ),
            "is_final": task.is_final,
            "error_code": task.error_code,
            "error_message": task.error_message,
            "retryable": task.retryable,
        }
        checkpoint["task"] = snapshot
        if row.status is WorkflowStatus.FAILED:
            checkpoint["current_node"] = "failed"
        stored_node = checkpoint.get("current_node")
        if isinstance(stored_node, str):
            row.current_node = stored_node
        return checkpoint

    @staticmethod
    def _write_failure(
        row: WorkflowRun,
        *,
        error_code: str,
        error_message: str,
        retryable: bool,
    ) -> None:
        """把工作流行标记为失败并保留脱敏错误信息。"""

        checkpoint = dict(row.checkpoint or {})
        checkpoint["kind"] = CHECKPOINT_KIND
        snapshot = dict(checkpoint.get("task") or {})
        snapshot.update(
            {
                "error_code": error_code,
                "error_message": error_message,
                "retryable": retryable,
            }
        )
        checkpoint["task"] = snapshot
        checkpoint["current_node"] = "failed"
        row.checkpoint = checkpoint
        row.status = WorkflowStatus.FAILED
        row.pause_reason = None
        row.resumable = False
        row.current_node = "failed"

    def _task_dto(self, row: WorkflowRun) -> GradingTaskStatusDTO:
        """由工作流行重建任务状态 DTO。"""

        checkpoint = row.checkpoint or {}
        snapshot = checkpoint.get("task") or {}
        created_at = _parse_datetime(snapshot.get("created_at")) or row.created_at
        return GradingTaskStatusDTO(
            task_id=row.workflow_id,
            submission_id=str(row.submission_id),
            status=WORKFLOW_TO_TASK_STATUS[row.status],
            durable=True,
            reused=False,
            created_at=created_at,
            started_at=_parse_datetime(snapshot.get("started_at")),
            finished_at=_parse_datetime(snapshot.get("finished_at")),
            expected_answer_count=_optional_int(snapshot.get("expected_answer_count")),
            graded_answer_count=_optional_int(snapshot.get("graded_answer_count")),
            pending_review_answer_count=_optional_int(
                snapshot.get("pending_review_answer_count")
            ),
            exam_result_status=_optional_status(snapshot.get("exam_result_status")),
            is_final=_optional_bool(snapshot.get("is_final")),
            error_code=_optional_str(snapshot.get("error_code")),
            error_message=_optional_str(snapshot.get("error_message")),
            retryable=_optional_bool(snapshot.get("retryable")),
        )

    def _rebuild_exam_result(
        self,
        session: Session,
        exam_result: ExamResult,
    ) -> ExamResultDTO:
        """由落库事实重建整卷结果 DTO：明细用评分行，汇总用落库值。"""

        rows = list(
            session.scalars(
                select(GradingResult).where(
                    GradingResult.submission_id == exam_result.submission_id
                )
            )
        )
        entries, payloads, decisions = self._rebuild_items(
            session, exam_result, rows
        )
        aggregated = self._aggregator.aggregate(
            SubmissionContext(
                submission_id=str(exam_result.submission_id),
                exam_id=str(exam_result.exam_id),
                student_id=str(exam_result.student_id),
                expected_answers=entries,
            ),
            results=payloads,
            decisions=decisions,
        )
        items = [
            _attach_decision(item, decisions.get(item.answer_id))
            for item in aggregated.items
        ]
        return aggregated.model_copy(
            update={
                "result_status": exam_result.result_status,
                "is_final": exam_result.is_final,
                "final_total_score": (
                    None
                    if exam_result.final_total_score is None
                    else _as_decimal(exam_result.final_total_score)
                ),
                "confirmed_subtotal": _as_decimal(exam_result.confirmed_subtotal),
                "total_max_score": _as_decimal(exam_result.total_max_score),
                "aggregated_at": exam_result.aggregated_at,
                "items": items,
            }
        )

    def _rebuild_items(
        self,
        session: Session,
        exam_result: ExamResult,
        rows: Sequence[GradingResult],
    ) -> tuple[list[ExpectedAnswer], list[GradingResultPayload], dict[str, ConfidenceDecision]]:
        """按检查点题序重排评分行，缺失题目按考试定义补为空位。"""

        rows_by_answer = {str(row.answer_id): row for row in rows}
        answers = {
            str(answer.id): answer
            for answer in session.scalars(
                select(Answer).where(
                    Answer.submission_id == exam_result.submission_id
                )
            )
        }
        exam = session.get(Exam, exam_result.exam_id)
        questions = {str(question.id): question for question in (exam.questions if exam else [])}
        ordered = list(self._stored_answer_order(session, exam_result))
        for answer_id in rows_by_answer:
            if answer_id not in ordered:
                ordered.append(answer_id)
        entries: list[ExpectedAnswer] = []
        payloads: list[GradingResultPayload] = []
        decisions: dict[str, ConfidenceDecision] = {}
        for index, answer_id in enumerate(ordered, start=1):
            row = rows_by_answer.get(answer_id)
            answer = answers.get(answer_id)
            if row is not None and answer is not None:
                entries.append(
                    self._entry_from_row(session, row, order=index)
                )
                payloads.append(_payload_from_row(row))
                decisions.update(_decisions_from_row(row))
                continue
            if answer is None:
                continue
            question = questions.get(str(answer.question_id))
            if question is None:
                continue
            entries.append(
                ExpectedAnswer(
                    order=index,
                    answer_id=answer_id,
                    question_id=str(question.id),
                    question_type=question.type,
                    max_score=_as_decimal(question.score),
                    knowledge_points=list(question.knowledge_points or ()),
                )
            )
        return entries, payloads, decisions

    @staticmethod
    def _entry_from_row(
        session: Session,
        row: GradingResult,
        *,
        order: int,
    ) -> ExpectedAnswer:
        """由评分行与所属答案构造预期条目：题型、满分与知识点取落库事实。"""

        answer = session.get(Answer, row.answer_id)
        question_id = None if answer is None else str(answer.question_id)
        if question_id is None:
            raise GradingTaskError(
                f"评分行 {row.answer_id} 缺少关联答案，无法重建题序。"
            )
        return ExpectedAnswer(
            order=order,
            answer_id=str(row.answer_id),
            question_id=question_id,
            question_type=row.question_type,
            max_score=_as_decimal(row.max_score),
            knowledge_points=list(row.knowledge_points),
        )

    @staticmethod
    def _stored_answer_order(
        session: Session,
        exam_result: ExamResult,
    ) -> tuple[str, ...]:
        """返回本次快照的答案顺序。

        权威来源是工作流检查点里的 ``answer_order``（生产路径在提交结果时必写入）。
        无检查点时退化为考试题目集合顺序：该顺序取决于题目关系的读取顺序，
        **不得当作正式题序保证**，因此正式读取必须依赖检查点。
        """

        row = session.scalars(
            select(WorkflowRun)
            .where(WorkflowRun.submission_id == exam_result.submission_id)
            .order_by(WorkflowRun.created_at.desc())
        ).first()
        if row is not None:
            checkpoint = row.checkpoint or {}
            stored = checkpoint.get("answer_order")
            if isinstance(stored, list) and stored:
                return tuple(str(item) for item in stored)
        exam = session.get(Exam, exam_result.exam_id)
        if exam is None:
            return ()
        answers = {
            str(answer.question_id): str(answer.id)
            for answer in session.scalars(
                select(Answer).where(
                    Answer.submission_id == exam_result.submission_id
                )
            )
        }
        return tuple(
            answers[str(question.id)]
            for question in exam.questions
            if str(question.id) in answers
        )


def _payload_from_item(item: QuestionResultDTO) -> GradingResultPayload:
    """把汇总明细还原为单题评分结果；仅用于结果入口的显式写入。"""

    return GradingResultPayload(
        question_type=item.question_type,
        score=float(item.score or 0),
        max_score=float(item.max_score),
        reason=item.reason or "（未提供评分理由）",
        correct_points=list(item.correct_points),
        missing_knowledge_points=list(item.missing_knowledge_points),
        knowledge_points=list(item.knowledge_points),
        suggestions=list(item.suggestions),
        confidence=float(item.confidence or 0),
        validation_status=item.validation_status or ValidationStatus.VALIDATED.value,
        review_status=item.review_status or ReviewStatus.NOT_REQUIRED.value,
        retrieved_context_ids=list(item.retrieved_context_ids),
        answer_id=item.answer_id,
        submission_id=item.submission_id,
    )


def _payload_from_row(row: GradingResult) -> GradingResultPayload:
    """把评分行还原为单题评分结果（保留原始置信度，不重算决策）。"""

    return GradingResultPayload(
        question_type=row.question_type,
        score=float(row.score),
        max_score=float(row.max_score),
        reason=row.reason,
        correct_points=list(row.correct_points),
        missing_knowledge_points=list(row.missing_knowledge_points),
        knowledge_points=list(row.knowledge_points),
        suggestions=list(row.suggestions),
        confidence=float(row.confidence),
        validation_status=row.validation_status.value,
        review_status=row.review_status.value,
        retrieved_context_ids=list(row.retrieved_context_ids),
        answer_id=str(row.answer_id),
        submission_id=str(row.submission_id),
    )


def _decisions_from_row(row: GradingResult) -> dict[str, ConfidenceDecision]:
    """由决策快照列还原当次决策；六列全空时返回空映射。"""

    if row.decision_confidence is None:
        return {}
    if (
        row.decision_threshold is None
        or row.decision_requires_review is None
        or row.decision_review_status is None
        or row.decision_grading_status is None
        or row.decision_reason is None
    ):
        raise GradingTaskError(
            f"评分行 {row.answer_id} 的决策快照不完整，拒绝按当前配置重算历史决策。"
        )
    return {
        str(row.answer_id): ConfidenceDecision(
            confidence=float(row.decision_confidence),
            threshold=float(row.decision_threshold),
            requires_review=bool(row.decision_requires_review),
            review_status=row.decision_review_status.value,
            grading_status=row.decision_grading_status.value,
            reason=row.decision_reason,
        )
    }


def _decision_from_dto(dto: ConfidenceDecisionDTO) -> ConfidenceDecision:
    """把决策快照 DTO 转换为决策对象。"""

    return ConfidenceDecision(
        confidence=float(dto.confidence),
        threshold=float(dto.threshold),
        requires_review=bool(dto.requires_review),
        review_status=dto.review_status,
        grading_status=dto.grading_status,
        reason=dto.reason,
    )


def _grading_status_of(decision: ConfidenceDecision) -> GradingStatus:
    """解析决策中的评分状态；未知取值显式失败。"""

    try:
        return GradingStatus(decision.grading_status)
    except ValueError:  # pragma: no cover - 防御性分支
        raise GradingTaskError(
            f"决策中的评分状态 {decision.grading_status} 无法映射。"
        ) from None


def _exam_id_of(session: Session, submission_id: UUID) -> UUID:
    """返回答卷关联的考试标识。"""

    exam_id = session.scalars(
        select(Submission.exam_id).where(Submission.id == submission_id)
    ).one_or_none()
    if exam_id is None:
        raise GradingSubmissionNotFoundError("答卷不存在，无法读取单题结果。")
    return exam_id


def _student_id_of(session: Session, submission_id: UUID) -> UUID:
    """返回答卷所属学生标识。"""

    student_id = session.scalars(
        select(Submission.student_id).where(Submission.id == submission_id)
    ).one_or_none()
    if student_id is None:
        raise GradingSubmissionNotFoundError("答卷不存在，无法读取单题结果。")
    return student_id


def _decision_dto(decision: ConfidenceDecision) -> ConfidenceDecisionDTO:
    """把决策对象转换为可读写快照 DTO。"""

    return ConfidenceDecisionDTO(
        confidence=decision.confidence,
        threshold=decision.threshold,
        requires_review=decision.requires_review,
        review_status=decision.review_status,
        grading_status=decision.grading_status,
        reason=decision.reason,
    )


def _attach_decision(
    item: QuestionResultDTO,
    decision: ConfidenceDecision | None,
) -> QuestionResultDTO:
    """把已保存的决策快照附加到单题 DTO。

    汇总阶段只为“自动接受”的主观题填充决策，待复核题不携带该字段；
    这里把落库的当次决策（含阈值与原因）如实回填，不重算历史结论。
    """

    if decision is None:
        return item
    return item.model_copy(update={"decision": _decision_dto(decision)})


def _as_uuid(value: str | UUID) -> UUID:
    """把字符串标识转换为 UUID；非法标识按未找到处理。"""

    if isinstance(value, UUID):
        return value
    try:
        return UUID(value)
    except ValueError as error:
        raise GradingSubmissionNotFoundError("标识非法。") from error


def _as_decimal(value: object) -> Decimal:
    """把分值转换为 Decimal。"""

    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _isoformat(value: datetime | None) -> str | None:
    """把时间转换为 ISO 字符串；``None`` 保持为空。"""

    return None if value is None else value.isoformat()


def _parse_datetime(value: object) -> datetime | None:
    """解析检查点中的 ISO 时间；非法值返回 ``None``。"""

    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _optional_int(value: object) -> int | None:
    """解析检查点中的整数；非法值返回 ``None``。"""

    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _optional_bool(value: object) -> bool | None:
    """解析检查点中的布尔值；非法值返回 ``None``。"""

    return value if isinstance(value, bool) else None


def _optional_str(value: object) -> str | None:
    """解析检查点中的字符串；非法值返回 ``None``。"""

    return value if isinstance(value, str) and value else None


def _optional_status(value: object) -> ExamResultStatus | None:
    """解析检查点中的整卷状态。"""

    if not isinstance(value, str):
        return None
    try:
        return ExamResultStatus(value)
    except ValueError:
        return None


__all__ = [
    "CHECKPOINT_KIND",
    "GRADING_TASK_INTERRUPTED",
    "INTERRUPTED_TASK_MESSAGE",
    "PENDING_REVIEW_PAUSE_REASON",
    "REQUIRED_TABLES",
    "TASK_TO_WORKFLOW_STATUS",
    "WORKFLOW_TO_TASK_STATUS",
    "DatabaseGradingRepository",
]
