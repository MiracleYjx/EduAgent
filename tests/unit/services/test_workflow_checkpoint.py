"""T073 检查点持久化的失败优先单元测试（含 LangGraph 跨实例恢复）。

TCR（测试契约记录）：

- **为什么原有测试不足以证明持久恢复**：原测试只断言“业务状态快照写入 ``WorkflowRun`` 并能读回”，
  并把 ``resumable`` 直接取自信状态声明。业务快照不是 LangGraph 的 runtime checkpoint，因此既不能
  证明暂停的原图可以跨实例继续执行，也不能证明 ``resumable=True`` 有持久事实依据；这类断言只能通过
  “真实 T072 图 + 数据库 Checkpointer + 销毁实例后仅凭数据库恢复”来证伪。
- **本次新增/修改的测试**：
  1. 新增跨实例恢复：实例 A 用 :class:`DatabaseCheckpointSaver` 跑真实 T072 混合答卷图到 interrupt，
     销毁会话与实例后，实例 B 仅按同一 ``thread_id`` 从数据库读回检查点、pending writes 与状态，
     用原 ``workflow_id``/``request_id`` 恢复并完成；断言已完成题目不再评分、题序与重评次数保持、
     自身不构造任何内存检查点。
  2. 新增 runtime 载荷测试：``state`` 段与 ``runtime`` 段共存不互相覆盖、checkpoint/metadata/父配置/
     channel_versions/pending writes 均可读回、保留上限裁剪最旧条目及其写入、``__interrupt__`` 等固定
     槽位覆盖而普通通道追加、未知 thread/checkpoint 返回 ``None``、pickle 载荷被拒绝、未绑定线程时
     写入显式失败。
  3. 收紧并改写 ``resumable`` 相关断言：只有持久的 runtime checkpoint 存在且能按同一 ``thread_id``
     读回时才为真；只有业务快照时为 ``False``（对应 H02 的“不假声明”）；``mark_completed`` 之后
     回落为 ``False``，``delete_thread`` 同时清除 runtime 段。
  4. 原有业务快照/终态标记/就绪诊断用例保留，仅在 ``resumable`` 语义处按新事实收窄断言
     （由“写状态即置真”改为“必须有 runtime checkpoint”），未放宽任何既有断言。
- **覆盖契约**：``.specify/plan.md`` §5（人工复核可中断可恢复）、``.specify/data-model.md``
  （WorkflowRun 状态与关键校验规则）、FR-036、T064 ``WorkflowRun`` 列约束、T065 业务状态载荷、
  T071 复核状态契约、T072 ``interrupt``/``resume`` 与教师决策契约，以及 langgraph-checkpoint 4.1.1
  的 ``BaseCheckpointSaver`` 接口。
- **测试环境**：启用外键约束的内存 SQLite（复用 M3 模型测试库辅助）。真实 PostgreSQL 上的列精度、
  外键与并发行为仍由 `alembic upgrade/check` 与一次性验证库覆盖，本文件不宣称已完成 PostgreSQL
  并发实测。
"""

from __future__ import annotations

import json
from collections.abc import Coroutine, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from backend.app.ai.agents.invocation import AgentInvocation
from backend.app.ai.agents.state import AgentError, AgentOutput, AgentStatus, AgentType
from backend.app.ai.workflows.grading_handoff import (
    GENERATE_DIAGNOSIS,
    LOAD_SUBMISSION,
    PENDING_REVIEW,
    UNIFIED_RESULT,
)
from backend.app.ai.workflows.grading_workflow import (
    GradingWorkflow,
    GradingWorkflowDeps,
    TeacherReviewDecision,
)
from backend.app.ai.workflows.state import (
    CHECKPOINT_STATE_KEY,
    WORKFLOW_STATE_PAYLOAD_KIND,
    WORKFLOW_STATE_PAYLOAD_VERSION,
    workflow_state_from_json,
    workflow_state_to_json,
)
from backend.app.core.database import Base
from backend.app.domain.enums import (
    QuestionType,
    ReviewStatus,
    SubmissionStatus,
    ValidationStatus,
    WorkflowStatus,
)
from backend.app.models import ExamResult, Submission, User, WorkflowRun
from backend.app.schemas.ai import GradingResult
from backend.app.schemas.grading import (
    ConfidenceDecisionDTO,
    DiagnosisReportDTO,
    DiagnosisStatus,
    ExamResultDTO,
    ExamResultStatus,
)
from backend.app.services.grading.grading_task_service import (
    GradingTargetAnswer,
    SubmissionSnapshot,
)
from backend.app.services.workflow_checkpoint import (
    CHECKPOINT_SAVED_AT_KEY,
    DEFAULT_RUNTIME_HISTORY_LIMIT,
    RUNTIME_ENVELOPE_KEY,
    RUNTIME_ENVELOPE_KIND,
    RUNTIME_ENVELOPE_VERSION,
    WORKFLOW_CHECKPOINT_COMPLETION_NOT_ALLOWED,
    WORKFLOW_CHECKPOINT_FAILED,
    WORKFLOW_CHECKPOINT_INVALID_INPUT,
    WORKFLOW_CHECKPOINT_NOT_FOUND,
    WORKFLOW_CHECKPOINT_OWNERSHIP_MISMATCH,
    WORKFLOW_CHECKPOINT_SERDE_REJECTED,
    WORKFLOW_CHECKPOINT_STATE_INVALID,
    WORKFLOW_CHECKPOINT_STORE_NOT_READY,
    WORKFLOW_CHECKPOINT_THREAD_UNBOUND,
    DatabaseCheckpointSaver,
    WorkflowCheckpointCompletionError,
    WorkflowCheckpointError,
    WorkflowCheckpointNotFoundError,
    WorkflowCheckpointOwnershipError,
    WorkflowCheckpointSerdeError,
    WorkflowCheckpointStateError,
    WorkflowCheckpointStore,
    WorkflowCheckpointStoreNotReadyError,
    WorkflowCheckpointThreadUnboundError,
    checkpoint_thread_id,
    pending_review_answer_ids,
    runtime_checkpoint_ids,
    runtime_has_checkpoint,
    runtime_pending_write_count,
)
from tests.unit.models.sqlite_support import (
    SubmissionFixture,
    create_sqlite_engine,
    seed_submission,
    sqlite_foreign_keys_enabled,
)
from tests.unit.settings_helpers import build_test_settings

WORKFLOW_ID = "workflow-t073"
REQUEST_ID = "request-t073"
THREAD_ID = "thread-t073"
FIXED_NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
PAUSE_REASON = "第 2 题评分结果需要教师复核，工作流已暂停。"
INTERRUPT_CHANNEL = "__interrupt__"


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """在同步用例中驱动异步入口；与仓库既有测试约定一致。"""

    import asyncio

    return asyncio.run(coroutine)


