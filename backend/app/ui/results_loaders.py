"""T057 生产结果与诊断加载器：把 ``ResultsQueryService`` 接入学生/教师结果视图。

S01 约束：

- 学生刷新入口接收 ``exam_id``，先用**当前学生授权范围内的答卷**定位 ``submission_id``，
  再调用详情查询；``exam_id`` 与 ``submission_id`` 不混用、不互相替代。
- 每次刷新自建查询服务与数据库会话，模块级不保存用户身份、不共享 Session。
- 加载器只读：诊断来自已持久化报告，刷新不会触发生成与 LLM 调用；任何失败由视图转为
  明确空态，不伪装成成绩或诊断结论。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import select

from backend.app.api.results import (
    ResultsQueryService,
    build_production_results_query_service,
)
from backend.app.api.reviews import DEFAULT_PAGE_SIZE, ReviewQueryService
from backend.app.core.database import get_session_factory
from backend.app.domain.enums import ReviewStatus
from backend.app.models import User
from backend.app.schemas.grading import (
    DiagnosisReportDTO,
    StudentResultSummaryDTO,
    SubmissionResultDTO,
    TeacherExamResultSummaryDTO,
)
from backend.app.services.course_service import CourseService, CourseSummary
from backend.app.services.exam_service import ExamService, ExamSummary
from backend.app.ui.results_view import configure_results_loaders


def state_user_id(state: Mapping[str, Any] | None) -> str:
    """从会话状态读取当前用户标识；缺失时返回空串。"""

    if not isinstance(state, Mapping):
        return ""
    return str(state.get("user_id") or "").strip()


def resolve_student_submission_id(
    service: ResultsQueryService,
    student_id: str,
    exam_id: str | None,
) -> str | None:
    """S01：在学生授权范围内按 ``exam_id`` 定位答卷标识。"""

    target = str(exam_id or "").strip()
    for summary in service.list_student_results(student_id):
        assert isinstance(summary, StudentResultSummaryDTO)
        if not target or str(summary.exam_id) == target:
            return str(summary.submission_id)
    return None


def load_student_result(
    exam_id: str | None = None,
    state: Mapping[str, Any] | None = None,
) -> SubmissionResultDTO | None:
    """学生逐题结果加载器；无法定位授权答卷时返回 ``None``（视图显示空态）。"""

    student_id = state_user_id(state)
    if not student_id:
        return None
    service = build_production_results_query_service()
    submission_id = resolve_student_submission_id(service, student_id, exam_id)
    if submission_id is None:
        return None
    return service.get_student_result(student_id, submission_id)


def _is_final_result(result: Any) -> bool:
    """读取整卷是否已形成最终成绩；对象与映射两种读模型都支持。"""

    if isinstance(result, Mapping):
        return bool(result.get("is_final", False))
    return bool(getattr(result, "is_final", False))


def load_student_diagnosis(
    exam_id: str | None = None,
    state: Mapping[str, Any] | None = None,
) -> tuple[DiagnosisReportDTO, bool] | None:
    """学生诊断加载器；一并返回整卷是否已形成最终成绩，供视图标注待复核。"""

    student_id = state_user_id(state)
    if not student_id:
        return None
    service = build_production_results_query_service()
    submission_id = resolve_student_submission_id(service, student_id, exam_id)
    if submission_id is None:
        return None
    result = service.get_student_result(student_id, submission_id)
    report = service.get_student_diagnosis(student_id, submission_id)
    return report, _is_final_result(result)


def load_teacher_results(
    course_id: str | None = None,
    exam_id: str | None = None,
    state: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]] | None:
    """读取教师授权结果，并附加姓名与权威待复核跳转上下文。"""

    teacher_id = state_user_id(state)
    target_course = str(course_id or "").strip()
    target_exam = str(exam_id or "").strip()
    if not teacher_id or not target_exam:
        return None
    if not _exam_belongs_to_course(teacher_id, target_course, target_exam):
        return None
    service = build_production_results_query_service()
    summaries = service.list_exam_results(teacher_id, target_exam)
    names = _student_names(summary.student_id for summary in summaries)
    pending_contexts = _pending_review_contexts(
        teacher_id,
        target_exam,
        summaries,
    )
    records: list[dict[str, Any]] = []
    for summary in summaries:
        record = summary.model_dump(mode="python")
        student_id = str(summary.student_id or "")
        record["student_name"] = names.get(student_id)
        context = pending_contexts.get(str(summary.submission_id))
        if context is not None:
            record["review_context"] = context
        records.append(record)
    return records


def load_teacher_courses(
    state: Mapping[str, Any] | None = None,
) -> list[CourseSummary] | None:
    """列出当前教师拥有的课程。"""

    teacher_id = state_user_id(state)
    if not teacher_id:
        return None
    with get_session_factory()() as session:
        return CourseService(session).list_courses(teacher_id=teacher_id)


def load_teacher_exams(
    course_id: str | None = None,
    state: Mapping[str, Any] | None = None,
) -> list[ExamSummary] | None:
    """列出当前教师在所选课程下的考试。"""

    teacher_id = state_user_id(state)
    target_course = str(course_id or "").strip()
    if not teacher_id or not target_course:
        return None
    with get_session_factory()() as session:
        return ExamService(session).list_exams(
            course_id=target_course,
            teacher_id=teacher_id,
        )


def load_teacher_summary(
    course_id: str | None = None,
    exam_id: str | None = None,
    state: Mapping[str, Any] | None = None,
) -> TeacherExamResultSummaryDTO | None:
    """读取服务端考试统计；不在 UI loader 中重新汇总。"""

    teacher_id = state_user_id(state)
    target_course = str(course_id or "").strip()
    target_exam = str(exam_id or "").strip()
    if not teacher_id or not target_exam:
        return None
    if not _exam_belongs_to_course(teacher_id, target_course, target_exam):
        return None
    return build_production_results_query_service().get_exam_summary(
        teacher_id,
        target_exam,
    )


def load_teacher_diagnosis(
    exam_id: str | None,
    submission_id: str | None,
    student_id: str | None,
    state: Mapping[str, Any] | None = None,
) -> tuple[DiagnosisReportDTO, bool] | None:
    """经教师详情授权后只读所选学生的持久化诊断。"""

    teacher_id = state_user_id(state)
    target_exam = str(exam_id or "").strip()
    target_submission = str(submission_id or "").strip()
    declared_student = str(student_id or "").strip()
    if not teacher_id or not target_exam or not target_submission:
        return None
    service = build_production_results_query_service()
    result = service.get_teacher_student_result(
        teacher_id,
        target_exam,
        target_submission,
    )
    if declared_student and str(result.student_id) != declared_student:
        return None
    report = service.get_student_diagnosis(
        str(result.student_id),
        target_submission,
    )
    return report, bool(result.is_final)


def _exam_belongs_to_course(
    teacher_id: str,
    course_id: str,
    exam_id: str,
) -> bool:
    """复用考试服务核验教师归属，并拒绝陈旧的课程/考试组合。"""

    with get_session_factory()() as session:
        exam = ExamService(session).get_exam(exam_id, teacher_id=teacher_id)
    return not course_id or str(exam.course_id) == course_id


def _student_names(student_ids: Any) -> dict[str, str]:
    """读取结果中学生的显示姓名，不改变成绩事实。"""

    identifiers = {
        UUID(str(student_id))
        for student_id in student_ids
        if str(student_id or "").strip()
    }
    if not identifiers:
        return {}
    with get_session_factory()() as session:
        rows = session.execute(
            select(User.id, User.username).where(User.id.in_(identifiers))
        ).all()
    return {str(user_id): str(username) for user_id, username in rows}


def _pending_review_contexts(
    teacher_id: str,
    exam_id: str,
    summaries: list[StudentResultSummaryDTO],
) -> dict[str, dict[str, str]]:
    """从复核读模型提取每份待复核答卷的首个真实答案与 Workflow。"""

    pending_submissions = {
        str(summary.submission_id)
        for summary in summaries
        if (summary.pending_review_count or 0) > 0
    }
    if not pending_submissions:
        return {}
    query = ReviewQueryService(session_factory=get_session_factory())
    contexts: dict[str, dict[str, str]] = {}
    offset = 0
    while len(contexts) < len(pending_submissions):
        page = query.list_queue(
            teacher_id,
            exam_id=exam_id,
            review_status=ReviewStatus.PENDING_REVIEW,
            limit=DEFAULT_PAGE_SIZE,
            offset=offset,
        )
        for item in page.items:
            submission_id = str(item.submission_id)
            if (
                submission_id not in pending_submissions
                or submission_id in contexts
            ):
                continue
            detail = query.get_answer_detail(
                teacher_id,
                submission_id,
                str(item.answer_id),
            )
            context = {
                "exam_id": str(item.exam_id),
                "submission_id": submission_id,
                "answer_id": str(item.answer_id),
                "question_id": str(item.question_id),
                "student_id": str(item.student_id),
                "student_name": str(item.student_name),
            }
            if detail.workflow_id:
                context["workflow_id"] = str(detail.workflow_id)
            contexts[submission_id] = context
        offset += len(page.items)
        if not page.items or offset >= page.total:
            break
    return contexts


def configure_production_results_loaders() -> None:
    """把生产加载器注入结果视图；应用装配期间调用一次。"""

    configure_results_loaders(
        student_loader=load_student_result,
        student_diagnosis_loader=load_student_diagnosis,
        teacher_courses_loader=load_teacher_courses,
        teacher_exams_loader=load_teacher_exams,
        teacher_loader=load_teacher_results,
        teacher_summary_loader=load_teacher_summary,
        teacher_diagnosis_loader=load_teacher_diagnosis,
    )


__all__ = [
    "configure_production_results_loaders",
    "load_student_diagnosis",
    "load_student_result",
    "load_teacher_courses",
    "load_teacher_diagnosis",
    "load_teacher_exams",
    "load_teacher_results",
    "load_teacher_summary",
    "resolve_student_submission_id",
    "state_user_id",
]
