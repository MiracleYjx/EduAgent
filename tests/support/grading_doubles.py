"""阅卷任务测试替身：内存结果存储、快照读取器、可替换执行器与进度更新器。

TCR（2026-09-16，T056 / B01、B04）：T056 需要可注入的结果存储与执行器替身，才能在不
接通 T060 持久化的前提下验证“只有真实成功受理任务才返回成功受理响应”“无权限与未就绪
语义”“重复触发不得重复调用 LLM”等合同。替身只用于测试，生产默认装配仍返回 503。

替身不复制生产逻辑：``InMemoryGradingRepository`` 只做字典读写，
``StubSubmissionReader`` 只做固定快照返回与权限判定，
``RecordingProgressUpdater`` 只记录调用。
"""

from __future__ import annotations

from datetime import UTC, datetime

from backend.app.schemas.grading import (
    ExamResultDTO,
    GradingTaskStatus,
    GradingTaskStatusDTO,
    QuestionResultDTO,
)
from backend.app.services.grading.grading_task_service import (
    GradingOutcome,
    GradingPermissionError,
    GradingSubmissionNotFoundError,
    SubmissionSnapshot,
)


class InMemoryGradingRepository:
    """仅测试使用的内存结果存储；进程内、重启即失。"""

    def __init__(self) -> None:
        self.tasks: dict[str, GradingTaskStatusDTO] = {}
        self.exam_results: dict[str, ExamResultDTO] = {}
        self.single_results: dict[tuple[str, str], QuestionResultDTO] = {}
        self.calls: list[str] = []

    def ensure_ready(self) -> None:
        """内存替身始终就绪；生产存储的就绪判断由 NotConfigured 实现负责。"""

        self.calls.append("ensure_ready")

    def get_task(self, task_id: str) -> GradingTaskStatusDTO | None:
        self.calls.append(f"get_task:{task_id}")
        return self.tasks.get(task_id)
    def find_task_for_submission(self, submission_id: str) -> GradingTaskStatusDTO | None:
        self.calls.append(f"find_task_for_submission:{submission_id}")
        candidates = [
            task for task in self.tasks.values() if task.submission_id == submission_id
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda task: task.created_at)

    def save_task(self, task: GradingTaskStatusDTO) -> None:
        self.calls.append(f"save_task:{task.task_id}:{task.status.value}")
        self.tasks[task.task_id] = task.model_copy(update={"reused": False})

    def get_exam_result(self, submission_id: str) -> ExamResultDTO | None:
        self.calls.append(f"get_exam_result:{submission_id}")
        return self.exam_results.get(submission_id)

    def save_exam_result(self, exam_result: ExamResultDTO) -> None:
        self.calls.append(f"save_exam_result:{exam_result.submission_id}")
        self.exam_results[exam_result.submission_id] = exam_result

    def get_single_result(
        self,
        submission_id: str,
        answer_id: str,
    ) -> QuestionResultDTO | None:
        self.calls.append(f"get_single_result:{submission_id}:{answer_id}")
        return self.single_results.get((submission_id, answer_id))

    def save_single_result(
        self,
        submission_id: str,
        result: QuestionResultDTO,
    ) -> None:
        self.calls.append(f"save_single_result:{submission_id}:{result.answer_id}")
        self.single_results[(submission_id, result.answer_id)] = result


class StubSubmissionReader:
    """固定快照读取器；可选限制教师归属。"""

    def __init__(
        self,
        snapshots: dict[str, SubmissionSnapshot] | None = None,
        *,
        allowed_teacher_id: str | None = None,
    ) -> None:
        self.snapshots = dict(snapshots or {})
        self.allowed_teacher_id = allowed_teacher_id
        self.loaded: list[str] = []
        self.teacher_loads: list[tuple[str, str]] = []

    def load(self, submission_id: str) -> SubmissionSnapshot:
        self.loaded.append(submission_id)
        snapshot = self.snapshots.get(submission_id)
        if snapshot is None:
            raise GradingSubmissionNotFoundError(f"答卷 {submission_id} 不存在。")
        return snapshot

    def load_for_teacher(
        self,
        submission_id: str,
        teacher_id: str,
    ) -> SubmissionSnapshot:
        self.teacher_loads.append((submission_id, teacher_id))
        if (
            self.allowed_teacher_id is not None
            and teacher_id != self.allowed_teacher_id
        ):
            raise GradingPermissionError("无权访问该答卷所属课程。")
        return self.load(submission_id)


