"""T056 阅卷 API：触发、任务状态与单题结构化结果。

契约依据：T056（阅卷触发、阅卷状态与单题结构化结果端点）、plan.md §5.1/§5.2、
FR-033～FR-037。

边界与权限：

- 触发与查询均要求教师业务权限（``TRIGGER_GRADING`` / ``VIEW_GRADING_RESULTS``），
  并沿 Submission → Exam → Course 校验课程归属；仅 Admin 不获得教师业务权限。
- 触发是异步受理：真实调度成功才返回 ``202`` 与 ``task_id``；结果存储或执行链路未接通
  时返回 ``503``，**不返回虚构任务标识**。
- 生产装配为真实仓储（``workflow_runs``/``grading_results``/``exam_results``）+ 真实主观题
  评分链路 + 后台任务执行器（:func:`build_production_grading_service`）；结果存储未迁移或
  不可连接时由仓储的 ``ensure_ready`` 显式失败。
- 任务状态已落库（``workflow_runs``），因此 ``durable`` 为 ``True``；进程中断后的遗留任务
  由启动阶段收敛为中断失败（:func:`recover_interrupted_grading_tasks`），需显式重评，
  **不提供自动恢复队列，也不具备 LangGraph 检查点恢复能力**。

错误语义：401 未认证（认证中间件）、403 无权限或跨课程、404 资源不存在、
409 状态冲突（草稿答卷、未请求重评）、422 输入非法、503 依赖未就绪。
"""

from __future__ import annotations

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import SQLAlchemyError

from backend.app.core.config import AppSettings, ConfigurationError
from backend.app.core.database import get_session_factory
from backend.app.core.security import require_permission
from backend.app.domain.permissions import Permission
from backend.app.models import User
from backend.app.schemas.grading import (
    GradingTaskStatusDTO,
    QuestionResultDTO,
)
from backend.app.services.grading.grading_repository import DatabaseGradingRepository
from backend.app.services.grading.grading_task_service import (
    GRADING_EXECUTION_NOT_READY,
    GRADING_NOT_ALLOWED,
    GRADING_PERMISSION_DENIED,
    GRADING_RESULT_NOT_FOUND,
    GRADING_RESULT_OWNERSHIP_MISMATCH,
    GRADING_STORE_NOT_READY,
    GRADING_SUBMISSION_INCOMPLETE,
    GRADING_SUBMISSION_NOT_FOUND,
    GRADING_TASK_FAILED,
    GRADING_TASK_NOT_FOUND,
    GRADING_TRIGGER_CONFLICT,
    DatabaseGradingSubmissionReader,
    DefaultScoringPipeline,
    GradingTaskError,
    GradingTaskService,
    InlineGradingTaskExecutor,
)
from backend.app.services.grading.subjective_pipeline import build_subjective_scorer

router = APIRouter(prefix="/api/grading", tags=["AI 阅卷"])

#: 错误码到 HTTP 状态码的映射；未列出的错误按 500 处理并保持脱敏。
_ERROR_STATUS: dict[str, int] = {
    GRADING_SUBMISSION_NOT_FOUND: 404,
    GRADING_TASK_NOT_FOUND: 404,
    GRADING_RESULT_NOT_FOUND: 404,
    GRADING_PERMISSION_DENIED: 403,
    GRADING_NOT_ALLOWED: 409,
    GRADING_SUBMISSION_INCOMPLETE: 409,
    GRADING_RESULT_OWNERSHIP_MISMATCH: 409,
    GRADING_TRIGGER_CONFLICT: 409,
    GRADING_STORE_NOT_READY: 503,
    GRADING_EXECUTION_NOT_READY: 503,
}


class TriggerGradingRequest(BaseModel):
    """阅卷触发请求；``regrade`` 显式表达是否重评已完成答卷。"""

    model_config = ConfigDict(extra="forbid")

    regrade: bool = False


def get_grading_task_service(request: Request) -> GradingTaskService:
    """装配阅卷任务服务。

    生产装配使用真实仓储、真实主观题评分链路与后台任务执行器；结果存储未迁移或不可连接时
    由 ``ensure_ready`` 显式失败，经错误映射返回 ``503 GRADING_STORE_NOT_READY``。测试可通过
    ``app.state.grading_task_service`` 或依赖覆盖注入替身。
    """

    configured = getattr(request.app.state, "grading_task_service", None)
    if configured is not None:
        return configured
    return build_production_grading_service()