@dataclass(frozen=True, slots=True)
class CheckpointEnv:
    """本次测试的内存数据库、会话与答卷夹具。"""

    engine: Any
    session: Session
    fixture: SubmissionFixture


@pytest.fixture()
def env() -> Iterator[CheckpointEnv]:
    """每个用例使用独立的内存 SQLite；外键约束必须真实生效。"""

    engine = create_sqlite_engine()
    assert sqlite_foreign_keys_enabled(engine)
    session = Session(engine)
    fixture = seed_submission(session)
    try:
        yield CheckpointEnv(engine=engine, session=session, fixture=fixture)
    finally:
        session.close()
        engine.dispose()


def _file_engine(path: str) -> Engine:
    """构造启用外键约束的文件型 SQLite（跨连接/跨线程可见同一份数据）。"""

    engine = create_engine(f"sqlite:///{path}")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return engine


@pytest.fixture()
def file_env(tmp_path: Any) -> Iterator[CheckpointEnv]:
    """文件型 SQLite 环境：供跨实例/跨连接恢复用例使用。"""

    engine = _file_engine(str(tmp_path / "checkpoints.db"))
    assert sqlite_foreign_keys_enabled(engine)
    session = Session(engine)
    fixture = seed_submission(session)
    session.close()
    try:
        yield CheckpointEnv(engine=engine, session=Session(engine), fixture=fixture)
    finally:
        engine.dispose()


def _store(env: CheckpointEnv, **overrides: Any) -> WorkflowCheckpointStore:
    """构造使用固定时钟的检查点存储（默认借入同一会话，便于跨实例读回）。"""

    kwargs: dict[str, Any] = {"session": env.session, "clock": lambda: FIXED_NOW}
    kwargs.update(overrides)
    return WorkflowCheckpointStore(**kwargs)


def _saver(env: CheckpointEnv, **overrides: Any) -> DatabaseCheckpointSaver:
    """构造独立会话的持久化 Checkpointer（生产用会话工厂语义）。"""

    kwargs: dict[str, Any] = {
        "session_factory": lambda: Session(env.engine),
        "clock": lambda: FIXED_NOW,
    }
    kwargs.update(overrides)
    return DatabaseCheckpointSaver(**kwargs)


def _grading_result(
    env: CheckpointEnv,
    *,
    answer_id: str,
    review_status: str = "Pending Review",
    score: float = 6.0,
) -> GradingResult:
    """构造与答卷一致的单题评分结果。"""

    return GradingResult(
        question_type=QuestionType.SHORT_ANSWER,
        score=score,
        max_score=10.0,
        reason="说明了变量的作用。",
        correct_points=["保存数据"],
        missing_knowledge_points=["引用数据"],
        knowledge_points=["变量"],
        suggestions=["补充变量引用。"],
        confidence=0.3,
        validation_status="Validated",
        review_status=review_status,
        answer_id=answer_id,
        submission_id=str(env.fixture.submission_id),
    )


def _decision(*, answer_id: str, review_status: str = "Pending Review") -> ConfidenceDecisionDTO:
    """构造置信度决策快照。"""

    return ConfidenceDecisionDTO(
        confidence=0.3,
        threshold=0.8,
        requires_review=True,
        review_status=review_status,
        grading_status="Pending Review",
        reason="置信度低于阈值，已进入人工复核队列。",
    )


def _state(env: CheckpointEnv, **overrides: Any) -> dict[str, Any]:
    """构造暂停等待复核的合法状态（T065 快照视图可接受）。"""

    answer_id = str(env.fixture.subjective_answer_id)
    state: dict[str, Any] = {
        "workflow_id": WORKFLOW_ID,
        "request_id": REQUEST_ID,
        "submission_id": str(env.fixture.submission_id),
        "status": WorkflowStatus.PAUSED,
        "current_node": PENDING_REVIEW,
        "current_answer_id": answer_id,
        "current_answer_order": 2,
        "retry_count": 0,
        "pause_reason": PAUSE_REASON,
        "resumable": True,
        "review_status": ReviewStatus.PENDING_REVIEW,
        "grading_results": {answer_id: _grading_result(env, answer_id=answer_id)},
        "confidence_decisions": {answer_id: _decision(answer_id=answer_id)},
    }
    state.update(overrides)
    return state


def _runtime_checkpoint(checkpoint_id: str, *, parent: str | None = None) -> dict[str, Any]:
    """构造一个 serde 可序列化的 LangGraph 运行时检查点（单元级替身）。"""

    del parent
    return {
        "v": 4,
        "id": checkpoint_id,
        "ts": FIXED_NOW.isoformat(),
        "channel_values": {"current_node": PENDING_REVIEW},
        "channel_versions": {"current_node": "00000000000000000000000000000001.0.5"},
        "versions_seen": {},
        "updated_channels": None,
    }


def _initial_state(env: CheckpointEnv) -> dict[str, Any]:
    """构造“运行已创建、尚未暂停”的初始业务状态（模拟 T076 启动入口）。"""

    return {
        "workflow_id": WORKFLOW_ID,
        "request_id": REQUEST_ID,
        "submission_id": str(env.fixture.submission_id),
        "status": WorkflowStatus.RUNNING,
        "current_node": LOAD_SUBMISSION,
    }


def _add_second_submission(env: CheckpointEnv) -> Submission:
    """同一数据库中新增一份属于其它学生的答卷，用于跨答卷覆盖检查。"""

    student = User(username="student-2", email="student2@example.com", password_hash="hashed")
    env.session.add(student)
    env.session.commit()
    submission = Submission(
        exam_id=env.fixture.exam_id,
        student_id=student.id,
        status=SubmissionStatus.SUBMITTED,
    )
    env.session.add(submission)
    env.session.commit()
    return submission


def _add_final_exam_result(env: CheckpointEnv) -> ExamResult:
    """写入一条最终成绩的整卷结果行，供运行记录关联最终结果引用。"""

    row = ExamResult(
        submission_id=env.fixture.submission_id,
        exam_id=env.fixture.exam_id,
        student_id=env.fixture.student_id,
        result_status=ExamResultStatus.FINAL,
        is_final=True,
        final_total_score=Decimal("16.00"),
        confirmed_subtotal=Decimal("16.00"),
        total_max_score=Decimal("20.00"),
        aggregated_at=FIXED_NOW,
    )
    env.session.add(row)
    env.session.commit()
    return row


# ---------------------------------------------------------------- T072 图替身


