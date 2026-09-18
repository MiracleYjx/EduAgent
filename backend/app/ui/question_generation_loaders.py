"""T075 AI 出题加载器：把 ``QuestionGenerationService`` 接入 AI 出题视图。

S01 约束（评审意见）：视图只依赖本模块提供的加载函数与展示 DTO，不直接导入 FastAPI 端点、
ORM 会话或领域仓储；每次调用自建服务与数据库会话，不缓存用户身份、不共享 Session。

职责边界：

- 生成端调用 T067/T068 编排（``generate_candidate_batch``），返回真实候选题与整批校验结论；
- 查询端按教师课程范围读取候选题（``list_candidates``/``load_candidate``）；
- 审核端提交教师结论（``submit_candidate_review``）；
- 课程下拉数据来自 M1 ``CourseService``，不创建占位课程。

加载器只做身份传递与空态处理，不构造领域对象、不推导业务状态。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import uuid4

from backend.app.api.question_generation import (
    DEFAULT_PAGE_SIZE,
    CandidateDTO,
    CandidateGenerationResponse,
    CandidatePageDTO,
    CandidateReviewOutcomeDTO,
    QuestionGenerationService,
    build_production_question_generation_service,
)
from backend.app.core.database import get_session_factory
from backend.app.domain.enums import QuestionStatus, QuestionType, UserRole
from backend.app.domain.permissions import PermissionDeniedError, normalize_role
from backend.app.services.course_service import CourseService

#: 出题条件未提供时使用的默认知识点集合（空集合表示不限定知识点）。
EMPTY_KNOWLEDGE_POINTS: tuple[str, ...] = ()


def state_user_id(state: Mapping[str, Any] | None) -> str:
    """从会话状态读取当前用户标识；缺失时返回空串。"""

    if not isinstance(state, Mapping):
        return ""
    return str(state.get("user_id") or "").strip()


def require_teacher(state: Mapping[str, Any] | None) -> str:
    """确认当前会话是已登录教师，并返回用户标识。

    这里只是界面层的快速失败；课程归属与候选题归属仍由后端服务在每个请求内重新校验。
    """

    if not isinstance(state, Mapping) or not state.get("access_token"):
        raise PermissionDeniedError("请先登录。")
    try:
        roles = {normalize_role(role) for role in state.get("roles", [])}
    except (TypeError, ValueError) as error:
        raise PermissionDeniedError("当前账号无权访问 AI 出题功能。") from error
    if UserRole.TEACHER not in roles:
        raise PermissionDeniedError("当前账号无权访问 AI 出题功能。")
    user_id = state_user_id(state)
    if not user_id:
        raise PermissionDeniedError("登录状态缺少用户标识。")
    return user_id


def _request_id(state: Mapping[str, Any] | None) -> str:
    """本次生成的请求追踪标识：会话未提供时由界面层生成，绝不使用空串。"""

    if isinstance(state, Mapping):
        candidate = str(state.get("request_id") or "").strip()
        if candidate:
            return candidate
    return str(uuid4())


def build_candidate_service() -> QuestionGenerationService:
    """构造生产出题服务；Provider/Embedding 未就绪时由调用期显式失败。"""

    return build_production_question_generation_service()


def load_courses(state: Mapping[str, Any] | None) -> list[tuple[str, str]]:
    """读取当前教师的课程选项；课程为空时不创建占位数据。"""

    teacher_id = require_teacher(state)
    with get_session_factory()() as session:
        courses = CourseService(session).list_courses(teacher_id=teacher_id)
    return [(course.name, course.id) for course in courses]


async def generate_candidate_batch(
    state: Mapping[str, Any] | None,
    *,
    course_id: str,
    knowledge_points: Sequence[str] = EMPTY_KNOWLEDGE_POINTS,
    difficulty: str | None = None,
    question_type: QuestionType | None = None,
    count: int = 1,
) -> CandidateGenerationResponse:
    """按教师条件生成候选题目并返回真实回执（含整批校验结论与检索依据）。"""

    teacher_id = require_teacher(state)
    service = build_candidate_service()
    return await service.generate_candidates(
        course_id=str(course_id or "").strip(),
        actor_id=teacher_id,
        request_id=_request_id(state),
        knowledge_points=[
            str(point).strip() for point in knowledge_points if str(point).strip()
        ],
        difficulty=(difficulty or "").strip() or None,
        question_type=question_type,
        count=count,
    )


def list_candidates(
    state: Mapping[str, Any] | None,
    *,
    course_id: str | None = None,
    candidate_status: QuestionStatus | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    offset: int = 0,
) -> CandidatePageDTO:
    """列出当前教师课程范围内的候选题。"""

    teacher_id = require_teacher(state)
    service = build_candidate_service()
    return service.list_candidates(
        actor_id=teacher_id,
        course_id=(course_id or "").strip() or None,
        candidate_status=candidate_status,
        limit=limit,
        offset=offset,
    )


def load_candidate(
    candidate_id: str,
    state: Mapping[str, Any] | None,
) -> CandidateDTO:
    """读取单个候选题；跨课程访问由服务层按 404 处理。"""

    teacher_id = require_teacher(state)
    service = build_candidate_service()
    return service.get_candidate(actor_id=teacher_id, candidate_id=str(candidate_id or "").strip())


def submit_candidate_review(
    state: Mapping[str, Any] | None,
    *,
    candidate_id: str,
    action: str,
    comment: str | None = None,
    expected_status: QuestionStatus | None = None,
) -> CandidateReviewOutcomeDTO:
    """提交教师审核结论（通过或退回修订）。"""

    teacher_id = require_teacher(state)
    service = build_candidate_service()
    return service.submit_review(
        actor_id=teacher_id,
        candidate_id=str(candidate_id or "").strip(),
        action="approve" if action == "approve" else "request_revision",
        comment=(comment or "").strip() or None,
        expected_status=expected_status,
    )


__all__ = [
    "EMPTY_KNOWLEDGE_POINTS",
    "build_candidate_service",
    "generate_candidate_batch",
    "list_candidates",
    "load_candidate",
    "load_courses",
    "require_teacher",
    "state_user_id",
    "submit_candidate_review",
]
