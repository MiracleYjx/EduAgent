"""T077 复核 API：待复核队列、单题详情与教师决策提交。

契约依据：T077（低置信度评分详情与教师确认/修改）、plan.md §5/§5.2、FR-036/FR-037、
T054 汇总、T060 结果存储、T062 ``ReviewRecord``、T064 ``WorkflowRun``、T073 检查点、
T074 ``ReviewService`` 与 ``TeacherReviewDecision``。

事实来源与边界：

- **权威查询源（B08）**：队列与详情只读持久化事实（``GradingResult``/``Answer``/``Submission``/
  ``Exam``/``Course``/``Question``/``ReviewRecord``）；LangGraph 检查点只提供运行控制信息
  （``workflow_id``/``thread_id``/状态），**不**用来拼装分数或学生答案。
- **对象级授权（B03）**：队列、详情与决策都必须落在教师自己的课程内；检索依据按
  ``retrieved_context_ids`` 二次按课程过滤，缺失片段明确计数，不从其他课程补齐。
- **修订字段白名单（B09）**：教师只能修改 ``score`` 与 ``reason``；题型、满分、知识点、来源片段、
  置信度、答案与答卷身份一律取数据库权威原结果，客户端不得提交覆盖。
- **部分成功（B10）**：决策已落库但恢复失败时返回 200 + ``decision_saved=True`` 与
  ``resume_status=pending``；重试恢复走 T076 的唯一恢复入口，不在本模块暴露第二个恢复端点。
  重复提交同一结论按既有记录幂等返回，**不**产生第二条 ``ReviewRecord``。
- **支持范围（遗漏5）**：本批只接受 ``confirm``（Confirmed）与 ``modify``（Modified）；
  ``Re-grade``/``Final`` 由后续批次提供，本模块明确 422 拒绝，UI 不显示未实现按钮。

错误语义：401 未认证、403 无权限或跨课程、404 评分结果不存在、409 陈旧决策或跨工作流、
422 输入非法（含修订缺分数/理由）、503 复核服务或存储未就绪。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.ai.workflows.grading_workflow import TeacherReviewDecision
from backend.app.api.workflow import build_production_review_service
from backend.app.core.config import AppSettings
from backend.app.core.database import get_session_factory
from backend.app.core.security import require_permission
from backend.app.domain.enums import (
    QuestionType,
    ReviewStatus,
    UserRole,
    WorkflowStatus,
)
from backend.app.domain.permissions import Permission
from backend.app.models import (
    Answer,
    Course,
    Document,
    DocumentChunk,
    Exam,
    GradingResult,
    Question,
    ReviewRecord,
    Submission,
    User,
    WorkflowRun,
)
from backend.app.schemas.ai import GradingResult as GradingResultPayload
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
    REVIEW_SERVICE_STALE_ROUND,
    ReviewOutcome,
    ReviewService,
    ReviewServiceError,
    ReviewStaleRoundError,
    find_matching_review_record,
)
from backend.app.services.workflow_checkpoint import (
    WorkflowCheckpointError,
    checkpoint_thread_id,
    run_kind_criteria,
    runtime_has_checkpoint,
)

router = APIRouter(prefix="/api/reviews", tags=["阅卷复核"])

#: API 层错误码：只补充接口语义，不重定义 T074 的错误码。
REVIEW_QUERY_ANSWER_NOT_FOUND: str = "REVIEW_QUERY_ANSWER_NOT_FOUND"
REVIEW_QUERY_PERMISSION_DENIED: str = "REVIEW_QUERY_PERMISSION_DENIED"
REVIEW_QUERY_INVALID_PAGE: str = "REVIEW_QUERY_INVALID_PAGE"
REVIEW_QUERY_STORE_NOT_READY: str = "REVIEW_QUERY_STORE_NOT_READY"
REVIEW_DECISION_WORKFLOW_REQUIRED: str = "REVIEW_DECISION_WORKFLOW_REQUIRED"
REVIEW_DECISION_WORKFLOW_MISMATCH: str = "REVIEW_DECISION_WORKFLOW_MISMATCH"
REVIEW_DECISION_STALE: str = "REVIEW_DECISION_STALE"
REVIEW_DECISION_REVISION_REQUIRED: str = "REVIEW_DECISION_REVISION_REQUIRED"
REVIEW_DECISION_INVALID_SCORE: str = "REVIEW_DECISION_INVALID_SCORE"
REVIEW_DECISION_UNSUPPORTED: str = "REVIEW_DECISION_UNSUPPORTED"
REVIEW_DECISION_SERVICE_NOT_READY: str = "REVIEW_DECISION_SERVICE_NOT_READY"

#: 错误码到 HTTP 状态码的映射；未列出的错误按 500 处理并保持脱敏。
_ERROR_STATUS: dict[str, int] = {
    REVIEW_QUERY_STORE_NOT_READY: 503,
    REVIEW_DECISION_SERVICE_NOT_READY: 503,
    REVIEW_SERVICE_NOT_READY: 503,
    REVIEW_QUERY_ANSWER_NOT_FOUND: 404,
    REVIEW_SERVICE_ANSWER_NOT_FOUND: 404,
    REVIEW_QUERY_PERMISSION_DENIED: 403,
    REVIEW_SERVICE_PERMISSION_DENIED: 403,
    REVIEW_DECISION_WORKFLOW_REQUIRED: 409,
    REVIEW_DECISION_WORKFLOW_MISMATCH: 409,
    REVIEW_DECISION_STALE: 409,
    REVIEW_SERVICE_STALE_DECISION: 409,
    REVIEW_SERVICE_STALE_ROUND: 409,
    REVIEW_SERVICE_CONFLICT: 409,
    REVIEW_SERVICE_IDENTITY_MISMATCH: 409,
    REVIEW_DECISION_REVISION_REQUIRED: 422,
    REVIEW_DECISION_INVALID_SCORE: 422,
    REVIEW_DECISION_UNSUPPORTED: 422,
    REVIEW_QUERY_INVALID_PAGE: 422,
    REVIEW_SERVICE_INVALID_DECISION: 422,
    REVIEW_SERVICE_REVISION_REQUIRED: 422,
    REVIEW_SERVICE_AGGREGATION_FAILED: 500,
}

#: 默认分页大小与上限。
DEFAULT_PAGE_SIZE: int = 50
MAX_PAGE_SIZE: int = 200

#: 本批支持的教师决策动作。
DECISION_ACTIONS: frozenset[str] = frozenset({"confirm", "modify"})


class ReviewQueryError(RuntimeError):
    """复核查询与决策的边界错误；保留脱敏错误码与可重试语义。"""

    error_code: str = REVIEW_QUERY_STORE_NOT_READY
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


class ReviewAnswerNotFoundError(ReviewQueryError):
    """评分结果不存在或不属于该答卷。"""

    error_code = REVIEW_QUERY_ANSWER_NOT_FOUND


class ReviewQueryPermissionError(ReviewQueryError):
    """教师对该答卷所属课程没有管理权。"""

    error_code = REVIEW_QUERY_PERMISSION_DENIED


class ReviewInvalidPageError(ReviewQueryError):
    """分页参数非法。"""

    error_code = REVIEW_QUERY_INVALID_PAGE


class ReviewDecisionWorkflowError(ReviewQueryError):
    """缺少可用的运行记录，或客户端声明的运行与数据库事实不一致。"""

    error_code = REVIEW_DECISION_WORKFLOW_REQUIRED


class ReviewDecisionStaleError(ReviewQueryError):
    """该题当前状态不允许再次提交教师结论。"""

    error_code = REVIEW_DECISION_STALE


class ReviewDecisionRevisionRequiredError(ReviewQueryError):
    """修改评分必须给出分数与理由。"""

    error_code = REVIEW_DECISION_REVISION_REQUIRED


class ReviewDecisionScoreError(ReviewQueryError):
    """修订分数非法（超出满分或不是有效数值）。"""

    error_code = REVIEW_DECISION_INVALID_SCORE


class ReviewDecisionUnsupportedError(ReviewQueryError):
    """本批不支持的决策类型。"""

    error_code = REVIEW_DECISION_UNSUPPORTED


class ReviewDecisionNotReadyError(ReviewQueryError):
    """复核服务未接线或未就绪。"""

    error_code = REVIEW_DECISION_SERVICE_NOT_READY


# ---------------------------------------------------------------------- 请求/响应 DTO


class ReviewEvidenceDTO(BaseModel):
    """一条检索依据；只返回按课程校验通过的片段。"""

    model_config = ConfigDict(frozen=True)

    chunk_id: str
    course_id: str | None = None
    document_id: str | None = None
    source_file: str | None = None
    chunk_index: int | None = None
    content: str


class ReviewQueueItemDTO(BaseModel):
    """复核队列条目；字段全部来自持久化事实。"""

    submission_id: str
    answer_id: str
    course_id: str
    exam_id: str
    exam_title: str
    student_id: str
    student_name: str
    question_id: str
    question_number: int
    question_type: QuestionType
    max_score: Decimal
    score: Decimal | None = None
    confidence: float | None = None
    review_status: ReviewStatus
    pending_review_round_id: UUID | None = None
    requires_review: bool
    updated_at: datetime


class ReviewQueuePageDTO(BaseModel):
    """队列分页结果。"""

    total: int
    limit: int
    offset: int
    items: list[ReviewQueueItemDTO]


class ReviewRecordDTO(BaseModel):
    """一条已落库的复核记录（审计轨迹）。"""

    review_record_id: str
    reviewer_id: str
    decision: ReviewStatus
    review_round_id: UUID | None = None
    original_score: Decimal
    original_reason: str
    original_knowledge_points: list[str] = Field(default_factory=list)
    final_score: Decimal | None = None
    final_reason: str | None = None
    comment: str | None = None
    created_at: datetime


class ReviewDetailDTO(BaseModel):
    """单题复核详情：学生答案、AI 评分、题目依据与运行身份。"""

    submission_id: str
    answer_id: str
    course_id: str
    exam_id: str
    exam_title: str
    student_id: str
    student_name: str
    question_id: str
    question_number: int
    question_type: QuestionType
    question_content: str
    reference_answer: str | None = None
    scoring_rubric: str | None = None
    max_score: Decimal
    student_answer: str
    score: Decimal | None = None
    reason: str | None = None
    knowledge_points: list[str] = Field(default_factory=list)
    missing_knowledge_points: list[str] = Field(default_factory=list)
    correct_points: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    confidence: float | None = None
    validation_status: str | None = None
    review_status: ReviewStatus
    pending_review_round_id: UUID | None = None
    requires_review: bool
    retrieved_context_ids: list[str] = Field(default_factory=list)
    evidence: list[ReviewEvidenceDTO] = Field(default_factory=list)
    unresolved_evidence_count: int = 0
    out_of_course_evidence_count: int = 0
    workflow_id: str | None = None
    thread_id: str | None = None
    workflow_status: WorkflowStatus | None = None
    resumable: bool = False
    review_records: list[ReviewRecordDTO] = Field(default_factory=list)
    updated_at: datetime


class TeacherDecisionRequest(BaseModel):
    """教师决策请求；轮次从复核详情原样回传。

    expected_review_round_id=None 为旧客户端兼容模式：按当前状态受理，沿用
    P3.1 幂等字段，但无法识别跨轮次迟到重试；回执明确标记 idempotency_degraded。
    """

    model_config = ConfigDict(extra="forbid")

    submission_id: UUID = Field(description="答卷标识。")
    answer_id: UUID = Field(description="答案标识。")
    action: Literal["confirm", "modify"] = Field(description="确认或修改评分。")
    score: Decimal | None = Field(default=None, description="修改后的分数。")
    reason: str | None = Field(default=None, max_length=65535, description="修改后的理由。")
    comment: str | None = Field(default=None, max_length=2000, description="教师备注。")
    workflow_id: str | None = Field(
        default=None, max_length=64, description="期望的工作流标识；不匹配即拒绝。"
    )
    expected_review_status: ReviewStatus | None = Field(
        default=None, description="期望的当前复核状态；不匹配即拒绝，防陈旧覆盖。"
    )
    expected_review_round_id: UUID | None = Field(
        default=None, description="期望的待复核轮次；None 降级为旧客户端兼容模式。"
    )

    @field_validator("reason", "comment", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: Any) -> str | None:
        """清理可选文本；非文本显式失败。"""

        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("该字段必须是文本。")
        return value.strip() or None


class ReviewDecisionOutcomeDTO(BaseModel):
    """决策回执；明确区分“仅保存待继续”与“已恢复”。"""

    workflow_id: str
    submission_id: str
    answer_id: str
    decision: ReviewStatus
    decision_saved: bool
    review_record_id: str | None = None
    review_round_id: UUID | None = None
    idempotency_degraded: bool = False
    resume_status: Literal["succeeded", "pending", "failed"] = "pending"
    resume_error_code: str | None = None
    workflow_status: WorkflowStatus
    pending_review_count: int = 0
    resumable: bool = False
    exam_result_persisted: bool = False
    diagnosis_error_code: str | None = None
    message: str


def _as_uuid(value: Any, label: str) -> UUID:
    """校验并规范化 UUID；非法值显式失败。"""

    if isinstance(value, UUID):
        return value
    text = str(value or "").strip()
    try:
        return UUID(text)
    except ValueError as error:
        raise ReviewAnswerNotFoundError(f"{label} 必须是 UUID，收到 {value!r}。") from error


def _answer_text(content: Any) -> str:
    """把答卷内容渲染为原文文本；结构类型按可读形式展开，不丢字段。"""

    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        return "\n".join(str(item) for item in content)
    if isinstance(content, Mapping):
        return "\n".join(f"{key}：{value}" for key, value in content.items())
    return str(content)


class ReviewQueryService:
    """复核读模型：队列与单题详情（只读，含课程归属校验）。

    :param session: 单会话用法（请求作用域或测试）；与 ``session_factory`` 二选一。
    :param session_factory: 自建会话用法（UI 或后台）；不复用请求作用域会话。
    """

    def __init__(
        self,
        *,
        session: Session | None = None,
        session_factory: Any | None = None,
    ) -> None:
        if session is None and session_factory is None:
            raise ValueError("必须提供 session 或 session_factory。")
        self._session = session
        self._session_factory = session_factory

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

    # ------------------------------------------------------------ 队列

    def list_queue(
        self,
        teacher_id: str,
        *,
        course_id: str | None = None,
        exam_id: str | None = None,
        student_id: str | None = None,
        review_status: ReviewStatus | str | None = ReviewStatus.PENDING_REVIEW,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> ReviewQueuePageDTO:
        """按教师课程范围列出待复核评分；默认只列出待人工复核。"""

        if limit < 1 or limit > MAX_PAGE_SIZE or offset < 0:
            raise ReviewInvalidPageError(
                f"分页参数非法：limit 必须在 1～{MAX_PAGE_SIZE} 之间且 offset 不为负。"
            )
        status = _review_status(review_status)
        try:
            with self._use_session() as session:
                conditions = [Course.created_by == _as_uuid(teacher_id, "教师标识")]
                if course_id is not None:
                    conditions.append(Course.id == _as_uuid(course_id, "课程标识"))
                if exam_id is not None:
                    conditions.append(Exam.id == _as_uuid(exam_id, "考试标识"))
                if student_id is not None:
                    conditions.append(Submission.student_id == _as_uuid(student_id, "学生标识"))
                if status is not None:
                    conditions.append(GradingResult.review_status == status)
                base = self._queue_query().where(*conditions)
                total = session.scalar(
                    select(func.count()).select_from(base.subquery())
                )
                rows = session.execute(
                    base.order_by(
                        Submission.submitted_at.desc(),
                        Answer.created_at,
                        GradingResult.id,
                    )
                    .limit(limit)
                    .offset(offset)
                ).all()
                orders = _question_orders(session, {row[3].id for row in rows})
        except SQLAlchemyError as error:
            raise ReviewQueryError(
                "复核队列读取失败：结果存储未就绪。",
                source_code=type(error).__name__,
            ) from error
        return ReviewQueuePageDTO(
            total=int(total or 0),
            limit=limit,
            offset=offset,
            items=[self._queue_item(row, orders) for row in rows],
        )

    @staticmethod
    def _queue_query() -> Any:
        """队列基查询：结果 → 答卷 → 考试 → 课程 → 答案 → 题目 → 学生。"""

        return (
            select(GradingResult, Answer, Submission, Exam, Course, Question, User)
            .join(Answer, GradingResult.answer_id == Answer.id)
            .join(Submission, GradingResult.submission_id == Submission.id)
            .join(Exam, Submission.exam_id == Exam.id)
            .join(Course, Exam.course_id == Course.id)
            .join(Question, Answer.question_id == Question.id)
            .join(User, Submission.student_id == User.id)
        )

    def _queue_item(
        self,
        row: Any,
        orders: Mapping[tuple[UUID, UUID], int] | None = None,
    ) -> ReviewQueueItemDTO:
        """把查询行映射为队列条目；不做任何推导或补全。"""

        result, answer, submission, exam, course, question, student = row
        resolved_orders = orders or {}
        return ReviewQueueItemDTO(
            submission_id=str(submission.id),
            answer_id=str(answer.id),
            course_id=str(course.id),
            exam_id=str(exam.id),
            exam_title=exam.title,
            student_id=str(student.id),
            student_name=student.username,
            question_id=str(question.id),
            question_number=resolved_orders.get((exam.id, question.id), 0),
            question_type=result.question_type,
            max_score=result.max_score,
            score=result.score,
            confidence=float(result.confidence),
            review_status=result.review_status,
            pending_review_round_id=result.pending_review_round_id,
            requires_review=bool(
                result.decision_requires_review
                if result.decision_requires_review is not None
                else result.review_status is ReviewStatus.PENDING_REVIEW
            ),
            updated_at=result.updated_at,
        )

    # ------------------------------------------------------------ 详情

    def get_answer_detail(
        self,
        teacher_id: str,
        submission_id: str,
        answer_id: str,
    ) -> ReviewDetailDTO:
        """读取单题复核详情；跨课程或跨答卷一律 404，不泄露其他课程事实。"""

        submission_uuid = _as_uuid(submission_id, "答卷标识")
        answer_uuid = _as_uuid(answer_id, "答案标识")
        try:
            with self._use_session() as session:
                row = session.execute(
                    self._queue_query()
                    .where(GradingResult.submission_id == submission_uuid)
                    .where(GradingResult.answer_id == answer_uuid)
                ).all()
                if not row:
                    raise ReviewAnswerNotFoundError("该答卷下没有该题的评分结果。")
                result, answer, submission, exam, course, question, student = row[0]
                self._ensure_course_owner(course, teacher_id)
                evidence, unresolved, out_of_course = self._load_evidence(
                    session, course_id=course.id, chunk_ids=result.retrieved_context_ids
                )
                records = list(
                    session.scalars(
                        select(ReviewRecord)
                        .where(ReviewRecord.grading_result_id == result.id)
                        .order_by(ReviewRecord.created_at, ReviewRecord.id)
                    )
                )
                # 复核详情只认本执行器（M4 工作流）的运行：M3 后台任务行没有 LangGraph 线程，
                # 不能拿来拼装恢复身份（H05）。
                run = session.scalars(
                    select(WorkflowRun)
                    .where(WorkflowRun.submission_id == submission.id)
                    .where(run_kind_criteria())
                    .order_by(WorkflowRun.created_at.desc(), WorkflowRun.id)
                    .limit(1)
                ).first()
                orders = _question_orders(session, {exam.id})
        except SQLAlchemyError as error:
            raise ReviewQueryError(
                "复核详情读取失败：结果存储未就绪。",
                source_code=type(error).__name__,
            ) from error
        thread_id = checkpoint_thread_id(run) if run is not None else None
        return ReviewDetailDTO(
            submission_id=str(submission.id),
            answer_id=str(answer.id),
            course_id=str(course.id),
            exam_id=str(exam.id),
            exam_title=exam.title,
            student_id=str(student.id),
            student_name=student.username,
            question_id=str(question.id),
            question_number=orders.get((exam.id, question.id), 0),
            question_type=result.question_type,
            question_content=question.content,
            reference_answer=question.reference_answer,
            scoring_rubric=question.scoring_rubric,
            max_score=question.score,
            student_answer=_answer_text(answer.content),
            score=result.score,
            reason=result.reason,
            knowledge_points=list(result.knowledge_points or []),
            missing_knowledge_points=list(result.missing_knowledge_points or []),
            correct_points=list(result.correct_points or []),
            suggestions=list(result.suggestions or []),
            confidence=float(result.confidence),
            validation_status=result.validation_status.value,
            review_status=result.review_status,
            pending_review_round_id=result.pending_review_round_id,
            requires_review=bool(
                result.decision_requires_review
                if result.decision_requires_review is not None
                else result.review_status is ReviewStatus.PENDING_REVIEW
            ),
            retrieved_context_ids=list(result.retrieved_context_ids or []),
            evidence=evidence,
            unresolved_evidence_count=unresolved,
            out_of_course_evidence_count=out_of_course,
            workflow_id=run.workflow_id if run is not None else None,
            thread_id=thread_id,
            workflow_status=run.status if run is not None else None,
            resumable=bool(
                run is not None
                and run.resumable
                and thread_id is not None
                and runtime_has_checkpoint(run.checkpoint, thread_id)
            ),
            review_records=[self._record_dto(record) for record in records],
            updated_at=result.updated_at,
        )

    @staticmethod
    def _ensure_course_owner(course: Course, teacher_id: str) -> None:
        """教师必须拥有该门课程。"""

        if str(course.created_by) != str(teacher_id):
            raise ReviewQueryPermissionError("无权访问该答卷所属课程。")

    def find_recorded_decision(
        self, teacher_id: str, decision: TeacherReviewDecision,
        expected_review_round_id: UUID | None = None,
    ) -> ReviewRecordDTO | None:
        """授权详情读取后，核验最新记录是否仍代表本次请求的决定。"""

        try:
            with self._use_session() as session:
                run = session.scalars(
                    select(WorkflowRun).where(
                        WorkflowRun.workflow_id == decision.workflow_id,
                        run_kind_criteria(),
                    )
                ).one_or_none()
                if run is None:
                    return None
                record = find_matching_review_record(
                    session, run=run, decision=decision, actor_id=teacher_id,
                    expected_review_round_id=expected_review_round_id,
                )
                return self._record_dto(record) if record is not None else None
        except SQLAlchemyError as error:
            raise ReviewQueryError(
                "复核记录读取失败：结果存储未就绪。",
                source_code=type(error).__name__,
            ) from error

    def count_pending_reviews(self, teacher_id: str, submission_id: str) -> int:
        """按授权答卷当前持久化评分状态计数，不使用恢复前的图快照。"""

        try:
            with self._use_session() as session:
                pending = self._queue_query().where(
                    Course.created_by == _as_uuid(teacher_id, "教师标识"),
                    GradingResult.submission_id == _as_uuid(submission_id, "答卷标识"),
                    GradingResult.review_status == ReviewStatus.PENDING_REVIEW,
                )
                return int(session.scalar(select(func.count()).select_from(pending.subquery())) or 0)
        except SQLAlchemyError as error:
            raise ReviewQueryError(
                "待复核数量读取失败：结果存储未就绪。",
                source_code=type(error).__name__,
            ) from error

    @staticmethod
    def _record_dto(record: ReviewRecord) -> ReviewRecordDTO:
        """把复核记录映射为审计 DTO。"""

        return ReviewRecordDTO(
            review_record_id=str(record.id),
            reviewer_id=str(record.reviewer_id),
            decision=record.decision,
            review_round_id=record.review_round_id,
            original_score=record.original_score,
            original_reason=record.original_reason,
            original_knowledge_points=list(record.original_knowledge_points or []),
            final_score=record.final_score,
            final_reason=record.final_reason,
            comment=record.comment,
            created_at=record.created_at,
        )

    @staticmethod
    def _load_evidence(
        session: Session,
        *,
        course_id: UUID,
        chunk_ids: Sequence[str],
    ) -> tuple[list[ReviewEvidenceDTO], int, int]:
        """按课程加载检索依据：本课程片段返回正文，其他课程与不可解析引用只计数。"""

        normalized: list[UUID] = []
        unresolved = 0
        for chunk_id in chunk_ids or ():
            try:
                normalized.append(_as_uuid(chunk_id, "片段标识"))
            except ReviewQueryError:
                unresolved += 1
        if not normalized:
            return [], unresolved, 0
        rows = list(
            session.execute(
                select(DocumentChunk, Document.original_filename)
                .join(Document, DocumentChunk.document_id == Document.id)
                .where(DocumentChunk.id.in_(normalized))
            )
        )
        evidence: list[ReviewEvidenceDTO] = []
        out_of_course = 0
        for chunk, filename in rows:
            if chunk.course_id != course_id:
                out_of_course += 1
                continue
            evidence.append(
                ReviewEvidenceDTO(
                    chunk_id=str(chunk.id),
                    course_id=str(chunk.course_id),
                    document_id=str(chunk.document_id),
                    source_file=filename,
                    chunk_index=chunk.chunk_index,
                    content=chunk.content,
                )
            )
        return evidence, unresolved, out_of_course


def _question_orders(
    session: Session,
    exam_ids: set[UUID],
) -> dict[tuple[UUID, UUID], int]:
    """按考试题目关系的权威顺序计算 1 基题号。

    与 T060 快照一致：顺序取 ``Exam.questions`` 关系顺序，不另建第二套排序规则。
    """

    orders: dict[tuple[UUID, UUID], int] = {}
    for exam_id in exam_ids:
        exam = session.get(Exam, exam_id)
        if exam is None:
            continue
        for index, question in enumerate(list(exam.questions), start=1):
            orders[(exam_id, question.id)] = index
    return orders


def _review_status(value: ReviewStatus | str | None) -> ReviewStatus | None:
    """规范化复核状态过滤值；空值表示不过滤。"""

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
    raise ReviewInvalidPageError(f"未知的复核状态过滤值：{value!r}。")


class ReviewDecisionService:
    """教师决策提交：权威详情 + T074 固定契约 + 部分成功回执。"""

    def __init__(
        self,
        *,
        query: ReviewQueryService,
        review_service: ReviewService | Any | None = None,
        review_service_provider: Any | None = None,
        settings: AppSettings | None = None,
    ) -> None:
        self._query = query
        self._review_service = review_service
        self._review_service_provider = review_service_provider
        self._settings = settings

    def _require_review_service(self) -> ReviewService:
        """获取 T074 复核服务；未接线时显式报未就绪。"""

        if self._review_service is not None:
            return self._review_service
        provider = self._review_service_provider
        if provider is None:
            raise ReviewDecisionNotReadyError("复核服务未接线：无法提交教师结论。")
        service = provider()
        if service is None:
            raise ReviewDecisionNotReadyError("复核服务未接线：无法提交教师结论。")
        return service

    async def submit(
        self,
        *,
        teacher_id: str,
        payload: TeacherDecisionRequest,
    ) -> ReviewDecisionOutcomeDTO:
        """提交确认或修改；重复提交按既有记录幂等返回。"""

        if payload.action not in DECISION_ACTIONS:
            raise ReviewDecisionUnsupportedError(
                f"本批不支持“{payload.action}”决策，请使用确认或修改。"
            )
        detail = self._query.get_answer_detail(
            teacher_id, str(payload.submission_id), str(payload.answer_id)
        )
        workflow_id, thread_id, workflow_status = self._resolve_run(detail, payload)
        if payload.action == "modify":
            revised = self._build_revision(detail, payload)
            decision_status = ReviewStatus.MODIFIED
        else:
            revised = None
            decision_status = ReviewStatus.CONFIRMED
        expected_status = payload.expected_review_status or ReviewStatus.PENDING_REVIEW
        if expected_status is not ReviewStatus.PENDING_REVIEW:
            raise ReviewDecisionStaleError(
                "教师决定必须基于 Pending Review 状态，请刷新后重试。"
            )
        expected_round = payload.expected_review_round_id
        degraded = expected_round is None
        if (
            detail.review_status is ReviewStatus.PENDING_REVIEW
            and expected_round is not None
            and detail.pending_review_round_id != expected_round
        ):
            raise ReviewStaleRoundError("复核轮次已变化，请刷新待复核详情后重试。")
        decision = _teacher_decision(
            workflow_id=workflow_id,
            thread_id=thread_id,
            answer_id=detail.answer_id,
            decision_status=decision_status,
            revised=revised,
            expected_review_status=expected_status.value,
        )
        duplicate = (
            self._query.find_recorded_decision(teacher_id, decision, expected_round)
            if detail.review_status is not ReviewStatus.PENDING_REVIEW
            else None
        )
        if duplicate is not None:
            return ReviewDecisionOutcomeDTO(
                workflow_id=workflow_id,
                submission_id=detail.submission_id,
                answer_id=detail.answer_id,
                decision=decision_status,
                decision_saved=True,
                review_record_id=duplicate.review_record_id,
                review_round_id=duplicate.review_round_id,
                idempotency_degraded=degraded,
                resume_status=(
                    "succeeded" if workflow_status is WorkflowStatus.COMPLETED else "pending"
                ),
                workflow_status=workflow_status,
                pending_review_count=self._query.count_pending_reviews(
                    teacher_id, detail.submission_id
                ),
                resumable=detail.resumable,
                message="该结论此前已保存，本次未重复写入；可用运行恢复入口继续。",
            )
        if detail.review_status is not ReviewStatus.PENDING_REVIEW:
            raise ReviewDecisionStaleError(
                f"该题当前复核状态为“{detail.review_status.value}”，无法再次提交教师结论。"
            )
        service = self._require_review_service()
        outcome = await service.submit_decision_async(
            decision,
            actor_id=str(teacher_id),
            actor_role=UserRole.TEACHER,
            comment=payload.comment,
            expected_review_round_id=expected_round,
        )
        if not isinstance(outcome, ReviewOutcome):  # pragma: no cover - 契约保护
            raise ReviewDecisionNotReadyError("复核服务返回了非预期的决策结果。")
        return ReviewDecisionOutcomeDTO(
            workflow_id=outcome.workflow_id,
            submission_id=outcome.submission_id,
            answer_id=outcome.answer_id,
            decision=outcome.decision,
            decision_saved=True,
            review_record_id=outcome.review_record_id,
            review_round_id=outcome.review_round_id,
            idempotency_degraded=degraded,
            resume_status=_resume_status(outcome),
            resume_error_code=outcome.resume_error_code,
            workflow_status=outcome.workflow_status,
            pending_review_count=self._query.count_pending_reviews(
                teacher_id, outcome.submission_id
            ),
            resumable=outcome.resumable,
            exam_result_persisted=outcome.exam_result_persisted,
            diagnosis_error_code=outcome.diagnosis_error_code,
            message=(
                "教师结论已生效，原工作流已恢复。"
                if outcome.resumed
                else "教师结论已保存，但恢复未完成；可在复核队列继续该题或稍后重试恢复。"
            ),
        )

    def _resolve_run(
        self,
        detail: ReviewDetailDTO,
        payload: TeacherDecisionRequest,
    ) -> tuple[str, str, WorkflowStatus]:
        """确定运行身份：一律取数据库事实，客户端声明只作一致性断言。"""

        if detail.workflow_id is None or detail.thread_id is None:
            raise ReviewDecisionWorkflowError(
                "该答卷还没有绑定阅卷运行，无法提交教师结论。"
            )
        declared = (payload.workflow_id or "").strip()
        if declared and declared != detail.workflow_id:
            raise ReviewDecisionWorkflowError(
                "请求声明的运行与该答卷的实际运行不一致，拒绝跨工作流提交。",
                error_code=REVIEW_DECISION_WORKFLOW_MISMATCH,
            )
        return (
            detail.workflow_id,
            detail.thread_id,
            detail.workflow_status or WorkflowStatus.PAUSED,
        )

    @staticmethod
    def _build_revision(
        detail: ReviewDetailDTO,
        payload: TeacherDecisionRequest,
    ) -> GradingResultPayload:
        """按白名单构造修订结果：只替换分数与理由，其余取数据库权威原结果。"""

        if payload.score is None or not (payload.reason or "").strip():
            raise ReviewDecisionRevisionRequiredError("修改评分必须给出新的分数与理由。")
        try:
            score = Decimal(str(payload.score))
            max_score = Decimal(str(detail.max_score))
        except (InvalidOperation, ValueError) as error:
            raise ReviewDecisionScoreError("修订分数必须是有效数值。") from error
        if score < 0 or score > max_score:
            raise ReviewDecisionScoreError(
                f"修订分数必须在 0～{max_score} 之间。"
            )
        return GradingResultPayload(
            question_type=detail.question_type,
            score=float(score),
            max_score=float(max_score),
            reason=str(payload.reason),
            correct_points=list(detail.correct_points),
            missing_knowledge_points=list(detail.missing_knowledge_points),
            knowledge_points=list(detail.knowledge_points),
            suggestions=list(detail.suggestions),
            confidence=detail.confidence if detail.confidence is not None else 0.0,
            validation_status=detail.validation_status or "Validated",
            review_status=ReviewStatus.MODIFIED.value,
            retrieved_context_ids=list(detail.retrieved_context_ids),
            answer_id=detail.answer_id,
            submission_id=detail.submission_id,
        )


def _teacher_decision(
    *,
    workflow_id: str,
    thread_id: str,
    answer_id: str,
    decision_status: ReviewStatus,
    revised: GradingResultPayload | None,
    expected_review_status: str | None,
) -> TeacherReviewDecision:
    """按 T074 固定契约构造教师决策载荷。"""

    return TeacherReviewDecision(
        workflow_id=workflow_id,
        thread_id=thread_id,
        answer_id=answer_id,
        review_status=decision_status.value,
        revised_result=revised,
        expected_review_status=expected_review_status,
    )


def _resume_status(outcome: ReviewOutcome) -> Literal["succeeded", "pending", "failed"]:
    """把恢复结果映射为回执状态；失败与待继续都可区分。"""

    if outcome.resumed:
        return "succeeded"
    return "failed" if outcome.resume_error_code else "pending"


# ---------------------------------------------------------------------- 生产装配


def build_production_review_query_service() -> ReviewQueryService:
    """构造生产复核读模型（每次调用自建会话）。"""

    return ReviewQueryService(session_factory=get_session_factory())


def get_review_query_service(request: Request) -> ReviewQueryService:
    """装配复核读模型；测试可通过 ``app.state.review_query_service`` 注入替身。"""

    configured = getattr(request.app.state, "review_query_service", None)
    if configured is not None:
        return configured
    return build_production_review_query_service()


def get_review_decision_service(request: Request) -> ReviewDecisionService:
    """装配决策服务；测试可通过 ``app.state.review_decision_service`` 注入替身。"""

    configured = getattr(request.app.state, "review_decision_service", None)
    if configured is not None:
        return configured
    return ReviewDecisionService(
        query=get_review_query_service(request),
        review_service_provider=build_production_review_service,
    )


ReviewQueryDependency = Annotated[ReviewQueryService, Depends(get_review_query_service)]
ReviewDecisionDependency = Annotated[
    ReviewDecisionService, Depends(get_review_decision_service)
]
ReviewTeacher = Annotated[
    User,
    Depends(require_permission(Permission.REVIEW_LOW_CONFIDENCE_GRADING)),
]


def _review_http_exception(error: BaseException) -> HTTPException:
    """把业务错误映射为脱敏的 HTTP 响应。"""

    if isinstance(error, ReviewQueryError):
        code = error.error_code
        detail = error.detail
        retryable = bool(error.retryable)
        source_code = error.source_code
    elif isinstance(error, (ReviewServiceError, WorkflowCheckpointError)):
        code = error.error_code
        detail = error.detail
        retryable = bool(getattr(error, "retryable", False))
        source_code = getattr(error, "source_code", None)
    else:  # pragma: no cover - 未预期错误保持脱敏
        code = REVIEW_DECISION_SERVICE_NOT_READY
        detail = "复核操作失败。"
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


# ---------------------------------------------------------------------- 端点


@router.get(
    "/queue",
    response_model=ReviewQueuePageDTO,
    summary="查询待复核队列",
)
def list_review_queue(
    teacher: ReviewTeacher,
    service: ReviewQueryDependency,
    course_id: UUID | None = None,
    exam_id: UUID | None = None,
    student_id: UUID | None = None,
    review_status: ReviewStatus | None = ReviewStatus.PENDING_REVIEW,
    limit: int = DEFAULT_PAGE_SIZE,
    offset: int = 0,
) -> ReviewQueuePageDTO:
    """列出当前教师课程范围内的复核队列（默认待人工复核）。"""

    try:
        return service.list_queue(
            str(teacher.id),
            course_id=str(course_id) if course_id is not None else None,
            exam_id=str(exam_id) if exam_id is not None else None,
            student_id=str(student_id) if student_id is not None else None,
            review_status=review_status,
            limit=limit,
            offset=offset,
        )
    except ReviewQueryError as error:
        raise _review_http_exception(error) from None


@router.get(
    "/queue/{submission_id}/answers/{answer_id}",
    response_model=ReviewDetailDTO,
    summary="查询单题复核详情",
)
def get_review_detail(
    submission_id: str,
    answer_id: str,
    teacher: ReviewTeacher,
    service: ReviewQueryDependency,
) -> ReviewDetailDTO:
    """返回学生答案原文、AI 评分、题目依据、检索依据与运行身份。"""

    try:
        return service.get_answer_detail(str(teacher.id), submission_id, answer_id)
    except ReviewQueryError as error:
        raise _review_http_exception(error) from None


@router.post(
    "/decisions",
    response_model=ReviewDecisionOutcomeDTO,
    summary="提交教师决策（确认 / 修改）",
)
async def submit_teacher_decision(
    payload: TeacherDecisionRequest,
    teacher: ReviewTeacher,
    service: ReviewDecisionDependency,
) -> ReviewDecisionOutcomeDTO:
    """提交教师结论；决定已保存而恢复未完成时以部分成功回执返回。"""

    try:
        return await service.submit(teacher_id=str(teacher.id), payload=payload)
    except ReviewQueryError as error:
        raise _review_http_exception(error) from None
    except (ReviewServiceError, WorkflowCheckpointError) as error:
        raise _review_http_exception(error) from None


__all__ = [
    "DECISION_ACTIONS",
    "DEFAULT_PAGE_SIZE",
    "ReviewAnswerNotFoundError",
    "ReviewDecisionNotReadyError",
    "ReviewDecisionOutcomeDTO",
    "ReviewDecisionRevisionRequiredError",
    "ReviewDecisionScoreError",
    "ReviewDecisionService",
    "ReviewDecisionStaleError",
    "ReviewDecisionUnsupportedError",
    "ReviewDecisionWorkflowError",
    "ReviewDetailDTO",
    "ReviewEvidenceDTO",
    "ReviewInvalidPageError",
    "ReviewQueryError",
    "ReviewQueryPermissionError",
    "ReviewQueryService",
    "ReviewQueueItemDTO",
    "ReviewQueuePageDTO",
    "ReviewRecordDTO",
    "TeacherDecisionRequest",
    "build_production_review_query_service",
    "get_review_decision_service",
    "get_review_query_service",
    "router",
]
