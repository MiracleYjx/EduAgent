"""T082 read-only report tools for the authenticated in-app tool boundary."""

from __future__ import annotations

import re
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from backend.app.api.results import (
    ResultsQueryService,
    build_production_results_query_service,
)
from backend.app.domain.enums import UserRole
from backend.app.domain.permissions import Permission
from backend.app.mcp.registry import (
    ToolContext,
    ToolExecutionError,
    ToolRegistry,
    ToolSpec,
)
from backend.app.models import Exam, User
from backend.app.schemas.grading import (
    DiagnosisReportDTO,
    DiagnosisStatus,
    StudentResultSummaryDTO,
)
from backend.app.services.course_service import (
    CourseNotFoundError,
    CoursePermissionError,
    CourseService,
)
from backend.app.services.exam_service import ExamService
from backend.app.services.grading.grading_task_service import (
    GradingPermissionError,
    GradingSubmissionNotFoundError,
    GradingTaskError,
)

_MAX_TEXT = 500
_UUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_PROFILE_SCOPE = "按答卷聚合，非跨考试画像"


class GetExamResultArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exam_id: UUID
    student_id: UUID | None = None


class GetStudentProfileArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    student_id: UUID
    course_id: UUID | None = None


class ReportType(str, Enum):
    EXAM = "exam"
    STUDENT = "student"


class CreateReportArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_type: ReportType
    exam_id: UUID | None = None
    student_id: UUID | None = None
    course_id: UUID | None = None


def _text(value: str) -> str:
    """Only a bounded, UUID-redacted excerpt crosses the tool boundary."""

    sanitized = _UUID_PATTERN.sub("[内部标识]", value)
    return sanitized if len(sanitized) <= _MAX_TEXT else sanitized[:_MAX_TEXT] + "…"


def _service(configured: ResultsQueryService | None) -> ResultsQueryService:
    return configured or build_production_results_query_service()


def _teacher(context: ToolContext) -> bool:
    return UserRole.TEACHER in context.roles


def _result_error(error: GradingTaskError) -> ToolExecutionError:
    if isinstance(error, GradingPermissionError):
        return ToolExecutionError("COURSE_FORBIDDEN", "无权访问该课程或结果。")
    if isinstance(error, GradingSubmissionNotFoundError):
        return ToolExecutionError("RESULT_NOT_FOUND", "考试或答卷不存在。")
    return ToolExecutionError(error.error_code, "结果读模型暂不可用。")


def _require_course(context: ToolContext, course_id: UUID) -> None:
    try:
        CourseService(context.session).get_course(
            course_id, teacher_id=context.actor_id
        )
    except CoursePermissionError as error:
        raise ToolExecutionError("COURSE_FORBIDDEN", "无权访问该课程。") from error
    except CourseNotFoundError as error:
        raise ToolExecutionError("COURSE_NOT_FOUND", "课程不存在。") from error


def _name(context: ToolContext, student_id: str | None) -> str | None:
    user = context.session.get(User, UUID(student_id)) if student_id else None
    return _text(user.username) if user is not None else None


def _result_row(context: ToolContext, item: StudentResultSummaryDTO) -> dict[str, Any]:
    return {
        "exam_title": _text(item.exam_title),
        "student_name": _name(context, item.student_id),
        "submitted_at": item.submitted_at.isoformat() if item.submitted_at else None,
        "total_score": str(item.total_score) if item.total_score is not None else None,
        "result_status": item.result_status.value if item.result_status else None,
        "is_final": item.is_final,
        "pending_review_count": item.pending_review_count,
        "not_ready_reason": _text(item.not_ready_reason)
        if item.not_ready_reason
        else None,
    }


def _diagnosis_row(report: DiagnosisReportDTO, *, exam_title: str) -> dict[str, Any]:
    row: dict[str, Any] = {
        "exam_title": _text(exam_title),
        "status": report.status.value,
        "generated_at": report.generated_at.isoformat()
        if report.generated_at
        else None,
        "mastery_by_knowledge_point": [],
        "weak_knowledge_points": [],
        "error_reasons": [],
        "learning_suggestions": [],
        "message": "",
    }
    if report.status is not DiagnosisStatus.READY:
        row["message"] = {
            DiagnosisStatus.NOT_READY: "该答卷暂无诊断。",
            DiagnosisStatus.FAILED: "该答卷诊断生成失败。",
            DiagnosisStatus.STALE: "该答卷诊断已过期。",
        }[report.status]
        return row
    row["mastery_by_knowledge_point"] = [
        {
            "knowledge_point": _text(item.knowledge_point),
            "mastery": str(item.mastery),
            "answered_count": item.answered_count,
            "correct_count": item.correct_count,
        }
        for item in report.mastery_by_knowledge_point
    ]
    row["weak_knowledge_points"] = [
        {
            "knowledge_point": _text(item.knowledge_point),
            "reason": _text(item.reason),
            "mastery": str(item.mastery),
        }
        for item in report.weak_knowledge_points
    ]
    row["error_reasons"] = [_text(item) for item in report.error_reasons]
    row["learning_suggestions"] = [_text(item) for item in report.learning_suggestions]
    return row


