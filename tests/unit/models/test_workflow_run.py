"""T064 WorkflowRun 模型单元测试。

覆盖范围：表结构与约束名、`workflow_id` 唯一、`request_id` 必填、状态复用 WorkflowStatus、
流程阶段指针（当前答案、整卷结果）可为空、检查点与暂停原因读写、重试计数与可恢复标记、
以及来源删除行为（答卷级联、阶段指针清空）。

“任务执行完成”不等于“成绩最终确认”：状态为 ``Completed`` 的运行记录允许没有
``exam_result_id``，模型不强制二者同时出现。

测试运行在启用外键约束的内存 SQLite 上；PostgreSQL 侧的列精度、约束与外键行为由
`alembic upgrade/check` 与一次性验证库上的 pg_catalog 断言覆盖。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import Enum as SAEnum
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.core.database import Base
from backend.app.domain.enums import WorkflowStatus
from backend.app.models import Answer, ExamResult, Submission, WorkflowRun
from backend.app.schemas.grading import ExamResultStatus
from tests.unit.models.sqlite_support import (
    SubmissionFixture,
    create_sqlite_engine,
    seed_submission,
)

WORKFLOW_COLUMNS = [
    "workflow_id",
    "request_id",
    "submission_id",
    "current_node",
    "current_answer_id",
    "status",
    "checkpoint",
    "pause_reason",
    "retry_count",
    "resumable",
    "exam_result_id",
    "id",
    "created_at",
    "updated_at",
]

WORKFLOW_CONSTRAINTS = {
    "workflow_status",
    "uq_workflow_runs_workflow_id",
    "ck_workflow_runs_retry_count_non_negative",
}


def _exam_result(fixture: SubmissionFixture) -> ExamResult:
    """构造一条最终成绩的整卷结果，供运行记录指向。"""

    return ExamResult(
        submission_id=fixture.submission_id,
        exam_id=fixture.exam_id,
        student_id=fixture.student_id,
        result_status=ExamResultStatus.FINAL,
        is_final=True,
        final_total_score=Decimal("16.00"),
        confirmed_subtotal=Decimal("16.00"),
        total_max_score=Decimal("20.00"),
        aggregated_at=datetime(2026, 9, 16, 12, 0, tzinfo=UTC),
    )


def _workflow(
    fixture: SubmissionFixture,
    *,
    workflow_id: str = "wf-0001",
    request_id: str = "req-0001",
    status: WorkflowStatus = WorkflowStatus.RUNNING,
    current_node: str | None = "grade_subjective",
    current_answer_id: UUID | None = None,
    checkpoint: dict[str, object] | None = None,
    pause_reason: str | None = None,
    retry_count: int = 0,
    resumable: bool = True,
    exam_result_id: UUID | None = None,
) -> WorkflowRun:
    """构造一条阅卷工作流运行记录。"""

    return WorkflowRun(
        workflow_id=workflow_id,
        request_id=request_id,
        submission_id=fixture.submission_id,
        current_node=current_node,
        current_answer_id=current_answer_id,
        status=status,
        checkpoint=checkpoint,
        pause_reason=pause_reason,
        retry_count=retry_count,
        resumable=resumable,
        exam_result_id=exam_result_id,
    )


def test_table_structure_matches_plan() -> None:
    """列集合、索引与约束名必须与 plan §5.2/§7 的 WorkflowRun 定义一致。"""

    table = Base.metadata.tables["workflow_runs"]

    assert [column.name for column in table.columns] == WORKFLOW_COLUMNS
    assert {index.name for index in table.indexes} == {
        "ix_workflow_runs_request_id",
        "ix_workflow_runs_submission_id",
    }
    assert WORKFLOW_CONSTRAINTS.issubset(
        {constraint.name for constraint in table.constraints}
    )


def test_status_column_reuses_workflow_status_enum() -> None:
    """工作流状态必须复用 domain 的 WorkflowStatus，不新增重复枚举。"""

    status_type = inspect(WorkflowRun).columns.status.type

    assert isinstance(status_type, SAEnum)
    assert status_type.enums == [item.value for item in WorkflowStatus]


def test_foreign_keys_declare_targets_and_delete_behavior() -> None:
    """答卷删除级联运行记录；阶段指针指向的行被删除时只清空指针。"""

    table = Base.metadata.tables["workflow_runs"]

    assert {fk.parent.name: fk.ondelete for fk in table.foreign_keys} == {
        "submission_id": "CASCADE",
        "current_answer_id": "SET NULL",
        "exam_result_id": "SET NULL",
    }
    assert {fk.target_fullname for fk in table.foreign_keys} == {
        "submissions.id",
        "answers.id",
        "exam_results.id",
    }


def test_run_round_trips_workflow_facts() -> None:
    """工作流事实（含检查点）必须跨 Session 完整读回。"""

    checkpoint = {
        "node": "grade_subjective",
        "pending_answer_ids": ["a-1", "a-1", "a-2"],
        "graded_count": 1,
    }

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        run = _workflow(
            fixture,
            status=WorkflowStatus.PAUSED,
            current_answer_id=fixture.subjective_answer_id,
            checkpoint=checkpoint,
            pause_reason="等待人工复核",
            retry_count=2,
            resumable=True,
        )
        session.add(run)
        session.commit()
        run_id = run.id

    with Session(engine) as session:
        stored = session.get(WorkflowRun, run_id)
        assert stored is not None
        assert stored.workflow_id == "wf-0001"
        assert stored.request_id == "req-0001"
        assert stored.submission_id == fixture.submission_id
        assert stored.current_node == "grade_subjective"
        assert stored.current_answer_id == fixture.subjective_answer_id
        assert stored.status is WorkflowStatus.PAUSED
        assert stored.pause_reason == "等待人工复核"
        assert stored.retry_count == 2
        assert stored.resumable is True
        assert stored.exam_result_id is None
        # 检查点保留原顺序与重复，便于恢复后重放同一批待处理题目。
        assert stored.checkpoint == checkpoint
        assert stored.checkpoint["pending_answer_ids"] == ["a-1", "a-1", "a-2"]
        assert stored.created_at is not None


@pytest.mark.parametrize(
    "status",
    list(WorkflowStatus),
    ids=lambda item: item.name,
)
def test_running_states_round_trip_without_exam_result(
    status: WorkflowStatus,
) -> None:
    """尚未形成最终成绩的运行记录允许没有整卷结果指针。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        run = _workflow(fixture, status=status, current_node=None)
        session.add(run)
        session.commit()
        run_id = run.id

    with Session(engine) as session:
        stored = session.get(WorkflowRun, run_id)
        assert stored is not None
        assert stored.status is status
        assert stored.exam_result_id is None


