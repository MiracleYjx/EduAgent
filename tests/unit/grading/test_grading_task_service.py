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

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.core.database import Base
from backend.app.domain.enums import (
    AnswerStatus,
    ExamStatus,
    QuestionType,
    SubmissionStatus,
)
from backend.app.models import Answer, Exam, Question, Submission
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
from backend.app.services.grading.grading_repository import GRADING_TASK_INTERRUPTED
from backend.app.services.grading.grading_task_service import (
    GRADING_EXECUTION_NOT_READY,
    GRADING_RESULT_NOT_FOUND,
    GRADING_RESULT_OWNERSHIP_MISMATCH,
    GRADING_STORE_NOT_READY,
    GRADING_TASK_NOT_FOUND,
    GRADING_TRIGGER_CONFLICT,
    DatabaseGradingSubmissionReader,
    DecisionRecordingPolicy,
    DefaultScoringPipeline,
    GradingExecutionNotReadyError,
    GradingOutcome,
    GradingResultNotFoundError,
    GradingResultOwnershipError,
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
    NonCallableSubmissionReader,
    RecordingExecutor,
    RecordingProgressUpdater,
    StubScoringPipeline,
    StubSubmissionReader,
    make_task,
)
from tests.unit.services.test_submission_service import (
    add_approved_question,
    add_course,
    add_published_exam,
    add_student,
    add_teacher,
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
    """未知任务与缺失单题结果均返回明确未找到（合同新增 teacher_id 参数）。"""

    service = _service()

    with pytest.raises(GradingTaskNotFoundError) as task_error:
        service.get_task("task-unknown", teacher_id=TEACHER_ID)
    assert task_error.value.error_code == GRADING_TASK_NOT_FOUND

    with pytest.raises(GradingResultNotFoundError) as result_error:
        service.get_single_result(
            SUBMISSION_ID,
            "answer-1",
            teacher_id=TEACHER_ID,
        )
    assert result_error.value.error_code == GRADING_RESULT_NOT_FOUND


def test_executor_persists_original_grading_fields() -> None:
    """TCR（2026-09-16，B02）：执行器保存的汇总条目必须保留原评分字段，
    否则单题查询与后续复核拿不到 confidence、要点与溯源信息。
    """

    repository = InMemoryGradingRepository()
    snapshot = _snapshot()
    result = GradingResult(
        question_type=QuestionType.SINGLE_CHOICE,
        score=6.0,
        max_score=10.0,
        reason="评分理由。",
        correct_points=["要点二", "要点一"],
        missing_knowledge_points=["缺失要点"],
        knowledge_points=["知识点 A"],
        suggestions=["建议 B", "建议 A"],
        confidence=0.77,
        retrieved_context_ids=["chunk-2", "chunk-1", "chunk-2"],
        answer_id="answer-1",
        submission_id=SUBMISSION_ID,
    )
    outcome = _outcome(snapshot, (result,))
    executor = InlineGradingTaskExecutor(
        repository=repository,
        reader=StubSubmissionReader({SUBMISSION_ID: snapshot}),
        pipeline=StubScoringPipeline(outcome),
    )
    repository.save_task(
        make_task("task-1", SUBMISSION_ID, status=GradingTaskStatus.QUEUED)
    )

    executor.execute("task-1", SUBMISSION_ID)
    stored = repository.get_single_result(SUBMISSION_ID, "answer-1")

    assert stored is not None
    assert stored.confidence == 0.77
    assert list(stored.correct_points) == ["要点二", "要点一"]
    assert list(stored.suggestions) == ["建议 B", "建议 A"]
    assert list(stored.retrieved_context_ids) == ["chunk-2", "chunk-1", "chunk-2"]
    assert stored.submission_id == SUBMISSION_ID


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


# --------------------------------------------------------------------------- #
# B03：快照必须以 Exam.questions 为权威题目集合
# --------------------------------------------------------------------------- #


#: 期望的错误码字面值；避免在红测阶段因新增符号尚不存在而只得到导入失败。
_INCOMPLETE_CODE = "GRADING_SUBMISSION_INCOMPLETE"


def _grading_session() -> Iterator[Session]:
    """用仓库既有内存 SQLite 方式构造真实数据库会话。"""

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session
    engine.dispose()


# --------------------------------------------------------------------------- #
# B01～B04：整批提交、追踪标识、并发锁与中断收敛
# --------------------------------------------------------------------------- #


def test_trigger_passes_request_id_inside_submission_lock() -> None:
    """触发在短事务锁内检查/创建任务，并把 request_id 交给仓储。"""

    repository = InMemoryGradingRepository()
    service = _service(repository=repository)

    task = service.trigger(
        SUBMISSION_ID, teacher_id=TEACHER_ID, request_id="request-fixed"
    )

    assert repository.request_ids[task.task_id] == "request-fixed"
    assert f"lock_submission:{SUBMISSION_ID}" in repository.calls
    assert task.durable is False


def test_executor_commits_outcome_and_completed_atomically() -> None:
    """TCR（B01）：终态必须随结果一起提交，禁止独立发布留下中断窗口。"""

    repository = InMemoryGradingRepository()
    snapshot = _snapshot()
    outcome = _outcome(snapshot, (_result_for("answer-1"),))
    progress = RecordingProgressUpdater()
    executor = InlineGradingTaskExecutor(
        repository=repository,
        reader=StubSubmissionReader({SUBMISSION_ID: snapshot}),
        pipeline=StubScoringPipeline(outcome),
        progress_updater=progress,
    )
    repository.save_task(
        make_task("task-1", SUBMISSION_ID, status=GradingTaskStatus.QUEUED)
    )

    executor.execute("task-1", SUBMISSION_ID)

    assert repository.outcome_calls == [(SUBMISSION_ID, "task-1")]
    assert repository.answer_orders[SUBMISSION_ID] == ("answer-1",)
    assert "save_task:task-1:Completed" not in repository.calls
    stored = repository.get_task("task-1")
    assert stored is not None
    assert stored.status is GradingTaskStatus.COMPLETED
    assert progress.completed == [(SUBMISSION_ID, "Final")]


def test_executor_failure_during_commit_keeps_error_code_without_partial_result() -> None:
    """提交阶段失败时进入失败处理：任务失败、无整卷结果、错误码保真。"""

    class FailingCommitRepository(InMemoryGradingRepository):
        """在整批提交时显式失败的替身，用于验证失败收敛。"""

        def save_outcome(
            self,
            submission_id: str,
            outcome: GradingOutcome,
            *,
            task_id: str | None = None,
            answer_order: object = None,
        ) -> None:
            raise GradingResultOwnershipError("答案不属于目标答卷，拒绝写入。")

    repository = FailingCommitRepository()
    snapshot = _snapshot()
    outcome = _outcome(snapshot, (_result_for("answer-1"),))
    progress = RecordingProgressUpdater()
    executor = InlineGradingTaskExecutor(
        repository=repository,
        reader=StubSubmissionReader({SUBMISSION_ID: snapshot}),
        pipeline=StubScoringPipeline(outcome),
        progress_updater=progress,
    )
    repository.save_task(
        make_task("task-1", SUBMISSION_ID, status=GradingTaskStatus.QUEUED)
    )

    executor.execute("task-1", SUBMISSION_ID)

    stored = repository.get_task("task-1")
    assert stored is not None
    assert stored.status is GradingTaskStatus.FAILED
    assert stored.error_code == GRADING_RESULT_OWNERSHIP_MISMATCH
    assert stored.retryable is False
    assert repository.get_exam_result(SUBMISSION_ID) is None
    assert progress.failed == [(SUBMISSION_ID, GRADING_RESULT_OWNERSHIP_MISMATCH)]


def test_commit_acknowledgement_failure_does_not_rewrite_committed_outcome() -> None:
    """TCR（B01）：提交已成功但回执异常时，以持久化终态为准，不改写为失败。"""
    class LostAcknowledgementRepository(InMemoryGradingRepository):
        def save_outcome(self, submission_id, outcome, **kwargs):
            super().save_outcome(submission_id, outcome, **kwargs)
            raise RuntimeError("提交回执中断")

    repository = LostAcknowledgementRepository()
    snapshot = _snapshot()
    outcome = _outcome(snapshot, (_result_for("answer-1"),))
    progress = RecordingProgressUpdater()
    executor = InlineGradingTaskExecutor(
        repository=repository, reader=StubSubmissionReader({SUBMISSION_ID: snapshot}),
        pipeline=StubScoringPipeline(outcome), progress_updater=progress,
    )
    repository.save_task(make_task("task-1", SUBMISSION_ID, status=GradingTaskStatus.QUEUED))
    executor.execute("task-1", SUBMISSION_ID)
    assert repository.get_task("task-1").status is GradingTaskStatus.COMPLETED
    assert repository.get_exam_result(SUBMISSION_ID) == outcome.exam_result
    assert progress.failed == []


def test_recover_interrupted_tasks_converges_running_tasks() -> None:
    """启动阶段把遗留进行中任务收敛为中断失败，已完成任务不受影响。"""

    repository = InMemoryGradingRepository()
    repository.save_task(
        make_task("task-running", SUBMISSION_ID, status=GradingTaskStatus.RUNNING)
    )
    repository.save_task(
        make_task("task-done", SUBMISSION_ID, status=GradingTaskStatus.COMPLETED)
    )
    service = _service(repository=repository)

    assert service.recover_interrupted_tasks() == 1

    interrupted = repository.get_task("task-running")
    assert interrupted is not None
    assert interrupted.status is GradingTaskStatus.FAILED
    assert interrupted.error_code == GRADING_TASK_INTERRUPTED
    assert interrupted.retryable is False
    done = repository.get_task("task-done")
    assert done is not None
    assert done.status is GradingTaskStatus.COMPLETED


def test_executor_records_diagnosis_only_for_final_result() -> None:
    """B05：仅在最终成绩提交成功后生成诊断；非最终结果不生成。"""

    class RecordingDiagnosisRecorder:
        """记录诊断生成调用的替身。"""

        def __init__(self) -> None:
            self.recorded: list[str] = []

        def record(self, exam_result: object) -> None:
            submission_id = exam_result.submission_id
            self.recorded.append(str(submission_id))

    repository = InMemoryGradingRepository()
    snapshot = _snapshot()
    outcome = _outcome(snapshot, (_result_for("answer-1"),))
    recorder = RecordingDiagnosisRecorder()
    executor = InlineGradingTaskExecutor(
        repository=repository,
        reader=StubSubmissionReader({SUBMISSION_ID: snapshot}),
        pipeline=StubScoringPipeline(outcome),
        diagnosis_recorder=recorder,
    )
    repository.save_task(
        make_task("task-1", SUBMISSION_ID, status=GradingTaskStatus.QUEUED)
    )

    executor.execute("task-1", SUBMISSION_ID)
    assert recorder.recorded == [SUBMISSION_ID]

    pending_snapshot = _snapshot(answers=(_subjective_answer(),))
    pending_result = GradingResult(
        question_type=QuestionType.SHORT_ANSWER,
        score=6.0,
        max_score=8.0,
        reason="评分理由。",
        correct_points=[],
        missing_knowledge_points=["知识点 A"],
        knowledge_points=["知识点 A"],
        suggestions=["继续练习。"],
        confidence=0.5,
        review_status="Pending Review",
        answer_id="answer-1",
        submission_id=SUBMISSION_ID,
    )
    pending_decision = ConfidenceDecision(
        confidence=0.5,
        threshold=0.8,
        requires_review=True,
        review_status="Pending Review",
        grading_status="Pending Review",
        reason="置信度低于阈值，进入待人工复核。",
    )
    pending_exam_result = ResultAggregator().aggregate(
        pending_snapshot.to_context(),
        results=[pending_result],
        decisions={"answer-1": pending_decision},
    )
    assert pending_exam_result.is_final is False
    pending_executor = InlineGradingTaskExecutor(
        repository=repository,
        reader=StubSubmissionReader({SUBMISSION_ID: pending_snapshot}),
        pipeline=StubScoringPipeline(
            GradingOutcome(
                results=(pending_result,),
                decisions={"answer-1": pending_decision},
                exam_result=pending_exam_result,
            )
        ),
        diagnosis_recorder=recorder,
    )
    repository.save_task(
        make_task("task-2", SUBMISSION_ID, status=GradingTaskStatus.QUEUED)
    )

    pending_executor.execute("task-2", SUBMISSION_ID)

    assert recorder.recorded == [SUBMISSION_ID]


# --------------------------------------------------------------------------- #
# B03：快照与持久化的既有读取用例（跟随下方 helper）
# --------------------------------------------------------------------------- #


def _seed_exam(
    session: Session,
    *,
    question_contents: tuple[str, ...] = ("解释变量。",),
) -> tuple[object, list[object], object]:
    """创建教师、课程、题目与已发布考试。"""

    teacher = add_teacher(session)
    course = add_course(session, teacher)
    question_ids = [
        add_approved_question(session, course, teacher, content=content)
        for content in question_contents
    ]
    exam = add_published_exam(session, course, teacher, list(question_ids))
    return exam, list(exam.questions), teacher


def _insert_submission(
    session: Session,
    exam: object,
    *,
    answered_questions: list[object],
) -> Submission:
    """直接插入答卷与答案行（绕过提交服务，构造不完整/越界数据）。"""

    student = add_student(session, username="reader")
    submission = Submission(
        exam_id=exam.id,
        student_id=student.id,
        status=SubmissionStatus.SUBMITTED,
        submitted_at=datetime.now(UTC),
    )
    session.add(submission)
    session.flush()
    for question in answered_questions:
        session.add(
            Answer(
                submission_id=submission.id,
                question_id=question.id,
                content="变量用于保存数据。",
                status=AnswerStatus.SUBMITTED,
            )
        )
    session.commit()
    return submission


def test_real_reader_snapshot_follows_exam_question_set() -> None:
    """完整答卷按考试题目集合与题序生成快照。"""

    for session in _grading_session():
        exam, questions, _ = _seed_exam(
            session, question_contents=("解释变量。", "解释作用域。")
        )
        submission = _insert_submission(
            session, exam, answered_questions=questions
        )
        reader = DatabaseGradingSubmissionReader(session=session)

        snapshot = reader.load(str(submission.id))

        assert [item.question_id for item in snapshot.answers] == [
            str(question.id) for question in questions
        ]
        context = snapshot.to_context()
        assert [item.order for item in context.expected_answers] == [1, 2]
        assert context.submission_id == str(submission.id)


def test_real_reader_question_order_is_deterministic() -> None:
    """题序必须按确定性键（创建时间 + 题目标识）给出，不跟随数据库返回顺序（P1.2.5）。

    数据刻意造成“创建时间顺序”与“题目标识字典序”相反：旧实现直接使用 ``exam.questions`` 的
    数据库返回顺序（复合主键索引即 ``question_id`` 升序），必然给出相反的题序。
    """

    for session in _grading_session():
        exam, questions, _ = _seed_exam(
            session, question_contents=("解释变量。", "解释作用域。")
        )
        # id 较大者先创建、id 较小者后创建，期望题序与 id 升序相反。
        later, earlier = sorted(questions, key=lambda item: str(item.id))
        earlier.created_at = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)
        later.created_at = datetime(2026, 9, 20, 11, 0, tzinfo=UTC)
        session.commit()
        submission = _insert_submission(session, exam, answered_questions=questions)
        # 强制重新加载关系：保证断言的是 ``order_by`` 而不是先前缓存的无序集合。
        session.expire_all()
        reader = DatabaseGradingSubmissionReader(session=session)

        snapshot = reader.load(str(submission.id))

        assert [item.id for item in exam.questions] == [earlier.id, later.id]
        assert [item.question_id for item in snapshot.answers] == [
            str(earlier.id),
            str(later.id),
        ]
        assert [item.order for item in snapshot.answers] == [1, 2]
        assert [item.order for item in snapshot.to_context().expected_answers] == [1, 2]