def _exam_result(
    context: ToolContext,
    args: GetExamResultArguments,
    service: ResultsQueryService,
) -> dict[str, Any]:
    try:
        if _teacher(context):
            summaries = service.list_exam_results(
                str(context.actor_id), str(args.exam_id)
            )
            summary = service.get_exam_summary(str(context.actor_id), str(args.exam_id))
            if args.student_id is not None:
                summaries = [
                    item
                    for item in summaries
                    if item.student_id == str(args.student_id)
                ]
            output: dict[str, Any] = {
                "summary": {
                    "submitted_count": summary.submitted_count,
                    "final_count": summary.final_count,
                    "pending_review_count": summary.pending_review_count,
                    "average_of_final_scores": str(summary.average_of_final_scores)
                    if summary.average_of_final_scores is not None
                    else None,
                    "not_ready": summary.not_ready,
                    "not_ready_reason": _text(summary.not_ready_reason)
                    if summary.not_ready_reason
                    else None,
                }
            }
        else:
            if args.student_id is not None and args.student_id != context.actor_id:
                raise ToolExecutionError("STUDENT_FORBIDDEN", "只能读取本人成绩。")
            summaries = [
                item
                for item in service.list_student_results(str(context.actor_id))
                if item.exam_id == str(args.exam_id)
            ]
            output = {}
    except GradingTaskError as error:
        raise _result_error(error) from error
    output["results"] = [_result_row(context, item) for item in summaries]
    output["count"] = len(summaries)
    output["message"] = "未找到可访问的考试结果。" if not summaries else ""
    return output


def _profile(
    context: ToolContext,
    args: GetStudentProfileArguments,
    service: ResultsQueryService,
) -> dict[str, Any]:
    try:
        if _teacher(context):
            if args.course_id is None:
                raise ToolExecutionError("COURSE_ID_REQUIRED", "教师查询必须指定课程。")
            _require_course(context, args.course_id)
            summaries = _teacher_student_summaries(
                context, service, args.student_id, args.course_id
            )
        else:
            if args.student_id != context.actor_id:
                raise ToolExecutionError("STUDENT_FORBIDDEN", "只能读取本人诊断。")
            summaries = service.list_student_results(str(context.actor_id))
            if args.course_id is not None:
                summaries = [
                    item
                    for item in summaries
                    if (exam := context.session.get(Exam, UUID(item.exam_id)))
                    is not None
                    and exam.course_id == args.course_id
                ]
        diagnoses = [
            _diagnosis_row(
                service.get_student_diagnosis(str(args.student_id), item.submission_id),
                exam_title=item.exam_title,
            )
            for item in summaries
        ]
    except GradingTaskError as error:
        raise _result_error(error) from error
    return {
        "scope": _PROFILE_SCOPE,
        "diagnoses": diagnoses,
        "count": len(diagnoses),
        "message": "该范围内暂无答卷诊断。" if not diagnoses else "",
    }


def _teacher_student_summaries(
    context: ToolContext,
    service: ResultsQueryService,
    student_id: UUID,
    course_id: UUID,
) -> list[StudentResultSummaryDTO]:
    """Only teacher-owned exams provide submissions for teacher diagnosis reads."""

    exams = ExamService(context.session).list_exams(
        course_id, teacher_id=context.actor_id
    )
    return [
        item
        for exam in exams
        for item in service.list_exam_results(str(context.actor_id), exam.id)
        if item.student_id == str(student_id)
    ]


def _create_report(
    context: ToolContext,
    args: CreateReportArguments,
    service: ResultsQueryService,
) -> dict[str, Any]:
    if args.report_type is ReportType.STUDENT:
        if args.course_id is None:
            raise ToolExecutionError("COURSE_ID_REQUIRED", "学生报告必须指定课程。")
        if args.student_id is None:
            raise ToolExecutionError("STUDENT_ID_REQUIRED", "学生报告必须指定学生。")
        _require_course(context, args.course_id)
        profile = _profile(
            context,
            GetStudentProfileArguments(
                student_id=args.student_id, course_id=args.course_id
            ),
            service,
        )
        try:
            results = _teacher_student_summaries(
                context, service, args.student_id, args.course_id
            )
        except GradingTaskError as error:
            raise _result_error(error) from error
        return {
            "report_type": "student",
            **profile,
            "results": [_result_row(context, item) for item in results],
        }
    if args.exam_id is None:
        raise ToolExecutionError("EXAM_ID_REQUIRED", "考试报告必须指定考试。")
    result = _exam_result(
        context, GetExamResultArguments(exam_id=args.exam_id), service
    )
    try:
        summaries = service.list_exam_results(str(context.actor_id), str(args.exam_id))
        diagnoses = [
            {
                "student_name": _name(context, item.student_id),
                **_diagnosis_row(
                    service.get_student_diagnosis(
                        item.student_id or "", item.submission_id
                    ),
                    exam_title=item.exam_title,
                ),
            }
            for item in summaries
        ]
    except GradingTaskError as error:
        raise _result_error(error) from error
    return {"report_type": "exam", **result, "diagnoses": diagnoses}


def register_report_tools(
    registry: ToolRegistry, *, results_service: ResultsQueryService | None = None
) -> None:
    """Register read-only tools; all service calls use the JWT-derived actor."""

    registry.register(
        ToolSpec(
            name="get_exam_result",
            description="读取授权考试的成绩及待复核状态。",
            permission=Permission.VIEW_EXAM_RESULTS,
            any_of_permissions=(Permission.VIEW_OWN_RESULTS,),
            arguments_model=GetExamResultArguments,
            handler=lambda context, args: _exam_result(
                context, args, _service(results_service)
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="get_student_profile",
            description="读取逐答卷诊断；不生成跨考试画像。",
            permission=Permission.VIEW_EXAM_RESULTS,
            any_of_permissions=(Permission.VIEW_OWN_RESULTS,),
            arguments_model=GetStudentProfileArguments,
            handler=lambda context, args: _profile(
                context, args, _service(results_service)
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="create_report",
            description="聚合已持久化的考试结果或逐答卷诊断报告。",
            permission=Permission.VIEW_EXAM_RESULTS,
            arguments_model=CreateReportArguments,
            handler=lambda context, args: _create_report(
                context, args, _service(results_service)
            ),
        )
    )


__all__ = ["register_report_tools"]
