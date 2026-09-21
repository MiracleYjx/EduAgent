"""T056 生产仓储测试：整批事务、稳定题序、任务状态映射与中断收敛。

TCR（2026-09-16，T056 / B01～B04）：结果保存必须按 ``answer_id`` 就地更新以保留复核记录，
整批产出必须一次事务写入，题序必须来自本次快照，任务状态必须与工作流状态分开表达。

测试使用内存 SQLite（复用 ``tests/unit/models/sqlite_support``）验证仓储行为；
真实 PostgreSQL 上的约束、类型与迁移一致性由隔离验证库上的
``alembic upgrade/check`` 与 pg_catalog 断言覆盖，不由本文件替代。

TCR（B01 修复）：补充提交前中断、提交后重启、异类检查点隔离及历史已保存结果保护，
防止任务终态与评分分开提交，或启动收敛破坏其它执行器和已有成绩。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from backend.app.ai.workflows.state import WORKFLOW_STATE_PAYLOAD_KIND
from backend.app.core.database import create_session_factory
from backend.app.domain.enums import (
    AnswerStatus,
    QuestionType,
    ReviewStatus,
    SubmissionStatus,
    WorkflowStatus,
)
from backend.app.models import (
    Answer,
    Exam,
    ExamResult,
    GradingResult,
    ReviewRecord,
    Submission,
    WorkflowRun,
)
from backend.app.schemas.ai import GradingResult as GradingResultPayload
from backend.app.schemas.grading import (
    ExamResultStatus,
    GradingTaskStatus,
    GradingTaskStatusDTO,
    SubmissionContext,
)
from backend.app.services.grading.confidence_policy import ConfidenceDecision
from backend.app.services.grading.grading_repository import (
    GRADING_TASK_INTERRUPTED,
    DatabaseGradingRepository,
)
from backend.app.services.grading.grading_task_service import (
    GRADING_STORE_NOT_READY,
    GRADING_SUBMISSION_NOT_FOUND,
    GradingOutcome,
    GradingResultOwnershipError,
    GradingStoreNotReadyError,
    GradingSubmissionNotFoundError,
    GradingTaskTraceMissingError,
)
from backend.app.services.grading.result_aggregator import ResultAggregator
from backend.app.services.workflow_checkpoint import WorkflowCheckpointError
from tests.unit.models.sqlite_support import (
    SubmissionFixture,
    create_sqlite_engine,
    seed_submission,
)

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine() -> Iterator[Engine]:
    """内存 SQLite 引擎（启用外键约束并建好全部业务表）。"""

    active = create_sqlite_engine()
    try:
        yield active
    finally:
        active.dispose()


@pytest.fixture
def fixture(engine: Engine) -> SubmissionFixture:
    """一份含客观题与主观题的最小答卷。"""

    with Session(engine) as session:
        return seed_submission(session)


@pytest.fixture
def repository(engine: Engine) -> DatabaseGradingRepository:
    """指向同一 SQLite 库的仓储；每次调用自建会话。"""

    return DatabaseGradingRepository(
        session_factory=lambda: Session(engine),
        clock=lambda: NOW,
    )


def _objective_payload(fixture: SubmissionFixture) -> GradingResultPayload:
    """客观题评分结果（确定性规则，无决策）。"""

    return GradingResultPayload(
        question_type=QuestionType.SINGLE_CHOICE,
        score=10.0,
        max_score=10.0,
        reason="参考答案一致。",
        correct_points=["tuple 是不可变类型"],
        missing_knowledge_points=[],
        knowledge_points=["数据类型"],
        suggestions=["继续保持。"],
        confidence=1.0,
        validation_status="Validated",
        review_status="Not Required",
        retrieved_context_ids=[],
        answer_id=str(fixture.objective_answer_id),
        submission_id=str(fixture.submission_id),
    )


def _subjective_payload(
    fixture: SubmissionFixture,
    *,
    score: float = 6.0,
    confidence: float = 0.5,
    review_status: str = "Pending Review",
) -> GradingResultPayload:
    """主观题评分结果（含是否进入待复核）。"""

    return GradingResultPayload(
        question_type=QuestionType.SHORT_ANSWER,
        score=score,
        max_score=10.0,
        reason="说明了数据保存作用。",
        correct_points=["保存数据"],
        missing_knowledge_points=["引用数据"],
        knowledge_points=["变量"],
        suggestions=["补充变量引用。"],
        confidence=confidence,
        validation_status="Validated",
        review_status=review_status,
        retrieved_context_ids=["chunk-1"],
        answer_id=str(fixture.subjective_answer_id),
        submission_id=str(fixture.submission_id),
    )


def _decision(*, confidence: float = 0.5, requires_review: bool = True) -> ConfidenceDecision:
    """与主观题结果匹配的置信度决策。"""

    return ConfidenceDecision(
        confidence=confidence,
        threshold=0.8,
        requires_review=requires_review,
        review_status="Pending Review" if requires_review else "Not Required",
        grading_status="Pending Review" if requires_review else "Accepted",
        reason="置信度低于阈值，进入待人工复核。",
    )


def _context(fixture: SubmissionFixture) -> SubmissionContext:
    """按考试题序构造汇总上下文。"""

    from backend.app.schemas.grading import ExpectedAnswer

    return SubmissionContext(
        submission_id=str(fixture.submission_id),
        exam_id=str(fixture.exam_id),
        student_id=str(fixture.student_id),
        expected_answers=[
            ExpectedAnswer(
                order=1,
                answer_id=str(fixture.objective_answer_id),
                question_id=str(fixture.objective_question_id),
                question_type=QuestionType.SINGLE_CHOICE,
                max_score=Decimal("10.00"),
                knowledge_points=["数据类型"],
            ),
            ExpectedAnswer(
                order=2,
                answer_id=str(fixture.subjective_answer_id),
                question_id=str(fixture.subjective_question_id),
                question_type=QuestionType.SHORT_ANSWER,
                max_score=Decimal("10.00"),
                knowledge_points=["变量"],
            ),
        ],
    )


def _outcome(
    fixture: SubmissionFixture,
    *,
    subjective: GradingResultPayload | None = None,
    decisions: dict[str, ConfidenceDecision] | None = None,
) -> GradingOutcome:
    """构造一次评分产出（含整卷汇总）。"""

    payloads = [_objective_payload(fixture), subjective or _subjective_payload(fixture)]
    decision_map = decisions if decisions is not None else {
        str(fixture.subjective_answer_id): _decision()
    }
    exam_result = ResultAggregator().aggregate(
        _context(fixture),
        results=payloads,
        decisions=decision_map,
    )
    return GradingOutcome(
        results=tuple(payloads),
        decisions=decision_map,
        exam_result=exam_result,
    )


def _task(
    fixture: SubmissionFixture,
    *,
    task_id: str = "task-1",
    status: GradingTaskStatus = GradingTaskStatus.QUEUED,
    **overrides: object,
) -> GradingTaskStatusDTO:
    """构造任务状态 DTO。"""

    values: dict[str, object] = {
        "task_id": task_id,
        "submission_id": str(fixture.submission_id),
        "status": status,
        "durable": True,
        "created_at": NOW,
    }
    values.update(overrides)
    return GradingTaskStatusDTO(**values)  # type: ignore[arg-type]


def _save_task(
    repository: DatabaseGradingRepository,
    fixture: SubmissionFixture,
    task: GradingTaskStatusDTO,
    *,
    request_id: str = "request-1",
) -> None:
    """写入任务状态；创建时提供追踪标识。"""

    repository.save_task(task, request_id=request_id)


def test_save_outcome_persists_results_and_reads_back_facts(
    engine: Engine,
    fixture: SubmissionFixture,
    repository: DatabaseGradingRepository,
) -> None:
    """整批保存后可从新会话读回单题结果、决策快照与整卷汇总事实。"""

    _save_task(repository, fixture, _task(fixture))
    outcome = _outcome(fixture)
    assert outcome.exam_result is not None
    repository.save_outcome(
        str(fixture.submission_id),
        outcome,
        task_id="task-1",
        answer_order=(
            str(fixture.objective_answer_id),
            str(fixture.subjective_answer_id),
        ),
    )

    with Session(engine) as session:
        assert len(list(session.scalars(select(GradingResult)))) == 2
        assert session.scalars(select(ExamResult)).one().aggregated_at.replace(
            tzinfo=None
        ) == outcome.exam_result.aggregated_at.replace(tzinfo=None)

    stored = repository.get_exam_result(str(fixture.submission_id))
    assert stored is not None
    assert stored.aggregated_at.replace(tzinfo=None) == (
        outcome.exam_result.aggregated_at.replace(tzinfo=None)
    )
    assert stored.total_max_score == Decimal("20.00")
    assert stored.is_final is False
    assert stored.result_status is ExamResultStatus.PENDING_REVIEW
    assert [item.answer_id for item in stored.items] == [
        str(fixture.objective_answer_id),
        str(fixture.subjective_answer_id),
    ]
    pending = stored.items[1]
    assert pending.requires_review is True
    assert pending.counted is False
    assert pending.effective_score is None

    single = repository.get_single_result(
        str(fixture.submission_id), str(fixture.subjective_answer_id)
    )
    assert single is not None
    assert single.confidence == 0.5
    assert single.decision is not None
    assert single.decision.threshold == 0.8
    assert single.decision.requires_review is True
    assert single.submission_id == str(fixture.submission_id)
    assert single.grading_status == "Pending Review"


def test_read_without_checkpoint_uses_degraded_exam_order(
    engine: Engine,
    fixture: SubmissionFixture,
    repository: DatabaseGradingRepository,
) -> None:
    """无检查点时题序退化为考试题目集合顺序，仅保证答案集合与题序编号完整。

    该路径只出现在未经过检查点的显式写入；正式读取依赖检查点题序，
    因此这里不断言具体顺序，只断言不再依赖数据库自然返回顺序导致的缺失。
    """

    repository.save_outcome(str(fixture.submission_id), _outcome(fixture))

    stored = repository.get_exam_result(str(fixture.submission_id))
    assert stored is not None
    with Session(engine) as session:
        expected = {
            str(answer.id)
            for answer in session.scalars(
                select(Answer).where(
                    Answer.submission_id == fixture.submission_id
                )
            )
        }
    assert {item.answer_id for item in stored.items} == expected
    assert [item.order for item in stored.items] == [1, 2]


def test_repeated_save_updates_row_and_keeps_review_record(
    engine: Engine,
    fixture: SubmissionFixture,
    repository: DatabaseGradingRepository,
) -> None:
    """重复保存就地更新评分行：主键不变，既有复核记录与其关联仍存在。"""

    _save_task(repository, fixture, _task(fixture))
    repository.save_outcome(
        str(fixture.submission_id), _outcome(fixture), task_id="task-1"
    )
    with Session(engine) as session:
        row = session.scalars(
            select(GradingResult).where(
                GradingResult.answer_id == fixture.subjective_answer_id
            )
        ).one()
        grading_result_id = row.id
        session.add(
            ReviewRecord(
                grading_result_id=row.id,
                reviewer_id=fixture.teacher_id,
                decision="Modified",
                original_score=Decimal("6.00"),
                original_reason="说明了数据保存作用。",
                original_knowledge_points=["变量"],
                final_score=Decimal("8.00"),
                final_reason="补充正确要点。",
                final_knowledge_points=["变量"],
                comment="教师复核。",
            )
        )
        session.commit()

    repository.save_outcome(
        str(fixture.submission_id),
        _outcome(fixture, subjective=_subjective_payload(fixture, score=9.0)),
    )

    with Session(engine) as session:
        rows = list(session.scalars(select(GradingResult)))
        assert len(rows) == 2
        assert rows[1].id == grading_result_id
        assert rows[1].score == Decimal("9.00")
        records = list(session.scalars(select(ReviewRecord)))
        assert len(records) == 1
        assert records[0].grading_result_id == grading_result_id


def test_save_outcome_rolls_back_when_answer_belongs_to_other_submission(
    engine: Engine,
    fixture: SubmissionFixture,
    repository: DatabaseGradingRepository,
) -> None:
    """跨答卷写入被拒绝，且整批事务回滚：不留“整卷结果 + 缺失单题”的中间态。"""

    with Session(engine) as session:
        exam = session.get(Exam, fixture.exam_id)
        assert exam is not None
        other = Submission(
            exam_id=exam.id,
            student_id=fixture.student_id,
            status=SubmissionStatus.SUBMITTED,
        )
        session.add(other)
        session.flush()
        foreign_answer = Answer(
            submission_id=other.id,
            question_id=fixture.subjective_question_id,
            content="他人答案。",
            status=AnswerStatus.SUBMITTED,
        )
        session.add(foreign_answer)
        session.commit()
        foreign_answer_id = str(foreign_answer.id)

    outcome = _outcome(fixture)
    foreign = _subjective_payload(fixture).model_copy(
        update={"answer_id": foreign_answer_id}
    )
    with pytest.raises(GradingResultOwnershipError):
        repository.save_outcome(
            str(fixture.submission_id),
            GradingOutcome(
                results=(_objective_payload(fixture), foreign),
                decisions=outcome.decisions,
                exam_result=outcome.exam_result,
            ),
        )

    with Session(engine) as session:
        assert list(session.scalars(select(ExamResult))) == []
        assert list(session.scalars(select(GradingResult))) == []
    assert repository.get_exam_result(str(fixture.submission_id)) is None


def test_stored_answer_order_is_reused_on_read(
    engine: Engine,
    fixture: SubmissionFixture,
    repository: DatabaseGradingRepository,
) -> None:
    """读回题序复用检查点快照，而不是数据库自然返回顺序。"""

    _save_task(repository, fixture, _task(fixture))
    repository.save_outcome(
        str(fixture.submission_id),
        _outcome(fixture),
        task_id="task-1",
        answer_order=(
            str(fixture.subjective_answer_id),
            str(fixture.objective_answer_id),
        ),
    )

    stored = repository.get_exam_result(str(fixture.submission_id))
    assert stored is not None
    assert [item.answer_id for item in stored.items] == [
        str(fixture.subjective_answer_id),
        str(fixture.objective_answer_id),
    ]
    assert [item.order for item in stored.items] == [1, 2]
    with Session(engine) as session:
        row = session.scalars(select(WorkflowRun)).one()
        assert (row.checkpoint or {})["answer_order"] == [
            str(fixture.subjective_answer_id),
            str(fixture.objective_answer_id),
        ]


def test_task_state_maps_pending_review_to_paused_workflow(
    engine: Engine,
    fixture: SubmissionFixture,
    repository: DatabaseGradingRepository,
) -> None:
    """存在待复核时任务为已完成、工作流为 Paused 且带原因。"""

    _save_task(repository, fixture, _task(fixture))
    repository.save_outcome(
        str(fixture.submission_id),
        _outcome(fixture),
        task_id="task-1",
        answer_order=(
            str(fixture.objective_answer_id),
            str(fixture.subjective_answer_id),
        ),
    )
    repository.save_task(
        _task(
            fixture,
            status=GradingTaskStatus.COMPLETED,
            started_at=NOW,
            finished_at=NOW,
            expected_answer_count=2,
            graded_answer_count=2,
            pending_review_answer_count=1,
            exam_result_status=ExamResultStatus.PENDING_REVIEW,
            is_final=False,
        )
    )

    stored = repository.get_task("task-1")
    assert stored is not None
    assert stored.status is GradingTaskStatus.COMPLETED
    assert stored.durable is True
    assert stored.pending_review_answer_count == 1
    assert stored.is_final is False
    assert stored.exam_result_status is ExamResultStatus.PENDING_REVIEW
    with Session(engine) as session:
        row = session.scalars(select(WorkflowRun)).one()
        assert row.status.value == "Paused"
        assert row.pause_reason is not None
        assert row.request_id == "request-1"
        assert row.exam_result_id is not None
        checkpoint = row.checkpoint or {}
        assert checkpoint["answer_order"] == [
            str(fixture.objective_answer_id),
            str(fixture.subjective_answer_id),
        ]
        assert checkpoint["task"]["pending_review_answer_count"] == 1


def test_failed_task_marks_answers_failed_in_same_transaction(
    engine: Engine,
    fixture: SubmissionFixture,
    repository: DatabaseGradingRepository,
) -> None:
    """任务失败时保留错误码与 retryable，并把答卷答案标记为失败。"""

    with Session(engine) as session:
        for answer in session.scalars(select(Answer)):
            answer.status = AnswerStatus.SUBMITTED
        session.commit()
    _save_task(repository, fixture, _task(fixture))
    repository.save_task(
        _task(
            fixture,
            status=GradingTaskStatus.FAILED,
            error_code="GRADING_PROVIDER_NOT_READY",
            error_message="评分 Provider 未就绪。",
            retryable=False,
        )
    )

    stored = repository.get_task("task-1")
    assert stored is not None
    assert stored.status is GradingTaskStatus.FAILED
    assert stored.error_code == "GRADING_PROVIDER_NOT_READY"
    assert stored.retryable is False
    with Session(engine) as session:
        assert session.scalars(select(ExamResult)).all() == []
        statuses = {answer.status for answer in session.scalars(select(Answer))}
        assert statuses == {AnswerStatus.FAILED}
        assert session.scalars(select(WorkflowRun)).one().status.value == "Failed"


def test_interrupted_tasks_are_converged_to_failure(
    engine: Engine,
    fixture: SubmissionFixture,
    repository: DatabaseGradingRepository,
) -> None:
    """遗留的进行中任务被收敛为中断失败，且任务可被显式重评重新创建。"""

    _save_task(
        repository,
        fixture,
        _task(fixture, status=GradingTaskStatus.RUNNING, started_at=NOW),
    )

    handled = repository.mark_interrupted_tasks_failed()

    assert handled == 1
    stored = repository.get_task("task-1")
    assert stored is not None
    assert stored.status is GradingTaskStatus.FAILED
    assert stored.error_code == GRADING_TASK_INTERRUPTED
    assert stored.retryable is False
    assert repository.mark_interrupted_tasks_failed() == 0


@pytest.mark.parametrize("kind", ["langgraph", "other-executor", None])
def test_recovery_leaves_foreign_checkpoints_untouched(
    engine: Engine, fixture: SubmissionFixture,
    repository: DatabaseGradingRepository, kind: str | None,
) -> None:
    checkpoint = {"kind": kind, "state": {"node": "review"}}
    with Session(engine) as session:
        session.add(WorkflowRun(
            workflow_id="foreign-task", request_id="foreign-request",
            submission_id=fixture.submission_id, status=WorkflowStatus.RUNNING,
            checkpoint=checkpoint, resumable=True, current_node="review",
        ))
        session.commit()

    assert repository.mark_interrupted_tasks_failed() == 0
    with Session(engine) as session:
        row = session.scalars(select(WorkflowRun)).one()
        assert row.status is WorkflowStatus.RUNNING
        assert row.checkpoint == checkpoint
        assert row.resumable is True
        assert row.current_node == "review"
        assert all(a.status is AnswerStatus.GRADED for a in session.scalars(select(Answer)))


def test_task_queries_ignore_other_executor_runs(
    engine: Engine,
    fixture: SubmissionFixture,
    repository: DatabaseGradingRepository,
) -> None:
    """任务查询只读 M3 kind：同答卷的 M4 工作流运行不得被当成后台任务（H05）。"""

    with Session(engine) as session:
        session.add(WorkflowRun(
            workflow_id="grading-m4-run", request_id="m4-request",
            submission_id=fixture.submission_id, status=WorkflowStatus.RUNNING,
            checkpoint={
                "kind": WORKFLOW_STATE_PAYLOAD_KIND,
                "version": "1",
                "state": {"status": "Running"},
            },
            current_node="grade",
        ))
        session.commit()

    assert repository.get_task("grading-m4-run") is None
    assert repository.find_task_for_submission(str(fixture.submission_id)) is None
    assert repository.mark_interrupted_tasks_failed() == 0

    _save_task(repository, fixture, _task(fixture))
    stored = repository.find_task_for_submission(str(fixture.submission_id))
    assert stored is not None
    assert stored.task_id == "task-1"

    with Session(engine) as session:
        row = session.scalars(
            select(WorkflowRun).where(WorkflowRun.workflow_id == "grading-m4-run")
        ).one()
        assert row.status is WorkflowStatus.RUNNING
        assert (row.checkpoint or {})["kind"] == WORKFLOW_STATE_PAYLOAD_KIND


def _run_writer(
    engine: Engine,
    fixture: SubmissionFixture,
    *,
    workflow_id: str = "grading-run-1",
    fail_after_write: bool = False,
) -> Callable[[Session], WorkflowRun]:
    """构造测试用工作流状态写入器：在调用方事务内写入 M4 运行行。

    真实生产实现是 ``WorkflowCheckpointStore.save_checkpoint_within``；此处只需满足“写入
    M4 运行行”与“可能失败”两个事实，不走检查点存储，避免把仓储事务测试耦合成存储集成测试。
    """

    def writer(session: Session) -> WorkflowRun:
        row = session.scalars(
            select(WorkflowRun).where(WorkflowRun.workflow_id == workflow_id)
        ).one_or_none()
        if row is None:
            row = WorkflowRun(
                workflow_id=workflow_id,
                request_id="request-m4",
                submission_id=fixture.submission_id,
                checkpoint={
                    "kind": WORKFLOW_STATE_PAYLOAD_KIND,
                    "version": "1",
                    "state": {"status": "Running"},
                },
            )
            session.add(row)
        if fail_after_write:
            raise WorkflowCheckpointError("运行状态写入失败（测试注入）。")
        return row

    return writer


def test_save_workflow_outcome_writes_results_and_state_in_one_transaction(
    engine: Engine,
    fixture: SubmissionFixture,
    repository: DatabaseGradingRepository,
) -> None:
    """单事务提交：单题结果、决策快照、最终整卷结果、答卷进度与工作流状态一起落库。"""

    outcome = _outcome(
        fixture,
        subjective=_subjective_payload(
            fixture, confidence=0.9, review_status="Not Required"
        ),
        decisions={
            str(fixture.subjective_answer_id): _decision(
                confidence=0.9, requires_review=False
            )
        },
    )
    assert outcome.exam_result is not None
    assert outcome.exam_result.is_final is True

    row = repository.save_workflow_outcome(
        str(fixture.submission_id),
        context=_context(fixture),
        exam_result=outcome.exam_result,
        state_writer=_run_writer(engine, fixture),
    )

    assert row.submission_id == fixture.submission_id
    assert row.exam_result_id is not None
    with Session(engine) as session:
        results = list(session.scalars(select(GradingResult)))
        assert len(results) == 2
        exam = session.scalars(select(ExamResult)).one()
        assert exam.is_final is True
        assert exam.result_status == ExamResultStatus.FINAL
        assert exam.final_total_score == Decimal("16.00")
        assert all(a.status is AnswerStatus.GRADED for a in session.scalars(select(Answer)))
        submission = session.get(Submission, fixture.submission_id)
        assert submission is not None
        assert submission.status is SubmissionStatus.REVIEWED
        assert submission.reviewed_at is not None
        assert submission.graded_at is not None
        assert session.scalars(select(WorkflowRun)).one().exam_result_id == exam.id


def test_save_workflow_outcome_rolls_back_when_state_write_fails(
    engine: Engine,
    fixture: SubmissionFixture,
    repository: DatabaseGradingRepository,
) -> None:
    """状态写入失败时整体回滚：没有评分行、没有整卷结果，运行行也不保留。"""

    outcome = _outcome(fixture)
    assert outcome.exam_result is not None
    with Session(engine) as session:
        answers_before = {
            answer.id: answer.status for answer in session.scalars(select(Answer))
        }
        graded_submission = session.get(Submission, fixture.submission_id)
        assert graded_submission is not None
        graded_at_before = graded_submission.graded_at

    with pytest.raises(WorkflowCheckpointError):
        repository.save_workflow_outcome(
            str(fixture.submission_id),
            context=_context(fixture),
            exam_result=outcome.exam_result,
            state_writer=_run_writer(engine, fixture, fail_after_write=True),
        )

    with Session(engine) as session:
        assert list(session.scalars(select(GradingResult))) == []
        assert list(session.scalars(select(ExamResult))) == []
        assert list(session.scalars(select(WorkflowRun))) == []
        assert {
            answer.id: answer.status for answer in session.scalars(select(Answer))
        } == answers_before
        submission = session.get(Submission, fixture.submission_id)
        assert submission is not None and submission.graded_at == graded_at_before


def test_save_workflow_outcome_aggregates_pending_exam_result(
    engine: Engine,
    fixture: SubmissionFixture,
    repository: DatabaseGradingRepository,
) -> None:
    """低置信度暂停：写单题结果并汇总出待复核（非 final）整卷结果。"""

    outcome = _outcome(fixture)

    row = repository.save_workflow_outcome(
        str(fixture.submission_id),
        context=_context(fixture),
        results=list(outcome.results),
        decisions=outcome.decisions,
        state_writer=_run_writer(engine, fixture),
    )

    assert row.exam_result_id is not None
    with Session(engine) as session:
        exam = session.scalars(select(ExamResult)).one()
        assert exam.is_final is False
        assert exam.result_status == ExamResultStatus.PENDING_REVIEW
        assert exam.final_total_score is None
        subjective = session.scalars(
            select(GradingResult).where(
                GradingResult.answer_id == fixture.subjective_answer_id
            )
        ).one()
        assert subjective.decision_requires_review is True
        assert subjective.decision_threshold == 0.8
        assert subjective.decision_reason == "置信度低于阈值，进入待人工复核。"
        assert subjective.review_status is ReviewStatus.PENDING_REVIEW


def test_save_workflow_outcome_marks_answers_failed_without_grades(
    engine: Engine,
    fixture: SubmissionFixture,
    repository: DatabaseGradingRepository,
) -> None:
    """评分/结构化失败：不写任何成绩，只把未完成答案标记为失败。"""

    with Session(engine) as session:
        for answer in session.scalars(select(Answer)):
            answer.status = AnswerStatus.SUBMITTED
        session.commit()

    repository.save_workflow_outcome(
        str(fixture.submission_id),
        context=_context(fixture),
        state_writer=_run_writer(engine, fixture),
    )

    with Session(engine) as session:
        assert list(session.scalars(select(GradingResult))) == []
        assert list(session.scalars(select(ExamResult))) == []
        assert all(
            answer.status is AnswerStatus.FAILED
            for answer in session.scalars(select(Answer))
        )
        run = session.scalars(select(WorkflowRun)).one()
        assert run.exam_result_id is None


@pytest.mark.parametrize("pending_review", [False, True])
def test_committed_outcome_survives_restart_with_terminal_task(
    engine: Engine, fixture: SubmissionFixture,
    repository: DatabaseGradingRepository, pending_review: bool,
) -> None:
    _save_task(repository, fixture, _task(fixture, status=GradingTaskStatus.RUNNING))
    outcome = _outcome(fixture) if pending_review else _outcome(
        fixture,
        subjective=_subjective_payload(fixture, confidence=0.9, review_status="Not Required"),
        decisions={str(fixture.subjective_answer_id): _decision(confidence=0.9, requires_review=False)},
    )
    repository.save_outcome(str(fixture.submission_id), outcome, task_id="task-1")
    # 模拟提交成功后进程退出：重启只重新建立仓储，不另行发布完成状态。
    restarted = DatabaseGradingRepository(session_factory=lambda: Session(engine))
    assert restarted.mark_interrupted_tasks_failed() == 0
    task = restarted.get_task("task-1")
    assert task is not None and task.status is GradingTaskStatus.COMPLETED
    assert task.finished_at is not None
    assert task.graded_answer_count == 2
    assert task.pending_review_answer_count == int(pending_review)
    stored = restarted.get_exam_result(str(fixture.submission_id))
    assert stored is not None and stored.is_final is not pending_review
    with Session(engine) as session:
        submission = session.get(Submission, fixture.submission_id)
        assert submission is not None
        assert submission.status is (
            SubmissionStatus.GRADED if pending_review else SubmissionStatus.REVIEWED
        )
        assert (submission.reviewed_at is not None) is not pending_review
        assert len(session.scalars(select(GradingResult)).all()) == 2
        assert all(a.status is AnswerStatus.GRADED for a in session.scalars(select(Answer)))
        row = session.scalars(select(WorkflowRun)).one()
        assert row.status is (WorkflowStatus.PAUSED if pending_review else WorkflowStatus.COMPLETED)


def test_interruption_before_commit_rolls_back_results_progress_and_terminal_task(
    engine: Engine, fixture: SubmissionFixture, repository: DatabaseGradingRepository,
) -> None:
    with Session(engine) as session:
        for answer in session.scalars(select(Answer)):
            answer.status = AnswerStatus.SUBMITTED
        session.commit()
    _save_task(repository, fixture, _task(fixture, status=GradingTaskStatus.RUNNING))

    class Interrupted(BaseException):
        """模拟进程退出，跳过执行器普通异常处理。"""

    def interrupted_session() -> Session:
        session = Session(engine)

        def interrupt_before_commit(active: Session) -> None:
            active.flush()
            raise Interrupted()

        event.listen(session, "before_commit", interrupt_before_commit)
        return session

    interrupted = DatabaseGradingRepository(session_factory=interrupted_session)
    with pytest.raises(Interrupted):
        interrupted.save_outcome(str(fixture.submission_id), _outcome(fixture), task_id="task-1")
    with Session(engine) as session:
        assert session.scalars(select(GradingResult)).all() == []
        assert session.scalars(select(ExamResult)).all() == []
        assert session.scalars(select(WorkflowRun)).one().status is WorkflowStatus.RUNNING
        assert all(a.status is AnswerStatus.SUBMITTED for a in session.scalars(select(Answer)))
    assert repository.mark_interrupted_tasks_failed() == 1
    with Session(engine) as session:
        assert all(a.status is AnswerStatus.FAILED for a in session.scalars(select(Answer)))


def test_recovery_preserves_saved_answers_and_only_fails_unscored_answers(
    engine: Engine, fixture: SubmissionFixture, repository: DatabaseGradingRepository,
) -> None:
    repository.save_outcome(str(fixture.submission_id), _outcome(fixture))
    _save_task(repository, fixture, _task(fixture, status=GradingTaskStatus.RUNNING))
    with Session(engine) as session:
        row = session.scalars(select(GradingResult).where(
            GradingResult.answer_id == fixture.subjective_answer_id,
        )).one()
        session.delete(row)
        answer = session.get(Answer, fixture.subjective_answer_id)
        assert answer is not None
        answer.status = AnswerStatus.GRADING
        session.commit()
    before = repository.get_single_result(str(fixture.submission_id), str(fixture.objective_answer_id))

    assert repository.mark_interrupted_tasks_failed() == 1
    assert repository.get_single_result(
        str(fixture.submission_id), str(fixture.objective_answer_id),
    ) == before
    with Session(engine) as session:
        assert session.get(Answer, fixture.objective_answer_id).status is AnswerStatus.GRADED
        assert session.get(Answer, fixture.subjective_answer_id).status is AnswerStatus.FAILED
        assert session.scalars(select(ExamResult)).one() is not None


def test_outcome_links_result_with_production_session_settings(
    engine: Engine, fixture: SubmissionFixture,
) -> None:
    """TCR（B01）：生产会话关闭自动刷新，任务仍须在同次提交关联真实结果主键。"""
    repository = DatabaseGradingRepository(session_factory=create_session_factory(engine))
    _save_task(repository, fixture, _task(fixture, status=GradingTaskStatus.RUNNING))
    repository.save_outcome(str(fixture.submission_id), _outcome(fixture), task_id="task-1")
    with Session(engine) as session:
        workflow = session.scalars(select(WorkflowRun)).one()
        result = session.scalars(select(ExamResult)).one()
        assert workflow.exam_result_id == result.id
        assert workflow.status is WorkflowStatus.PAUSED


def test_task_creation_requires_request_id(
    engine: Engine,
    fixture: SubmissionFixture,
    repository: DatabaseGradingRepository,
) -> None:
    """创建任务缺少 request_id 时显式失败，不写入无追踪标识的记录。"""

    with pytest.raises(GradingTaskTraceMissingError):
        repository.save_task(_task(fixture))

    with Session(engine) as session:
        assert list(session.scalars(select(WorkflowRun))) == []


def test_lock_submission_rejects_unknown_submission(
    fixture: SubmissionFixture,
    repository: DatabaseGradingRepository,
) -> None:
    """锁定不存在的答卷时按未找到处理，不静默跳过并发保护。"""

    with (
        pytest.raises(GradingSubmissionNotFoundError) as error,
        repository.lock_submission("8f14e45f-ceea-467a-9a1e-1c1b0c4d0b0b"),
    ):
        pass

    assert error.value.error_code == GRADING_SUBMISSION_NOT_FOUND


def test_ensure_ready_reports_missing_tables() -> None:
    """结果表缺失（未迁移）时就绪检查显式失败，供 API 映射为 503。"""

    empty_engine = create_engine("sqlite:///:memory:")
    try:
        repository = DatabaseGradingRepository(
            session_factory=lambda: Session(empty_engine)
        )
        with pytest.raises(GradingStoreNotReadyError) as error:
            repository.ensure_ready()
    finally:
        empty_engine.dispose()

    assert error.value.error_code == GRADING_STORE_NOT_READY


def test_save_single_result_updates_existing_row(
    engine: Engine,
    fixture: SubmissionFixture,
    repository: DatabaseGradingRepository,
) -> None:
    """显式单题写入入口同样按 answer_id 就地更新。"""

    _save_task(repository, fixture, _task(fixture))
    repository.save_outcome(
        str(fixture.submission_id), _outcome(fixture), task_id="task-1"
    )
    stored = repository.get_single_result(
        str(fixture.submission_id), str(fixture.objective_answer_id)
    )
    assert stored is not None

    repository.save_single_result(
        str(fixture.submission_id),
        stored.model_copy(update={"score": Decimal("8.00"), "effective_score": None}),
    )

    updated = repository.get_single_result(
        str(fixture.submission_id), str(fixture.objective_answer_id)
    )
    assert updated is not None
    assert updated.score == Decimal("8.00")
    with Session(engine) as session:
        rows = list(session.scalars(select(GradingResult)))
        assert len(rows) == 2
