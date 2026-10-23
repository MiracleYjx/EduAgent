"""T079 正式入口闭环集成测试。

测试使用真实 PostgreSQL 隔离 schema、真实 T069/T072/T073/T074、结果仓储与诊断存储。
只替换评分/诊断 LLM Provider、检索、Embedding 与 Reranker 外部边界；不得手工写业务结果表。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from backend.app.ai.agents.grading_agent import GradingAgent
from backend.app.ai.llm.base import BaseLLMProvider, LLMMessage
from backend.app.api.results import ResultsQueryService, get_results_query_service
from backend.app.api.reviews import (
    ReviewDecisionService,
    ReviewQueryService,
    get_review_decision_service,
    get_review_query_service,
)
from backend.app.api.workflow import (
    DiagnosisRecorderAdapter,
    WorkflowService,
    build_review_service,
    get_workflow_service,
)
from backend.app.core.database import get_db
from backend.app.domain.enums import (
    AnswerStatus,
    ExamStatus,
    QuestionStatus,
    QuestionType,
    ReviewStatus,
    SubmissionStatus,
    UserRole,
)
from backend.app.models import (
    Answer,
    Course,
    Exam,
    ExamQuestion,
    Question,
    Role,
    Submission,
    User,
)
from backend.app.services.auth_service import create_access_token
from backend.app.services.diagnosis_service import DiagnosisService
from backend.app.services.grading.diagnosis_report_store import (
    DiagnosisReportStore,
)
from backend.app.services.grading.grading_repository import DatabaseGradingRepository
from backend.app.services.grading.grading_task_service import (
    DatabaseGradingSubmissionReader,
)
from backend.app.services.grading.subjective_grader import SubjectiveGradingPayload
from backend.app.services.workflow_checkpoint import WorkflowCheckpointStore
from tests.postgres_helpers import isolated_postgres_engine
from tests.support.application_lifecycle import bind_startup_session_factory
from tests.support.exam_scoring_fixtures import confirm_synthetic_exam_basis
from tests.support.retained_component_app import component_app as create_app
from tests.support.subjective_grading_doubles import (
    StubEmbeddingProvider,
    StubReranker,
    StubRetriever,
    make_chunk,
)
from tests.unit.settings_helpers import build_test_settings

THRESHOLD = 0.8
FIXED_NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Paper:
    """测试答卷的数据库标识。"""

    teacher_id: str
    student_id: str
    course_id: str
    exam_id: str
    submission_id: str
    objective_answer_id: str
    subjective_answer_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReviewEnv:
    """一次测试使用的隔离 PostgreSQL 环境。"""

    engine: Engine
    paper: Paper


@pytest.fixture
def env() -> Iterator[ReviewEnv]:
    """创建含一道客观题和两道主观题的真实答卷。"""

    with isolated_postgres_engine() as engine, Session(engine) as session:
        yield ReviewEnv(engine=engine, paper=_seed_paper(session, "mixed"))


@pytest.fixture
def solo_env() -> Iterator[ReviewEnv]:
    """创建只有一道主观题的真实答卷，便于验证暂停和恢复。"""

    with isolated_postgres_engine() as engine, Session(engine) as session:
        yield ReviewEnv(engine=engine, paper=_seed_paper(session, "solo", solo=True))


def _seed_paper(session: Session, suffix: str, *, solo: bool = False) -> Paper:
    """只准备输入事实：教师、课程、已发布考试、答卷和答案。"""

    teacher = User(
        username=f"t079-{suffix}-teacher",
        email=f"t079-{suffix}-teacher@example.com",
        password_hash="hashed-password",
    )
    teacher.roles.append(Role(name=UserRole.TEACHER))
    student = User(
        username=f"t079-{suffix}-student",
        email=f"t079-{suffix}-student@example.com",
        password_hash="hashed-password",
    )
    student.roles.append(Role(name=UserRole.STUDENT))
    course = Course(name=f"T079 {suffix} 课程", creator=teacher)
    first = Question(
        course=course,
        creator=teacher,
        type=QuestionType.SINGLE_CHOICE,
        content="下列哪个是不可变类型？",
        options=["list", "tuple"],
        reference_answer="tuple",
        knowledge_points=["数据类型"],
        score=Decimal("10.00"),
        status=QuestionStatus.APPROVED,
    )
    if solo:
        first.type = QuestionType.SHORT_ANSWER
        first.content = "解释变量的作用。"
        first.options = None
        first.reference_answer = "变量用于保存数据。"
        first.scoring_rubric = "说明保存和引用数据即可。"
        first.knowledge_points = ["变量"]
    questions = [first]
    if not solo:
        questions.extend(
            [
                Question(
                    course=course,
                    creator=teacher,
                    type=QuestionType.SHORT_ANSWER,
                    content="解释变量的作用。",
                    reference_answer="变量用于保存数据。",
                    scoring_rubric="说明保存和引用数据即可。",
                    knowledge_points=["变量"],
                    score=Decimal("10.00"),
                    status=QuestionStatus.APPROVED,
                ),
                Question(
                    course=course,
                    creator=teacher,
                    type=QuestionType.SHORT_ANSWER,
                    content="解释函数的作用。",
                    reference_answer="函数用于复用逻辑。",
                    scoring_rubric="说明封装与复用即可。",
                    knowledge_points=["函数"],
                    score=Decimal("10.00"),
                    status=QuestionStatus.APPROVED,
                ),
            ]
        )
    exam = Exam(
        course=course,
        creator=teacher,
        title=f"T079 {suffix} 测验",
        exam_question_links=[
            ExamQuestion(question=question, order_index=index)
            for index, question in enumerate(questions, start=1)
        ],
        status=ExamStatus.PUBLISHED,
    )
    submission = Submission(
        exam=exam,
        student=student,
        status=SubmissionStatus.SUBMITTED,
    )
    answers = [
        Answer(
            submission=submission,
            question=first,
            content="变量用于保存数据。" if solo else "tuple",
            status=AnswerStatus.SUBMITTED,
        )
    ]
    for question in questions[1:]:
        answers.append(
            Answer(
                submission=submission,
                question=question,
                content="变量用于保存数据。",
                status=AnswerStatus.SUBMITTED,
            )
        )
    session.add(submission)
    confirm_synthetic_exam_basis(session, exam)
    session.commit()
    return Paper(
        teacher_id=str(teacher.id),
        student_id=str(student.id),
        course_id=str(course.id),
        exam_id=str(exam.id),
        submission_id=str(submission.id),
        objective_answer_id=str(answers[0].id),
        subjective_answer_ids=tuple(
            str(answer.id)
            for answer, question in zip(answers, questions, strict=True)
            if question.type is QuestionType.SHORT_ANSWER
        ),
    )


class SequenceScoringProvider:
    """按预定顺序返回结构化评分结果的 LLM Provider 替身。"""

    def __init__(self, confidences: Sequence[float], *, score: float = 6.0) -> None:
        self.confidences = list(confidences)
        self.score = score
        self.calls: list[dict[str, Any]] = []

    async def generate_structured(
        self,
        messages: Sequence[Mapping[str, Any]],
        schema: type[SubjectiveGradingPayload],
        **kwargs: Any,
    ) -> SubjectiveGradingPayload:
        self.calls.append({"messages": list(messages), "schema": schema, "kwargs": kwargs})
        confidence = self.confidences.pop(0) if self.confidences else 0.95
        return schema(
            score=self.score,
            confidence=confidence,
            reason="说明了核心概念。",
            correct_points=["核心概念"],
            missing_knowledge_points=["补充例子"],
            suggestions=["补充相关例子。"],
        )


class InvalidStructuredProvider:
    """返回超出满分的结构化载荷，验证平台拒绝未校验结果。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def generate_structured(
        self,
        messages: Sequence[Mapping[str, Any]],
        schema: type[SubjectiveGradingPayload],
        **kwargs: Any,
    ) -> SubjectiveGradingPayload:
        self.calls.append({"messages": list(messages), "schema": schema, "kwargs": kwargs})
        return schema(
            score=99.0,
            confidence=0.95,
            reason="超出满分的无效结果。",
            correct_points=["无效"],
            missing_knowledge_points=[],
            suggestions=["拒绝该结果。"],
        )


