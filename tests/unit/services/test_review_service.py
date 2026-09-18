"""T074 复核服务的失败优先单元测试（原子落库、原图恢复与并发一致性）。

TCR（测试契约记录）：

- **为什么上一版测试不足以证明复核一致性与并发**：上一版只用内存检查点 + 替身写入器，
  因此无法证明 (a) 每次教师结论都真的立即写入 ``ReviewRecord``/当前 ``GradingResult``/当前
  ``ExamResult``（替身只能证明“被调用”，不能证明数据库事实），(b) 恢复用的是**原工作流**的持久
  检查点而不是服务内部重算，(c) 事务内的原子状态比较能挡住并发复核，(d) 诊断失败时最终成绩被保留、
  运行状态如实表达。这些必须用真实 T073 持久 Checkpointer + 真实 T060 写入器 + 真实 T072 图来证伪。
- **本次新增/修改的测试**：
  1. 确认第一题：复核记录、单题结果与当前整卷结果都已落库，整卷仍非最终、待复核计数下降；
     恢复后由原图继续评下一题，已完成题不再评分。
  2. ``Modified``：修订结果进入单题行与整卷结果，``ReviewRecord.original_*`` 保留复核前事实。
  3. ``Re-grade``：从原检查点恢复并只重评目标答案；最新评分与最新置信度决策替换当前事实，
     首次低置信度事实仍可从复核记录追溯；人工复核要求不因置信度升高而自动解除。
  4. 重复提交与幂等重试都不产生第二条复核记录。
  5. 两个独立 Session 处理同一答案：只有一个写入成功，另一个得到明确状态冲突错误。
  6. 最后一题确认后形成最终成绩，并以图内诊断为准（不重复生成）。
  7. 诊断失败（图内可重试、图内不可重试、T061 记录器可重试失败）都保留最终成绩，并进入
     ``Paused + resumable=True`` 或 ``Failed`` 的正确状态。
  8. 依赖未接线（会话工厂、复核记录、结果写入器、工作流恢复、诊断）时返回明确 ``NOT_READY``，
     且不产生任何副作用；内存检查点不支撑持久恢复，因此不声明 ``resumable``。
  9. 权限、陈旧、修订结果与身份守位全部在产生副作用之前拒绝，并断言没有写复核记录。
- **覆盖契约**：``plan.md`` §5/§5.2、``data-model.md``（ReviewRecord/ExamResult/WorkflowRun）、
  FR-035~FR-038、``contracts/agent-workflow.md``、T062 ``ReviewRecord`` 列约束、T072 教师决策与
  中断/恢复契约、T073 检查点与 runtime 检查点、T054 汇总器、T060 结果写入。
- **并发说明**：并发用例在同一份文件型 SQLite 上用两个独立 Session 串行复现“两个请求都已通过预检、
  随后竞争同一行”的顺序；真实 PostgreSQL 上的并行事务未被实测，因此本文件不宣称已完成 PostgreSQL
  并发实测，只证明原子条件更新（``rowcount == 1``）与回滚语义。
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from backend.app.ai.agents.invocation import AgentInvocation
from backend.app.ai.agents.state import AgentOutput, AgentStatus, AgentType
from backend.app.ai.workflows.grading_handoff import (
    LOAD_SUBMISSION,
    PENDING_REVIEW,
)
from backend.app.ai.workflows.grading_workflow import (
    GRADING_WORKFLOW_DIAGNOSIS_FAILED,
    GradingWorkflow,
    GradingWorkflowDeps,
    TeacherReviewDecision,
)
from backend.app.ai.workflows.state import (
    workflow_state_from_checkpoint_payload,
)
from backend.app.core.database import Base
from backend.app.core.retry_policy import ProviderErrorInfo, ProviderExecutionError
from backend.app.domain.enums import (
    AnswerStatus,
    ExamStatus,
    QuestionStatus,
    QuestionType,
    ReviewStatus,
    SubmissionStatus,
    UserRole,
    ValidationStatus,
    WorkflowStatus,
)
from backend.app.models import (
    Answer,
    Course,
    Exam,
    ExamResult,
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
    ConfidenceDecisionDTO,
    DiagnosisReportDTO,
    DiagnosisStatus,
    ExamResultDTO,
    ExamResultStatus,
)
from backend.app.services.grading.grading_repository import DatabaseGradingRepository
from backend.app.services.grading.grading_task_service import (
    DatabaseGradingSubmissionReader,
    GradingPermissionError,
    GradingTargetAnswer,
    SubmissionSnapshot,
)
from backend.app.services.grading.result_aggregator import ResultAggregator
from backend.app.services.review_service import (
    DECISION_RECORDED_PAUSE_REASON,
    REVIEW_SERVICE_CONFLICT,
    REVIEW_SERVICE_NOT_READY,
    REVIEW_SERVICE_PERMISSION_DENIED,
    REVIEW_SERVICE_REVISION_REQUIRED,
    REVIEW_SERVICE_STALE_DECISION,
    REVIEW_SERVICE_WORKFLOW_NOT_FOUND,
    DatabaseReviewRecordStore,
    ReviewAnswerNotFoundError,
    ReviewConflictError,
    ReviewIdentityError,
    ReviewInvalidDecisionError,
    ReviewPermissionError,
    ReviewRevisionRequiredError,
    ReviewService,
    ReviewServiceNotReadyError,
    ReviewStaleDecisionError,
    ReviewWorkflowNotFoundError,
)
from backend.app.services.workflow_checkpoint import (
    RUNTIME_ENVELOPE_KEY,
    DatabaseCheckpointSaver,
    WorkflowCheckpointStore,
    checkpoint_thread_id,
    runtime_has_checkpoint,
)
from tests.unit.settings_helpers import build_test_settings

WORKFLOW_ID = "workflow-t074"
REQUEST_ID = "request-t074"
THREAD_ID = "thread-t074"
FIXED_NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
THRESHOLD = 0.8
#: 参数默认值哨兵：区分“未传参”与“显式传 None（未接线）”。
_DEFAULT: Any = object()
OBJECTIVE_SCORE = Decimal("10.00")
SUBJECTIVE_MAX_SCORE = Decimal("10.00")
SUBJECTIVE_SCORE = 6.0


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """在同步用例中驱动异步入口；与仓库既有测试约定一致。"""

    return asyncio.run(coroutine)


@dataclass(frozen=True, slots=True)
class Paper:
    """一份含 1 道客观题与 2 道主观题的答卷夹具。"""

    teacher_id: UUID
    student_id: UUID
    course_id: UUID
    submission_id: UUID
    objective_question_id: UUID
    first_question_id: UUID
    second_question_id: UUID
    objective_answer_id: UUID
    first_answer_id: UUID
    second_answer_id: UUID


@dataclass(frozen=True, slots=True)
class ReviewEnv:
    """本次测试的文件型 SQLite 环境（独立连接/会话可见同一份数据）。"""

    engine: Engine
    paper: Paper

    @property
    def owner_id(self) -> str:
        return str(self.paper.teacher_id)


def _engine(path: str) -> Engine:
    """构造启用外键约束的文件型 SQLite 引擎，并建好全部业务表。"""

    engine = create_engine(f"sqlite:///{path}")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return engine


def _seed_paper(session: Session) -> Paper:
    """创建教师、课程、1 道客观题与 2 道主观题、考试、答卷与答案。"""

    teacher = User(username="teacher-t074", email="teacher-t074@example.com", password_hash="h")
    teacher.roles.append(Role(name=UserRole.TEACHER))
    student = User(username="student-t074", email="student-t074@example.com", password_hash="h")
    student.roles.append(Role(name=UserRole.STUDENT))
    course = Course(name="Python 基础", creator=teacher)
    objective = Question(
        course=course,
        creator=teacher,
        type=QuestionType.SINGLE_CHOICE,
        content="下列哪个是不可变类型？",
        options=["list", "tuple"],
        reference_answer="tuple",
        knowledge_points=["数据类型"],
        score=OBJECTIVE_SCORE,
        status=QuestionStatus.APPROVED,
    )
    first = Question(
        course=course,
        creator=teacher,
        type=QuestionType.SHORT_ANSWER,
        content="解释变量的作用。",
        reference_answer="变量用于保存数据。",
        scoring_rubric="说明保存和引用数据即可。",
        knowledge_points=["变量"],
        score=SUBJECTIVE_MAX_SCORE,
        status=QuestionStatus.APPROVED,
    )
    second = Question(
        course=course,
        creator=teacher,
        type=QuestionType.SHORT_ANSWER,
        content="解释函数的作用。",
        reference_answer="函数用于复用逻辑。",
        scoring_rubric="说明封装与复用即可。",
        knowledge_points=["函数"],
        score=SUBJECTIVE_MAX_SCORE,
        status=QuestionStatus.APPROVED,
    )
    exam = Exam(
        course=course,
        creator=teacher,
        title="第一章测验",
        questions=[objective, first, second],
        status=ExamStatus.PUBLISHED,
    )
    submission = Submission(exam=exam, student=student, status=SubmissionStatus.SUBMITTED)
    objective_answer = Answer(
        submission=submission,
        question=objective,
        content="tuple",
        status=AnswerStatus.SUBMITTED,
    )
    first_answer = Answer(
        submission=submission,
        question=first,
        content="变量用于保存数据。",
        status=AnswerStatus.SUBMITTED,
    )
    second_answer = Answer(
        submission=submission,
        question=second,
        content="函数用于复用逻辑。",
        status=AnswerStatus.SUBMITTED,
    )
    session.add(submission)
    session.commit()
    return Paper(
        teacher_id=teacher.id,
        student_id=student.id,
        course_id=course.id,
        submission_id=submission.id,
        objective_question_id=objective.id,
        first_question_id=first.id,
        second_question_id=second.id,
        objective_answer_id=objective_answer.id,
        first_answer_id=first_answer.id,
        second_answer_id=second_answer.id,
    )


def _seed_single_subjective(session: Session) -> Paper:
    """创建只含 1 道低置信度主观题的答卷，用于快速到达诊断节点。"""

    teacher = User(username="teacher-solo", email="teacher-solo@example.com", password_hash="h")
    teacher.roles.append(Role(name=UserRole.TEACHER))
    student = User(username="student-solo", email="student-solo@example.com", password_hash="h")
    student.roles.append(Role(name=UserRole.STUDENT))
    course = Course(name="Python 基础", creator=teacher)
    question = Question(
        course=course,
        creator=teacher,
        type=QuestionType.SHORT_ANSWER,
        content="解释变量的作用。",
        reference_answer="变量用于保存数据。",
        scoring_rubric="说明保存和引用数据即可。",
        knowledge_points=["变量"],
        score=SUBJECTIVE_MAX_SCORE,
        status=QuestionStatus.APPROVED,
    )
    exam = Exam(
        course=course,
        creator=teacher,
        title="单题测验",
        questions=[question],
        status=ExamStatus.PUBLISHED,
    )
    submission = Submission(exam=exam, student=student, status=SubmissionStatus.SUBMITTED)
    answer = Answer(
        submission=submission,
        question=question,
        content="变量用于保存数据。",
        status=AnswerStatus.SUBMITTED,
    )
    session.add(submission)
    session.commit()
    return Paper(
        teacher_id=teacher.id,
        student_id=student.id,
        course_id=course.id,
        submission_id=submission.id,
        objective_question_id=question.id,
        first_question_id=question.id,
        second_question_id=question.id,
        objective_answer_id=answer.id,
        first_answer_id=answer.id,
        second_answer_id=answer.id,
    )


@pytest.fixture()
def env(tmp_path: Any) -> Iterator[ReviewEnv]:
    """每个用例使用独立的文件型 SQLite；外键约束真实生效。"""

    engine = _engine(str(tmp_path / "review.db"))
    session = Session(engine)
    paper = _seed_paper(session)
    session.close()
    try:
        yield ReviewEnv(engine=engine, paper=paper)
    finally:
        engine.dispose()


@pytest.fixture()
def solo_env(tmp_path: Any) -> Iterator[ReviewEnv]:
    """单题答卷环境（诊断路径用例）。"""

    engine = _engine(str(tmp_path / "review_solo.db"))
    session = Session(engine)
    paper = _seed_single_subjective(session)
    session.close()
    try:
        yield ReviewEnv(engine=engine, paper=paper)
    finally:
        engine.dispose()


# ---------------------------------------------------------------- 测试替身


class _StubGradingAgent:
    """逐题返回预置评分输出；每题第二次评分可给出不同置信度与分数。"""

    def __init__(
        self,
        *,
        submission_id: str,
        low_confidence_answer_ids: set[str] | None = None,
        regrade: dict[str, tuple[float, float]] | None = None,
    ) -> None:
        self.submission_id = submission_id
        self.low_confidence = low_confidence_answer_ids or set()
        self.regrade = regrade or {}
        self.calls: list[str] = []

    async def grade_answer_async(
        self,
        snapshot: SubmissionSnapshot,
        target: GradingTargetAnswer,
        *,
        request_id: str,
        workflow_id: str | None = None,
        session: Any = None,
        settings: Any = None,
    ) -> AgentInvocation:
        repeat = self.calls.count(target.answer_id)
        self.calls.append(target.answer_id)
        if target.question_type is QuestionType.SINGLE_CHOICE:
            score, confidence = float(OBJECTIVE_SCORE), 1.0
        elif repeat > 0 and target.answer_id in self.regrade:
            score, confidence = self.regrade[target.answer_id]
        else:
            score = SUBJECTIVE_SCORE
            confidence = 0.3 if target.answer_id in self.low_confidence else 0.95
        needs_review = confidence < THRESHOLD
        result = _grading_result(
            target,
            self.submission_id,
            score=score,
            confidence=confidence,
            review_status=(
                ReviewStatus.PENDING_REVIEW.value
                if needs_review
                else ReviewStatus.NOT_REQUIRED.value
            ),
        )
        decision = _decision_dto(confidence=confidence, requires_review=needs_review)
        return AgentInvocation(
            request_id=request_id,
            workflow_id=workflow_id,
            output=AgentOutput(
                agent_type=AgentType.GRADING,
                status=AgentStatus.PENDING_REVIEW if needs_review else AgentStatus.SUCCESS,
                validation_status=ValidationStatus.VALIDATED,
                question_type=target.question_type,
                grading_result=result,
                confidence=confidence,
                confidence_decision=decision,
                requires_review=needs_review,
            ),
        )

    def score(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("图必须逐题调用 grade_answer_async，不得调用整卷 score。")


class _StubDiagnosisService:
    """T072 诊断节点依赖的诊断服务替身：可配置失败。"""

    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[ExamResultDTO] = []

    async def generate(self, exam_result: ExamResultDTO) -> DiagnosisReportDTO:
        self.calls.append(exam_result)
        if self.error is not None:
            raise self.error
        return DiagnosisReportDTO(
            submission_id=exam_result.submission_id,
            student_id=exam_result.student_id,
            status=DiagnosisStatus.READY,
            generated_at=FIXED_NOW,
            source_exam_result_updated_at=exam_result.aggregated_at,
        )


class _RecordingDiagnosisRecorder:
    """T061 诊断记录器替身：可配置失败，并记录调用。"""

    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[ExamResultDTO] = []

    def record(self, exam_result: ExamResultDTO) -> DiagnosisReportDTO:
        self.calls.append(exam_result)
        if self.error is not None:
            raise self.error
        return DiagnosisReportDTO(
            submission_id=exam_result.submission_id,
            student_id=exam_result.student_id,
            status=DiagnosisStatus.READY,
            generated_at=FIXED_NOW,
            source_exam_result_updated_at=exam_result.aggregated_at,
        )


def _grading_result(
    target: GradingTargetAnswer,
    submission_id: str,
    *,
    score: float,
    confidence: float,
    review_status: str,
    answer_id: str | None = None,
) -> GradingResult:
    """构造与题目目标一致的评分结果。"""

    return GradingResult(
        question_type=target.question_type,
        score=score,
        max_score=float(target.max_score),
        reason="说明了要点。",
        correct_points=["要点"],
        missing_knowledge_points=["补充"],
        knowledge_points=list(target.knowledge_points),
        suggestions=["补充要点。"],
        confidence=confidence,
        validation_status=ValidationStatus.VALIDATED.value,
        review_status=review_status,
        answer_id=answer_id if answer_id is not None else target.answer_id,
        submission_id=submission_id,
    )


def _decision_dto(*, confidence: float, requires_review: bool) -> ConfidenceDecisionDTO:
    """构造本次置信度决策快照。"""

    return ConfidenceDecisionDTO(
        confidence=confidence,
        threshold=THRESHOLD,
        requires_review=requires_review,
        review_status=(
            ReviewStatus.PENDING_REVIEW.value
            if requires_review
            else ReviewStatus.NOT_REQUIRED.value
        ),
        grading_status="Pending Review" if requires_review else "Accepted",
        reason="置信度低于阈值，已进入人工复核队列。" if requires_review else "自动接受。",
    )


# ---------------------------------------------------------------- 环境辅助


def _snapshot(env: ReviewEnv) -> SubmissionSnapshot:
    """由答卷夹具构造权威快照（题序与题型与题目定义一致）。"""

    return DatabaseGradingSubmissionReader(
        session_factory=lambda: Session(env.engine)
    ).load(str(env.paper.submission_id))


def _store(env: ReviewEnv) -> WorkflowCheckpointStore:
    """构造检查点存储（独立会话，模拟请求作用域之外的调用方）。"""

    return WorkflowCheckpointStore(
        session_factory=lambda: Session(env.engine), clock=lambda: FIXED_NOW
    )


def _saver(env: ReviewEnv) -> DatabaseCheckpointSaver:
    """构造持久化 LangGraph Checkpointer（真实跨实例持久恢复支撑）。"""

    return DatabaseCheckpointSaver(
        session_factory=lambda: Session(env.engine), clock=lambda: FIXED_NOW
    )


def _workflow(
    env: ReviewEnv,
    *,
    agent: Any,
    checkpointer: Any,
    diagnosis_service: Any = _DEFAULT,
) -> GradingWorkflow:
    """构造 T072 图工作流，注入给定 Checkpointer；诊断服务可显式传 None 表示未接线。"""

    deps = GradingWorkflowDeps(
        snapshot=_snapshot(env),
        agent=agent,
        diagnosis_service=(
            _StubDiagnosisService() if diagnosis_service is _DEFAULT else diagnosis_service
        ),
        settings=build_test_settings(confidence_threshold=THRESHOLD),
    )
    return GradingWorkflow(deps, checkpointer=checkpointer)


def _persist_current_results(env: ReviewEnv, state: dict[str, Any]) -> ExamResultDTO:
    """模拟 T076 启动入口：把暂停时的整卷结果落库（T060 边界），供复核记录引用评分行。"""

    snapshot = _snapshot(env)
    results: list[GradingResult] = []
    for target in snapshot.answers:
        item = (state.get("grading_results") or {}).get(target.answer_id)
        if isinstance(item, GradingResult):
            results.append(item)
    decisions = {
        str(answer_id): item
        for answer_id, item in (state.get("confidence_decisions") or {}).items()
        if isinstance(item, ConfidenceDecisionDTO)
    }
    exam_result = ResultAggregator().aggregate(
        snapshot.to_context(),
        results=results,
        decisions=decisions,
    )
    DatabaseGradingRepository(
        session_factory=lambda: Session(env.engine), clock=lambda: FIXED_NOW
    ).save_exam_result(exam_result)
    return exam_result


def _pause_run(
    env: ReviewEnv,
    *,
    agent: _StubGradingAgent,
    saver: DatabaseCheckpointSaver | InMemorySaver,
    diagnosis_service: Any = _DEFAULT,
    persist: bool = True,
) -> tuple[GradingWorkflow, dict[str, Any], ExamResultDTO | None]:
    """运行到待复核暂停，并把业务状态快照（必要时还有整卷结果）落库。"""

    store = _store(env)
    store.save_checkpoint(
        WORKFLOW_ID,
        {
            "workflow_id": WORKFLOW_ID,
            "request_id": REQUEST_ID,
            "submission_id": str(env.paper.submission_id),
            "status": WorkflowStatus.RUNNING,
            "current_node": LOAD_SUBMISSION,
        },
        LOAD_SUBMISSION,
        thread_id=THREAD_ID,
    )
    workflow = _workflow(
        env, agent=agent, checkpointer=saver, diagnosis_service=diagnosis_service
    )
    paused = _run(
        workflow.run_async(
            request_id=REQUEST_ID,
            workflow_id=WORKFLOW_ID,
            submission_id=str(env.paper.submission_id),
            thread_id=THREAD_ID,
        )
    )
    assert paused.interrupted is True
    state = dict(paused.state)
    store.save_checkpoint(
        WORKFLOW_ID,
        state,
        PENDING_REVIEW,
        pause_reason=state["pause_reason"],
        thread_id=THREAD_ID,
    )
    if persist:
        return workflow, state, _persist_current_results(env, state)
    return workflow, state, None


def _service(
    env: ReviewEnv,
    *,
    workflow: GradingWorkflow | None,
    reader: Any = _DEFAULT,
    writer: Any = _DEFAULT,
    diagnosis: Any = _DEFAULT,
    records: Any = _DEFAULT,
    session_factory: Any = _DEFAULT,
) -> ReviewService:
    """构造复核服务；默认接线真实读取器、真实结果写入器与真实复核记录存储。

    显式传 ``None`` 表示该依赖未接线（默认值哨兵只代表“未传参”）。
    """

    engine = env.engine
    return ReviewService(
        checkpoints=_store(env),
        reader=(
            DatabaseGradingSubmissionReader(session_factory=lambda: Session(engine))
            if reader is _DEFAULT
            else reader
        ),
        session_factory=(
            (lambda: Session(engine)) if session_factory is _DEFAULT else session_factory
        ),
        workflow_provider=None if workflow is None else (lambda run, state: workflow),
        result_writer=(
            DatabaseGradingRepository(session_factory=lambda: Session(engine))
            if writer is _DEFAULT
            else writer
        ),
        diagnosis=(
            _RecordingDiagnosisRecorder() if diagnosis is _DEFAULT else diagnosis
        ),
        review_records=(
            DatabaseReviewRecordStore() if records is _DEFAULT else records
        ),
    )


def _paused_answer(env: ReviewEnv) -> str:
    """返回当前暂停等待复核的答案标识（由运行记录给出，不依赖题序假设）。"""

    row = _checkpoint_row(env)
    assert row.current_answer_id is not None
    return str(row.current_answer_id)


def _subjective_answer_ids(env: ReviewEnv) -> list[str]:
    """按权威题序返回主观题答案标识（题序由 T060 读取器给出）。"""

    return [
        item.answer_id
        for item in _snapshot(env).answers
        if item.question_type is QuestionType.SHORT_ANSWER
    ]


def _decision_payload(
    env: ReviewEnv,
    *,
    review_status: str,
    answer_id: str | None = None,
    revised_result: GradingResult | None = None,
    expected_review_status: str | None = ReviewStatus.PENDING_REVIEW.value,
    thread_id: str = THREAD_ID,
    workflow_id: str = WORKFLOW_ID,
) -> TeacherReviewDecision:
    """构造教师决策载荷（默认针对当前暂停等待复核的题目）。"""

    resolved = answer_id if answer_id is not None else _paused_answer(env)
    return TeacherReviewDecision(
        workflow_id=workflow_id,
        thread_id=thread_id,
        answer_id=resolved,
        review_status=review_status,
        revised_result=revised_result,
        expected_review_status=expected_review_status,
    )


def _records(env: ReviewEnv) -> list[ReviewRecord]:
    """读回全部复核记录。"""

    with Session(env.engine) as session:
        return list(session.scalars(select(ReviewRecord)).all())


def _grading_row(env: ReviewEnv, answer_id: str) -> GradingResultRow:
    """读回单题评分行。"""

    with Session(env.engine) as session:
        row = session.scalars(
            select(GradingResultRow).where(
                GradingResultRow.answer_id == UUID(answer_id)
            )
        ).one()
        return row


def _exam_result_row(env: ReviewEnv) -> ExamResult | None:
    """读回整卷结果行。"""

    with Session(env.engine) as session:
        return session.scalars(
            select(ExamResult).where(ExamResult.submission_id == env.paper.submission_id)
        ).one_or_none()


def _checkpoint_row(env: ReviewEnv) -> WorkflowRun:
    """读回工作流运行记录。"""

    with Session(env.engine) as session:
        row = session.scalars(
            select(WorkflowRun).where(WorkflowRun.workflow_id == WORKFLOW_ID)
        ).one()
        return row


def _retryable_provider_error() -> ProviderExecutionError:
    """构造可重试的 Provider 失败（T009 契约）。"""

    return ProviderExecutionError(
        ProviderErrorInfo(
            code="PROVIDER_TIMEOUT",
            message="Provider 超时。",
            attempt_count=3,
            retryable=True,
        )
    )


# ---------------------------------------------------------------- 原子落库


def test_confirmed_decision_persists_record_result_and_pending_counts(env: ReviewEnv) -> None:
    """确认一题：复核记录、单题结果与当前整卷结果立即落库，下一题仍在待复核。"""

    paper = env.paper
    subjectives = _subjective_answer_ids(env)
    agent = _StubGradingAgent(
        submission_id=str(paper.submission_id),
        low_confidence_answer_ids=set(subjectives),
    )
    saver = _saver(env)
    workflow, _, paused_exam = _pause_run(env, agent=agent, saver=saver)
    target = _paused_answer(env)
    pending_next = next(item for item in subjectives if item != target)
    assert paused_exam is not None
    assert paused_exam.is_final is False
    assert paused_exam.pending_review_answer_count == 1
    before = _exam_result_row(env)
    assert before is not None
    assert before.is_final is False

    service = _service(env, workflow=workflow)
    outcome = service.submit_decision(
        _decision_payload(env, review_status=ReviewStatus.CONFIRMED.value),
        actor_id=env.owner_id,
        actor_role=UserRole.TEACHER,
        comment="分数与理由一致，确认。",
    )

    assert outcome.resumed is True
    assert outcome.review_record_id is not None
    # 复核记录：操作者、决定、复核前后事实、备注。
    records = _records(env)
    assert len(records) == 1
    record = records[0]
    assert str(record.id) == outcome.review_record_id
    assert record.reviewer_id == paper.teacher_id
    assert record.decision is ReviewStatus.CONFIRMED
    assert record.original_score == Decimal("6.00")
    assert record.final_score == Decimal("6.00")
    assert record.comment == "分数与理由一致，确认。"
    # 单题结果已更新为教师结论。
    row = _grading_row(env, target)
    assert row.review_status is ReviewStatus.CONFIRMED
    assert row.score == Decimal("6.00")
    # 同一事务内的投影：整卷结果仍然存在且未最终（还有题目待复核）。
    exam_result = _exam_result_row(env)
    assert exam_result is not None
    assert exam_result.is_final is False
    # 仍有待复核/未评分题目时状态为 Pending 或 Pending Review（取决于题序），但绝不为最终。
    assert exam_result.result_status in {
        ExamResultStatus.PENDING,
        ExamResultStatus.PENDING_REVIEW,
    }
    # 该题已计入成绩（教师结论立即生效），下一题仍待复核。
    assert outcome.exam_result is not None
    assert outcome.exam_result.is_final is False
    confirmed_item = next(
        item for item in outcome.exam_result.items if item.answer_id == target
    )
    assert confirmed_item.counted is True
    assert confirmed_item.review_status == ReviewStatus.CONFIRMED.value
    pending_item = next(
        item for item in outcome.exam_result.items if item.answer_id == pending_next
    )
    assert pending_item.requires_review is True
    # 恢复原图后继续评下一题，并再次停在待复核；已完成题目不重复评分。
    assert outcome.workflow_status is WorkflowStatus.PAUSED
    assert outcome.interrupted is True
    assert outcome.pending_answer_ids == (pending_next,)
    assert all(agent.calls.count(answer_id) == 1 for answer_id in set(agent.calls))
    # 业务状态与持久 runtime 检查点一致：暂停可恢复。
    checkpoint = _checkpoint_row(env)
    assert checkpoint.status is WorkflowStatus.PAUSED
    assert checkpoint.pause_reason is not None
    assert runtime_has_checkpoint(checkpoint.checkpoint, THREAD_ID) is True
    assert checkpoint.resumable is True
    assert checkpoint_thread_id(checkpoint) == THREAD_ID


def test_modified_decision_keeps_original_facts_and_applies_revision(env: ReviewEnv) -> None:
    """修改：修订结果进入单题行与整卷结果，复核记录保留复核前事实。"""

    paper = env.paper
    subjectives = _subjective_answer_ids(env)
    agent = _StubGradingAgent(
        submission_id=str(paper.submission_id),
        low_confidence_answer_ids={subjectives[0]},
    )
    saver = _saver(env)
    workflow, state, paused_exam = _pause_run(env, agent=agent, saver=saver)
    target_id = _paused_answer(env)
    assert target_id == subjectives[0]
    assert paused_exam is not None
    assert paused_exam.is_final is False
    snapshot = _snapshot(env)
    target = next(item for item in snapshot.answers if item.answer_id == target_id)
    revised = _grading_result(
        target,
        str(paper.submission_id),
        score=9.0,
        confidence=0.95,
        review_status=ReviewStatus.NOT_REQUIRED.value,
    )
    service = _service(env, workflow=workflow)

    outcome = service.submit_decision(
        _decision_payload(
            env,
            review_status=ReviewStatus.MODIFIED.value,
            revised_result=revised,
        ),
        actor_id=env.owner_id,
        actor_role=UserRole.TEACHER,
    )

    assert outcome.decision is ReviewStatus.MODIFIED
    assert outcome.resumed is True
    records = _records(env)
    assert len(records) == 1
    record = records[0]
    # 复核前事实来自评分行，修订事实来自教师结论。
    assert record.decision is ReviewStatus.MODIFIED
    assert record.original_score == Decimal("6.00")
    assert record.original_reason == state["grading_results"][target_id].reason
    assert record.final_score == Decimal("9.00")
    assert record.final_reason == revised.reason
    assert record.final_knowledge_points == list(revised.knowledge_points)
    # 单题行与整卷结果都用修订后的分数；其余题目自动接受后整卷形成最终成绩。
    row = _grading_row(env, target_id)
    assert row.score == Decimal("9.00")
    assert row.review_status is ReviewStatus.MODIFIED
    exam_result = _exam_result_row(env)
    assert exam_result is not None
    assert exam_result.is_final is True
    assert exam_result.confirmed_subtotal == Decimal("25.00")
    assert outcome.workflow_status is WorkflowStatus.COMPLETED


def test_regrade_resumes_original_graph_and_replaces_latest_decision(env: ReviewEnv) -> None:
    """重评：回到原图评分节点、只重评目标答案，并替换最新置信度决策。"""

    paper = env.paper
    subjectives = _subjective_answer_ids(env)
    target_id = subjectives[0]
    agent = _StubGradingAgent(
        submission_id=str(paper.submission_id),
        low_confidence_answer_ids={target_id},
        regrade={target_id: (8.0, 0.95)},
    )
    saver = _saver(env)
    workflow, _, _ = _pause_run(env, agent=agent, saver=saver)
    assert _paused_answer(env) == target_id
    service = _service(env, workflow=workflow)

    outcome = service.request_regrade(
        WORKFLOW_ID,
        THREAD_ID,
        target_id,
        actor_id=env.owner_id,
        actor_role=UserRole.TEACHER,
        expected_review_status=ReviewStatus.PENDING_REVIEW.value,
        comment="理由与知识点不完整，请重评。",
    )

    # 只重评目标答案：其它答案都没有被再次评分。
    assert agent.calls.count(target_id) == 2
    assert all(
        agent.calls.count(answer_id) == 1
        for answer_id in set(agent.calls) - {target_id}
    )
    assert outcome.decision is ReviewStatus.RE_GRADE
    records = _records(env)
    assert len(records) == 1
    assert records[0].decision is ReviewStatus.RE_GRADE
    assert records[0].final_score is None
    # 首次低置信度事实保留在复核记录中（分数与理由来自重评前的评分行）。
    assert records[0].original_score == Decimal("6.00")
    assert records[0].original_reason == "说明了要点。"
    # 最新评分与最新决策替换当前事实，且人工复核要求未被自动解除。
    checkpoint = _checkpoint_row(env)
    restored = workflow_state_from_checkpoint_payload(checkpoint.checkpoint)
    assert restored["grading_results"][target_id].score == Decimal("8.00")
    latest = restored["confidence_decisions"][target_id]
    assert latest.confidence == 0.95
    assert latest.requires_review is True
    assert latest.review_status == ReviewStatus.PENDING_REVIEW.value
    assert restored["review_status"] == ReviewStatus.PENDING_REVIEW
    assert outcome.workflow_status is WorkflowStatus.PAUSED
    assert outcome.pending_answer_ids == (target_id,)
    assert outcome.resumable is True
    # 重评后的单题行与最新事实一致。
    row = _grading_row(env, target_id)
    assert row.score == Decimal("8.00")
    assert row.review_status is ReviewStatus.PENDING_REVIEW
    assert row.confidence == 0.95
    # 待复核题在单题行上不写“已接受决策”快照（T054/T060 口径）：最新决策事实由图状态保持，
    # 人工复核要求与最新评分均未被自动解除。
    assert row.decision_requires_review is None


def test_repeated_decision_and_retry_do_not_duplicate_records(env: ReviewEnv) -> None:
    """重复提交被陈旧守位拒绝，幂等重试不产生第二条复核记录。"""

    paper = env.paper
    first_answer = str(paper.first_answer_id)
    agent = _StubGradingAgent(
        submission_id=str(paper.submission_id), low_confidence_answer_ids={first_answer}
    )
    saver = _saver(env)
    workflow, _, _ = _pause_run(env, agent=agent, saver=saver)
    service = _service(env, workflow=workflow)
    decision = _decision_payload(env, review_status=ReviewStatus.CONFIRMED.value)

    first = service.submit_decision(decision, actor_id=env.owner_id, actor_role=UserRole.TEACHER)
    assert first.review_record_id is not None
    assert len(_records(env)) == 1

    with pytest.raises(ReviewStaleDecisionError) as stale:
        service.submit_decision(decision, actor_id=env.owner_id, actor_role=UserRole.TEACHER)
    assert stale.value.error_code == REVIEW_SERVICE_STALE_DECISION
    assert len(_records(env)) == 1

    retry = service.resume_recorded_decision(
        decision, actor_id=env.owner_id, actor_role=UserRole.TEACHER
    )
    assert retry.resumed is True
    assert retry.review_record_id is None
    assert len(_records(env)) == 1


def test_concurrent_decisions_only_one_wins(env: ReviewEnv) -> None:
    """两个独立 Session 处理同一答案：只有一次原子状态转换成功，另一个得到冲突错误。"""

    paper = env.paper
    subjectives = _subjective_answer_ids(env)
    agent = _StubGradingAgent(
        submission_id=str(paper.submission_id),
        low_confidence_answer_ids={subjectives[0]},
    )
    saver = _saver(env)
    workflow, _, _ = _pause_run(env, agent=agent, saver=saver)
    target_id = _paused_answer(env)
    assert target_id == subjectives[0]
    service_a = _service(env, workflow=workflow)
    service_b = _service(env, workflow=workflow)
    decision_a = _decision_payload(env, review_status=ReviewStatus.CONFIRMED.value)
    decision_b = _decision_payload(
        env,
        review_status=ReviewStatus.MODIFIED.value,
        revised_result=_grading_result(
            next(item for item in _snapshot(env).answers if item.answer_id == target_id),
            str(paper.submission_id),
            score=9.0,
            confidence=0.95,
            review_status=ReviewStatus.NOT_REQUIRED.value,
        ),
    )

    # B 先完成预检（读到 Pending Review），随后 A 提交并提交事务，B 再尝试写入。
    prepared_b = service_b._prepare(decision_b, actor_id=env.owner_id, actor_role=UserRole.TEACHER)
    assert prepared_b.already_recorded is False
    assert prepared_b.recorded_status == ReviewStatus.PENDING_REVIEW.value
    outcome_a = service_a.submit_decision(
        decision_a, actor_id=env.owner_id, actor_role=UserRole.TEACHER
    )
    assert outcome_a.review_record_id is not None

    with pytest.raises(ReviewConflictError) as conflict:
        service_b._apply_decision(
            prepared_b,
            decision_b,
            actor_id=env.owner_id,
            comment=None,
        )
    assert conflict.value.error_code == REVIEW_SERVICE_CONFLICT

    # 只留下第一次教师决定的那条记录，且评分行没有被覆盖。
    records = _records(env)
    assert len(records) == 1
    assert records[0].decision is ReviewStatus.CONFIRMED
    row = _grading_row(env, target_id)
    assert row.review_status is ReviewStatus.CONFIRMED
    assert row.score == Decimal("6.00")


def test_final_decision_forms_final_result_and_uses_graph_diagnosis(solo_env: ReviewEnv) -> None:
    """最后一题确认后形成最终成绩；诊断以图内重新生成为准，不重复生成。"""

    env = solo_env
    answer_id = str(env.paper.first_answer_id)
    agent = _StubGradingAgent(
        submission_id=str(env.paper.submission_id), low_confidence_answer_ids={answer_id}
    )
    saver = _saver(env)
    graph_diagnosis = _StubDiagnosisService()
    workflow, _, _ = _pause_run(env, agent=agent, saver=saver, diagnosis_service=graph_diagnosis)
    recorder = _RecordingDiagnosisRecorder()
    service = _service(env, workflow=workflow, diagnosis=recorder)

    outcome = service.submit_decision(
        _decision_payload(env, review_status=ReviewStatus.CONFIRMED.value),
        actor_id=env.owner_id,
        actor_role=UserRole.TEACHER,
    )

    assert outcome.workflow_status is WorkflowStatus.COMPLETED
    assert outcome.exam_result is not None and outcome.exam_result.is_final is True
    assert outcome.exam_result.final_total_score == Decimal("6.00")
    exam_result = _exam_result_row(env)
    assert exam_result is not None
    assert exam_result.is_final is True
    assert exam_result.result_status is ExamResultStatus.FINAL
    assert exam_result.final_total_score == Decimal("6.00")
    checkpoint = _checkpoint_row(env)
    assert checkpoint.status is WorkflowStatus.COMPLETED
    assert checkpoint.resumable is False
    assert checkpoint.exam_result_id == exam_result.id
    # 图内诊断已由恢复后的 Generate Diagnosis 节点重新生成，T061 不重复生成。
    assert len(graph_diagnosis.calls) == 1
    assert recorder.calls == []
    assert outcome.diagnosis is not None and outcome.diagnosis.status is DiagnosisStatus.READY
    assert outcome.diagnosis_error_code is None


# ---------------------------------------------------------------- 诊断失败


def test_graph_diagnosis_retryable_failure_keeps_final_result_and_pauses(
    solo_env: ReviewEnv,
) -> None:
    """图内诊断可重试失败：最终成绩保留，运行以 Paused + resumable 表达。"""

    env = solo_env
    answer_id = str(env.paper.first_answer_id)
    agent = _StubGradingAgent(
        submission_id=str(env.paper.submission_id), low_confidence_answer_ids={answer_id}
    )
    saver = _saver(env)
    workflow, _, _ = _pause_run(
        env,
        agent=agent,
        saver=saver,
        diagnosis_service=_StubDiagnosisService(error=_retryable_provider_error()),
    )
    recorder = _RecordingDiagnosisRecorder()
    service = _service(env, workflow=workflow, diagnosis=recorder)

    outcome = service.submit_decision(
        _decision_payload(env, review_status=ReviewStatus.CONFIRMED.value),
        actor_id=env.owner_id,
        actor_role=UserRole.TEACHER,
    )

    assert outcome.exam_result is not None and outcome.exam_result.is_final is True
    assert outcome.workflow_status is WorkflowStatus.PAUSED
    assert outcome.resumable is True
    exam_result = _exam_result_row(env)
    assert exam_result is not None and exam_result.is_final is True
    assert exam_result.final_total_score == Decimal("6.00")
    checkpoint = _checkpoint_row(env)
    assert checkpoint.status is WorkflowStatus.PAUSED
    assert checkpoint.pause_reason is not None
    assert runtime_has_checkpoint(checkpoint.checkpoint, THREAD_ID) is True
    # 图内没有可用诊断，T061 记录器补齐一次（不是空成功，也不是空诊断）。
    assert recorder.calls and len(recorder.calls) == 1


def test_graph_diagnosis_hard_failure_keeps_final_result_and_fails(solo_env: ReviewEnv) -> None:
    """图内诊断不可重试失败：最终成绩保留，运行标记 Failed 且不宣称可恢复。"""

    env = solo_env
    answer_id = str(env.paper.first_answer_id)
    agent = _StubGradingAgent(
        submission_id=str(env.paper.submission_id), low_confidence_answer_ids={answer_id}
    )
    saver = _saver(env)
    workflow, _, _ = _pause_run(
        env,
        agent=agent,
        saver=saver,
        diagnosis_service=_StubDiagnosisService(error=ValueError("诊断服务不可用。")),
    )
    service = _service(env, workflow=workflow)

    outcome = service.submit_decision(
        _decision_payload(env, review_status=ReviewStatus.CONFIRMED.value),
        actor_id=env.owner_id,
        actor_role=UserRole.TEACHER,
    )

    assert outcome.exam_result is not None and outcome.exam_result.is_final is True
    assert outcome.workflow_status is WorkflowStatus.FAILED
    assert outcome.resumable is False
    exam_result = _exam_result_row(env)
    assert exam_result is not None and exam_result.is_final is True
    checkpoint = _checkpoint_row(env)
    assert checkpoint.status is WorkflowStatus.FAILED
    assert checkpoint.resumable is False
    restored = workflow_state_from_checkpoint_payload(checkpoint.checkpoint)
    assert restored["error"] is not None
    assert restored["error"].error_code == GRADING_WORKFLOW_DIAGNOSIS_FAILED


def test_recorder_retryable_failure_marks_paused_with_error_code(solo_env: ReviewEnv) -> None:
    """T061 记录器可重试失败：最终成绩保留，运行进入 Paused 并给出真实错误码。"""

    env = solo_env
    answer_id = str(env.paper.first_answer_id)
    agent = _StubGradingAgent(
        submission_id=str(env.paper.submission_id), low_confidence_answer_ids={answer_id}
    )
    saver = _saver(env)
    # 图内诊断未接线 → 图以“诊断失败且保留结果”结束；T061 记录器再以可重试错误失败。
    workflow, _, _ = _pause_run(env, agent=agent, saver=saver, diagnosis_service=None)
    recorder = _RecordingDiagnosisRecorder(error=_retryable_provider_error())
    service = _service(env, workflow=workflow, diagnosis=recorder)

    outcome = service.submit_decision(
        _decision_payload(env, review_status=ReviewStatus.CONFIRMED.value),
        actor_id=env.owner_id,
        actor_role=UserRole.TEACHER,
    )

    assert outcome.diagnosis is None
    assert outcome.diagnosis_error_code == "PROVIDER_TIMEOUT"
    assert outcome.workflow_status is WorkflowStatus.PAUSED
    assert outcome.resumable is True
    assert outcome.exam_result is not None and outcome.exam_result.is_final is True
    exam_result = _exam_result_row(env)
    assert exam_result is not None and exam_result.is_final is True
    checkpoint = _checkpoint_row(env)
    assert checkpoint.status is WorkflowStatus.PAUSED
    assert checkpoint.pause_reason is not None
    assert checkpoint.resumable is True
    restored = workflow_state_from_checkpoint_payload(checkpoint.checkpoint)
    assert restored["diagnosis"] is None
    assert restored["pause_reason"] is not None


# ---------------------------------------------------------------- 依赖与守位


def test_unwired_dependencies_return_not_ready(env: ReviewEnv) -> None:
    """任一依赖未接线都在产生副作用之前报 NOT_READY，不返回空成功。"""

    paper = env.paper
    first_answer = str(paper.first_answer_id)
    agent = _StubGradingAgent(
        submission_id=str(paper.submission_id), low_confidence_answer_ids={first_answer}
    )
    saver = _saver(env)
    workflow, _, _ = _pause_run(env, agent=agent, saver=saver)
    decision = _decision_payload(env, review_status=ReviewStatus.CONFIRMED.value)
    variants = {
        "workflow": _service(env, workflow=None),
        "records": _service(env, workflow=workflow, records=None),
        "writer": _service(env, workflow=workflow, writer=None),
        "diagnosis": _service(env, workflow=workflow, diagnosis=None),
        "session": _service(env, workflow=workflow, session_factory=None),
    }

    for name, service in variants.items():
        with pytest.raises(ReviewServiceNotReadyError) as error:
            service.submit_decision(decision, actor_id=env.owner_id, actor_role=UserRole.TEACHER)
        assert error.value.error_code == REVIEW_SERVICE_NOT_READY, name
    assert _records(env) == []
    assert agent.calls.count(first_answer) == 1


def test_in_memory_checkpointer_does_not_claim_resumable(env: ReviewEnv) -> None:
    """内存检查点不构成持久恢复支撑：结论可用，但不得声明可恢复。"""

    paper = env.paper
    first_answer = str(paper.first_answer_id)
    agent = _StubGradingAgent(
        submission_id=str(paper.submission_id), low_confidence_answer_ids={first_answer}
    )
    workflow, _, _ = _pause_run(env, agent=agent, saver=InMemorySaver())
    service = _service(env, workflow=workflow)

    outcome = service.submit_decision(
        _decision_payload(env, review_status=ReviewStatus.CONFIRMED.value),
        actor_id=env.owner_id,
        actor_role=UserRole.TEACHER,
    )

    assert outcome.resumed is True
    assert outcome.resumable is False
    checkpoint = _checkpoint_row(env)
    assert checkpoint.resumable is False
    assert RUNTIME_ENVELOPE_KEY not in (checkpoint.checkpoint or {})
    assert len(_records(env)) == 1


def test_guards_reject_before_side_effects(env: ReviewEnv) -> None:
    """权限、陈旧、修订结果与身份守位都在写入之前拒绝，且不留任何副作用。"""

    paper = env.paper
    first_answer = str(paper.first_answer_id)
    agent = _StubGradingAgent(
        submission_id=str(paper.submission_id), low_confidence_answer_ids={first_answer}
    )
    saver = _saver(env)
    workflow, _, _ = _pause_run(env, agent=agent, saver=saver)
    service = _service(env, workflow=workflow)
    calls_before = list(agent.calls)
    target = next(item for item in _snapshot(env).answers if item.answer_id == first_answer)
    revised = _grading_result(
        target,
        str(paper.submission_id),
        score=9.0,
        confidence=0.95,
        review_status=ReviewStatus.NOT_REQUIRED.value,
    )

    with pytest.raises(ReviewPermissionError) as student:
        service.submit_decision(
            _decision_payload(env, review_status=ReviewStatus.CONFIRMED.value),
            actor_id=env.owner_id,
            actor_role=UserRole.STUDENT,
        )
    assert student.value.error_code == REVIEW_SERVICE_PERMISSION_DENIED
    with pytest.raises(ReviewPermissionError):
        service.submit_decision(
            _decision_payload(env, review_status=ReviewStatus.CONFIRMED.value),
            actor_id="   ",
            actor_role=UserRole.TEACHER,
        )
    with pytest.raises(ReviewStaleDecisionError):
        service.submit_decision(
            _decision_payload(
                env,
                review_status=ReviewStatus.CONFIRMED.value,
                expected_review_status=ReviewStatus.CONFIRMED.value,
            ),
            actor_id=env.owner_id,
            actor_role=UserRole.TEACHER,
        )
    with pytest.raises(ReviewRevisionRequiredError) as missing:
        service.submit_decision(
            _decision_payload(env, review_status=ReviewStatus.MODIFIED.value),
            actor_id=env.owner_id,
            actor_role=UserRole.TEACHER,
        )
    assert missing.value.error_code == REVIEW_SERVICE_REVISION_REQUIRED
    with pytest.raises(ReviewInvalidDecisionError):
        service.submit_decision(
            _decision_payload(
                env,
                review_status=ReviewStatus.CONFIRMED.value,
                revised_result=revised,
            ),
            actor_id=env.owner_id,
            actor_role=UserRole.TEACHER,
        )
    with pytest.raises(ReviewInvalidDecisionError):
        service.submit_decision(
            _decision_payload(
                env,
                review_status=ReviewStatus.CONFIRMED.value,
                thread_id="  ",
            ),
            actor_id=env.owner_id,
            actor_role=UserRole.TEACHER,
        )
    with pytest.raises(ReviewWorkflowNotFoundError) as unknown:
        service.submit_decision(
            _decision_payload(
                env,
                review_status=ReviewStatus.CONFIRMED.value,
                workflow_id="workflow-other",
            ),
            actor_id=env.owner_id,
            actor_role=UserRole.TEACHER,
        )
    assert unknown.value.error_code == REVIEW_SERVICE_WORKFLOW_NOT_FOUND
    with pytest.raises(ReviewAnswerNotFoundError):
        service.submit_decision(
            _decision_payload(
                env,
                review_status=ReviewStatus.CONFIRMED.value,
                answer_id=str(uuid4()),
            ),
            actor_id=env.owner_id,
            actor_role=UserRole.TEACHER,
        )
    with pytest.raises(ReviewIdentityError):
        service.submit_decision(
            _decision_payload(
                env,
                review_status=ReviewStatus.CONFIRMED.value,
                thread_id="thread-other",
            ),
            actor_id=env.owner_id,
            actor_role=UserRole.TEACHER,
        )
    with pytest.raises(ReviewIdentityError):
        service.submit_decision(
            _decision_payload(
                env,
                review_status=ReviewStatus.MODIFIED.value,
                revised_result=_grading_result(
                    next(
                        item
                        for item in _snapshot(env).answers
                        if item.answer_id == str(paper.second_answer_id)
                    ),
                    str(paper.submission_id),
                    score=9.0,
                    confidence=0.95,
                    review_status=ReviewStatus.NOT_REQUIRED.value,
                    answer_id=str(paper.second_answer_id),
                ),
            ),
            actor_id=env.owner_id,
            actor_role=UserRole.TEACHER,
        )
    # 真实读取器校验课程归属：陌生教师被拒绝。
    with pytest.raises(GradingPermissionError):
        service.submit_decision(
            _decision_payload(env, review_status=ReviewStatus.CONFIRMED.value),
            actor_id=str(uuid4()),
            actor_role=UserRole.TEACHER,
        )

    assert _records(env) == []
    assert agent.calls == calls_before


def test_missing_grading_row_is_rejected(env: ReviewEnv) -> None:
    """没有评分结果行时不得凭空创建复核记录。"""

    paper = env.paper
    first_answer = str(paper.first_answer_id)
    agent = _StubGradingAgent(
        submission_id=str(paper.submission_id), low_confidence_answer_ids={first_answer}
    )
    saver = _saver(env)
    workflow, _, _ = _pause_run(env, agent=agent, saver=saver, persist=False)
    service = _service(env, workflow=workflow)

    with pytest.raises(ReviewAnswerNotFoundError):
        service.submit_decision(
            _decision_payload(env, review_status=ReviewStatus.CONFIRMED.value),
            actor_id=env.owner_id,
            actor_role=UserRole.TEACHER,
        )

    assert _records(env) == []


def test_resume_failure_is_reported_as_pending_recovery(env: ReviewEnv) -> None:
    """决定已落库但恢复失败：如实返回“已保存、待恢复”，不谎称成功也不写第二条记录。"""

    paper = env.paper
    subjectives = _subjective_answer_ids(env)
    agent = _StubGradingAgent(
        submission_id=str(paper.submission_id),
        low_confidence_answer_ids={subjectives[0]},
    )
    saver = _saver(env)
    workflow, _, paused_exam = _pause_run(env, agent=agent, saver=saver)
    target_id = _paused_answer(env)
    # 真实图已构建并跑到暂停；本次只验证恢复失败时的“已保存、待恢复”语义。
    assert workflow is not None

    class _BrokenWorkflow:
        async def apply_teacher_decision_async(self, decision: Any, *, resume: bool = False) -> Any:
            del decision, resume
            raise RuntimeError("恢复时底层错误。")

    service = _service(env, workflow=_BrokenWorkflow())  # type: ignore[arg-type]
    outcome = service.submit_decision(
        _decision_payload(env, review_status=ReviewStatus.CONFIRMED.value),
        actor_id=env.owner_id,
        actor_role=UserRole.TEACHER,
    )

    assert outcome.resumed is False
    assert outcome.resume_error_code == "RuntimeError"
    assert outcome.review_record_id is not None
    assert len(_records(env)) == 1
    # 同一事务的投影已经落库：该题分数已计入当前成绩，且运行仍为“待恢复”。
    assert paused_exam is not None
    exam_result = _exam_result_row(env)
    assert exam_result is not None
    assert exam_result.confirmed_subtotal == paused_exam.confirmed_subtotal + Decimal("6.00")
    assert _grading_row(env, target_id).review_status is ReviewStatus.CONFIRMED
    checkpoint = _checkpoint_row(env)
    assert checkpoint.status is WorkflowStatus.PAUSED
    assert checkpoint.pause_reason == DECISION_RECORDED_PAUSE_REASON
    restored = workflow_state_from_checkpoint_payload(checkpoint.checkpoint)
    assert restored["pause_reason"] == DECISION_RECORDED_PAUSE_REASON
    # 图的状态事实保持原样（教师结论由恢复过程写入），因此仍显示待复核。
    assert restored["review_status"] == ReviewStatus.PENDING_REVIEW


def test_sync_entry_rejects_running_event_loop(env: ReviewEnv) -> None:
    """已处于事件循环内时必须使用异步入口，不得嵌套 ``asyncio.run``。"""

    paper = env.paper
    first_answer = str(paper.first_answer_id)
    agent = _StubGradingAgent(
        submission_id=str(paper.submission_id), low_confidence_answer_ids={first_answer}
    )
    saver = _saver(env)
    workflow, _, _ = _pause_run(env, agent=agent, saver=saver)
    service = _service(env, workflow=workflow)

    async def _attempt() -> str:
        with pytest.raises(Exception) as error:
            service.submit_decision(
                _decision_payload(env, review_status=ReviewStatus.CONFIRMED.value),
                actor_id=env.owner_id,
                actor_role=UserRole.TEACHER,
            )
        assert getattr(error.value, "error_code", None) == "REVIEW_SERVICE_ASYNC_REQUIRED"
        return "rejected"

    assert _run(_attempt()) == "rejected"
