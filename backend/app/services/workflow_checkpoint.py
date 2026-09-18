"""阅卷工作流检查点持久化（T073）。

契约依据：``.specify/plan.md`` §5（状态图与控制逻辑）、``.specify/data-model.md``
（WorkflowRun 状态与关键校验规则）、``backend/app/models/workflow_run.py``（T064 列定义）、
``backend/app/ai/workflows/state.py``（T065 业务状态快照载荷）、
``backend/app/ai/workflows/grading_handoff.py``（T071 复核状态契约）与 FR-036（人工复核可中断、
可恢复，恢复必须沿用原运行标识）。

职责与边界：

- **只做持久化**：把工作流业务状态快照写入 T064 ``WorkflowRun`` 的 ``checkpoint`` 列，并如实记录
  ``current_node``/``current_answer_id``/``status``/``pause_reason``/``retry_count``/``resumable``。
  本模块不执行图、不判定置信度、不写教师结论，也不实现 T076 的 API。
- **消费 T065 编解码**：状态一律经 :func:`workflow_state_to_checkpoint_payload` 校验并编码，
  ``kind``/``version`` 复用 T065 常量；本模块不定义第二套状态载荷格式。解码走
  :func:`workflow_state_from_checkpoint_payload`，因此 T072 的恢复入口可以原样消费落库内容。
- **``resumable`` 不假声明（H02）**：只有“状态为 ``Paused`` + 已有暂停原因 + 状态自身声明可恢复”
  三者同时成立才记为真。T072 只在注入检查点支撑时才把状态置为可恢复，因此数据库版存储不会因为
  “快照已落库”而把不可恢复的运行升级为可恢复。
- **失败与完成必须可核验**：``mark_completed`` 要求状态中没有失败事实、没有待人工复核题目；
  ``mark_failed`` 按 T072 ``_failure`` 的口径写入 ``status=Failed``、``error``、``resumable=False``，
  并清空 ``review_status``（失败不得与“待复核”并存）。两者都拒绝在缺少可用状态快照时伪造状态。
- **身份一致**：同一 ``workflow_id`` 只能绑定一个 ``submission_id`` 与 ``request_id``；跨答卷或跨请求
  的覆盖一律拒绝，避免把 A 卷的检查点写到 B 卷运行记录上。

时间戳：``created_at``/``updated_at`` 由 T064 的时间戳混入提供；载荷内的 ``saved_at`` 记录本次
落库时间，供排障与 T076 展示。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.ai.agents.state import AgentError
from backend.app.ai.workflows.grading_handoff import ACCEPTED_REVIEW_STATES
from backend.app.ai.workflows.state import (
    GradingWorkflowState,
    WorkflowStateError,
    workflow_state_from_checkpoint_payload,
    workflow_state_to_checkpoint_payload,
)
from backend.app.domain.enums import ReviewStatus, WorkflowStatus
from backend.app.models import ExamResult, WorkflowRun
from backend.app.schemas.grading import ConfidenceDecisionDTO

#: 检查点状态缺失、类型非法或不合法值。
WORKFLOW_CHECKPOINT_INVALID_INPUT: Final[str] = "WORKFLOW_CHECKPOINT_INVALID_INPUT"
#: 目标 ``workflow_id`` 没有检查点记录。
WORKFLOW_CHECKPOINT_NOT_FOUND: Final[str] = "WORKFLOW_CHECKPOINT_NOT_FOUND"
#: 检查点存储未就绪（未注入会话或表不存在）。
WORKFLOW_CHECKPOINT_STORE_NOT_READY: Final[str] = "WORKFLOW_CHECKPOINT_STORE_NOT_READY"
#: 状态无法校验或编码为 T065 业务状态快照。
WORKFLOW_CHECKPOINT_STATE_INVALID: Final[str] = "WORKFLOW_CHECKPOINT_STATE_INVALID"
#: 检查点已属于其它答卷或其它请求。
WORKFLOW_CHECKPOINT_OWNERSHIP_MISMATCH: Final[str] = "WORKFLOW_CHECKPOINT_OWNERSHIP_MISMATCH"
#: 运行事实不允许标记完成（仍有待复核题目或保留了失败事实）。
WORKFLOW_CHECKPOINT_COMPLETION_NOT_ALLOWED: Final[str] = "WORKFLOW_CHECKPOINT_COMPLETION_NOT_ALLOWED"
#: 调用方只给出脱敏说明时的默认失败错误码。
WORKFLOW_CHECKPOINT_FAILED: Final[str] = "WORKFLOW_CHECKPOINT_FAILED"

#: 载荷中记录 LangGraph 线程标识的附加键；不在 T065 业务状态快照字段集合内。
CHECKPOINT_THREAD_ID_KEY: Final[str] = "thread_id"
#: 载荷中记录本次落库时间的附加键。
CHECKPOINT_SAVED_AT_KEY: Final[str] = "saved_at"
#: 就绪检查所需的表。
REQUIRED_TABLES: Final[tuple[str, ...]] = ("workflow_runs",)
#: 载荷必须给出的身份字段（与 T065 ``IDENTITY_STATE_FIELDS`` 一致）。
CHECKPOINT_IDENTITY_FIELDS: Final[tuple[str, ...]] = (
    "workflow_id",
    "request_id",
    "submission_id",
)


class WorkflowCheckpointError(RuntimeError):
    """检查点边界错误；保留脱敏错误码，按不可重试的业务问题处理。"""

    error_code: str = WORKFLOW_CHECKPOINT_INVALID_INPUT

    def __init__(self, detail: str, *, error_code: str | None = None) -> None:
        if error_code is not None:
            self.error_code = error_code
        super().__init__(f"{self.error_code}：{detail}")
        self.detail = detail


class WorkflowCheckpointNotFoundError(WorkflowCheckpointError):
    """目标工作流没有检查点记录。"""

    error_code = WORKFLOW_CHECKPOINT_NOT_FOUND


class WorkflowCheckpointStoreNotReadyError(WorkflowCheckpointError):
    """检查点存储未就绪。"""

    error_code = WORKFLOW_CHECKPOINT_STORE_NOT_READY


class WorkflowCheckpointStateError(WorkflowCheckpointError):
    """状态快照缺失或无法编码/解码。"""

    error_code = WORKFLOW_CHECKPOINT_STATE_INVALID


class WorkflowCheckpointOwnershipError(WorkflowCheckpointError):
    """检查点身份与既有运行记录不一致。"""

    error_code = WORKFLOW_CHECKPOINT_OWNERSHIP_MISMATCH


class WorkflowCheckpointCompletionError(WorkflowCheckpointError):
    """运行事实不允许宣称完成。"""

    error_code = WORKFLOW_CHECKPOINT_COMPLETION_NOT_ALLOWED


def _required_text(value: object, label: str) -> str:
    """校验必填文本；空白或非文本显式失败，不生成占位值。"""

    if not isinstance(value, str) or not value.strip():
        raise WorkflowCheckpointError(f"检查点缺少 {label}，拒绝写入不完整记录。")
    return value.strip()


def _required_uuid(value: object, label: str) -> UUID:
    """校验必需 UUID；非 UUID 显式失败，不静默丢弃身份。"""

    if isinstance(value, UUID):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return UUID(value.strip())
        except ValueError as error:
            raise WorkflowCheckpointError(f"{label} 必须是 UUID，收到 {value!r}。") from error
    raise WorkflowCheckpointError(f"检查点缺少 {label}，拒绝写入不完整记录。")


def _optional_uuid(value: object, label: str) -> UUID | None:
    """校验可选 UUID；值为空时返回 ``None``。"""

    if value is None:
        return None
    return _required_uuid(value, label)


def _as_status(value: object) -> WorkflowStatus | None:
    """解析工作流状态；未知取值显式失败，不猜测状态。"""

    if value is None:
        return None
    if isinstance(value, WorkflowStatus):
        return value
    if isinstance(value, str):
        try:
            return WorkflowStatus(value)
        except ValueError as error:
            raise WorkflowCheckpointError(
                f"状态 {value!r} 不是合法的工作流状态。"
            ) from error
    raise WorkflowCheckpointError(f"状态必须是 WorkflowStatus 或其取值，收到 {value!r}。")


def _retry_count(value: object) -> int:
    """校验重评计数：拒绝布尔值与负数被当成计数。"""

    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WorkflowCheckpointError("retry_count 必须是 >= 0 的整数。")
    return value


def _as_agent_error(error: object) -> AgentError:
    """把失败输入收敛为 T065 的 ``AgentError``；只记录调用方给出的脱敏说明。"""

    if isinstance(error, AgentError):
        return error
    if isinstance(error, str):
        return AgentError(
            error_code=WORKFLOW_CHECKPOINT_FAILED,
            message=_required_text(error, "error"),
        )
    if isinstance(error, BaseException):
        code = getattr(error, "error_code", None)
        return AgentError(
            error_code=(
                code if isinstance(code, str) and code.strip() else WORKFLOW_CHECKPOINT_FAILED
            ),
            message=str(error).strip() or type(error).__name__,
            retryable=bool(getattr(error, "retryable", False)),
        )
    raise WorkflowCheckpointError("失败标记需要脱敏说明字符串、AgentError 或异常对象。")


def _encode_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """校验并编码业务状态快照；T065 校验失败统一收敛为检查点状态错误。"""

    try:
        return workflow_state_to_checkpoint_payload(state)
    except WorkflowStateError as error:
        raise WorkflowCheckpointStateError(str(error)) from error


def pending_review_answer_ids(state: Mapping[str, Any]) -> tuple[str, ...]:
    """返回状态中仍待人工复核的答案标识（消费 T071 ``ACCEPTED_REVIEW_STATES``）。"""

    raw_decisions = state.get("confidence_decisions") or {}
    if not isinstance(raw_decisions, Mapping):
        return ()
    pending = [
        str(answer_id)
        for answer_id, decision in raw_decisions.items()
        if isinstance(decision, ConfidenceDecisionDTO)
        and decision.review_status not in ACCEPTED_REVIEW_STATES
    ]
    return tuple(sorted(pending))


def checkpoint_thread_id(run: WorkflowRun) -> str | None:
    """返回载荷中记录的 LangGraph 线程标识；未记录时返回 ``None``。"""

    checkpoint = run.checkpoint
    if not isinstance(checkpoint, Mapping):
        return None
    value = checkpoint.get(CHECKPOINT_THREAD_ID_KEY)
    return value if isinstance(value, str) and value.strip() else None


class WorkflowCheckpointStore:
    """基于 T064 ``WorkflowRun`` 的检查点存储（T073）。

    :param session_factory: 会话工厂；自建会话在使用后关闭。
    :param session: 借入会话（请求作用域或测试）；本模块不关闭借入会话，但会提交自身写入。
    :param clock: 时间来源，便于测试固定 ``saved_at``。
    """

    #: 检查点存放在 ``workflow_runs``，可跨进程重启查询。
    durable: bool = True

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session] | None = None,
        session: Session | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if session is not None and session_factory is not None:
            raise WorkflowCheckpointError("检查点存储只能注入会话或会话工厂之一。")
        self._session = session
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------------ 会话

    @contextmanager
    def _use_session(self) -> Iterator[Session]:
        """借入会话直接使用；否则自建并保证关闭。未注入任何会话源时显式报未就绪。"""

        if self._session is not None:
            yield self._session
            return
        factory = self._session_factory
        if factory is None:
            raise WorkflowCheckpointStoreNotReadyError(
                "检查点存储未就绪：未注入数据库会话或会话工厂。"
            )
        created = factory()
        try:
            yield created
        finally:
            created.close()

    def ensure_ready(self) -> None:
        """最小就绪判断；未迁移或不可连接时抛可诊断的未就绪错误。"""

        try:
            with self._use_session() as session:
                for table in REQUIRED_TABLES:
                    session.execute(text(f"SELECT 1 FROM {table} LIMIT 1"))
        except SQLAlchemyError as error:
            raise WorkflowCheckpointStoreNotReadyError(
                "检查点存储未就绪："
                f"{type(error).__name__}；请确认数据库可连接且迁移已执行到 head。"
            ) from None

    def _find(self, session: Session, workflow_id: str) -> WorkflowRun | None:
        """按 ``workflow_id`` 定位运行记录。"""

        return session.scalars(
            select(WorkflowRun).where(WorkflowRun.workflow_id == workflow_id)
        ).one_or_none()

    def _require(self, session: Session, workflow_id: str) -> WorkflowRun:
        """按 ``workflow_id`` 取运行记录；不存在即显式失败。"""

        row = self._find(session, workflow_id)
        if row is None:
            raise WorkflowCheckpointNotFoundError(f"工作流 {workflow_id} 没有检查点记录。")
        return row

    # ------------------------------------------------------------ 保存与加载

    def save_checkpoint(
        self,
        workflow_id: str,
        state: Mapping[str, Any],
        current_node: str,
        pause_reason: str | None = None,
        *,
        thread_id: str | None = None,
    ) -> WorkflowRun:
        """写入或更新检查点，并返回已落库的运行记录。

        ``current_node`` 与显式的 ``pause_reason`` 会覆盖状态中的同名值：暂停事实由调用方断言，
        不能出现“给了暂停原因却仍是 Running”的自相矛盾记录。显式暂停原因在状态为 ``Running``
        或未判定时把状态提升为 ``Paused``，在 ``Failed``/``Completed`` 时显式拒绝。
        """

        run_id = _required_text(workflow_id, "workflow_id")
        node = _required_text(current_node, "current_node")
        if not isinstance(state, Mapping) or not state:
            raise WorkflowCheckpointError("检查点状态必须是非空映射。")

        normalized: dict[str, Any] = dict(state)
        stored_id = normalized.get("workflow_id")
        if stored_id is not None and _required_text(stored_id, "workflow_id") != run_id:
            raise WorkflowCheckpointOwnershipError(
                "状态中的 workflow_id 与目标工作流不一致，拒绝跨工作流写入检查点。"
            )
        normalized["workflow_id"] = run_id
        normalized["current_node"] = node
        if pause_reason is not None:
            reason = _required_text(pause_reason, "pause_reason")
            current_status = _as_status(normalized.get("status"))
            if current_status in (WorkflowStatus.FAILED, WorkflowStatus.COMPLETED):
                raise WorkflowCheckpointError(
                    f"状态为 {current_status.value} 的工作流不允许写暂停原因。"
                )
            normalized["pause_reason"] = reason
            if current_status is None or current_status is WorkflowStatus.RUNNING:
                normalized["status"] = WorkflowStatus.PAUSED

        status = _as_status(normalized.get("status"))
        request_id = _required_text(normalized.get("request_id"), "request_id")
        submission_id = _required_uuid(normalized.get("submission_id"), "submission_id")
        current_answer_id = _optional_uuid(
            normalized.get("current_answer_id"), "current_answer_id"
        )
        retry_count = _retry_count(normalized.get("retry_count"))
        stored_pause = normalized.get("pause_reason")
        if stored_pause is not None:
            stored_pause = _required_text(stored_pause, "pause_reason")

        checkpoint = _encode_state(normalized)
        checkpoint[CHECKPOINT_SAVED_AT_KEY] = self._clock().isoformat()
        if thread_id is not None:
            checkpoint[CHECKPOINT_THREAD_ID_KEY] = _required_text(thread_id, "thread_id")

        resolved_status = status if status is not None else WorkflowStatus.RUNNING
        # H02：只有状态自身声明可恢复、且确实处于暂停且有暂停原因时才为真。
        resumable = (
            resolved_status is WorkflowStatus.PAUSED
            and stored_pause is not None
            and normalized.get("resumable") is True
        )

        with self._use_session() as session:
            try:
                row = self._find(session, run_id)
                if row is None:
                    row = WorkflowRun(
                        workflow_id=run_id,
                        request_id=request_id,
                        submission_id=submission_id,
                    )
                    session.add(row)
                elif row.submission_id != submission_id or row.request_id != request_id:
                    raise WorkflowCheckpointOwnershipError(
                        "检查点已属于其它答卷或其它请求，拒绝覆盖既有运行记录。"
                    )
                row.request_id = request_id
                row.submission_id = submission_id
                row.current_node = node
                row.current_answer_id = current_answer_id
                row.status = resolved_status
                row.checkpoint = checkpoint
                row.pause_reason = stored_pause
                row.retry_count = retry_count
                row.resumable = resumable
                session.commit()
            except Exception:
                session.rollback()
                raise
            session.refresh(row)
            return row

    def load_checkpoint(self, workflow_id: str) -> WorkflowRun | None:
        """按 ``workflow_id`` 读取运行记录；不存在返回 ``None``（不补占位行）。"""

        run_id = _required_text(workflow_id, "workflow_id")
        with self._use_session() as session:
            return self._find(session, run_id)

    def list_resumable(self, workflow_id: str | None = None) -> list[WorkflowRun]:
        """列出可恢复的运行记录；给出 ``workflow_id`` 时只筛该工作流。"""

        criteria: list[Any] = [WorkflowRun.resumable.is_(True)]
        if workflow_id is not None:
            criteria.append(WorkflowRun.workflow_id == _required_text(workflow_id, "workflow_id"))
        with self._use_session() as session:
            rows = session.scalars(
                select(WorkflowRun)
                .where(*criteria)
                .order_by(WorkflowRun.created_at, WorkflowRun.workflow_id)
            ).all()
            return list(rows)

    # ------------------------------------------------------------ 状态还原

    def restore_state(self, run: WorkflowRun | str) -> GradingWorkflowState:
        """由检查点载荷还原业务状态；缺记录、缺快照或载荷非法都显式失败。"""

        row = run if isinstance(run, WorkflowRun) else self.load_checkpoint(run)
        if row is None:
            raise WorkflowCheckpointNotFoundError("检查点记录不存在，无法还原工作流状态。")
        checkpoint = row.checkpoint
        if not isinstance(checkpoint, Mapping):
            raise WorkflowCheckpointStateError(
                f"工作流 {row.workflow_id} 没有状态快照，无法还原工作流状态。"
            )
        try:
            return workflow_state_from_checkpoint_payload(checkpoint)
        except WorkflowStateError as error:
            raise WorkflowCheckpointStateError(str(error)) from error

    def _usable_state(self, row: WorkflowRun) -> GradingWorkflowState | None:
        """返回可用的状态快照；快照缺失或非法时返回 ``None``（标记类操作据此拒绝）。"""

        checkpoint = row.checkpoint
        if not isinstance(checkpoint, Mapping):
            return None
        try:
            return workflow_state_from_checkpoint_payload(checkpoint)
        except WorkflowStateError:
            return None

    def _final_exam_result_id(self, session: Session, submission_id: UUID) -> UUID | None:
        """按答卷查找已最终确认的整卷结果行，作为运行记录的最终结果引用。"""

        return session.scalars(
            select(ExamResult.id).where(
                ExamResult.submission_id == submission_id,
                ExamResult.is_final.is_(True),
            )
        ).one_or_none()

    def _write_state(self, row: WorkflowRun, state: Mapping[str, Any]) -> dict[str, Any]:
        """重新编码状态并保留原载荷中的线程标识与落库时间。"""

        checkpoint = _encode_state(state)
        checkpoint[CHECKPOINT_SAVED_AT_KEY] = self._clock().isoformat()
        stored_thread = checkpoint_thread_id(row)
        if stored_thread is not None:
            checkpoint[CHECKPOINT_THREAD_ID_KEY] = stored_thread
        return checkpoint

    # ------------------------------------------------------------ 终态标记

    def mark_completed(
        self,
        workflow_id: str,
        *,
        exam_result_id: str | None = None,
    ) -> WorkflowRun:
        """把运行标记为 ``Completed`` 并给出最终结果引用。

        标记前必须核验：状态快照可用、没有保留失败事实、没有仍待人工复核的题目
        （T065 明确“存在待复核题目时不得宣称流程完成”）。未显式给出 ``exam_result_id`` 时，
        按答卷查找已最终确认的整卷结果行自动关联。
        """

        run_id = _required_text(workflow_id, "workflow_id")
        with self._use_session() as session:
            try:
                row = self._require(session, run_id)
                state = self._usable_state(row)
                if state is None:
                    raise WorkflowCheckpointStateError(
                        f"工作流 {run_id} 没有可用的状态快照，拒绝宣称完成。"
                    )
                pending = pending_review_answer_ids(state)
                if state.get("error") is not None:
                    raise WorkflowCheckpointCompletionError(
                        "状态中保留了失败事实，拒绝标记完成。"
                    )
                if pending or state.get("review_status") == ReviewStatus.PENDING_REVIEW:
                    raise WorkflowCheckpointCompletionError(
                        "仍存在待人工复核的题目，拒绝标记完成；该运行应保持 Paused。"
                    )
                updated: dict[str, Any] = dict(state)
                updated.update(
                    {
                        "status": WorkflowStatus.COMPLETED,
                        "pause_reason": None,
                        "resumable": False,
                        "error": None,
                    }
                )
                row.checkpoint = self._write_state(row, updated)
                row.status = WorkflowStatus.COMPLETED
                row.pause_reason = None
                row.resumable = False
                row.exam_result_id = (
                    _required_uuid(exam_result_id, "exam_result_id")
                    if exam_result_id is not None
                    else self._final_exam_result_id(session, row.submission_id)
                )
                session.commit()
            except Exception:
                session.rollback()
                raise
            session.refresh(row)
            return row

    def mark_failed(self, workflow_id: str, error: str | AgentError | BaseException) -> WorkflowRun:
        """把运行标记为 ``Failed``，并把脱敏失败事实写入状态快照。

        与 T072 ``_failure`` 口径一致：``status=Failed``、写入 ``error``、``pause_reason=None``、
        ``resumable=False``，并清空 ``review_status``（失败不得与“待复核”并存）。已形成的整卷结果
        与逐题结果保持不变，不因失败被丢弃或改写。
        """

        run_id = _required_text(workflow_id, "workflow_id")
        failure = _as_agent_error(error)
        with self._use_session() as session:
            try:
                row = self._require(session, run_id)
                state = self._usable_state(row)
                if state is None:
                    raise WorkflowCheckpointStateError(
                        f"工作流 {run_id} 没有可用的状态快照，无法记录失败状态。"
                    )
                updated: dict[str, Any] = dict(state)
                updated.update(
                    {
                        "status": WorkflowStatus.FAILED,
                        "error": failure,
                        "pause_reason": None,
                        "resumable": False,
                        "review_status": None,
                    }
                )
                row.checkpoint = self._write_state(row, updated)
                row.status = WorkflowStatus.FAILED
                row.pause_reason = None
                row.resumable = False
                session.commit()
            except Exception:
                session.rollback()
                raise
            session.refresh(row)
            return row


__all__ = [
    "CHECKPOINT_IDENTITY_FIELDS",
    "CHECKPOINT_SAVED_AT_KEY",
    "CHECKPOINT_THREAD_ID_KEY",
    "REQUIRED_TABLES",
    "WORKFLOW_CHECKPOINT_COMPLETION_NOT_ALLOWED",
    "WORKFLOW_CHECKPOINT_FAILED",
    "WORKFLOW_CHECKPOINT_INVALID_INPUT",
    "WORKFLOW_CHECKPOINT_NOT_FOUND",
    "WORKFLOW_CHECKPOINT_OWNERSHIP_MISMATCH",
    "WORKFLOW_CHECKPOINT_STATE_INVALID",
    "WORKFLOW_CHECKPOINT_STORE_NOT_READY",
    "WorkflowCheckpointCompletionError",
    "WorkflowCheckpointError",
    "WorkflowCheckpointNotFoundError",
    "WorkflowCheckpointOwnershipError",
    "WorkflowCheckpointStateError",
    "WorkflowCheckpointStore",
    "WorkflowCheckpointStoreNotReadyError",
    "checkpoint_thread_id",
    "pending_review_answer_ids",
]