class SuggestionProvider(BaseLLMProvider):
    """诊断建议 LLM Provider 替身；诊断计算与持久化仍使用生产实现。"""

    provider_name = "t079-diagnosis"

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def generate_structured(
        self,
        messages: Sequence[LLMMessage],
        schema: type[BaseModel],
        model: str | None = None,
    ) -> BaseModel:
        self.calls.append({"messages": list(messages), "schema": schema, "model": model})
        if self.error is not None:
            raise self.error
        return schema.model_validate({"suggestions": ["继续复习相关知识点。"]})


def _session_factory(env: ReviewEnv) -> Callable[[], Session]:
    """返回每次调用都创建新 Session 的工厂。"""

    return lambda: Session(env.engine)


def _agent(env: ReviewEnv, provider: Any) -> tuple[GradingAgent, Any, Any, Any]:
    """装配真实 GradingAgent，仅替换允许的外部边界。"""

    retriever = StubRetriever(
        [
            make_chunk(
                "t079-context",
                course_id=env.paper.course_id,
                document_id="t079-document",
            )
        ]
    )
    embedding = StubEmbeddingProvider()
    reranker = StubReranker()
    agent = GradingAgent(
        provider=provider,
        retriever=retriever,
        reranker=reranker,
        embedding_provider=embedding,
        settings=build_test_settings(confidence_threshold=THRESHOLD),
        session_factory=_session_factory(env),
    )
    return agent, retriever, embedding, reranker


def _headers(user_id: str, role: UserRole) -> dict[str, str]:
    """签发正式认证依赖可识别的测试令牌。"""

    settings = build_test_settings()
    token = create_access_token(
        UUID(user_id),
        secret_key=settings.JWT_SECRET_KEY,
        roles=[role],
        expires_delta=timedelta(minutes=60),
    )
    return {"Authorization": f"Bearer {token}"}