class _StubGradingAgent:
    """逐题返回预置评分输出；主观题低置信度触发待复核暂停。"""

    def __init__(self, *, submission_id: str) -> None:
        self.submission_id = submission_id
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
        self.calls.append(target.answer_id)
        if target.question_type is QuestionType.SINGLE_CHOICE:
            score, confidence, needs_review = 10.0, 1.0, False
        else:
            score, confidence, needs_review = 6.0, 0.3, True
        result = GradingResult(
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
            review_status=(
                ReviewStatus.PENDING_REVIEW.value
                if needs_review
                else ReviewStatus.NOT_REQUIRED.value
            ),
            answer_id=target.answer_id,
            submission_id=self.submission_id,
        )
        decision = ConfidenceDecisionDTO(
            confidence=confidence,
            threshold=0.8,
            requires_review=needs_review,
            review_status=(
                ReviewStatus.PENDING_REVIEW.value
                if needs_review
                else ReviewStatus.NOT_REQUIRED.value
            ),
            grading_status="Pending Review" if needs_review else "Accepted",
            reason="置信度低于阈值，已进入人工复核队列。" if needs_review else "自动接受。",
        )
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
    """T072 诊断节点依赖的诊断服务替身。"""

    def __init__(self) -> None:
        self.calls: list[ExamResultDTO] = []

    async def generate(self, exam_result: ExamResultDTO) -> DiagnosisReportDTO:
        self.calls.append(exam_result)
        return DiagnosisReportDTO(
            submission_id=exam_result.submission_id,
            student_id=exam_result.student_id,
            status=DiagnosisStatus.READY,
            generated_at=FIXED_NOW,
            source_exam_result_updated_at=exam_result.aggregated_at,
        )


