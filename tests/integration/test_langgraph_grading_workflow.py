"""T079 完整 LangGraph 阅卷 Workflow 集成测试。

测试使用真实 PostgreSQL 隔离 schema、真实 T069/T072/T073/T074 组件，只替换检索、Embedding、
评分 Provider 和诊断生成等外部边界。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from backend.app.ai.agents.grading_agent import GradingAgent
from backend.app.ai.agents.state import confidence_decision_from_snapshot
from backend.app.ai.workflows.grading_handoff import LOAD_SUBMISSION
from backend.app.ai.workflows.grading_workflow import (
    GradingWorkflow,
    GradingWorkflowDeps,
    TeacherReviewDecision,
)
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
    Question,
    ReviewRecord,
    Role,
    Submission,
    User,
    WorkflowRun,
)
from backend.app.models import (
    GradingResult as GradingResultRow,
)
from backend.app.schemas.ai import GradingResult
from backend.app.schemas.grading import (
    DiagnosisReportDTO,
    DiagnosisStatus,
    ExamResultDTO,
)
from backend.app.services.grading.grading_repository import DatabaseGradingRepository
from backend.app.services.grading.grading_task_service import (
    DatabaseGradingSubmissionReader,
    SubmissionSnapshot,
)
from backend.app.services.grading.result_aggregator import ResultAggregator
from backend.app.services.grading.subjective_grader import SubjectiveGradingPayload
from backend.app.services.review_service import (
    DatabaseReviewRecordStore,
    ReviewConflictError,
    ReviewService,
    ReviewStaleDecisionError,
)
from backend.app.services.workflow_checkpoint import (
    DatabaseCheckpointSaver,
    WorkflowCheckpointStore,
    checkpoint_thread_id,
    runtime_has_checkpoint,
)
from tests.postgres_helpers import isolated_postgres_engine
from tests.support.subjective_grading_doubles import (
    StubEmbeddingProvider,
    StubReranker,
    StubRetriever,
    StubScoringProvider,
    make_chunk,
)
from tests.unit.settings_helpers import build_test_settings

WORKFLOW_ID = "workflow-t079"
REQUEST_ID = "request-t079"
THREAD_ID = "thread-t079"
THRESHOLD = 0.8
FIXED_NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Paper:
    """混合答卷的数据库标识。"""

    teacher_id: str
    student_id: str
    course_id: str
    submission_id: str
    objective_answer_id: str
    subjective_answer_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReviewEnv:
    """一次测试使用的隔离 PostgreSQL 环境。"""

    engine: Engine
    paper: Paper

    @property
    def teacher_id(self) -> str:
        return self.paper.teacher_id


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
    """写入教师、课程、已发布考试、答卷和答案。"""

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
    objective = Question(
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
        objective.type = QuestionType.SHORT_ANSWER
        objective.content = "解释变量的作用。"
        objective.options = None
        objective.reference_answer = "变量用于保存数据。"
        objective.scoring_rubric = "说明保存和引用数据即可。"
        objective.knowledge_points = ["变量"]
    questions = [objective]
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
            question=objective,
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
        submission_id=str(submission.id),
        objective_answer_id=str(answers[0].id),
        subjective_answer_ids=tuple(
            str(answer.id)
            for answer, question in zip(answers, questions, strict=True)
            if question.type is QuestionType.SHORT_ANSWER
        ),
    )


class SequenceScoringProvider:
    """按预定顺序返回结构化评分结果的 Provider 替身。"""

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


class DiagnosisDouble:
    """只替换诊断外部生成，保留图内诊断门槛和状态流转。"""

    def __init__(self) -> None:
        self.generate_calls: list[ExamResultDTO] = []
        self.record_calls: list[ExamResultDTO] = []

    async def generate(self, exam_result: ExamResultDTO) -> DiagnosisReportDTO:
        self.generate_calls.append(exam_result)
        return DiagnosisReportDTO(
            submission_id=exam_result.submission_id,
            student_id=exam_result.student_id,
            status=DiagnosisStatus.READY,
            generated_at=FIXED_NOW,
            source_exam_result_updated_at=exam_result.aggregated_at,
            learning_suggestions=["继续复习相关知识点。"],
        )

    def record(self, exam_result: ExamResultDTO) -> DiagnosisReportDTO:
        self.record_calls.append(exam_result)
        return asyncio.run(self.generate(exam_result))


def _snapshot(env: ReviewEnv) -> SubmissionSnapshot:
    """从真实数据库读取权威答卷快照。"""

    return DatabaseGradingSubmissionReader(
        session_factory=lambda: Session(env.engine)
    ).load(env.paper.submission_id)


def _store(env: ReviewEnv) -> WorkflowCheckpointStore:
    """构造使用独立数据库会话的业务检查点存储。"""

    return WorkflowCheckpointStore(
        session_factory=lambda: Session(env.engine),
        clock=lambda: FIXED_NOW,
    )


def _saver(env: ReviewEnv) -> DatabaseCheckpointSaver:
    """构造持久化 LangGraph Checkpointer。"""

    return DatabaseCheckpointSaver(
        session_factory=lambda: Session(env.engine),
        clock=lambda: FIXED_NOW,
    )


def _agent(
    env: ReviewEnv,
    provider: Any,
) -> tuple[GradingAgent, Any, Any, Any]:
    """装配真实 GradingAgent，仅替换检索、重排、Embedding 和评分 Provider。"""

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
        session_factory=lambda: Session(env.engine),
    )
    return agent, retriever, embedding, reranker


def _workflow(
    env: ReviewEnv,
    agent: GradingAgent,
    saver: DatabaseCheckpointSaver,
    diagnosis: DiagnosisDouble,
) -> GradingWorkflow:
    """装配真实 LangGraph 图和持久化检查点。"""

    return GradingWorkflow(
        GradingWorkflowDeps(
            snapshot=_snapshot(env),
            agent=agent,
            diagnosis_service=diagnosis,
            settings=build_test_settings(confidence_threshold=THRESHOLD),
        ),
        checkpointer=saver,
    )


def _seed_checkpoint(
    env: ReviewEnv,
    *,
    workflow_id: str,
    request_id: str,
    thread_id: str,
) -> WorkflowCheckpointStore:
    """写入启动业务检查点，供 LangGraph runtime 检查点绑定。"""

    store = _store(env)
    store.save_checkpoint(
        workflow_id,
        {
            "workflow_id": workflow_id,
            "request_id": request_id,
            "submission_id": env.paper.submission_id,
            "status": WorkflowStatus.RUNNING,
            "current_node": LOAD_SUBMISSION,
            "retry_count": 0,
            "resumable": False,
        },
        LOAD_SUBMISSION,
        thread_id=thread_id,
    )
    return store


def _persist_paused(
    env: ReviewEnv,
    store: WorkflowCheckpointStore,
    state: Mapping[str, Any],
    *,
    workflow_id: str,
    thread_id: str,
) -> ExamResultDTO:
    """把暂停时的业务状态和当前整卷结果写入真实持久层。"""

    state_copy = dict(state)
    store.save_checkpoint(
        workflow_id,
        state_copy,
        str(state_copy["current_node"]),
        pause_reason=str(state_copy["pause_reason"]),
        thread_id=thread_id,
    )
    snapshot = _snapshot(env)
    results = [
        item
        for item in (state_copy.get("grading_results") or {}).values()
        if isinstance(item, GradingResult)
    ]
    decisions = {
        str(answer_id): confidence_decision_from_snapshot(item)
        for answer_id, item in (state_copy.get("confidence_decisions") or {}).items()
    }
    exam_result = ResultAggregator().aggregate(
        snapshot.to_context(),
        results=results,
        decisions=decisions,
        now=FIXED_NOW,
    )
    DatabaseGradingRepository(
        session_factory=lambda: Session(env.engine),
        clock=lambda: FIXED_NOW,
    ).save_exam_result(exam_result)
    return exam_result


def _review_service(
    env: ReviewEnv,
    workflow: GradingWorkflow,
    diagnosis: DiagnosisDouble,
) -> ReviewService:
    """按 T074 生产依赖装配真实复核服务。"""

    session_factory = lambda: Session(env.engine)
    return ReviewService(
        checkpoints=_store(env),
        reader=DatabaseGradingSubmissionReader(session_factory=session_factory),
        session_factory=session_factory,
        workflow_provider=lambda _run, _state: workflow,
        result_writer=DatabaseGradingRepository(
            session_factory=session_factory,
            clock=lambda: FIXED_NOW,
        ),
        diagnosis=diagnosis,
        review_records=DatabaseReviewRecordStore(),
    )


def test_mixed_submission_runs_real_graph_and_keeps_objective_deterministic(
    env: ReviewEnv,
) -> None:
    """混合答卷完整走图：客观题规则评分，主观题检索和结构化 Provider 后形成诊断。"""

    provider = StubScoringProvider(score=6.0, confidence=0.95)
    agent, retriever, embedding, reranker = _agent(env, provider)
    diagnosis = DiagnosisDouble()
    saver = _saver(env)
    store = _seed_checkpoint(
        env,
        workflow_id=WORKFLOW_ID,
        request_id=REQUEST_ID,
        thread_id=THREAD_ID,
    )
    workflow = _workflow(env, agent, saver, diagnosis)
    result = asyncio.run(
        workflow.run_async(
            request_id=REQUEST_ID,
            workflow_id=WORKFLOW_ID,
            submission_id=env.paper.submission_id,
            thread_id=THREAD_ID,
        )
    )

    assert result.interrupted is False
    assert result.state["status"] is WorkflowStatus.COMPLETED
    assert result.state["exam_result"].is_final is True
    assert result.state["final_results"] == result.state["exam_result"].items
    assert result.state["diagnosis"] is not None
    assert len(provider.calls) == len(env.paper.subjective_answer_ids)
    assert len(retriever.calls) == len(env.paper.subjective_answer_ids)
    assert len(embedding.queries) == len(env.paper.subjective_answer_ids)
    assert len(reranker.calls) == len(env.paper.subjective_answer_ids)
    objective = result.state["grading_results"][env.paper.objective_answer_id]
    assert objective.score == 10
    assert all(call["messages"][0]["role"] == "system" for call in provider.calls)
    assert result.state["final_results"]

    store.save_checkpoint(
        WORKFLOW_ID,
        dict(result.state),
        str(result.state["current_node"]),
        thread_id=THREAD_ID,
    )
    DatabaseGradingRepository(
        session_factory=lambda: Session(env.engine),
        clock=lambda: FIXED_NOW,
    ).save_exam_result(result.state["exam_result"])
    completed = store.mark_completed(WORKFLOW_ID)
    assert completed.status is WorkflowStatus.COMPLETED
    with Session(env.engine) as session:
        assert session.scalars(select(WorkflowRun)).one().status is WorkflowStatus.COMPLETED


def test_structured_validation_failure_stops_before_final_results(
    solo_env: ReviewEnv,
) -> None:
    """结构化评分越界时图进入失败状态，不生成最终结果或诊断。"""

    provider = InvalidStructuredProvider()
    agent, _, _, _ = _agent(solo_env, provider)
    diagnosis = DiagnosisDouble()
    saver = _saver(solo_env)
    store = _seed_checkpoint(
        solo_env,
        workflow_id=f"{WORKFLOW_ID}-invalid",
        request_id=f"{REQUEST_ID}-invalid",
        thread_id=f"{THREAD_ID}-invalid",
    )
    workflow = _workflow(solo_env, agent, saver, diagnosis)
    result = asyncio.run(
        workflow.run_async(
            request_id=f"{REQUEST_ID}-invalid",
            workflow_id=f"{WORKFLOW_ID}-invalid",
            submission_id=solo_env.paper.submission_id,
            thread_id=f"{THREAD_ID}-invalid",
        )
    )

    assert result.interrupted is False
    assert result.state["status"] is WorkflowStatus.FAILED
    assert result.state["error"] is not None
    assert result.state.get("final_results") == []
    assert result.state.get("exam_result") is None
    assert result.state.get("diagnosis") is None
    assert len(provider.calls) == 1
    store.save_checkpoint(
        f"{WORKFLOW_ID}-invalid",
        dict(result.state),
        str(result.state["current_node"]),
        thread_id=f"{THREAD_ID}-invalid",
    )
    failed = store.mark_failed(f"{WORKFLOW_ID}-invalid", result.state["error"])
    assert failed.status is WorkflowStatus.FAILED
    with Session(solo_env.engine) as session:
        assert session.scalars(select(GradingResultRow)).all() == []


def test_review_service_regrades_then_teacher_confirmation_unlocks_diagnosis(
    env: ReviewEnv,
) -> None:
    """低置信度暂停后，Reviewer 重评回到评分节点，教师确认后才生成最终诊断。"""

    provider = SequenceScoringProvider([0.3, 0.95, 0.95])
    agent, _, _, _ = _agent(env, provider)
    diagnosis = DiagnosisDouble()
    saver = _saver(env)
    workflow_id = f"{WORKFLOW_ID}-review"
    request_id = f"{REQUEST_ID}-review"
    thread_id = f"{THREAD_ID}-review"
    store = _seed_checkpoint(
        env,
        workflow_id=workflow_id,
        request_id=request_id,
        thread_id=thread_id,
    )
    workflow = _workflow(env, agent, saver, diagnosis)
    paused = asyncio.run(
        workflow.run_async(
            request_id=request_id,
            workflow_id=workflow_id,
            submission_id=env.paper.submission_id,
            thread_id=thread_id,
        )
    )
    assert paused.interrupted is True
    assert paused.state["status"] is WorkflowStatus.PAUSED
    assert paused.pending_answer_ids == (env.paper.subjective_answer_ids[0],)
    assert paused.state.get("final_results") == []
    assert paused.state.get("diagnosis") is None
    paused_exam = _persist_paused(
        env,
        store,
        paused.state,
        workflow_id=workflow_id,
        thread_id=thread_id,
    )
    assert paused_exam.is_final is False
    assert paused_exam.pending_review_answer_count == 1
    service = _review_service(env, workflow, diagnosis)
    target_id = env.paper.subjective_answer_ids[0]

    regraded = service.request_regrade(
        workflow_id,
        thread_id,
        target_id,
        actor_id=env.teacher_id,
        actor_role=UserRole.TEACHER,
        expected_review_status=ReviewStatus.PENDING_REVIEW.value,
        comment="请重新核对评分理由。",
    )
    assert regraded.decision is ReviewStatus.RE_GRADE
    assert regraded.workflow_status is WorkflowStatus.PAUSED
    assert regraded.exam_result is not None and regraded.exam_result.is_final is False
    assert regraded.diagnosis is None
    assert diagnosis.generate_calls == []
    assert len(provider.calls) == 2

    confirmed = service.submit_decision(
        TeacherReviewDecision(
            workflow_id=workflow_id,
            thread_id=thread_id,
            answer_id=target_id,
            review_status=ReviewStatus.CONFIRMED.value,
            expected_review_status=ReviewStatus.PENDING_REVIEW.value,
        ),
        actor_id=env.teacher_id,
        actor_role=UserRole.TEACHER,
        comment="教师确认重评结果。",
    )
    assert confirmed.workflow_status is WorkflowStatus.COMPLETED
    assert confirmed.exam_result is not None and confirmed.exam_result.is_final is True
    assert confirmed.exam_result.final_total_score is not None
    assert confirmed.diagnosis is not None
    assert len(diagnosis.generate_calls) == 1
    assert len(provider.calls) == 3
    with Session(env.engine) as session:
        records = list(session.scalars(select(ReviewRecord)))
        assert [record.decision for record in records] == [
            ReviewStatus.RE_GRADE,
            ReviewStatus.CONFIRMED,
        ]
        row = session.scalars(
            select(GradingResultRow).where(
                GradingResultRow.answer_id == target_id,
            )
        ).one()
        assert row.review_status is ReviewStatus.CONFIRMED
        assert session.scalars(select(WorkflowRun)).one().status is WorkflowStatus.COMPLETED


def test_cross_instance_resume_uses_postgres_runtime_checkpoint(
    solo_env: ReviewEnv,
) -> None:
    """实例 A 中断后销毁图对象，实例 B 只从 PostgreSQL 检查点恢复并完成。"""

    workflow_id = f"{WORKFLOW_ID}-cross-instance"
    request_id = f"{REQUEST_ID}-cross-instance"
    thread_id = f"{THREAD_ID}-cross-instance"
    provider_a = SequenceScoringProvider([0.3])
    agent_a, _, _, _ = _agent(solo_env, provider_a)
    diagnosis_a = DiagnosisDouble()
    saver_a = _saver(solo_env)
    store_a = _seed_checkpoint(
        solo_env,
        workflow_id=workflow_id,
        request_id=request_id,
        thread_id=thread_id,
    )
    workflow_a = _workflow(solo_env, agent_a, saver_a, diagnosis_a)
    paused = asyncio.run(
        workflow_a.run_async(
            request_id=request_id,
            workflow_id=workflow_id,
            submission_id=solo_env.paper.submission_id,
            thread_id=thread_id,
        )
    )
    assert paused.interrupted is True
    _persist_paused(
        solo_env,
        store_a,
        paused.state,
        workflow_id=workflow_id,
        thread_id=thread_id,
    )
    paused_row = store_a.load_checkpoint(workflow_id)
    assert paused_row is not None
    assert checkpoint_thread_id(paused_row) == thread_id
    assert runtime_has_checkpoint(paused_row.checkpoint, thread_id)

    del workflow_a, saver_a, agent_a, provider_a, diagnosis_a

    provider_b = SequenceScoringProvider([0.95])
    agent_b, _, _, _ = _agent(solo_env, provider_b)
    diagnosis_b = DiagnosisDouble()
    saver_b = _saver(solo_env)
    workflow_b = _workflow(solo_env, agent_b, saver_b, diagnosis_b)
    service_b = _review_service(solo_env, workflow_b, diagnosis_b)
    resumed = service_b.submit_decision(
        TeacherReviewDecision(
            workflow_id=workflow_id,
            thread_id=thread_id,
            answer_id=solo_env.paper.subjective_answer_ids[0],
            review_status=ReviewStatus.CONFIRMED.value,
            expected_review_status=ReviewStatus.PENDING_REVIEW.value,
        ),
        actor_id=solo_env.teacher_id,
        actor_role=UserRole.TEACHER,
    )

    assert resumed.resumed is True
    assert resumed.workflow_status is WorkflowStatus.COMPLETED
    assert resumed.exam_result is not None and resumed.exam_result.is_final is True
    assert resumed.diagnosis is not None
    assert provider_b.calls == []
    assert len(diagnosis_b.generate_calls) == 1
    assert diagnosis_b.generate_calls[0].submission_id == resumed.exam_result.submission_id
    assert diagnosis_b.generate_calls[0].is_final is True
    with Session(solo_env.engine) as session:
        assert session.scalars(select(WorkflowRun)).one().status is WorkflowStatus.COMPLETED


def test_concurrent_teacher_decisions_only_one_updates_postgres_row(
    solo_env: ReviewEnv,
) -> None:
    """两个独立请求同时提交确认，只有一个事务能通过条件状态更新。"""

    workflow_id = f"{WORKFLOW_ID}-concurrent"
    request_id = f"{REQUEST_ID}-concurrent"
    thread_id = f"{THREAD_ID}-concurrent"
    provider = SequenceScoringProvider([0.3])
    agent, _, _, _ = _agent(solo_env, provider)
    diagnosis = DiagnosisDouble()
    saver = _saver(solo_env)
    store = _seed_checkpoint(
        solo_env,
        workflow_id=workflow_id,
        request_id=request_id,
        thread_id=thread_id,
    )
    workflow = _workflow(solo_env, agent, saver, diagnosis)
    paused = asyncio.run(
        workflow.run_async(
            request_id=request_id,
            workflow_id=workflow_id,
            submission_id=solo_env.paper.submission_id,
            thread_id=thread_id,
        )
    )
    assert paused.interrupted is True
    _persist_paused(
        solo_env,
        store,
        paused.state,
        workflow_id=workflow_id,
        thread_id=thread_id,
    )

    service_a = _review_service(solo_env, workflow, diagnosis)
    service_b = _review_service(solo_env, workflow, diagnosis)
    decision = TeacherReviewDecision(
        workflow_id=workflow_id,
        thread_id=thread_id,
        answer_id=solo_env.paper.subjective_answer_ids[0],
        review_status=ReviewStatus.CONFIRMED.value,
        expected_review_status=ReviewStatus.PENDING_REVIEW.value,
    )
    barrier = threading.Barrier(2)

    def submit(service: ReviewService) -> str:
        barrier.wait()
        try:
            service.submit_decision(
                decision,
                actor_id=solo_env.teacher_id,
                actor_role=UserRole.TEACHER,
                comment="并发确认。",
            )
        except (ReviewConflictError, ReviewStaleDecisionError):
            return "conflict"
        return "success"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(submit, (service_a, service_b)))

    assert sorted(outcomes) == ["conflict", "success"]
    with Session(solo_env.engine) as session:
        records = list(session.scalars(select(ReviewRecord)))
        assert len(records) == 1
        assert records[0].decision is ReviewStatus.CONFIRMED
        row = session.scalars(select(GradingResultRow)).one()
        assert row.review_status is ReviewStatus.CONFIRMED


__all__ = ["test_mixed_submission_runs_real_graph_and_keeps_objective_deterministic"]
