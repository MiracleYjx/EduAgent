"""T063 AgentRun 模型单元测试。

覆盖范围：表结构与约束名、`request_id` 必填、`user_id` 与 `workflow_id` 可为空、
Trace 状态取值（`success`/`failure`/`pending_review`）与 WorkflowStatus 分离、
结构化输出校验状态复用 ValidationStatus、未知用量保存 NULL 而不填假值、
错误摘要可空、耗时与 Token 计数非负，以及 Workflow 关联被删除时只清空指针。

测试运行在启用外键约束的内存 SQLite 上；PostgreSQL 侧的列精度、约束与外键行为由
`alembic upgrade/check` 与一次性验证库上的 pg_catalog 断言覆盖。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import Enum as SAEnum
from sqlalchemy import String, func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.core.database import Base
from backend.app.domain.enums import ValidationStatus, WorkflowStatus
from backend.app.models import AgentRun, ExamResult, WorkflowRun
from backend.app.schemas.grading import ExamResultStatus
from tests.unit.models.sqlite_support import (
    SubmissionFixture,
    create_sqlite_engine,
    seed_submission,
)

AGENT_RUN_COLUMNS = [
    "agent_type",
    "workflow_id",
    "request_id",
    "user_id",
    "input_summary",
    "output_summary",
    "validation_status",
    "status",
    "latency_ms",
    "model",
    "prompt_version",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "error_code",
    "error_message",
    "error_retryable",
    "id",
    "created_at",
    "updated_at",
]

AGENT_RUN_CONSTRAINTS = {
    "agent_validation_status",
    "ck_agent_runs_status_trace_value",
    "ck_agent_runs_latency_non_negative",
    "ck_agent_runs_token_counts_non_negative",
}

#: plan §7 的 Trace 状态取值（与 WorkflowStatus 分离）。
TRACE_STATUS_VALUES = ("success", "failure", "pending_review")
#: 不得出现在 Trace 状态列中的工作流状态字面值。
NON_TRACE_STATUS_VALUES = tuple(item.value for item in WorkflowStatus)


def _workflow_run(
    fixture: SubmissionFixture,
    *,
    workflow_id: str = "wf-0001",
    request_id: str = "req-0001",
) -> WorkflowRun:
    """构造一条运行中的工作流记录，供 AgentRun 关联。"""

    return WorkflowRun(
        workflow_id=workflow_id,
        request_id=request_id,
        submission_id=fixture.submission_id,
        current_node="grade_subjective",
        status=WorkflowStatus.RUNNING,
    )


def _exam_result(fixture: SubmissionFixture) -> ExamResult:
    """构造一条整卷结果，供工作流记录指向（非必需，仅用于构造完整链路）。"""

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


def _agent_run(
    fixture: SubmissionFixture,
    *,
    agent_type: str = "grading",
    workflow_id: str | None = None,
    request_id: str = "req-0001",
    user_id: UUID | None = None,
    input_summary: str | None = "答卷 3 道主观题与检索片段 5 条",
    output_summary: str | None = "结构化评分结果通过校验",
    validation_status: ValidationStatus | None = ValidationStatus.VALIDATED,
    status: str = "success",
    latency_ms: int | None = 820,
    model: str | None = "doubao-pro-32k",
    prompt_version: str | None = "grading-v3",
    input_tokens: int | None = 1200,
    output_tokens: int | None = 380,
    total_tokens: int | None = 1580,
    error_code: str | None = None,
    error_message: str | None = None,
    error_retryable: bool | None = None,
) -> AgentRun:
    """构造一条 Agent 运行追踪记录。"""

    return AgentRun(
        agent_type=agent_type,
        workflow_id=workflow_id,
        request_id=request_id,
        user_id=user_id,
        input_summary=input_summary,
        output_summary=output_summary,
        validation_status=validation_status,
        status=status,
        latency_ms=latency_ms,
        model=model,
        prompt_version=prompt_version,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        error_code=error_code,
        error_message=error_message,
        error_retryable=error_retryable,
    )


def test_table_structure_matches_plan() -> None:
    """列集合、索引与约束名必须与 plan §7 的追踪记录定义一致。"""

    table = Base.metadata.tables["agent_runs"]

    assert [column.name for column in table.columns] == AGENT_RUN_COLUMNS
    assert {index.name for index in table.indexes} == {
        "ix_agent_runs_request_id",
        "ix_agent_runs_user_id",
        "ix_agent_runs_workflow_id",
    }
    assert AGENT_RUN_CONSTRAINTS.issubset(
        {constraint.name for constraint in table.constraints}
    )
    assert all("uq_" not in str(constraint.name) for constraint in table.constraints)


def test_status_column_uses_trace_values_not_workflow_status() -> None:
    """status 使用 Trace 状态字符串，不复用 WorkflowStatus 枚举。"""

    status_type = inspect(AgentRun).columns.status.type

    assert isinstance(status_type, String)
    assert not isinstance(status_type, SAEnum)
    assert WorkflowStatus.QUEUED.value in NON_TRACE_STATUS_VALUES


def test_validation_status_column_reuses_validation_status_enum() -> None:
    """结构化输出校验状态复用 domain 的 ValidationStatus。"""

    validation_type = inspect(AgentRun).columns.validation_status.type

    assert isinstance(validation_type, SAEnum)
    assert validation_type.enums == [item.value for item in ValidationStatus]


def test_foreign_keys_declare_targets_and_delete_behavior() -> None:
    """工作流关联在被引用运行删除时清空；用户关联为 RESTRICT。"""

    table = Base.metadata.tables["agent_runs"]

    assert {fk.parent.name: fk.ondelete for fk in table.foreign_keys} == {
        "workflow_id": "SET NULL",
        "user_id": "RESTRICT",
    }
    assert {fk.target_fullname for fk in table.foreign_keys} == {
        "workflow_runs.workflow_id",
        "users.id",
    }


@pytest.mark.parametrize("status", TRACE_STATUS_VALUES)
def test_trace_status_values_round_trip(status: str) -> None:
    """三个 Trace 状态都必须可写入读回。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        run = _agent_run(fixture, status=status, user_id=fixture.teacher_id)
        session.add(run)
        session.commit()
        run_id = run.id

    with Session(engine) as session:
        stored = session.get(AgentRun, run_id)
        assert stored is not None
        assert stored.status == status


