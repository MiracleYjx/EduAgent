"""Agent 输入/输出共用状态类型（T065）。

契约依据：``.specify/contracts/agent-workflow.md`` 的 Agent Responsibilities 与 Required State、
``backend/app/models/agent_run.py``（T063 的 Trace 字段）、``backend/app/schemas/ai.py``（T010 的
候选题目与 ``GradingResult``）、``.specify/plan.md`` §5 与 §7。

设计约束：

- **只定义类型**：本模块不实现 Supervisor/Question/Grading/Reviewer Agent 的路由、评分或复核
  逻辑，也不实现 LangGraph 图与检查点持久化（T066-T070、T072、T073）。
- **复用既有类型**：候选题目复用 ``QuestionCandidate``，单题评分复用 ``GradingResult``，整卷结果
  复用 ``ExamResultDTO``，决策快照复用 ``ConfidenceDecisionDTO``，题型/校验/复核状态复用
  ``backend.app.domain.enums``；不复制第二套同名类型。
- **可序列化**：字段只承载文本、数值、枚举、既有 Pydantic DTO 与递归 JSON 值，禁止 Session、
  Provider、ORM 实体或其它不可 JSON 序列化的对象；自由参数字段由递归 ``JsonValue`` 约束。
- **状态自洽**：``failure`` 必须带脱敏错误且不得声明需要人工复核；``pending_review`` 必须显式
  ``requires_review=True``；低置信度待复核不等于执行失败；Agent 输出不得声明教师人工结论
  （``Reviewer`` 建议不得代替教师授权）。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.app.ai.retrieval.base import RetrievedChunk
from backend.app.domain.enums import QuestionType, ReviewStatus, ValidationStatus
from backend.app.schemas.ai import (
    ConfidenceScore,
    GradingResult,
    NonEmptyText,
    QuestionCandidate,
)
from backend.app.schemas.grading import ConfidenceDecisionDTO, SubmissionContext
from backend.app.services.grading.confidence_policy import (
    HUMAN_DECIDED_REVIEW_STATES,
    ConfidenceDecision,
)

#: 共用 Agent 状态类型版本；字段或语义发生变化时必须递增。
AGENT_STATE_VERSION: Final[str] = "1"

#: Agent Trace 状态取值；与 T063 ``AgentRun`` 的 CHECK 约束取值逐字一致。
AGENT_STATUS_TRACE_VALUES: Final[frozenset[str]] = frozenset(
    {"success", "failure", "pending_review"}
)

#: 递归 JSON 标量；自由参数字段只接受这些取值。
type JsonScalar = str | int | float | bool | None
#: 递归 JSON 值；用于出题条件、阈值等可序列化参数，禁止 Session/Provider/ORM 实体。
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]


class AgentType(StrEnum):
    """四类职责清晰的 Agent 标识。"""

    SUPERVISOR = "Supervisor"
    QUESTION = "Question"
    GRADING = "Grading"
    REVIEWER = "Reviewer"


class AgentStatus(StrEnum):
    """Agent 或节点调用的 Trace 状态。

    刻意不复用 :class:`backend.app.domain.enums.WorkflowStatus`：Trace 状态描述一次调用是否
    执行成功，而工作流状态描述整卷流程所处阶段；``pending_review`` 表示结果需要人工复核，
    既不是失败也不是完成。
    """

    SUCCESS = "success"
    FAILURE = "failure"
    PENDING_REVIEW = "pending_review"


class ReviewDecision(StrEnum):
    """Reviewer Agent 的结构化复核建议。

    取值保持 ``accept``/``revise``/``regrade`` 契约；这些是 Agent 建议，**不等于**教师通过
    review API 给出的 ``Confirmed``/``Modified`` 人工结论（T074/T077）。
    """

    ACCEPT = "accept"
    REVISE = "revise"
    REGRADE = "regrade"


class SupervisorAction(StrEnum):
    """Supervisor Agent 的流程控制信号。"""

    ROUTE = "route"
    FINISH = "finish"
    PAUSE = "pause"


def _blank_to_none(value: str | None) -> str | None:
    """把空白来源标识归一为 ``None``；缺失来源不得伪装成空字符串。"""

    if value is None:
        return None
    text = value.strip()
    return text or None


class AgentError(BaseModel):
    """Agent 或节点失败的脱敏错误。

    字段与 M3 的错误合同一致（``error_code``/``message``/``retryable``）；``message`` 只允许写
    脱敏说明，禁止写入 API Key、Authorization Header、完整 Prompt 或学生答案原文。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    error_code: NonEmptyText = Field(description="平台错误码。")
    message: NonEmptyText = Field(description="脱敏失败说明。")
    retryable: bool = Field(default=False, description="是否可重试。")
    source_code: NonEmptyText | None = Field(
        default=None, description="来源 Provider 错误码（已脱敏）。"
    )
    attempt_count: int | None = Field(
        default=None, ge=0, description="来源 Provider 尝试次数；未知时为 None。"
    )