def test_real_reader_rejects_missing_answer() -> None:
    """TCR（2026-09-16，B03）：考试两题仅一条 Answer 时必须显式失败，
    不得按已有答案反推预期集合后静默给出最终成绩。
    """

    for session in _grading_session():
        exam, questions, _ = _seed_exam(
            session, question_contents=("解释变量。", "解释作用域。")
        )
        submission = _insert_submission(
            session, exam, answered_questions=questions[:1]
        )
        reader = DatabaseGradingSubmissionReader(session=session)

        with pytest.raises(Exception) as error:
            reader.load(str(submission.id))

        assert getattr(error.value, "error_code", None) == _INCOMPLETE_CODE
        assert getattr(error.value, "retryable", None) is False


def test_real_reader_rejects_answer_outside_exam() -> None:
    """答卷含考试外题目答案时必须显式失败。"""

    for session in _grading_session():
        exam, questions, teacher = _seed_exam(session)
        course = exam.course
        other_question_id = add_approved_question(
            session, course, teacher, content="另一道题的题干。"
        )
        other_question = session.get(Question, other_question_id)
        assert other_question is not None
        submission = _insert_submission(
            session, exam, answered_questions=[*questions, other_question]
        )
        reader = DatabaseGradingSubmissionReader(session=session)

        with pytest.raises(Exception) as error:
            reader.load(str(submission.id))

        assert getattr(error.value, "error_code", None) == _INCOMPLETE_CODE


