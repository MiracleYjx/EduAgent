"""LangGraph 阅卷工作流共用状态与业务状态快照载荷（T065）。

契约依据：``.specify/contracts/agent-workflow.md`` 的 Grading Workflow States 与 Required State、
``.specify/plan.md`` §5、``backend/app/models/workflow_run.py``（T064 的 ``WorkflowRun`` 列）。

本模块只定义状态与编解码边界，**不**实现 Agent 逻辑、LangGraph 节点/条件边（T072）与检查点
持久化（T073）。产物是"版本化业务状态快照载荷"：它承载业务状态，不承载 LangGraph 的通道版本、
thread/checkpoint 标识、待写入任务或执行记录；完整恢复由 T073 接入 Checkpointer 时处理。

关键约定：

- **TypedDict 通道**：``GradingWorkflowState`` 使用 ``total=False``，LangGraph 会为每个键建立
  通道，节点返回局部更新即可。缺键表示"该通道尚未写入"，显式 ``None`` 表示"该阶段已判定为
  无值"，两者含义不同，编解码必须保持这一区别。
- **完整快照校验边界**：``WorkflowStateSnapshot`` 在编码前与解码后各校验一次。完整快照必须
  有非空 ``workflow_id``/``request_id``/``submission_id``；阶段性字段允许合法缺省；未知字段、
  非法枚举、非有限或越界置信度、负 ``retry_count``、错误字段类型与未知 ``kind``/``version``
  一律显式失败。"能编码"不等于"状态合法"。
- **多题集合**：``grading_results`` 与 ``confidence_decisions`` 以 ``answer_id`` 为键，顺序节点
  完整回写集合、重评按答案替换，**不得**直接追加而造成重复计分；``final_results`` 是逐题最终
  结果集合，必须与 ``exam_result.items`` 同源。
- **逐题槽位清理规则（O01）**：切换题目或重新评分时必须清空 ``ANSWER_SLOT_FIELDS`` 中的槽位
  （``query``/``retrieved_context``/``grading_result``/``confidence_decision``/``error`` 等），
  否则局部更新会沿用上一题的旧值；``SUBMISSION_COLLECTION_FIELDS`` 属于整卷级事实，必须保留。
- **状态分离（O02）**：流程状态复用 ``WorkflowStatus``，复核状态复用 ``ReviewStatus``，两者与
  Agent Trace 状态（``backend.app.ai.agents.state.AgentStatus``）分离。待复核既不是失败也不是
  完成；教师的人工结论可以覆盖自动决策，但 Agent 不得声明人工结论。
- **载荷版本**：``kind="langgraph-grading-state-payload"``、``version="1"``，与 M3（T056）的
  ``background-task-checkpoint`` 显式区分；编解码不推断 ``resumable``，也不自动置真。
- **可序列化**：状态只承载文本、数值、枚举、既有 Pydantic DTO 与递归 JSON 值，禁止 Session、
  Provider、ORM 实体或其它不可 JSON 序列化对象。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Any, ClassVar, Final, TypedDict, cast, get_type_hints

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from backend.app.ai.agents.state import AgentError, RetrievedContextItem
from backend.app.domain.enums import (
    QuestionType,
    ReviewStatus,
    ValidationStatus,
    WorkflowStatus,
)
from backend.app.schemas.ai import ConfidenceScore, GradingResult, NonEmptyText
from backend.app.schemas.grading import (
    ConfidenceDecisionDTO,
    DiagnosisReportDTO,
    DiagnosisStatus,
    ExamResultDTO,
    QuestionResultDTO,
    SubmissionContext,
)

#: 状态缺失、类型非法或状态之间不自洽。
WORKFLOW_STATE_INVALID: Final[str] = "WORKFLOW_STATE_INVALID"
#: 状态中含有无法序列化为严格 JSON 的值。
WORKFLOW_STATE_NOT_SERIALIZABLE: Final[str] = "WORKFLOW_STATE_NOT_SERIALIZABLE"
#: 检查点载荷的 kind 不属于 LangGraph 业务状态载荷。
WORKFLOW_STATE_PAYLOAD_KIND_MISMATCH: Final[str] = "WORKFLOW_STATE_PAYLOAD_KIND_MISMATCH"
#: 检查点载荷版本不受支持。
WORKFLOW_STATE_PAYLOAD_VERSION_UNSUPPORTED: Final[str] = (
    "WORKFLOW_STATE_PAYLOAD_VERSION_UNSUPPORTED"
)

#: 业务状态快照载荷的 kind；与 M3 的后台任务检查点显式区分。
WORKFLOW_STATE_PAYLOAD_KIND: Final[str] = "langgraph-grading-state-payload"
#: 业务状态快照载荷版本；字段或语义变化时必须递增。
WORKFLOW_STATE_PAYLOAD_VERSION: Final[str] = "1"
#: M3（T056）后台任务检查点的 kind；本模块必须拒绝该载荷。
LEGACY_BACKGROUND_TASK_CHECKPOINT_KIND: Final[str] = "background-task-checkpoint"
#: 载荷中承载业务状态的键名。
CHECKPOINT_STATE_KEY: Final[str] = "state"

#: 外部契约（`.specify/contracts/agent-workflow.md` 的 Required State）要求的字段。
#: 测试会从契约文件逐项解析并对照本常量，避免只做自身比对。
CONTRACT_STATE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "workflow_id",
        "submission_id",
        "current_answer_id",
        "question_type",
        "retrieved_context_ids",
        "grading_result",
        "validation_status",
        "confidence",
        "review_status",
        "final_results",
        "diagnosis",
        "error",
    }
)
#: T065 任务明文要求的字段集合：契约 Required State 的超集。
#: 契约文件的代码块未列出 `query`（主观题检索查询文本）与 `retrieved_context`（检索片段明细），
#: 但任务与 plan.md §5.3 要求状态同时承载二者；本常量显式记录该差异，且不修改契约文件本身。
TASK_MANDATED_STATE_FIELDS: Final[frozenset[str]] = CONTRACT_STATE_FIELDS | {
    "query",
    "retrieved_context",
}
#: 完整快照必须具备的非空身份字段。
IDENTITY_STATE_FIELDS: Final[frozenset[str]] = frozenset(
    {"workflow_id", "request_id", "submission_id"}
)
#: 运行控制字段；与 T064 ``WorkflowRun`` 的列一一对应。
RUN_CONTROL_STATE_FIELDS: Final[frozenset[str]] = frozenset(
    {"status", "current_node", "retry_count", "pause_reason", "resumable"}
)
#: 逐题槽位：换题或重新评分前必须清空，避免沿用上一题的值。
ANSWER_SLOT_FIELDS: Final[frozenset[str]] = frozenset(
    {
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
)
#: 整卷级集合：换题时不得清空，只在有新的汇总事实时整体替换。
SUBMISSION_COLLECTION_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "submission_context",
        "grading_results",
        "confidence_decisions",
        "final_results",
        "exam_result",
        "diagnosis",
    }
)

#: 严格整数：拒绝字符串与布尔值被隐式转换为计数。
StrictCount = Annotated[int, Field(strict=True, ge=0)]
#: 严格布尔：拒绝 ``"true"``/``1`` 之类的隐式真值转换。
StrictFlag = Annotated[bool, Field(strict=True)]


class GradingWorkflowState(TypedDict, total=False):
    """LangGraph 阅卷工作流共用状态。

    所有键都是可选通道（``total=False``）：节点按需增量写回，缺键表示尚未到达该阶段。
    T072 若需要累积通道，可在本模块用 ``Annotated[..., reducer]`` 扩展，不得改变字段名与
    载荷中的 JSON 形状；新增字段必须同时递增 ``WORKFLOW_STATE_PAYLOAD_VERSION``。
    """

    # --- 身份与运行控制（对应 T064 WorkflowRun 列） ---
    workflow_id: str
    request_id: str
    submission_id: str
    status: WorkflowStatus | None
    current_node: str | None
    current_answer_id: str | None
    current_answer_order: int | None
    retry_count: int
    pause_reason: str | None
    resumable: bool | None

    # --- 答卷与当前题 ---
    submission_context: SubmissionContext | None
    question_type: QuestionType | None

    # --- 检索证据（主观题） ---
    query: str | None
    retrieved_context_ids: list[str]
    retrieved_context: list[RetrievedContextItem]

    # --- 评分与校验 ---
    grading_result: GradingResult | None
    grading_results: dict[str, GradingResult]
    validation_status: ValidationStatus | None
    confidence: float | None
    confidence_decision: ConfidenceDecisionDTO | None
    confidence_decisions: dict[str, ConfidenceDecisionDTO]

    # --- 复核 ---
    review_status: ReviewStatus | None

    # --- 汇总与诊断 ---
    final_results: list[QuestionResultDTO]
    exam_result: ExamResultDTO | None
    diagnosis: DiagnosisReportDTO | None

    # --- 失败 ---
    error: AgentError | None


class WorkflowStateError(RuntimeError):
    """工作流状态与载荷编解码失败基类；按不可重试的业务问题处理。"""

    error_code: ClassVar[str] = WORKFLOW_STATE_INVALID

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.error_code}：{detail}")
        self.detail = detail


class WorkflowStateNotSerializableError(WorkflowStateError):
    """状态中含有无法序列化为严格 JSON 的值。"""

    error_code: ClassVar[str] = WORKFLOW_STATE_NOT_SERIALIZABLE


class WorkflowStatePayloadKindError(WorkflowStateError):
    """检查点载荷 kind 不是 LangGraph 业务状态载荷。"""

    error_code: ClassVar[str] = WORKFLOW_STATE_PAYLOAD_KIND_MISMATCH


class WorkflowStatePayloadVersionError(WorkflowStateError):
    """检查点载荷版本不受支持。"""

    error_code: ClassVar[str] = WORKFLOW_STATE_PAYLOAD_VERSION_UNSUPPORTED


class WorkflowStateSnapshot(BaseModel):
    """完整工作流快照的校验视图。

    与 ``GradingWorkflowState`` 的字段名必须一致（测试逐项核对），两者的分工不同：TypedDict
    给 LangGraph 与节点代码提供静态通道类型，本模型给编解码提供运行时校验。身份字段必填且
    非空；其余字段缺省合法，表示该阶段尚未到达。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    workflow_id: NonEmptyText = Field(description="工作流实例标识。")
    request_id: NonEmptyText = Field(description="贯穿请求的追踪标识。")
    submission_id: NonEmptyText = Field(description="本次运行处理的答卷。")

    status: WorkflowStatus | None = Field(default=None, description="工作流流程状态。")
    current_node: NonEmptyText | None = Field(default=None, description="当前节点名。")
    current_answer_id: NonEmptyText | None = Field(
        default=None, description="当前处理的答案标识。"
    )
    current_answer_order: int | None = Field(
        default=None, ge=1, description="当前题序，从 1 开始。"
    )
    retry_count: StrictCount | None = Field(default=None, description="重试次数。")
    pause_reason: NonEmptyText | None = Field(
        default=None, description="暂停原因；暂停状态必须给出。"
    )
    resumable: StrictFlag | None = Field(
        default=None, description="是否可恢复；缺省表示未判定，不得自动置真。"
    )

    submission_context: SubmissionContext | None = Field(
        default=None, description="权威题目集合与稳定题序。"
    )
    question_type: QuestionType | None = Field(default=None, description="当前题目题型。")
    query: NonEmptyText | None = Field(
        default=None, description="当前主观题的检索查询文本。"
    )
    retrieved_context_ids: list[NonEmptyText] = Field(
        default_factory=list, description="实际写入 Final Context 的片段标识，保持顺序。"
    )
    retrieved_context: list[RetrievedContextItem] = Field(
        default_factory=list, description="当前题的检索片段明细。"
    )

    grading_result: GradingResult | None = Field(
        default=None, description="当前题的单题评分结果。"
    )
    grading_results: dict[NonEmptyText, GradingResult] = Field(
        default_factory=dict, description="整卷单题结果集合，键为 answer_id。"
    )
    validation_status: ValidationStatus | None = Field(
        default=None, description="当前题结构化校验状态。"
    )
    confidence: ConfidenceScore | None = Field(
        default=None, description="当前题评分置信度，范围 [0, 1] 且必须为有限值。"
    )
    confidence_decision: ConfidenceDecisionDTO | None = Field(
        default=None, description="当前题的置信度决策快照。"
    )
    confidence_decisions: dict[NonEmptyText, ConfidenceDecisionDTO] = Field(
        default_factory=dict, description="整卷决策快照集合，键为 answer_id。"
    )
    review_status: ReviewStatus | None = Field(
        default=None, description="复核状态；人工结论优先于自动决策。"
    )

    final_results: list[QuestionResultDTO] = Field(
        default_factory=list, description="逐题最终结果集合，与 exam_result.items 同源。"
    )
    exam_result: ExamResultDTO | None = Field(
        default=None, description="整卷汇总结果。"
    )
    diagnosis: DiagnosisReportDTO | None = Field(
        default=None, description="学生诊断报告。"
    )

    error: AgentError | None = Field(default=None, description="失败时的脱敏错误。")

    @model_validator(mode="after")
    def _validate_run_control(self) -> WorkflowStateSnapshot:
        """流程状态必须与错误、暂停原因和可恢复标记自洽。"""

        if self.status is WorkflowStatus.FAILED:
            if self.error is None:
                raise ValueError("失败状态必须给出 error。")
            if self.resumable is True:
                raise ValueError("失败状态不得声明可恢复，resumable 不得自动置真。")
        if self.status is WorkflowStatus.PAUSED and self.pause_reason is None:
            raise ValueError("暂停状态必须给出 pause_reason，不得静默暂停。")
        if self.status is WorkflowStatus.COMPLETED and self.error is not None:
            raise ValueError("完成状态不得携带 error。")
        if self.review_status is ReviewStatus.PENDING_REVIEW and self.error is not None:
            raise ValueError("待人工复核不是失败，不得同时给出 error。")
        if (
            self.status is WorkflowStatus.COMPLETED
            and self.review_status is ReviewStatus.PENDING_REVIEW
        ):
            raise ValueError("存在待复核题目时不得宣称流程完成，状态应为 Paused。")
        return self

    @model_validator(mode="after")
    def _validate_collections(self) -> WorkflowStateSnapshot:
        """整卷集合必须与权威答案集合和整卷结果保持一致。"""

        expected_answer_ids = (
            {item.answer_id for item in self.submission_context.expected_answers}
            if self.submission_context is not None
            else None
        )
        if expected_answer_ids is not None:
            for name, keys in (
                ("grading_results", set(self.grading_results)),
                ("confidence_decisions", set(self.confidence_decisions)),
            ):
                unknown = keys - expected_answer_ids
                if unknown:
                    raise ValueError(
                        f"{name} 含不属于本答卷的答案键：{sorted(unknown)}。"
                    )

        if self.exam_result is not None:
            if self.exam_result.is_final and not self.final_results:
                raise ValueError("已最终确认的整卷结果必须给出 final_results。")
            if self.final_results and self.final_results != self.exam_result.items:
                raise ValueError(
                    "final_results 必须与 exam_result.items 同源，不得出现两份不同版本。"
                )
        return self

    @model_validator(mode="after")
    def _validate_diagnosis(self) -> WorkflowStateSnapshot:
        """就绪诊断只能来自已最终确认的整卷结果。"""

        if self.diagnosis is None:
            return self
        if self.diagnosis.status is DiagnosisStatus.READY:
            if self.exam_result is None:
                raise ValueError("就绪诊断必须关联整卷结果。")
            if not self.exam_result.is_final:
                raise ValueError(
                    "非最终成绩不得配就绪诊断，诊断只能消费已接受或已复核的最终结果。"
                )
        return self