class RetrievedContextItem(BaseModel):
    """写入 Final Context 的检索片段引用，保真映射 M2 的 ``RetrievedChunk``。

    保真要求：

    - 保留 ``course_id``/``document_id``/``metadata``/``source_mode`` 与各阶段分数
      （``semantic_score``/``keyword_score``/``fusion_score``/``rerank_score``）以及 ``rank``；
    - 缺失分数保持 ``None``，**不得**用 0 填充，否则无法区分"未参与该阶段"与"得分为 0"；
    - 不声明 ``RetrievedChunk.score`` 这类派生字段，避免与 ``extra="forbid"`` 冲突并固化第二套
      排序规则；排序分数由读取侧按 M2 既有优先级推导。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    chunk_id: NonEmptyText = Field(description="知识片段标识。")
    course_id: NonEmptyText | None = Field(default=None, description="所属课程标识。")
    document_id: NonEmptyText | None = Field(default=None, description="所属资料标识。")
    content: NonEmptyText = Field(description="实际写入 Final Context 的片段正文。")
    metadata: dict[str, JsonValue] = Field(
        default_factory=dict, description="片段来源元数据；仅允许 JSON 可序列化值。"
    )
    semantic_score: float | None = Field(default=None, description="语义检索分数。")
    keyword_score: float | None = Field(default=None, description="关键词检索分数。")
    fusion_score: float | None = Field(default=None, description="加权融合分数。")
    rerank_score: float | None = Field(default=None, description="重排分数。")
    rank: int | None = Field(
        default=None, ge=1, description="从 1 开始的排序名次；0 视为未编号并归一为 None。"
    )
    source_mode: NonEmptyText | None = Field(
        default=None, description="候选来源标记：vector / keyword / both。"
    )

    @classmethod
    def from_chunk(cls, chunk: RetrievedChunk) -> RetrievedContextItem:
        """按字段一一映射 M2 检索候选。

        不使用 ``RetrievedChunk.as_dict()`` 透传：该字典包含派生 ``score`` 字段，会与
        ``extra="forbid"`` 冲突并引入与 M2 不同步的排序语义。
        """

        return cls(
            chunk_id=chunk.chunk_id,
            course_id=_blank_to_none(chunk.course_id),
            document_id=_blank_to_none(chunk.document_id),
            content=chunk.content,
            metadata=dict(chunk.metadata),
            semantic_score=chunk.semantic_score,
            keyword_score=chunk.keyword_score,
            fusion_score=chunk.fusion_score,
            rerank_score=chunk.rerank_score,
            rank=chunk.rank or None,
            source_mode=_blank_to_none(chunk.source_mode),
        )


class QuestionGenerationRequest(BaseModel):
    """Question Agent 的出题条件（FR-024）。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    course_id: NonEmptyText = Field(description="出题所属课程。")
    knowledge_points: list[NonEmptyText] = Field(
        default_factory=list, description="要求覆盖的知识点。"
    )
    difficulty: NonEmptyText | None = Field(default=None, description="难度要求。")
    question_type: QuestionType | None = Field(default=None, description="题型要求。")
    count: int = Field(default=1, ge=1, description="候选题目数量，必须大于零。")


