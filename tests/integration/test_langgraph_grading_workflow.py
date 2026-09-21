"""T079 正式入口闭环集成测试。

测试使用真实 PostgreSQL 隔离 schema、真实 T069/T072/T073/T074、结果仓储与诊断存储。
只替换评分/诊断 LLM Provider、检索、Embedding 与 Reranker 外部边界；不得手工写业务结果表。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier
from typing import Any
from uuid import UUID

import gradio as gr
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.ai.agents.grading_agent import GradingAgent
from backend.app.ai.llm.base import BaseLLMProvider, LLMMessage
from backend.app.ai.workflows.grading_workflow import GradingWorkflow
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
from backend.app.core.app import create_app
from backend.app.core.database import get_db
from backend.app.domain.enums import (
    AnswerStatus,
    ExamStatus,
    QuestionStatus,
    QuestionType,
    ReviewStatus,
    SubmissionStatus,
    UserRole,
    WorkflowStatus,
)
from backend.app.models import (
    Answer,
    Course,
    Exam,
    ExamResult,
    GradingResult,
    Question,
    ReviewRecord,
    Role,
    Submission,
    User,
    WorkflowRun,
)
from backend.app.services.auth_service import create_access_token
from backend.app.services.diagnosis_service import DiagnosisService
from backend.app.services.grading.diagnosis_report_store import (
    DiagnosisReportStore,
    DiagnosisStoreNotReadyError,
)
from backend.app.services.grading.grading_repository import DatabaseGradingRepository
from backend.app.services.grading.grading_task_service import (
    DatabaseGradingSubmissionReader,
)
from backend.app.services.grading.subjective_grader import SubjectiveGradingPayload
from backend.app.services.workflow_checkpoint import WorkflowCheckpointStore
from tests.postgres_helpers import isolated_postgres_engine
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
        questions=questions,
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

    with gr.Blocks() as ui:
        gr.Markdown("T079 正式入口集成测试")
    app = create_app(settings=settings, gradio_app=ui)

    def database() -> Iterator[Session]:
        with Session(env.engine) as session:
            yield session

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_workflow_service] = lambda: workflow
    app.dependency_overrides[get_review_query_service] = lambda: review_query
    app.dependency_overrides[get_review_decision_service] = lambda: review_decision
    app.dependency_overrides[get_results_query_service] = lambda: results
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


def test_high_confidence_result_is_readable_from_results_api(
    env: ReviewEnv,
) -> None:
    """高置信度混合答卷从启动 API 完成，并由结果/诊断 API 读回。"""

    scoring = SequenceScoringProvider([0.95, 0.95])
    diagnosis = SuggestionProvider()
    with _api_client(
        env,
        scoring_provider=scoring,
        diagnosis_provider=diagnosis,
    ) as harness:
        started = _start(harness, env)
        result = _student_result(harness, env)
        report = _student_diagnosis(harness, env)

    assert started.status_code == 200
    assert started.json()["status"] == WorkflowStatus.COMPLETED.value
    assert result.status_code == 200
    body = result.json()
    assert body["is_final"] is True
    assert body["total_score"] == "22.00"
    assert len(body["items"]) == 3
    assert report.status_code == 200
    assert report.json()["status"] == "Ready"
    assert report.json()["learning_suggestions"] == ["继续复习相关知识点。"]
    assert len(scoring.calls) == 2
    assert len(diagnosis.calls) == 1


def test_low_confidence_result_enters_real_review_queue(
    env: ReviewEnv,
) -> None:
    """低置信度结果由启动 API 落库，并由真实复核队列与详情读到。"""

    scoring = SequenceScoringProvider([0.3])
    with _api_client(env, scoring_provider=scoring) as harness:
        started = _start(harness, env)
        queue = harness.client.get(
            "/api/reviews/queue",
            headers=_teacher_headers(env),
        )
        teacher_result = _teacher_result(harness, env)

        assert started.status_code == 200
        run = started.json()
        assert run["status"] == WorkflowStatus.PAUSED.value
        assert queue.status_code == 200
        queued = queue.json()
        assert queued["total"] == 1
        item = queued["items"][0]
        assert item["answer_id"] == env.paper.subjective_answer_ids[0]
        detail = harness.client.get(
            (
                f"/api/reviews/queue/{env.paper.submission_id}/answers/"
                f"{item['answer_id']}"
            ),
            headers=_teacher_headers(env),
        )

    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["review_status"] == ReviewStatus.PENDING_REVIEW.value
    assert detail_body["workflow_id"] == run["workflow_id"]
    assert detail_body["workflow_status"] == WorkflowStatus.PAUSED.value
    assert teacher_result.status_code == 200
    pending = teacher_result.json()
    assert pending["is_final"] is False
    assert pending["pending_review_count"] == 1
    assert len(pending["items"]) == 3
    assert pending["items"][2]["missing"] is True


@pytest.mark.parametrize("action", ["confirm", "modify"])
def test_decision_retry_returns_original_record_and_live_pending_count(
    env: ReviewEnv, action: str
) -> None:
    """真实 PostgreSQL/API 落库后跨实例重试，仅返回原记录及当前待复核数。"""

    with _api_client(env, scoring_provider=SequenceScoringProvider([0.3, 0.3])) as first:
        started = _start(first, env)
        assert started.status_code == 200
        body = _confirm_body(env, started.json()["workflow_id"])
        detail = first.client.get(
            f"/api/reviews/queue/{env.paper.submission_id}/answers/{body['answer_id']}",
            headers=_teacher_headers(env),
        ).json()
        round_id = detail["pending_review_round_id"]
        assert round_id is not None and UUID(round_id).version == 4
        body["expected_review_round_id"] = round_id
        body["action"] = action
        if action == "modify":
            body.update(score="8.50", reason="教师补充评分理由。")
        saved = first.client.post(
            "/api/reviews/decisions", headers=_teacher_headers(env), json=body
        )
        assert saved.status_code == 200
        assert saved.json()["pending_review_count"] == 1
        assert saved.json()["review_round_id"] == round_id
        assert saved.json()["idempotency_degraded"] is False
        record_id = saved.json()["review_record_id"]
    scoring = SequenceScoringProvider([])
    with _api_client(env, scoring_provider=scoring) as second:
        retry = second.client.post(
            "/api/reviews/decisions", headers=_teacher_headers(env), json=body
        )
        assert retry.status_code == 200
        assert retry.json()["review_record_id"] == record_id
        assert retry.json()["pending_review_count"] == 1
        assert retry.json()["review_round_id"] == round_id
        assert retry.json()["idempotency_degraded"] is False
        assert scoring.calls == []
        with Session(env.engine) as session:
            assert len(list(session.scalars(select(ReviewRecord)))) == 1
        next_body = _confirm_body(env, body["workflow_id"])
        next_body["answer_id"] = env.paper.subjective_answer_ids[1]
        final = second.client.post(
            "/api/reviews/decisions", headers=_teacher_headers(env), json=next_body
        )
        assert final.status_code == 200
        assert final.json()["workflow_status"] == WorkflowStatus.COMPLETED.value
        retry = second.client.post(
            "/api/reviews/decisions", headers=_teacher_headers(env), json=body
        )
        assert retry.status_code == 200
        assert retry.json()["review_record_id"] == record_id
        assert retry.json()["pending_review_count"] == 0
        assert retry.json()["resume_status"] == "succeeded"
        assert scoring.calls == []
    with Session(env.engine) as session:
        assert len(list(session.scalars(select(ReviewRecord)))) == 2


def test_saved_decision_counts_database_pending_after_resume_failure(
    solo_env: ReviewEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """决定事务已提交、图恢复失败时，计数反映已修改评分而不是旧图 Pending。"""

    async def fail_resume(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("测试注入恢复失败。")

    with _api_client(solo_env, scoring_provider=SequenceScoringProvider([0.3])) as harness:
        started = _start(harness, solo_env)
        assert started.status_code == 200
        body = _confirm_body(solo_env, started.json()["workflow_id"])
        body.update(action="modify", score="8.00", reason="教师修订。")
        monkeypatch.setattr(GradingWorkflow, "apply_teacher_decision_async", fail_resume)
        saved = harness.client.post(
            "/api/reviews/decisions", headers=_teacher_headers(solo_env), json=body
        )
        assert saved.status_code == 200
        assert saved.json()["decision_saved"] is True
        assert saved.json()["resume_status"] == "failed"
        assert saved.json()["pending_review_count"] == 0
        retry = harness.client.post(
            "/api/reviews/decisions", headers=_teacher_headers(solo_env), json=body
        )
        assert retry.status_code == 200
        assert retry.json()["review_record_id"] == saved.json()["review_record_id"]
        assert retry.json()["pending_review_count"] == 0
    with Session(solo_env.engine) as session:
        assert len(list(session.scalars(select(ReviewRecord)))) == 1
        assert session.scalars(select(GradingResult)).one().review_status is ReviewStatus.MODIFIED


def test_invalid_structured_result_fails_without_fake_result(
    solo_env: ReviewEnv,
) -> None:
    """越界结构化评分经启动 API 进入 Failed，结果 API 不伪造空成绩。"""

    provider = InvalidStructuredProvider()
    with _api_client(solo_env, scoring_provider=provider) as harness:
        started = _start(harness, solo_env)
        result = _student_result(harness, solo_env)

    assert started.status_code == 200
    assert started.json()["status"] == WorkflowStatus.FAILED.value
    assert result.status_code == 200
    assert result.json()["is_final"] is None
    assert result.json()["total_score"] is None
    assert result.json()["items"] == []
    assert len(provider.calls) == 1
    with Session(solo_env.engine) as session:
        assert list(session.scalars(select(GradingResult))) == []
        assert list(session.scalars(select(ExamResult))) == []


def test_review_then_new_session_resume_reloads_result_and_diagnosis(
    solo_env: ReviewEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """复核 API 保存结论；新应用实例从 PostgreSQL 恢复原 Workflow 并补齐诊断。"""

    original_save = DiagnosisReportStore.save

    def fail_diagnosis_save(_store: DiagnosisReportStore, _report: Any) -> Any:
        raise DiagnosisStoreNotReadyError(
            "测试模拟诊断报告写入暂时不可用。",
            retryable=True,
        )

    monkeypatch.setattr(DiagnosisReportStore, "save", fail_diagnosis_save)
    first_scoring = SequenceScoringProvider([0.3])
    first_diagnosis = SuggestionProvider()
    with _api_client(
        solo_env,
        scoring_provider=first_scoring,
        diagnosis_provider=first_diagnosis,
    ) as first:
        started = _start(first, solo_env)
        workflow_id = started.json()["workflow_id"]
        reviewed = first.client.post(
            "/api/reviews/decisions",
            headers=_teacher_headers(solo_env),
            json=_confirm_body(solo_env, workflow_id),
        )
        result_after_review = _student_result(first, solo_env)
        diagnosis_after_review = _student_diagnosis(first, solo_env)
    monkeypatch.setattr(DiagnosisReportStore, "save", original_save)

    assert started.status_code == 200
    assert started.json()["status"] == WorkflowStatus.PAUSED.value
    assert reviewed.status_code == 200
    review_body = reviewed.json()
    assert review_body["decision_saved"] is True
    assert review_body["workflow_status"] == WorkflowStatus.PAUSED.value
    assert review_body["resumable"] is True
    assert review_body["diagnosis_error_code"]
    assert result_after_review.status_code == 200
    assert result_after_review.json()["is_final"] is True
    assert result_after_review.json()["total_score"] == "6.00"
    assert diagnosis_after_review.json()["status"] == "Not Ready"

    assert len(first_diagnosis.calls) == 1
    second_scoring = SequenceScoringProvider([0.95])
    successful_diagnosis = SuggestionProvider()
    with _api_client(
        solo_env,
        scoring_provider=second_scoring,
        diagnosis_provider=successful_diagnosis,
    ) as second:
        resumed = second.client.post(
            f"/api/workflow/runs/{workflow_id}/resume",
            headers=_teacher_headers(solo_env),
            json={},
        )
        final_result = _student_result(second, solo_env)
        final_diagnosis = _student_diagnosis(second, solo_env)

    assert resumed.status_code == 200
    assert resumed.json()["status"] == WorkflowStatus.COMPLETED.value
    assert resumed.json()["resumed"] is True
    assert final_result.status_code == 200
    assert final_result.json()["is_final"] is True
    assert final_result.json()["total_score"] == "6.00"
    assert final_diagnosis.status_code == 200
    assert final_diagnosis.json()["status"] == "Ready"
    assert final_diagnosis.json()["learning_suggestions"] == [
        "继续复习相关知识点。"
    ]
    assert second_scoring.calls == []
    with Session(solo_env.engine) as session:
        records = list(session.scalars(select(ReviewRecord)))
        assert len(records) == 1
        assert records[0].decision is ReviewStatus.CONFIRMED
        assert session.scalars(select(WorkflowRun)).one().status is WorkflowStatus.COMPLETED


def test_concurrent_review_api_decisions_only_one_updates_postgres_row(
    solo_env: ReviewEnv,
) -> None:
    """两个正式 API 请求同时提交同一结论，仅一个复核事务能更新 PostgreSQL 行。"""

    with _api_client(
        solo_env,
        scoring_provider=SequenceScoringProvider([0.3]),
    ) as starter:
        started = _start(starter, solo_env)
    assert started.status_code == 200
    workflow_id = started.json()["workflow_id"]
    body = _confirm_body(solo_env, workflow_id)
    barrier = Barrier(2)

    with (
        _api_client(
            solo_env,
            scoring_provider=SequenceScoringProvider([]),
        ) as first,
        _api_client(
            solo_env,
            scoring_provider=SequenceScoringProvider([]),
        ) as second,
    ):

        def submit(harness: ApiHarness) -> Any:
            barrier.wait()
            return harness.client.post(
                "/api/reviews/decisions",
                headers=_teacher_headers(solo_env),
                json=body,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(submit, (first, second)))

    assert sorted(response.status_code for response in responses) == [200, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json()["detail"]["error_code"] == "REVIEW_SERVICE_CONFLICT"
    with Session(solo_env.engine) as session:
        records = list(session.scalars(select(ReviewRecord)))
        assert len(records) == 1
        assert records[0].decision is ReviewStatus.CONFIRMED
        row = session.scalars(select(GradingResult)).one()
        assert row.review_status is ReviewStatus.CONFIRMED


def test_outcome_commit_failure_leaves_no_partial_results(
    solo_env: ReviewEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正式启动的结果事务提交失败时，成绩、进度与完成态整体回滚。"""

    with Session(solo_env.engine) as session:
        answer_before = {
            str(answer.id): answer.status
            for answer in session.scalars(select(Answer))
        }
        submission = session.get(Submission, UUID(solo_env.paper.submission_id))
        assert submission is not None
        submission_before = (submission.status, submission.graded_at)

    def failing_outcome_session() -> Session:
        session = Session(solo_env.engine)
        commit = session.commit

        def fail_commit() -> None:
            if session.info.get("wrote_results"):
                raise SQLAlchemyError("测试模拟结果事务提交失败")
            commit()

        monkeypatch.setattr(session, "commit", fail_commit)

        @event.listens_for(session, "before_flush")
        def track_result_writes(active: Session, *_args: Any) -> None:
            if any(
                isinstance(row, (GradingResult, ExamResult))
                for row in active.new.union(active.dirty)
            ):
                active.info["wrote_results"] = True

        @event.listens_for(session, "before_commit", once=True)
        def call_patched_commit(active: Session) -> None:
            # save_workflow_outcome 使用 session.begin()；在它真正提交前调用已 monkeypatch
            # 的 session.commit，使故障落在生产事务的提交边界而不是业务写入中间。
            if active.info.get("wrote_results"):
                active.commit()

        return session

    with _api_client(
        solo_env,
        scoring_provider=SequenceScoringProvider([0.95]),
        outcome_session_factory=failing_outcome_session,
        raise_server_exceptions=False,
    ) as harness:
        response = _start(harness, solo_env)

    assert response.status_code == 500
    with Session(solo_env.engine) as session:
        assert list(session.scalars(select(GradingResult))) == []
        assert list(session.scalars(select(ExamResult))) == []
        run = session.scalars(select(WorkflowRun)).one()
        assert run.status is not WorkflowStatus.COMPLETED
        answer_after = {
            str(answer.id): answer.status
            for answer in session.scalars(select(Answer))
        }
        submission = session.get(Submission, UUID(solo_env.paper.submission_id))
        assert submission is not None
        assert (submission.status, submission.graded_at) == submission_before
    assert answer_after == answer_before
