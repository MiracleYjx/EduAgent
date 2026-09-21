"""T076 Workflow API：LangGraph 阅卷启动、状态查询与恢复。

契约依据：T076（启动/状态/恢复端点）、plan.md §5/§5.2、FR-029～FR-038、T060 快照读取、
T061 诊断记录器、T069 阅卷 Agent、T072 图与教师决策契约、T073 持久检查点、T074 复核服务。

边界与不变量：

- **幂等启动**：以“同一答卷的活动运行”为幂等边界。重复启动复用既有 ``workflow_id``/
  ``thread_id``，不为同一答卷并行创建两个活动运行；草稿答卷或已完成运行显式返回 409。
- **执行归属**：``workflow_runs`` 由 M3 后台任务与 M4 工作流共用。本模块只读写 kind 为 T065
  业务状态载荷的 M4 运行行（:func:`run_kind_criteria`）；M3 后台任务检查点不参与 M4 的幂等
  启动、状态判定与恢复，反之 M3 的查询也只读自己的 kind。
- **公开状态事实源**：``WorkflowRun`` 行（业务状态载荷）是 API 状态事实源；LangGraph runtime
  检查点只供恢复使用，不序列化给客户端。``resumable=True`` 之前必须用
  :meth:`WorkflowCheckpointStore.runtime_ready` 确认同一 ``thread_id`` 的持久 runtime 检查点真实存在。
- **唯一恢复入口**：本模块的 ``POST /api/workflow/runs/{workflow_id}/resume`` 是唯一恢复语义。
  暂停在人工复核时**必须**已有教师结论（``review_records``），并委托 T074
  ``ReviewService.resume_recorded_decision_async`` 继续原图；不得用裸 ``resume_async`` 绕过教师确认。
  系统故障暂停才走 T072 ``resume_async``，且要求运行确实带持久 runtime 检查点。
- **角色差异**：教师可见节点、暂停原因、待复核答案与错误来源；学生只能看到自己的进度与公开状态，
  不返回暂定分数、教师参考答案、评分标准、内部错误或检查点载荷。
- **诊断适配（B07）**：T061 ``DiagnosisRecorder.record`` 内部使用 ``asyncio.run``，不能在事件循环内
  调用。本模块的 :class:`DiagnosisRecorderAdapter` 提供真正可等待的
  ``async generate``（供 T072 诊断节点）与线程边界的同步 ``record``（供 T074 复核服务），
  两者都返回经 Pydantic 校验的 ``DiagnosisReportDTO``，保存走独立短事务。

错误语义：401 未认证、403 无权限或跨课程、404 运行/答卷不存在、409 状态冲突（未提交答卷、
已完成运行、人工复核未结论、运行不可恢复）、422 输入非法、503 依赖未就绪（存储、Checkpointer、
复核服务）。运行中途故障写入真实运行状态，不映射回“启动前未就绪”。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.ai.agents.grading_agent import GradingAgent
from backend.app.ai.workflows.grading_handoff import LOAD_SUBMISSION
from backend.app.ai.workflows.grading_workflow import (
    GradingWorkflow,
    GradingWorkflowDeps,
    GradingWorkflowError,
    TeacherReviewDecision,
)
from backend.app.core.config import AppSettings
from backend.app.core.database import get_session_factory
from backend.app.core.security import (
    get_user_roles,
    require_any_permission,
    require_permission,
)
from backend.app.domain.enums import (
    SubmissionStatus,
    UserRole,
    WorkflowStatus,
)
from backend.app.domain.permissions import Permission
from backend.app.models import GradingResult, ReviewRecord, User, WorkflowRun
from backend.app.schemas.grading import DiagnosisReportDTO
from backend.app.services.diagnosis_service import DiagnosisService
from backend.app.services.grading.diagnosis_report_store import (
    STORABLE_STATUSES,
    DiagnosisReportStore,
)
from backend.app.services.grading.grading_repository import DatabaseGradingRepository
from backend.app.services.grading.grading_task_service import (
    GRADING_PERMISSION_DENIED,
    GRADING_STORE_NOT_READY,
    GRADING_SUBMISSION_INCOMPLETE,
    GRADING_SUBMISSION_NOT_FOUND,
    DatabaseGradingSubmissionReader,
    GradingTaskError,
)
from backend.app.services.review_service import (
    REVIEW_SERVICE_AGGREGATION_FAILED,
    REVIEW_SERVICE_ANSWER_NOT_FOUND,
    REVIEW_SERVICE_CONFLICT,
    REVIEW_SERVICE_IDENTITY_MISMATCH,
    REVIEW_SERVICE_INVALID_DECISION,
    REVIEW_SERVICE_NOT_READY,
    REVIEW_SERVICE_PERMISSION_DENIED,
    REVIEW_SERVICE_REVISION_REQUIRED,
    REVIEW_SERVICE_STALE_DECISION,
    DatabaseReviewRecordStore,
    ReviewOutcome,
    ReviewService,
    ReviewServiceError,
)
from backend.app.services.workflow_checkpoint import (
    WORKFLOW_CHECKPOINT_NOT_FOUND,
    WORKFLOW_CHECKPOINT_STORE_NOT_READY,
    WORKFLOW_CHECKPOINT_THREAD_UNBOUND,
    DatabaseCheckpointSaver,
    WorkflowCheckpointError,
    WorkflowCheckpointStore,
    checkpoint_thread_id,
    pending_review_answer_ids,
    run_kind_criteria,
)

router = APIRouter(prefix="/api/workflow", tags=["阅卷工作流"])

#: API 层错误码：只补充接口语义，不重定义 T060/T072/T073/T074 的错误码。
WORKFLOW_SERVICE_NOT_READY: str = "WORKFLOW_SERVICE_NOT_READY"
WORKFLOW_RUN_NOT_FOUND: str = "WORKFLOW_RUN_NOT_FOUND"
WORKFLOW_RUN_PERMISSION_DENIED: str = "WORKFLOW_RUN_PERMISSION_DENIED"
WORKFLOW_SUBMISSION_NOT_READY: str = "WORKFLOW_SUBMISSION_NOT_READY"
WORKFLOW_RUN_ALREADY_COMPLETED: str = "WORKFLOW_RUN_ALREADY_COMPLETED"
WORKFLOW_RUN_REGRADE_UNSUPPORTED: str = "WORKFLOW_RUN_REGRADE_UNSUPPORTED"
WORKFLOW_REVIEW_DECISION_REQUIRED: str = "WORKFLOW_REVIEW_DECISION_REQUIRED"
WORKFLOW_RUN_NOT_RESUMABLE: str = "WORKFLOW_RUN_NOT_RESUMABLE"
WORKFLOW_DIAGNOSIS_FAILED: str = "WORKFLOW_DIAGNOSIS_FAILED"

#: 错误码到 HTTP 状态码的映射；未列出的错误按 500 处理并保持脱敏。
_ERROR_STATUS: dict[str, int] = {
    WORKFLOW_SERVICE_NOT_READY: 503,
    WORKFLOW_CHECKPOINT_STORE_NOT_READY: 503,
    GRADING_STORE_NOT_READY: 503,
    REVIEW_SERVICE_NOT_READY: 503,
    WORKFLOW_RUN_NOT_FOUND: 404,
    WORKFLOW_CHECKPOINT_NOT_FOUND: 404,
    GRADING_SUBMISSION_NOT_FOUND: 404,
    REVIEW_SERVICE_ANSWER_NOT_FOUND: 404,
    WORKFLOW_RUN_PERMISSION_DENIED: 403,
    GRADING_PERMISSION_DENIED: 403,
    REVIEW_SERVICE_PERMISSION_DENIED: 403,
    WORKFLOW_SUBMISSION_NOT_READY: 409,
    WORKFLOW_RUN_ALREADY_COMPLETED: 409,
    WORKFLOW_RUN_REGRADE_UNSUPPORTED: 409,
    WORKFLOW_REVIEW_DECISION_REQUIRED: 409,
    WORKFLOW_RUN_NOT_RESUMABLE: 409,
    WORKFLOW_CHECKPOINT_THREAD_UNBOUND: 409,
    GRADING_SUBMISSION_INCOMPLETE: 409,
    REVIEW_SERVICE_STALE_DECISION: 409,
    REVIEW_SERVICE_CONFLICT: 409,
    REVIEW_SERVICE_IDENTITY_MISMATCH: 409,
    REVIEW_SERVICE_INVALID_DECISION: 422,
    REVIEW_SERVICE_REVISION_REQUIRED: 422,
    REVIEW_SERVICE_AGGREGATION_FAILED: 500,
}

#: 视为“活动中”的运行状态：重复启动复用这些运行。
ACTIVE_RUN_STATUSES: frozenset[WorkflowStatus] = frozenset(
    {WorkflowStatus.RUNNING, WorkflowStatus.PAUSED}
)

#: 允许启动阅卷的答卷生命周期状态（既有的提交生命周期）。
STARTABLE_SUBMISSION_STATUSES: frozenset[str] = frozenset({SubmissionStatus.SUBMITTED.value})


class WorkflowRunError(RuntimeError):
    """工作流端点边界错误；保留脱敏错误码与可重试语义。"""

    error_code: str = WORKFLOW_SERVICE_NOT_READY
    retryable: bool = False
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


class WorkflowNotReadyError(WorkflowRunError):
    """依赖未接线或存储未就绪。"""

    error_code = WORKFLOW_SERVICE_NOT_READY


class WorkflowRunNotFoundError(WorkflowRunError):
    """运行记录不存在。"""

    error_code = WORKFLOW_RUN_NOT_FOUND


class WorkflowRunPermissionError(WorkflowRunError):
    """无权限访问该运行或其答卷。"""

    error_code = WORKFLOW_RUN_PERMISSION_DENIED


class WorkflowSubmissionNotReadyError(WorkflowRunError):
    """答卷生命周期不允许启动阅卷。"""

    error_code = WORKFLOW_SUBMISSION_NOT_READY


class WorkflowRunConflictError(WorkflowRunError):
    """运行状态冲突（已完成或需要显式重评）。"""

    error_code = WORKFLOW_RUN_ALREADY_COMPLETED


class WorkflowReviewDecisionRequiredError(WorkflowRunError):
    """暂停在人工复核：必须先有教师结论才能恢复。"""

    error_code = WORKFLOW_REVIEW_DECISION_REQUIRED


class WorkflowRunNotResumableError(WorkflowRunError):
    """运行不具备可恢复条件。"""

    error_code = WORKFLOW_RUN_NOT_RESUMABLE


class DiagnosisRecorderAdapter:
    """T061 诊断记录器的线程边界适配器（B07）。

    - :meth:`generate`：T072 诊断节点使用，真正 ``await`` T061 ``DiagnosisService.generate``，
      再用 ``asyncio.to_thread`` 把同步存储写入放到线程边界，返回 ``DiagnosisReportDTO``；
    - :meth:`record`：T074 复核服务在事件循环内**同步**调用，因此这里在线程池中运行独立事件循环，
      避免在工作事件循环内执行 ``asyncio.run``。

    两条路径都不修改 T061 代码，只替换不兼容的同步包装。
    """

    def __init__(self, *, store: DiagnosisReportStore, service: DiagnosisService) -> None:
        self._store = store
        self._service = service

    async def generate(self, exam_result: Any) -> DiagnosisReportDTO:
        """可等待的诊断生成：先生成，再在独立短事务保存。"""

        report = await self._service.generate(exam_result)
        if report.status not in STORABLE_STATUSES:
            return report
        return await asyncio.to_thread(self._store.save, report)

    def record(self, exam_result: Any) -> DiagnosisReportDTO:
        """同步入口：在线程边界内运行独立事件循环，禁止在事件循环内直接 ``asyncio.run``。"""

        def _run() -> DiagnosisReportDTO:
            return asyncio.run(self.generate(exam_result))

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return _run()
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(_run).result()


# ---------------------------------------------------------------------- 请求/响应 DTO


class WorkflowRunStartRequest(BaseModel):
    """启动请求；``regrade`` 只用于显式表达意图，本批不支持自动重评。"""

    model_config = ConfigDict(extra="forbid")

    regrade: bool = Field(default=False, description="是否请求重评已完成答卷。")


class WorkflowRunResumeRequest(BaseModel):
    """恢复请求；人工复核暂停时必须给出 ``answer_id``。"""

    model_config = ConfigDict(extra="forbid")

    answer_id: str | None = Field(default=None, max_length=64, description="恢复目标答案。")
    expected_review_status: str | None = Field(
        default=None,
        max_length=32,
        description="期望的当前复核状态；不匹配即拒绝，防陈旧覆盖。",
    )
    comment: str | None = Field(default=None, max_length=2000, description="恢复说明。")


class TeacherWorkflowRunDTO(BaseModel):
    """教师视图：完整运行事实（含节点、暂停原因与待复核答案）。"""

    workflow_id: str
    submission_id: str
    request_id: str
    status: WorkflowStatus
    current_node: str | None = None
    pause_reason: str | None = None
    resumable: bool
    interrupted: bool = False
    pending_answer_ids: list[str] = Field(default_factory=list)
    retry_count: int = 0
    thread_id: str | None = None
    reused: bool = False
    updated_at: datetime


class StudentWorkflowStatusDTO(BaseModel):
    """学生视图：只包含进度与公开状态。"""

    workflow_id: str
    submission_id: str
    status: WorkflowStatus
    finished: bool
    requires_teacher_review: bool
    pending_review_count: int
    updated_at: datetime


class WorkflowResumeOutcomeDTO(BaseModel):
    """恢复回执：区分“系统恢复完成”与“结论已保存、流程待继续”。"""

    workflow_id: str
    status: WorkflowStatus
    resumed: bool
    decision_saved: bool = False
    review_record_id: str | None = None
    resume_error_code: str | None = None
    pending_answer_ids: list[str] = Field(default_factory=list)
    resumable: bool
    pause_reason: str | None = None
    message: str


# ---------------------------------------------------------------------- 装配


def _workflow_deps(
    *,
    snapshot: Any,
    agent: Any,
    diagnosis_service: Any,
    session_factory: Callable[[], Session] | None,
    settings: AppSettings | None,
) -> GradingWorkflowDeps:
    """构造 T072 运行依赖；组件一律由调用方注入，不在节点内自建。"""

    return GradingWorkflowDeps(
        snapshot=snapshot,
        agent=agent,
        diagnosis_service=diagnosis_service,
        session_factory=session_factory,
        settings=settings,
    )


class WorkflowService:
    """阅卷工作流的启动、状态与恢复编排。

    :param checkpoints: T073 检查点存储（``WorkflowRun`` 业务状态事实源）。
    :param reader: T060 答卷快照读取（含教师课程归属校验）。
    :param session_factory: 自建会话工厂；Agent/LLM 调用前必须结束写事务。
    :param agent: T069 阅卷 Agent；``None`` 时无法运行（启动前显式 503）。
    :param diagnosis_service: T061 诊断入口（:class:`DiagnosisRecorderAdapter` 或替身）。
    :param settings: 运行期配置；与 T069 共用同一个 ``AppSettings``。
    :param review_service_provider: T074 复核服务工厂；人工复核暂停的恢复委托它执行。
    :param clock: 时间来源，便于测试固定时间。
    """

    def __init__(
        self,
        *,
        checkpoints: WorkflowCheckpointStore,
        reader: Any,
        session_factory: Callable[[], Session] | None = None,
        session: Session | None = None,
        agent: Any | None = None,
        diagnosis_service: Any | None = None,
        settings: AppSettings | None = None,
        review_service_provider: Callable[[], ReviewService] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._checkpoints = checkpoints
        self._reader = reader
        self._session_factory = session_factory
        self._session = session
        self._agent = agent
        self._diagnosis_service = diagnosis_service
        self._settings = settings
        self._review_service_provider = review_service_provider
        self._clock = clock or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------ 依赖与装配

    @property
    def checkpoints(self) -> WorkflowCheckpointStore:
        """返回检查点存储，供复核服务复用同一事实源。"""

        return self._checkpoints

    @property
    def reader(self) -> Any:
        """返回答卷快照读取组件。"""

        return self._reader

    @property
    def diagnosis_service(self) -> Any:
        """返回诊断入口（同时满足 T072 ``generate`` 与 T074 ``record``）。"""

        return self._diagnosis_service

    def _ensure_ready(self) -> None:
        """依赖齐全才允许产生副作用；缺失时在写入前显式报未就绪。"""

        try:
            self._checkpoints.ensure_ready()
        except WorkflowCheckpointError as error:
            raise WorkflowNotReadyError(
                "检查点存储未就绪：请先迁移数据库并检查连接。",
                source_code=error.error_code,
            ) from error
        if self._reader is None:
            raise WorkflowNotReadyError("答卷读取未接线：无法启动阅卷。")
        if self._agent is None:
            raise WorkflowNotReadyError("阅卷 Agent 未接线：无法启动阅卷。")

    @contextmanager
    def _use_session(self) -> Iterator[Session]:
        """按配置提供会话；自建会话使用后关闭，借入会话不关闭。"""

        if self._session_factory is not None:
            session = self._session_factory()
            try:
                yield session
            finally:
                session.close()
            return
        assert self._session is not None
        yield self._session

    def _checkpointer(self) -> DatabaseCheckpointSaver:
        """构造持久化 Checkpointer；生产使用会话工厂语义。"""

        if self._session_factory is not None:
            return DatabaseCheckpointSaver(session_factory=self._session_factory)
        return DatabaseCheckpointSaver(session=self._session)

    def _build_workflow(self, snapshot: Any, checkpointer: Any) -> GradingWorkflow:
        """按依赖构造 T072 图；检查点由本模块注入。"""

        deps = _workflow_deps(
            snapshot=snapshot,
            agent=self._agent,
            diagnosis_service=self._diagnosis_service,
            session_factory=self._session_factory,
            settings=self._settings,
        )
        return GradingWorkflow(deps, checkpointer=checkpointer)

    def workflow_for_run(self, run: WorkflowRun, state: Mapping[str, Any]) -> GradingWorkflow:
        """按运行记录重建 T072 图（T074 复核服务恢复原图时使用）。"""

        del state
        snapshot = self._reader.load(str(run.submission_id))
        saver = self._checkpointer()
        thread_id = checkpoint_thread_id(run)
        if thread_id:
            saver.bind_thread(thread_id, run.workflow_id)
        return self._build_workflow(snapshot, saver)

    def _review_service(self) -> ReviewService:
        """获取 T074 复核服务；未接线时显式报未就绪。"""

        provider = self._review_service_provider
        if provider is None:
            raise WorkflowNotReadyError("复核服务未接线：无法恢复人工复核暂停的运行。")
        return provider()

    def use_review_service_provider(
        self,
        provider: Callable[[], ReviewService],
    ) -> None:
        """注册 T074 复核服务工厂；仅生产装配点使用。"""

        self._review_service_provider = provider

    # ------------------------------------------------------------ 启动

    async def start_run(
        self,
        *,
        submission_id: str,
        actor_id: str,
        request_id: str,
        regrade: bool = False,
    ) -> TeacherWorkflowRunDTO:
        """启动一次阅卷运行；活动运行存在时幂等复用。"""

        self._ensure_ready()
        snapshot = self._load_for_teacher(submission_id, actor_id)
        existing = self._latest_run(submission_id)
        if existing is not None and existing.status in ACTIVE_RUN_STATUSES:
            return self._teacher_dto(existing, reused=True)
        if existing is not None:
            if regrade:
                raise WorkflowRunConflictError(
                    "该答卷已有运行记录；重评请使用阅卷 API 的显式重评入口。",
                    error_code=WORKFLOW_RUN_REGRADE_UNSUPPORTED,
                )
            raise WorkflowRunConflictError(
                f"该答卷的运行已处于“{existing.status.value}”，如需重评请显式请求。"
            )
        self._ensure_startable(snapshot)
        workflow_id = f"grading-{uuid4()}"
        thread_id = str(uuid4())
        initial_state: dict[str, Any] = {
            "workflow_id": workflow_id,
            "request_id": request_id,
            "submission_id": str(submission_id),
            "current_answer_order": 1,
            "retry_count": 0,
            "status": WorkflowStatus.RUNNING,
            "current_node": LOAD_SUBMISSION,
        }
        # 先落运行记录，再把 thread_id 绑定到该记录：Checkpointer 只允许写入已绑定的运行。
        self._save_state(
            workflow_id=workflow_id,
            state=initial_state,
            current_node=LOAD_SUBMISSION,
            thread_id=thread_id,
        )
        saver = self._checkpointer()
        saver.bind_thread(thread_id, workflow_id)
        workflow = self._build_workflow(snapshot, saver)
        result = await workflow.run_async(
            request_id=request_id,
            workflow_id=workflow_id,
            submission_id=str(submission_id),
            thread_id=thread_id,
        )
        row = self._persist_result(
            workflow_id=workflow_id,
            state=result.state,
            thread_id=thread_id,
        )
        return self._teacher_dto(row, interrupted=bool(result.interrupted))

    def _ensure_startable(self, snapshot: Any) -> None:
        """只允许符合既有提交生命周期的答卷启动阅卷。"""

        status = str(getattr(snapshot, "status", "") or "")
        if status not in STARTABLE_SUBMISSION_STATUSES:
            raise WorkflowSubmissionNotReadyError(
                f"答卷当前状态为“{status or '未知'}”，只有已提交的答卷才能启动阅卷。"
            )

    # ------------------------------------------------------------ 状态

    def get_run(self, *, workflow_id: str, actor_id: str, roles: Sequence[UserRole]) -> Any:
        """按角色返回运行状态：教师完整事实，学生公开子集。"""

        self._ensure_ready()
        row = self._require_run(workflow_id)
        if UserRole.TEACHER in set(roles):
            self._load_for_teacher(str(row.submission_id), actor_id)
            return self._teacher_dto(row)
        snapshot = self._load_for_student(str(row.submission_id), actor_id)
        del snapshot
        return self._student_dto(row)

    # ------------------------------------------------------------ 恢复

    async def resume_run(
        self,
        *,
        workflow_id: str,
        actor_id: str,
        answer_id: str | None = None,
        expected_review_status: str | None = None,
        comment: str | None = None,
    ) -> WorkflowResumeOutcomeDTO:
        """唯一恢复入口：人工复核暂停委托 T074，系统故障暂停才走 T072 恢复。"""

        self._ensure_ready()
        row = self._require_run(workflow_id)
        self._load_for_teacher(str(row.submission_id), actor_id)
        thread_id = checkpoint_thread_id(row)
        if not thread_id:
            raise WorkflowRunNotResumableError("运行记录缺少线程绑定，无法恢复。")
        state = self._checkpoints.restore_state(row)
        pending = pending_review_answer_ids(state)
        if pending:
            return await self._resume_with_teacher_decision(
                row=row,
                thread_id=thread_id,
                pending=pending,
                answer_id=answer_id,
                expected_review_status=expected_review_status,
                comment=comment,
                actor_id=actor_id,
            )
        if not row.resumable or not self._checkpoints.runtime_ready(
            row.workflow_id, thread_id
        ):
            raise WorkflowRunNotResumableError(
                "该运行没有可恢复的持久检查点，不提供自动恢复。"
            )
        snapshot = self._reader.load(str(row.submission_id))
        saver = self._checkpointer()
        saver.bind_thread(thread_id, row.workflow_id)
        workflow = self._build_workflow(snapshot, saver)
        result = await workflow.resume_async(thread_id=thread_id)
        updated = self._persist_result(
            workflow_id=row.workflow_id,
            state=result.state,
            thread_id=thread_id,
        )
        return WorkflowResumeOutcomeDTO(
            workflow_id=updated.workflow_id,
            status=updated.status,
            resumed=True,
            pending_answer_ids=list(pending_review_answer_ids(result.state)),
            resumable=bool(updated.resumable),
            pause_reason=updated.pause_reason,
            message="运行已按原线程恢复。",
        )

    async def _resume_with_teacher_decision(
        self,
        *,
        row: WorkflowRun,
        thread_id: str,
        pending: Sequence[str],
        answer_id: str | None,
        expected_review_status: str | None,
        comment: str | None,
        actor_id: str,
    ) -> WorkflowResumeOutcomeDTO:
        """人工复核暂停：必须已有教师结论，委托 T074 幂等恢复，不注入客户端状态。"""

        target = str(answer_id or "").strip()
        if not target:
            raise WorkflowReviewDecisionRequiredError(
                "该运行暂停在人工复核：请先在复核页提交教师结论，再按目标答案恢复。"
            )
        if target not in set(pending):
            raise WorkflowReviewDecisionRequiredError(
                "目标答案不在待复核集合内，拒绝恢复其他题目。"
            )
        recorded = self._recorded_decision(str(row.submission_id), target)
        if recorded is None:
            raise WorkflowReviewDecisionRequiredError(
                "该答案尚无教师结论记录，拒绝绕过人工复核直接恢复。"
            )
        decision = TeacherReviewDecision(
            workflow_id=row.workflow_id,
            thread_id=thread_id,
            answer_id=target,
            review_status=recorded.decision.value,
            revised_result=None,
            expected_review_status=expected_review_status,
        )
        outcome = await self._review_service().resume_recorded_decision_async(
            decision,
            actor_id=actor_id,
            actor_role=UserRole.TEACHER,
        )
        if not isinstance(outcome, ReviewOutcome):  # pragma: no cover - 契约保护
            raise WorkflowNotReadyError("复核服务返回了非预期的恢复结果。")
        return WorkflowResumeOutcomeDTO(
            workflow_id=outcome.workflow_id,
            status=outcome.workflow_status,
            resumed=outcome.resumed,
            decision_saved=True,
            review_record_id=outcome.review_record_id or str(recorded.id),
            resume_error_code=outcome.resume_error_code,
            pending_answer_ids=list(outcome.pending_answer_ids),
            resumable=outcome.resumable,
            pause_reason=None if outcome.resumed else "教师结论已保存，等待继续恢复原工作流。",
            message=(
                "教师结论已生效，原工作流已恢复。"
                if outcome.resumed
                else "教师结论已保存，但恢复未完成；可稍后按同一答案重试恢复。"
            ),
        )

    # ------------------------------------------------------------ 持久化与读取

    def _save_state(
        self,
        *,
        workflow_id: str,
        state: Mapping[str, Any],
        current_node: str,
        thread_id: str,
        pause_reason: str | None = None,
    ) -> WorkflowRun:
        """写入业务状态快照；失败按存储未就绪显式失败。"""

        try:
            return self._checkpoints.save_checkpoint(
                workflow_id,
                state,
                current_node,
                pause_reason,
                thread_id=thread_id,
            )
        except WorkflowCheckpointError as error:
            raise WorkflowNotReadyError(
                "运行状态写入失败：检查点存储未就绪。",
                source_code=error.error_code,
            ) from error

    def _persist_result(
        self,
        *,
        workflow_id: str,
        state: Mapping[str, Any],
        thread_id: str,
    ) -> WorkflowRun:
        """按一次运行/恢复的真实结论落库：暂停原因与可恢复性都取真实状态。

        不向状态里塞入派生字段：状态必须通过 T065 快照校验；待复核答案由
        :func:`pending_review_answer_ids` 从权威状态推导。
        """

        payload = dict(state)
        current_node = str(payload.get("current_node") or LOAD_SUBMISSION)
        pause_reason = payload.get("pause_reason")
        return self._save_state(
            workflow_id=workflow_id,
            state=payload,
            current_node=current_node,
            thread_id=thread_id,
            pause_reason=str(pause_reason) if pause_reason else None,
        )

    def _latest_run(self, submission_id: str) -> WorkflowRun | None:
        """读取该答卷最近一次运行记录；只读属于本执行器（M4 工作流）的行。

        同一张 ``workflow_runs`` 表还存放 M3 后台任务检查点：那些行不是 M4 的运行，因此不能
        参与本模块的幂等启动与状态判定。
        """

        with self._use_session() as session:
            return session.scalars(
                select(WorkflowRun)
                .where(WorkflowRun.submission_id == _as_uuid(submission_id))
                .where(run_kind_criteria())
                .order_by(WorkflowRun.created_at.desc(), WorkflowRun.id)
                .limit(1)
            ).first()

    def _require_run(self, workflow_id: str) -> WorkflowRun:
        """按 ``workflow_id`` 加载运行记录；不存在时 404。"""

        text = str(workflow_id or "").strip()
        if not text:
            raise WorkflowRunNotFoundError("缺少运行标识。")
        with self._use_session() as session:
            row = session.scalars(
                select(WorkflowRun).where(WorkflowRun.workflow_id == text)
            ).first()
        if row is None:
            raise WorkflowRunNotFoundError("运行记录不存在。")
        return row

    def _recorded_decision(self, submission_id: str, answer_id: str) -> ReviewRecord | None:
        """读取该答案最近一次已落库的教师结论（权威事实，不接受客户端声明）。"""

        with self._use_session() as session:
            return session.scalars(
                select(ReviewRecord)
                .join(GradingResult, ReviewRecord.grading_result_id == GradingResult.id)
                .where(GradingResult.submission_id == _as_uuid(submission_id))
                .where(GradingResult.answer_id == _as_uuid(answer_id))
                .order_by(ReviewRecord.created_at.desc(), ReviewRecord.id)
                .limit(1)
            ).first()

    def _load_for_teacher(self, submission_id: str, actor_id: str) -> Any:
        """按教师课程归属读取答卷快照。"""

        try:
            return self._reader.load_for_teacher(str(submission_id), str(actor_id))
        except GradingTaskError as error:
            raise _grading_error(error) from None

    def _load_for_student(self, submission_id: str, actor_id: str) -> Any:
        """按学生本人归属读取答卷快照。"""

        try:
            snapshot = self._reader.load(str(submission_id))
        except GradingTaskError as error:
            raise _grading_error(error) from None
        if str(getattr(snapshot, "student_id", "")) != str(actor_id):
            raise WorkflowRunPermissionError("只能查询属于自己的答卷状态。")
        return snapshot

    # ------------------------------------------------------------ DTO

    def _pending(self, row: WorkflowRun) -> tuple[str, ...]:
        """从已落库的权威状态还原待复核答案；还原失败时不猜测。

        只读 JSON 载荷不够：``pending_review_answer_ids`` 需要 T065 类型化状态（T073
        ``restore_state`` 负责解码并校验），因此这里统一走存储的还原入口。
        """

        try:
            state = self._checkpoints.restore_state(row)
        except WorkflowCheckpointError:
            return ()
        return pending_review_answer_ids(state)

    def _teacher_dto(
        self,
        row: WorkflowRun,
        *,
        interrupted: bool = False,
        reused: bool = False,
    ) -> TeacherWorkflowRunDTO:
        """教师视图：含节点、暂停原因与待复核答案，不含 runtime 检查点载荷。"""

        return TeacherWorkflowRunDTO(
            workflow_id=row.workflow_id,
            submission_id=str(row.submission_id),
            request_id=row.request_id,
            status=row.status,
            current_node=row.current_node,
            pause_reason=row.pause_reason,
            resumable=bool(row.resumable),
            interrupted=interrupted,
            pending_answer_ids=list(self._pending(row)),
            retry_count=row.retry_count,
            thread_id=checkpoint_thread_id(row),
            reused=reused,
            updated_at=row.updated_at,
        )

    def _student_dto(self, row: WorkflowRun) -> StudentWorkflowStatusDTO:
        """学生视图：只包含进度与公开状态。"""

        pending = self._pending(row)
        return StudentWorkflowStatusDTO(
            workflow_id=row.workflow_id,
            submission_id=str(row.submission_id),
            status=row.status,
            finished=row.status is WorkflowStatus.COMPLETED,
            requires_teacher_review=bool(pending),
            pending_review_count=len(pending),
            updated_at=row.updated_at,
        )


def _as_uuid(value: str) -> UUID:
    """校验并规范化 UUID；非法值显式失败。"""

    if isinstance(value, UUID):
        return value
    text = str(value).strip()
    try:
        return UUID(text)
    except ValueError as error:
        raise WorkflowRunNotFoundError(f"标识必须是 UUID，收到 {value!r}。") from error


def _grading_error(error: GradingTaskError) -> WorkflowRunError:
    """把 T060 读取错误映射为工作流边界错误。"""

    if error.error_code == GRADING_PERMISSION_DENIED:
        return WorkflowRunPermissionError("无权访问该答卷所属课程。")
    if error.error_code == GRADING_SUBMISSION_NOT_FOUND:
        return WorkflowRunNotFoundError("答卷不存在。")
    if error.error_code == GRADING_SUBMISSION_INCOMPLETE:
        return WorkflowSubmissionNotReadyError("答卷尚未完成，无法阅卷。")
    return WorkflowRunError(error.detail, error_code=error.error_code)


# ---------------------------------------------------------------------- 生产装配


def build_workflow_service(
    *,
    checkpoints: WorkflowCheckpointStore,
    reader: Any,
    agent: Any,
    diagnosis_service: Any,
    settings: AppSettings | None = None,
    session_factory: Callable[[], Session] | None = None,
    review_service_provider: Callable[[], ReviewService] | None = None,
) -> WorkflowService:
    """按显式组件装配工作流服务；缺依赖时应由调用方先显式失败。"""

    return WorkflowService(
        checkpoints=checkpoints,
        reader=reader,
        session_factory=session_factory,
        agent=agent,
        diagnosis_service=diagnosis_service,
        settings=settings,
        review_service_provider=review_service_provider,
    )


def build_review_service(
    *,
    checkpoints: WorkflowCheckpointStore,
    reader: Any,
    diagnosis: Any,
    workflow_provider: Callable[[WorkflowRun, Mapping[str, Any]], Any],
    session_factory: Callable[[], Session],
) -> ReviewService:
    """装配 T074 复核服务：注入数据库 Checkpointer、T060 结果写入与 T061 诊断入口。

    这是 T074 的唯一生产装配点（T077 复核 API 复用同一函数），避免两套装配产生事实分叉。
    """

    return ReviewService(
        checkpoints=checkpoints,
        reader=reader,
        session_factory=session_factory,
        workflow_provider=workflow_provider,
        result_writer=DatabaseGradingRepository(session_factory=session_factory),
        diagnosis=diagnosis,
        review_records=DatabaseReviewRecordStore(),
    )


def build_production_workflow_service(
    settings: AppSettings | None = None,
) -> WorkflowService:
    """构造生产工作流服务：数据库 Checkpointer + T060 读取 + T069 Agent + T061 诊断。

    Provider/Embedding 未就绪时由 T069 在逐题调用期显式失败（真实运行状态与错误码回写），
    不会在启动前伪造 503；数据库或检查点存储不可用时才在产生副作用前返回 503。
    """

    session_factory = get_session_factory()
    checkpoints = WorkflowCheckpointStore(session_factory=session_factory)
    reader = DatabaseGradingSubmissionReader(session_factory=session_factory)
    diagnosis = DiagnosisRecorderAdapter(
        store=DiagnosisReportStore(session_factory=session_factory),
        service=DiagnosisService(),
    )
    service = WorkflowService(
        checkpoints=checkpoints,
        reader=reader,
        session_factory=session_factory,
        agent=GradingAgent(session_factory=session_factory, settings=settings),
        diagnosis_service=diagnosis,
        settings=settings,
    )
    service.use_review_service_provider(
        lambda: build_review_service(
            checkpoints=checkpoints,
            reader=reader,
            diagnosis=diagnosis,
            workflow_provider=service.workflow_for_run,
            session_factory=session_factory,
        )
    )
    return service


def build_production_review_service(settings: AppSettings | None = None) -> ReviewService:
    """独立构造生产复核服务；T077 复核 API 直接复用该装配。"""

    session_factory = get_session_factory()
    checkpoints = WorkflowCheckpointStore(session_factory=session_factory)
    reader = DatabaseGradingSubmissionReader(session_factory=session_factory)
    diagnosis = DiagnosisRecorderAdapter(
        store=DiagnosisReportStore(session_factory=session_factory),
        service=DiagnosisService(),
    )
    workflow_service = WorkflowService(
        checkpoints=checkpoints,
        reader=reader,
        session_factory=session_factory,
        agent=GradingAgent(session_factory=session_factory, settings=settings),
        diagnosis_service=diagnosis,
        settings=settings,
    )
    return build_review_service(
        checkpoints=checkpoints,
        reader=reader,
        diagnosis=diagnosis,
        workflow_provider=workflow_service.workflow_for_run,
        session_factory=session_factory,
    )


def get_workflow_service(request: Request) -> WorkflowService:
    """装配工作流服务；测试可通过 ``app.state.workflow_service`` 注入替身。"""

    configured = getattr(request.app.state, "workflow_service", None)
    if configured is not None:
        return configured
    return build_production_workflow_service()


WorkflowServiceDependency = Annotated[WorkflowService, Depends(get_workflow_service)]
WorkflowOperator = Annotated[User, Depends(require_permission(Permission.TRIGGER_GRADING))]
WorkflowViewer = Annotated[
    User,
    Depends(
        require_any_permission(
            [Permission.VIEW_GRADING_RESULTS, Permission.VIEW_OWN_RESULTS]
        )
    ),
]


def _workflow_http_exception(error: BaseException) -> HTTPException:
    """把业务错误映射为脱敏的 HTTP 响应。"""

    if isinstance(error, WorkflowRunError):
        code = error.error_code
        detail = error.detail
        retryable = bool(error.retryable)
        source_code = error.source_code
    elif isinstance(
        error,
        (ReviewServiceError, WorkflowCheckpointError, GradingWorkflowError),
    ):
        code = error.error_code
        detail = error.detail
        retryable = bool(getattr(error, "retryable", False))
        source_code = getattr(error, "source_code", None)
    else:  # pragma: no cover - 未预期错误保持脱敏
        code = WORKFLOW_SERVICE_NOT_READY
        detail = "工作流执行失败。"
        retryable = False
        source_code = None
    return HTTPException(
        status_code=_ERROR_STATUS.get(code, 500),
        detail={
            "error_code": code,
            "message": detail,
            "retryable": retryable,
            "source_code": source_code,
        },
    )


def _resolve_request_id(header_value: str | None) -> str:
    """请求追踪标识：优先使用客户端请求头，缺失时由服务端生成。"""

    candidate = (header_value or "").strip()
    if candidate and len(candidate) <= 64:
        return candidate
    return str(uuid4())


# ---------------------------------------------------------------------- 端点


@router.post(
    "/submissions/{submission_id}/runs",
    response_model=TeacherWorkflowRunDTO,
    summary="启动 LangGraph 阅卷",
)
async def start_workflow_run(
    submission_id: str,
    payload: WorkflowRunStartRequest,
    teacher: WorkflowOperator,
    service: WorkflowServiceDependency,
    x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> TeacherWorkflowRunDTO:
    """受理并执行一次阅卷运行；活动运行存在时返回既有运行（幂等）。"""

    try:
        return await service.start_run(
            submission_id=submission_id,
            actor_id=str(teacher.id),
            request_id=_resolve_request_id(x_request_id),
            regrade=payload.regrade,
        )
    except WorkflowRunError as error:
        raise _workflow_http_exception(error) from None
    except (GradingWorkflowError, ReviewServiceError, WorkflowCheckpointError) as error:
        raise _workflow_http_exception(error) from None


@router.get(
    "/runs/{workflow_id}",
    summary="查询阅卷运行状态",
)
def get_workflow_run(
    workflow_id: str,
    user: WorkflowViewer,
    service: WorkflowServiceDependency,
) -> TeacherWorkflowRunDTO | StudentWorkflowStatusDTO:
    """按角色返回运行状态：教师完整事实，学生公开子集。"""

    roles = tuple(get_user_roles(user))
    try:
        return service.get_run(workflow_id=workflow_id, actor_id=str(user.id), roles=roles)
    except WorkflowRunError as error:
        raise _workflow_http_exception(error) from None
    except (ReviewServiceError, WorkflowCheckpointError) as error:
        raise _workflow_http_exception(error) from None


@router.post(
    "/runs/{workflow_id}/resume",
    response_model=WorkflowResumeOutcomeDTO,
    summary="恢复阅卷运行（唯一恢复入口）",
)
async def resume_workflow_run(
    workflow_id: str,
    payload: WorkflowRunResumeRequest,
    teacher: WorkflowOperator,
    service: WorkflowServiceDependency,
) -> WorkflowResumeOutcomeDTO:
    """恢复运行：人工复核暂停委托 T074，系统故障暂停走 T072 恢复。"""

    try:
        return await service.resume_run(
            workflow_id=workflow_id,
            actor_id=str(teacher.id),
            answer_id=payload.answer_id,
            expected_review_status=payload.expected_review_status,
            comment=payload.comment,
        )
    except WorkflowRunError as error:
        raise _workflow_http_exception(error) from None
    except (GradingWorkflowError, ReviewServiceError, WorkflowCheckpointError) as error:
        raise _workflow_http_exception(error) from None


__all__ = [
    "ACTIVE_RUN_STATUSES",
    "STARTABLE_SUBMISSION_STATUSES",
    "WORKFLOW_REVIEW_DECISION_REQUIRED",
    "WORKFLOW_RUN_ALREADY_COMPLETED",
    "WORKFLOW_RUN_NOT_FOUND",
    "WORKFLOW_RUN_NOT_RESUMABLE",
    "WORKFLOW_RUN_PERMISSION_DENIED",
    "WORKFLOW_RUN_REGRADE_UNSUPPORTED",
    "WORKFLOW_SERVICE_NOT_READY",
    "WORKFLOW_SUBMISSION_NOT_READY",
    "DiagnosisRecorderAdapter",
    "StudentWorkflowStatusDTO",
    "TeacherWorkflowRunDTO",
    "WorkflowNotReadyError",
    "WorkflowResumeOutcomeDTO",
    "WorkflowReviewDecisionRequiredError",
    "WorkflowRunConflictError",
    "WorkflowRunError",
    "WorkflowRunNotFoundError",
    "WorkflowRunNotResumableError",
    "WorkflowRunPermissionError",
    "WorkflowRunResumeRequest",
    "WorkflowRunStartRequest",
    "WorkflowService",
    "WorkflowSubmissionNotReadyError",
    "build_production_review_service",
    "build_production_workflow_service",
    "build_review_service",
    "build_workflow_service",
    "get_workflow_service",
    "router",
]