#: 状态字段的类型提示；用于按字段使用 Pydantic 编解码，避免手写字段表。
_STATE_FIELD_HINTS: Final[Mapping[str, Any]] = MappingProxyType(
    get_type_hints(GradingWorkflowState, include_extras=True)
)
#: 每个状态字段的编解码适配器（JSON 模式）。
_STATE_FIELD_ADAPTERS: Final[Mapping[str, TypeAdapter[Any]]] = MappingProxyType(
    {name: TypeAdapter(hint) for name, hint in _STATE_FIELD_HINTS.items()}
)


def _validate(payload: Any) -> WorkflowStateSnapshot:
    """按完整快照视图校验；未知字段、缺身份字段与非法取值都显式失败。"""

    if not isinstance(payload, Mapping):
        raise WorkflowStateError(
            f"工作流状态必须是映射，收到类型 {type(payload).__name__}。"
        )
    try:
        return WorkflowStateSnapshot.model_validate(dict(payload))
    except ValidationError as error:
        raise WorkflowStateError(f"工作流状态校验失败：{_describe(error)}") from error


def _describe(error: ValidationError) -> str:
    """把校验错误压缩为脱敏的字段级说明，不复制整个状态正文。"""

    details = "; ".join(
        f"{'.'.join(str(part) for part in item.get('loc', ())) or '<root>'}: {item.get('msg', '')}"
        for item in error.errors()[:5]
    )
    return details or "未知校验错误"