def _snapshot(env: CheckpointEnv) -> SubmissionSnapshot:
    """构造与答卷夹具一致的权威快照（混合题型）。"""

    return SubmissionSnapshot(
        submission_id=str(env.fixture.submission_id),
        exam_id=str(env.fixture.exam_id),
        student_id=str(env.fixture.student_id),
        course_id=str(env.fixture.course_id),
        status="Submitted",
        answers=(
            GradingTargetAnswer(
                order=1,
                answer_id=str(env.fixture.objective_answer_id),
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
                answer_id=str(env.fixture.subjective_answer_id),
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


def _workflow(env: CheckpointEnv, *, agent: Any, checkpointer: Any) -> GradingWorkflow:
    """构造 T072 图工作流；检查点由调用方注入（本文件只注入数据库实现）。"""

    deps = GradingWorkflowDeps(
        snapshot=_snapshot(env),
        agent=agent,
        diagnosis_service=_StubDiagnosisService(),
        settings=build_test_settings(confidence_threshold=0.8),
    )
    return GradingWorkflow(deps, checkpointer=checkpointer)


# ---------------------------------------------------------------- 业务快照用例


def test_save_then_load_round_trips_state_and_run_control(env: CheckpointEnv) -> None:
    """保存后由另一个存储实例读回：运行事实与业务状态必须完全一致。"""

    store = _store(env)
    state = _state(env)
    row = store.save_checkpoint(WORKFLOW_ID, state, PENDING_REVIEW, thread_id=THREAD_ID)

    assert row.workflow_id == WORKFLOW_ID
    assert row.request_id == REQUEST_ID
    assert row.submission_id == env.fixture.submission_id
    assert row.current_node == PENDING_REVIEW
    assert row.current_answer_id == env.fixture.subjective_answer_id
    assert row.status is WorkflowStatus.PAUSED
    assert row.pause_reason == PAUSE_REASON
    assert row.retry_count == 0
    # 只有业务快照、没有 runtime 检查点时不得声明可恢复（H02）。
    assert row.resumable is False
    assert runtime_has_checkpoint(row.checkpoint, THREAD_ID) is False

    reread = _store(env)
    loaded = reread.load_checkpoint(WORKFLOW_ID)
    assert loaded is not None
    assert loaded.id == row.id
    assert reread.restore_state(loaded) == workflow_state_from_json(
        workflow_state_to_json(state)
    )


def test_payload_is_strict_json_and_versioned(env: CheckpointEnv) -> None:
    """载荷必须是 T065 版本化快照（严格 JSON），并记录落库时间与线程标识。"""

    store = _store(env)
    row = store.save_checkpoint(WORKFLOW_ID, _state(env), PENDING_REVIEW, thread_id=THREAD_ID)
    payload = row.checkpoint
    assert isinstance(payload, dict)

    assert payload["kind"] == WORKFLOW_STATE_PAYLOAD_KIND
    assert payload["version"] == WORKFLOW_STATE_PAYLOAD_VERSION
    assert isinstance(payload[CHECKPOINT_STATE_KEY], dict)
    assert payload[CHECKPOINT_SAVED_AT_KEY] == FIXED_NOW.isoformat()
    assert checkpoint_thread_id(row) == THREAD_ID
    # 严格 JSON：不含 NaN/Infinity，也不依赖 Python 专用类型。
    json.dumps(payload, ensure_ascii=False, allow_nan=False)


def test_resumable_is_not_claimed_without_state_support(env: CheckpointEnv) -> None:
    """暂停事实照实记录，但“可恢复”必须由状态声明与持久 runtime 检查点共同支撑。"""

    store = _store(env)
    paused_without_claim = _state(env, resumable=False)
    row = store.save_checkpoint(
        WORKFLOW_ID, paused_without_claim, PENDING_REVIEW, thread_id=THREAD_ID
    )

    assert row.status is WorkflowStatus.PAUSED
    assert row.pause_reason == PAUSE_REASON
    assert row.resumable is False
    assert store.list_resumable() == []

    running_with_claim = _state(
        env,
        status=WorkflowStatus.RUNNING,
        pause_reason=None,
        resumable=True,
        review_status=None,
        confidence_decisions={},
    )
    resumed = store.save_checkpoint(
        WORKFLOW_ID, running_with_claim, PENDING_REVIEW, thread_id=THREAD_ID
    )
    assert resumed.status is WorkflowStatus.RUNNING
    assert resumed.resumable is False


def test_paused_state_without_reason_is_rejected(env: CheckpointEnv) -> None:
    """T065：暂停状态必须给出暂停原因，不得静默暂停。"""

    store = _store(env)
    with pytest.raises(WorkflowCheckpointStateError) as error:
        store.save_checkpoint(
            WORKFLOW_ID,
            _state(env, pause_reason=None),
            PENDING_REVIEW,
        )

    assert error.value.error_code == WORKFLOW_CHECKPOINT_STATE_INVALID


def test_explicit_pause_reason_promotes_running_state(env: CheckpointEnv) -> None:
    """显式暂停原因把 Running 提升为 Paused，避免“有原因却仍在运行”的矛盾记录。"""

    store = _store(env)
    state = _state(
        env,
        status=WorkflowStatus.RUNNING,
        pause_reason=None,
        resumable=False,
        review_status=None,
        confidence_decisions={},
    )
    row = store.save_checkpoint(WORKFLOW_ID, state, PENDING_REVIEW, PAUSE_REASON, thread_id=THREAD_ID)

    assert row.status is WorkflowStatus.PAUSED
    assert row.pause_reason == PAUSE_REASON
    assert row.resumable is False
    restored = store.restore_state(row)
    assert restored["status"] is WorkflowStatus.PAUSED
    assert restored["pause_reason"] == PAUSE_REASON


def test_pause_reason_rejected_for_terminal_states(env: CheckpointEnv) -> None:
    """已失败或已完成的工作流不允许再写暂停原因。"""

    store = _store(env)
    failed = _state(
        env,
        status=WorkflowStatus.FAILED,
        pause_reason=None,
        resumable=False,
        review_status=None,
        error=AgentError(error_code="GRADING_FAILED", message="阅卷失败。"),
    )
    with pytest.raises(WorkflowCheckpointError) as error:
        store.save_checkpoint(WORKFLOW_ID, failed, PENDING_REVIEW, PAUSE_REASON)

    assert error.value.error_code == WORKFLOW_CHECKPOINT_INVALID_INPUT


@pytest.mark.parametrize(
    ("overrides", "expected_code"),
    [
        ({"submission_id": None}, WORKFLOW_CHECKPOINT_INVALID_INPUT),
        ({"request_id": ""}, WORKFLOW_CHECKPOINT_INVALID_INPUT),
        ({"retry_count": -1}, WORKFLOW_CHECKPOINT_INVALID_INPUT),
        ({"retry_count": True}, WORKFLOW_CHECKPOINT_INVALID_INPUT),
        ({"current_answer_id": "answer-1"}, WORKFLOW_CHECKPOINT_INVALID_INPUT),
        (
            {"status": WorkflowStatus.FAILED, "pause_reason": None, "resumable": False},
            WORKFLOW_CHECKPOINT_STATE_INVALID,
        ),
        (
            {
                "status": WorkflowStatus.FAILED,
                "pause_reason": None,
                "resumable": True,
                "error": AgentError(error_code="GRADING_FAILED", message="阅卷失败。"),
            },
            WORKFLOW_CHECKPOINT_STATE_INVALID,
        ),
        ({"unknown_field": "x"}, WORKFLOW_CHECKPOINT_STATE_INVALID),
    ],
)
def test_invalid_state_is_rejected(
    env: CheckpointEnv,
    overrides: dict[str, Any],
    expected_code: str,
) -> None:
    """缺身份、非法枚举、负计数、非 UUID 答案标识与未知字段都必须显式失败。"""

    store = _store(env)
    with pytest.raises(WorkflowCheckpointError) as error:
        store.save_checkpoint(WORKFLOW_ID, _state(env, **overrides), PENDING_REVIEW)

    assert error.value.error_code == expected_code


def test_blank_workflow_id_and_missing_state_are_rejected(env: CheckpointEnv) -> None:
    """空白标识与空状态都不得落库。"""

    store = _store(env)
    with pytest.raises(WorkflowCheckpointError):
        store.save_checkpoint("   ", _state(env), PENDING_REVIEW)
    with pytest.raises(WorkflowCheckpointError):
        store.save_checkpoint(WORKFLOW_ID, {}, PENDING_REVIEW)


def test_state_of_other_workflow_is_rejected(env: CheckpointEnv) -> None:
    """状态自带的工作流标识与目标不一致时拒绝，避免跨工作流写入。"""

    store = _store(env)
    with pytest.raises(WorkflowCheckpointOwnershipError) as error:
        store.save_checkpoint(
            WORKFLOW_ID,
            _state(env, workflow_id="workflow-other"),
            PENDING_REVIEW,
        )

    assert error.value.error_code == WORKFLOW_CHECKPOINT_OWNERSHIP_MISMATCH


def test_existing_run_bound_to_other_submission_is_rejected(env: CheckpointEnv) -> None:
    """同一 ``workflow_id`` 已绑定其它答卷或其它请求时不得覆盖。"""

    store = _store(env)
    store.save_checkpoint(WORKFLOW_ID, _state(env), PENDING_REVIEW)
    other = _add_second_submission(env)

    with pytest.raises(WorkflowCheckpointOwnershipError):
        store.save_checkpoint(
            WORKFLOW_ID,
            _state(
                env,
                submission_id=str(other.id),
                grading_results={},
                confidence_decisions={},
            ),
            PENDING_REVIEW,
        )
    with pytest.raises(WorkflowCheckpointOwnershipError):
        store.save_checkpoint(WORKFLOW_ID, _state(env, request_id="request-other"), PENDING_REVIEW)

    assert env.session.query(WorkflowRun).count() == 1


def test_list_resumable_requires_runtime_checkpoint(env: CheckpointEnv) -> None:
    """可恢复列表只返回确实持有 runtime 检查点的暂停运行。"""

    store = _store(env)
    saver = _saver(env)
    store.save_checkpoint(WORKFLOW_ID, _initial_state(env), LOAD_SUBMISSION, thread_id=THREAD_ID)
    saver.put(
        {"configurable": {"thread_id": THREAD_ID, "checkpoint_ns": ""}},
        _runtime_checkpoint("ckpt-1"),
        {"source": "loop", "step": 1},
        {"current_node": "00000000000000000000000000000001.0.5"},
    )
    paused = store.save_checkpoint(
        WORKFLOW_ID, _state(env), PENDING_REVIEW, thread_id=THREAD_ID
    )
    store.save_checkpoint(
        "workflow-other",
        _state(
            env,
            workflow_id="workflow-other",
            status=WorkflowStatus.RUNNING,
            pause_reason=None,
            resumable=False,
            review_status=None,
            confidence_decisions={},
        ),
        "next_answer",
    )

    assert paused.resumable is True
    assert [row.workflow_id for row in store.list_resumable()] == [WORKFLOW_ID]
    assert [row.workflow_id for row in store.list_resumable(WORKFLOW_ID)] == [WORKFLOW_ID]
    assert store.list_resumable("workflow-other") == []
    assert store.runtime_ready(WORKFLOW_ID, THREAD_ID) is True
    assert store.runtime_ready("workflow-other", THREAD_ID) is False


def test_mark_completed_links_final_exam_result(env: CheckpointEnv) -> None:
    """完成标记核验通过后写入终态，并按答卷关联最终整卷结果。"""

    store = _store(env)
    accepted = _state(
        env,
        status=WorkflowStatus.RUNNING,
        pause_reason=None,
        resumable=False,
        review_status=ReviewStatus.NOT_REQUIRED,
        confidence_decisions={},
    )
    store.save_checkpoint(WORKFLOW_ID, accepted, UNIFIED_RESULT, thread_id=THREAD_ID)
    exam_result = _add_final_exam_result(env)

    row = store.mark_completed(WORKFLOW_ID)

    assert row.status is WorkflowStatus.COMPLETED
    assert row.resumable is False
    assert row.pause_reason is None
    assert row.exam_result_id == exam_result.id
    assert checkpoint_thread_id(row) == THREAD_ID
    restored = store.restore_state(row)
    assert restored["status"] is WorkflowStatus.COMPLETED
    assert restored["error"] is None
    assert store.list_resumable() == []


def test_mark_completed_accepts_explicit_exam_result_reference(env: CheckpointEnv) -> None:
    """显式给出的最终结果引用优先于自动查找。"""

    store = _store(env)
    store.save_checkpoint(
        WORKFLOW_ID,
        _state(
            env,
            status=WorkflowStatus.RUNNING,
            pause_reason=None,
            resumable=False,
            review_status=None,
            confidence_decisions={},
        ),
        GENERATE_DIAGNOSIS,
    )
    exam_result = _add_final_exam_result(env)

    row = store.mark_completed(WORKFLOW_ID, exam_result_id=str(exam_result.id))

    assert row.exam_result_id == exam_result.id


def test_mark_completed_rejects_pending_review_and_failure_facts(env: CheckpointEnv) -> None:
    """存在待复核题目或保留失败事实时不得宣称完成。"""

    store = _store(env)
    store.save_checkpoint(WORKFLOW_ID, _state(env), PENDING_REVIEW)
    with pytest.raises(WorkflowCheckpointCompletionError) as error:
        store.mark_completed(WORKFLOW_ID)
    assert error.value.error_code == WORKFLOW_CHECKPOINT_COMPLETION_NOT_ALLOWED
    unchanged = store.load_checkpoint(WORKFLOW_ID)
    assert unchanged is not None
    assert unchanged.status is WorkflowStatus.PAUSED

    failed_state = _state(
        env,
        workflow_id="workflow-failed",
        status=WorkflowStatus.FAILED,
        pause_reason=None,
        resumable=False,
        review_status=None,
        confidence_decisions={},
        error=AgentError(error_code="GRADING_WORKFLOW_DIAGNOSIS_FAILED", message="诊断失败。"),
    )
    store.save_checkpoint("workflow-failed", failed_state, GENERATE_DIAGNOSIS)
    with pytest.raises(WorkflowCheckpointCompletionError):
        store.mark_completed("workflow-failed")


def test_mark_failed_records_sanitized_error(env: CheckpointEnv) -> None:
    """失败标记写入脱敏错误、清空暂停原因与复核状态，并保留已形成的集合。"""

    store = _store(env)
    store.save_checkpoint(WORKFLOW_ID, _state(env), PENDING_REVIEW, thread_id=THREAD_ID)

    row = store.mark_failed(WORKFLOW_ID, "Provider 超时，重试已用尽。")

    assert row.status is WorkflowStatus.FAILED
    assert row.resumable is False
    assert row.pause_reason is None
    assert checkpoint_thread_id(row) == THREAD_ID
    restored = store.restore_state(row)
    assert restored["status"] is WorkflowStatus.FAILED
    assert restored["resumable"] is False
    assert restored["review_status"] is None
    assert restored["error"] == AgentError(
        error_code=WORKFLOW_CHECKPOINT_FAILED,
        message="Provider 超时，重试已用尽。",
    )
    # 已形成的逐题结果不因失败被丢弃或改写。
    assert set(restored["grading_results"]) == {str(env.fixture.subjective_answer_id)}


def test_mark_failed_consumes_agent_error_and_exception(env: CheckpointEnv) -> None:
    """AgentError 与平台异常的脱敏错误码和可重试语义必须保留。"""

    store = _store(env)
    store.save_checkpoint(WORKFLOW_ID, _state(env), PENDING_REVIEW)
    explicit = AgentError(error_code="GRADING_WORKFLOW_AGGREGATION_FAILED", message="汇总失败。")
    row = store.mark_failed(WORKFLOW_ID, explicit)
    assert store.restore_state(row)["error"] == explicit

    store.save_checkpoint("workflow-2", _state(env, workflow_id="workflow-2"), PENDING_REVIEW)
    failure = WorkflowCheckpointError("检查点写失败。", error_code="CHECKPOINT_WRITE_FAILED")
    second = store.mark_failed("workflow-2", failure)
    restored = store.restore_state(second)
    assert restored["error"] == AgentError(
        error_code="CHECKPOINT_WRITE_FAILED",
        message=str(failure),
        retryable=False,
    )


def test_mark_operations_require_usable_snapshot(env: CheckpointEnv) -> None:
    """缺少可用状态快照时拒绝伪造完成或失败状态。"""

    env.session.add(
        WorkflowRun(
            workflow_id="workflow-no-checkpoint",
            request_id=REQUEST_ID,
            submission_id=env.fixture.submission_id,
            status=WorkflowStatus.RUNNING,
            checkpoint=None,
        )
    )
    env.session.commit()
    store = _store(env)

    with pytest.raises(WorkflowCheckpointStateError):
        store.mark_completed("workflow-no-checkpoint")
    with pytest.raises(WorkflowCheckpointStateError):
        store.mark_failed("workflow-no-checkpoint", "失败。")
    with pytest.raises(WorkflowCheckpointStateError):
        store.restore_state("workflow-no-checkpoint")


def test_mark_operations_reject_unknown_workflow(env: CheckpointEnv) -> None:
    """未知工作流必须显式报缺记录，而不是静默成功。"""

    store = _store(env)
    with pytest.raises(WorkflowCheckpointNotFoundError) as error:
        store.mark_completed("workflow-missing")
    assert error.value.error_code == WORKFLOW_CHECKPOINT_NOT_FOUND
    with pytest.raises(WorkflowCheckpointNotFoundError):
        store.mark_failed("workflow-missing", "失败。")
    with pytest.raises(WorkflowCheckpointNotFoundError):
        store.restore_state("workflow-missing")
    assert store.load_checkpoint("workflow-missing") is None


def test_unknown_status_label_is_rejected(env: CheckpointEnv) -> None:
    """状态标签非法时给出明确错误码，不静默按 Running 处理。"""

    store = _store(env)
    with pytest.raises(WorkflowCheckpointError) as error:
        store.save_checkpoint(WORKFLOW_ID, _state(env, status="Archived"), PENDING_REVIEW)
    assert error.value.error_code == WORKFLOW_CHECKPOINT_INVALID_INPUT


def test_session_factory_path_persists_and_reads_back(env: CheckpointEnv) -> None:
    """会话工厂路径同样可持久化并跨实例读回（自建会话在使用后关闭）。"""

    factory_store = _store(env, session=None, session_factory=lambda: Session(env.engine))
    row = factory_store.save_checkpoint(WORKFLOW_ID, _state(env), PENDING_REVIEW)

    loaded = _store(env).load_checkpoint(row.workflow_id)
    assert loaded is not None
    assert loaded.id == row.id
    assert loaded.checkpoint == row.checkpoint


def test_store_without_session_source_and_mixed_injection(env: CheckpointEnv) -> None:
    """未注入会话源时显式报未就绪；同时注入两种会话源即拒绝。"""

    not_ready = WorkflowCheckpointStore()
    with pytest.raises(WorkflowCheckpointStoreNotReadyError) as error:
        not_ready.ensure_ready()
    assert error.value.error_code == WORKFLOW_CHECKPOINT_STORE_NOT_READY
    with pytest.raises(WorkflowCheckpointStoreNotReadyError):
        not_ready.load_checkpoint(WORKFLOW_ID)

    with pytest.raises(WorkflowCheckpointError):
        WorkflowCheckpointStore(session=env.session, session_factory=lambda: Session(env.engine))
    with pytest.raises(WorkflowCheckpointError):
        DatabaseCheckpointSaver(session=env.session, session_factory=lambda: Session(env.engine))


def test_ensure_ready_detects_missing_table(env: CheckpointEnv) -> None:
    """表缺失或不可连接即视为未就绪；saver 同样具备就绪判断。"""

    store = _store(env, session=None, session_factory=lambda: Session(env.engine))
    store.ensure_ready()
    _saver(env).ensure_ready()

    Base.metadata.drop_all(env.engine)
    with pytest.raises(WorkflowCheckpointStoreNotReadyError):
        store.ensure_ready()


def test_pending_review_answer_ids_consumes_accepted_states(env: CheckpointEnv) -> None:
    """待复核集合按 T071 已接受状态判定：已确认的答案不再计入。"""

    answer_id = str(env.fixture.subjective_answer_id)
    objective_id = str(env.fixture.objective_answer_id)
    state = _state(
        env,
        confidence_decisions={
            answer_id: _decision(answer_id=answer_id),
            objective_id: _decision(answer_id=objective_id, review_status="Confirmed"),
        },
    )

    assert pending_review_answer_ids(state) == (answer_id,)


# ---------------------------------------------------------------- runtime 载荷用例


def test_runtime_envelope_coexists_with_business_state(env: CheckpointEnv) -> None:
    """runtime 段与 T065 state 段共存于同一 JSON 列且互不覆盖。"""

    store = _store(env)
    saver = _saver(env)
    store.save_checkpoint(WORKFLOW_ID, _initial_state(env), LOAD_SUBMISSION, thread_id=THREAD_ID)
    config = {"configurable": {"thread_id": THREAD_ID, "checkpoint_ns": ""}}

    returned = saver.put(
        config,
        _runtime_checkpoint("ckpt-1"),
        {"source": "loop", "step": 1},
        {"current_node": "00000000000000000000000000000001.0.5"},
    )

    assert returned["configurable"]["checkpoint_id"] == "ckpt-1"
    row = store.load_checkpoint(WORKFLOW_ID)
    assert row is not None
    payload = row.checkpoint
    assert isinstance(payload, dict)
    # 业务段仍在，且仍可经 T065 解码。
    assert payload["kind"] == WORKFLOW_STATE_PAYLOAD_KIND
    assert store.restore_state(row)["status"] is WorkflowStatus.RUNNING
    # runtime 段记录了线程、命名空间、检查点标识与通道版本。
    runtime = payload[RUNTIME_ENVELOPE_KEY]
    assert runtime["kind"] == RUNTIME_ENVELOPE_KIND
    assert runtime["version"] == RUNTIME_ENVELOPE_VERSION
    assert runtime["thread_id"] == THREAD_ID
    assert runtime["namespaces"][""]["latest_checkpoint_id"] == "ckpt-1"
    entry = runtime["namespaces"][""]["entries"][0]
    assert entry["checkpoint_id"] == "ckpt-1"
    assert entry["checkpoint_ns"] == ""
    assert entry["channel_versions"] == {
        "current_node": "00000000000000000000000000000001.0.5"
    }
    assert entry["saved_at"] == FIXED_NOW.isoformat()
    assert json.dumps(payload, ensure_ascii=False, allow_nan=False)

    tuple_read = saver.get_tuple(config)
    assert tuple_read is not None
    assert tuple_read.checkpoint["id"] == "ckpt-1"
    assert tuple_read.checkpoint["channel_values"] == {"current_node": PENDING_REVIEW}
    assert tuple_read.metadata["source"] == "loop"
    assert tuple_read.parent_config is None
    assert runtime_checkpoint_ids(row.checkpoint, THREAD_ID) == ("ckpt-1",)


def test_runtime_history_is_bounded_and_drops_old_writes(env: CheckpointEnv) -> None:
    """保留上限只保留最新检查点，被丢弃检查点的写入一并清除。"""

    store = _store(env)
    saver = _saver(env, history_limit=2)
    store.save_checkpoint(WORKFLOW_ID, _initial_state(env), LOAD_SUBMISSION, thread_id=THREAD_ID)
    config: dict[str, Any] = {"configurable": {"thread_id": THREAD_ID, "checkpoint_ns": ""}}
    for index in range(1, 4):
        config = saver.put(
            config,
            _runtime_checkpoint(f"ckpt-{index}"),
            {"source": "loop", "step": index},
            {"current_node": f"0000000000000000000000000000000{index}.0.5"},
        )
        saver.put_writes(
            config,
            [("value", index)],
            task_id=f"task-{index}",
        )

    row = store.load_checkpoint(WORKFLOW_ID)
    assert row is not None
    assert runtime_checkpoint_ids(row.checkpoint, THREAD_ID) == ("ckpt-2", "ckpt-3")
    latest = saver.get_tuple({"configurable": {"thread_id": THREAD_ID}})
    assert latest is not None
    assert latest.checkpoint["id"] == "ckpt-3"
    assert runtime_pending_write_count(row.checkpoint, THREAD_ID) == 1
    oldest = saver.get_tuple(
        {"configurable": {"thread_id": THREAD_ID, "checkpoint_id": "ckpt-1"}}
    )
    assert oldest is None


def test_put_writes_merge_semantics_and_pending_write_readback(env: CheckpointEnv) -> None:
    """固定槽位覆盖、普通通道追加，并能从持久层读回 pending writes 与父配置。"""

    store = _store(env)
    saver = _saver(env)
    store.save_checkpoint(WORKFLOW_ID, _initial_state(env), LOAD_SUBMISSION, thread_id=THREAD_ID)
    config: dict[str, Any] = {"configurable": {"thread_id": THREAD_ID, "checkpoint_ns": ""}}
    first = saver.put(config, _runtime_checkpoint("ckpt-1"), {"source": "loop", "step": 1}, {})
    saver.put_writes(first, [("value", 1), ("value", 2)], task_id="task-a")
    saver.put_writes(first, [(INTERRUPT_CHANNEL, ["ask"])], task_id="task-b")
    saver.put_writes(first, [(INTERRUPT_CHANNEL, ["ask-again"])], task_id="task-b")
    second = saver.put(
        first,
        _runtime_checkpoint("ckpt-2"),
        {"source": "loop", "step": 2},
        {"value": "00000000000000000000000000000002.0.5"},
    )

    stored = saver.get_tuple({"configurable": {"thread_id": THREAD_ID, "checkpoint_id": "ckpt-1"}})
    assert stored is not None
    assert [(item[1], item[2]) for item in stored.pending_writes or []] == [
        ("value", 1),
        ("value", 2),
        (INTERRUPT_CHANNEL, ["ask-again"]),
    ]
    latest = saver.get_tuple({"configurable": {"thread_id": THREAD_ID}})
    assert latest is not None
    assert latest.checkpoint["id"] == "ckpt-2"
    assert latest.parent_config == {
        "configurable": {
            "thread_id": THREAD_ID,
            "checkpoint_ns": "",
            "checkpoint_id": "ckpt-1",
        }
    }
    assert latest.pending_writes == []
    second_config = second["configurable"]
    assert second_config["checkpoint_id"] == "ckpt-2"


def test_unknown_thread_and_checkpoint_return_none(env: CheckpointEnv) -> None:
    """未知 thread 或未保留的 checkpoint_id 返回 ``None``，不伪造检查点。"""

    store = _store(env)
    saver = _saver(env)
    store.save_checkpoint(WORKFLOW_ID, _initial_state(env), LOAD_SUBMISSION, thread_id=THREAD_ID)
    saver.put(
        {"configurable": {"thread_id": THREAD_ID}},
        _runtime_checkpoint("ckpt-1"),
        {"source": "loop", "step": 1},
        {},
    )

    assert saver.get_tuple({"configurable": {"thread_id": "thread-unknown"}}) is None
    assert (
        saver.get_tuple({"configurable": {"thread_id": THREAD_ID, "checkpoint_id": "ckpt-其他"}})
        is None
    )
    assert saver.runtime_summary("thread-unknown") is None
    # 空配置可列出持有 runtime 检查点的运行；未持有的命名空间不返回任何条目。
    assert len(list(saver.list(None, limit=1))) == 1
    assert list(saver.list({"configurable": {"thread_id": THREAD_ID, "checkpoint_ns": "subgraph"}})) == []


def test_unbound_thread_write_is_rejected_then_bind_recovers(env: CheckpointEnv) -> None:
    """未绑定线程时写入显式失败；绑定后写入成功且可从持久层解析。"""

    store = _store(env)
    saver = _saver(env)
    store.save_checkpoint("workflow-unbound", _state(env, workflow_id="workflow-unbound"), PENDING_REVIEW)

    with pytest.raises(WorkflowCheckpointThreadUnboundError) as error:
        saver.put(
            {"configurable": {"thread_id": "thread-unbound"}},
            _runtime_checkpoint("ckpt-1"),
            {"source": "loop", "step": 1},
            {},
        )
    assert error.value.error_code == WORKFLOW_CHECKPOINT_THREAD_UNBOUND

    bound = saver.bind_thread("thread-unbound", "workflow-unbound")
    assert bound.workflow_id == "workflow-unbound"
    saver.put(
        {"configurable": {"thread_id": "thread-unbound"}},
        _runtime_checkpoint("ckpt-1"),
        {"source": "loop", "step": 1},
        {},
    )
    # 新实例不带任何内存缓存，也能按 thread_id 从数据库解析。
    fresh = _saver(env)
    assert fresh.get_tuple({"configurable": {"thread_id": "thread-unbound"}}) is not None
    with pytest.raises(WorkflowCheckpointError):
        saver.bind_thread("thread-other", "workflow-unbound")


def test_pickle_payload_is_rejected(env: CheckpointEnv) -> None:
    """载荷类型为 pickle 时读写都显式拒绝，不使用任意对象反序列化。"""

    store = _store(env)
    saver = _saver(env)
    store.save_checkpoint(WORKFLOW_ID, _initial_state(env), LOAD_SUBMISSION, thread_id=THREAD_ID)
    saver.put(
        {"configurable": {"thread_id": THREAD_ID}},
        _runtime_checkpoint("ckpt-1"),
        {"source": "loop", "step": 1},
        {},
    )
    row = store.load_checkpoint(WORKFLOW_ID)
    assert row is not None
    payload = dict(row.checkpoint)
    runtime = dict(payload[RUNTIME_ENVELOPE_KEY])
    namespaces = dict(runtime["namespaces"])
    namespace = dict(namespaces[""])
    entries = [dict(entry) for entry in namespace["entries"]]
    entries[0]["checkpoint"] = {"type": "pickle", "data": "gASV"}
    namespace["entries"] = entries
    namespaces[""] = namespace
    runtime["namespaces"] = namespaces
    payload[RUNTIME_ENVELOPE_KEY] = runtime
    row.checkpoint = payload
    env.session.commit()

    with pytest.raises(WorkflowCheckpointSerdeError) as error:
        saver.get_tuple({"configurable": {"thread_id": THREAD_ID}})
    assert error.value.error_code == WORKFLOW_CHECKPOINT_SERDE_REJECTED


def test_runtime_section_survives_terminal_marks(env: CheckpointEnv) -> None:
    """终态标记不破坏 runtime 检查点；完成与失败都保留可审计的运行事实。"""

    store = _store(env)
    saver = _saver(env)
    store.save_checkpoint(WORKFLOW_ID, _initial_state(env), LOAD_SUBMISSION, thread_id=THREAD_ID)
    saver.put(
        {"configurable": {"thread_id": THREAD_ID}},
        _runtime_checkpoint("ckpt-1"),
        {"source": "loop", "step": 1},
        {},
    )
    accepted = _state(
        env,
        status=WorkflowStatus.RUNNING,
        pause_reason=None,
        resumable=False,
        review_status=ReviewStatus.NOT_REQUIRED,
        confidence_decisions={},
    )
    store.save_checkpoint(WORKFLOW_ID, accepted, UNIFIED_RESULT, thread_id=THREAD_ID)

    completed = store.mark_completed(WORKFLOW_ID)
    assert runtime_has_checkpoint(completed.checkpoint, THREAD_ID) is True
    assert saver.get_tuple({"configurable": {"thread_id": THREAD_ID}}) is not None

    store.save_checkpoint(
        "workflow-failed",
        _state(env, workflow_id="workflow-failed"),
        PENDING_REVIEW,
        thread_id="thread-failed",
    )
    saver.put(
        {"configurable": {"thread_id": "thread-failed"}},
        _runtime_checkpoint("ckpt-failed"),
        {"source": "loop", "step": 1},
        {},
    )
    failed = store.mark_failed("workflow-failed", "Provider 超时。")
    assert failed.status is WorkflowStatus.FAILED
    # 终态标记不删除 runtime 检查点：失败仍可审计，也不谎称可恢复。
    assert runtime_has_checkpoint(failed.checkpoint, "thread-failed") is True
    assert runtime_has_checkpoint(failed.checkpoint, THREAD_ID) is False
    assert failed.resumable is False

    saver.delete_thread(THREAD_ID)
    cleared = store.load_checkpoint(WORKFLOW_ID)
    assert cleared is not None
    assert cleared.resumable is False
    assert RUNTIME_ENVELOPE_KEY not in (cleared.checkpoint or {})


# ---------------------------------------------------------------- 跨实例恢复


def test_cross_instance_resume_recovers_original_workflow(file_env: CheckpointEnv) -> None:
    """T073：真实 T072 图的暂停必须能跨实例恢复（不依赖任何内存检查点）。

    实例 A：数据库 Checkpointer + 混合答卷图 → 低置信度在 ``Pending Review`` 真正中断；
    销毁实例 A 的会话与对象后，实例 B 仅按同一 ``thread_id`` 从数据库读回检查点与待恢复任务，
    用原 ``workflow_id``/``request_id`` 恢复并把教师结论推进到完成。
    """

    env = file_env

    store_a = _store(env)
    saver_a = _saver(env)
    store_a.save_checkpoint(WORKFLOW_ID, _initial_state(env), LOAD_SUBMISSION, thread_id=THREAD_ID)
    agent_a = _StubGradingAgent(submission_id=str(env.fixture.submission_id))
    workflow_a = _workflow(env, agent=agent_a, checkpointer=saver_a)

    paused = _run(
        workflow_a.run_async(
            request_id=REQUEST_ID,
            workflow_id=WORKFLOW_ID,
            submission_id=str(env.fixture.submission_id),
            thread_id=THREAD_ID,
        )
    )

    assert paused.interrupted is True
    assert agent_a.calls == [
        str(env.fixture.objective_answer_id),
        str(env.fixture.subjective_answer_id),
    ]
    state_a = dict(paused.state)
    saved = store_a.save_checkpoint(
        WORKFLOW_ID,
        state_a,
        PENDING_REVIEW,
        pause_reason=state_a["pause_reason"],
        thread_id=THREAD_ID,
    )

    # 数据库中的暂停事实：状态、当前题、暂停原因与待恢复任务都已在持久层。
    assert saved.status is WorkflowStatus.PAUSED
    assert saved.resumable is True
    stored_state = store_a.restore_state(saved)
    assert stored_state["current_answer_id"] == str(env.fixture.subjective_answer_id)
    assert stored_state["review_status"] == ReviewStatus.PENDING_REVIEW
    assert stored_state["retry_count"] == 0
    assert set(stored_state["grading_results"]) == {
        str(env.fixture.objective_answer_id),
        str(env.fixture.subjective_answer_id),
    }
    pending_before = saver_a.get_tuple({"configurable": {"thread_id": THREAD_ID}})
    assert pending_before is not None
    assert any(
        channel == INTERRUPT_CHANNEL for _, channel, _ in (pending_before.pending_writes or [])
    )
    assert saver_a.runtime_summary(THREAD_ID)["pending_write_count"] >= 1  # type: ignore[index]

    # 销毁实例 A：不再持有任何会话或内存状态（仅保留同一个数据库引擎）。
    env.session.close()
    del saver_a, workflow_a, agent_a, pending_before, paused

    # 实例 B：全新 Checkpointer 与全新编译的图，只从数据库读取检查点。
    saver_b = DatabaseCheckpointSaver(
        session_factory=lambda: Session(env.engine), clock=lambda: FIXED_NOW
    )
    agent_b = _StubGradingAgent(submission_id=str(env.fixture.submission_id))
    workflow_b = _workflow(env, agent=agent_b, checkpointer=saver_b)
    assert saver_b.get_tuple({"configurable": {"thread_id": THREAD_ID}}) is not None

    decision = TeacherReviewDecision(
        workflow_id=WORKFLOW_ID,
        thread_id=THREAD_ID,
        answer_id=str(env.fixture.subjective_answer_id),
        review_status=ReviewStatus.CONFIRMED.value,
        expected_review_status=ReviewStatus.PENDING_REVIEW.value,
    )
    resumed = _run(workflow_b.apply_teacher_decision_async(decision, resume=True))

    assert resumed is not None
    assert resumed.interrupted is False
    assert resumed.state["status"] is WorkflowStatus.COMPLETED
    assert resumed.state["workflow_id"] == WORKFLOW_ID
    assert resumed.state["request_id"] == REQUEST_ID
    assert resumed.state["retry_count"] == 0
    assert set(resumed.state["grading_results"]) == {
        str(env.fixture.objective_answer_id),
        str(env.fixture.subjective_answer_id),
    }
    assert resumed.state["exam_result"].is_final is True
    assert resumed.state["diagnosis"] is not None
    # 已完成题目不在实例 B 重新评分：恢复使用持久检查点而不是重跑整卷。
    assert agent_b.calls == []

    # 恢复完成后的运行事实由业务快照如实记录（runtime 检查点仍在，便于追踪）。
    final = WorkflowCheckpointStore(
        session_factory=lambda: Session(env.engine), clock=lambda: FIXED_NOW
    ).save_checkpoint(
        WORKFLOW_ID,
        dict(resumed.state),
        GENERATE_DIAGNOSIS,
        thread_id=THREAD_ID,
    )
    assert final.status is WorkflowStatus.COMPLETED
    assert final.resumable is False
    assert runtime_has_checkpoint(final.checkpoint, THREAD_ID) is True
    assert DEFAULT_RUNTIME_HISTORY_LIMIT >= 2
    with Session(env.engine) as probe:
        assert probe.query(WorkflowRun).count() == 1
