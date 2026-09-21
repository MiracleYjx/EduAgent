"""阅卷工作流检查点持久化（T073）。

契约依据：``.specify/plan.md`` §5（状态图与控制逻辑）、``.specify/data-model.md``
（WorkflowRun 状态与关键校验规则）、``backend/app/models/workflow_run.py``（T064 列定义）、
``backend/app/ai/workflows/state.py``（T065 业务状态快照载荷）、
``backend/app/ai/workflows/grading_handoff.py``（T071 复核状态契约）与 FR-036（人工复核可中断、
可恢复，恢复必须沿用原运行标识）。

本模块包含两件事，分别对应两类持久化事实，二者共存于 T064 ``WorkflowRun.checkpoint`` 这一
已有 JSON 列，互不覆盖：

1. :class:`WorkflowCheckpointStore`：保存 **业务状态快照**（T065 版本化载荷 + 运行控制事实）。
   它是可审计的业务事实（当前节点、暂停原因、重试次数、是否可恢复），**不是** LangGraph 的
   runtime checkpoint。
2. :class:`DatabaseCheckpointSaver`：实现当前安装版本（langgraph-checkpoint 4.1.1）的
   ``BaseCheckpointSaver`` 接口，把 **LangGraph 运行时检查点**（完整 checkpoint 含
   ``channel_values``/``channel_versions``、metadata、父配置、pending writes 与保留上限内的历史）
   写入同一行的 ``runtime`` 段。它可以直接注入 T072 ``GradingWorkflow(checkpointer=...)``，
   使跨进程/跨实例的暂停恢复成为可验证事实而不是声明。

职责与边界：

- **只做持久化**：本模块不执行图、不判定置信度、不写教师结论，也不实现 T076 的 API。
- **业务载荷仍走 T065 编解码**：``state`` 段一律经
  :func:`workflow_state_to_checkpoint_payload` 校验并编码；``runtime`` 段只承载 LangGraph
  恢复所需事实，二者互不解释对方的字段。
- **``resumable`` 不假声明（H02）**：只有“状态为 ``Paused`` + 已有暂停原因 + 状态自身声明可恢复
  + 持久 runtime checkpoint 已写入且能按同一 ``thread_id`` 从持久层读回”同时成立才记为真。
  只有业务快照、没有 runtime checkpoint 时一律为 ``False``。
- **序列化不使用 pickle**：runtime 段一律经 LangGraph 官方
  :class:`~langgraph.checkpoint.serde.jsonplus.JsonPlusSerializer`（``pickle_fallback=False``）
  的 ``dumps_typed``/``loads_typed`` 处理，并在编解码两侧显式拒绝 ``pickle`` 类型载荷。
- **失败与完成必须可核验**：``mark_completed`` 要求状态中没有失败事实、没有待人工复核题目；
  ``mark_failed`` 按 T072 ``_failure`` 的口径写入 ``status=Failed``、``error``、``resumable=False``，
  并清空 ``review_status``。两者都拒绝在缺少可用状态快照时伪造状态，并原样保留 ``runtime`` 段。
- **身份一致**：同一 ``workflow_id`` 只能绑定一个 ``submission_id``、一个 ``request_id`` 与一个
  LangGraph ``thread_id``；跨答卷、跨请求或跨线程的覆盖一律拒绝。
- **执行归属**：本模块只读写 T065 业务状态载荷（``kind`` 为 T065 载荷 kind）的运行行。同一张
  ``workflow_runs`` 表里的 M3 后台任务检查点（``background-task-checkpoint``）不属于本执行器：
  既不进入恢复列表与线程解析，也不允许被本模块覆盖；反之 M3 的查询也只读自己的 kind。
- **不为运行记录编造线程**：``thread_id`` 与 ``workflow_id`` 可以不同（T072 允许），因此
  saver 按“内存缓存 → ``workflow_id`` 精确匹配 → ``runtime``/业务载荷中的线程绑定”三层解析
  运行记录；解析不到时显式要求调用方先创建运行记录或 ``bind_thread()``，不静默写到别的行。

时间戳：``created_at``/``updated_at`` 由 T064 的时间戳混入提供；``state``/``runtime`` 段内的
``saved_at`` 记录各自最近一次的落库时间，供排障与 T076 展示。
"""

from __future__ import annotations