def build_production_grading_service(
    settings: AppSettings | None = None,
) -> GradingTaskService:
    """构造生产阅卷任务服务：真实仓储 + 真实评分管道 + 后台任务执行器。

    进度与结果在同一事务内由仓储写入，因此这里**不注入**独立的进度更新器，避免提交后再
    用第二个事务重复写同一批进度。主观题评分器自建并关闭会话，LLM 调用不持有写事务。
    """

    session_factory = get_session_factory()
    repository = DatabaseGradingRepository(session_factory=session_factory)
    reader = DatabaseGradingSubmissionReader(session_factory=session_factory)
    pipeline = DefaultScoringPipeline(
        subjective_scorer=build_subjective_scorer(
            session_factory=session_factory,
            settings=settings,
        )
    )
    return GradingTaskService(
        repository=repository,
        reader=reader,
        executor=InlineGradingTaskExecutor(
            repository=repository,
            reader=reader,
            pipeline=pipeline,
        ),
    )


def recover_interrupted_grading_tasks() -> int:
    """启动阶段把遗留的进行中任务收敛为中断失败，返回处理条数。

    结果存储未接通（未迁移、不可连接或配置不完整）时跳过并返回 0：启动阶段的收敛不得
    阻断应用启动；此时也不存在可收敛的持久化任务事实。
    """

    try:
        return build_production_grading_service().recover_interrupted_tasks()
    except (GradingTaskError, SQLAlchemyError, ConfigurationError):
        return 0


def _grading_http_exception(error: GradingTaskError) -> HTTPException:
    """把业务错误映射为脱敏的 HTTP 响应。"""

    status_code = _ERROR_STATUS.get(error.error_code, 500)
    return HTTPException(
        status_code=status_code,
        detail={
            "error_code": error.error_code,
            "message": error.detail,
            "retryable": bool(error.retryable),
        },
    )


@router.post(
    "/submissions/{submission_id}/trigger",
    status_code=202,
    summary="触发一份答卷的 AI 阅卷",
)
def trigger_grading(
    submission_id: str,
    payload: TriggerGradingRequest,
    background_tasks: BackgroundTasks,
    user: Annotated[User, Depends(require_permission(Permission.TRIGGER_GRADING))],
    service: Annotated[GradingTaskService, Depends(get_grading_task_service)],
) -> GradingTaskStatusDTO:
    """受理阅卷任务；只有真实受理成功时才返回 202 与任务标识。"""

    try:
        return service.trigger(
            submission_id,
            teacher_id=str(user.id),
            regrade=payload.regrade,
            request_id=str(uuid4()),
            scheduler=lambda task_id, target_submission: background_tasks.add_task(
                service.executor.execute,
                task_id,
                target_submission,
            ),
        )
    except GradingTaskError as error:
        raise _grading_http_exception(error) from None


@router.get(
    "/tasks/{task_id}",
    summary="查询阅卷任务状态",
)
def get_grading_task(
    task_id: str,
    user: Annotated[User, Depends(require_permission(Permission.VIEW_GRADING_RESULTS))],
    service: Annotated[GradingTaskService, Depends(get_grading_task_service)],
) -> GradingTaskStatusDTO:
    """查询任务状态；教师身份取自认证上下文并校验课程归属。"""

    try:
        return service.get_task(task_id, teacher_id=str(user.id))
    except GradingTaskError as error:
        raise _grading_http_exception(error) from None


@router.get(
    "/submissions/{submission_id}/answers/{answer_id}",
    summary="查询单题结构化评分结果",
)
def get_grading_answer_result(
    submission_id: str,
    answer_id: str,
    user: Annotated[User, Depends(require_permission(Permission.VIEW_GRADING_RESULTS))],
    service: Annotated[GradingTaskService, Depends(get_grading_task_service)],
) -> QuestionResultDTO:
    """返回单题结构化评分（含待复核标识与决策原因）。

    学生面接口属 T057；本端点要求教师身份并先校验答卷所属课程。
    """

    try:
        return service.get_single_result(
            submission_id,
            answer_id,
            teacher_id=str(user.id),
        )
    except GradingTaskError as error:
        raise _grading_http_exception(error) from None


__all__ = [
    "GRADING_TASK_FAILED",
    "TriggerGradingRequest",
    "build_production_grading_service",
    "get_grading_task_service",
    "recover_interrupted_grading_tasks",
    "router",
]