def _resolve_field(name: str) -> TypeAdapter[Any]:
    """解析字段适配器；未知字段显式失败，避免静默丢弃检查点数据。"""

    adapter = _STATE_FIELD_ADAPTERS.get(name)
    if adapter is None:
        raise WorkflowStateError(
            f"未知状态字段 {name!r}；新增字段必须递增 {WORKFLOW_STATE_PAYLOAD_VERSION!r} 版本。"
        )
    return adapter


def workflow_state_to_json(state: Mapping[str, Any]) -> dict[str, Any]:
    """把工作流状态编码为可严格 JSON 序列化的字典。

    编码前执行完整快照校验（"能编码"不等于"状态合法"），随后按字段做 JSON 模式序列化，并以
    ``allow_nan=False`` 兜底，保证产物不含 ``NaN``/``Infinity`` 等非法 JSON 值。只有状态中实际
    存在的键会写入结果，保持"缺键"与"显式 null"的区别。
    """

    snapshot = _validate(state)
    payload: dict[str, Any] = {}
    for name in state:
        adapter = _resolve_field(name)
        payload[name] = adapter.dump_python(getattr(snapshot, name), mode="json")
    try:
        json.dumps(dict(payload), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise WorkflowStateNotSerializableError(
            f"状态无法序列化为严格 JSON：{error}"
        ) from error
    return payload


def workflow_state_from_json(payload: Mapping[str, Any]) -> GradingWorkflowState:
    """由状态字典还原工作流状态；解码后同样执行 Schema 校验。

    只有载荷中出现的键才会还原，缺键保持"通道未写入"语义，不被补成一批 ``None``。
    """

    snapshot = _validate(payload)
    restored: dict[str, Any] = {}
    for name in payload:
        _resolve_field(name)
        restored[name] = getattr(snapshot, name)
    return cast(GradingWorkflowState, restored)


def workflow_state_to_checkpoint_payload(state: Mapping[str, Any]) -> dict[str, Any]:
    """构造版本化业务状态快照载荷，供 T073 写入 ``WorkflowRun.checkpoint``。

    载荷只承载业务状态；LangGraph 的通道版本、thread/checkpoint 标识、待写入任务与执行记录不在
    本模块职责内。
    """

    return {
        "kind": WORKFLOW_STATE_PAYLOAD_KIND,
        "version": WORKFLOW_STATE_PAYLOAD_VERSION,
        CHECKPOINT_STATE_KEY: workflow_state_to_json(state),
    }


def workflow_state_from_checkpoint_payload(
    checkpoint: Mapping[str, Any],
) -> GradingWorkflowState:
    """由检查点载荷还原状态；拒绝未知 kind/version 与 M3 后台任务检查点。"""

    if not isinstance(checkpoint, Mapping):
        raise WorkflowStateError(
            f"检查点载荷必须是映射，收到类型 {type(checkpoint).__name__}。"
        )
    kind = checkpoint.get("kind")
    if kind != WORKFLOW_STATE_PAYLOAD_KIND:
        raise WorkflowStatePayloadKindError(
            f"检查点 kind 必须为 {WORKFLOW_STATE_PAYLOAD_KIND!r}，收到 {kind!r}；"
            f"{LEGACY_BACKGROUND_TASK_CHECKPOINT_KIND!r} 是 M3 后台任务检查点，不是 LangGraph "
            "业务状态载荷。"
        )
    version = checkpoint.get("version")
    if version != WORKFLOW_STATE_PAYLOAD_VERSION:
        raise WorkflowStatePayloadVersionError(
            f"检查点版本必须为 {WORKFLOW_STATE_PAYLOAD_VERSION!r}，收到 {version!r}。"
        )
    raw_state = checkpoint.get(CHECKPOINT_STATE_KEY)
    if not isinstance(raw_state, Mapping):
        raise WorkflowStateError(
            f"检查点载荷缺少 {CHECKPOINT_STATE_KEY!r} 状态内容。"
        )
    return workflow_state_from_json(raw_state)


__all__ = [
    "ANSWER_SLOT_FIELDS",
    "CHECKPOINT_STATE_KEY",
    "CONTRACT_STATE_FIELDS",
    "IDENTITY_STATE_FIELDS",
    "LEGACY_BACKGROUND_TASK_CHECKPOINT_KIND",
    "RUN_CONTROL_STATE_FIELDS",
    "SUBMISSION_COLLECTION_FIELDS",
    "TASK_MANDATED_STATE_FIELDS",
    "WORKFLOW_STATE_INVALID",
    "WORKFLOW_STATE_NOT_SERIALIZABLE",
    "WORKFLOW_STATE_PAYLOAD_KIND",
    "WORKFLOW_STATE_PAYLOAD_KIND_MISMATCH",
    "WORKFLOW_STATE_PAYLOAD_VERSION",
    "WORKFLOW_STATE_PAYLOAD_VERSION_UNSUPPORTED",
    "GradingWorkflowState",
    "WorkflowStateError",
    "WorkflowStateNotSerializableError",
    "WorkflowStatePayloadKindError",
    "WorkflowStatePayloadVersionError",
    "WorkflowStateSnapshot",
    "workflow_state_from_checkpoint_payload",
    "workflow_state_from_json",
    "workflow_state_to_checkpoint_payload",
    "workflow_state_to_json",
]
