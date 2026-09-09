"""学生考试、答题保存和答卷提交 API。"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from backend.app.core.database import get_db
from backend.app.core.security import require_permission
from backend.app.domain.enums import QuestionType, SubmissionStatus
from backend.app.domain.permissions import Permission
from backend.app.models import Exam, User
from backend.app.services.submission_service import (
    AnswerContent,
    AnswerSummary,
    AvailableExamSummary,
    SubmissionConflictError,
    SubmissionNotFoundError,
    SubmissionPermissionError,
    SubmissionService,
    SubmissionServiceError,
    SubmissionSummary,
    SubmissionValidationError,
)

router = APIRouter(prefix="/api/submissions", tags=["学生答卷"])
exam_submission_router = APIRouter(prefix="/api/exams", tags=["学生答卷"])


class StudentQuestionSummary(BaseModel):
    """学生作答时可见的题目内容，不包含参考答案和评分标准。"""

    model_config = ConfigDict(frozen=True)

    id: str
    type: QuestionType
    content: str
    options: dict[str, Any] | list[Any] | None = None
    difficulty: str | None = None
    knowledge_points: list[str] = Field(default_factory=list)
    score: Decimal
    position: int


class StudentExamDetail(AvailableExamSummary):
    """学生打开考试时看到的安全考试详情。"""

    questions: list[StudentQuestionSummary] = Field(default_factory=list)
    submission: SubmissionSummary | None = None


AnswerPayload = str | list[str] | dict[str, str] | None
AnswerCollectionPayload = list["AnswerRequest"] | dict[str, AnswerPayload]


def _normalize_answer_content(value: Any) -> AnswerContent:
    """校验 API 答案内容，并保留题型所需的字符串列表或对象结构。"""

    if value is None:
        return None
    if isinstance(value, str):
        if "\x00" in value:
            raise ValueError("答案包含无效字符。")
        return value.strip()
    if isinstance(value, list):
        if not all(isinstance(item, str) for item in value):
            raise ValueError("答案列表必须只包含字符串。")
        if any("\x00" in item for item in value):
            raise ValueError("答案包含无效字符。")
        return [item.strip() for item in value]
    if isinstance(value, dict):
        if not all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in value.items()
        ):
            raise ValueError("答案对象的键和值必须是字符串。")
        if any("\x00" in key or "\x00" in item for key, item in value.items()):
            raise ValueError("答案包含无效字符。")
        return {key.strip(): item.strip() for key, item in value.items()}
    raise ValueError("答案必须是字符串、字符串列表、字符串对象或空值。")


class AnswerRequest(BaseModel):
    """保存单道题答案时使用的请求条目。"""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )

    question_id: UUID = Field(
        validation_alias=AliasChoices("question_id", "question"),
        description="题目标识。",
    )
    content: AnswerPayload = Field(
        default=None,
        validation_alias=AliasChoices(
            "content",
            "answer",
            "value",
            "answer_content",
        ),
        description="答案内容。",
    )

    @field_validator("content", mode="before")
    @classmethod
    def normalize_content(cls, value: Any) -> AnswerContent:
        """清理答案文本并限制 JSON 结构。"""

        return _normalize_answer_content(value)


class AnswersRequest(BaseModel):
    """批量保存答案时使用的请求体。"""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )

    answers: list[AnswerRequest] = Field(
        min_length=1,
        validation_alias=AliasChoices("answers", "items", "answer_items"),
        description="答案条目列表，也兼容题目标识到答案的对象映射。",
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_answer_collection(cls, value: Any) -> Any:
        """把对象映射或直接列表统一为答案条目列表。"""

        if isinstance(value, list):
            return {"answers": value}
        if not isinstance(value, Mapping):
            return value
        if any(key in value for key in ("answers", "items", "answer_items")):
            raw = value.get("answers", value.get("items", value.get("answer_items")))
            if isinstance(raw, Mapping):
                return {
                    "answers": [
                        {"question_id": question_id, "content": content}
                        for question_id, content in raw.items()
                    ]
                }
            return {**value, "answers": raw}
        return {
            "answers": [
                {"question_id": question_id, "content": content}
                for question_id, content in value.items()
            ]
        }


class SubmissionCreateRequest(BaseModel):
    """开始或继续一场考试时使用的请求体。"""

    model_config = ConfigDict(extra="forbid")

    exam_id: UUID = Field(description="考试标识。")
    answers: AnswerCollectionPayload | None = Field(
        default=None,
        description="可选的初始答案。",
    )
    initialize_answers: bool = Field(
        default=True,
        description="是否为考试全部题目创建草稿答案占位记录。",
    )

    @field_validator("answers", mode="before")
    @classmethod
    def normalize_answers(cls, value: Any) -> Any:
        """校验并保留批量答案的列表或对象形式。"""

        if value is None:
            return None
        if isinstance(value, Mapping):
            return {
                str(question_id): _normalize_answer_content(content)
                for question_id, content in value.items()
            }
        if isinstance(value, list):
            return value
        raise ValueError("答案列表格式无效。")


class SubmissionStartRequest(BaseModel):
    """通过考试路径开始答卷时使用的请求体。"""

    model_config = ConfigDict(extra="forbid")

    answers: AnswerCollectionPayload | None = None
    initialize_answers: bool = True

    @field_validator("answers", mode="before")
    @classmethod
    def normalize_answers(cls, value: Any) -> Any:
        """校验开始答卷时的可选答案。"""

        if value is None:
            return None
        if isinstance(value, Mapping):
            return {
                str(question_id): _normalize_answer_content(content)
                for question_id, content in value.items()
            }
        if isinstance(value, list):
            return value
        raise ValueError("答案列表格式无效。")


class SubmissionSubmitRequest(BaseModel):
    """提交答卷时使用的可选请求体。"""

    model_config = ConfigDict(extra="forbid")

    answers: AnswerCollectionPayload | None = None

    @field_validator("answers", mode="before")
    @classmethod
    def normalize_answers(cls, value: Any) -> Any:
        """校验提交时附带的答案。"""

        if value is None:
            return None
        if isinstance(value, Mapping):
            return {
                str(question_id): _normalize_answer_content(content)
                for question_id, content in value.items()
            }
        if isinstance(value, list):
            return value
        raise ValueError("答案列表格式无效。")


class ExamSubmitRequest(SubmissionSubmitRequest):
    """通过考试路径提交答卷时使用的请求体。"""

    submission_id: UUID | None = Field(default=None, description="已有答卷标识。")


# 兼容不同调用方对请求类型的命名。
SubmissionStartPayload = SubmissionStartRequest
SubmissionAnswerRequest = AnswerRequest
SubmissionAnswersRequest = AnswersRequest
SubmissionSubmitPayload = SubmissionSubmitRequest


def get_submission_service(
    session: Annotated[Session, Depends(get_db)],
) -> SubmissionService:
    """创建使用当前请求数据库会话的答卷服务。"""

    return SubmissionService(session)


SubmissionServiceDependency = Annotated[
    SubmissionService,
    Depends(get_submission_service),
]
StudentExamViewer = Annotated[
    User,
    Depends(require_permission(Permission.VIEW_AVAILABLE_EXAMS)),
]
StudentExamTaker = Annotated[
    User,
    Depends(require_permission(Permission.TAKE_EXAMS)),
]
StudentExamSubmitter = Annotated[
    User,
    Depends(require_permission(Permission.SUBMIT_EXAMS)),
]
StudentSubmissionViewer = Annotated[
    User,
    Depends(require_permission(Permission.VIEW_OWN_RESULTS)),
]


def _submission_http_exception(error: BaseException) -> HTTPException:
    """将答卷服务异常转换为统一的中文 HTTP 错误。"""

    if isinstance(error, SubmissionNotFoundError):
        code = status.HTTP_404_NOT_FOUND
    elif isinstance(error, SubmissionPermissionError):
        code = status.HTTP_403_FORBIDDEN
    elif isinstance(error, SubmissionConflictError):
        code = status.HTTP_409_CONFLICT
    elif isinstance(error, (SubmissionValidationError, ValueError)):
        code = status.HTTP_422_UNPROCESSABLE_ENTITY
    else:
        code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HTTPException(
        status_code=code,
        detail=str(error) or "答卷操作失败，请稍后重试。",
    )


def _service_answers(
    answers: AnswerCollectionPayload | None,
) -> list[dict[str, Any]] | dict[str, AnswerContent] | None:
    """把请求 DTO 转成答卷服务支持的答案集合。"""

    if answers is None:
        return None
    if isinstance(answers, Mapping):
        return {
            str(question_id): _normalize_answer_content(content)
            for question_id, content in answers.items()
        }
    return [
        {
            "question_id": item.question_id,
            "content": item.content,
        }
        for item in answers
    ]


def _load_student_exam_detail(
    service: SubmissionService,
    exam_id: UUID,
    student_id: UUID,
) -> StudentExamDetail:
    """加载已开放考试的题目内容，并过滤学生不可见的答案字段。"""

    available = service.get_available_exam(
        exam_id,
        student_id=student_id,
    )
    try:
        exam = service.session.scalar(
            select(Exam).options(selectinload(Exam.questions)).where(Exam.id == exam_id)
        )
    except SQLAlchemyError as exc:
        raise SubmissionServiceError("无法读取考试题目。") from exc
    if exam is None:
        raise SubmissionNotFoundError("考试不存在。")

    questions = [
        StudentQuestionSummary(
            id=str(question.id),
            type=question.type,
            content=question.content,
            options=question.options,
            difficulty=question.difficulty,
            knowledge_points=list(question.knowledge_points or []),
            score=question.score,
            position=position,
        )
        for position, question in enumerate(exam.questions, start=1)
    ]
    submission = service.find_submission(exam_id, student_id)
    return StudentExamDetail(
        **available.model_dump(),
        questions=questions,
        submission=submission,
    )


def _start_submission(
    service: SubmissionService,
    exam_id: UUID,
    student: User,
    *,
    answers: AnswerCollectionPayload | None,
    initialize_answers: bool,
    response: Response | None = None,
) -> SubmissionSummary:
    """统一处理开始答卷，并为草稿重复打开返回成功状态。"""

    existing = service.find_submission(exam_id, student.id)
    result = service.create_submission(
        exam_id,
        student_id=student.id,
        answers=_service_answers(answers),
        initialize_answers=initialize_answers,
    )
    if response is not None and existing is not None:
        response.status_code = status.HTTP_200_OK
    return result


@router.get(
    "/exams",
    response_model=list[AvailableExamSummary],
)
@router.get(
    "/available-exams",
    response_model=list[AvailableExamSummary],
)
def list_available_exams(
    student: StudentExamViewer,
    service: SubmissionServiceDependency,
) -> list[AvailableExamSummary]:
    """列出当前学生有资格参加且处于开放时间窗的考试。"""

    try:
        return service.list_available_exams(
            student_id=student.id,
        )
    except SubmissionServiceError as exc:
        raise _submission_http_exception(exc) from None


list_student_exams = list_available_exams


@router.get(
    "/exams/{exam_id}",
    response_model=StudentExamDetail,
)
def get_available_exam(
    exam_id: UUID,
    student: StudentExamViewer,
    service: SubmissionServiceDependency,
) -> StudentExamDetail:
    """读取考试题目和当前学生已有的草稿答卷。"""

    try:
        return _load_student_exam_detail(
            service,
            exam_id,
            student.id,
        )
    except (SubmissionServiceError, SQLAlchemyError, ValueError) as exc:
        raise _submission_http_exception(exc) from None


@router.get(
    "/exams/{exam_id}/submission",
    response_model=SubmissionSummary,
)
def get_exam_submission(
    exam_id: UUID,
    student: StudentSubmissionViewer,
    service: SubmissionServiceDependency,
) -> SubmissionSummary:
    """读取当前学生在指定考试中的答卷。"""

    try:
        return service.get_submission_for_exam(exam_id, student.id)
    except SubmissionServiceError as exc:
        raise _submission_http_exception(exc) from None


@router.post(
    "",
    response_model=SubmissionSummary,
    status_code=status.HTTP_201_CREATED,
)
def create_submission(
    payload: SubmissionCreateRequest,
    student: StudentExamTaker,
    service: SubmissionServiceDependency,
    response: Response,
) -> SubmissionSummary:
    """开始或继续一场考试，并返回草稿答卷。"""

    try:
        return _start_submission(
            service,
            payload.exam_id,
            student,
            answers=payload.answers,
            initialize_answers=payload.initialize_answers,
            response=response,
        )
    except (SubmissionServiceError, ValueError) as exc:
        raise _submission_http_exception(exc) from None


@router.post(
    "/exams/{exam_id}/start",
    response_model=SubmissionSummary,
    status_code=status.HTTP_201_CREATED,
)
def start_exam_submission(
    exam_id: UUID,
    student: StudentExamTaker,
    service: SubmissionServiceDependency,
    response: Response,
    payload: SubmissionStartRequest | None = None,
) -> SubmissionSummary:
    """通过嵌套考试路径开始或继续学生答卷。"""

    request = payload or SubmissionStartRequest()
    try:
        return _start_submission(
            service,
            exam_id,
            student,
            answers=request.answers,
            initialize_answers=request.initialize_answers,
            response=response,
        )
    except (SubmissionServiceError, ValueError) as exc:
        raise _submission_http_exception(exc) from None


@router.get(
    "",
    response_model=list[SubmissionSummary],
)
def list_my_submissions(
    student: StudentSubmissionViewer,
    service: SubmissionServiceDependency,
    exam_id: UUID | None = None,
    submission_status: Annotated[
        SubmissionStatus | None,
        Query(alias="status"),
    ] = None,
) -> list[SubmissionSummary]:
    """列出当前学生自己的答卷摘要。"""

    try:
        return service.list_submissions(
            student_id=student.id,
            exam_id=exam_id,
            status=submission_status,
        )
    except SubmissionServiceError as exc:
        raise _submission_http_exception(exc) from None


@router.get(
    "/{submission_id}/answers",
    response_model=list[AnswerSummary],
)
def list_submission_answers(
    submission_id: UUID,
    student: StudentSubmissionViewer,
    service: SubmissionServiceDependency,
) -> list[AnswerSummary]:
    """读取当前学生答卷中的全部答案。"""

    try:
        return service.list_answers(submission_id, student_id=student.id)
    except SubmissionServiceError as exc:
        raise _submission_http_exception(exc) from None


@router.get(
    "/{submission_id}",
    response_model=SubmissionSummary,
)
def get_submission(
    submission_id: UUID,
    student: StudentSubmissionViewer,
    service: SubmissionServiceDependency,
) -> SubmissionSummary:
    """读取当前学生自己的答卷详情。"""

    try:
        return service.get_submission(submission_id, student_id=student.id)
    except SubmissionServiceError as exc:
        raise _submission_http_exception(exc) from None


@router.post(
    "/{submission_id}/answers",
    response_model=SubmissionSummary,
)
@router.put(
    "/{submission_id}/answers",
    response_model=SubmissionSummary,
)
def save_submission_answers(
    submission_id: UUID,
    payload: AnswersRequest,
    student: StudentExamTaker,
    service: SubmissionServiceDependency,
) -> SubmissionSummary:
    """保存当前学生草稿答卷中的一批答案。"""

    try:
        service.save_answers(
            submission_id,
            _service_answers(payload.answers) or [],
            student_id=student.id,
        )
        return service.get_submission(submission_id, student_id=student.id)
    except (SubmissionServiceError, ValueError) as exc:
        raise _submission_http_exception(exc) from None


@router.post(
    "/{submission_id}/answers/{question_id}",
    response_model=AnswerSummary,
)
@router.put(
    "/{submission_id}/answers/{question_id}",
    response_model=AnswerSummary,
)
def save_submission_answer(
    submission_id: UUID,
    question_id: UUID,
    payload: AnswerRequest,
    student: StudentExamTaker,
    service: SubmissionServiceDependency,
) -> AnswerSummary:
    """保存当前学生草稿答卷中的单道答案。"""

    if payload.question_id != question_id:
        raise HTTPException(
            status_code=422, detail="路径题目标识与请求题目标识不一致。"
        )
    try:
        return service.save_answer(
            submission_id,
            question_id,
            payload.content,
            student_id=student.id,
        )
    except (SubmissionServiceError, ValueError) as exc:
        raise _submission_http_exception(exc) from None


@router.post(
    "/{submission_id}/submit",
    response_model=SubmissionSummary,
)
@router.post(
    "/{submission_id}/complete",
    response_model=SubmissionSummary,
)
def submit_submission(
    submission_id: UUID,
    student: StudentExamSubmitter,
    service: SubmissionServiceDependency,
    payload: SubmissionSubmitRequest | None = None,
) -> SubmissionSummary:
    """校验完整答案并提交答卷，提交后不再允许修改。"""

    try:
        return service.submit_submission(
            submission_id,
            student_id=student.id,
            answers=_service_answers(payload.answers if payload else None),
        )
    except (SubmissionServiceError, ValueError) as exc:
        raise _submission_http_exception(exc) from None


def _submit_exam_by_path(
    exam_id: UUID,
    payload: ExamSubmitRequest | None,
    student: User,
    service: SubmissionService,
) -> SubmissionSummary:
    """兼容冻结文档中的考试提交路径，并保持同一答卷状态规则。"""

    request = payload or ExamSubmitRequest()
    if request.submission_id is not None:
        existing = service.get_submission(request.submission_id, student_id=student.id)
        if existing.exam_id != str(exam_id):
            raise SubmissionValidationError("答卷不属于指定考试。")
        return service.submit_submission(
            request.submission_id,
            student_id=student.id,
            answers=_service_answers(request.answers),
        )

    draft = service.create_submission(
        exam_id,
        student_id=student.id,
        answers=_service_answers(request.answers),
        initialize_answers=True,
    )
    return service.submit_submission(
        draft.id,
        student_id=student.id,
    )


@exam_submission_router.post(
    "/{exam_id}/submit",
    response_model=SubmissionSummary,
)
def submit_exam(
    exam_id: UUID,
    student: StudentExamSubmitter,
    service: SubmissionServiceDependency,
    payload: ExamSubmitRequest | None = None,
) -> SubmissionSummary:
    """通过考试资源路径创建并提交学生答卷。"""

    try:
        return _submit_exam_by_path(exam_id, payload, student, service)
    except (SubmissionServiceError, ValueError) as exc:
        raise _submission_http_exception(exc) from None


@exam_submission_router.post(
    "/{exam_id}/submissions",
    response_model=SubmissionSummary,
    status_code=status.HTTP_201_CREATED,
)
def start_exam_submission_by_path(
    exam_id: UUID,
    student: StudentExamTaker,
    service: SubmissionServiceDependency,
    response: Response,
    payload: SubmissionStartRequest | None = None,
) -> SubmissionSummary:
    """通过考试资源路径开始或继续学生答卷。"""

    request = payload or SubmissionStartRequest()
    try:
        return _start_submission(
            service,
            exam_id,
            student,
            answers=request.answers,
            initialize_answers=request.initialize_answers,
            response=response,
        )
    except (SubmissionServiceError, ValueError) as exc:
        raise _submission_http_exception(exc) from None


__all__ = [
    "AnswerRequest",
    "AnswersRequest",
    "ExamSubmitRequest",
    "StudentExamDetail",
    "StudentQuestionSummary",
    "SubmissionAnswerRequest",
    "SubmissionAnswersRequest",
    "SubmissionCreateRequest",
    "SubmissionServiceDependency",
    "SubmissionStartPayload",
    "SubmissionStartRequest",
    "SubmissionSubmitPayload",
    "SubmissionSubmitRequest",
    "exam_submission_router",
    "get_submission_service",
    "list_available_exams",
    "list_my_submissions",
    "router",
    "save_submission_answer",
    "save_submission_answers",
    "start_exam_submission",
    "start_exam_submission_by_path",
    "submit_exam",
    "submit_submission",
]