def test_full_trace_record_round_trips() -> None:
    """完整追踪记录（含 Workflow 关联、Token 用量与耗时）必须跨 Session 读回。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        workflow = _workflow_run(fixture)
        session.add(_exam_result(fixture))
        session.add(workflow)
        session.commit()

        run = _agent_run(
            fixture,
            workflow_id=workflow.workflow_id,
            user_id=fixture.teacher_id,
        )
        session.add(run)
        session.commit()
        run_id = run.id
        workflow_id = workflow.workflow_id

    with Session(engine) as session:
        stored = session.get(AgentRun, run_id)
        assert stored is not None
        assert stored.agent_type == "grading"
        assert stored.workflow_id == workflow_id
        assert stored.request_id == "req-0001"
        assert stored.user_id == fixture.teacher_id
        assert stored.input_summary == "答卷 3 道主观题与检索片段 5 条"
        assert stored.output_summary == "结构化评分结果通过校验"
        assert stored.validation_status is ValidationStatus.VALIDATED
        assert stored.latency_ms == 820
        assert stored.model == "doubao-pro-32k"
        assert stored.prompt_version == "grading-v3"
        assert stored.input_tokens == 1200
        assert stored.output_tokens == 380
        assert stored.total_tokens == 1580
        assert stored.error_code is None
        assert stored.error_message is None
        assert stored.error_retryable is None
        assert stored.created_at is not None


def test_unknown_usage_and_missing_context_stay_null() -> None:
    """独立 Agent 调用没有 Workflow、模型用量或用户上下文时保存 NULL，不填假值。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        run = _agent_run(
            fixture,
            agent_type="diagnosis",
            workflow_id=None,
            user_id=None,
            latency_ms=None,
            model=None,
            prompt_version=None,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
        )
        session.add(run)
        session.commit()
        run_id = run.id

    with Session(engine) as session:
        stored = session.get(AgentRun, run_id)
        assert stored is not None
        assert stored.agent_type == "diagnosis"
        assert stored.workflow_id is None
        assert stored.user_id is None
        assert stored.latency_ms is None
        assert stored.model is None
        assert stored.prompt_version is None
        assert stored.input_tokens is None
        assert stored.output_tokens is None
        assert stored.total_tokens is None