class SupervisorDecision(BaseModel):
    """Supervisor Agent 的路由与工具选择决策。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    action: SupervisorAction = Field(description="流程控制信号。")
    next_agent: AgentType | None = Field(default=None, description="下一步执行的 Agent。")
    tool_names: list[NonEmptyText] = Field(
        default_factory=list, description="本次选择的工具名。"
    )
    reason: NonEmptyText = Field(description="决策理由。")

    @model_validator(mode="after")
    def _validate_route_target(self) -> SupervisorDecision:
        """``route`` 必须给出目标 Agent；``finish``/``pause`` 不得残留路由目标。"""

        if self.action is SupervisorAction.ROUTE:
            if self.next_agent is None:
                raise ValueError("路由决策必须给出 next_agent。")
        elif self.next_agent is not None:
            raise ValueError("非路由决策不得给出 next_agent。")
        return self


class ReviewerOutcome(BaseModel):
    """Reviewer Agent 对分数、理由与知识点的结构化结论。

    本类型只表达 Agent 建议；教师确认/修改后的状态由 T074/T077 的复核服务写入。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    decision: ReviewDecision = Field(description="复核决策。")
    reason: NonEmptyText = Field(description="决策理由。")
    revised_grading_result: GradingResult | None = Field(
        default=None, description="修订后的评分结果；仅 revise 决策可提供。"
    )

    @model_validator(mode="after")
    def _validate_revision(self) -> ReviewerOutcome:
        """``revise`` 必须给出修订结果；``accept``/``regrade`` 不得直接改分。"""

        if self.decision is ReviewDecision.REVISE:
            if self.revised_grading_result is None:
                raise ValueError("revise 决策必须给出 revised_grading_result。")
        elif self.revised_grading_result is not None:
            raise ValueError("accept/regrade 决策不得直接给出修订后的评分结果。")
        return self


class AgentInput(BaseModel):
    """四类 Agent 共用的调用信封。

    - ``request_id`` 必填（plan §7 要求贯穿请求）；独立 Agent 调用允许 ``workflow_id=None``；
    - ``submission_context`` 复用 T054 的 ``SubmissionContext``，提供权威题目集合与稳定题序；
    - 领域输入继续复用既有类型（T050 ``SubjectiveGradingSource``/``GradingContext``、
      ``QuestionCandidate``），本类型只承载跨 Agent 共用的上下文，不复制第二套输入类型；
    - ``parameters`` 只允许递归 JSON 值，核心结果不得塞进该字典。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    agent_type: AgentType = Field(description="被调用的 Agent。")
    request_id: NonEmptyText = Field(description="贯穿本次请求的追踪标识。")
    workflow_id: NonEmptyText | None = Field(
        default=None, description="所属工作流；独立 Agent 调用为 None。"
    )
    user_id: NonEmptyText | None = Field(default=None, description="发起人标识。")
    submission_context: SubmissionContext | None = Field(
        default=None, description="评分/复核输入的权威答卷上下文。"
    )
    answer_id: NonEmptyText | None = Field(default=None, description="当前题目答案标识。")
    question_id: NonEmptyText | None = Field(default=None, description="当前题目标识。")
    question_type: QuestionType | None = Field(default=None, description="当前题目题型。")
    query: NonEmptyText | None = Field(
        default=None, description="主观题 Query Construction 产物。"
    )
    retrieved_context_ids: list[NonEmptyText] = Field(
        default_factory=list, description="实际写入 Final Context 的片段标识，保持顺序。"
    )
    retrieved_context: list[RetrievedContextItem] = Field(
        default_factory=list, description="检索片段明细。"
    )
    grading_result: GradingResult | None = Field(
        default=None, description="被复核的评分结果（Reviewer 输入）。"
    )
    confidence_decision: ConfidenceDecisionDTO | None = Field(
        default=None, description="被复核的置信度决策快照（Reviewer 输入）。"
    )
    generation_request: QuestionGenerationRequest | None = Field(
        default=None, description="出题条件（Question Agent 输入）。"
    )
    parameters: dict[str, JsonValue] = Field(
        default_factory=dict, description="附加参数；只允许 JSON 可序列化值。"
    )


class AgentOutput(BaseModel):
    """四类 Agent 共用的结构化输出。

    - Supervisor 使用 ``supervisor_decision``；
    - Question 使用 ``question_candidates``（``QuestionCandidate`` 列表）；
    - Grading 使用 ``grading_result``/``confidence``/``confidence_decision``/
      ``validation_status``；
    - Reviewer 使用 ``review_outcome``；
    - ``summary``/``model``/``prompt_version`` 供 T063 ``AgentRun`` 追踪字段使用，且只写脱敏摘要。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    agent_type: AgentType = Field(description="产出该结果的 Agent。")
    status: AgentStatus = Field(description="Trace 状态。")
    summary: NonEmptyText | None = Field(default=None, description="脱敏输出摘要。")
    validation_status: ValidationStatus | None = Field(
        default=None, description="结构化校验结果；未校验时为 None。"
    )
    question_type: QuestionType | None = Field(default=None, description="相关题目题型。")
    grading_result: GradingResult | None = Field(
        default=None, description="已通过校验的单题评分结果。"
    )
    confidence: ConfidenceScore | None = Field(default=None, description="评分置信度。")
    confidence_decision: ConfidenceDecisionDTO | None = Field(
        default=None, description="本次置信度决策快照。"
    )
    requires_review: bool = Field(default=False, description="是否需要人工复核。")
    review_status: ReviewStatus | None = Field(
        default=None, description="平台复核状态；Agent 不得写入教师人工结论。"
    )
    review_outcome: ReviewerOutcome | None = Field(
        default=None, description="Reviewer Agent 的结构化建议。"
    )
    question_candidates: list[QuestionCandidate] = Field(
        default_factory=list, description="候选题目；必须保持待审核状态。"
    )
    supervisor_decision: SupervisorDecision | None = Field(
        default=None, description="Supervisor Agent 的路由决策。"
    )
    retrieved_context_ids: list[NonEmptyText] = Field(
        default_factory=list, description="本次使用的片段标识，保持顺序。"
    )
    model: NonEmptyText | None = Field(default=None, description="实际使用的模型。")
    prompt_version: NonEmptyText | None = Field(
        default=None, description="实际使用的 Prompt 版本。"
    )
    error: AgentError | None = Field(default=None, description="失败时的脱敏错误。")

    @model_validator(mode="after")
    def _validate_status_and_review_scope(self) -> AgentOutput:
        """校验 Trace 状态、错误与复核结论的一致性。"""

        if self.status is AgentStatus.FAILURE:
            if self.error is None:
                raise ValueError("失败输出必须给出 error。")
            if self.requires_review:
                raise ValueError("失败输出不得同时声明需要人工复核。")
        elif self.error is not None:
            raise ValueError("非失败输出不得携带 error。")

        if self.status is AgentStatus.PENDING_REVIEW and not self.requires_review:
            raise ValueError("待复核输出必须显式 requires_review=True。")

        if self.review_status is not None and self.review_status.value in (
            HUMAN_DECIDED_REVIEW_STATES
        ):
            raise ValueError("Agent 不得输出教师人工复核结论。")

        if (
            self.grading_result is not None
            and self.grading_result.review_status in HUMAN_DECIDED_REVIEW_STATES
        ):
            raise ValueError("Agent 不得输出带人工复核结论的评分结果。")

        return self


