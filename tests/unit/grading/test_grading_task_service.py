"""T054 阅卷应用层单元测试：置信度决策记录与每次评分独享状态。

TCR（2026-09-16，T054 / B02）：``ConfidencePolicy.apply()`` 只回填 ``review_status``，
不把 :class:`ConfidenceDecision` 交给调用方，导致汇总阶段无法区分“已执行置信度检查且
自动接受”与“默认 Not Required”。修复方式是在应用层提供实现 ``ConfidencePolicyLike``
的记录策略，复用既有 ``evaluate()`` 判定并保存本次决策；先新增失败用例，再实现
``backend/app/services/grading/grading_task_service.py`` 中的 ``DecisionRecordingPolicy``。

测试只检查业务合同（FR-035、plan.md §5.4、data-model.md 状态图），不修改既有评分核心，
不使用全局 last_decision 或跨评分共享状态。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from backend.app.domain.enums import QuestionType
from backend.app.schemas.ai import GradingResult
from backend.app.schemas.grading import (
    ExamResultStatus,
    GradingTaskStatus,
    QuestionResultDTO,
)
from backend.app.services.grading.confidence_policy import (
    ConfidenceDecision,
    ConfidencePolicy,
    ManualReviewStateError,
    NotValidatedResultError,
)
from backend.app.services.grading.grading_task_service import (
    GRADING_EXECUTION_NOT_READY,
    GRADING_RESULT_NOT_FOUND,
    GRADING_STORE_NOT_READY,
    GRADING_TASK_NOT_FOUND,
    GRADING_TRIGGER_CONFLICT,
    DecisionRecordingPolicy,
    DefaultScoringPipeline,
    GradingExecutionNotReadyError,
    GradingOutcome,
    GradingResultNotFoundError,
    GradingStoreNotReadyError,
    GradingTargetAnswer,
    GradingTaskNotFoundError,
    GradingTaskService,
    GradingTriggerConflictError,
    InlineGradingTaskExecutor,
    NotConfiguredGradingRepository,
    SubmissionSnapshot,
)
from backend.app.services.grading.result_aggregator import ResultAggregator
from backend.app.services.grading.subjective_grader import SubjectiveGrader
from tests.support.grading_doubles import (
    InMemoryGradingRepository,
    RecordingExecutor,
    RecordingProgressUpdater,
    StubScoringPipeline,
    StubSubmissionReader,
    make_task,
)
from tests.unit.settings_helpers import build_test_settings


def _result(
    answer_id: str | None = "answer-1",
    *,
    question_type: QuestionType = QuestionType.SHORT_ANSWER,
    confidence: float = 0.9,
    review_status: str = "Not Required",
    validation_status: str = "Validated",
) -> GradingResult:
    """构造单题评分结果 DTO。"""

    return GradingResult(
        question_type=question_type,
        score=6.0,
        max_score=10.0,
        reason="评分理由。",
        correct_points=["要点一"],
        missing_knowledge_points=[],
        knowledge_points=["知识点 A"],
        suggestions=["继续练习。"],
        confidence=confidence,
        validation_status=validation_status,
        review_status=review_status,
        answer_id=answer_id,
        submission_id="submission-1",
    )


def _settings() -> object:
    """构造阈值 0.8 的测试配置。"""

    return build_test_settings(confidence_threshold=0.8)


def test_policy_records_decision_and_returns_policy_result() -> None:
    """策略回填 review_status 的同时保存本次决策。"""

    settings = _settings()
    policy = DecisionRecordingPolicy(
        ConfidencePolicy(threshold=0.9),
        settings=settings,  # type: ignore[arg-type]
    )

    updated = policy.apply(_result(confidence=0.95))

    assert updated.review_status == "Not Required"
    recorded = policy.decision_for("answer-1")
    assert recorded is not None
    assert recorded.threshold == 0.9
    assert recorded.requires_review is False
    assert recorded.grading_status == "Accepted"


def test_low_confidence_decision_marks_pending_review() -> None:
    """低置信度主观题记录待复核决策并回填 Pending Review。"""

    settings = _settings()
    policy = DecisionRecordingPolicy(settings=settings)  # type: ignore[arg-type]

    updated = policy.apply(_result(confidence=0.4))

    assert updated.review_status == "Pending Review"
    recorded = policy.decision_for("answer-1")
    assert recorded is not None
    assert recorded.requires_review is True
    assert recorded.threshold == 0.8


def test_objective_result_is_accepted_without_review() -> None:
    """客观题按状态图直接接受，不进入主观题置信度复核。"""

    settings = _settings()
    policy = DecisionRecordingPolicy(settings=settings)  # type: ignore[arg-type]

    policy.apply(
        _result(
            question_type=QuestionType.SINGLE_CHOICE,
            confidence=0.1,
        )
    )

    recorded = policy.decision_for("answer-1")
    assert recorded is not None
    assert recorded.requires_review is False
    assert recorded.review_status == "Not Required"


def test_recorded_decisions_are_isolated_per_instance() -> None:
    """每次评分独享策略实例，决策不跨实例共享。"""

    settings = _settings()
    first = DecisionRecordingPolicy(settings=settings)  # type: ignore[arg-type]
    second = DecisionRecordingPolicy(settings=settings)  # type: ignore[arg-type]

    first.apply(_result("answer-1", confidence=0.95))

    assert first.decision_for("answer-1") is not None
    assert second.decision_for("answer-1") is None
    assert len(second.decisions) == 0
    assert second.decisions_by_answer_id() == {}


def test_human_decided_result_is_rejected_and_not_recorded() -> None:
    """已有人工结论的结果不得被自动策略覆盖，也不产生新记录。"""

    settings = _settings()
    policy = DecisionRecordingPolicy(settings=settings)  # type: ignore[arg-type]

    with pytest.raises(ManualReviewStateError):
        policy.apply(_result(review_status="Modified"))

    assert policy.decisions == ()


def test_unvalidated_result_is_rejected() -> None:
    """未通过结构化校验的结果不得记录决策。"""

    settings = _settings()
    policy = DecisionRecordingPolicy(settings=settings)  # type: ignore[arg-type]

    with pytest.raises(NotValidatedResultError):
        policy.apply(_result(validation_status="Pending"))

    assert policy.decisions == ()


def test_missing_answer_identity_is_recorded_as_none() -> None:
    """结果缺少 answer_id 时仍如实记录，交由汇总阶段判定身份缺失。"""

    settings = _settings()
    policy = DecisionRecordingPolicy(settings=settings)  # type: ignore[arg-type]

    policy.apply(_result(None, confidence=0.95))

    assert len(policy.decisions) == 1
    assert policy.decisions[0].answer_id is None
    assert policy.decisions_by_answer_id() == {}


def test_policy_satisfies_subjective_grader_injection_point() -> None:
    """记录策略可作为 ``ConfidencePolicyLike`` 注入既有评分器。"""

    settings = _settings()
    policy = DecisionRecordingPolicy(settings=settings)  # type: ignore[arg-type]
    grader = SubjectiveGrader(policy=policy)

    assert callable(policy.apply)
    assert grader is not None


# --------------------------------------------------------------------------- #
# T056：任务服务、执行器与默认评分管道
# --------------------------------------------------------------------------- #

SUBMISSION_ID = "submission-1"
TEACHER_ID = "teacher-1"


def _objective_answer(
    order: int = 1,
    answer_id: str = "answer-1",
    *,
    reference: str = "A",
    student: str = "A",
    max_score: str = "10.00",
    knowledge_points: tuple[str, ...] = ("知识点 A",),
) -> GradingTargetAnswer:
    """构造客观题阅卷目标。"""

    return GradingTargetAnswer(
        order=order,
        answer_id=answer_id,
        question_id=f"question-{order}",
        question_type=QuestionType.SINGLE_CHOICE,
        max_score=Decimal(max_score),
        knowledge_points=knowledge_points,
        content="下列哪个选项正确？",
        reference_answer=reference,
        student_answer=student,
    )


def _subjective_answer(order: int = 1) -> GradingTargetAnswer:
    """构造主观题阅卷目标。"""

    return GradingTargetAnswer(
        order=order,
        answer_id=f"answer-{order}",
        question_id=f"question-{order}",
        question_type=QuestionType.SHORT_ANSWER,
        max_score=Decimal("8.00"),
        knowledge_points=("知识点 A",),
        content="请解释变量作用域。",
        reference_answer="变量作用域决定可见范围。",
        scoring_rubric="答出可见范围得满分。",
        student_answer="变量作用域是可见范围。",
    )


def _snapshot(
    *,
    submission_id: str = SUBMISSION_ID,
    status: str = "Submitted",
    answers: tuple[GradingTargetAnswer, ...] | None = None,
) -> SubmissionSnapshot:
    """构造答卷快照。"""

    return SubmissionSnapshot(
        submission_id=submission_id,
        exam_id="exam-1",
        student_id="student-1",
        course_id="course-1",
        status=status,
        answers=answers if answers is not None else (_objective_answer(),),
    )


def _result_for(answer_id: str, *, score: str = "10", max_score: str = "10") -> GradingResult:
    """构造单题评分结果。"""

    return GradingResult(
        question_type=QuestionType.SINGLE_CHOICE,
        score=float(score),
        max_score=float(max_score),
        reason="评分理由。",
        correct_points=["要点一"],
        missing_knowledge_points=[],
        knowledge_points=["知识点 A"],
        suggestions=["继续练习。"],
        confidence=0.9,
        answer_id=answer_id,
        submission_id=SUBMISSION_ID,
    )


def _outcome(snapshot: SubmissionSnapshot, results: tuple[GradingResult, ...]) -> GradingOutcome:
    """用 T054 汇总服务构造执行器期望的产出。"""

    return GradingOutcome(
        results=results,
        decisions={},
        exam_result=ResultAggregator().aggregate(
            snapshot.to_context(), results=list(results)
        ),
    )


def _service(
    *,
    repository: object | None = None,
    reader: object | None = None,
    executor: object | None = None,
    **kwargs: object,
) -> GradingTaskService:
    """构造被测任务服务。"""

    resolved_repository = (
        repository if repository is not None else InMemoryGradingRepository()
    )
    resolved_reader = (
        reader
        if reader is not None
        else StubSubmissionReader(
            {SUBMISSION_ID: _snapshot()},
            allowed_teacher_id=TEACHER_ID,
        )
    )
    resolved_executor = executor if executor is not None else RecordingExecutor()
    return GradingTaskService(
        repository=resolved_repository,  # type: ignore[arg-type]
        reader=resolved_reader,  # type: ignore[arg-type]
        executor=resolved_executor,  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


def test_trigger_creates_queued_task_and_schedules_execution() -> None:
    """触发为已提交答卷创建排队任务，并交给执行器调度。"""

    repository = InMemoryGradingRepository()
    recorder = RecordingExecutor()
    service = _service(repository=repository, executor=recorder)

    task = service.trigger(SUBMISSION_ID, teacher_id=TEACHER_ID, scheduler=recorder)

    assert task.status is GradingTaskStatus.QUEUED
    assert task.reused is False
    assert task.durable is False
    assert repository.get_task(task.task_id) is not None
    assert recorder.scheduled == [(task.task_id, SUBMISSION_ID)]


def test_trigger_reuses_in_flight_task_without_rescheduling() -> None:
    """同一答卷已有进行中任务时复用，不重复调度或调用 LLM。"""

    repository = InMemoryGradingRepository()
    existing = make_task("task-existing", SUBMISSION_ID, status=GradingTaskStatus.RUNNING)
    repository.save_task(existing)
    recorder = RecordingExecutor()
    service = _service(repository=repository, executor=recorder)

    task = service.trigger(SUBMISSION_ID, teacher_id=TEACHER_ID, scheduler=recorder)

    assert task.task_id == "task-existing"
    assert task.reused is True
    assert recorder.scheduled == []


def test_trigger_completed_task_requires_regrade() -> None:
    """已完成任务在未请求重评时返回状态冲突。"""

    repository = InMemoryGradingRepository()
    repository.save_task(
        make_task("task-done", SUBMISSION_ID, status=GradingTaskStatus.COMPLETED)
    )
    service = _service(repository=repository, reader=StubSubmissionReader(
        {SUBMISSION_ID: _snapshot(status="Graded")}, allowed_teacher_id=TEACHER_ID
    ))

    with pytest.raises(GradingTriggerConflictError) as error:
        service.trigger(SUBMISSION_ID, teacher_id=TEACHER_ID)

    assert error.value.error_code == GRADING_TRIGGER_CONFLICT


def test_trigger_regrade_creates_new_task() -> None:
    """显式请求重评时创建新任务。"""

    repository = InMemoryGradingRepository()
    repository.save_task(
        make_task("task-done", SUBMISSION_ID, status=GradingTaskStatus.COMPLETED)
    )
    recorder = RecordingExecutor()
    service = _service(
        repository=repository,
        reader=StubSubmissionReader(
            {SUBMISSION_ID: _snapshot(status="Graded")}, allowed_teacher_id=TEACHER_ID
        ),
        executor=recorder,
    )

    task = service.trigger(
        SUBMISSION_ID, teacher_id=TEACHER_ID, regrade=True, scheduler=recorder
    )

    assert task.task_id != "task-done"
    assert task.status is GradingTaskStatus.QUEUED


def test_trigger_rejects_draft_submission() -> None:
    """草稿答卷不得触发阅卷。"""

    service = _service(
        reader=StubSubmissionReader(
            {SUBMISSION_ID: _snapshot(status="Draft")}, allowed_teacher_id=TEACHER_ID
        )
    )

    with pytest.raises(Exception) as error:
        service.trigger(SUBMISSION_ID, teacher_id=TEACHER_ID)

    assert getattr(error.value, "error_code", None) == "GRADING_NOT_ALLOWED"


def test_trigger_denies_teacher_without_course_ownership() -> None:
    """教师越权访问他人课程答卷时被拒绝。"""

    service = _service()

    with pytest.raises(Exception) as error:
        service.trigger(SUBMISSION_ID, teacher_id="teacher-other")

    assert getattr(error.value, "error_code", None) == "GRADING_PERMISSION_DENIED"


def test_trigger_reports_store_not_ready() -> None:
    """结果存储未接通时返回显式未就绪，不返回虚构任务标识。"""

    service = _service(repository=NotConfiguredGradingRepository())

    with pytest.raises(GradingStoreNotReadyError) as error:
        service.trigger(SUBMISSION_ID, teacher_id=TEACHER_ID)

    assert error.value.error_code == GRADING_STORE_NOT_READY


def test_task_status_and_single_result_not_found() -> None:
    """未知任务与缺失单题结果均返回明确未找到。"""

    service = _service()

    with pytest.raises(GradingTaskNotFoundError) as task_error:
        service.get_task("task-unknown")
    assert task_error.value.error_code == GRADING_TASK_NOT_FOUND

    with pytest.raises(GradingResultNotFoundError) as result_error:
        service.get_single_result(SUBMISSION_ID, "answer-1")
    assert result_error.value.error_code == GRADING_RESULT_NOT_FOUND


def test_executor_persists_outcome_and_progress() -> None:
    """执行器写入整卷结果、单题结果与进度状态。"""

    repository = InMemoryGradingRepository()
    snapshot = _snapshot()
    reader = StubSubmissionReader({SUBMISSION_ID: snapshot})
    outcome = _outcome(snapshot, (_result_for("answer-1"),))
    progress = RecordingProgressUpdater()
    executor = InlineGradingTaskExecutor(
        repository=repository,
        reader=reader,
        pipeline=StubScoringPipeline(outcome),
        progress_updater=progress,
    )
    repository.save_task(make_task("task-1", SUBMISSION_ID, status=GradingTaskStatus.QUEUED))

    executor.execute("task-1", SUBMISSION_ID)

    stored = repository.get_task("task-1")
    assert stored is not None
    assert stored.status is GradingTaskStatus.COMPLETED
    assert stored.is_final is True
    assert stored.exam_result_status is ExamResultStatus.FINAL
    assert repository.get_exam_result(SUBMISSION_ID) is not None
    assert repository.get_single_result(SUBMISSION_ID, "answer-1") is not None
    assert progress.completed == [(SUBMISSION_ID, "Final")]


def test_executor_marks_task_failed_on_pipeline_error() -> None:
    """执行失败时保留错误码并记录失败进度，不写空成功结果。"""

    repository = InMemoryGradingRepository()
    reader = StubSubmissionReader({SUBMISSION_ID: _snapshot()})
    progress = RecordingProgressUpdater()
    executor = InlineGradingTaskExecutor(
        repository=repository,
        reader=reader,
        pipeline=StubScoringPipeline(
            error=GradingExecutionNotReadyError("主观题阅卷链路尚未接通。")
        ),
        progress_updater=progress,
    )
    repository.save_task(make_task("task-1", SUBMISSION_ID, status=GradingTaskStatus.QUEUED))

    executor.execute("task-1", SUBMISSION_ID)

    stored = repository.get_task("task-1")
    assert stored is not None
    assert stored.status is GradingTaskStatus.FAILED
    assert stored.error_code == GRADING_EXECUTION_NOT_READY
    assert stored.retryable is False
    assert repository.get_exam_result(SUBMISSION_ID) is None
    assert progress.failed == [(SUBMISSION_ID, GRADING_EXECUTION_NOT_READY)]


def test_default_pipeline_grades_objective_items_deterministically() -> None:
    """默认评分管道对客观题给出确定性结果，且不调用 LLM。"""

    snapshot = _snapshot(
        answers=(
            _objective_answer(order=1, answer_id="answer-1", reference="A", student="A"),
            _objective_answer(
                order=2,
                answer_id="answer-2",
                reference="B",
                student="C",
                max_score="5.00",
            ),
        )
    )

    outcome = DefaultScoringPipeline().score(snapshot)

    assert [result.score for result in outcome.results] == [10.0, 0.0]
    assert outcome.exam_result.is_final is True
    assert outcome.exam_result.final_total_score == Decimal("10.00")
    assert outcome.exam_result.total_max_score == Decimal("15.00")


def test_default_pipeline_requires_subjective_pipeline() -> None:
    """主观题需要 T069 阅卷链路；未注入时显式未就绪，不伪造评分。"""

    snapshot = _snapshot(answers=(_subjective_answer(),))

    with pytest.raises(GradingExecutionNotReadyError) as error:
        DefaultScoringPipeline().score(snapshot)

    assert error.value.error_code == GRADING_EXECUTION_NOT_READY


def test_question_result_dto_reuse_payload_is_serializable() -> None:
    """单题结果 DTO 可序列化，供 API 直接返回。"""

    dto = QuestionResultDTO(
        order=1,
        answer_id="answer-1",
        question_id="question-1",
        question_type=QuestionType.SINGLE_CHOICE,
        max_score=Decimal("10.00"),
        score=Decimal("10.00"),
        effective_score=Decimal("10.00"),
        counted=True,
        grading_status="Accepted",
        review_status="Not Required",
        validation_status="Validated",
        reason="评分理由。",
    )

    assert dto.model_dump()["answer_id"] == "answer-1"
    assert ConfidenceDecision(
        confidence=0.9,
        threshold=0.8,
        requires_review=False,
        review_status="Not Required",
        grading_status="Accepted",
        reason="自动接受。",
    ).requires_review is False