def test_completed_run_does_not_require_final_result() -> None:
    """任务执行完成不等于成绩最终确认，二者不得由模型强制绑定。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        run = _workflow(
            fixture,
            workflow_id="wf-completed",
            status=WorkflowStatus.COMPLETED,
            current_node="diagnose",
            resumable=False,
        )
        session.add(run)
        session.commit()
        run_id = run.id

    with Session(engine) as session:
        stored = session.get(WorkflowRun, run_id)
        assert stored is not None
        assert stored.status is WorkflowStatus.COMPLETED
        assert stored.exam_result_id is None


def test_run_can_point_at_exam_result() -> None:
    """流程到达汇总阶段后可以指向整卷结果。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        exam_result = _exam_result(fixture)
        session.add(exam_result)
        session.commit()
        run = _workflow(
            fixture, exam_result_id=exam_result.id, status=WorkflowStatus.COMPLETED
        )
        session.add(run)
        session.commit()
        run_id = run.id
        exam_result_id = exam_result.id

    with Session(engine) as session:
        stored = session.get(WorkflowRun, run_id)
        assert stored is not None
        assert stored.exam_result_id == exam_result_id


def test_workflow_id_is_unique() -> None:
    """同一个 workflow_id 只能有一条运行记录。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        session.add(_workflow(fixture))
        session.commit()

        session.add(_workflow(fixture, request_id="req-0002"))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_request_id_is_required() -> None:
    """request_id 贯穿追踪链路，必须落库而不是可空。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        session.add(_workflow(fixture, request_id=None))  # type: ignore[arg-type]
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_negative_retry_count_is_rejected() -> None:
    """重试次数是计数事实，不接受负数。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        session.add(_workflow(fixture, retry_count=-1))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_failed_run_keeps_pause_reason_and_resumable_flag() -> None:
    """失败运行如实保存暂停原因与是否可恢复，不擅自改写为可恢复。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        run = _workflow(
            fixture,
            status=WorkflowStatus.FAILED,
            current_node="grade_subjective",
            pause_reason="Provider 超时",
            retry_count=3,
            resumable=False,
        )
        session.add(run)
        session.commit()
        run_id = run.id

    with Session(engine) as session:
        stored = session.get(WorkflowRun, run_id)
        assert stored is not None
        assert stored.status is WorkflowStatus.FAILED
        assert stored.pause_reason == "Provider 超时"
        assert stored.retry_count == 3
        assert stored.resumable is False


def test_deleting_submission_cascades_its_runs() -> None:
    """删除答卷时其运行记录级联删除，不残留悬空运行。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        session.add(_workflow(fixture))
        session.commit()

    with Session(engine) as session:
        submission = session.get(Submission, fixture.submission_id)
        assert submission is not None
        session.delete(submission)
        session.commit()

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(WorkflowRun)) == 0


def test_deleting_current_answer_only_clears_pointer() -> None:
    """当前答案被删除时只清空指针，保留运行记录与检查点。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        run = _workflow(
            fixture,
            current_answer_id=fixture.subjective_answer_id,
            checkpoint={"node": "grade_subjective"},
        )
        session.add(run)
        session.commit()
        run_id = run.id

    with Session(engine) as session:
        answer = session.get(Answer, fixture.subjective_answer_id)
        assert answer is not None
        session.delete(answer)
        session.commit()

    with Session(engine) as session:
        stored = session.get(WorkflowRun, run_id)
        assert stored is not None
        assert stored.current_answer_id is None
        assert stored.checkpoint == {"node": "grade_subjective"}


def test_deleting_exam_result_only_clears_pointer() -> None:
    """整卷结果被删除时只清空指针，不删除运行历史。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        exam_result = _exam_result(fixture)
        session.add(exam_result)
        session.commit()
        run = _workflow(fixture, exam_result_id=exam_result.id)
        session.add(run)
        session.commit()
        run_id = run.id
        exam_result_id = exam_result.id

    with Session(engine) as session:
        stored_result = session.get(ExamResult, exam_result_id)
        assert stored_result is not None
        session.delete(stored_result)
        session.commit()

    with Session(engine) as session:
        stored = session.get(WorkflowRun, run_id)
        assert stored is not None
        assert stored.exam_result_id is None
        assert stored.status is WorkflowStatus.RUNNING
