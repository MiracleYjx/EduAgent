"""T074 复核服务的失败优先单元测试。

TCR（测试契约记录）：

- 必要性：教师复核是唯一解除人工复核的事实（B01），因此“写入结论 → 恢复运行 → 重汇总 → 落库结果
  与诊断”的每一步都必须可核验：权限、身份、防陈旧、修订结果校验、待复核时不得落库部分成绩、
  Reviewer 的 ``regrade`` 必须由教师显式请求。
- 契约依据：``.specify/plan.md`` §5/§5.2、``.specify/data-model.md``（ReviewRecord、WorkflowRun
  状态转换）、FR-035~FR-038、``.specify/contracts/agent-workflow.md``，以及 T072 已固定的
  ``TeacherReviewDecision``/``apply_teacher_decision_async`` 契约与 T073 检查点接口。
- 覆盖行为：``Confirmed``/``Modified``/``Re-grade`` 三种结论；陈旧状态、跨工作流、跨答案与缺修订结果
  的拒绝；教师确认后触发 T054 重汇总与 T061 诊断；未形成最终成绩时不落库、不生成诊断；依赖未接线
  时显式报未就绪；同步入口在事件循环内显式报错。

测试使用真实 T072 图（内存检查点）+ 真实 T073 检查点存储（启用外键的内存 SQLite）+ 真实 T054
汇总器；只替身 Agent 评分、诊断 Provider 与 T060/T061 落库入口（其结果归属边界不在本批内）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.orm import Session

from backend.app.ai.agents.invocation import AgentInvocation
from backend.app.ai.agents.state import AgentOutput, AgentStatus, AgentType
from backend.app.ai.workflows.grading_handoff import PENDING_REVIEW
from backend.app.ai.workflows.grading_workflow import (
    GradingWorkflow,
    GradingWorkflowDeps,
    TeacherReviewDecision,
)
from backend.app.domain.enums import (
    GradingStatus,
    QuestionType,
    ReviewStatus,
    UserRole,
    ValidationStatus,
    WorkflowStatus,
)
from backend.app.models import GradingResult as GradingResultRow
from backend.app.models import ReviewRecord
from backend.app.schemas.ai import GradingResult
from backend.app.schemas.grading import (
    ConfidenceDecisionDTO,
    DiagnosisReportDTO,
    DiagnosisStatus,
    ExamResultDTO,
)
from backend.app.services.grading.grading_task_service import (
    GradingTargetAnswer,
    SubmissionSnapshot,
)
from backend.app.services.review_service import (
    REVIEW_SERVICE_ASYNC_REQUIRED,
    REVIEW_SERVICE_IDENTITY_MISMATCH,
    REVIEW_SERVICE_INVALID_DECISION,
    REVIEW_SERVICE_NOT_READY,
    REVIEW_SERVICE_PERMISSION_DENIED,
    REVIEW_SERVICE_REVISION_REQUIRED,
    REVIEW_SERVICE_STALE_DECISION,
    REVIEW_SERVICE_WORKFLOW_NOT_FOUND,
    DatabaseReviewRecordStore,
    ReviewAnswerNotFoundError,
    ReviewIdentityError,
    ReviewInvalidDecisionError,
    ReviewPermissionError,
    ReviewRevisionRequiredError,
    ReviewService,
    ReviewServiceNotReadyError,
    ReviewStaleDecisionError,
    ReviewWorkflowNotFoundError,
)
from backend.app.services.workflow_checkpoint import WorkflowCheckpointStore
from tests.unit.models.sqlite_support import (
    SubmissionFixture,
    create_sqlite_engine,
    seed_submission,
)
from tests.unit.settings_helpers import build_test_settings

WORKFLOW_ID = "workflow-t074"
REQUEST_ID = "request-t074"
THREAD_ID = "thread-t074"
PAUSE_REASON_PREFIX = "第 "


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """在同步用例中驱动异步入口；与仓库既有测试约定一致。"""

    return asyncio.run(coroutine)


@dataclass(frozen=True, slots=True)
class ReviewEnv:
    """本次测试的内存数据库、会话与答卷夹具。"""

    engine: Any
    session: Session
    fixture: SubmissionFixture

    @property
    def owner_id(self) -> str:
        """教师（答卷所属课程创建者）标识。"""

        return str(self.fixture.teacher_id)

    @property
    def subjective_answer_id(self) -> str:
        """主观题答案标识。"""

        return str(self.fixture.subjective_answer_id)

    @property
    def objective_answer_id(self) -> str:
        """客观题答案标识。"""

        return str(self.fixture.objective_answer_id)


@pytest.fixture()
def env() -> Any:
    """构造答卷、评分行与内存 SQLite；评分行代表教师复核前的落库事实。"""

    engine = create_sqlite_engine()
    session = Session(engine)
    fixture = seed_submission(session)
    session.add(
        GradingResultRow(
            answer_id=fixture.objective_answer_id,
            submission_id=fixture.submission_id,
            question_type=QuestionType.SINGLE_CHOICE,
            score=Decimal("10.00"),
            max_score=Decimal("10.00"),
            reason="客观题与标准答案一致。",
            knowledge_points=["数据类型"],
            confidence=1.0,
            validation_status=ValidationStatus.VALIDATED,
            review_status=ReviewStatus.NOT_REQUIRED,
        )
    )
    session.add(
        GradingResultRow(
            answer_id=fixture.subjective_answer_id,
            submission_id=fixture.submission_id,
            question_type=QuestionType.SHORT_ANSWER,
            score=Decimal("6.00"),
            max_score=Decimal("10.00"),
            reason="说明了变量的作用。",
            correct_points=["保存数据"],
            missing_knowledge_points=["引用数据"],
            knowledge_points=["变量"],
            suggestions=["补充变量引用。"],
            confidence=0.3,
            validation_status=ValidationStatus.VALIDATED,
            review_status=ReviewStatus.PENDING_REVIEW,
            decision_confidence=0.3,
            decision_threshold=0.8,
            decision_requires_review=True,
            decision_review_status=ReviewStatus.PENDING_REVIEW,
            decision_grading_status=GradingStatus.PENDING_REVIEW,
            decision_reason="置信度低于阈值，已进入人工复核队列。",
        )
    )
    session.commit()
    try:
        yield ReviewEnv(engine=engine, session=session, fixture=fixture)
    finally:
        session.close()
        engine.dispose()


# ---------------------------------------------------------------- 测试替身


class _StubGradingAgent:
    """逐题返回预置评分输出；重评时改用 ``later_confidence``。"""

    def __init__(
        self,
        *,
        submission_id: str,
        subjective_score: float = 6.0,
        subjective_confidence: float = 0.3,
        later_confidence: float | None = None,
    ) -> None:
        self.submission_id = submission_id
        self.subjective_score = subjective_score
        self.subjective_confidence = subjective_confidence
        self.later_confidence = later_confidence
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
            result = _grading_result(
                target,
                self.submission_id,
                score=10.0,
                review_status=ReviewStatus.NOT_REQUIRED.value,
                confidence=1.0,
            )
            decision = _decision(confidence=1.0, requires_review=False)
        else:
            confidence = self.subjective_confidence
            if repeat > 0 and self.later_confidence is not None:
                confidence = self.later_confidence
            requires_review = confidence < 0.8
            result = _grading_result(
                target,
                self.submission_id,
                score=self.subjective_score,
                review_status=(
                    ReviewStatus.PENDING_REVIEW.value
                    if requires_review
                    else ReviewStatus.NOT_REQUIRED.value
                ),
                confidence=confidence,
            )
            decision = _decision(confidence=confidence, requires_review=requires_review)
        return AgentInvocation(
            request_id=request_id,
            workflow_id=workflow_id,
            output=AgentOutput(
                agent_type=AgentType.GRADING,
                status=(
                    AgentStatus.PENDING_REVIEW
                    if decision.requires_review
                    else AgentStatus.SUCCESS
                ),
                validation_status=ValidationStatus.VALIDATED,
                question_type=target.question_type,
                grading_result=result,
                confidence=result.confidence,
                confidence_decision=decision,
                requires_review=decision.requires_review,
            ),
        )

    def score(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("图必须逐题调用 grade_answer_async，不得调用整卷 score。")


class _StubDiagnosisService:
    """T072 诊断节点依赖的诊断服务替身（只用于让图走到完成态）。"""

    def __init__(self) -> None:
        self.calls: list[ExamResultDTO] = []

    async def generate(self, exam_result: ExamResultDTO) -> DiagnosisReportDTO:
        self.calls.append(exam_result)
        return DiagnosisReportDTO(
            submission_id=exam_result.submission_id,
            student_id=exam_result.student_id,
            status=DiagnosisStatus.READY,
            generated_at=datetime.now(UTC),
            source_exam_result_updated_at=exam_result.aggregated_at,
        )


class _AccessDeniedError(RuntimeError):
    """答卷读取器在教师无课程归属时抛出的错误（T060 边界替身）。"""


class _RecordingReader:
    """答卷快照读取替身：记录调用并拒绝非所属教师。"""

    def __init__(self, snapshot: SubmissionSnapshot, owner_id: str) -> None:
        self.snapshot = snapshot
        self.owner_id = owner_id
        self.calls: list[tuple[str, str]] = []
        self.override: SubmissionSnapshot | None = None

    def load(self, submission_id: str) -> SubmissionSnapshot:
        return self.snapshot

    def load_for_teacher(self, submission_id: str, teacher_id: str) -> SubmissionSnapshot:
        self.calls.append((submission_id, teacher_id))
        if teacher_id != self.owner_id:
            raise _AccessDeniedError("教师不属于该答卷所属课程。")
        return self.override if self.override is not None else self.snapshot


class _RecordingResultWriter:
    """T060 最终结果写入替身：记录落库调用。"""

    def __init__(self) -> None:
        self.saved: list[ExamResultDTO] = []

    def save_exam_result(self, exam_result: ExamResultDTO) -> None:
        self.saved.append(exam_result)


class _RecordingDiagnosisRecorder:
    """T061 诊断生成与落库替身：记录调用。"""

    def __init__(self) -> None:
        self.calls: list[ExamResultDTO] = []

    def record(self, exam_result: ExamResultDTO) -> DiagnosisReportDTO:
        self.calls.append(exam_result)
        return DiagnosisReportDTO(
            submission_id=exam_result.submission_id,
            student_id=exam_result.student_id,
            status=DiagnosisStatus.READY,
            generated_at=datetime.now(UTC),
            source_exam_result_updated_at=exam_result.aggregated_at,
        )


# ---------------------------------------------------------------- 构造辅助


def _grading_result(
    target: GradingTargetAnswer,
    submission_id: str,
    *,
    score: float,
    review_status: str,
    confidence: float,
    answer_id: str | None = None,
) -> GradingResult:
    """构造与题目目标一致的评分结果。"""

    return GradingResult(
        question_type=target.question_type,
        score=score,
        max_score=float(target.max_score),
        reason="说明了变量的作用。",
        correct_points=["保存数据"],
        missing_knowledge_points=["引用数据"],
        knowledge_points=list(target.knowledge_points),
        suggestions=["补充变量引用。"],
        confidence=confidence,
        validation_status=ValidationStatus.VALIDATED.value,
        review_status=review_status,
        answer_id=answer_id if answer_id is not None else target.answer_id,
        submission_id=submission_id,
    )


def _decision(*, confidence: float, requires_review: bool) -> ConfidenceDecisionDTO:
    """构造本次置信度决策快照。"""

    return ConfidenceDecisionDTO(
        confidence=confidence,
        threshold=0.8,
        requires_review=requires_review,
        review_status=(
            ReviewStatus.PENDING_REVIEW.value
            if requires_review
            else ReviewStatus.NOT_REQUIRED.value
        ),
        grading_status=(
            GradingStatus.PENDING_REVIEW.value if requires_review else GradingStatus.ACCEPTED.value
        ),
        reason=(
            "置信度低于阈值，已进入人工复核队列。"
            if requires_review
            else "置信度达到阈值，自动接受。"
        ),
    )


def _snapshot(env: ReviewEnv) -> SubmissionSnapshot:
    """构造与答卷夹具一致的权威快照。"""

    return SubmissionSnapshot(
        submission_id=str(env.fixture.submission_id),
        exam_id=str(env.fixture.exam_id),
        student_id=str(env.fixture.student_id),
        course_id=str(env.fixture.course_id),
        status="Submitted",
        answers=(
            GradingTargetAnswer(
                order=1,
                answer_id=env.objective_answer_id,
                question_id=str(env.fixture.objective_question_id),
                question_type=QuestionType.SINGLE_CHOICE,
                max_score=Decimal("10.00"),
                knowledge_points=("数据类型",),
                content="下列哪个是不可变类型？",
                reference_answer="tuple",
                student_answer="tuple",
            ),
            GradingTargetAnswer(
                order=2,
                answer_id=env.subjective_answer_id,
                question_id=str(env.fixture.subjective_question_id),
                question_type=QuestionType.SHORT_ANSWER,
                max_score=Decimal("10.00"),
                knowledge_points=("变量",),
                content="解释变量的作用。",
                reference_answer="变量用于保存数据。",
                scoring_rubric="说明保存和引用数据即可。",
                student_answer="变量用于保存数据。",
            ),
        ),
    )


def _workflow(
    snapshot: SubmissionSnapshot,
    *,
    agent: Any,
    diagnosis: Any,
    checkpointer: Any,
) -> GradingWorkflow:
    """构造 T072 图工作流：注入内存检查点，使暂停与恢复真实发生。"""

    deps = GradingWorkflowDeps(
        snapshot=snapshot,
        agent=agent,
        diagnosis_service=diagnosis,
        settings=build_test_settings(confidence_threshold=0.8),
    )
    return GradingWorkflow(deps, checkpointer=checkpointer)


def _checkpoint_store(env: ReviewEnv) -> WorkflowCheckpointStore:
    """使用独立会话读写检查点（与会话工厂用法一致）。"""

    return WorkflowCheckpointStore(session_factory=lambda: Session(env.engine))


def _service(
    env: ReviewEnv,
    *,
    workflow: GradingWorkflow | None,
    reader: Any = None,
    writer: Any = None,
    diagnosis: Any = None,
    review_records: Any = None,
) -> ReviewService:
    """构造复核服务；默认接线真实的检查点存储与复核记录存储。"""

    return ReviewService(
        checkpoints=_checkpoint_store(env),
        reader=reader if reader is not None else _RecordingReader(_snapshot(env), env.owner_id),
        workflow_provider=(None if workflow is None else lambda run, state: workflow),
        result_writer=writer,
        diagnosis=diagnosis,
        review_records=(
            review_records
            if review_records is not None
            else DatabaseReviewRecordStore(session_factory=lambda: Session(env.engine))
        ),
    )


def _pause_review(env: ReviewEnv, workflow: GradingWorkflow, agent: Any) -> dict[str, Any]:
    """运行到待复核暂停，并把暂停状态落库为检查点（T076 启动入口的职责替身）。"""

    snapshot = _snapshot(env)
    paused = _run(
        workflow.run_async(
            request_id=REQUEST_ID,
            workflow_id=WORKFLOW_ID,
            submission_id=snapshot.submission_id,
            thread_id=THREAD_ID,
        )
    )
    assert paused.interrupted is True
    assert agent.calls.count(env.subjective_answer_id) == 1
    state = dict(paused.state)
    assert state["status"] is WorkflowStatus.PAUSED
    _checkpoint_store(env).save_checkpoint(
        WORKFLOW_ID,
        state,
        PENDING_REVIEW,
        pause_reason=state["pause_reason"],
        thread_id=THREAD_ID,
    )
    return state


def _decision_payload(
    env: ReviewEnv,
    *,
    review_status: str,
    revised_result: GradingResult | None = None,
    expected_review_status: str | None = ReviewStatus.PENDING_REVIEW.value,
    thread_id: str = THREAD_ID,
    workflow_id: str = WORKFLOW_ID,
    answer_id: str | None = None,
) -> TeacherReviewDecision:
    """构造教师决策载荷。"""

    return TeacherReviewDecision(
        workflow_id=workflow_id,
        thread_id=thread_id,
        answer_id=answer_id if answer_id is not None else env.subjective_answer_id,
        review_status=review_status,
        revised_result=revised_result,
        expected_review_status=expected_review_status,
    )


# ---------------------------------------------------------------- 用例


def test_confirmed_decision_completes_and_persists_results(env: ReviewEnv) -> None:
    """``Confirmed``：恢复原运行 → 重汇总 → 落库整卷结果 → 触发诊断 → 同步检查点。"""

    snapshot = _snapshot(env)
    agent = _StubGradingAgent(submission_id=snapshot.submission_id)
    workflow = _workflow(
        snapshot,
        agent=agent,
        diagnosis=_StubDiagnosisService(),
        checkpointer=InMemorySaver(),
    )
    _pause_review(env, workflow, agent)
    writer = _RecordingResultWriter()
    diagnosis = _RecordingDiagnosisRecorder()
    reader = _RecordingReader(snapshot, env.owner_id)
    service = _service(
        env, workflow=workflow, reader=reader, writer=writer, diagnosis=diagnosis
    )

    outcome = service.submit_decision(
        _decision_payload(env, review_status=ReviewStatus.CONFIRMED.value),
        actor_id=env.owner_id,
        actor_role=UserRole.TEACHER,
        comment="分数与理由一致，确认。",
    )

    assert outcome.decision is ReviewStatus.CONFIRMED
    assert outcome.workflow_status is WorkflowStatus.COMPLETED
    assert outcome.interrupted is False
    assert outcome.pending_answer_ids == ()
    assert outcome.resumable is False
    assert outcome.exam_result is not None and outcome.exam_result.is_final is True
    assert outcome.exam_result_persisted is True
    assert writer.saved == [outcome.exam_result]
    assert diagnosis.calls == [outcome.exam_result]
    assert outcome.diagnosis is not None and outcome.diagnosis.status is DiagnosisStatus.READY
    # 教师访问校验按答卷与操作者执行。
    assert reader.calls == [(str(env.fixture.submission_id), env.owner_id)]
    # 复核记录保留操作者与复核前后事实。
    record = env.session.get(ReviewRecord, UUID(outcome.review_record_id))
    assert record is not None
    assert record.reviewer_id == env.fixture.teacher_id
    assert record.decision is ReviewStatus.CONFIRMED
    assert record.original_score == Decimal("6.00")
    assert record.final_score == Decimal("6.00")
    assert record.comment == "分数与理由一致，确认。"
    # 检查点同步为终态，并关联最终整卷结果。
    row = _checkpoint_store(env).load_checkpoint(WORKFLOW_ID)
    assert row is not None
    assert row.status is WorkflowStatus.COMPLETED
    assert row.resumable is False
    assert row.pause_reason is None
    assert _checkpoint_store(env).restore_state(row)["status"] is WorkflowStatus.COMPLETED


def test_modified_decision_uses_validated_revised_result(env: ReviewEnv) -> None:
    """``Modified``：修订分数进入最终成绩与复核记录，且必须经校验。"""

    snapshot = _snapshot(env)
    agent = _StubGradingAgent(submission_id=snapshot.submission_id)
    workflow = _workflow(
        snapshot,
        agent=agent,
        diagnosis=_StubDiagnosisService(),
        checkpointer=InMemorySaver(),
    )
    _pause_review(env, workflow, agent)
    writer = _RecordingResultWriter()
    diagnosis = _RecordingDiagnosisRecorder()
    service = _service(env, workflow=workflow, writer=writer, diagnosis=diagnosis)
    revised = _grading_result(
        _snapshot(env).answers[1],
        snapshot.submission_id,
        score=9.0,
        review_status=ReviewStatus.NOT_REQUIRED.value,
        confidence=0.95,
    )

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
    assert outcome.exam_result is not None and outcome.exam_result.is_final is True
    assert outcome.exam_result.final_total_score == Decimal("19.00")
    item = next(
        item for item in outcome.exam_result.items if item.answer_id == env.subjective_answer_id
    )
    assert item.score == Decimal("9.00")
    record = env.session.get(ReviewRecord, UUID(outcome.review_record_id))
    assert record is not None
    assert record.decision is ReviewStatus.MODIFIED
    assert record.original_score == Decimal("6.00")
    assert record.final_score == Decimal("9.00")
    assert record.final_reason == revised.reason
    assert record.final_knowledge_points == ["变量"]


def test_regrade_decision_returns_to_grading_without_closing_review(env: ReviewEnv) -> None:
    """``Re-grade``：回到评分节点重评，但不得自动解除人工复核，也不落库部分成绩。"""

    snapshot = _snapshot(env)
    agent = _StubGradingAgent(
        submission_id=snapshot.submission_id,
        subjective_confidence=0.3,
        later_confidence=0.95,
    )
    workflow = _workflow(
        snapshot,
        agent=agent,
        diagnosis=_StubDiagnosisService(),
        checkpointer=InMemorySaver(),
    )
    _pause_review(env, workflow, agent)
    writer = _RecordingResultWriter()
    diagnosis = _RecordingDiagnosisRecorder()
    service = _service(env, workflow=workflow, writer=writer, diagnosis=diagnosis)

    outcome = service.request_regrade(
        WORKFLOW_ID,
        THREAD_ID,
        env.subjective_answer_id,
        actor_id=env.owner_id,
        actor_role=UserRole.TEACHER,
        expected_review_status=ReviewStatus.PENDING_REVIEW.value,
        comment="理由与知识点不完整，请重评。",
    )

    # Reviewer 的建议不解除复核：重评后仍停在待复核，等待教师结论。
    assert outcome.decision is ReviewStatus.RE_GRADE
    assert agent.calls.count(env.subjective_answer_id) == 2
    assert outcome.workflow_status is WorkflowStatus.PAUSED
    assert outcome.interrupted is True
    assert outcome.pending_answer_ids == (env.subjective_answer_id,)
    # H02：本用例的图使用内存检查点，运行记录中没有持久 runtime 检查点，
    # 因此检查点存储不再声明可恢复（修复 1 收紧了 resumable 的写入条件）。
    assert outcome.resumable is False
    assert outcome.exam_result is None
    assert outcome.exam_result_persisted is False
    assert writer.saved == []
    assert diagnosis.calls == []
    record = env.session.get(ReviewRecord, UUID(outcome.review_record_id))
    assert record is not None
    assert record.decision is ReviewStatus.RE_GRADE
    assert record.final_score is None
    assert record.final_reason is None
    row = _checkpoint_store(env).load_checkpoint(WORKFLOW_ID)
    assert row is not None
    assert row.status is WorkflowStatus.PAUSED
    # 同上：内存检查点不构成持久恢复支撑，检查点不再声明可恢复。
    assert row.resumable is False
    assert row.pause_reason is not None


def test_stale_and_invalid_decisions_are_rejected_before_side_effects(env: ReviewEnv) -> None:
    """陈旧状态、缺修订结果、非法状态与越权角色都必须在产生副作用前拒绝。"""

    snapshot = _snapshot(env)
    agent = _StubGradingAgent(submission_id=snapshot.submission_id)
    workflow = _workflow(
        snapshot,
        agent=agent,
        diagnosis=_StubDiagnosisService(),
        checkpointer=InMemorySaver(),
    )
    _pause_review(env, workflow, agent)
    writer = _RecordingResultWriter()
    service = _service(env, workflow=workflow, writer=writer)
    calls_before = list(agent.calls)

    with pytest.raises(ReviewStaleDecisionError) as stale:
        service.submit_decision(
            _decision_payload(
                env,
                review_status=ReviewStatus.CONFIRMED.value,
                expected_review_status=ReviewStatus.CONFIRMED.value,
            ),
            actor_id=env.owner_id,
            actor_role=UserRole.TEACHER,
        )
    assert stale.value.error_code == REVIEW_SERVICE_STALE_DECISION

    with pytest.raises(ReviewRevisionRequiredError) as missing:
        service.submit_decision(
            _decision_payload(env, review_status=ReviewStatus.MODIFIED.value),
            actor_id=env.owner_id,
            actor_role=UserRole.TEACHER,
        )
    assert missing.value.error_code == REVIEW_SERVICE_REVISION_REQUIRED

    with pytest.raises(ReviewInvalidDecisionError) as unexpected:
        service.submit_decision(
            _decision_payload(
                env,
                review_status=ReviewStatus.CONFIRMED.value,
                revised_result=_grading_result(
                    snapshot.answers[1],
                    snapshot.submission_id,
                    score=9.0,
                    review_status=ReviewStatus.NOT_REQUIRED.value,
                    confidence=0.95,
                ),
            ),
            actor_id=env.owner_id,
            actor_role=UserRole.TEACHER,
        )
    assert unexpected.value.error_code == REVIEW_SERVICE_INVALID_DECISION

    with pytest.raises(ReviewInvalidDecisionError):
        service.submit_decision(
            _decision_payload(
                env,
                review_status=ReviewStatus.CONFIRMED.value,
                expected_review_status=None,
                thread_id="   ",
            ),
            actor_id=env.owner_id,
            actor_role=UserRole.TEACHER,
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

    # 所有拒绝都发生在写入之前：未重评、未写复核记录、未落库结果、检查点保持暂停。
    assert agent.calls == calls_before
    assert writer.saved == []
    assert env.session.query(ReviewRecord).count() == 0
    row = _checkpoint_store(env).load_checkpoint(WORKFLOW_ID)
    assert row is not None
    assert row.status is WorkflowStatus.PAUSED


def test_cross_workflow_and_answer_identity_are_rejected(env: ReviewEnv) -> None:
    """跨工作流、跨答案与线程不一致的写入一律拒绝。"""

    snapshot = _snapshot(env)
    agent = _StubGradingAgent(submission_id=snapshot.submission_id)
    workflow = _workflow(
        snapshot,
        agent=agent,
        diagnosis=_StubDiagnosisService(),
        checkpointer=InMemorySaver(),
    )
    _pause_review(env, workflow, agent)
    service = _service(env, workflow=workflow, writer=_RecordingResultWriter())
    calls_before = list(agent.calls)

    with pytest.raises(ReviewWorkflowNotFoundError) as missing:
        service.submit_decision(
            _decision_payload(
                env,
                review_status=ReviewStatus.CONFIRMED.value,
                workflow_id="workflow-other",
            ),
            actor_id=env.owner_id,
            actor_role=UserRole.TEACHER,
        )
    assert missing.value.error_code == REVIEW_SERVICE_WORKFLOW_NOT_FOUND

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

    with pytest.raises(ReviewIdentityError) as thread:
        service.submit_decision(
            _decision_payload(
                env,
                review_status=ReviewStatus.CONFIRMED.value,
                thread_id="thread-other",
            ),
            actor_id=env.owner_id,
            actor_role=UserRole.TEACHER,
        )
    assert thread.value.error_code == REVIEW_SERVICE_IDENTITY_MISMATCH

    with pytest.raises(ReviewIdentityError):
        service.submit_decision(
            _decision_payload(
                env,
                review_status=ReviewStatus.MODIFIED.value,
                revised_result=_grading_result(
                    snapshot.answers[0],
                    snapshot.submission_id,
                    score=9.0,
                    review_status=ReviewStatus.NOT_REQUIRED.value,
                    confidence=0.95,
                    answer_id=env.objective_answer_id,
                ),
            ),
            actor_id=env.owner_id,
            actor_role=UserRole.TEACHER,
        )

    assert agent.calls == calls_before
    assert env.session.query(ReviewRecord).count() == 0


def test_teacher_without_course_access_is_rejected(env: ReviewEnv) -> None:
    """教师对答卷没有课程归属时，读取器的拒绝必须原样传播（不做绕过分支）。"""

    snapshot = _snapshot(env)
    agent = _StubGradingAgent(submission_id=snapshot.submission_id)
    workflow = _workflow(
        snapshot,
        agent=agent,
        diagnosis=_StubDiagnosisService(),
        checkpointer=InMemorySaver(),
    )
    _pause_review(env, workflow, agent)
    reader = _RecordingReader(snapshot, owner_id=str(uuid4()))
    service = _service(env, workflow=workflow, reader=reader, writer=_RecordingResultWriter())

    with pytest.raises(_AccessDeniedError):
        service.submit_decision(
            _decision_payload(env, review_status=ReviewStatus.CONFIRMED.value),
            actor_id=env.owner_id,
            actor_role=UserRole.TEACHER,
        )

    assert env.session.query(ReviewRecord).count() == 0


def test_snapshot_of_other_submission_is_rejected(env: ReviewEnv) -> None:
    """读取器返回的答卷与检查点记录不一致时拒绝，避免把结论写到别的答卷。"""

    snapshot = _snapshot(env)
    agent = _StubGradingAgent(submission_id=snapshot.submission_id)
    workflow = _workflow(
        snapshot,
        agent=agent,
        diagnosis=_StubDiagnosisService(),
        checkpointer=InMemorySaver(),
    )
    _pause_review(env, workflow, agent)
    reader = _RecordingReader(snapshot, env.owner_id)
    reader.override = SubmissionSnapshot(
        submission_id=str(uuid4()),
        exam_id=snapshot.exam_id,
        student_id=snapshot.student_id,
        course_id=snapshot.course_id,
        status=snapshot.status,
        answers=snapshot.answers,
    )
    service = _service(env, workflow=workflow, reader=reader, writer=_RecordingResultWriter())

    with pytest.raises(ReviewIdentityError):
        service.submit_decision(
            _decision_payload(env, review_status=ReviewStatus.CONFIRMED.value),
            actor_id=env.owner_id,
            actor_role=UserRole.TEACHER,
        )

    assert env.session.query(ReviewRecord).count() == 0


def test_unwired_dependencies_are_reported_not_silently_skipped(env: ReviewEnv) -> None:
    """工作流恢复、最终结果写入与诊断未接线时显式报未就绪。"""

    snapshot = _snapshot(env)
    agent = _StubGradingAgent(submission_id=snapshot.submission_id)
    workflow = _workflow(
        snapshot,
        agent=agent,
        diagnosis=_StubDiagnosisService(),
        checkpointer=InMemorySaver(),
    )
    _pause_review(env, workflow, agent)

    without_workflow = _service(env, workflow=None, writer=_RecordingResultWriter())
    with pytest.raises(ReviewServiceNotReadyError) as not_ready:
        without_workflow.submit_decision(
            _decision_payload(env, review_status=ReviewStatus.CONFIRMED.value),
            actor_id=env.owner_id,
            actor_role=UserRole.TEACHER,
        )
    assert not_ready.value.error_code == REVIEW_SERVICE_NOT_READY
    assert env.session.query(ReviewRecord).count() == 0

    without_writer = _service(env, workflow=workflow, diagnosis=_RecordingDiagnosisRecorder())
    with pytest.raises(ReviewServiceNotReadyError):
        without_writer.submit_decision(
            _decision_payload(env, review_status=ReviewStatus.CONFIRMED.value),
            actor_id=env.owner_id,
            actor_role=UserRole.TEACHER,
        )
    # 结论已写入复核记录，但未形成成绩落库：如实报未就绪，不假装成功。
    assert env.session.query(ReviewRecord).count() == 1


def test_sync_entry_rejects_running_event_loop(env: ReviewEnv) -> None:
    """已处于事件循环内时必须使用异步入口，不得嵌套 ``asyncio.run``。"""

    snapshot = _snapshot(env)
    agent = _StubGradingAgent(submission_id=snapshot.submission_id)
    workflow = _workflow(
        snapshot,
        agent=agent,
        diagnosis=_StubDiagnosisService(),
        checkpointer=InMemorySaver(),
    )
    _pause_review(env, workflow, agent)
    service = _service(env, workflow=workflow, writer=_RecordingResultWriter())

    async def _attempt() -> str:
        with pytest.raises(Exception) as error:
            service.submit_decision(
                _decision_payload(env, review_status=ReviewStatus.CONFIRMED.value),
                actor_id=env.owner_id,
                actor_role=UserRole.TEACHER,
            )
        assert getattr(error.value, "error_code", None) == REVIEW_SERVICE_ASYNC_REQUIRED
        return "rejected"

    assert _run(_attempt()) == "rejected"
