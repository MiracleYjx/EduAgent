"""T073 检查点持久化的失败优先单元测试。

TCR（测试契约记录）：

- 必要性：检查点只“能写”不等于“可恢复”。必须固定：载荷是 T065 版本化快照（可被 T072 恢复入口
  消费）、``resumable`` 不会因为“快照已落库”而被升级为真（H02）、同一 ``workflow_id`` 不会被
  另一个答卷或请求覆盖、失败与完成标记都必须可核验而不是空写状态。
- 契约依据：``.specify/plan.md`` §5、``.specify/data-model.md``（WorkflowRun 状态与关键校验规则）、
  ``backend/app/models/workflow_run.py``（T064 列）、``backend/app/ai/workflows/state.py``（T065 载荷）、
  FR-036（恢复沿用原运行标识）。
- 覆盖行为：保存/加载往返一致、严格 JSON 与版本化载荷、暂停原因与状态转换、可恢复列表筛选、
  跨答卷/跨工作流拒绝、失败/完成标记的核验与拒绝、未就绪与缺记录诊断。

测试运行在启用外键约束的内存 SQLite 上，复用 M3 的模型测试库辅助；真实 PostgreSQL 的列精度与
外键行为仍由 `alembic upgrade/check` 与一次性验证库覆盖，不由本文件替代。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session

from backend.app.ai.agents.state import AgentError
from backend.app.ai.workflows.grading_handoff import PENDING_REVIEW
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
    WorkflowStatus,
)
from backend.app.models import ExamResult, Submission, User, WorkflowRun
from backend.app.schemas.ai import GradingResult
from backend.app.schemas.grading import ConfidenceDecisionDTO, ExamResultStatus
from backend.app.services.workflow_checkpoint import (
    CHECKPOINT_SAVED_AT_KEY,
    WORKFLOW_CHECKPOINT_COMPLETION_NOT_ALLOWED,
    WORKFLOW_CHECKPOINT_FAILED,
    WORKFLOW_CHECKPOINT_INVALID_INPUT,
    WORKFLOW_CHECKPOINT_NOT_FOUND,
    WORKFLOW_CHECKPOINT_OWNERSHIP_MISMATCH,
    WORKFLOW_CHECKPOINT_STATE_INVALID,
    WORKFLOW_CHECKPOINT_STORE_NOT_READY,
    WorkflowCheckpointCompletionError,
    WorkflowCheckpointError,
    WorkflowCheckpointNotFoundError,
    WorkflowCheckpointOwnershipError,
    WorkflowCheckpointStateError,
    WorkflowCheckpointStore,
    WorkflowCheckpointStoreNotReadyError,
    checkpoint_thread_id,
    pending_review_answer_ids,
)
from tests.unit.models.sqlite_support import (
    SubmissionFixture,
    create_sqlite_engine,
    seed_submission,
    sqlite_foreign_keys_enabled,
)

WORKFLOW_ID = "workflow-t073"
REQUEST_ID = "request-t073"
THREAD_ID = "thread-t073"
FIXED_NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
PAUSE_REASON = "第 2 题评分结果需要教师复核，工作流已暂停。"


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


def _store(env: CheckpointEnv, **overrides: Any) -> WorkflowCheckpointStore:
    """构造使用固定时钟的检查点存储（默认借入同一会话，便于跨实例读回）。"""

    kwargs: dict[str, Any] = {"session": env.session, "clock": lambda: FIXED_NOW}
    kwargs.update(overrides)
    return WorkflowCheckpointStore(**kwargs)


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
    assert row.resumable is True

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
    """H02：暂停事实照实记录，但“可恢复”必须由状态自身声明。"""

    store = _store(env)
    paused_without_claim = _state(env, resumable=False)
    row = store.save_checkpoint(WORKFLOW_ID, paused_without_claim, PENDING_REVIEW)

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
    resumed = store.save_checkpoint(WORKFLOW_ID, running_with_claim, PENDING_REVIEW)
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
    row = store.save_checkpoint(WORKFLOW_ID, state, PENDING_REVIEW, PAUSE_REASON)

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


def test_list_resumable_filters_by_flag_and_workflow_id(env: CheckpointEnv) -> None:
    """可恢复列表只返回真正声明可恢复的运行，并可按工作流筛选。"""

    store = _store(env)
    store.save_checkpoint(WORKFLOW_ID, _state(env), PENDING_REVIEW)
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

    assert [row.workflow_id for row in store.list_resumable()] == [WORKFLOW_ID]
    assert [row.workflow_id for row in store.list_resumable(WORKFLOW_ID)] == [WORKFLOW_ID]
    assert store.list_resumable("workflow-other") == []


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
    store.save_checkpoint(WORKFLOW_ID, accepted, "unified_result", thread_id=THREAD_ID)
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
        "generate_diagnosis",
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
    store.save_checkpoint("workflow-failed", failed_state, "generate_diagnosis")
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


def test_ensure_ready_detects_missing_table(env: CheckpointEnv) -> None:
    """表缺失或不可连接即视为未就绪。"""

    store = _store(env, session=None, session_factory=lambda: Session(env.engine))
    store.ensure_ready()

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