def test_real_reader_rejects_exam_without_questions() -> None:
    """考试无题时不得生成快照。"""

    for session in _grading_session():
        exam, questions, teacher = _seed_exam(session)
        empty_exam = Exam(
            course_id=exam.course_id,
            created_by=teacher.id,
            title="空考试",
            status=ExamStatus.PUBLISHED,
        )
        session.add(empty_exam)
        session.commit()
        submission = _insert_submission(
            session, empty_exam, answered_questions=questions
        )
        reader = DatabaseGradingSubmissionReader(session=session)

        with pytest.raises(Exception) as error:
            reader.load(str(submission.id))

        assert getattr(error.value, "error_code", None) == _INCOMPLETE_CODE


def test_trigger_requires_store_readiness_before_reading_submission() -> None:
    """TCR（2026-09-16，B04）：结果存储未配置时必须在读取业务答卷前失败，
    否则业务库读取异常会泄漏为非预期 500，而非约定的
    ``503 GRADING_STORE_NOT_READY``。

    修复前预期：先调用读取器（AssertionError）；修复后：GradingStoreNotReadyError，
    且执行器与评分调用次数均为 0。
    """

    reader = NonCallableSubmissionReader()
    recorder = RecordingExecutor()
    service = GradingTaskService(
        repository=NotConfiguredGradingRepository(),
        reader=reader,
        executor=recorder,
    )

    with pytest.raises(GradingStoreNotReadyError) as error:
        service.trigger(SUBMISSION_ID, teacher_id=TEACHER_ID)

    assert error.value.error_code == GRADING_STORE_NOT_READY
    assert reader.teacher_load_calls == 0
    assert reader.load_calls == 0
    assert recorder.executed == []


