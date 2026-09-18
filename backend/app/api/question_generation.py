"""T075 AI 出题 API：候选题生成、候选查询与教师审核。

契约依据：T075（AI 出题请求与候选题目审核操作）、plan.md §4.1（AI 出题完整链路）、
FR-024～FR-028、T067 Question Agent、T068 Question Validator。

两阶段状态机（本模块的核心边界）：

1. **生成端（系统）**：教师条件 → T067 ``QuestionAgent.generate`` → T068
   ``validate_candidates`` → DTO 映射 ``Question`` → **单事务批量写入**。校验通过的候选题存
   ``Pending Review``，校验不通过的存 ``Needs Revision``；Provider/结构化输出整体失败时
   **不创建半批数据**，写库失败整批回滚。
2. **教师审核端**：只处理已持久化为 ``Pending Review`` 的候选题，且以 T068
   ``plan_transition(actor=teacher)`` 为唯一转换判定；禁止 ``Candidate Generation → Approved``
   和任何自动 ``Published``。

已知边界（不静默丢弃、不伪造）：

- T067 的 ``QuestionCandidate.source_context_ids`` 与生成使用的检索片段在题库模型中没有对应
  列（本批不新增迁移），因此只在生成响应中显式返回，并声明 ``sources_persisted=False``。
- 教师退回修订意见同样没有持久列，响应以 ``comment_persisted=False`` 明确说明，不声称已保存。
- ``request_id`` 从认证请求头 ``X-Request-ID`` 取得，缺失时由服务端生成并回显。

错误语义：401 未认证（认证中间件）、403 无权限或跨课程、404 候选题不存在、409 状态冲突或
陈旧决策、422 输入非法或依赖上下文不足、503 依赖未就绪（Provider/Embedding/检索/结果存储）。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.ai.agents.question_agent import (
    QUESTION_CANDIDATE_COUNT_MISMATCH,
    QUESTION_EMBEDDING_PROVIDER_FAILED,
    QUESTION_EMBEDDING_PROVIDER_NOT_READY,
    QUESTION_INSUFFICIENT_CONTEXT,
    QUESTION_INVALID_INPUT,
    QUESTION_INVALID_LLM_RESPONSE,
    QUESTION_MISSING_GENERATION_REQUEST,
    QUESTION_NOT_CANDIDATE_STATUS,
    QUESTION_PROVIDER_FAILED,
    QUESTION_PROVIDER_NOT_READY,
    QUESTION_RETRIEVAL_DATABASE_FAILED,
    QUESTION_RETRIEVAL_FAILED,
    QUESTION_RETRIEVAL_UNSUPPORTED_DIALECT,
    QUESTION_TYPE_MISMATCH,
    QUESTION_UNKNOWN_SOURCE_CONTEXT,
    QuestionAgent,
)
from backend.app.ai.agents.state import (
    AgentInput,
    AgentOutput,
    AgentStatus,
    AgentType,
    QuestionGenerationRequest,
)
from backend.app.core.config import AppSettings
from backend.app.core.database import get_session_factory
from backend.app.core.security import require_permission
from backend.app.domain.enums import QuestionStatus, QuestionType
from backend.app.domain.permissions import Permission
from backend.app.models import Course, Document, DocumentChunk, Question, User
from backend.app.schemas.ai import QuestionCandidate
from backend.app.services.question_validator import (
    CANDIDATE_GENERATION_STATUS,
    QUESTION_ANSWER_ENCODING_INCOMPATIBLE,
    QUESTION_AUTOMATIC_PUBLISH_BLOCKED,
    QUESTION_DIFFICULTY_MISMATCH,
    QUESTION_EMPTY_CANDIDATE_BATCH,
    QUESTION_KNOWLEDGE_POINT_UNCOVERED,
    QUESTION_MISSING_COURSE_EVIDENCE,
    QUESTION_NOT_CANDIDATE,
    QUESTION_RUBRIC_UNUSABLE,
    QUESTION_SCORE_NOT_STORABLE,
    QUESTION_STATUS_TRANSITION_BLOCKED,
    QUESTION_UNKNOWN_COURSE_EVIDENCE,
    CandidateBatchValidation,
    CandidateValidationResult,
    QuestionValidationIssue,
    QuestionValidator,
    ValidationActor,
    plan_transition,
)

router = APIRouter(prefix="/api/question-generation", tags=["AI 出题"])

#: 生成响应中候选来源标识；题库模型无来源列，因此该标识只出现在响应载荷中。
GENERATION_ORIGIN: str = CANDIDATE_GENERATION_STATUS

#: 一次请求允许的候选数量上限，避免单次生成拖垮请求与上下文预算。
MAX_CANDIDATES_PER_REQUEST: int = 20

#: 候选题列表的默认审核状态范围（AI 出题工作台只关心候选与审核状态）。
CANDIDATE_STATUS_SCOPE: tuple[QuestionStatus, ...] = (
    QuestionStatus.CANDIDATE_GENERATION,
    QuestionStatus.PENDING_REVIEW,
    QuestionStatus.NEEDS_REVISION,
)

#: 候选题默认分页大小与上限。
DEFAULT_PAGE_SIZE: int = 50
MAX_PAGE_SIZE: int = 200

#: API 层错误码：只在题库模型与 Agent/Validator 契约之外补充“接口语义”错误。
QUESTION_GENERATION_FAILED: str = "QUESTION_GENERATION_FAILED"
QUESTION_CANDIDATE_NOT_FOUND: str = "QUESTION_CANDIDATE_NOT_FOUND"
QUESTION_CANDIDATE_NOT_PENDING_REVIEW: str = "QUESTION_CANDIDATE_NOT_PENDING_REVIEW"
QUESTION_CANDIDATE_STALE: str = "QUESTION_CANDIDATE_STALE"
QUESTION_CANDIDATE_STORE_NOT_READY: str = "QUESTION_CANDIDATE_STORE_NOT_READY"
QUESTION_CANDIDATE_PERMISSION_DENIED: str = "QUESTION_CANDIDATE_PERMISSION_DENIED"
QUESTION_REVISION_COMMENT_REQUIRED: str = "QUESTION_REVISION_COMMENT_REQUIRED"
QUESTION_GENERATION_INVALID_PAGE: str = "QUESTION_GENERATION_INVALID_PAGE"

#: 错误码到 HTTP 状态码的映射；未列出的错误按 500 处理并保持脱敏。
_ERROR_STATUS: dict[str, int] = {
    # 依赖未就绪：Provider、Embedding、检索存储与候选写入存储
    QUESTION_PROVIDER_NOT_READY: 503,
    QUESTION_EMBEDDING_PROVIDER_NOT_READY: 503,
    QUESTION_EMBEDDING_PROVIDER_FAILED: 503,
    QUESTION_RETRIEVAL_UNSUPPORTED_DIALECT: 503,
    QUESTION_RETRIEVAL_DATABASE_FAILED: 503,
    QUESTION_CANDIDATE_STORE_NOT_READY: 503,
    # 外部依赖调用失败：可重试的网关类失败
    QUESTION_PROVIDER_FAILED: 502,
    QUESTION_RETRIEVAL_FAILED: 502,
    # 输入与上下文问题
    QUESTION_INVALID_INPUT: 422,
    QUESTION_MISSING_GENERATION_REQUEST: 422,
    QUESTION_INSUFFICIENT_CONTEXT: 422,
    QUESTION_UNKNOWN_SOURCE_CONTEXT: 422,
    QUESTION_CANDIDATE_COUNT_MISMATCH: 422,
    QUESTION_TYPE_MISMATCH: 422,
    QUESTION_INVALID_LLM_RESPONSE: 422,
    QUESTION_EMPTY_CANDIDATE_BATCH: 422,
    QUESTION_ANSWER_ENCODING_INCOMPATIBLE: 422,
    QUESTION_MISSING_COURSE_EVIDENCE: 422,
    QUESTION_UNKNOWN_COURSE_EVIDENCE: 422,
    QUESTION_RUBRIC_UNUSABLE: 422,
    QUESTION_SCORE_NOT_STORABLE: 422,
    QUESTION_DIFFICULTY_MISMATCH: 422,
    QUESTION_KNOWLEDGE_POINT_UNCOVERED: 422,
    QUESTION_NOT_CANDIDATE: 422,
    QUESTION_NOT_CANDIDATE_STATUS: 422,
    QUESTION_REVISION_COMMENT_REQUIRED: 422,
    QUESTION_GENERATION_INVALID_PAGE: 422,
    # 状态冲突
    QUESTION_STATUS_TRANSITION_BLOCKED: 409,
    QUESTION_AUTOMATIC_PUBLISH_BLOCKED: 409,
    QUESTION_CANDIDATE_NOT_PENDING_REVIEW: 409,
    QUESTION_CANDIDATE_STALE: 409,
    # 资源与权限
    QUESTION_CANDIDATE_NOT_FOUND: 404,
    QUESTION_CANDIDATE_PERMISSION_DENIED: 403,
}


class QuestionGenerationError(RuntimeError):
    """AI 出题与候选审核的边界错误；保留脱敏错误码与可重试语义。"""

    error_code: str = QUESTION_GENERATION_FAILED
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


class CandidateNotFoundError(QuestionGenerationError):
    """候选题不存在或不属于当前教师的课程。"""

    error_code = QUESTION_CANDIDATE_NOT_FOUND


class CandidatePermissionError(QuestionGenerationError):
    """教师对目标课程没有管理权。"""

    error_code = QUESTION_CANDIDATE_PERMISSION_DENIED


class CandidateNotPendingReviewError(QuestionGenerationError):
    """只有已持久化为 ``Pending Review`` 的候选题才允许教师审核。"""

    error_code = QUESTION_CANDIDATE_NOT_PENDING_REVIEW


class CandidateStaleError(QuestionGenerationError):
    """预检基准与数据库当前状态不一致，拒绝陈旧覆盖。"""

    error_code = QUESTION_CANDIDATE_STALE


class CandidateStoreNotReadyError(QuestionGenerationError):
    """候选题写入存储未就绪或写入失败（整批未落库）。"""

    error_code = QUESTION_CANDIDATE_STORE_NOT_READY


class RevisionCommentRequiredError(QuestionGenerationError):
    """退回修订必须给出修订意见。"""

    error_code = QUESTION_REVISION_COMMENT_REQUIRED


class GenerationFailedError(QuestionGenerationError):
    """T067/T068 上报的生成或校验失败；原样保留上游错误码。"""


class InvalidPageError(QuestionGenerationError):
    """分页参数非法。"""

    error_code = QUESTION_GENERATION_INVALID_PAGE


# ---------------------------------------------------------------------- 请求/响应 DTO


class CandidateGenerationRequest(BaseModel):
    """教师出题条件（FR-024）。"""

    model_config = ConfigDict(extra="forbid")

    course_id: UUID = Field(description="出题所属课程。")
    knowledge_points: list[str] = Field(
        default_factory=list,
        max_length=32,
        description="要求覆盖的知识点。",
    )
    difficulty: str | None = Field(default=None, max_length=160, description="难度要求。")
    question_type: QuestionType | None = Field(default=None, description="题型要求。")
    count: int = Field(
        default=1,
        ge=1,
        le=MAX_CANDIDATES_PER_REQUEST,
        description="候选题目数量。",
    )

    @field_validator("knowledge_points", mode="before")
    @classmethod
    def normalize_knowledge_points(cls, value: Any) -> list[str]:
        """清理知识点并去重，拒绝空字符串。"""

        if value is None:
            return []
        if isinstance(value, str) or not isinstance(value, list):
            raise TypeError("知识点必须是字符串列表。")
        normalized: list[str] = []
        for point in value:
            if not isinstance(point, str) or not point.strip():
                raise ValueError("知识点必须是非空字符串。")
            item = point.strip()
            if item not in normalized:
                normalized.append(item)
        return normalized

    @field_validator("difficulty", mode="before")
    @classmethod
    def normalize_difficulty(cls, value: Any) -> str | None:
        """清理可选难度。"""

        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("难度必须是文本。")
        return value.strip() or None


class ValidationIssueDTO(BaseModel):
    """一条校验问题；直接透传 T068 的脱敏原因码、说明与字段名。"""

    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    field: str | None = None


class CandidateValidationDTO(BaseModel):
    """候选题（或整批）的校验结论。"""

    model_config = ConfigDict(frozen=True)

    status: QuestionStatus = Field(description="建议审核状态。")
    issues: list[ValidationIssueDTO] = Field(default_factory=list)


class CandidateDTO(BaseModel):
    """候选题的持久化事实；字段与题库模型一一对应，不做推导。"""

    model_config = ConfigDict(frozen=True)

    candidate_id: str
    course_id: str
    question_type: QuestionType
    content: str
    options: list[str] | None = None
    reference_answer: str | None = None
    scoring_rubric: str | None = None
    difficulty: str | None = None
    knowledge_points: list[str] = Field(default_factory=list)
    score: Decimal
    status: QuestionStatus
    created_at: datetime
    updated_at: datetime


class GeneratedCandidateDTO(CandidateDTO):
    """生成响应中的候选题：额外携带来源标识、校验结论与本次生成依据。"""

    origin: str = GENERATION_ORIGIN
    validation: CandidateValidationDTO
    source_context_ids: list[str] = Field(default_factory=list)
    sources_persisted: bool = False


class GenerationEvidenceDTO(BaseModel):
    """生成使用的检索片段；只返回按课程校验通过的真实片段。"""

    model_config = ConfigDict(frozen=True)

    chunk_id: str
    course_id: str | None = None
    document_id: str | None = None
    source_file: str | None = None
    chunk_index: int | None = None
    content: str


class CandidateGenerationResponse(BaseModel):
    """一次生成的完整回执：候选题、整批校验与合作事实。"""

    request_id: str
    course_id: str
    generated_count: int
    candidates: list[GeneratedCandidateDTO]
    batch_validation: CandidateValidationDTO
    evidence: list[GenerationEvidenceDTO] = Field(default_factory=list)
    out_of_course_source_count: int = 0
    unresolved_source_count: int = 0
    sources_persisted: bool = False
    model: str | None = None
    prompt_version: str | None = None


class CandidatePageDTO(BaseModel):
    """候选题分页结果；``total`` 与过滤条件一致。"""

    total: int
    limit: int
    offset: int
    items: list[CandidateDTO]


class CandidateReviewRequest(BaseModel):
    """教师审核动作；客户端的 ``expected_status`` 只作为陈旧断言，不改变状态机。"""

    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "request_revision"] = Field(
        description="审核通过或退回修订。"
    )
    comment: str | None = Field(default=None, max_length=2000, description="修订意见。")
    expected_status: QuestionStatus | None = Field(
        default=None,
        description="期望的当前候选状态；不匹配即拒绝，防止陈旧覆盖。",
    )

    @field_validator("comment", mode="before")
    @classmethod
    def normalize_comment(cls, value: Any) -> str | None:
        """清理可选修订意见。"""

        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("修订意见必须是文本。")
        return value.strip() or None


class CandidateReviewOutcomeDTO(BaseModel):
    """审核结果；``comment_persisted`` 明确说明修订意见是否落库。"""

    candidate_id: str
    decision: Literal["approve", "request_revision"]
    previous_status: QuestionStatus
    status: QuestionStatus
    comment: str | None = None
    comment_persisted: bool = False
    request_id: str


# ---------------------------------------------------------------------- 编排服务


def _validation_dto(issues: Sequence[QuestionValidationIssue]) -> list[ValidationIssueDTO]:
    """把校验问题转换为响应 DTO（保序、不改写说明）。"""

    return [
        ValidationIssueDTO(code=issue.code, message=issue.message, field=issue.field)
        for issue in issues
    ]


def _candidate_dto(question: Question) -> CandidateDTO:
    """把题库行映射为候选题 DTO；不做任何推导或补全。"""

    return CandidateDTO(
        candidate_id=str(question.id),
        course_id=str(question.course_id),
        question_type=question.type,
        content=question.content,
        options=question.options if isinstance(question.options, list) else None,
        reference_answer=question.reference_answer,
        scoring_rubric=question.scoring_rubric,
        difficulty=question.difficulty,
        knowledge_points=list(question.knowledge_points or []),
        score=question.score,
        status=question.status,
        created_at=question.created_at,
        updated_at=question.updated_at,
    )


def _generated_dto(
    question: Question,
    result: CandidateValidationResult,
    source_context_ids: Sequence[str],
) -> GeneratedCandidateDTO:
    """生成响应用 DTO：携带来源标识、校验结论与本次依据（均未落库）。"""

    base = _candidate_dto(question)
    return GeneratedCandidateDTO(
        **base.model_dump(),
        validation=CandidateValidationDTO(
            status=result.status,
            issues=_validation_dto(result.issues),
        ),
        source_context_ids=list(source_context_ids),
        sources_persisted=False,
    )


class QuestionGenerationService:
    """AI 出题编排（生成端）与候选题教师审核（教师端）。

    :param session_factory: 会话工厂；生成端在调用 Provider 前后使用**两个**会话，
        Provider 调用结束后（``Agent`` 返回前）不留写事务，随后用一个新事务整批写入候选题。
    :param session: 借入会话（请求作用域或测试）；与 ``session_factory`` 二选一。
    :param agent: T067 Question Agent；``None`` 时按默认构造（Provider 按调用期配置解析，
        未配置时显式报 ``QUESTION_PROVIDER_NOT_READY``）。
    :param validator: T068 Question Validator；``None`` 时按默认构造。
    :param retriever: 检索组件覆盖；``None`` 时按调用期配置解析。
    :param embedding_provider: Embedding 组件覆盖；``None`` 时按调用期配置解析。
    :param settings: 生成期配置覆盖；``None`` 时使用全局设置。
    """

    def __init__(
        self,
        *,
        session_factory: Any | None = None,
        session: Session | None = None,
        agent: QuestionAgent | Any | None = None,
        validator: QuestionValidator | None = None,
        retriever: Any | None = None,
        embedding_provider: Any | None = None,
        settings: AppSettings | None = None,
    ) -> None:
        if session is None and session_factory is None:
            raise ValueError("必须提供 session 或 session_factory。")
        self._session_factory = session_factory
        self._session = session
        self._agent = agent if agent is not None else QuestionAgent()
        self._validator = validator if validator is not None else QuestionValidator()
        self._retriever = retriever
        self._embedding_provider = embedding_provider
        self._settings = settings

    # ------------------------------------------------------------ 依赖解析

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

    # ------------------------------------------------------------ 生成端

    async def generate_candidates(
        self,
        *,
        course_id: str,
        actor_id: str,
        request_id: str,
        knowledge_points: Sequence[str] = (),
        difficulty: str | None = None,
        question_type: QuestionType | None = None,
        count: int = 1,
    ) -> CandidateGenerationResponse:
        """按教师条件生成候选题、自动校验并整批落库。"""

        request = self._build_request(
            course_id=course_id,
            knowledge_points=knowledge_points,
            difficulty=difficulty,
            question_type=question_type,
            count=count,
        )
        agent_input = AgentInput(
            agent_type=AgentType.QUESTION,
            request_id=request_id,
            user_id=actor_id,
            generation_request=request,
        )
        candidate_course_id = self._require_owned_course(course_id, actor_id)
        # 读会话只用于课程校验与检索；Provider 调用结束后立即关闭，写事务另开。
        with self._use_session() as session:
            output = await self._agent.generate(
                session,
                agent_input,
                retriever=self._retriever,
                embedding_provider=self._embedding_provider,
                settings=self._settings,
            )
        candidates = self._require_generation_output(output)
        batch = self._validator.validate_candidates(
            candidates,
            retrieved_context_ids=output.retrieved_context_ids,
            generation_request=request,
        )
        rows = self._persist_candidates(
            course_id=candidate_course_id,
            actor_id=actor_id,
            candidates=candidates,
            batch=batch,
        )
        evidence, out_of_course, unresolved = self._load_evidence(
            course_id=candidate_course_id,
            chunk_ids=output.retrieved_context_ids,
        )
        return CandidateGenerationResponse(
            request_id=request_id,
            course_id=str(candidate_course_id),
            generated_count=len(rows),
            candidates=[
                _generated_dto(
                    question,
                    result,
                    candidates[index].source_context_ids,
                )
                for index, (question, result) in enumerate(
                    zip(rows, batch.results, strict=True)
                )
            ],
            batch_validation=CandidateValidationDTO(
                status=batch.status,
                issues=_validation_dto(batch.issues),
            ),
            evidence=evidence,
            out_of_course_source_count=out_of_course,
            unresolved_source_count=unresolved,
            sources_persisted=False,
            model=output.model,
            prompt_version=output.prompt_version,
        )

    def _build_request(
        self,
        *,
        course_id: str,
        knowledge_points: Sequence[str],
        difficulty: str | None,
        question_type: QuestionType | None,
        count: int,
    ) -> QuestionGenerationRequest:
        """构造 T065 出题条件；非法值由 Pydantic 显式拒绝。"""

        try:
            return QuestionGenerationRequest(
                course_id=str(course_id),
                knowledge_points=[str(point) for point in knowledge_points],
                difficulty=difficulty,
                question_type=question_type,
                count=count,
            )
        except ValueError as error:
            raise GenerationFailedError(
                "出题条件不合法，无法生成候选题目。",
                error_code=QUESTION_INVALID_INPUT,
                source_code=type(error).__name__,
            ) from error

    def _require_generation_output(
        self,
        output: AgentOutput,
    ) -> list[QuestionCandidate]:
        """校验生成输出；失败或空批显式拒绝，不产生半批数据。"""

        if output.status is not AgentStatus.SUCCESS:
            error = output.error
            raise GenerationFailedError(
                error.message if error is not None else "AI 出题失败。",
                error_code=(
                    error.error_code if error is not None else QUESTION_GENERATION_FAILED
                ),
                retryable=bool(error.retryable) if error is not None else False,
                source_code=error.source_code if error is not None else None,
            )
        candidates = list(output.question_candidates)
        if not candidates:
            raise GenerationFailedError(
                "生成结果为空批次，未创建任何候选题。",
                error_code=QUESTION_EMPTY_CANDIDATE_BATCH,
            )
        return candidates

    def _persist_candidates(
        self,
        *,
        course_id: UUID,
        actor_id: str,
        candidates: Sequence[QuestionCandidate],
        batch: CandidateBatchValidation,
    ) -> list[Question]:
        """在单事务内整批写入候选题：``Candidate Generation`` → 校验结论状态。"""

        if len(batch.results) != len(candidates):
            raise CandidateStoreNotReadyError(
                "校验结果与候选题数量不一致，拒绝写入不完整批次。"
            )
        created: list[Question] = []
        creator_id = _as_uuid(actor_id)
        with self._use_session() as session:
            try:
                for candidate, result in zip(candidates, batch.results, strict=True):
                    target = self._validated_candidate_status(result)
                    question = Question(
                        course_id=course_id,
                        type=candidate.question_type,
                        content=candidate.content,
                        options=list(candidate.options) if candidate.options else None,
                        reference_answer=candidate.reference_answer,
                        scoring_rubric=candidate.scoring_rubric,
                        difficulty=candidate.difficulty,
                        knowledge_points=list(candidate.knowledge_points),
                        score=Decimal(str(candidate.score)),
                        status=target,
                        created_by=creator_id,
                    )
                    session.add(question)
                    created.append(question)
                session.commit()
            except QuestionGenerationError:
                session.rollback()
                raise
            except (SQLAlchemyError, ValueError, ArithmeticError) as error:
                session.rollback()
                raise CandidateStoreNotReadyError(
                    "候选题写入失败，本批次未落库。",
                    source_code=type(error).__name__,
                ) from error
            for question in created:
                session.refresh(question)
        return created

    @staticmethod
    def _validated_candidate_status(result: CandidateValidationResult) -> QuestionStatus:
        """以 T068 状态机判定候选（``Candidate Generation`` → 校验结论）的落库状态。"""

        return plan_transition(
            QuestionStatus.CANDIDATE_GENERATION,
            result.status,
            actor=ValidationActor.VALIDATOR,
        )

    def _load_evidence(
        self,
        *,
        course_id: UUID,
        chunk_ids: Sequence[str],
    ) -> tuple[list[GenerationEvidenceDTO], int, int]:
        """按课程范围加载检索依据：属于本课程的片段才返回，其余分类计数。

        - 同 ``course_id`` 的片段：返回真实正文与来源资料名；
        - 属于其他课程的片段：计入 ``out_of_course``，**不**返回正文；
        - 无法解析为片段标识的引用：计入 ``unresolved``，不猜测来源。
        """

        normalized: list[UUID] = []
        unresolved = 0
        for chunk_id in chunk_ids:
            try:
                normalized.append(_as_uuid(str(chunk_id)))
            except ValueError:
                unresolved += 1
        if not normalized:
            return [], 0, unresolved
        evidence: list[GenerationEvidenceDTO] = []
        out_of_course = 0
        with self._use_session() as session:
            rows = list(
                session.execute(
                    select(DocumentChunk, Document.original_filename).join(
                        Document, DocumentChunk.document_id == Document.id
                    ).where(DocumentChunk.id.in_(normalized))
                )
            )
        for chunk, filename in rows:
            if chunk.course_id != course_id:
                out_of_course += 1
                continue
            evidence.append(
                GenerationEvidenceDTO(
                    chunk_id=str(chunk.id),
                    course_id=str(chunk.course_id),
                    document_id=str(chunk.document_id),
                    source_file=filename,
                    chunk_index=chunk.chunk_index,
                    content=chunk.content,
                )
            )
        return evidence, out_of_course, unresolved

    # ------------------------------------------------------------ 教师端

    def list_candidates(
        self,
        *,
        actor_id: str,
        course_id: str | None = None,
        candidate_status: QuestionStatus | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> CandidatePageDTO:
        """按课程与状态列出候选题；不指定状态时只列出候选审核范围内的题目。"""

        if limit < 1 or limit > MAX_PAGE_SIZE or offset < 0:
            raise InvalidPageError(
                f"分页参数非法：limit 必须在 1～{MAX_PAGE_SIZE} 之间且 offset 不为负。"
            )
        with self._use_session() as session:
            statuses = (
                (candidate_status,) if candidate_status is not None else CANDIDATE_STATUS_SCOPE
            )
            owned_course_id = (
                self._require_owned_course(course_id, actor_id)
                if course_id is not None
                else None
            )
            if owned_course_id is not None:
                conditions = (
                    Question.course_id == owned_course_id,
                    Question.status.in_(statuses),
                )
            else:
                conditions = (
                    Question.course_id.in_(
                        select(Course.id).where(Course.created_by == _as_uuid(actor_id))
                    ),
                    Question.status.in_(statuses),
                )
            total = session.scalar(
                select(func.count()).select_from(Question).where(*conditions)
            )
            rows = list(
                session.scalars(
                    select(Question)
                    .where(*conditions)
                    .order_by(Question.created_at.desc(), Question.id)
                    .limit(limit)
                    .offset(offset)
                )
            )
            return CandidatePageDTO(
                total=int(total or 0),
                limit=limit,
                offset=offset,
                items=[_candidate_dto(row) for row in rows],
            )

    def get_candidate(self, *, actor_id: str, candidate_id: str) -> CandidateDTO:
        """读取单个候选题；不存在或跨课程一律 404，不泄露其他课程的题目。"""

        with self._use_session() as session:
            question = self._require_candidate(session, candidate_id)
            self._ensure_candidate_access(session, question, actor_id)
            return _candidate_dto(question)

    def submit_review(
        self,
        *,
        actor_id: str,
        candidate_id: str,
        action: Literal["approve", "request_revision"],
        comment: str | None = None,
        expected_status: QuestionStatus | None = None,
    ) -> CandidateReviewOutcomeDTO:
        """教师审核已持久化的候选题：``Pending Review`` → ``Approved``/``Needs Revision``。"""

        if action == "request_revision" and not (comment or "").strip():
            raise RevisionCommentRequiredError("退回修订必须给出修订意见。")
        target = (
            QuestionStatus.APPROVED
            if action == "approve"
            else QuestionStatus.NEEDS_REVISION
        )
        with self._use_session() as session:
            question = self._require_candidate(session, candidate_id)
            self._ensure_candidate_access(session, question, actor_id)
            current = question.status
            if current is not QuestionStatus.PENDING_REVIEW:
                raise CandidateNotPendingReviewError(
                    f"候选题当前状态为“{current.value}”，只有“待审核”的候选题才能审核。"
                )
            if expected_status is not None and expected_status is not current:
                raise CandidateStaleError(
                    f"候选题状态已变为“{current.value}”，请刷新后重新审核。"
                )
            # T068 状态机是唯一转换判定；自动发布与越权状态在此被显式拒绝。
            plan_transition(current, target, actor=ValidationActor.TEACHER)
            try:
                updated = cast(
                    CursorResult[Any],
                    session.execute(
                        update(Question)
                        .where(Question.id == question.id)
                        .where(Question.status == QuestionStatus.PENDING_REVIEW)
                        .values(status=target)
                    ),
                )
                if updated.rowcount != 1:
                    session.rollback()
                    raise CandidateStaleError(
                        "候选题状态已被其他请求修改，本次审核未生效。"
                    )
                session.commit()
            except QuestionGenerationError:
                raise
            except SQLAlchemyError as error:
                session.rollback()
                raise CandidateStoreNotReadyError(
                    "审核状态写入失败，候选题状态保持不变。",
                    source_code=type(error).__name__,
                ) from error
        return CandidateReviewOutcomeDTO(
            candidate_id=candidate_id,
            decision=action,
            previous_status=current,
            status=target,
            comment=comment,
            comment_persisted=False,
            request_id="",
        )

    # ------------------------------------------------------------ 校验与授权

    def _require_owned_course(self, course_id: str, actor_id: str) -> UUID:
        """确认课程存在且属于当前教师；否则 404/403。"""

        try:
            normalized = _as_uuid(course_id)
        except ValueError as error:
            raise CandidateNotFoundError("课程标识不合法。") from error
        with self._use_session() as session:
            course = session.get(Course, normalized)
            if course is None:
                raise CandidateNotFoundError("课程不存在。")
            if course.created_by != _as_uuid(actor_id):
                raise CandidatePermissionError("无权对该课程出题。")
        return normalized

    @staticmethod
    def _require_candidate(session: Session, candidate_id: str) -> Question:
        """加载候选题；非 UUID 或不存在一律 404。"""

        try:
            normalized = _as_uuid(candidate_id)
        except ValueError as error:
            raise CandidateNotFoundError("候选题标识不合法。") from error
        question = session.get(Question, normalized)
        if question is None:
            raise CandidateNotFoundError("候选题不存在。")
        return question

    @staticmethod
    def _ensure_candidate_access(
        session: Session,
        question: Question,
        actor_id: str,
    ) -> None:
        """候选题必须属于当前教师的课程。"""

        course = session.get(Course, question.course_id)
        if course is None:
            raise CandidateNotFoundError("候选题所属课程不存在。")
        if course.created_by != _as_uuid(actor_id):
            raise CandidateNotFoundError("候选题不存在。")


def _as_uuid(value: str) -> UUID:
    """校验并规范化 UUID；非法值显式失败。"""

    if isinstance(value, UUID):
        return value
    text = str(value).strip()
    try:
        return UUID(text)
    except ValueError as error:
        raise ValueError(f"标识必须是 UUID，收到 {value!r}。") from error


def build_production_question_generation_service(
    settings: AppSettings | None = None,
    *,
    retriever: Any | None = None,
    embedding_provider: Any | None = None,
) -> QuestionGenerationService:
    """构造生产出题服务：数据库会话工厂 + T067 Agent + T068 Validator。

    T067 Agent 的 Provider 与 Embedding 在调用期按配置解析；未配置时 Agent 显式返回
    ``QUESTION_PROVIDER_NOT_READY``/``QUESTION_EMBEDDING_PROVIDER_NOT_READY``，
    接口据此返回 503，不产生任何候选题。``retriever``/``embedding_provider`` 只用于显式替换
    外部检索组件（例如评测或测试替身），默认仍按配置解析。
    """

    return QuestionGenerationService(
        session_factory=get_session_factory(),
        agent=QuestionAgent(),
        validator=QuestionValidator(),
        retriever=retriever,
        embedding_provider=embedding_provider,
        settings=settings,
    )


def get_question_generation_service(request: Request) -> QuestionGenerationService:
    """装配出题服务；测试可通过 ``app.state.question_generation_service`` 注入替身。"""

    configured = getattr(request.app.state, "question_generation_service", None)
    if configured is not None:
        return configured
    return build_production_question_generation_service()


QuestionGenerationServiceDependency = Annotated[
    QuestionGenerationService,
    Depends(get_question_generation_service),
]
CandidateGenerator = Annotated[
    User,
    Depends(require_permission(Permission.GENERATE_QUESTION_CANDIDATES)),
]
CandidateReviewer = Annotated[
    User,
    Depends(require_permission(Permission.REVIEW_QUESTIONS)),
]


def _generation_http_exception(error: QuestionGenerationError) -> HTTPException:
    """把业务错误映射为脱敏的 HTTP 响应。"""

    return HTTPException(
        status_code=_ERROR_STATUS.get(error.error_code, 500),
        detail={
            "error_code": error.error_code,
            "message": error.detail,
            "retryable": bool(error.retryable),
            "source_code": error.source_code,
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
    "/candidates",
    response_model=CandidateGenerationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="按教师条件生成 AI 候选题目",
)
async def generate_candidates(
    payload: CandidateGenerationRequest,
    teacher: CandidateGenerator,
    service: QuestionGenerationServiceDependency,
    x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> CandidateGenerationResponse:
    """生成候选题目并整批落库；检索上下文不足时返回 422，不编造题目。"""

    request_id = _resolve_request_id(x_request_id)
    try:
        return await service.generate_candidates(
            course_id=str(payload.course_id),
            actor_id=str(teacher.id),
            request_id=request_id,
            knowledge_points=payload.knowledge_points,
            difficulty=payload.difficulty,
            question_type=payload.question_type,
            count=payload.count,
        )
    except QuestionGenerationError as error:
        raise _generation_http_exception(error) from None


@router.get(
    "/candidates",
    response_model=CandidatePageDTO,
    summary="查询候选题列表",
)
def list_candidates(
    teacher: CandidateReviewer,
    service: QuestionGenerationServiceDependency,
    course_id: UUID | None = None,
    candidate_status: QuestionStatus | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    offset: int = 0,
) -> CandidatePageDTO:
    """列出当前教师课程范围内的候选题，支持状态过滤与稳定分页。"""

    try:
        return service.list_candidates(
            actor_id=str(teacher.id),
            course_id=str(course_id) if course_id is not None else None,
            candidate_status=candidate_status,
            limit=limit,
            offset=offset,
        )
    except QuestionGenerationError as error:
        raise _generation_http_exception(error) from None


@router.get(
    "/candidates/{candidate_id}",
    response_model=CandidateDTO,
    summary="查询单个候选题",
)
def get_candidate(
    candidate_id: str,
    teacher: CandidateReviewer,
    service: QuestionGenerationServiceDependency,
) -> CandidateDTO:
    """读取单个候选题；跨课程访问返回 404，不泄露其他课程题目。"""

    try:
        return service.get_candidate(actor_id=str(teacher.id), candidate_id=candidate_id)
    except QuestionGenerationError as error:
        raise _generation_http_exception(error) from None


@router.post(
    "/candidates/{candidate_id}/review",
    response_model=CandidateReviewOutcomeDTO,
    summary="提交教师审核决策（通过 / 退回修订）",
)
def review_candidate(
    candidate_id: str,
    payload: CandidateReviewRequest,
    teacher: CandidateReviewer,
    service: QuestionGenerationServiceDependency,
    x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> CandidateReviewOutcomeDTO:
    """审核通过或退回修订；状态转换由 T068 状态机判定。"""

    try:
        outcome = service.submit_review(
            actor_id=str(teacher.id),
            candidate_id=candidate_id,
            action=payload.action,
            comment=payload.comment,
            expected_status=payload.expected_status,
        )
    except QuestionGenerationError as error:
        raise _generation_http_exception(error) from None
    return outcome.model_copy(update={"request_id": _resolve_request_id(x_request_id)})


__all__ = [
    "CANDIDATE_STATUS_SCOPE",
    "GENERATION_ORIGIN",
    "MAX_CANDIDATES_PER_REQUEST",
    "CandidateDTO",
    "CandidateGenerationRequest",
    "CandidateGenerationResponse",
    "CandidateNotFoundError",
    "CandidateNotPendingReviewError",
    "CandidatePageDTO",
    "CandidatePermissionError",
    "CandidateReviewOutcomeDTO",
    "CandidateReviewRequest",
    "CandidateStaleError",
    "CandidateStoreNotReadyError",
    "CandidateValidationDTO",
    "GeneratedCandidateDTO",
    "GenerationEvidenceDTO",
    "GenerationFailedError",
    "InvalidPageError",
    "QuestionGenerationError",
    "QuestionGenerationService",
    "RevisionCommentRequiredError",
    "ValidationIssueDTO",
    "build_production_question_generation_service",
    "get_question_generation_service",
    "router",
]
