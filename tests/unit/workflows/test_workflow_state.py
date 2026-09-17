"""T065 LangGraph 阅卷工作流状态与业务状态快照失败优先测试。

任务编号：T065（M4 Agent + Workflow；实现见 `backend/app/ai/workflows/state.py`）。
必要性：`.specify/contracts/agent-workflow.md` 的 Required State 与 `plan.md` §5 要求工作流在
逐题循环、低置信度暂停、教师复核后恢复和最终诊断之间保持同一份可追踪状态；T073 需要把该状态
作为检查点载荷持久化并恢复。若状态字段缺失、只能累积单题槽位、或"序列化成功"被当作"状态合法"，
就会重现 M3 已修复过的重复计分、旧值复用与非最终成绩配最终诊断等问题。
覆盖内容：
1. Required State 逐项对照 `.specify/contracts/agent-workflow.md`（不比对自身常量）；
2. 完整快照必须有非空 `workflow_id`/`request_id`/`submission_id`，阶段性字段允许缺省；
   `total=False` 的节点局部更新保持"缺键 = 通道未写入、显式 null = 无值"的区别；
3. 多题集合：`grading_results`/`confidence_decisions` 以 `answer_id` 为键、重评按答案替换、
   `final_results` 为逐题集合且必须与 `exam_result.items` 一致；`SubmissionContext` 提供权威
   题目集合与稳定题序；
4. 逐题槽位清理规则（`ANSWER_SLOT_FIELDS`）与整卷保留集合（`SUBMISSION_COLLECTION_FIELDS`）
   互不相交且覆盖全部工作通道；
5. 状态自洽：失败必须带脱敏错误且不得自动可恢复、暂停必须给出原因、待复核不等于完成或失败、
   人工结论可覆盖自动决策、`Ready` 诊断必须来自已最终确认的整卷结果；
6. 校验边界：编码前与解码后都执行 Schema 校验，拒绝未知字段、未知 kind/version、M3
   `background-task-checkpoint` 载荷、非法枚举、非有限或越界置信度、负 `retry_count`、
   字符串型 `int`/`bool`；
7. 保真：Decimal 标度、时区时间与诊断来源时间往返不变。
执行方法（先红后绿）：``python -m pytest tests/unit/workflows -q``（实现前应因模块缺失而失败）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, get_type_hints

import pytest

from backend.app.ai.agents.state import AgentError, RetrievedContextItem
from backend.app.ai.workflows import state as workflow_state_module
from backend.app.ai.workflows.state import (
    ANSWER_SLOT_FIELDS,
    CONTRACT_STATE_FIELDS,
    IDENTITY_STATE_FIELDS,
    LEGACY_BACKGROUND_TASK_CHECKPOINT_KIND,
    RUN_CONTROL_STATE_FIELDS,
    SUBMISSION_COLLECTION_FIELDS,
    TASK_MANDATED_STATE_FIELDS,
    WORKFLOW_STATE_INVALID,
    WORKFLOW_STATE_NOT_SERIALIZABLE,
    WORKFLOW_STATE_PAYLOAD_KIND,
    WORKFLOW_STATE_PAYLOAD_KIND_MISMATCH,
    WORKFLOW_STATE_PAYLOAD_VERSION,
    WORKFLOW_STATE_PAYLOAD_VERSION_UNSUPPORTED,
    GradingWorkflowState,
    WorkflowStateError,
    WorkflowStateNotSerializableError,
    WorkflowStatePayloadKindError,
    WorkflowStatePayloadVersionError,
    WorkflowStateSnapshot,
    workflow_state_from_checkpoint_payload,
    workflow_state_from_json,
    workflow_state_to_checkpoint_payload,
    workflow_state_to_json,
)
from backend.app.domain.enums import (
    QuestionType,
    ReviewStatus,
    ValidationStatus,
    WorkflowStatus,
)
from backend.app.schemas.ai import GradingResult
from backend.app.schemas.grading import (
    ConfidenceDecisionDTO,
    DiagnosisReportDTO,
    DiagnosisStatus,
    ExamResultDTO,
    ExamResultStatus,
    MasteryByKnowledgePointDTO,
    QuestionResultDTO,
    SubmissionContext,
)

SUBMISSION_ID = "submission-1"
EXAM_ID = "exam-1"
STUDENT_ID = "student-1"
ANSWER_ONE = "answer-1"
ANSWER_TWO = "answer-2"
#: 固定时区时间，用于验证往返后时区与来源时间不被改写。
AGGREGATED_AT = datetime(2026, 5, 1, 8, 30, tzinfo=UTC)
#: M3 后台任务检查点中的 kind 取值（T056）；LangGraph 状态载荷必须显式区分。
LEGACY_M3_KIND = "background-task-checkpoint"


def _submission_context() -> SubmissionContext:
    """构造权威题目集合与稳定题序（1=简答主观题，2=单选客观题）。"""

    return SubmissionContext.model_validate(
        {
            "submission_id": SUBMISSION_ID,
            "exam_id": EXAM_ID,
            "student_id": STUDENT_ID,
            "expected_answers": [
                {
                    "order": 1,
                    "answer_id": ANSWER_ONE,
                    "question_id": "question-1",
                    "question_type": QuestionType.SHORT_ANSWER.value,
                    "max_score": "10.00",
                    "knowledge_points": ["函数极限"],
                },
                {
                    "order": 2,
                    "answer_id": ANSWER_TWO,
                    "question_id": "question-2",
                    "question_type": QuestionType.SINGLE_CHOICE.value,
                    "max_score": "5.00",
                    "knowledge_points": ["导数定义"],
                },
            ],
        }
    )


def _grading_result(
    answer_id: str,
    *,
    score: float = 8.5,
    question_type: QuestionType = QuestionType.SHORT_ANSWER,
    confidence: float = 0.9,
    review_status: str = ReviewStatus.NOT_REQUIRED.value,
) -> GradingResult:
    """构造单题评分结果。"""

    return GradingResult(
        question_type=question_type,
        score=score,
        max_score=10.0,
        reason="要点基本齐全。",
        correct_points=["左右极限相等"],
        missing_knowledge_points=["连续性"],
        knowledge_points=["函数极限"],
        suggestions=["复习连续性与极限的关系。"],
        confidence=confidence,
        validation_status=ValidationStatus.VALIDATED.value,
        review_status=review_status,
        retrieved_context_ids=["chunk-1"],
        answer_id=answer_id,
        submission_id=SUBMISSION_ID,
    )


def _decision_dto(*, requires_review: bool = False, confidence: float = 0.9) -> ConfidenceDecisionDTO:
    """构造本次置信度决策快照。"""

    return ConfidenceDecisionDTO(
        confidence=confidence,
        threshold=0.7,
        requires_review=requires_review,
        review_status=(
            ReviewStatus.PENDING_REVIEW.value
            if requires_review
            else ReviewStatus.NOT_REQUIRED.value
        ),
        grading_status="Pending Review" if requires_review else "Accepted",
        reason="置信度与阈值的比较结果。",
    )


def _question_result(
    answer_id: str,
    order: int,
    *,
    score: str = "8.50",
    max_score: str = "10.00",
    counted: bool = True,
    requires_review: bool = False,
) -> QuestionResultDTO:
    """构造单个预期题目的汇总结果。"""

    effective = Decimal(score) if counted else None
    return QuestionResultDTO(
        order=order,
        answer_id=answer_id,
        question_id=f"question-{order}",
        question_type=QuestionType.SHORT_ANSWER,
        max_score=Decimal(max_score),
        score=Decimal(score),
        effective_score=effective,
        counted=counted,
        missing=False,
        requires_review=requires_review,
        grading_status="Accepted" if counted else "Pending Review",
        review_status=(
            ReviewStatus.NOT_REQUIRED.value
            if counted
            else ReviewStatus.PENDING_REVIEW.value
        ),
        validation_status=ValidationStatus.VALIDATED.value,
        reason="要点基本齐全。",
        knowledge_points=["函数极限"],
        missing_knowledge_points=["连续性"],
        correct_points=["左右极限相等"],
        suggestions=["复习连续性与极限的关系。"],
        retrieved_context_ids=["chunk-1"],
        confidence=0.9,
        submission_id=SUBMISSION_ID,
        decision=None if counted else _decision_dto(requires_review=True),
    )


def _exam_result(
    items: list[QuestionResultDTO],
    *,
    is_final: bool = True,
    result_status: ExamResultStatus | None = None,
    pending_review_answer_count: int = 0,
) -> ExamResultDTO:
    """构造整卷汇总结果；默认已最终确认。"""

    resolved_status = result_status or (
        ExamResultStatus.FINAL if is_final else ExamResultStatus.PENDING_REVIEW
    )
    return ExamResultDTO(
        submission_id=SUBMISSION_ID,
        exam_id=EXAM_ID,
        student_id=STUDENT_ID,
        result_status=resolved_status,
        is_final=is_final,
        final_total_score=Decimal("8.50") if is_final else None,
        confirmed_subtotal=Decimal("8.50"),
        confirmed_subtotal_label="已确认部分小计：8.50",
        total_max_score=Decimal("15.00"),
        expected_answer_count=2,
        graded_answer_count=2,
        counted_answer_count=2 - pending_review_answer_count,
        pending_review_answer_count=pending_review_answer_count,
        items=items,
        aggregated_at=AGGREGATED_AT,
    )


def _diagnosis(status: DiagnosisStatus = DiagnosisStatus.READY) -> DiagnosisReportDTO:
    """构造学生诊断报告；默认就绪状态。"""

    return DiagnosisReportDTO(
        exam_result_id="exam-result-1",
        submission_id=SUBMISSION_ID,
        student_id=STUDENT_ID,
        status=status,
        mastery_by_knowledge_point=[
            MasteryByKnowledgePointDTO(
                knowledge_point="函数极限",
                answered_count=1,
                correct_count=0,
                awarded_score=Decimal("8.50"),
                max_score=Decimal("10.00"),
                mastery=Decimal("0.85"),
            )
        ],
        weak_knowledge_points=[],
        error_reasons=["连续性要点缺失。"],
        learning_suggestions=["复习连续性与极限的关系。"],
        generated_at=AGGREGATED_AT if status is DiagnosisStatus.READY else None,
        source_exam_result_updated_at=(
            AGGREGATED_AT if status is DiagnosisStatus.READY else None
        ),
    )


def _complete_state() -> GradingWorkflowState:
    """构造一份完成两题自动接受、已形成最终成绩与就绪诊断的状态。"""

    items = [_question_result(ANSWER_ONE, 1), _question_result(ANSWER_TWO, 2, max_score="5.00")]
    return {
        "workflow_id": "workflow-1",
        "request_id": "request-1",
        "submission_id": SUBMISSION_ID,
        "status": WorkflowStatus.COMPLETED,
        "current_node": "generate_diagnosis",
        "current_answer_id": ANSWER_TWO,
        "current_answer_order": 2,
        "retry_count": 0,
        "pause_reason": None,
        "resumable": False,
        "submission_context": _submission_context(),
        "question_type": QuestionType.SINGLE_CHOICE,
        "query": "【题目】导数定义",
        "retrieved_context_ids": ["chunk-1"],
        "retrieved_context": [
            RetrievedContextItem.from_chunk(
                _retrieved_chunk()  # type: ignore[arg-type]
            )
        ],
        "grading_result": _grading_result(ANSWER_TWO, question_type=QuestionType.SINGLE_CHOICE),
        "grading_results": {
            ANSWER_ONE: _grading_result(ANSWER_ONE),
            ANSWER_TWO: _grading_result(ANSWER_TWO, question_type=QuestionType.SINGLE_CHOICE),
        },
        "validation_status": ValidationStatus.VALIDATED,
        "confidence": 0.9,
        "confidence_decision": _decision_dto(),
        "confidence_decisions": {
            ANSWER_ONE: _decision_dto(),
            ANSWER_TWO: _decision_dto(),
        },
        "review_status": ReviewStatus.NOT_REQUIRED,
        "final_results": items,
        "exam_result": _exam_result(items),
        "diagnosis": _diagnosis(),
        "error": None,
    }


def _retrieved_chunk(**overrides: Any) -> Any:
    """构造 M2 检索候选；仅在状态测试中用于验证检索 DTO 可进入快照。"""

    from backend.app.ai.retrieval.base import RetrievedChunk

    payload: dict[str, Any] = {
        "chunk_id": "chunk-1",
        "course_id": "course-1",
        "document_id": "document-1",
        "content": "极限存在的条件说明。",
        "metadata": {"page": 3},
        "rank": 1,
    }
    payload.update(overrides)
    return RetrievedChunk(**payload)


def _contract_required_state_fields() -> set[str]:
    """从外部契约文件解析 Required State 字段清单（逐项对照，不比对自身常量）。"""

    repo_root = Path(__file__).resolve().parents[3]
    text = (repo_root / ".specify" / "contracts" / "agent-workflow.md").read_text(
        encoding="utf-8"
    )
    section = text.split("## Required State", 1)[1]
    block = section.split("```text", 1)[1].split("```", 1)[0]
    return {line.strip() for line in block.splitlines() if line.strip()}


def _round_trip(state: GradingWorkflowState) -> GradingWorkflowState:
    """按检查点载荷完整往返一次状态。"""

    payload = workflow_state_to_checkpoint_payload(state)
    json.dumps(payload, ensure_ascii=False, allow_nan=False)
    return workflow_state_from_checkpoint_payload(
        json.loads(json.dumps(payload, ensure_ascii=False))
    )


def test_required_state_matches_external_contract_field_by_field() -> None:
    """Required State 必须与 `.specify/contracts/agent-workflow.md` 逐项一致且有明确类型。"""

    contract_fields = _contract_required_state_fields()
    assert contract_fields == set(CONTRACT_STATE_FIELDS)
    # 契约文件是 T065 任务要求集合的子集：任务额外明文要求主观题查询文本与检索片段明细。
    assert set(TASK_MANDATED_STATE_FIELDS) - contract_fields == {
        "query",
        "retrieved_context",
    }
    annotations = get_type_hints(GradingWorkflowState)
    assert set(TASK_MANDATED_STATE_FIELDS) <= set(annotations)
    assert set(TASK_MANDATED_STATE_FIELDS) <= set(WorkflowStateSnapshot.model_fields)
    # 要求的核心字段不得退化为 Any。
    for name in sorted(TASK_MANDATED_STATE_FIELDS):
        assert annotations[name] is not Any


def test_state_fields_are_partitioned_into_identity_control_and_payload_sets() -> None:
    """身份、运行控制、逐题槽位与整卷集合必须互不相交且覆盖全部工作通道。"""

    all_fields = set(get_type_hints(GradingWorkflowState))
    assert IDENTITY_STATE_FIELDS == {"workflow_id", "request_id", "submission_id"}
    assert set(RUN_CONTROL_STATE_FIELDS) == {
        "status",
        "current_node",
        "retry_count",
        "pause_reason",
        "resumable",
    }
    # 换题/重评时必须清空的逐题槽位。
    assert set(ANSWER_SLOT_FIELDS) == {
        "current_answer_id",
        "current_answer_order",
        "question_type",
        "query",
        "retrieved_context_ids",
        "retrieved_context",
        "grading_result",
        "validation_status",
        "confidence",
        "confidence_decision",
        "review_status",
        "error",
    }
    # 整卷级集合必须保留，不得随换题清空。
    assert set(SUBMISSION_COLLECTION_FIELDS) == {
        "submission_context",
        "grading_results",
        "confidence_decisions",
        "final_results",
        "exam_result",
        "diagnosis",
    }
    groups = [
        set(IDENTITY_STATE_FIELDS),
        set(RUN_CONTROL_STATE_FIELDS),
        set(ANSWER_SLOT_FIELDS),
        set(SUBMISSION_COLLECTION_FIELDS),
    ]
    assert set().union(*groups) == all_fields
    for index, group in enumerate(groups):
        for other in groups[index + 1 :]:
            assert group & other == set()
    # 任务要求的字段必须全部落在身份/控制/槽位/集合四组之一，说明清理规则覆盖要求。
    for name in sorted(TASK_MANDATED_STATE_FIELDS):
        assert any(name in group for group in groups)


def test_full_snapshot_requires_identity_fields() -> None:
    """完整快照必须有非空身份字段；节点局部更新允许缺省但不得缺少身份。"""

    for name in sorted(IDENTITY_STATE_FIELDS):
        partial = {
            key: value
            for key, value in _complete_state().items()
            if key != name
        }
        with pytest.raises(WorkflowStateError) as excinfo:
            workflow_state_to_json(partial)
        assert excinfo.value.error_code == WORKFLOW_STATE_INVALID

    with pytest.raises(WorkflowStateError) as blank:
        workflow_state_to_json(
            {"workflow_id": "  ", "request_id": "request-1", "submission_id": SUBMISSION_ID}
        )
    assert blank.value.error_code == WORKFLOW_STATE_INVALID

    with pytest.raises(WorkflowStateError):
        workflow_state_to_json(
            {"request_id": "request-1", "submission_id": SUBMISSION_ID}
        )


def test_initial_state_round_trip_keeps_only_written_keys() -> None:
    """节点尚未写入的通道必须保持缺键语义，不得被补成一批 None。"""

    initial: GradingWorkflowState = {
        "workflow_id": "workflow-1",
        "request_id": "request-1",
        "submission_id": SUBMISSION_ID,
    }
    payload = workflow_state_to_json(initial)
    assert payload == dict(initial)
    assert _round_trip(initial) == dict(initial)


def test_missing_key_differs_from_explicit_null() -> None:
    """缺键表示通道未写入，显式 null 表示该阶段已判定为无值，两者往返后必须可区分。"""

    written: GradingWorkflowState = {
        "workflow_id": "workflow-1",
        "request_id": "request-1",
        "submission_id": SUBMISSION_ID,
        "query": None,
    }
    payload = workflow_state_to_json(written)
    assert "query" in payload
    assert payload["query"] is None
    restored = workflow_state_from_json(payload)
    assert "query" in restored
    assert restored["query"] is None

    absent = workflow_state_from_json(
        {
            "workflow_id": "workflow-1",
            "request_id": "request-1",
            "submission_id": SUBMISSION_ID,
        }
    )
    assert "query" not in absent


def test_two_answer_state_round_trip_preserves_collections_scale_and_time() -> None:
    """两题累计状态往返后必须保留集合、稳定题序、Decimal 标度、时区时间与诊断来源时间。"""

    state = _complete_state()
    restored = _round_trip(state)

    assert set(restored["grading_results"]) == {ANSWER_ONE, ANSWER_TWO}
    assert set(restored["confidence_decisions"]) == {ANSWER_ONE, ANSWER_TWO}
    assert restored["grading_results"][ANSWER_ONE] == state["grading_results"][ANSWER_ONE]
    assert [
        item.answer_id for item in restored["final_results"]
    ] == [ANSWER_ONE, ANSWER_TWO]
    assert restored["confidence_decisions"][ANSWER_TWO].threshold == 0.7
    assert restored["validation_status"] is ValidationStatus.VALIDATED
    assert restored["question_type"] is QuestionType.SINGLE_CHOICE
    assert restored["review_status"] is ReviewStatus.NOT_REQUIRED

    exam_result = restored["exam_result"]
    assert exam_result is not None
    assert str(exam_result.total_max_score) == "15.00"
    assert str(exam_result.final_total_score) == "8.50"
    assert str(exam_result.items[0].max_score) == "10.00"
    assert exam_result.aggregated_at == AGGREGATED_AT
    assert exam_result.aggregated_at.tzinfo is not None
    assert [item.order for item in exam_result.items] == [1, 2]

    diagnosis = restored["diagnosis"]
    assert diagnosis is not None
    assert diagnosis.status is DiagnosisStatus.READY
    assert diagnosis.generated_at == AGGREGATED_AT
    assert diagnosis.source_exam_result_updated_at == AGGREGATED_AT
    assert diagnosis.mastery_by_knowledge_point[0].mastery == Decimal("0.85")

    context = restored["submission_context"]
    assert context is not None
    assert [item.order for item in context.expected_answers] == [1, 2]
    assert context.expected_answers[0].answer_id == ANSWER_ONE


def test_regrade_replaces_answer_key_without_duplicating_others() -> None:
    """重评必须按答案替换同一键，既不重复计分也不影响其它答案。"""

    state = _complete_state()
    regraded: GradingWorkflowState = {
        **state,
        "grading_results": {
            **state["grading_results"],
            ANSWER_ONE: _grading_result(ANSWER_ONE, score=5.0),
        },
    }
    assert set(regraded["grading_results"]) == {ANSWER_ONE, ANSWER_TWO}
    assert regraded["grading_results"][ANSWER_ONE].score == 5.0
    assert (
        regraded["grading_results"][ANSWER_TWO]
        == state["grading_results"][ANSWER_TWO]
    )
    restored = _round_trip(regraded)
    assert restored["grading_results"][ANSWER_ONE].score == 5.0


def test_exam_result_items_must_agree_with_final_results() -> None:
    """`final_results` 与整卷逐题结果必须同源，不得出现两份不同版本。"""

    state = _complete_state()
    items = list(state["final_results"])
    mismatched: GradingWorkflowState = {
        **state,
        "final_results": items[:1],
    }
    with pytest.raises(WorkflowStateError) as excinfo:
        workflow_state_to_json(mismatched)
    assert excinfo.value.error_code == WORKFLOW_STATE_INVALID

    final_without_items: GradingWorkflowState = {**state, "final_results": []}
    with pytest.raises(WorkflowStateError):
        workflow_state_to_json(final_without_items)


def test_pending_review_state_is_not_failure_or_completion() -> None:
    """待复核状态必须带暂停原因，且不得与失败或完成混用。"""

    items = [_question_result(ANSWER_ONE, 1), _question_result(ANSWER_TWO, 2, counted=False, requires_review=True)]
    paused: GradingWorkflowState = {
        "workflow_id": "workflow-2",
        "request_id": "request-2",
        "submission_id": SUBMISSION_ID,
        "status": WorkflowStatus.PAUSED,
        "current_node": "confidence_check",
        "current_answer_id": ANSWER_TWO,
        "retry_count": 0,
        "pause_reason": "置信度低于阈值，等待教师复核。",
        "resumable": True,
        "submission_context": _submission_context(),
        "grading_results": {
            ANSWER_ONE: _grading_result(ANSWER_ONE),
            ANSWER_TWO: _grading_result(ANSWER_TWO, score=5.0, confidence=0.4),
        },
        "confidence_decisions": {
            ANSWER_ONE: _decision_dto(),
            ANSWER_TWO: _decision_dto(requires_review=True, confidence=0.4),
        },
        "review_status": ReviewStatus.PENDING_REVIEW,
        "final_results": [],
        "exam_result": _exam_result(items, is_final=False, pending_review_answer_count=1),
        "diagnosis": _diagnosis(DiagnosisStatus.NOT_READY),
        "error": None,
    }
    restored = _round_trip(paused)
    assert restored["status"] is WorkflowStatus.PAUSED
    assert restored["review_status"] is ReviewStatus.PENDING_REVIEW
    assert restored["pause_reason"] == "置信度低于阈值，等待教师复核。"
    assert restored["confidence_decisions"][ANSWER_TWO].requires_review is True

    # 待复核不等于失败。
    with pytest.raises(WorkflowStateError):
        workflow_state_to_json(
            {
                **paused,
                "error": AgentError(
                    error_code="AGENT_FAILED",
                    message="Provider 调用失败。",
                    retryable=True,
                ),
            }
        )
    # 待复核不得同时宣称流程完成。
    with pytest.raises(WorkflowStateError):
        workflow_state_to_json({**paused, "status": WorkflowStatus.COMPLETED})
    # 暂停必须说明原因，不得静默暂停。
    without_reason = {
        key: value for key, value in paused.items() if key != "pause_reason"
    }
    with pytest.raises(WorkflowStateError):
        workflow_state_to_json(without_reason)


def test_teacher_decision_may_supersede_automatic_decision() -> None:
    """教师确认/修改结论必须能覆盖自动决策状态，并完整往返。"""

    state: GradingWorkflowState = {
        **_complete_state(),
        "status": WorkflowStatus.COMPLETED,
        "review_status": ReviewStatus.MODIFIED,
        "confidence_decisions": {
            ANSWER_ONE: _decision_dto(requires_review=True, confidence=0.4),
            ANSWER_TWO: _decision_dto(),
        },
        "grading_results": {
            ANSWER_ONE: _grading_result(
                ANSWER_ONE, score=6.0, review_status=ReviewStatus.MODIFIED.value
            ),
            ANSWER_TWO: _grading_result(ANSWER_TWO),
        },
    }
    restored = _round_trip(state)
    assert restored["review_status"] is ReviewStatus.MODIFIED
    # 历史自动决策原样保留，不被重新判定。
    assert restored["confidence_decisions"][ANSWER_ONE].confidence == 0.4
    assert restored["confidence_decisions"][ANSWER_ONE].threshold == 0.7
    assert (
        restored["grading_results"][ANSWER_ONE].review_status
        == ReviewStatus.MODIFIED.value
    )


def test_failed_state_requires_error_and_never_auto_resumable() -> None:
    """失败状态必须带脱敏错误，且编解码不得把 `resumable` 自动置真。"""

    failed: GradingWorkflowState = {
        "workflow_id": "workflow-3",
        "request_id": "request-3",
        "submission_id": SUBMISSION_ID,
        "status": WorkflowStatus.FAILED,
        "current_node": "failed",
        "retry_count": 2,
        "pause_reason": None,
        "resumable": False,
        "submission_context": _submission_context(),
        "grading_results": {ANSWER_ONE: _grading_result(ANSWER_ONE)},
        "confidence_decisions": {},
        "final_results": [],
        "error": AgentError(
            error_code="AGENT_PROVIDER_UNAVAILABLE",
            message="Provider 未就绪，无法完成评分。",
            retryable=True,
            attempt_count=2,
        ),
    }
    restored = _round_trip(failed)
    assert restored["status"] is WorkflowStatus.FAILED
    assert restored["resumable"] is False
    assert restored["retry_count"] == 2
    error = restored["error"]
    assert error is not None
    assert error.error_code == "AGENT_PROVIDER_UNAVAILABLE"
    assert error.retryable is True
    assert error.attempt_count == 2

    with pytest.raises(WorkflowStateError):
        workflow_state_to_json(
            {key: value for key, value in failed.items() if key != "error"}
        )
    with pytest.raises(WorkflowStateError):
        workflow_state_to_json({**failed, "resumable": True})

    # 未写入 resumable 的状态不得被编解码补成 True。
    without_flag = {key: value for key, value in failed.items() if key != "resumable"}
    payload = workflow_state_to_checkpoint_payload(without_flag)
    assert "resumable" not in payload["state"]
    restored_without_flag = workflow_state_from_checkpoint_payload(payload)
    assert "resumable" not in restored_without_flag


def test_ready_diagnosis_requires_final_exam_result() -> None:
    """就绪诊断必须来自已最终确认的整卷结果，非最终结果不得配就绪诊断。"""

    state = _complete_state()
    without_exam_result = {key: value for key, value in state.items() if key != "exam_result"}
    with pytest.raises(WorkflowStateError):
        workflow_state_to_json(without_exam_result)

    with pytest.raises(WorkflowStateError):
        workflow_state_to_json(
            {
                **state,
                "exam_result": _exam_result(
                    list(state["final_results"]),
                    is_final=False,
                    result_status=ExamResultStatus.PENDING_REVIEW,
                    pending_review_answer_count=1,
                ),
            }
        )

    # 未就绪诊断允许与尚未最终确认的整卷结果共存。
    not_ready: GradingWorkflowState = {
        **state,
        "diagnosis": _diagnosis(DiagnosisStatus.NOT_READY),
        "exam_result": _exam_result(
            list(state["final_results"]),
            is_final=False,
            result_status=ExamResultStatus.PENDING_REVIEW,
            pending_review_answer_count=1,
        ),
    }
    restored = _round_trip(not_ready)
    assert restored["diagnosis"] is not None
    assert restored["diagnosis"].status is DiagnosisStatus.NOT_READY
    assert restored["exam_result"] is not None
    assert restored["exam_result"].is_final is False


def test_collection_keys_must_belong_to_submission_context() -> None:
    """单题集合的键必须属于权威答案集合，避免混入其它答卷或其他题目的结果。"""

    state = _complete_state()
    with pytest.raises(WorkflowStateError) as excinfo:
        workflow_state_to_json(
            {
                **state,
                "grading_results": {**state["grading_results"], "answer-9": _grading_result("answer-9")},
            }
        )
    assert excinfo.value.error_code == WORKFLOW_STATE_INVALID

    with pytest.raises(WorkflowStateError):
        workflow_state_to_json(
            {
                **state,
                "confidence_decisions": {
                    **state["confidence_decisions"],
                    "answer-9": _decision_dto(),
                },
            }
        )


@pytest.mark.parametrize(
    "mutation",
    [
        {"unknown_field": 1},
        {"confidence": "high"},
        {"confidence": float("nan")},
        {"confidence": float("inf")},
        {"confidence": 1.5},
        {"retry_count": "5"},
        {"retry_count": -1},
        {"resumable": "true"},
        {"question_type": "Graded"},
        {"status": "完成"},
        {"retrieved_context_ids": [""]},
        {"validation_status": "Maybe"},
        {"review_status": "Reviewed"},
        {"final_results": "not-a-list"},
        {"current_answer_order": 0},
        {"workflow_id": 42},
    ],
)
def test_state_rejects_unknown_or_invalid_values(mutation: dict[str, Any]) -> None:
    """编码边界必须拒绝未知字段、非法枚举、非有限值、错误类型与越界数值。"""

    state = {**_complete_state(), **mutation}
    with pytest.raises(WorkflowStateError) as excinfo:
        workflow_state_to_json(state)  # type: ignore[arg-type]
    assert excinfo.value.error_code == WORKFLOW_STATE_INVALID
    with pytest.raises(WorkflowStateError):
        workflow_state_from_json(state)


def test_decode_also_validates_and_rejects_unknown_fields() -> None:
    """解码后同样执行 Schema 校验，拒绝未知字段与损坏的检查点内容。"""

    payload = workflow_state_to_json(_complete_state())
    with pytest.raises(WorkflowStateError) as excinfo:
        workflow_state_from_json({**payload, "unexpected": None})
    assert excinfo.value.error_code == WORKFLOW_STATE_INVALID

    with pytest.raises(WorkflowStateError):
        workflow_state_from_json({"workflow_id": "workflow-1", "request_id": "r"})


def test_checkpoint_payload_kind_and_version_are_enforced() -> None:
    """检查点载荷必须带明确 kind/version，并拒绝未知版本与 M3 后台任务检查点。"""

    assert WORKFLOW_STATE_PAYLOAD_KIND == "langgraph-grading-state-payload"
    assert WORKFLOW_STATE_PAYLOAD_VERSION == "1"
    assert LEGACY_BACKGROUND_TASK_CHECKPOINT_KIND == LEGACY_M3_KIND

    payload = workflow_state_to_checkpoint_payload(_complete_state())
    assert payload["kind"] == WORKFLOW_STATE_PAYLOAD_KIND
    assert payload["version"] == WORKFLOW_STATE_PAYLOAD_VERSION
    assert workflow_state_from_checkpoint_payload(payload)["workflow_id"] == "workflow-1"

    # M3 的 T056 后台任务检查点载荷必须被显式拒绝，不能被误读为 LangGraph 状态。
    with pytest.raises(WorkflowStatePayloadKindError) as kind_error:
        workflow_state_from_checkpoint_payload(
            {"kind": LEGACY_M3_KIND, "answer_order": [], "task": {"created_at": None}}
        )
    assert kind_error.value.error_code == WORKFLOW_STATE_PAYLOAD_KIND_MISMATCH

    with pytest.raises(WorkflowStatePayloadKindError):
        workflow_state_from_checkpoint_payload(
            {"version": WORKFLOW_STATE_PAYLOAD_VERSION, "state": {}}
        )

    with pytest.raises(WorkflowStatePayloadVersionError) as version_error:
        workflow_state_from_checkpoint_payload(
            {**payload, "version": "2"}
        )
    assert version_error.value.error_code == WORKFLOW_STATE_PAYLOAD_VERSION_UNSUPPORTED

    with pytest.raises(WorkflowStateError):
        workflow_state_from_checkpoint_payload("not-a-mapping")  # type: ignore[arg-type]


def test_checkpoint_payload_is_strict_json() -> None:
    """载荷必须能通过严格 JSON 序列化（禁止 NaN/Infinity），并保持可恢复。"""

    payload = workflow_state_to_checkpoint_payload(_complete_state())
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    restored = workflow_state_from_checkpoint_payload(json.loads(encoded))
    assert restored["submission_id"] == SUBMISSION_ID
    assert restored["diagnosis"] is not None
    assert json.dumps(
        workflow_state_to_json(_complete_state()), ensure_ascii=False, allow_nan=False
    )


def test_non_serializable_payload_is_reported_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    """JSON 兜底校验失败必须给出专用错误码，而不是静默产出非法 JSON。"""

    def _failing_dumps(*args: Any, **kwargs: Any) -> str:
        raise ValueError("Object of type Session is not JSON serializable")

    monkeypatch.setattr(workflow_state_module.json, "dumps", _failing_dumps)
    with pytest.raises(WorkflowStateNotSerializableError) as excinfo:
        workflow_state_to_json(_complete_state())
    assert excinfo.value.error_code == WORKFLOW_STATE_NOT_SERIALIZABLE
