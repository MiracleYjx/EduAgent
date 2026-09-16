"""T054/T056 阅卷应用层：置信度决策记录、任务边界、执行器与结果存储合同。

本模块是 M3 阅卷链路的应用/适配层，包含三层职责：

1. **决策记录**（T054）：:class:`DecisionRecordingPolicy` 复用既有
   :meth:`ConfidencePolicy.evaluate` 的判定记录本次 :class:`ConfidenceDecision`，
   再交给 :meth:`ConfidencePolicy.apply` 回填 ``review_status``。
2. **执行与存储边界**（T056）：:class:`GradingTaskExecutor` 定义任务执行入口，
   :class:`GradingRepository` 定义结果与任务状态的读写合同，
   :class:`GradingSubmissionReader` 定义答卷快照读取，
   :class:`ScoringPipeline` 定义单份答卷的评分管道。
3. **应用服务**（T056）：:class:`GradingTaskService` 负责触发校验、重复触发语义、
   状态查询、单题结果查询与中断任务收敛。

明确边界与限制（不得夸大）：

- 任务身份统一使用 ``task_id``（UUID4），并作为 ``workflow_runs.workflow_id`` 落库；
  ``durable`` 如实反映任务状态是否已持久化（生产仓储为 ``True``）。本阶段**不是**
  LangGraph 工作流，检查点是普通后台任务检查点，**不具备 LangGraph 检查点恢复能力**。
- 生产默认已接通真实结果存储（T060 结果表与 T064 任务状态）：触发入口装配
  :class:`~backend.app.services.grading.grading_repository.DatabaseGradingRepository`；
  :class:`NotConfiguredGradingRepository` 仅用于显式未配置场景，任何读写都抛
  :class:`GradingStoreNotReadyError`（由 API 映射为 ``503 GRADING_STORE_NOT_READY``），
  **不返回虚构 task_id、不写空成功响应**。
- 持久进度使用**既有列**（``Submission.status``/``graded_at``、``Answer.status``），由
  :class:`DatabaseGradingProgressUpdater` 在自己的会话中更新，绝不复用请求作用域会话。
- 未接通的生产执行仍以显式失败表达：:class:`DefaultScoringPipeline` 对客观题给出确定性
  结果；主观题由 :mod:`backend.app.services.grading.subjective_pipeline` 适配既有
  T050 检索上下文、T052 评分器与 T053 置信度策略注入，Provider 未就绪时显式失败，不伪造评分。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final, NoReturn, Protocol
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from backend.app.core.config import AppSettings
from backend.app.domain.enums import (
    AnswerStatus,
    GradingMode,
    QuestionType,
    SubmissionStatus,
)
from backend.app.models import Answer, Course, Exam, Submission
from backend.app.schemas.ai import GradingResult
from backend.app.schemas.grading import (
    DiagnosisReportDTO,
    ExamResultDTO,
    GradingTaskStatus,
    GradingTaskStatusDTO,
    QuestionResultDTO,
    SubmissionContext,
)
from backend.app.services.grading.confidence_policy import (
    ConfidenceDecision,
    ConfidencePolicy,
)
from backend.app.services.grading.objective_grader import ObjectiveGrader
from backend.app.services.grading.question_router import QuestionRouter
from backend.app.services.grading.result_aggregator import ResultAggregator

#: 结果存储未接通（T060 前）。
GRADING_STORE_NOT_READY: Final[str] = "GRADING_STORE_NOT_READY"
#: 评分链路未接通（T069 前的生产执行）。
GRADING_EXECUTION_NOT_READY: Final[str] = "GRADING_EXECUTION_NOT_READY"
#: 任务不存在。
GRADING_TASK_NOT_FOUND: Final[str] = "GRADING_TASK_NOT_FOUND"
#: 单题结果不存在。
GRADING_RESULT_NOT_FOUND: Final[str] = "GRADING_RESULT_NOT_FOUND"
#: 答卷不存在。
GRADING_SUBMISSION_NOT_FOUND: Final[str] = "GRADING_SUBMISSION_NOT_FOUND"
#: 答卷状态不允许阅卷。
GRADING_NOT_ALLOWED: Final[str] = "GRADING_NOT_ALLOWED"
#: 重复触发已完成任务且未请求重评。
GRADING_TRIGGER_CONFLICT: Final[str] = "GRADING_TRIGGER_CONFLICT"
#: 当前用户无权访问该答卷所属课程。
GRADING_PERMISSION_DENIED: Final[str] = "GRADING_PERMISSION_DENIED"
#: 答卷与考试题目集合不一致（缺题、越界或重复），不可重试。
GRADING_SUBMISSION_INCOMPLETE: Final[str] = "GRADING_SUBMISSION_INCOMPLETE"
#: 未预期的执行失败。
GRADING_TASK_FAILED: Final[str] = "GRADING_TASK_FAILED"
#: 评分结果携带的答案不属于目标答卷（数据一致性错误，不可重试）。
GRADING_RESULT_OWNERSHIP_MISMATCH: Final[str] = "GRADING_RESULT_OWNERSHIP_MISMATCH"
#: 创建任务时缺少 ``request_id``（plan §7 要求贯穿并落库的追踪标识）。
GRADING_TASK_TRACE_MISSING: Final[str] = "GRADING_TASK_TRACE_MISSING"

#: 允许触发阅卷的答卷状态。
TRIGGERABLE_SUBMISSION_STATES: Final[frozenset[str]] = frozenset(
    {SubmissionStatus.SUBMITTED.value}
)
#: 需要显式重评才能再次触发的答卷状态。
REGREADABLE_SUBMISSION_STATES: Final[frozenset[str]] = frozenset(
    {SubmissionStatus.GRADED.value, SubmissionStatus.REVIEWED.value}
)
#: 进行中的任务状态：重复触发时复用这些任务。
IN_FLIGHT_TASK_STATES: Final[frozenset[GradingTaskStatus]] = frozenset(
    {GradingTaskStatus.QUEUED, GradingTaskStatus.RUNNING}
)


@dataclass(frozen=True, slots=True)
class RecordedDecision:
    """一次评分产生的决策及其答案标识。"""

    answer_id: str | None
    decision: ConfidenceDecision


class DecisionRecordingPolicy:
    """记录置信度决策的策略包装，可注入 :class:`SubjectiveGrader`。

    :param policy: 既有置信度策略；``None`` 时按 ``settings`` 构造。
    :param settings: 运行配置；仅在未传入 ``policy`` 时用于解析阈值。
    """

    def __init__(
        self,
        policy: ConfidencePolicy | None = None,
        *,
        settings: AppSettings | None = None,
    ) -> None:
        self._policy = policy if policy is not None else ConfidencePolicy(settings=settings)
        self._records: list[RecordedDecision] = []

    @property
    def decisions(self) -> tuple[RecordedDecision, ...]:
        """按记录顺序返回本次实例产生的全部决策。"""

        return tuple(self._records)

    def decision_for(self, answer_id: str) -> ConfidenceDecision | None:
        """返回指定答案最近一次的决策；没有记录时返回 ``None``。"""

        for record in reversed(self._records):
            if record.answer_id == answer_id:
                return record.decision
        return None

    def decisions_by_answer_id(self) -> dict[str, ConfidenceDecision]:
        """返回 ``answer_id`` 到决策的映射，供汇总阶段消费。

        缺少 ``answer_id`` 的记录无法定位题目，只保留在 :attr:`decisions` 中，
        由汇总阶段按身份缺失处理。
        """

        resolved: dict[str, ConfidenceDecision] = {}
        for record in self._records:
            if record.answer_id is not None:
                resolved[record.answer_id] = record.decision
        return resolved

    def apply(self, result: GradingResult) -> GradingResult:
        """执行置信度检查、记录决策并返回回填后的结果。

        仅当既有策略接受该结果（已通过结构化校验且没有人工结论）时才记录决策，
        避免把被拒绝的输入当作已完成检查。
        """

        decision = self._policy.evaluate(
            result.confidence,
            question_type=result.question_type,
        )
        updated = self._policy.apply(result)
        self._records.append(
            RecordedDecision(answer_id=result.answer_id, decision=decision)
        )
        return updated


class GradingTaskError(RuntimeError):
    """阅卷任务边界错误基类；保留脱敏错误码与重试语义。"""

    error_code: str = GRADING_TASK_FAILED
    retryable: bool = False

    def __init__(
        self,
        detail: str,
        *,
        retryable: bool | None = None,
        source_code: str | None = None,
    ) -> None:
        super().__init__(f"{self.error_code}：{detail}")
        self.detail = detail
        if retryable is not None:
            self.retryable = retryable
        self.source_code = source_code


class GradingStoreNotReadyError(GradingTaskError):
    """结果存储未接通（T060 前）。"""

    error_code = GRADING_STORE_NOT_READY


class GradingExecutionNotReadyError(GradingTaskError):
    """评分执行链路未接通（T069 前）。"""

    error_code = GRADING_EXECUTION_NOT_READY


class GradingTaskNotFoundError(GradingTaskError):
    """任务不存在。"""

    error_code = GRADING_TASK_NOT_FOUND


class GradingResultNotFoundError(GradingTaskError):
    """单题结果不存在。"""

    error_code = GRADING_RESULT_NOT_FOUND


class GradingSubmissionNotFoundError(GradingTaskError):
    """答卷不存在。"""

    error_code = GRADING_SUBMISSION_NOT_FOUND


class GradingNotAllowedError(GradingTaskError):
    """答卷状态不允许阅卷。"""

    error_code = GRADING_NOT_ALLOWED


class GradingTriggerConflictError(GradingTaskError):
    """重复触发已完成任务且未请求重评。"""

    error_code = GRADING_TRIGGER_CONFLICT


class GradingPermissionError(GradingTaskError):
    """当前用户无权访问该答卷所属课程。"""

    error_code = GRADING_PERMISSION_DENIED


class GradingSubmissionIncompleteError(GradingTaskError):
    """答卷与考试题目集合不一致：缺题、越界或重复。

    不可重试；消息只含题目标识，不包含学生答案内容。
    """

    error_code = GRADING_SUBMISSION_INCOMPLETE


class GradingResultOwnershipError(GradingTaskError):
    """评分结果携带的答案不属于目标答卷；拒绝写入以免污染他人答卷。"""

    error_code = GRADING_RESULT_OWNERSHIP_MISMATCH


class GradingTaskTraceMissingError(GradingTaskError):
    """创建任务时未提供 ``request_id``；拒绝写入无追踪标识的任务记录。"""

    error_code = GRADING_TASK_TRACE_MISSING


@dataclass(frozen=True, slots=True)
class GradingTargetAnswer:
    """单题阅卷目标：题目定义、标准答案与学生作答的只读快照。"""

    order: int
    answer_id: str
    question_id: str
    question_type: QuestionType
    max_score: Decimal
    knowledge_points: tuple[str, ...] = ()
    content: str = ""
    reference_answer: str | None = None
    scoring_rubric: str | None = None
    student_answer: str | Sequence[str] | Mapping[str, str] | None = None


@dataclass(frozen=True, slots=True)
class SubmissionSnapshot:
    """一份答卷的只读快照，只包含触发与评分所需的字段。"""

    submission_id: str
    exam_id: str
    student_id: str
    course_id: str
    status: str
    answers: tuple[GradingTargetAnswer, ...]

    def to_context(self) -> SubmissionContext:
        """转换为 T054 汇总所需的预期题目集合与题序。"""

        from backend.app.schemas.grading import ExpectedAnswer

        return SubmissionContext(
            submission_id=self.submission_id,
            exam_id=self.exam_id,
            student_id=self.student_id,
            expected_answers=[
                ExpectedAnswer(
                    order=answer.order,
                    answer_id=answer.answer_id,
                    question_id=answer.question_id,
                    question_type=answer.question_type,
                    max_score=answer.max_score,
                    knowledge_points=list(answer.knowledge_points),
                )
                for answer in self.answers
            ],
        )


@dataclass(frozen=True, slots=True)
class GradingOutcome:
    """一次评分管道的产出：单题结果、决策与整卷结果。"""

    results: tuple[GradingResult, ...]
    decisions: Mapping[str, ConfidenceDecision] = field(default_factory=dict)
    exam_result: ExamResultDTO | None = None


class GradingRepository(Protocol):
    """任务状态与结果的读写合同。

    生产实现是 :class:`~backend.app.services.grading.grading_repository.DatabaseGradingRepository`；
    测试可注入内存替身，但内存实现不得作为正式成绩事实源。

    ``ensure_ready()`` 是最小就绪判断：未接通的实现必须先失败，由调用方在读取业务数据、
    创建任务或调用执行器之前抛出，避免业务库异常泄漏为非预期 500。

    写入语义（B01～B03）：

    - ``save_outcome`` 必须在**一次事务**内提交单题结果、决策快照、整卷结果与答卷进度；
      单题结果按 ``answer_id`` 就地更新，保留主键与既有复核记录关联，不做批量删除；
    - ``save_task`` 写入任务状态与检查点；任务进入失败状态时同事务更新失败进度；
    - ``lock_submission`` 提供“检查已有任务—创建任务”所需的短事务互斥；
    - ``durable`` 如实反映任务状态是否已持久化。
    """

    #: 任务状态是否已持久化（可跨进程重启查询）。
    durable: bool

    def ensure_ready(self) -> None: ...

    def get_task(self, task_id: str) -> GradingTaskStatusDTO | None: ...

    def find_task_for_submission(
        self,
        submission_id: str,
    ) -> GradingTaskStatusDTO | None: ...

    def save_task(
        self,
        task: GradingTaskStatusDTO,
        *,
        request_id: str | None = None,
    ) -> None: ...

    def lock_submission(self, submission_id: str) -> AbstractContextManager[None]: ...

    def mark_interrupted_tasks_failed(self) -> int: ...

    def get_exam_result(self, submission_id: str) -> ExamResultDTO | None: ...

    def save_exam_result(self, exam_result: ExamResultDTO) -> None: ...

    def save_outcome(
        self,
        submission_id: str,
        outcome: GradingOutcome,
        *,
        task_id: str | None = None,
        answer_order: Sequence[str] | None = None,
    ) -> None: ...

    def get_single_result(
        self,
        submission_id: str,
        answer_id: str,
    ) -> QuestionResultDTO | None: ...

    def save_single_result(
        self,
        submission_id: str,
        result: QuestionResultDTO,
    ) -> None: ...


class NotConfiguredGradingRepository:
    """未接通的结果存储：任何访问都显式未就绪，不伪造结果。

    生产装配已改用真实仓储；本实现仅用于显式未配置的场景与单测，
    保证“未接通时返回 503 而非虚构任务或空成功”这一语义仍可验证。
    """

    #: 未接通时无任何持久化事实。
    durable: bool = False

    def _reject(self) -> NoReturn:
        raise GradingStoreNotReadyError(
            "结果存储尚未接通，无法保存或查询阅卷状态与成绩。"
        )

    def ensure_ready(self) -> None:
        self._reject()

    def get_task(self, task_id: str) -> GradingTaskStatusDTO | None:
        self._reject()

    def find_task_for_submission(
        self,
        submission_id: str,
    ) -> GradingTaskStatusDTO | None:
        self._reject()

    def save_task(
        self,
        task: GradingTaskStatusDTO,
        *,
        request_id: str | None = None,
    ) -> None:
        self._reject()

    def lock_submission(self, submission_id: str) -> AbstractContextManager[None]:
        self._reject()

    def mark_interrupted_tasks_failed(self) -> int:
        self._reject()

    def get_exam_result(self, submission_id: str) -> ExamResultDTO | None:
        self._reject()

    def save_exam_result(self, exam_result: ExamResultDTO) -> None:
        self._reject()

    def save_outcome(
        self,
        submission_id: str,
        outcome: GradingOutcome,
        *,
        task_id: str | None = None,
        answer_order: Sequence[str] | None = None,
    ) -> None:
        self._reject()

    def get_single_result(
        self,
        submission_id: str,
        answer_id: str,
    ) -> QuestionResultDTO | None:
        self._reject()

    def save_single_result(
        self,
        submission_id: str,
        result: QuestionResultDTO,
    ) -> None:
        self._reject()


class GradingSubmissionReader(Protocol):
    """答卷快照读取合同。"""

    def load(self, submission_id: str) -> SubmissionSnapshot: ...

    def load_for_teacher(
        self,
        submission_id: str,
        teacher_id: str,
    ) -> SubmissionSnapshot: ...


class DatabaseGradingSubmissionReader:
    """从数据库读取答卷快照，并检查教师课程归属。

    :param session: 单会话用法（API 依赖注入）；与 ``session_factory`` 二选一。
    :param session_factory: 自建会话用法（后台任务）；不得复用请求作用域会话。
    """

    def __init__(
        self,
        *,
        session: Session | None = None,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        if session is None and session_factory is None:
            raise ValueError("必须提供 session 或 session_factory。")
        self._session = session
        self._session_factory = session_factory

    @contextmanager
    def _use_session(self) -> Iterator[Session]:
        if self._session_factory is not None:
            session = self._session_factory()
            try:
                yield session
            finally:
                session.close()
            return
        assert self._session is not None
        yield self._session

    def load(self, submission_id: str) -> SubmissionSnapshot:
        with self._use_session() as session:
            submission = self._load_submission(session, submission_id)
            return self._build_snapshot(session, submission)

    def load_for_teacher(
        self,
        submission_id: str,
        teacher_id: str,
    ) -> SubmissionSnapshot:
        with self._use_session() as session:
            submission = self._load_submission(session, submission_id)
            exam = session.get(Exam, submission.exam_id)
            course = session.get(Course, exam.course_id) if exam is not None else None
            if course is None:
                raise GradingSubmissionNotFoundError(
                    f"答卷 {submission_id} 关联的考试或课程不存在。"
                )
            if str(course.created_by) != str(teacher_id):
                raise GradingPermissionError("无权访问该答卷所属课程。")
            return self._build_snapshot(session, submission)

    @staticmethod
    def _load_submission(session: Session, submission_id: str) -> Submission:
        submission = session.get(Submission, _as_uuid(submission_id))
        if submission is None:
            raise GradingSubmissionNotFoundError(f"答卷 {submission_id} 不存在。")
        return submission

    @staticmethod
    def _build_snapshot(session: Session, submission: Submission) -> SubmissionSnapshot:
        """按考试题目集合生成快照；不完整或越界数据显式失败。

        权威题目集合与题序来自 ``Exam.questions`` 关系列表（现有题序约定），
        不从已有 ``Answer`` 反推，避免集合被缩小或污染（plan §5.1/§5.2）。
        不自动创建答案、不补零分、不删除多余答案、不修改冻结作答。
        """

        exam = session.get(Exam, submission.exam_id)
        if exam is None:
            raise GradingSubmissionNotFoundError("答卷关联的考试不存在。")
        questions = list(exam.questions)
        if not questions:
            raise GradingSubmissionIncompleteError("考试没有可评分的题目。")
        expected_ids = {question.id for question in questions}
        answers_by_question: dict[UUID, Answer] = {}
        for answer in submission.answers:
            if answer.question_id not in expected_ids:
                raise GradingSubmissionIncompleteError(
                    f"答案不属于当前考试：{answer.question_id}。"
                )
            if answer.question_id in answers_by_question:
                raise GradingSubmissionIncompleteError(
                    f"答卷存在重复答案：{answer.question_id}。"
                )
            answers_by_question[answer.question_id] = answer
        missing = [
            str(question.id)
            for question in questions
            if question.id not in answers_by_question
        ]
        if missing:
            raise GradingSubmissionIncompleteError(
                f"答卷缺少题目答案：{'、'.join(missing)}。"
            )
        targets = [
            GradingTargetAnswer(
                order=order,
                answer_id=str(answers_by_question[question.id].id),
                question_id=str(question.id),
                question_type=question.type,
                max_score=_as_decimal(question.score),
                knowledge_points=tuple(question.knowledge_points or ()),
                content=question.content,
                reference_answer=question.reference_answer,
                scoring_rubric=question.scoring_rubric,
                student_answer=answers_by_question[question.id].content,
            )
            for order, question in enumerate(questions, start=1)
        ]
        return SubmissionSnapshot(
            submission_id=str(submission.id),
            exam_id=str(exam.id),
            student_id=str(submission.student_id),
            course_id=str(exam.course_id),
            status=submission.status.value,
            answers=tuple(targets),
        )


class GradingProgressUpdater(Protocol):
    """持久进度更新合同：只更新既有列，不新增模型。"""

    def mark_running(self, submission_id: str) -> None: ...

    def mark_completed(
        self,
        submission_id: str,
        exam_result: ExamResultDTO,
    ) -> None: ...

    def mark_failed(self, submission_id: str, error_code: str) -> None: ...


class GradingDiagnosisRecorder(Protocol):
    """最终整卷结果形成后的诊断生成与保存合同（B05）。

    实现须在最终成绩提交成功后才被调用；生成失败只能影响诊断报告本身，
    不得回滚已提交的成绩与汇总事实。
    """

    def record(self, exam_result: ExamResultDTO) -> DiagnosisReportDTO | None: ...


class DatabaseGradingProgressUpdater:
    """使用既有 ``Submission``/``Answer`` 列记录持久进度。"""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    @contextmanager
    def _use_session(self) -> Iterator[Session]:
        if self._session_factory is None:
            raise GradingStoreNotReadyError(
                "进度更新需要可用的会话工厂，当前未接通持久化。"
            )
        session = self._session_factory()
        try:
            yield session
        finally:
            session.close()

    def mark_running(self, submission_id: str) -> None:
        with self._use_session() as session:
            submission = self._require_submission(session, submission_id)
            for answer in submission.answers:
                answer.status = AnswerStatus.GRADING
            session.commit()

    def mark_completed(
        self,
        submission_id: str,
        exam_result: ExamResultDTO,
    ) -> None:
        with self._use_session() as session:
            submission = self._require_submission(session, submission_id)
            moment = self._clock()
            for answer in submission.answers:
                answer.status = AnswerStatus.GRADED
            submission.graded_at = moment
            if submission.status is SubmissionStatus.SUBMITTED:
                submission.status = SubmissionStatus.GRADED
            session.commit()

    def mark_failed(self, submission_id: str, error_code: str) -> None:
        with self._use_session() as session:
            submission = self._require_submission(session, submission_id)
            for answer in submission.answers:
                answer.status = AnswerStatus.FAILED
            session.commit()

    @staticmethod
    def _require_submission(session: Session, submission_id: str) -> Submission:
        submission = session.get(Submission, _as_uuid(submission_id))
        if submission is None:
            raise GradingSubmissionNotFoundError(f"答卷 {submission_id} 不存在。")
        return submission


class ScoringPipeline(Protocol):
    """单份答卷的评分管道合同；T069 的阅卷 Agent 将实现同一合同。"""

    def score(self, snapshot: SubmissionSnapshot) -> GradingOutcome: ...


class DefaultScoringPipeline:
    """默认评分管道：客观题走确定性规则，主观题需要注入 T069 阅卷链路。

    :param subjective_scorer: 主观题评分函数，返回 ``(GradingResult, ConfidenceDecision)``；
        ``None`` 表示主观题执行链路未接通，遇到主观题时显式未就绪。
    """

    def __init__(
        self,
        subjective_scorer: (
            Callable[
                [SubmissionSnapshot, GradingTargetAnswer],
                tuple[GradingResult, ConfidenceDecision],
            ]
            | None
        ) = None,
    ) -> None:
        self._objective = ObjectiveGrader()
        self._router = QuestionRouter()
        self._aggregator = ResultAggregator()
        self._subjective_scorer = subjective_scorer

    def score(self, snapshot: SubmissionSnapshot) -> GradingOutcome:
        results: list[GradingResult] = []
        decisions: dict[str, ConfidenceDecision] = {}
        for target in snapshot.answers:
            mode = self._router.route_type(target.question_type)
            if mode is GradingMode.OBJECTIVE:
                results.append(self._grade_objective(snapshot, target))
                continue
            if self._subjective_scorer is None:
                raise GradingExecutionNotReadyError(
                    "主观题阅卷链路（T069）尚未接通，无法完成评分。"
                )
            result, decision = self._subjective_scorer(snapshot, target)
            results.append(result)
            decisions[target.answer_id] = decision
        exam_result = self._aggregator.aggregate(
            snapshot.to_context(),
            results=results,
            decisions=decisions,
        )
        return GradingOutcome(
            results=tuple(results),
            decisions=decisions,
            exam_result=exam_result,
        )

    def _grade_objective(
        self,
        snapshot: SubmissionSnapshot,
        target: GradingTargetAnswer,
    ) -> GradingResult:
        student_answer = target.student_answer
        if isinstance(student_answer, Mapping):
            raise GradingExecutionNotReadyError(
                "字典形态的学生答案缺少键语义与顺序约定，不能用于客观题评分。"
            )
        return self._objective.grade(
            question_type=target.question_type,
            reference_answer=target.reference_answer,
            student_answer=student_answer,
            max_score=float(target.max_score),
            knowledge_points=target.knowledge_points,
            answer_id=target.answer_id,
            submission_id=snapshot.submission_id,
        )


class GradingTaskExecutor(Protocol):
    """任务执行入口合同。"""

    def execute(self, task_id: str, submission_id: str) -> None: ...


class InlineGradingTaskExecutor:
    """默认执行器：读取快照、运行评分管道、写入结果与持久进度。

    :param progress_updater: 进度更新实现；``None`` 时跳过持久进度更新。
    :param diagnosis_recorder: 诊断生成与保存入口（B05）；``None`` 时不生成诊断。
    """

    def __init__(
        self,
        *,
        repository: GradingRepository,
        reader: GradingSubmissionReader,
        pipeline: ScoringPipeline,
        progress_updater: GradingProgressUpdater | None = None,
        diagnosis_recorder: GradingDiagnosisRecorder | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._reader = reader
        self._pipeline = pipeline
        self._progress = progress_updater
        self._diagnosis = diagnosis_recorder
        self._clock = clock or (lambda: datetime.now(UTC))

    def execute(self, task_id: str, submission_id: str) -> None:
        """执行任务；失败以任务状态与进度表达，不向后台调度器泄漏异常。

        写入顺序（B03）：

        1. 发布 ``Running``（含 ``started_at``）；
        2. 读取快照并评分，此阶段**不持有写事务**，LLM 调用不阻塞其它写者；
        3. 以一次 :meth:`GradingRepository.save_outcome` 提交单题结果、决策快照、
           整卷结果与答卷进度；任何失败整体回滚并转入失败处理，不留部分成功；
        4. 提交成功后才发布 ``Completed``（存在待复核时工作流落为 ``Paused``）。
        """

        task = self._repository.get_task(task_id)
        if task is None:
            raise GradingTaskNotFoundError(f"任务 {task_id} 不存在。")
        started_at = task.started_at or self._clock()
        self._repository.save_task(
            task.model_copy(
                update={
                    "status": GradingTaskStatus.RUNNING,
                    "reused": False,
                    "started_at": started_at,
                }
            )
        )
        if self._progress is not None:
            self._progress.mark_running(submission_id)
        try:
            snapshot = self._reader.load(submission_id)
            outcome = self._pipeline.score(snapshot)
            exam_result = outcome.exam_result
            if exam_result is None:
                raise GradingExecutionNotReadyError("评分管道未返回整卷结果。")
            self._repository.save_outcome(
                submission_id,
                outcome,
                task_id=task_id,
                answer_order=tuple(answer.answer_id for answer in snapshot.answers),
            )
        except GradingTaskError as error:
            self._record_failure(task_id, submission_id, error)
            return
        except Exception as error:  # noqa: BLE001 - 统一收敛为脱敏失败
            self._record_failure(
                task_id,
                submission_id,
                GradingTaskError(f"阅卷任务执行失败（{type(error).__name__}）。"),
            )
            return

        self._repository.save_task(
            task.model_copy(
                update={
                    "status": GradingTaskStatus.COMPLETED,
                    "reused": False,
                    "started_at": started_at,
                    "finished_at": self._clock(),
                    "expected_answer_count": exam_result.expected_answer_count,
                    "graded_answer_count": exam_result.graded_answer_count,
                    "pending_review_answer_count": (
                        exam_result.pending_review_answer_count
                    ),
                    "exam_result_status": exam_result.result_status,
                    "is_final": exam_result.is_final,
                }
            )
        )
        if self._progress is not None:
            self._progress.mark_completed(submission_id, exam_result)
        if exam_result.is_final:
            self._record_diagnosis(exam_result)

    def _record_diagnosis(self, exam_result: ExamResultDTO) -> None:
        """最终成绩提交成功后生成并保存诊断报告（B05）。

        仅当整卷已形成最终成绩时调用；诊断生成失败只影响诊断报告（由存储层按
        ``Failed`` 记录），**不回滚已提交的成绩与汇总事实**，也不向调度器泄漏异常。
        """

        recorder = self._diagnosis
        if recorder is None:
            return
        try:
            recorder.record(exam_result)
        except GradingTaskError:
            # 存储未就绪等基础设施问题不得抹掉已经提交的成绩与汇总事实。
            return

    def _record_failure(
        self,
        task_id: str,
        submission_id: str,
        error: GradingTaskError,
    ) -> None:
        stored = self._repository.get_task(task_id)
        if stored is not None:
            self._repository.save_task(
                stored.model_copy(
                    update={
                        "status": GradingTaskStatus.FAILED,
                        "reused": False,
                        "started_at": stored.started_at or self._clock(),
                        "finished_at": self._clock(),
                        "error_code": error.error_code,
                        "error_message": error.detail,
                        "retryable": bool(error.retryable),
                    }
                )
            )
        if self._progress is not None:
            self._progress.mark_failed(submission_id, error.error_code)


class GradingTaskService:
    """阅卷任务应用服务：触发校验、任务状态与单题结果查询。

    :param repository: 任务与结果存储；生产默认未就绪。
    :param reader: 答卷快照读取；教师访问必须携带课程归属校验。
    :param executor: 任务执行器。
    :param scheduler: 后台调度函数 ``(task_id, submission_id) -> None``；
        ``None`` 时同步执行（供无后台环境的调用方与测试使用）。
    :param clock: 时间来源，便于测试固定时间。
    """

    def __init__(
        self,
        *,
        repository: GradingRepository,
        reader: GradingSubmissionReader,
        executor: GradingTaskExecutor,
        scheduler: Callable[[str, str], None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._reader = reader
        self._executor = executor
        self._scheduler = scheduler
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def executor(self) -> GradingTaskExecutor:
        """返回执行器，便于 API 把它交给后台任务调度。"""

        return self._executor

    def trigger(
        self,
        submission_id: str,
        *,
        teacher_id: str,
        regrade: bool = False,
        scheduler: Callable[[str, str], None] | None = None,
        request_id: str | None = None,
    ) -> GradingTaskStatusDTO:
        """触发阅卷；仅在真实受理任务时返回任务状态。

        “检查已有任务—创建任务”在 :meth:`GradingRepository.lock_submission` 提供的短事务
        互斥内完成，避免并发触发为同一答卷创建两个任务并互相覆盖结果。``request_id`` 由
        触发入口生成（未提供时生成 UUID4）并贯穿任务记录。
        """

        self._repository.ensure_ready()
        snapshot = self._reader.load_for_teacher(submission_id, teacher_id)
        if snapshot.status not in TRIGGERABLE_SUBMISSION_STATES:
            if snapshot.status not in REGREADABLE_SUBMISSION_STATES:
                raise GradingNotAllowedError(
                    f"答卷状态 {snapshot.status} 不允许触发阅卷。"
                )
            if not regrade:
                raise GradingTriggerConflictError(
                    "答卷已完成评分，需要显式请求重评。"
                )
        trace_id = request_id or str(uuid4())
        with self._repository.lock_submission(submission_id):
            existing = self._repository.find_task_for_submission(submission_id)
            if existing is not None:
                if existing.status in IN_FLIGHT_TASK_STATES:
                    return existing.model_copy(update={"reused": True})
                if not regrade:
                    raise GradingTriggerConflictError("该答卷已有已完成的阅卷任务。")

            task = GradingTaskStatusDTO(
                task_id=str(uuid4()),
                submission_id=submission_id,
                status=GradingTaskStatus.QUEUED,
                durable=self._repository.durable,
                created_at=self._clock(),
            )
            self._repository.save_task(task, request_id=trace_id)
        schedule = scheduler if scheduler is not None else self._scheduler
        if schedule is not None:
            schedule(task.task_id, submission_id)
        else:
            self.executor.execute(task.task_id, submission_id)
        return task

    def recover_interrupted_tasks(self) -> int:
        """把遗留的进行中任务收敛为中断失败，返回处理条数。

        单进程部署下进程重启后不可能仍有任务在运行；这里如实标记为中断失败，由教师显式
        请求重评重新执行。**不实现自动恢复队列**，也不宣称具备 LangGraph 检查点恢复能力。
        """

        return self._repository.mark_interrupted_tasks_failed()

    def get_task(self, task_id: str, *, teacher_id: str) -> GradingTaskStatusDTO:
        """查询任务状态。

        先取得定位答卷所需的任务元数据，再按 ``task.submission_id`` 校验教师课程归属；
        授权通过后才返回任务内容（plan §5.2、FR-036/FR-039）。

        :param teacher_id: 认证上下文中的教师标识；不做可省略的绕过分支。
        """

        self._repository.ensure_ready()
        task = self._repository.get_task(task_id)
        if task is None:
            raise GradingTaskNotFoundError(f"任务 {task_id} 不存在。")
        self._reader.load_for_teacher(task.submission_id, teacher_id)
        return task

    def get_single_result(
        self,
        submission_id: str,
        answer_id: str,
        *,
        teacher_id: str,
    ) -> QuestionResultDTO:
        """查询单题结构化结果。

        先校验教师对该答卷所属课程的访问权限，再查询该答卷下的 ``answer_id``；
        不得仅凭权限守位就返回他课程结果。

        :param teacher_id: 认证上下文中的教师标识；不做可省略的绕过分支。
        """

        self._repository.ensure_ready()
        self._reader.load_for_teacher(submission_id, teacher_id)
        result = self._repository.get_single_result(submission_id, answer_id)
        if result is None:
            raise GradingResultNotFoundError(
                f"答案 {answer_id} 尚无结构化评分结果。"
            )
        return result


def _as_uuid(value: str) -> UUID:
    """把字符串标识转换为 UUID；非法标识按未找到处理。"""

    try:
        return UUID(value)
    except ValueError as error:
        raise GradingSubmissionNotFoundError("答卷标识非法。") from error


def _as_decimal(value: object) -> Decimal:
    """把题目分值转换为 Decimal。"""

    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


__all__ = [
    "GRADING_EXECUTION_NOT_READY",
    "GRADING_NOT_ALLOWED",
    "GRADING_PERMISSION_DENIED",
    "GRADING_RESULT_NOT_FOUND",
    "GRADING_RESULT_OWNERSHIP_MISMATCH",
    "GRADING_STORE_NOT_READY",
    "GRADING_SUBMISSION_INCOMPLETE",
    "GRADING_SUBMISSION_NOT_FOUND",
    "GRADING_TASK_FAILED",
    "GRADING_TASK_NOT_FOUND",
    "GRADING_TASK_TRACE_MISSING",
    "GRADING_TRIGGER_CONFLICT",
    "IN_FLIGHT_TASK_STATES",
    "REGREADABLE_SUBMISSION_STATES",
    "TRIGGERABLE_SUBMISSION_STATES",
    "DatabaseGradingProgressUpdater",
    "DatabaseGradingSubmissionReader",
    "DecisionRecordingPolicy",
    "DefaultScoringPipeline",
    "GradingDiagnosisRecorder",
    "GradingExecutionNotReadyError",
    "GradingNotAllowedError",
    "GradingOutcome",
    "GradingPermissionError",
    "GradingProgressUpdater",
    "GradingRepository",
    "GradingResultNotFoundError",
    "GradingResultOwnershipError",
    "GradingStoreNotReadyError",
    "GradingSubmissionIncompleteError",
    "GradingSubmissionNotFoundError",
    "GradingSubmissionReader",
    "GradingTargetAnswer",
    "GradingTaskError",
    "GradingTaskExecutor",
    "GradingTaskNotFoundError",
    "GradingTaskService",
    "GradingTaskTraceMissingError",
    "GradingTriggerConflictError",
    "InlineGradingTaskExecutor",
    "NotConfiguredGradingRepository",
    "RecordedDecision",
    "ScoringPipeline",
    "SubmissionSnapshot",
]