@pytest.mark.parametrize(
    ("confidence", "expected_status"),
    [(0.799, "Pending Review"), (0.8, "Not Required"), (0.95, "Not Required")],
)
def test_default_recording_reuses_single_decision(
    monkeypatch: pytest.MonkeyPatch, confidence: float, expected_status: str,
) -> None:
    """默认记录器保存真实回填使用的那次决策，边界值不重复判定。"""

    decisions: list[ConfidenceDecision] = []
    original_evaluate = ConfidencePolicy.evaluate

    def observe_evaluate(policy, value, *, question_type=None):
        decision = original_evaluate(policy, value, question_type=question_type)
        decisions.append(decision)
        return decision

    monkeypatch.setattr(ConfidencePolicy, "evaluate", observe_evaluate)
    recording = DecisionRecordingPolicy(
        settings=build_test_settings(confidence_threshold=0.8),
    )
    result = _result(confidence=confidence)

    updated = recording.apply(result)

    assert len(decisions) == 1
    assert recording.decision_for("answer-1") is decisions[0]
    assert updated.review_status == decisions[0].review_status == expected_status
    assert updated.model_dump(exclude={"review_status"}) == result.model_dump(
        exclude={"review_status"},
    )
    assert result.review_status == "Not Required"


@pytest.mark.parametrize("reject", [False, True])
def test_recording_preserves_injected_policy(reject: bool) -> None:
    """显式策略的覆盖方法、调用顺序与拒绝后不记录的行为不变。"""

    events: list[str] = []
    decisions: list[ConfidenceDecision] = []

    class CustomPolicy(ConfidencePolicy):
        def evaluate(self, value, *, question_type=None):
            events.append("evaluate")
            decision = super().evaluate(value, question_type=question_type)
            decisions.append(decision)
            return decision

        def apply(self, result):
            events.append("apply")
            if reject:
                raise ManualReviewStateError("自定义策略拒绝此结果。")
            updated = super().apply(result)
            return updated.model_copy(update={"suggestions": ["自定义策略的建议"]})

    recording = DecisionRecordingPolicy(
        policy=CustomPolicy(threshold=0.95),
        settings=build_test_settings(confidence_threshold=0.1),
    )
    if reject:
        with pytest.raises(ManualReviewStateError, match="自定义策略拒绝"):
            recording.apply(_result(confidence=0.9))
        assert events == ["evaluate", "apply"]
        assert recording.decisions == ()
        return

    updated = recording.apply(_result(confidence=0.9))

    assert events == ["evaluate", "apply", "evaluate"]
    assert recording.decision_for("answer-1") is decisions[0]
    assert updated.review_status == "Pending Review"
    assert updated.suggestions == ["自定义策略的建议"]