class StubScoringPipeline:
    """返回预设结果或抛错的评分管道替身。"""

    def __init__(
        self,
        outcome: GradingOutcome | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.outcome = outcome
        self.error = error
        self.calls: list[str] = []

    def score(self, snapshot: SubmissionSnapshot) -> GradingOutcome:
        self.calls.append(snapshot.submission_id)
        if self.error is not None:
            raise self.error
        assert self.outcome is not None
        return self.outcome


class NonCallableSubmissionReader:
    """被调用即失败的快照读取器替身。

    用于证明未就绪路径不会读取业务答卷数据；任何调用都会被记录并显式失败。
    """

    def __init__(self) -> None:
        self.load_calls = 0
        self.teacher_load_calls = 0

    def load(self, submission_id: str) -> SubmissionSnapshot:
        self.load_calls += 1
        raise AssertionError("未就绪路径不得读取业务答卷数据。")

    def load_for_teacher(
        self,
        submission_id: str,
        teacher_id: str,
    ) -> SubmissionSnapshot:
        self.teacher_load_calls += 1
        raise AssertionError("未就绪路径不得读取业务答卷数据。")


class RecordingExecutor:
    """记录调度与执行调用的执行器替身。

    既实现 ``GradingTaskExecutor.execute``，也可作为 ``scheduler`` 可调用对象使用。
    """

    def __init__(self, *, error: Exception | None = None) -> None:
        self.scheduled: list[tuple[str, str]] = []
        self.executed: list[tuple[str, str]] = []
        self.error = error

    def execute(self, task_id: str, submission_id: str) -> None:
        self.executed.append((task_id, submission_id))
        if self.error is not None:
            raise self.error

    def __call__(self, task_id: str, submission_id: str) -> None:
        self.scheduled.append((task_id, submission_id))
        self.execute(task_id, submission_id)


class RecordingProgressUpdater:
    """记录进度更新的替身。"""

    def __init__(self) -> None:
        self.running: list[str] = []
        self.completed: list[tuple[str, str]] = []
        self.failed: list[tuple[str, str]] = []

    def mark_running(self, submission_id: str) -> None:
        self.running.append(submission_id)

    def mark_completed(
        self,
        submission_id: str,
        exam_result: ExamResultDTO,
    ) -> None:
        self.running.append(submission_id)
        self.completed.append((submission_id, exam_result.result_status.value))

    def mark_failed(self, submission_id: str, error_code: str) -> None:
        self.failed.append((submission_id, error_code))


def make_task(
    task_id: str,
    submission_id: str,
    *,
    status: GradingTaskStatus = GradingTaskStatus.RUNNING,
    created_at: datetime | None = None,
) -> GradingTaskStatusDTO:
    """构造任务状态 DTO 用例数据。"""

    return GradingTaskStatusDTO(
        task_id=task_id,
        submission_id=submission_id,
        status=status,
        created_at=created_at or datetime(2026, 9, 16, 12, 0, tzinfo=UTC),
        started_at=created_at or datetime(2026, 9, 16, 12, 0, tzinfo=UTC),
    )


__all__ = [
    "InMemoryGradingRepository",
    "NonCallableSubmissionReader",
    "RecordingExecutor",
    "RecordingProgressUpdater",
    "StubScoringPipeline",
    "StubSubmissionReader",
    "make_task",
]
