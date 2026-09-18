"""T077 复核加载器：把复核读模型与 T074 决策服务接入复核工作台。

S01 约束（评审意见）：视图只依赖本模块的加载函数与展示 DTO，不直接导入 FastAPI 端点、
ORM 会话或领域仓储；每次调用自建服务与数据库会话，不缓存用户身份、不共享 Session。

职责边界：

- ``load_queue``/``load_detail`` 只读 T077 复核读模型（权威持久化事实）；
- ``submit_decision`` 调用 T074 ``ReviewService`` 的决策入口（经 T077 决策服务构造固定契约），
  部分成功（结论已保存、恢复未完成）原样回传给界面，绝不伪装成“已恢复”。

加载器不做领域判断、不构造领域对象、不推导分数或复核状态。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from backend.app.api.reviews import (
    DEFAULT_PAGE_SIZE,
    ReviewDecisionOutcomeDTO,
    ReviewDecisionService,
    ReviewDetailDTO,
    ReviewQueryService,
    ReviewQueuePageDTO,
    TeacherDecisionRequest,
)
from backend.app.api.workflow import build_production_review_service
from backend.app.core.database import get_session_factory
from backend.app.domain.enums import ReviewStatus, UserRole
from backend.app.domain.permissions import PermissionDeniedError, normalize_role


def state_user_id(state: Mapping[str, Any] | None) -> str:
    """从会话状态读取当前用户标识；缺失时返回空串。"""

    if not isinstance(state, Mapping):
        return ""
    return str(state.get("user_id") or "").strip()


def require_teacher(state: Mapping[str, Any] | None) -> str:
    """确认当前会话是已登录教师，并返回用户标识（服务端仍会重新校验）。"""

    if not isinstance(state, Mapping) or not state.get("access_token"):
        raise PermissionDeniedError("请先登录。")
    try:
        roles = {normalize_role(role) for role in state.get("roles", [])}
    except (TypeError, ValueError) as error:
        raise PermissionDeniedError("当前账号无权访问阅卷复核功能。") from error
    if UserRole.TEACHER not in roles:
        raise PermissionDeniedError("当前账号无权访问阅卷复核功能。")
    user_id = state_user_id(state)
    if not user_id:
        raise PermissionDeniedError("登录状态缺少用户标识。")
    return user_id


def _require_uuid(value: Any, label: str) -> UUID:
    """校验并规范化 UUID；非法值显式失败（不静默丢弃）。"""

    if isinstance(value, UUID):
        return value
    text = str(value or "").strip()
    try:
        return UUID(text)
    except ValueError as error:
        raise PermissionDeniedError(f"{label} 必须是 UUID，收到 {value!r}。") from error


def _review_status(value: ReviewStatus | str | None) -> ReviewStatus | None:
    """把可选的复核状态值规范化为枚举；空值保持 ``None``。"""

    if value is None:
        return None
    if isinstance(value, ReviewStatus):
        return value
    text = str(value).strip()
    if not text:
        return None
    for candidate in ReviewStatus:
        if text.lower() in {candidate.value.lower(), candidate.name.lower()}:
            return candidate
    raise PermissionDeniedError(f"未知的复核状态：{value!r}。")


def build_query_service() -> ReviewQueryService:
    """构造只读复核读模型。"""

    return ReviewQueryService(session_factory=get_session_factory())


def build_decision_service() -> ReviewDecisionService:
    """构造复核决策服务：读模型 + T074 生产复核服务。"""

    return ReviewDecisionService(
        query=build_query_service(),
        review_service_provider=build_production_review_service,
    )


def load_queue(
    state: Mapping[str, Any] | None,
    *,
    course_id: str | None = None,
    exam_id: str | None = None,
    student_id: str | None = None,
    review_status: ReviewStatus | str | None = ReviewStatus.PENDING_REVIEW,
    limit: int = DEFAULT_PAGE_SIZE,
    offset: int = 0,
) -> ReviewQueuePageDTO:
    """读取当前教师的复核队列。"""

    require_teacher(state)
    if course_id:
        return build_query_service().list_queue(
            state_user_id(state),
            course_id=str(course_id),
            review_status=review_status,
            limit=limit,
            offset=offset,
        )
    return build_query_service().list_queue(
        state_user_id(state),
        exam_id=str(exam_id) if exam_id else None,
        student_id=str(student_id) if student_id else None,
        review_status=review_status,
        limit=limit,
        offset=offset,
    )


def load_detail(
    state: Mapping[str, Any] | None,
    *,
    submission_id: str,
    answer_id: str,
) -> ReviewDetailDTO:
    """读取单题复核详情（学生答案、AI 评分、题目依据、检索依据）。"""

    teacher_id = require_teacher(state)
    return build_query_service().get_answer_detail(
        teacher_id, str(submission_id), str(answer_id)
    )


async def submit_decision(
    state: Mapping[str, Any] | None,
    *,
    submission_id: str,
    answer_id: str,
    action: str,
    score: Any = None,
    reason: str | None = None,
    comment: str | None = None,
    workflow_id: str | None = None,
    expected_review_status: ReviewStatus | str | None = None,
) -> ReviewDecisionOutcomeDTO:
    """提交教师结论（确认或修改）；返回真实回执，包含部分成功语义。"""

    teacher_id = require_teacher(state)
    payload = TeacherDecisionRequest(
        submission_id=_require_uuid(submission_id, "答卷标识"),
        answer_id=_require_uuid(answer_id, "答案标识"),
        action="modify" if action == "modify" else "confirm",
        score=score if action == "modify" else None,
        reason=reason if action == "modify" else None,
        comment=comment,
        workflow_id=workflow_id,
        expected_review_status=_review_status(expected_review_status),
    )
    return await build_decision_service().submit(teacher_id=teacher_id, payload=payload)


__all__ = [
    "build_decision_service",
    "build_query_service",
    "load_detail",
    "load_queue",
    "require_teacher",
    "state_user_id",
    "submit_decision",
]