def test_failure_record_keeps_masked_error_summary() -> None:
    """失败追踪如实保存脱敏错误码与摘要，并对齐可重试标记。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        run = _agent_run(
            fixture,
            status="failure",
            validation_status=None,
            output_summary=None,
            error_code="PROVIDER_TIMEOUT",
            error_message="Provider 调用超时，已脱敏。",
            error_retryable=True,
        )
        session.add(run)
        session.commit()
        run_id = run.id

    with Session(engine) as session:
        stored = session.get(AgentRun, run_id)
        assert stored is not None
        assert stored.status == "failure"
        assert stored.error_code == "PROVIDER_TIMEOUT"
        assert stored.error_message == "Provider 调用超时，已脱敏。"
        assert stored.error_retryable is True
        assert stored.validation_status is None
        assert stored.output_summary is None


def test_request_id_is_required() -> None:
    """request_id 必填，独立 Agent 调用同样不得缺省。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        session.add(_agent_run(fixture, request_id=None))  # type: ignore[arg-type]
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_agent_type_is_required() -> None:
    """agent_type 必填，缺失时拒绝写入。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        session.add(_agent_run(fixture, agent_type=None))  # type: ignore[arg-type]
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


@pytest.mark.parametrize(
    "status", NON_TRACE_STATUS_VALUES, ids=lambda item: item.replace(" ", "-")
)
def test_workflow_status_values_are_rejected(status: str) -> None:
    """WorkflowStatus 取值不得写入 Trace 状态列。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        session.add(_agent_run(fixture, status=status))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_negative_latency_is_rejected() -> None:
    """耗时为负说明采集有误，必须拒绝。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        session.add(_agent_run(fixture, latency_ms=-1))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


@pytest.mark.parametrize(
    ("field", "kwargs"),
    [
        ("input_tokens", {"input_tokens": -1}),
        ("output_tokens", {"output_tokens": -1}),
        ("total_tokens", {"total_tokens": -1}),
    ],
)
def test_negative_token_counts_are_rejected(
    field: str, kwargs: dict[str, int]
) -> None:
    """Token 统计不得为负，避免把采集失败写成负数用量。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        session.add(_agent_run(fixture, **kwargs))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_workflow_relationship_is_readonly_and_resolves_run() -> None:
    """单向 Workflow 关联只读，避免追踪记录反向改写工作流状态。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        workflow = _workflow_run(fixture)
        session.add(workflow)
        session.commit()
        run = _agent_run(fixture, workflow_id=workflow.workflow_id)
        session.add(run)
        session.commit()
        run_id = run.id

    assert AgentRun.workflow.property.viewonly is True

    with Session(engine) as session:
        stored = session.get(AgentRun, run_id)
        assert stored is not None
        assert stored.workflow is not None
        assert stored.workflow.workflow_id == "wf-0001"
        assert stored.workflow.status is WorkflowStatus.RUNNING


def test_deleting_workflow_run_only_clears_pointer() -> None:
    """工作流运行被删除时只清空关联指针，追踪记录保留。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        workflow = _workflow_run(fixture)
        session.add(workflow)
        session.commit()
        run = _agent_run(fixture, workflow_id=workflow.workflow_id)
        session.add(run)
        session.commit()
        run_id = run.id
        workflow_row_id = workflow.id

    with Session(engine) as session:
        stored_workflow = session.get(WorkflowRun, workflow_row_id)
        assert stored_workflow is not None
        session.delete(stored_workflow)
        session.commit()

    with Session(engine) as session:
        stored = session.get(AgentRun, run_id)
        assert stored is not None
        assert stored.workflow_id is None
        assert stored.request_id == "req-0001"


def test_trace_records_are_listable_by_request_id() -> None:
    """同一次请求的多条 Agent 追踪必须能按 request_id 汇总。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        session.add(_agent_run(fixture, agent_type="grading", status="success"))
        session.add(_agent_run(fixture, agent_type="reviewer", status="failure"))
        session.add(
            _agent_run(fixture, agent_type="diagnosis", request_id="req-0002")
        )
        session.commit()

    with Session(engine) as session:
        count = session.scalar(
            select(func.count())
            .select_from(AgentRun)
            .where(AgentRun.request_id == "req-0001")
        )
        assert count == 2