import base64
import copy
import random
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    SerializerProtocol,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.ai.agents.state import AgentError
from backend.app.ai.workflows.grading_handoff import ACCEPTED_REVIEW_STATES
from backend.app.ai.workflows.state import (
    WORKFLOW_STATE_PAYLOAD_KIND,
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
#: LangGraph 运行检查点载荷缺失、版本不受支持或自相矛盾。
WORKFLOW_CHECKPOINT_RUNTIME_INVALID: Final[str] = "WORKFLOW_CHECKPOINT_RUNTIME_INVALID"
#: LangGraph ``thread_id`` 尚未绑定任何运行记录。
WORKFLOW_CHECKPOINT_THREAD_UNBOUND: Final[str] = "WORKFLOW_CHECKPOINT_THREAD_UNBOUND"
#: 载荷要求 pickle 序列化（本模块显式拒绝）。
WORKFLOW_CHECKPOINT_SERDE_REJECTED: Final[str] = "WORKFLOW_CHECKPOINT_SERDE_REJECTED"
#: 调用方只给出脱敏说明时的默认失败错误码。
WORKFLOW_CHECKPOINT_FAILED: Final[str] = "WORKFLOW_CHECKPOINT_FAILED"

#: 载荷中记录 LangGraph 线程标识的附加键；不在 T065 业务状态快照字段集合内。
CHECKPOINT_THREAD_ID_KEY: Final[str] = "thread_id"
#: 载荷中记录本次落库时间的附加键。
CHECKPOINT_SAVED_AT_KEY: Final[str] = "saved_at"
#: 载荷中标识执行归属的键；M3 后台任务在该键上写 ``background-task-checkpoint``。
CHECKPOINT_KIND_KEY: Final[str] = "kind"
#: 本执行器（M4 LangGraph 工作流）的运行归属 kind，与 T065 业务状态载荷 kind 同源：
#: 能按该 kind 读出业务状态的行就属于本执行器。M3 后台任务检查点使用
#: ``grading_repository.CHECKPOINT_KIND``（``background-task-checkpoint``）。
#: 两类检查点共用 ``workflow_runs`` 表，因此两侧的读写都必须带 kind 过滤，不互相覆盖。
WORKFLOW_RUN_KIND: Final[str] = WORKFLOW_STATE_PAYLOAD_KIND
#: 就绪检查所需的表。
REQUIRED_TABLES: Final[tuple[str, ...]] = ("workflow_runs",)
#: 载荷必须给出的身份字段（与 T065 ``IDENTITY_STATE_FIELDS`` 一致）。
CHECKPOINT_IDENTITY_FIELDS: Final[tuple[str, ...]] = (
    "workflow_id",
    "request_id",
    "submission_id",
)

#: ``checkpoint`` 列中承载 LangGraph 运行检查点的段名。
RUNTIME_ENVELOPE_KEY: Final[str] = "runtime"
#: runtime 段的 kind 与版本；字段或语义变化时必须递增版本。
RUNTIME_ENVELOPE_KIND: Final[str] = "langgraph-runtime-checkpoint"
RUNTIME_ENVELOPE_VERSION: Final[str] = "1"
#: 每个线程/命名空间保留的 runtime 检查点数量上限（最旧优先丢弃）。
DEFAULT_RUNTIME_HISTORY_LIMIT: Final[int] = 10
#: 本模块显式拒绝的序列化类型（不使用任意对象反序列化）。
REJECTED_SERDE_TYPE: Final[str] = "pickle"


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


class WorkflowCheckpointRuntimeError(WorkflowCheckpointError):
    """LangGraph 运行检查点载荷非法或版本不受支持。"""

    error_code = WORKFLOW_CHECKPOINT_RUNTIME_INVALID


class WorkflowCheckpointThreadUnboundError(WorkflowCheckpointError):
    """``thread_id`` 尚未绑定运行记录。"""

    error_code = WORKFLOW_CHECKPOINT_THREAD_UNBOUND


class WorkflowCheckpointSerdeError(WorkflowCheckpointError):
    """载荷要求 pickle 序列化，本模块拒绝。"""

    error_code = WORKFLOW_CHECKPOINT_SERDE_REJECTED


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


# ---------------------------------------------------------------- runtime 载荷

def run_kind_criteria() -> Any:
    """返回“属于本执行器（M4 LangGraph 工作流）”的运行行过滤条件。

    ``workflow_runs`` 由 M3 后台任务与 M4 工作流共用：M3 写 ``background-task-checkpoint``，
    M4 写 T065 业务状态载荷 kind。任何按答卷、线程或全表扫描运行行的查询都必须带该条件，
    避免把另一个执行器的行当成自己的工作流。
    """

    return WorkflowRun.checkpoint[CHECKPOINT_KIND_KEY].as_string() == WORKFLOW_RUN_KIND


def _ensure_run_owned(row: WorkflowRun) -> None:
    """确认既有运行行属于本执行器；M3 后台任务或其它执行器的行一律拒绝写入。

    没有任何检查点内容的行（例如刚由其它入口创建、尚未写入业务状态的运行记录）按本执行器的
    行处理：真正写入时会由 T065 状态载荷补上归属 kind。
    """

    envelope = row.checkpoint
    stored_kind = (
        envelope.get(CHECKPOINT_KIND_KEY) if isinstance(envelope, Mapping) else None
    )
    if stored_kind is not None and stored_kind != WORKFLOW_RUN_KIND:
        raise WorkflowCheckpointOwnershipError(
            f"运行记录 {row.workflow_id} 的检查点 kind 为 {stored_kind!r}，"
            f"不属于本执行器（{WORKFLOW_RUN_KIND!r}），拒绝写入。"
        )


# ---------------------------------------------------------------- runtime 载荷


def _envelope(row: WorkflowRun) -> dict[str, Any]:
    """返回运行记录载荷的**深拷贝**；空值或非映射按空载荷处理。

    必须深拷贝：JSON 列不做就地变更跟踪，若只浅拷贝而修改嵌套结构（runtime 的命名空间、
    检查点条目与写入集合），SQLAlchemy 会认为列值未变化而不发 UPDATE，导致运行时检查点
    静默丢失。
    """

    checkpoint = row.checkpoint
    return copy.deepcopy(dict(checkpoint)) if isinstance(checkpoint, Mapping) else {}


def runtime_section(envelope: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """返回载荷中的 ``runtime`` 段；缺失或类型非法时返回 ``None``。"""

    if not isinstance(envelope, Mapping):
        return None
    runtime = envelope.get(RUNTIME_ENVELOPE_KEY)
    return runtime if isinstance(runtime, Mapping) else None


def runtime_thread_id(envelope: Mapping[str, Any] | None) -> str | None:
    """返回 runtime 段绑定的 LangGraph 线程标识。"""

    runtime = runtime_section(envelope)
    if runtime is None:
        return None
    value = runtime.get("thread_id")
    return value if isinstance(value, str) and value.strip() else None


def runtime_namespaces(envelope: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """返回 runtime 段中的命名空间映射（thread 下的 checkpoint_ns）。"""

    runtime = runtime_section(envelope)
    if runtime is None:
        return {}
    namespaces = runtime.get("namespaces")
    return namespaces if isinstance(namespaces, Mapping) else {}


def runtime_checkpoint_ids(
    envelope: Mapping[str, Any] | None,
    thread_id: str,
    *,
    checkpoint_ns: str = "",
) -> tuple[str, ...]:
    """返回该线程/命名空间下已持久化的 runtime 检查点标识（最旧在前）。"""

    if runtime_thread_id(envelope) != thread_id:
        return ()
    namespace = runtime_namespaces(envelope).get(checkpoint_ns)
    if not isinstance(namespace, Mapping):
        return ()
    entries = namespace.get("entries")
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        return ()
    return tuple(
        str(entry.get("checkpoint_id"))
        for entry in entries
        if isinstance(entry, Mapping) and entry.get("checkpoint_id")
    )


def runtime_has_checkpoint(
    envelope: Mapping[str, Any] | None,
    thread_id: str,
    *,
    checkpoint_ns: str = "",
) -> bool:
    """判断该载荷是否真的带有可恢复的 runtime 检查点。"""

    return bool(runtime_checkpoint_ids(envelope, thread_id, checkpoint_ns=checkpoint_ns))


def runtime_pending_write_count(
    envelope: Mapping[str, Any] | None,
    thread_id: str,
    *,
    checkpoint_ns: str = "",
) -> int:
    """返回最新 runtime 检查点上的 pending write 条数（供恢复与排障核对）。"""

    if runtime_thread_id(envelope) != thread_id:
        return 0
    namespace = runtime_namespaces(envelope).get(checkpoint_ns)
    if not isinstance(namespace, Mapping):
        return 0
    writes = namespace.get("writes")
    if not isinstance(writes, Mapping):
        return 0
    latest = namespace.get("latest_checkpoint_id")
    if not isinstance(latest, str):
        return 0
    stored = writes.get(latest)
    if not isinstance(stored, Sequence) or isinstance(stored, (str, bytes)):
        return 0
    return len(stored)


def checkpoint_thread_id(row: WorkflowRun) -> str | None:
    """返回载荷绑定的 LangGraph 线程标识：runtime 段优先，其次 T073 记录的附加键。"""

    envelope = _envelope(row)
    bound = runtime_thread_id(envelope)
    if bound is not None:
        return bound
    value = envelope.get(CHECKPOINT_THREAD_ID_KEY)
    return value if isinstance(value, str) and value.strip() else None


def _encode_blob(serde: SerializerProtocol, value: Any) -> dict[str, str]:
    """用 LangGraph 官方 serde 序列化一个值；拒绝 pickle 载荷。"""

    type_name, data = serde.dumps_typed(value)
    if type_name == REJECTED_SERDE_TYPE:
        raise WorkflowCheckpointSerdeError(
            "拒绝持久化 pickle 载荷：runtime 检查点必须使用 LangGraph msgpack 序列化。"
        )
    return {"type": str(type_name), "data": base64.b64encode(data).decode("ascii")}


def _decode_blob(serde: SerializerProtocol, blob: object) -> Any:
    """还原一个 serde 载荷；类型标记或内容非法时显式失败。"""

    if not isinstance(blob, Mapping):
        raise WorkflowCheckpointRuntimeError("runtime 检查点载荷缺少序列化类型标记。")
    type_name = blob.get("type")
    data = blob.get("data")
    if not isinstance(type_name, str) or not isinstance(data, str):
        raise WorkflowCheckpointRuntimeError("runtime 检查点载荷的类型标记或内容非法。")
    if type_name == REJECTED_SERDE_TYPE:
        raise WorkflowCheckpointSerdeError(
            "拒绝加载 pickle 载荷：runtime 检查点必须使用 LangGraph msgpack 序列化。"
        )
    try:
        raw = base64.b64decode(data.encode("ascii"), validate=True)
    except ValueError as error:
        raise WorkflowCheckpointRuntimeError("runtime 检查点载荷不是合法的 base64。") from error
    return serde.loads_typed((type_name, raw))


def _thread_config(config: RunnableConfig | None) -> tuple[str, str]:
    """解析运行配置中的 ``thread_id`` 与 ``checkpoint_ns``。"""

    configurable = (config or {}).get("configurable") or {}
    thread_id = configurable.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise WorkflowCheckpointRuntimeError(
            "LangGraph 运行配置缺少 thread_id，无法定位运行记录。"
        )
    checkpoint_ns = configurable.get("checkpoint_ns")
    return thread_id.strip(), (checkpoint_ns if isinstance(checkpoint_ns, str) else "")


class _SessionBoundStore:
    """会话注入与会话生命周期的共用实现（不导出）。"""

    def __init__(
        self,
        *,
        session: Session | None = None,
        session_factory: Callable[[], Session] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if session is not None and session_factory is not None:
            raise WorkflowCheckpointError("检查点存储只能注入会话或会话工厂之一。")
        self._session = session
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        #: ``thread_id`` → ``workflow_id`` 的实例级解析缓存。
        self._thread_bindings: dict[str, str] = {}

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

    def _resolve_thread_row(self, session: Session, thread_id: str) -> WorkflowRun | None:
        """按 LangGraph ``thread_id`` 解析运行记录。

        解析顺序：本实例缓存 → ``workflow_id`` 精确匹配 → 持久载荷中的线程绑定
        （``runtime`` 段优先，其次 T073 记录的附加键）。最后一层需要扫描运行记录表，因此
        一旦解析成功即写入实例缓存，供同一实例后续的逐超级步写入复用。
        """

        cached = self._thread_bindings
        bound = cached.get(thread_id)
        if isinstance(bound, str):
            row = self._find(session, bound)
            if row is not None:
                return row
            cached.pop(thread_id, None)
        row = self._find(session, thread_id)
        if row is not None:
            cached[thread_id] = row.workflow_id
            return row
        candidates = session.scalars(
            select(WorkflowRun)
            .where(run_kind_criteria())
            .order_by(WorkflowRun.updated_at.desc())
        ).all()
        for candidate in candidates:
            envelope = candidate.checkpoint
            if not isinstance(envelope, Mapping):
                continue
            if runtime_thread_id(envelope) == thread_id or (
                envelope.get(CHECKPOINT_THREAD_ID_KEY) == thread_id
            ):
                cached[thread_id] = candidate.workflow_id
                return candidate
        return None

    def _runtime_available(
        self,
        session: Session,
        *,
        thread_id: str | None,
        run_id: str,
    ) -> bool:
        """核验持久层确实能按同一 ``thread_id`` 读回该运行的 runtime 检查点。"""

        if thread_id is None:
            return False
        row = self._resolve_thread_row(session, thread_id)
        if row is None or row.workflow_id != run_id:
            return False
        return runtime_has_checkpoint(row.checkpoint, thread_id)


class WorkflowCheckpointStore(_SessionBoundStore):
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
        super().__init__(session=session, session_factory=session_factory, clock=clock)

    # ------------------------------------------------------------ 依赖解析

    def bind_thread(self, thread_id: str, workflow_id: str) -> WorkflowRun:
        """把 LangGraph ``thread_id`` 绑定到运行记录，供 saver 与恢复入口复用。"""

        run_id = _required_text(workflow_id, "workflow_id")
        with self._use_session() as session:
            row = self._require(session, run_id)
        self._thread_bindings[_required_text(thread_id, "thread_id")] = run_id
        return row

    def runtime_ready(self, workflow_id: str, thread_id: str) -> bool:
        """判断该运行是否真的带有可恢复的 runtime 检查点（恢复准入的唯一事实依据）。"""

        run_id = _required_text(workflow_id, "workflow_id")
        with self._use_session() as session:
            row = self._find(session, run_id)
            if row is None:
                return False
            return runtime_has_checkpoint(
                row.checkpoint, _required_text(thread_id, "thread_id")
            )

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
        """写入或更新业务状态快照，并返回已落库的运行记录。

        ``current_node`` 与显式的 ``pause_reason`` 会覆盖状态中的同名值：暂停事实由调用方断言，
        不能出现“给了暂停原因却仍是 Running”的自相矛盾记录。显式暂停原因在状态为 ``Running``
        或未判定时把状态提升为 ``Paused``，在 ``Failed``/``Completed`` 时显式拒绝。

        ``runtime`` 段（由 :class:`DatabaseCheckpointSaver` 维护）在本方法中原样保留；``resumable``
        只有在持久 runtime 检查点存在且能按同一 ``thread_id`` 读回时才可能为真。
        """

        with self._use_session() as session:
            try:
                row = self.save_checkpoint_within(
                    session,
                    workflow_id,
                    state,
                    current_node,
                    pause_reason,
                    thread_id=thread_id,
                )
                session.commit()
            except Exception:
                session.rollback()
                raise
            session.refresh(row)
            return row

    def save_checkpoint_within(
        self,
        session: Session,
        workflow_id: str,
        state: Mapping[str, Any],
        current_node: str,
        pause_reason: str | None = None,
        *,
        thread_id: str | None = None,
    ) -> WorkflowRun:
        """在调用方事务内写入业务状态快照（不提交、不关闭会话）。

        校验、身份绑定与 ``resumable`` 门禁与 :meth:`save_checkpoint` 完全一致；供复核服务把
        “复核记录 + 单题结果 + 当前整卷结果 + 工作流业务状态”写入同一事务。
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

        payload = _encode_state(normalized)
        payload[CHECKPOINT_SAVED_AT_KEY] = self._clock().isoformat()
        resolved_thread = (
            _required_text(thread_id, "thread_id") if thread_id is not None else None
        )
        if resolved_thread is not None:
            payload[CHECKPOINT_THREAD_ID_KEY] = resolved_thread

        resolved_status = status if status is not None else WorkflowStatus.RUNNING
        # H02：状态自身声明可恢复、确实处于暂停且有暂停原因，而且持久 runtime 检查点真实存在。
        declared_resumable = (
            resolved_status is WorkflowStatus.PAUSED
            and stored_pause is not None
            and normalized.get("resumable") is True
        )

        row = self._find(session, run_id)
        if row is None:
            row = WorkflowRun(
                workflow_id=run_id,
                request_id=request_id,
                submission_id=submission_id,
            )
            session.add(row)
        else:
            _ensure_run_owned(row)
            if row.submission_id != submission_id or row.request_id != request_id:
                raise WorkflowCheckpointOwnershipError(
                    "检查点已属于其它答卷或其它请求，拒绝覆盖既有运行记录。"
                )
        envelope = _envelope(row)
        bound_thread = runtime_thread_id(envelope)
        if (
            resolved_thread is not None
            and bound_thread is not None
            and bound_thread != resolved_thread
        ):
            raise WorkflowCheckpointRuntimeError(
                "runtime 检查点已绑定其它 thread_id，拒绝在同一运行记录上改写线程身份。"
            )
        envelope.update(payload)
        row.request_id = request_id
        row.submission_id = submission_id
        row.current_node = node
        row.current_answer_id = current_answer_id
        row.status = resolved_status
        row.checkpoint = envelope
        row.pause_reason = stored_pause
        row.retry_count = retry_count
        session.flush()
        row.resumable = declared_resumable and self._runtime_available(
            session,
            thread_id=resolved_thread,
            run_id=run_id,
        )
        return row

    def load_checkpoint(self, workflow_id: str) -> WorkflowRun | None:
        """按 ``workflow_id`` 读取运行记录；不存在返回 ``None``（不补占位行）。"""

        run_id = _required_text(workflow_id, "workflow_id")
        with self._use_session() as session:
            return self._find(session, run_id)

    def list_resumable(self, workflow_id: str | None = None) -> list[WorkflowRun]:
        """列出可恢复的运行记录；给出 ``workflow_id`` 时只筛该工作流。"""

        criteria: list[Any] = [WorkflowRun.resumable.is_(True), run_kind_criteria()]
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
        """重新编码业务状态并合并回原载荷：保留 ``runtime`` 段、线程标识与既有附加键。"""

        envelope = _envelope(row)
        envelope.update(_encode_state(state))
        envelope[CHECKPOINT_SAVED_AT_KEY] = self._clock().isoformat()
        stored_thread = checkpoint_thread_id(row)
        if stored_thread is not None:
            envelope[CHECKPOINT_THREAD_ID_KEY] = stored_thread
        return envelope

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
        ``resumable=False``，并清空 ``review_status``（失败不得与“待复核”并存）。已形成的整卷结果、
        逐题结果与 ``runtime`` 检查点保持不变，不因失败被丢弃或改写。
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


class DatabaseCheckpointSaver(_SessionBoundStore, BaseCheckpointSaver[str]):
    """把 LangGraph 运行检查点持久化到 T064 ``WorkflowRun`` 的 JSON 列（T073）。

    实现当前安装版本 langgraph-checkpoint 4.1.1 ``BaseCheckpointSaver`` 实际要求的接口：

    - ``put``/``aput``：保存完整 checkpoint（含 ``channel_values``/``channel_versions``）、metadata、
      父检查点标识与本次新增通道版本；返回带 ``checkpoint_id`` 的新配置。
    - ``put_writes``/``aput_writes``：按 ``(task_id, idx)`` 合并 pending writes，固定槽位
      （``__error__``/``__scheduled__``/``__interrupt__``/``__resume__``）覆盖、普通通道追加，
      与 ``InMemorySaver`` 的规则一致，保证中断写可被恢复读取。
    - ``get_tuple``/``aget_tuple``、``list``/``alist``：按 ``thread_id``（可带 ``checkpoint_ns``
      与 ``checkpoint_id``）读回检查点、metadata、父配置与 pending writes。
    - ``delete_thread``/``adelete_thread``：清除该线程的 runtime 段并同步下调 ``resumable``。
    - ``get_next_version``：与 ``InMemorySaver`` 相同的字符串版本格式。

    边界与限制（不假声明）：

    - 只保留每个线程/命名空间最近 ``history_limit`` 个检查点，最旧检查点及其写入会被丢弃；
      ``list`` 只能列出保留窗口内的历史。
    - 会话为同步 SQLAlchemy 会话（项目现有数据库栈），因此 ``a*`` 方法内联执行同一套数据库
      操作而不是线程池调度；这是为了不跨线程共享请求作用域会话。
    - ``thread_id`` 与 ``workflow_id`` 可以不同：按“实例缓存 → ``workflow_id`` 精确匹配 →
      持久载荷中的线程绑定”解析运行记录，解析不到时抛
      :class:`WorkflowCheckpointThreadUnboundError`，不把检查点写到别的运行记录上。

    :param session_factory: 会话工厂；自建会话在使用后关闭。
    :param session: 借入会话（测试或请求作用域）；本模块不关闭借入会话。
    :param clock: 时间来源，便于测试固定 ``saved_at``。
    :param history_limit: 每个线程/命名空间保留的检查点数量上限。
    :param serde: 序列化器；默认 ``JsonPlusSerializer(pickle_fallback=False)``。
    """

    def __init__(
        self,
        *,
        session: Session | None = None,
        session_factory: Callable[[], Session] | None = None,
        clock: Callable[[], datetime] | None = None,
        history_limit: int = DEFAULT_RUNTIME_HISTORY_LIMIT,
        serde: SerializerProtocol | None = None,
    ) -> None:
        _SessionBoundStore.__init__(
            self,
            session=session,
            session_factory=session_factory,
            clock=clock,
        )
        BaseCheckpointSaver.__init__(
            self,
            serde=serde if serde is not None else JsonPlusSerializer(pickle_fallback=False),
        )
        if (
            isinstance(history_limit, bool)
            or not isinstance(history_limit, int)
            or history_limit < 1
        ):
            raise WorkflowCheckpointError("runtime 检查点保留上限必须是 >= 1 的整数。")
        self._history_limit = history_limit

    @property
    def history_limit(self) -> int:
        """返回单个线程/命名空间的检查点保留上限。"""

        return self._history_limit

    # ------------------------------------------------------------ 绑定与摘要

    def bind_thread(self, thread_id: str, workflow_id: str) -> WorkflowRun:
        """显式绑定 LangGraph 线程与运行记录（首次写入前的声明式入口）。

        首次写入时若持久载荷中还没有线程绑定，调用方（T074/T076）应先用本方法声明映射；
        写入成功后线程绑定即持久化在 ``runtime`` 段，后续实例无需再次绑定。
        """

        run_id = _required_text(workflow_id, "workflow_id")
        thread = _required_text(thread_id, "thread_id")
        with self._use_session() as session:
            row = self._require(session, run_id)
            bound = runtime_thread_id(row.checkpoint)
            if bound is not None and bound != thread:
                raise WorkflowCheckpointRuntimeError(
                    f"运行记录 {run_id} 的 runtime 检查点已绑定线程 {bound!r}，拒绝改绑。"
                )
        self._thread_bindings[thread] = run_id
        return row

    def runtime_summary(self, thread_id: str) -> dict[str, Any] | None:
        """返回该线程的 runtime 检查点摘要；没有持久检查点时返回 ``None``。"""

        thread = _required_text(thread_id, "thread_id")
        with self._use_session() as session:
            row = self._resolve_thread_row(session, thread)
            if row is None:
                return None
            envelope = row.checkpoint
            if runtime_thread_id(envelope) != thread:
                return None
            namespaces = runtime_namespaces(envelope)
            namespace = namespaces.get("")
            latest: str | None = None
            checkpoint_ids: tuple[str, ...] = ()
            if isinstance(namespace, Mapping):
                entries = namespace.get("entries")
                if isinstance(entries, Sequence) and not isinstance(entries, (str, bytes)):
                    checkpoint_ids = tuple(
                        str(entry.get("checkpoint_id"))
                        for entry in entries
                        if isinstance(entry, Mapping) and entry.get("checkpoint_id")
                    )
                value = namespace.get("latest_checkpoint_id")
                latest = value if isinstance(value, str) else None
            return {
                "thread_id": thread,
                "workflow_id": row.workflow_id,
                "latest_checkpoint_id": latest,
                "checkpoint_ids": checkpoint_ids,
                "pending_write_count": runtime_pending_write_count(envelope, thread),
                "history_limit": self._history_limit,
            }

    # ------------------------------------------------------------ 存储原语

    def _namespace(self, envelope: dict[str, Any], thread_id: str, checkpoint_ns: str) -> dict[str, Any]:
        """取（必要时创建）运行记录载荷中的 runtime 命名空间容器。"""

        runtime = envelope.get(RUNTIME_ENVELOPE_KEY)
        if runtime is None:
            runtime = {}
            envelope[RUNTIME_ENVELOPE_KEY] = runtime
        if not isinstance(runtime, dict):
            raise WorkflowCheckpointRuntimeError("runtime 段必须是映射。")
        bound = runtime.get("thread_id")
        if bound is not None and bound != thread_id:
            raise WorkflowCheckpointRuntimeError(
                "runtime 检查点已绑定其它 thread_id，拒绝跨线程写入同一运行记录。"
            )
        runtime["kind"] = RUNTIME_ENVELOPE_KIND
        runtime["version"] = RUNTIME_ENVELOPE_VERSION
        runtime["thread_id"] = thread_id
        namespaces = runtime.setdefault("namespaces", {})
        if not isinstance(namespaces, dict):
            raise WorkflowCheckpointRuntimeError("runtime.namespaces 必须是映射。")
        namespace = namespaces.setdefault(checkpoint_ns, {})
        if not isinstance(namespace, dict):
            raise WorkflowCheckpointRuntimeError("runtime 命名空间必须是映射。")
        entries = namespace.setdefault("entries", [])
        if not isinstance(entries, list):
            raise WorkflowCheckpointRuntimeError("runtime 命名空间的检查点集合必须是列表。")
        writes = namespace.setdefault("writes", {})
        if not isinstance(writes, dict):
            raise WorkflowCheckpointRuntimeError("runtime 命名空间的写入集合必须是映射。")
        return namespace

    def _trim(self, namespace: dict[str, Any]) -> None:
        """按保留上限丢弃最旧检查点及其写入，避免单行 JSON 无界增长。"""

        entries = namespace.get("entries")
        writes = namespace.get("writes")
        if not isinstance(entries, list) or not isinstance(writes, dict):
            return
        while len(entries) > self._history_limit:
            dropped = entries.pop(0)
            if isinstance(dropped, Mapping):
                dropped_id = dropped.get("checkpoint_id")
                if isinstance(dropped_id, str):
                    writes.pop(dropped_id, None)

    def _stored_entries(self, namespace: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        """返回命名空间中按写入顺序排列的检查点条目。"""

        entries = namespace.get("entries")
        if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
            return []
        return [entry for entry in entries if isinstance(entry, Mapping)]

    def _checkpoint_tuple(
        self,
        row: WorkflowRun,
        *,
        thread_id: str,
        checkpoint_ns: str,
        config: RunnableConfig,
        entry: Mapping[str, Any],
        namespace: Mapping[str, Any],
    ) -> CheckpointTuple:
        """由存储条目重建 ``CheckpointTuple``；pending writes 一并还原。"""

        writes = namespace.get("writes")
        stored_writes: list[Any] = []
        if isinstance(writes, Mapping):
            candidate = writes.get(str(entry.get("checkpoint_id")))
            if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes)):
                stored_writes = [item for item in candidate if isinstance(item, Mapping)]
        parent_id = entry.get("parent_checkpoint_id")
        return CheckpointTuple(
            config=config,
            checkpoint=_decode_blob(self.serde, entry.get("checkpoint")),
            metadata=_decode_blob(self.serde, entry.get("metadata")),
            parent_config=(
                {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": parent_id,
                    }
                }
                if isinstance(parent_id, str) and parent_id
                else None
            ),
            pending_writes=[
                (
                    str(item.get("task_id")),
                    str(item.get("channel")),
                    _decode_blob(self.serde, item.get("value")),
                )
                for item in stored_writes
            ],
        )

    # ------------------------------------------------------------ 检索

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """读回检查点、metadata、父配置与 pending writes；不存在返回 ``None``。"""

        thread_id, checkpoint_ns = _thread_config(config)
        requested = get_checkpoint_id(config)
        with self._use_session() as session:
            row = self._resolve_thread_row(session, thread_id)
            if row is None:
                return None
            envelope = row.checkpoint
            if runtime_thread_id(envelope) != thread_id:
                return None
            namespace = runtime_namespaces(envelope).get(checkpoint_ns)
            if not isinstance(namespace, Mapping):
                return None
            entries = self._stored_entries(namespace)
            if requested is not None:
                entries = [item for item in entries if item.get("checkpoint_id") == requested]
            if not entries:
                return None
            return self._checkpoint_tuple(
                row,
                thread_id=thread_id,
                checkpoint_ns=checkpoint_ns,
                config=config,
                entry=entries[-1],
                namespace=namespace,
            )

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """异步版 ``get_tuple``：会话为同步 SQLAlchemy 会话，因此内联执行同一套数据库操作。"""

        return self.get_tuple(config)

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        """列出保留窗口内的检查点（最新在前，与 ``InMemorySaver`` 同序）。"""

        targets: list[tuple[WorkflowRun, RunnableConfig | None]] = []
        requested_thread: str | None = None
        requested_ns: str | None = None
        requested_id: str | None = None
        if config is not None:
            requested_thread, requested_ns = _thread_config(config)
            requested_id = get_checkpoint_id(config)
        before_id = get_checkpoint_id(before) if before is not None else None
        with self._use_session() as session:
            if requested_thread is not None:
                row = self._resolve_thread_row(session, requested_thread)
                if row is not None:
                    targets.append((row, config))
            else:
                targets.extend(
                    (row, None)
                    for row in session.scalars(
                        select(WorkflowRun)
                        .where(run_kind_criteria())
                        .order_by(WorkflowRun.updated_at.desc())
                    ).all()
                    if isinstance(row.checkpoint, Mapping)
                )
            collected: list[CheckpointTuple] = []
            for row, row_config in targets:
                envelope = row.checkpoint
                bound = runtime_thread_id(envelope)
                if bound is None:
                    continue
                if requested_thread is not None and bound != requested_thread:
                    continue
                namespaces = runtime_namespaces(envelope)
                for namespace_ns, namespace in namespaces.items():
                    if not isinstance(namespace, Mapping):
                        continue
                    if requested_ns is not None and namespace_ns != requested_ns:
                        continue
                    entries = sorted(
                        self._stored_entries(namespace),
                        key=lambda entry: str(entry.get("checkpoint_id")),
                        reverse=True,
                    )
                    for entry in entries:
                        checkpoint_id = str(entry.get("checkpoint_id"))
                        if requested_id is not None and checkpoint_id != requested_id:
                            continue
                        if before_id is not None and checkpoint_id >= before_id:
                            continue
                        metadata = _decode_blob(self.serde, entry.get("metadata"))
                        if filter and not all(
                            metadata.get(key) == value for key, value in filter.items()
                        ):
                            continue
                        if limit is not None and len(collected) >= limit:
                            break
                        collected.append(
                            self._checkpoint_tuple(
                                row,
                                thread_id=bound,
                                checkpoint_ns=namespace_ns,
                                config=(
                                    row_config
                                    if row_config is not None
                                    else {
                                        "configurable": {
                                            "thread_id": bound,
                                            "checkpoint_ns": namespace_ns,
                                            "checkpoint_id": checkpoint_id,
                                        }
                                    }
                                ),
                                entry=entry,
                                namespace=namespace,
                            )
                        )
            return iter(collected)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        """异步版 ``list``：逐条转发同步结果。"""

        for item in self.list(config, filter=filter, before=before, limit=limit):
            yield item

    # ------------------------------------------------------------ 写入

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """保存一个 runtime 检查点（含完整通道值与版本），返回带新 ``checkpoint_id`` 的配置。"""

        thread_id, checkpoint_ns = _thread_config(config)
        parent_id = get_checkpoint_id(config)
        checkpoint_id = checkpoint["id"]
        envelope_blob = _encode_blob(self.serde, checkpoint)
        metadata_blob = _encode_blob(self.serde, get_checkpoint_metadata(config, metadata))
        with self._use_session() as session:
            try:
                row = self._require_thread_row(session, thread_id)
                envelope = _envelope(row)
                namespace = self._namespace(envelope, thread_id, checkpoint_ns)
                entries = namespace.setdefault("entries", [])
                assert isinstance(entries, list)
                entries.append(
                    {
                        "checkpoint_id": checkpoint_id,
                        "parent_checkpoint_id": parent_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint": envelope_blob,
                        "metadata": metadata_blob,
                        "channel_versions": {
                            str(key): value
                            for key, value in dict(
                                checkpoint.get("channel_versions") or {}
                            ).items()
                        },
                        "new_versions": {
                            str(key): value for key, value in dict(new_versions).items()
                        },
                        "saved_at": self._clock().isoformat(),
                    }
                )
                namespace["latest_checkpoint_id"] = checkpoint_id
                self._trim(namespace)
                runtime = envelope.get(RUNTIME_ENVELOPE_KEY)
                if isinstance(runtime, dict):
                    runtime["saved_at"] = self._clock().isoformat()
                row.checkpoint = envelope
                session.commit()
            except Exception:
                session.rollback()
                raise
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """异步版 ``put``：内联执行同一套数据库写入。"""

        return self.put(config, checkpoint, metadata, new_versions)

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """合并 pending writes：固定槽位覆盖、普通通道追加（与 ``InMemorySaver`` 一致）。"""

        thread_id, checkpoint_ns = _thread_config(config)
        with self._use_session() as session:
            try:
                row = self._require_thread_row(session, thread_id)
                envelope = _envelope(row)
                namespace = self._namespace(envelope, thread_id, checkpoint_ns)
                entries = self._stored_entries(namespace)
                checkpoint_id = get_checkpoint_id(config)
                if not checkpoint_id:
                    latest = namespace.get("latest_checkpoint_id")
                    checkpoint_id = latest if isinstance(latest, str) else None
                if not checkpoint_id or not any(
                    str(entry.get("checkpoint_id")) == checkpoint_id for entry in entries
                ):
                    raise WorkflowCheckpointRuntimeError(
                        "尚无已保存的 runtime 检查点，拒绝写入 pending writes。"
                    )
                writes_map = namespace.setdefault("writes", {})
                assert isinstance(writes_map, dict)
                stored = writes_map.setdefault(checkpoint_id, [])
                if not isinstance(stored, list):
                    raise WorkflowCheckpointRuntimeError("runtime 写入集合必须是列表。")
                existing = {
                    (item.get("task_id"), item.get("idx"))
                    for item in stored
                    if isinstance(item, Mapping)
                }
                for index, (channel, value) in enumerate(writes):
                    inner_index = int(WRITES_IDX_MAP.get(channel, index))
                    key = (task_id, inner_index)
                    if inner_index >= 0:
                        if key in existing:
                            continue
                    else:
                        stored[:] = [
                            item
                            for item in stored
                            if not (
                                isinstance(item, Mapping)
                                and (item.get("task_id"), item.get("idx")) == key
                            )
                        ]
                    stored.append(
                        {
                            "task_id": task_id,
                            "idx": inner_index,
                            "channel": str(channel),
                            "task_path": task_path,
                            "value": _encode_blob(self.serde, value),
                        }
                    )
                    existing.add(key)
                runtime = envelope.get(RUNTIME_ENVELOPE_KEY)
                if isinstance(runtime, dict):
                    runtime["saved_at"] = self._clock().isoformat()
                row.checkpoint = envelope
                session.commit()
            except Exception:
                session.rollback()
                raise

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """异步版 ``put_writes``：内联执行同一套数据库写入。"""

        self.put_writes(config, writes, task_id, task_path)

    # ------------------------------------------------------------ 删除与版本

    def delete_thread(self, thread_id: str) -> None:
        """清除该线程的 runtime 段并下调 ``resumable``；业务状态快照保持不动。"""

        thread = _required_text(thread_id, "thread_id")
        with self._use_session() as session:
            try:
                row = self._resolve_thread_row(session, thread)
                if row is None:
                    return
                envelope = _envelope(row)
                envelope.pop(RUNTIME_ENVELOPE_KEY, None)
                if envelope.get(CHECKPOINT_THREAD_ID_KEY) == thread:
                    envelope.pop(CHECKPOINT_THREAD_ID_KEY, None)
                row.checkpoint = envelope or None
                row.resumable = False
                session.commit()
            except Exception:
                session.rollback()
                raise
        self._thread_bindings.pop(thread, None)

    async def adelete_thread(self, thread_id: str) -> None:
        """异步版 ``delete_thread``：内联执行同一套数据库操作。"""

        self.delete_thread(thread_id)

    def get_next_version(self, current: str | None, channel: None) -> str:
        """生成下一个通道版本；格式与 ``InMemorySaver`` 相同（``<计数>.<随机小数>``）。"""

        if current is None:
            current_version = 0
        elif isinstance(current, int):
            current_version = current
        else:
            current_version = int(str(current).split(".")[0])
        return f"{current_version + 1:032}.{random.random():016}"

    def _require_thread_row(self, session: Session, thread_id: str) -> WorkflowRun:
        """取线程绑定的运行记录；未绑定时显式失败，不写到别的运行记录上。"""

        row = self._resolve_thread_row(session, thread_id)
        if row is None:
            raise WorkflowCheckpointThreadUnboundError(
                f"thread_id {thread_id!r} 尚未绑定检查点运行记录："
                "请先创建运行记录（保存业务状态快照）或调用 bind_thread()。"
            )
        return row


__all__ = [
    "CHECKPOINT_IDENTITY_FIELDS",
    "CHECKPOINT_KIND_KEY",
    "CHECKPOINT_SAVED_AT_KEY",
    "CHECKPOINT_THREAD_ID_KEY",
    "DEFAULT_RUNTIME_HISTORY_LIMIT",
    "REQUIRED_TABLES",
    "RUNTIME_ENVELOPE_KEY",
    "RUNTIME_ENVELOPE_KIND",
    "RUNTIME_ENVELOPE_VERSION",
    "WORKFLOW_CHECKPOINT_COMPLETION_NOT_ALLOWED",
    "WORKFLOW_CHECKPOINT_FAILED",
    "WORKFLOW_CHECKPOINT_INVALID_INPUT",
    "WORKFLOW_CHECKPOINT_NOT_FOUND",
    "WORKFLOW_CHECKPOINT_OWNERSHIP_MISMATCH",
    "WORKFLOW_CHECKPOINT_RUNTIME_INVALID",
    "WORKFLOW_CHECKPOINT_SERDE_REJECTED",
    "WORKFLOW_CHECKPOINT_STATE_INVALID",
    "WORKFLOW_CHECKPOINT_STORE_NOT_READY",
    "WORKFLOW_CHECKPOINT_THREAD_UNBOUND",
    "WORKFLOW_RUN_KIND",
    "DatabaseCheckpointSaver",
    "WorkflowCheckpointCompletionError",
    "WorkflowCheckpointError",
    "WorkflowCheckpointNotFoundError",
    "WorkflowCheckpointOwnershipError",
    "WorkflowCheckpointRuntimeError",
    "WorkflowCheckpointSerdeError",
    "WorkflowCheckpointStateError",
    "WorkflowCheckpointStore",
    "WorkflowCheckpointStoreNotReadyError",
    "WorkflowCheckpointThreadUnboundError",
    "checkpoint_thread_id",
    "pending_review_answer_ids",
    "run_kind_criteria",
    "runtime_checkpoint_ids",
    "runtime_has_checkpoint",
    "runtime_namespaces",
    "runtime_pending_write_count",
    "runtime_section",
    "runtime_thread_id",
]