def confidence_decision_snapshot(decision: ConfidenceDecision) -> ConfidenceDecisionDTO:
    """把 T053 的决策事实对象转换为可序列化快照。

    只做字段搬运，不重新判定阈值，也不改变 ``requires_review``/``review_status``。
    """

    return ConfidenceDecisionDTO(
        confidence=decision.confidence,
        threshold=decision.threshold,
        requires_review=decision.requires_review,
        review_status=decision.review_status,
        grading_status=decision.grading_status,
        reason=decision.reason,
    )


def confidence_decision_from_snapshot(snapshot: ConfidenceDecisionDTO) -> ConfidenceDecision:
    """由快照还原 T053 决策对象，供 T054 汇总接口使用。

    历史阈值原样恢复，**不**按当前配置重新判定：恢复过程必须复现当时的事实。
    """

    return ConfidenceDecision(
        confidence=snapshot.confidence,
        threshold=snapshot.threshold,
        requires_review=snapshot.requires_review,
        review_status=snapshot.review_status,
        grading_status=snapshot.grading_status,
        reason=snapshot.reason,
    )


__all__ = [
    "AGENT_STATE_VERSION",
    "AGENT_STATUS_TRACE_VALUES",
    "AgentError",
    "AgentInput",
    "AgentOutput",
    "AgentStatus",
    "AgentType",
    "JsonScalar",
    "JsonValue",
    "QuestionGenerationRequest",
    "RetrievedContextItem",
    "ReviewDecision",
    "ReviewerOutcome",
    "SupervisorAction",
    "SupervisorDecision",
    "confidence_decision_from_snapshot",
    "confidence_decision_snapshot",
]
