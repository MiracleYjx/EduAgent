"""T057 结果与诊断读模型 API：学生本人反馈与教师考试结果。

契约依据：T057（学生成绩、错题、诊断与知识点掌握情况视图）、FR-038～FR-040、
plan.md §5.2、tasks.md 的 T111/T112（服务端事实来源与最终成绩统计）。

读模型边界：

- 结果事实来源是结果存储（:class:`~backend.app.services.grading.grading_task_service.GradingRepository`），
  未接通时返回 ``503 GRADING_STORE_NOT_READY``，**不返回 0 分或 0 人的假结果**。
- 尚无成绩时返回明确空态（``not_ready_reason`` 非空、``total_score`` 为 ``None``）。
- 学生面只展示已确认条目；未确认（待复核）题目不计入正式错题，也不计入掌握度。
- 教师摘要的 ``average_of_final_scores`` 只统计最终成绩；存在尚无结果的答卷时 ``not_ready`` 为真。

权限：学生身份取自认证上下文（``VIEW_OWN_RESULTS``）并校验 ``Submission.student_id``；
教师端点要求 ``VIEW_EXAM_RESULTS`` 并沿 Exam → Course 校验课程归属；仅 Admin 不获得
教师业务权限。错误语义：401 未认证、403 无权限、404 不存在、503 依赖未就绪。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.database import get_session_factory
from backend.app.core.security import require_permission
from backend.app.domain.enums import SubmissionStatus
from backend.app.domain.permissions import Permission
from backend.app.models import Course, Exam, Submission, User
from backend.app.schemas.grading import (
    DiagnosisReportDTO,
    DiagnosisStatus,
    QuestionResultDTO,
    StudentResultSummaryDTO,
    SubmissionResultDTO,
    TeacherExamResultSummaryDTO,
)
from backend.app.services.diagnosis_service import DIAGNOSIS_NOT_READY
from backend.app.services.grading.diagnosis_report_store import DiagnosisReportStore
from backend.app.services.grading.grading_repository import DatabaseGradingRepository
from backend.app.services.grading.grading_task_service import (
    GRADING_EXECUTION_NOT_READY,
    GRADING_NOT_ALLOWED,
    GRADING_PERMISSION_DENIED,
    GRADING_RESULT_NOT_FOUND,
    GRADING_STORE_NOT_READY,
    GRADING_SUBMISSION_NOT_FOUND,
    GRADING_TASK_NOT_FOUND,
    GRADING_TRIGGER_CONFLICT,
    GradingPermissionError,
    GradingRepository,
    GradingSubmissionNotFoundError,
    GradingTaskError,
)

router = APIRouter(prefix="/api/results", tags=["成绩与诊断"])

#: 尚无成绩时的明确空态原因。
RESULT_NOT_READY_REASON = "成绩尚未生成：阅卷任务未完成或结果存储未接通。"
#: 教师摘要存在尚无结果答卷时的说明。
TEACHER_NOT_READY_REASON = "存在尚无成绩的答卷，统计仅覆盖已生成的结果。"

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


class ResultsQueryService:
    """结果读模型查询服务；学生面与教师面共用同一事实来源。

    :param repository: 任务与结果存储；生产默认未就绪（T060 前返回 503）。
    :param session: 单会话用法；与 ``session_factory`` 二选一。
    :param session_factory: 自建会话用法（UI 或后台）；不复用请求作用域会话。
    :param diagnosis_store: 诊断报告读写存储；``None`` 时诊断端点返回明确的未就绪状态。
        诊断读取永远只读，不触发生成与 LLM 调用。
    """

    def __init__(
        self,
        *,
        repository: GradingRepository,
        session: Session | None = None,
        session_factory: Callable[[], Session] | None = None,
        diagnosis_store: DiagnosisReportStore | None = None,
    ) -> None:
        if session is None and session_factory is None:
            raise ValueError("必须提供 session 或 session_factory。")
        self._repository = repository
        self._session = session
        self._session_factory = session_factory
        self._diagnosis_store = diagnosis_store

    @contextmanager
    def _use_session(self) -> Iterator[Session]:
        if self._session_factory is not None:
            session = self._session_factory()
            try:
                yield session
            finally:
                session.close()
            return
        assert self._session is not None
        yield self._session

    # ------------------------------------------------------------------ 学生面

    def list_student_results(self, student_id: str) -> list[StudentResultSummaryDTO]:
        """列出学生自己的答卷结果摘要。"""

        self._repository.ensure_ready()
        with self._use_session() as session:
            submissions = self._submissions_for_student(session, student_id)
            return [self._summary(session, item) for item in submissions]

    def get_student_result(
        self,
        student_id: str,
        submission_id: str,
    ) -> SubmissionResultDTO:
        """读取学生自己的答卷结果；仅返回已确认条目。"""

        self._repository.ensure_ready()
        with self._use_session() as session:
            submission = self._require_submission(session, submission_id)
            self._ensure_student_owner(submission, student_id)
            return self._detail(session, submission, include_unconfirmed=False)

    def get_student_diagnosis(
        self,
        student_id: str,
        submission_id: str,
    ) -> DiagnosisReportDTO:
        """读取学生自己的诊断报告；未就绪时返回明确状态。"""

        self._repository.ensure_ready()
        with self._use_session() as session:
            submission = self._require_submission(session, submission_id)
            self._ensure_student_owner(submission, student_id)
            return self._diagnosis(submission)

    # ------------------------------------------------------------------ 教师面

    def list_exam_results(
        self,
        teacher_id: str,
        exam_id: str,
    ) -> list[StudentResultSummaryDTO]:
        """列出授权课程下某场考试的学生结果摘要。"""

        self._repository.ensure_ready()
        with self._use_session() as session:
            exam = self._require_owned_exam(session, exam_id, teacher_id)
            submissions = list(
                session.scalars(
                    select(Submission).where(Submission.exam_id == exam.id)
                )
            )
            return [
                self._summary(session, item, with_student=True)
                for item in submissions
            ]

    def get_exam_summary(
        self,
        teacher_id: str,
        exam_id: str,
    ) -> TeacherExamResultSummaryDTO:
        """汇总已提交生命周期的答卷；平均分只统计最终成绩，草稿不参与。"""

        self._repository.ensure_ready()
        with self._use_session() as session:
            exam = self._require_owned_exam(session, exam_id, teacher_id)
            submissions = list(
                session.scalars(
                    select(Submission).where(
                        Submission.exam_id == exam.id,
                        Submission.status.in_(
                            (
                                SubmissionStatus.SUBMITTED,
                                SubmissionStatus.GRADED,
                                SubmissionStatus.REVIEWED,
                            )
                        ),
                    )
                )
            )
            finals: list[Decimal] = []
            pending_total = 0
            not_ready = False
            for submission in submissions:
                result = self._repository.get_exam_result(str(submission.id))
                if result is None:
                    not_ready = True
                    continue
                pending_total += result.pending_review_answer_count
                if result.is_final and result.final_total_score is not None:
                    finals.append(result.final_total_score)
            average: Decimal | None = None
            if finals:
                average = (sum(finals) / Decimal(len(finals))).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            return TeacherExamResultSummaryDTO(
                exam_id=str(exam.id),
                submitted_count=len(submissions),
                final_count=len(finals),
                pending_review_count=pending_total,
                average_of_final_scores=average,
                not_ready=not_ready,
                not_ready_reason=TEACHER_NOT_READY_REASON if not_ready else None,
            )

    def get_teacher_student_result(
        self,
        teacher_id: str,
        exam_id: str,
        submission_id: str,
    ) -> SubmissionResultDTO:
        """教师读取授权课程内某位学生的结果；包含待复核条目以便复核。"""

        with self._use_session() as session:
            exam = self._require_owned_exam(session, exam_id, teacher_id)
            submission = self._require_submission(session, submission_id)
            if submission.exam_id != exam.id:
                raise GradingSubmissionNotFoundError(
                    "该答卷不属于指定考试。"
                )
            return self._detail(session, submission, include_unconfirmed=True)

    # ------------------------------------------------------------------ 内部

    def _submissions_for_student(
        self,
        session: Session,
        student_id: str,
    ) -> list[Submission]:
        return list(
            session.scalars(
                select(Submission)
                .where(Submission.student_id == _as_uuid(student_id, "学生标识"))
                .order_by(Submission.submitted_at.desc())
            )
        )

    def _require_submission(self, session: Session, submission_id: str) -> Submission:
        submission = session.get(
            Submission, _as_uuid(submission_id, "答卷标识")
        )
        if submission is None:
            raise GradingSubmissionNotFoundError(f"答卷 {submission_id} 不存在。")
        return submission

    def _require_owned_exam(
        self,
        session: Session,
        exam_id: str,
        teacher_id: str,
    ) -> Exam:
        exam = session.get(Exam, _as_uuid(exam_id, "考试标识"))
        if exam is None:
            raise GradingSubmissionNotFoundError(f"考试 {exam_id} 不存在。")
        course = session.get(Course, exam.course_id)
        if course is None or str(course.created_by) != str(teacher_id):
            raise GradingPermissionError("无权访问该考试所属课程。")
        return exam

    @staticmethod
    def _ensure_student_owner(submission: Submission, student_id: str) -> None:
        if str(submission.student_id) != str(student_id):
            raise GradingPermissionError("无权访问他人的答卷与诊断。")

    def _summary(
        self,
        session: Session,
        submission: Submission,
        *,
        with_student: bool = False,
    ) -> StudentResultSummaryDTO:
        exam = session.get(Exam, submission.exam_id)
        result = self._repository.get_exam_result(str(submission.id))
        return StudentResultSummaryDTO(
            submission_id=str(submission.id),
            exam_id=str(submission.exam_id),
            exam_title=exam.title if exam is not None else "（考试信息缺失）",
            student_id=str(submission.student_id) if with_student else None,
            submitted_at=submission.submitted_at,
            result_status=result.result_status if result is not None else None,
            is_final=result.is_final if result is not None else None,
            total_score=result.final_total_score if result is not None else None,
            confirmed_subtotal=(
                result.confirmed_subtotal if result is not None else None
            ),
            pending_review_count=(
                result.pending_review_answer_count if result is not None else None
            ),
            not_ready_reason=(
                None if result is not None else RESULT_NOT_READY_REASON
            ),
        )

    def _detail(
        self,
        session: Session,
        submission: Submission,
        *,
        include_unconfirmed: bool,
    ) -> SubmissionResultDTO:
        exam = session.get(Exam, submission.exam_id)
        base: dict[str, object] = {
            "submission_id": str(submission.id),
            "exam_id": str(submission.exam_id),
            "exam_title": exam.title if exam is not None else "（考试信息缺失）",
            "student_id": str(submission.student_id),
        }
        result = self._repository.get_exam_result(str(submission.id))
        if result is None:
            return SubmissionResultDTO(
                **base,  # type: ignore[arg-type]
                not_ready_reason=RESULT_NOT_READY_REASON,
            )
        items: list[QuestionResultDTO] = list(result.items)
        if not include_unconfirmed:
            items = [item for item in items if item.counted]
        mistakes = [
            item.answer_id
            for item in items
            if item.counted and (item.effective_score or Decimal(0)) < item.max_score
        ]
        return SubmissionResultDTO(
            **base,  # type: ignore[arg-type]
            result_status=result.result_status,
            is_final=result.is_final,
            total_score=result.final_total_score,
            confirmed_subtotal=result.confirmed_subtotal,
            confirmed_subtotal_label=result.confirmed_subtotal_label,
            total_max_score=result.total_max_score,
            expected_answer_count=result.expected_answer_count,
            graded_answer_count=result.graded_answer_count,
            pending_review_count=result.pending_review_answer_count,
            items=items,
            mistake_answer_ids=mistakes,
        )

    def _diagnosis(self, submission: Submission) -> DiagnosisReportDTO:
        """读取已持久化的诊断报告；GET 只读，不触发生成与 LLM 调用。

        - 尚未生成报告：返回 ``Not Ready`` 空态，不落库；
        - 报告来源汇总时间与当前 ``ExamResult.aggregated_at`` 不一致：返回 ``Stale``；
        - ``Failed`` 报告如实返回错误码，已提交成绩不受影响。
        """

        result = self._repository.get_exam_result(str(submission.id))
        if self._diagnosis_store is None:
            return DiagnosisReportDTO(
                submission_id=str(submission.id),
                student_id=str(submission.student_id),
                status=DiagnosisStatus.NOT_READY,
                error_code=DIAGNOSIS_NOT_READY,
                retryable=False,
                source_exam_result_updated_at=(
                    None if result is None else result.aggregated_at
                ),
            )
        return self._diagnosis_store.read(str(submission.id), result)


def build_production_results_query_service() -> ResultsQueryService:
    """构造生产结果读模型服务：真实仓储读取成绩 + 只读诊断读取。

    结果事实来源是 ``exam_results``/``grading_results``；诊断来自持久化 ``diagnosis_reports``，
    GET 路径不会调用 T055 生成，也不产生 LLM 调用。
    """

    session_factory = get_session_factory()
    return ResultsQueryService(
        repository=DatabaseGradingRepository(session_factory=session_factory),
        session_factory=session_factory,
        diagnosis_store=DiagnosisReportStore(session_factory=session_factory),
    )


def get_results_query_service(request: Request) -> ResultsQueryService:
    """装配结果读模型服务；生产使用真实仓储与只读诊断存储。"""

    configured = getattr(request.app.state, "results_query_service", None)
    if configured is not None:
        return configured
    return build_production_results_query_service()


def _results_http_exception(error: GradingTaskError) -> HTTPException:
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


@router.get(
    "/me/submissions",
    summary="列出学生自己的答卷结果",
)
def list_my_results(
    user: Annotated[User, Depends(require_permission(Permission.VIEW_OWN_RESULTS))],
    service: Annotated[ResultsQueryService, Depends(get_results_query_service)],
) -> list[StudentResultSummaryDTO]:
    """学生结果列表；身份取自认证上下文。"""

    try:
        return service.list_student_results(str(user.id))
    except GradingTaskError as error:
        raise _results_http_exception(error) from None


@router.get(
    "/me/submissions/{submission_id}",
    summary="读取学生自己的答卷结果与错题",
)
def get_my_result(
    submission_id: str,
    user: Annotated[User, Depends(require_permission(Permission.VIEW_OWN_RESULTS))],
    service: Annotated[ResultsQueryService, Depends(get_results_query_service)],
) -> SubmissionResultDTO:
    """学生本人反馈；待复核题目不计入错题与最终总分。"""

    try:
        return service.get_student_result(str(user.id), submission_id)
    except GradingTaskError as error:
        raise _results_http_exception(error) from None


@router.get(
    "/me/submissions/{submission_id}/diagnosis",
    summary="读取学生自己的诊断报告",
)
def get_my_diagnosis(
    submission_id: str,
    user: Annotated[User, Depends(require_permission(Permission.VIEW_OWN_RESULTS))],
    service: Annotated[ResultsQueryService, Depends(get_results_query_service)],
) -> DiagnosisReportDTO:
    """学生诊断；未就绪时返回 Not Ready，不返回伪造建议。"""

    try:
        return service.get_student_diagnosis(str(user.id), submission_id)
    except GradingTaskError as error:
        raise _results_http_exception(error) from None


@router.get(
    "/exams/{exam_id}/submissions",
    summary="列出考试下学生的结果摘要",
)
def list_exam_results(
    exam_id: str,
    user: Annotated[User, Depends(require_permission(Permission.VIEW_EXAM_RESULTS))],
    service: Annotated[ResultsQueryService, Depends(get_results_query_service)],
) -> list[StudentResultSummaryDTO]:
    """教师面结果列表；仅限本人课程。"""

    try:
        return service.list_exam_results(str(user.id), exam_id)
    except GradingTaskError as error:
        raise _results_http_exception(error) from None


@router.get(
    "/exams/{exam_id}/summary",
    summary="读取考试结果摘要",
)
def get_exam_summary(
    exam_id: str,
    user: Annotated[User, Depends(require_permission(Permission.VIEW_EXAM_RESULTS))],
    service: Annotated[ResultsQueryService, Depends(get_results_query_service)],
) -> TeacherExamResultSummaryDTO:
    """教师摘要；平均分只统计最终成绩。"""

    try:
        return service.get_exam_summary(str(user.id), exam_id)
    except GradingTaskError as error:
        raise _results_http_exception(error) from None


@router.get(
    "/exams/{exam_id}/submissions/{submission_id}",
    summary="教师读取指定学生结果与待复核条目",
)
def get_exam_student_result(
    exam_id: str,
    submission_id: str,
    user: Annotated[User, Depends(require_permission(Permission.VIEW_EXAM_RESULTS))],
    service: Annotated[ResultsQueryService, Depends(get_results_query_service)],
) -> SubmissionResultDTO:
    """教师面单份结果；包含待复核条目并标注临时状态。"""

    try:
        return service.get_teacher_student_result(
            str(user.id), exam_id, submission_id
        )
    except GradingTaskError as error:
        raise _results_http_exception(error) from None


def _as_uuid(value: str, label: str) -> UUID:
    """把字符串标识转换为 UUID；非法标识按不存在处理。"""

    try:
        return UUID(value)
    except ValueError as error:
        raise GradingSubmissionNotFoundError(f"{label}非法。") from error


__all__ = [
    "RESULT_NOT_READY_REASON",
    "TEACHER_NOT_READY_REASON",
    "ResultsQueryService",
    "build_production_results_query_service",
    "get_results_query_service",
    "router",
]
