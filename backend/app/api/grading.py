"""T056 阅卷 API：触发、任务状态与单题结构化结果。

契约依据：T056（阅卷触发、阅卷状态与单题结构化结果端点）、plan.md §5.1/§5.2、
FR-033～FR-037。

边界与权限：

- 触发与查询均要求教师业务权限（``TRIGGER_GRADING`` / ``VIEW_GRADING_RESULTS``），
  并沿 Submission → Exam → Course 校验课程归属；仅 Admin 不获得教师业务权限。
- 触发是异步受理：真实调度成功才返回 ``202`` 与 ``task_id``；结果存储或执行链路未接通
  时返回 ``503``，**不返回虚构任务标识**。
- 本批的 ``task_id`` 不是 ``WorkflowRun``（T064），不提供跨重启恢复；响应中的
  ``durable`` 如实为 ``False``。

错误语义：401 未认证（认证中间件）、403 无权限或跨课程、404 资源不存在、
409 状态冲突（草稿答卷、未请求重评）、422 输入非法、503 依赖未就绪。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.security import require_permission
from backend.app.domain.permissions import Permission
from backend.app.models import User
from backend.app.schemas.grading import (
    GradingTaskStatusDTO,
    QuestionResultDTO,
)
from backend.app.services.grading.grading_task_service import (
    GRADING_EXECUTION_NOT_READY,
    GRADING_NOT_ALLOWED,
    GRADING_PERMISSION_DENIED,
    GRADING_RESULT_NOT_FOUND,
    GRADING_STORE_NOT_READY,
    GRADING_SUBMISSION_NOT_FOUND,
    GRADING_TASK_FAILED,
    GRADING_TASK_NOT_FOUND,
    GRADING_TRIGGER_CONFLICT,
    DatabaseGradingSubmissionReader,
    DefaultScoringPipeline,
    GradingTaskError,
    GradingTaskService,
    InlineGradingTaskExecutor,
    NotConfiguredGradingRepository,
)

router = APIRouter(prefix="/api/grading", tags=["AI 阅卷"])

#: 错误码到 HTTP 状态码的映射；未列出的错误按 500 处理并保持脱敏。
_ERROR_STATUS: dict[str, int] = {
    GRADING_SUBMISSION_NOT_FOUND: 404,
    GRADING_TASK_NOT_FOUND: 404,
    GRADING_RESULT_NOT_FOUND: 404,
    GRADING_PERMISSION_DENIED: 403,
    GRADING_NOT_ALLOWED: 409,
    GRADING_TRIGGER_CONFLICT: 409,
    GRADING_STORE_NOT_READY: 503,
    GRADING_EXECUTION_NOT_READY: 503,
}


class TriggerGradingRequest(BaseModel):
    """阅卷触发请求；``regrade`` 显式表达是否重评已完成答卷。"""

    model_config = ConfigDict(extra="forbid")

    regrade: bool = False


def get_grading_task_service(
    request: Request,
    session: Annotated[Session, Depends(get_db)],
) -> GradingTaskService:
    """装配阅卷任务服务。

    生产默认使用 :class:`NotConfiguredGradingRepository`：结果存储与任务状态持久化属
    T060/T064，未接通前一律返回 503；测试或后续集成可通过
    ``app.state.grading_task_service`` 注入显式装配。
    """

    configured = getattr(request.app.state, "grading_task_service", None)
    if configured is not None:
        return configured
    repository = NotConfiguredGradingRepository()
    reader = DatabaseGradingSubmissionReader(session=session)
    return GradingTaskService(
        repository=repository,
        reader=reader,
        executor=InlineGradingTaskExecutor(
            repository=repository,
            reader=reader,
            pipeline=DefaultScoringPipeline(),
        ),
    )


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
    "get_grading_task_service",
    "router",
]