def _teacher_headers(env: ReviewEnv) -> dict[str, str]:
    return _headers(env.paper.teacher_id, UserRole.TEACHER)


def _student_headers(env: ReviewEnv) -> dict[str, str]:
    return _headers(env.paper.student_id, UserRole.STUDENT)


@dataclass(frozen=True, slots=True)
class ApiHarness:
    """一个独立应用实例；其服务与数据库会话工厂均重新创建。"""

    client: TestClient
    workflow: WorkflowService


@contextmanager
def _api_client(
    env: ReviewEnv,
    *,
    scoring_provider: Any,
    diagnosis_provider: BaseLLMProvider | None = None,
    outcome_session_factory: Callable[[], Session] | None = None,
    raise_server_exceptions: bool = True,
) -> Iterator[ApiHarness]:
    """装配真实 API、Workflow、复核、结果与诊断存储。"""

    sessions = _session_factory(env)
    settings = build_test_settings(confidence_threshold=THRESHOLD)
    checkpoints = WorkflowCheckpointStore(
        session_factory=sessions,
        clock=lambda: FIXED_NOW,
    )
    reader = DatabaseGradingSubmissionReader(session_factory=sessions)
    agent, _, _, _ = _agent(env, scoring_provider)
    diagnosis = DiagnosisRecorderAdapter(
        store=DiagnosisReportStore(session_factory=sessions),
        service=DiagnosisService(provider=diagnosis_provider or SuggestionProvider()),
    )
    workflow = WorkflowService(
        checkpoints=checkpoints,
        reader=reader,
        session_factory=sessions,
        agent=agent,
        diagnosis_service=diagnosis,
        repository=DatabaseGradingRepository(
            session_factory=outcome_session_factory or sessions,
            clock=lambda: FIXED_NOW,
        ),
        settings=settings,
        clock=lambda: FIXED_NOW,
    )
    review = build_review_service(
        checkpoints=checkpoints,
        reader=reader,
        diagnosis=diagnosis,
        workflow_provider=workflow.workflow_for_run,
        session_factory=sessions,
    )
    workflow.use_review_service_provider(lambda: review)
    review_query = ReviewQueryService(session_factory=sessions)
    review_decision = ReviewDecisionService(
        query=review_query,
        review_service=review,
        settings=settings,
    )
    results = ResultsQueryService(
        repository=DatabaseGradingRepository(
            session_factory=sessions,
            clock=lambda: FIXED_NOW,
        ),
        session_factory=sessions,
        diagnosis_store=DiagnosisReportStore(session_factory=sessions),
    )

    app = create_app(settings=settings, routers=('workflow', 'reviews', 'grading', 'results'))

    def database() -> Iterator[Session]:
        with Session(env.engine) as session:
            yield session

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_workflow_service] = lambda: workflow
    app.dependency_overrides[get_review_query_service] = lambda: review_query
    app.dependency_overrides[get_review_decision_service] = lambda: review_decision
    app.dependency_overrides[get_results_query_service] = lambda: results
    from sqlalchemy.orm import sessionmaker

    with pytest.MonkeyPatch.context() as patch:
        bind_startup_session_factory(patch, sessionmaker(bind=env.engine, expire_on_commit=False))
        with TestClient(
            app,
            raise_server_exceptions=raise_server_exceptions,
        ) as client:
            yield ApiHarness(client=client, workflow=workflow)


def _start(harness: ApiHarness, env: ReviewEnv) -> Any:
    """从唯一正式启动入口创建运行。"""

    return harness.client.post(
        f"/api/workflow/submissions/{env.paper.submission_id}/runs",
        headers=_teacher_headers(env),
        json={"regrade": False},
    )


def _teacher_result(harness: ApiHarness, env: ReviewEnv) -> Any:
    """从教师正式结果入口读取答卷。"""

    return harness.client.get(
        (
            f"/api/results/exams/{env.paper.exam_id}/submissions/"
            f"{env.paper.submission_id}"
        ),
        headers=_teacher_headers(env),
    )


def _student_result(harness: ApiHarness, env: ReviewEnv) -> Any:
    """从学生正式结果入口读取答卷。"""

    return harness.client.get(
        f"/api/results/me/submissions/{env.paper.submission_id}",
        headers=_student_headers(env),
    )


def _student_diagnosis(harness: ApiHarness, env: ReviewEnv) -> Any:
    """从学生正式结果入口读取诊断。"""

    return harness.client.get(
        f"/api/results/me/submissions/{env.paper.submission_id}/diagnosis",
        headers=_student_headers(env),
    )


def _confirm_body(env: ReviewEnv, workflow_id: str) -> dict[str, Any]:
    """构造教师确认请求；身份只来自启动与复核读模型返回的正式事实。"""

    return {
        "submission_id": env.paper.submission_id,
        "answer_id": env.paper.subjective_answer_ids[0],
        "action": "confirm",
        "workflow_id": workflow_id,
        "expected_review_status": ReviewStatus.PENDING_REVIEW.value,
        "comment": "教师确认 AI 评分。",
    }
