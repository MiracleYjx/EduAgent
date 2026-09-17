"""Supervisor Agent：任务理解、工具选择与 Agent 路由决策（T066）。

契约依据：``.specify/plan.md`` §4 Agent 分工与 §5 LangGraph 控制逻辑、
``.specify/contracts/agent-workflow.md`` 的 Agent Responsibilities 与 Contract Rules、
FR-030（客观题必须是确定性规则、不得调用 LLM）、FR-032（主观题走检索 + 结构化评分）。

设计约束：

- **只做路由决策**：本模块不实现 LangGraph 图、节点执行、检查点持久化（T072/T073），
  也不调用数据库、Embedding 或 LLM Provider。路由是纯函数：同一输入必然得到同一决策。
- **显式任务种类**：``decide`` 接收显式的 :class:`AgentTaskKind`，不使用
  ``AgentInput.agent_type``（它表示被调用的 Agent，不是任务种类），核心结果也不塞进
  ``parameters``。
- **只读整卷快照**：整卷收尾只读既有的 ``GradingWorkflowState`` 快照，不写入、不根据单题
  结果推断整卷完成。
- **待复核优先暂停**：契约要求低置信度结果进入 ``Pending Review`` 并暂停工作流，因此检测到
  待人工复核信号时输出 ``pause``（Trace 状态 ``pending_review``），优先于任何 Agent 路由；
  只有显式 ``review`` 任务才路由 Reviewer，且 Reviewer 建议不解除人工复核状态。
- **客观题不选 LLM 工具**：客观/主观判定复用 T046 的 ``QuestionRouter``，不另建题型归属规则。
- **错误脱敏**：失败输出只包含平台错误码与脱敏中文说明，不回显输入原文。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from backend.app.ai.agents.state import (
    AgentError,
    AgentInput,
    AgentOutput,
    AgentStatus,
    AgentType,
    SupervisorAction,
    SupervisorDecision,
)
from backend.app.ai.workflows.state import GradingWorkflowState
from backend.app.domain.enums import GradingMode, ReviewStatus
from backend.app.schemas.grading import (
    DiagnosisReportDTO,
    ExamResultDTO,
    ExamResultStatus,
    QuestionResultDTO,
)
from backend.app.services.diagnosis_service import DiagnosisService
from backend.app.services.grading.confidence_policy import HUMAN_DECIDED_REVIEW_STATES
from backend.app.services.grading.question_router import (
    QuestionRouter,
    normalize_question_type,
)

#: 任务种类或输入信封非法。
SUPERVISOR_INVALID_INPUT: Final[str] = "SUPERVISOR_INVALID_INPUT"
#: 出题任务缺少整体出题条件。
SUPERVISOR_MISSING_GENERATION_REQUEST: Final[str] = "SUPERVISOR_MISSING_GENERATION_REQUEST"
#: 评分任务缺少题型，无法完成客观/主观分流。
SUPERVISOR_MISSING_QUESTION_TYPE: Final[str] = "SUPERVISOR_MISSING_QUESTION_TYPE"
#: 复核任务缺少被复核的评分结果。
SUPERVISOR_MISSING_GRADING_RESULT: Final[str] = "SUPERVISOR_MISSING_GRADING_RESULT"
#: 整卷收尾缺少只读工作流快照。
SUPERVISOR_MISSING_WORKFLOW_STATE: Final[str] = "SUPERVISOR_MISSING_WORKFLOW_STATE"
#: 整卷汇总结果或诊断尚未就绪，不能结束流程。
SUPERVISOR_FINALIZE_NOT_READY: Final[str] = "SUPERVISOR_FINALIZE_NOT_READY"
#: 路由表没有覆盖该任务种类。
SUPERVISOR_ROUTE_NOT_FOUND: Final[str] = "SUPERVISOR_ROUTE_NOT_FOUND"

#: 失败码对应的脱敏中文提示。
SUPERVISOR_ERROR_MESSAGES: Final[Mapping[str, str]] = MappingProxyType(
    {
        SUPERVISOR_INVALID_INPUT: "路由输入不合法，已停止调度。",
        SUPERVISOR_MISSING_GENERATION_REQUEST: "缺少出题条件，无法调度出题任务。",
        SUPERVISOR_MISSING_QUESTION_TYPE: "缺少题目题型，无法完成评分分流。",
        SUPERVISOR_MISSING_GRADING_RESULT: "缺少被复核的评分结果，无法调度复核任务。",
        SUPERVISOR_MISSING_WORKFLOW_STATE: "缺少整卷工作流快照，无法判断是否收尾。",
        SUPERVISOR_FINALIZE_NOT_READY: "整卷汇总结果或诊断尚未就绪，不能结束流程。",
        SUPERVISOR_ROUTE_NOT_FOUND: "该任务种类没有可用的路由目标。",
    }
)

#: 路由决策版本；路由规则或工具集合变化时必须递增。
SUPERVISOR_ROUTING_VERSION: Final[str] = "1"

#: 待人工复核信号：评分的复核状态取值。
PENDING_REVIEW_SIGNALS: Final[frozenset[str]] = frozenset(
    {ReviewStatus.PENDING_REVIEW.value}
)

#: 需要重新评分：允许对应评分调度，但不得当作最终接受。
REGRADE_REVIEW_STATUS: Final[str] = ReviewStatus.RE_GRADE.value

#: 已形成人工/最终结论的复核状态；历史自动决策不得覆盖它们。
#: 复用 M3 `HUMAN_DECIDED_REVIEW_STATES`，仅把 `Re-grade` 单独拆出。
FINAL_ACCEPT_REVIEW_STATUSES: Final[frozenset[str]] = frozenset(
    HUMAN_DECIDED_REVIEW_STATES - {REGRADE_REVIEW_STATUS}
)


class AgentTaskKind(StrEnum):
    """Supervisor 可调度的任务种类；作为显式参数传入，不从 Agent 标识推断。"""

    QUESTION_GENERATION = "question_generation"
    GRADING = "grading"
    REVIEW = "review"
    FINALIZE = "finalize"


class SupervisorTool(StrEnum):
    """Supervisor 可选择的工具集合；不含 ``diagnosis_generation``（诊断由 T072 执行）。"""

    KNOWLEDGE_RETRIEVAL = "knowledge_retrieval"
    OBJECTIVE_RULE_GRADE = "objective_rule_grade"
    SUBJECTIVE_RAG_GRADING = "subjective_rag_grading"
    REVIEWER_CHECK = "reviewer_check"


#: 任务种类允许的目标 Agent；``finalize`` 只结束流程，不允许路由到任何 Agent。
SUPERVISOR_ROUTE_TABLE: Final[Mapping[AgentTaskKind, frozenset[AgentType]]] = (
    MappingProxyType(
        {
            AgentTaskKind.QUESTION_GENERATION: frozenset({AgentType.QUESTION}),
            AgentTaskKind.GRADING: frozenset({AgentType.GRADING}),
            AgentTaskKind.REVIEW: frozenset({AgentType.REVIEWER}),
            AgentTaskKind.FINALIZE: frozenset(),
        }
    )
)

#: 评分路由的工具集合：客观题只允许确定性规则工具（FR-030），主观题必须含检索与结构化评分。
GRADING_TOOLS_BY_MODE: Final[Mapping[GradingMode, tuple[SupervisorTool, ...]]] = (
    MappingProxyType(
        {
            GradingMode.OBJECTIVE: (SupervisorTool.OBJECTIVE_RULE_GRADE,),
            GradingMode.SUBJECTIVE: (
                SupervisorTool.KNOWLEDGE_RETRIEVAL,
                SupervisorTool.SUBJECTIVE_RAG_GRADING,
            ),
        }
    )
)

#: 每种任务必须提供的输入字段；缺失即失败，不做默认分支兜底。
TASK_REQUIRED_INPUT_FIELDS: Final[Mapping[AgentTaskKind, tuple[str, ...]]] = (
    MappingProxyType(
        {
            AgentTaskKind.QUESTION_GENERATION: ("generation_request",),
            AgentTaskKind.GRADING: ("question_type",),
            AgentTaskKind.REVIEW: ("grading_result",),
            AgentTaskKind.FINALIZE: (),
        }
    )
)

#: 输入字段缺失时使用的错误码。
MISSING_INPUT_ERROR_CODES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "generation_request": SUPERVISOR_MISSING_GENERATION_REQUEST,
        "question_type": SUPERVISOR_MISSING_QUESTION_TYPE,
        "grading_result": SUPERVISOR_MISSING_GRADING_RESULT,
    }
)


def normalize_task_kind(value: AgentTaskKind | str) -> AgentTaskKind:
    """把任务种类名称或取值规范化为 :class:`AgentTaskKind`；未知取值显式失败。

    类型不属于 ``AgentTaskKind`` 或字符串时抛出 :class:`TypeError`，字符串取值未知时抛出
    :class:`ValueError`；调用方统一收敛为 ``SUPERVISOR_INVALID_INPUT``。
    """

    if isinstance(value, AgentTaskKind):
        return value
    if not isinstance(value, str):
        raise TypeError("任务种类必须是 AgentTaskKind 或字符串。")
    candidate = value.strip()
    for supported in AgentTaskKind:
        if candidate.lower() in {supported.value.lower(), supported.name.lower()}:
            return supported
    raise ValueError(SUPERVISOR_ERROR_MESSAGES[SUPERVISOR_INVALID_INPUT])


def _normalize_review_status(value: object) -> str | None:
    """把复核状态取值归一为文本（兼容枚举与字符串）；无法识别时返回 ``None``。"""

    if isinstance(value, ReviewStatus):
        return value.value
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _review_statuses(
    agent_input: AgentInput,
    workflow_state: GradingWorkflowState | Mapping[str, object] | None,
) -> list[str]:
    """收集当前题的权威复核状态，按具体程度排序（整卷槽位 → 逐题结果 → 调用输入）。"""

    statuses: list[str] = []
    current_answer_id = agent_input.answer_id
    if isinstance(workflow_state, Mapping):
        status = _normalize_review_status(workflow_state.get("review_status"))
        if status is not None:
            statuses.append(status)
        raw_answer_id = workflow_state.get("current_answer_id")
        if isinstance(raw_answer_id, str) and raw_answer_id.strip():
            current_answer_id = raw_answer_id.strip()
        results = workflow_state.get("grading_results")
        if isinstance(results, Mapping) and current_answer_id is not None:
            entry = results.get(current_answer_id)
            entry_status = _normalize_review_status(getattr(entry, "review_status", None))
            if entry_status is None and isinstance(entry, Mapping):
                entry_status = _normalize_review_status(entry.get("review_status"))
            if entry_status is not None:
                statuses.append(entry_status)
    if agent_input.grading_result is not None:
        status = _normalize_review_status(agent_input.grading_result.review_status)
        if status is not None:
            statuses.append(status)
    return statuses


def _exam_result_has_pending_review(exam_result: ExamResultDTO) -> bool:
    """判断整卷是否仍存在待人工复核的题目（H02：权威复核状态优先）。

    - 平台汇总的 ``pending_review_answer_count`` 是首选信号；
    - 逐题已有权威状态时：``Pending Review`` 计入待复核，``Confirmed``/``Modified``/``Final``
      与需要重新评分的 ``Re-grade`` 不计入；
    - 逐题无权威状态时才回退到历史 ``requires_review`` 标记。
    """

    if exam_result.pending_review_answer_count > 0:
        return True
    for item in exam_result.items:
        status = _normalize_review_status(item.review_status)
        if status in FINAL_ACCEPT_REVIEW_STATUSES or status == REGRADE_REVIEW_STATUS:
            continue
        if status in PENDING_REVIEW_SIGNALS or (status is None and item.requires_review):
            return True
    return False


class SupervisorAgent:
    """根据任务种类、输入信封与只读整卷快照给出路由与流程控制决策。

    :param router: 题型分流器；默认使用无状态 :class:`QuestionRouter`，与 M3 评分链路口径一致。
    """
    """根据任务种类、输入信封与只读整卷快照给出路由与流程控制决策。

    :param router: 题型分流器；默认使用无状态 :class:`QuestionRouter`，与 M3 评分链路口径一致。
    """

    def __init__(self, *, router: QuestionRouter | None = None) -> None:
        self._router = router if router is not None else QuestionRouter()
        # 只复用 M3 的“诊断是否对应当前整卷结果”判定，不复制评分汇总算法。
        self._diagnosis_service = DiagnosisService()

    def decide(
        self,
        task_kind: AgentTaskKind | str,
        agent_input: AgentInput,
        *,
        workflow_state: GradingWorkflowState | Mapping[str, object] | None = None,
    ) -> AgentOutput:
        """返回 Supervisor 的结构化决策；任何不可路由情形都返回脱敏失败输出。"""

        try:
            kind = normalize_task_kind(task_kind)
        except (TypeError, ValueError):
            return self._failure(
                SUPERVISOR_INVALID_INPUT,
                SUPERVISOR_ERROR_MESSAGES[SUPERVISOR_INVALID_INPUT],
            )
        if not isinstance(agent_input, AgentInput):
            return self._failure(
                SUPERVISOR_INVALID_INPUT,
                SUPERVISOR_ERROR_MESSAGES[SUPERVISOR_INVALID_INPUT],
            )

        # 待人工复核优先暂停（契约要求低置信度暂停工作流），显式复核任务除外。
        if kind is not AgentTaskKind.REVIEW and self._is_pending_review(
            agent_input,
            workflow_state,
        ):
            return self._paused()

        missing = [
            field_name
            for field_name in TASK_REQUIRED_INPUT_FIELDS.get(kind, ())
            if getattr(agent_input, field_name, None) is None
        ]
        if missing:
            code = MISSING_INPUT_ERROR_CODES.get(
                missing[0],
                SUPERVISOR_INVALID_INPUT,
            )
            return self._failure(code, SUPERVISOR_ERROR_MESSAGES.get(code, "路由输入不完整。"))

        if kind is AgentTaskKind.FINALIZE:
            return self._finalize(workflow_state)
        if kind is AgentTaskKind.QUESTION_GENERATION:
            return self._route(
                AgentType.QUESTION,
                (SupervisorTool.KNOWLEDGE_RETRIEVAL,),
                "出题任务需要课程知识检索后生成候选题目。",
            )
        if kind is AgentTaskKind.REVIEW:
            return self._route(
                AgentType.REVIEWER,
                (SupervisorTool.REVIEWER_CHECK,),
                "显式复核任务交由 Reviewer Agent 检查分数、理由与知识点。",
                requires_review=self._is_pending_review(agent_input, workflow_state),
            )
        if kind is AgentTaskKind.GRADING:
            return self._grade(agent_input, workflow_state)
        return self._failure(
            SUPERVISOR_ROUTE_NOT_FOUND,
            SUPERVISOR_ERROR_MESSAGES[SUPERVISOR_ROUTE_NOT_FOUND],
        )

    def _grade(
        self,
        agent_input: AgentInput,
        workflow_state: GradingWorkflowState | Mapping[str, object] | None,
    ) -> AgentOutput:
        """按题型分流评分工具：客观题只走确定性规则，主观题走检索 + 结构化评分。"""

        try:
            question_type = normalize_question_type(agent_input.question_type)
            mode = self._router.route_type(question_type)
        except (TypeError, ValueError):
            return self._failure(
                SUPERVISOR_MISSING_QUESTION_TYPE,
                SUPERVISOR_ERROR_MESSAGES[SUPERVISOR_MISSING_QUESTION_TYPE],
            )
        tools = GRADING_TOOLS_BY_MODE.get(mode)
        if tools is None:
            return self._failure(
                SUPERVISOR_ROUTE_NOT_FOUND,
                SUPERVISOR_ERROR_MESSAGES[SUPERVISOR_ROUTE_NOT_FOUND],
            )
        reason = (
            "客观题使用确定性规则评分，不调用检索或模型。"
            if mode is GradingMode.OBJECTIVE
            else "主观题需要课程检索上下文与结构化评分输出。"
        )
        return self._route(
            AgentType.GRADING,
            tools,
            reason,
            requires_review=self._is_pending_review(agent_input, workflow_state),
        )

    def _finalize(
        self,
        workflow_state: GradingWorkflowState | Mapping[str, object] | None,
    ) -> AgentOutput:
        """整卷收尾：按真实结果与诊断状态判定就绪，未就绪一律不得结束流程。

        就绪条件（全部满足才返回 ``finish``）：

        1. ``exam_result`` 是 :class:`ExamResultDTO` 且已最终确认（``is_final`` 为真且
           ``result_status`` 为 ``Final``）；
        2. 无缺题、评分失败或未通过结构化校验的题目（不把“对象非空”当成就绪）；
        3. ``final_results`` 为非空 :class:`QuestionResultDTO` 序列，且数量与预期题目数量一致；
        4. ``diagnosis`` 是 :class:`DiagnosisReportDTO`，并由 M3 ``DiagnosisService.is_current``
           判定对应当前整卷结果（``Not Ready``/``Failed``/``Stale`` 与旧版本 ``Ready`` 均不就绪）。

        整卷仍有待人工复核题目时返回 ``pause``；其余未就绪情况保留
        ``SUPERVISOR_FINALIZE_NOT_READY`` 失败语义。
        """

        if not isinstance(workflow_state, Mapping):
            return self._failure(
                SUPERVISOR_MISSING_WORKFLOW_STATE,
                SUPERVISOR_ERROR_MESSAGES[SUPERVISOR_MISSING_WORKFLOW_STATE],
            )
        exam_result = workflow_state.get("exam_result")
        if not isinstance(exam_result, ExamResultDTO):
            return self._not_ready()
        if _exam_result_has_pending_review(exam_result):
            return self._paused()
        if (
            not exam_result.is_final
            or exam_result.result_status is not ExamResultStatus.FINAL
            or exam_result.missing_answer_ids
            or exam_result.failed_answer_ids
            or exam_result.not_validated_answer_ids
        ):
            return self._not_ready()

        final_results = workflow_state.get("final_results")
        if (
            not isinstance(final_results, Sequence)
            or isinstance(final_results, (str, bytes))
            or not final_results
            or not all(isinstance(item, QuestionResultDTO) for item in final_results)
            or len(final_results) != exam_result.expected_answer_count
        ):
            return self._not_ready()

        diagnosis = workflow_state.get("diagnosis")
        if not isinstance(diagnosis, DiagnosisReportDTO):
            return self._not_ready()
        if not self._diagnosis_service.is_current(diagnosis, exam_result):
            return self._not_ready()

        return AgentOutput(
            agent_type=AgentType.SUPERVISOR,
            status=AgentStatus.SUCCESS,
            summary="整卷结果与诊断均已就绪，工作流可以结束。",
            supervisor_decision=SupervisorDecision(
                action=SupervisorAction.FINISH,
                tool_names=[],
                reason="逐题结果已最终确认、整卷结果与诊断均对应当前结果，无需继续调度。",
            ),
        )

    def _not_ready(self) -> AgentOutput:
        """整卷、逐题结果或诊断尚未就绪；保留显式失败语义。"""

        return self._failure(
            SUPERVISOR_FINALIZE_NOT_READY,
            SUPERVISOR_ERROR_MESSAGES[SUPERVISOR_FINALIZE_NOT_READY],
        )

    def _route(
        self,
        target: AgentType,
        tools: tuple[SupervisorTool, ...],
        reason: str,
        *,
        requires_review: bool = False,
    ) -> AgentOutput:
        """构造路由成功的结构化决策；不写任何人工复核结论。"""

        if not any(target in targets for targets in SUPERVISOR_ROUTE_TABLE.values()):
            return self._failure(
                SUPERVISOR_ROUTE_NOT_FOUND,
                SUPERVISOR_ERROR_MESSAGES[SUPERVISOR_ROUTE_NOT_FOUND],
            )
        return AgentOutput(
            agent_type=AgentType.SUPERVISOR,
            status=AgentStatus.SUCCESS,
            summary=f"路由到 {target.value} Agent。",
            requires_review=requires_review,
            supervisor_decision=SupervisorDecision(
                action=SupervisorAction.ROUTE,
                next_agent=target,
                tool_names=[tool.value for tool in tools],
                reason=reason,
            ),
        )

    def _paused(self) -> AgentOutput:
        """待人工复核时暂停工作流：等待教师复核，不路由任何 Agent。"""

        return AgentOutput(
            agent_type=AgentType.SUPERVISOR,
            status=AgentStatus.PENDING_REVIEW,
            summary="检测到待人工复核的评分结果，已暂停工作流等待教师复核。",
            requires_review=True,
            supervisor_decision=SupervisorDecision(
                action=SupervisorAction.PAUSE,
                tool_names=[],
                reason="存在低置信度或待复核结果，只有教师可以确认或修改。",
            ),
        )

    def _failure(self, code: str, message: str) -> AgentOutput:
        """构造脱敏失败输出；失败不得同时声明需要人工复核。"""

        return AgentOutput(
            agent_type=AgentType.SUPERVISOR,
            status=AgentStatus.FAILURE,
            error=AgentError(error_code=code, message=message, retryable=False),
        )

    @staticmethod
    def _is_pending_review(
        agent_input: AgentInput,
        workflow_state: GradingWorkflowState | Mapping[str, object] | None,
    ) -> bool:
        """检测待人工复核信号；权威复核状态优先于历史自动决策。

        判定顺序（H02）：

        1. 当前题权威复核状态为 ``Confirmed``/``Modified``/``Final`` 时，教师或最终结论已成立，
           历史 ``requires_review=True`` 不得重新暂停（复用 M3 ``HUMAN_DECIDED_REVIEW_STATES`` 口径）；
        2. 权威状态为 ``Pending Review`` 时暂停；
        3. 权威状态为 ``Re-grade`` 时允许重新评分调度，不暂停，也不视为最终接受；
        4. 无任何权威状态时才回退到历史自动决策。

        历史 ``confidence``/``threshold``/``requires_review`` 只读，不重新判定、不修改。
        """

        statuses = _review_statuses(agent_input, workflow_state)
        if any(status in FINAL_ACCEPT_REVIEW_STATUSES for status in statuses):
            return False
        if any(status in PENDING_REVIEW_SIGNALS for status in statuses):
            return True
        if any(status == REGRADE_REVIEW_STATUS for status in statuses):
            return False
        decision = agent_input.confidence_decision
        return bool(decision is not None and decision.requires_review)


__all__ = [
    "FINAL_ACCEPT_REVIEW_STATUSES",
    "GRADING_TOOLS_BY_MODE",
    "MISSING_INPUT_ERROR_CODES",
    "PENDING_REVIEW_SIGNALS",
    "REGRADE_REVIEW_STATUS",
    "SUPERVISOR_ERROR_MESSAGES",
    "SUPERVISOR_FINALIZE_NOT_READY",
    "SUPERVISOR_INVALID_INPUT",
    "SUPERVISOR_MISSING_GENERATION_REQUEST",
    "SUPERVISOR_MISSING_GRADING_RESULT",
    "SUPERVISOR_MISSING_QUESTION_TYPE",
    "SUPERVISOR_MISSING_WORKFLOW_STATE",
    "SUPERVISOR_ROUTE_NOT_FOUND",
    "SUPERVISOR_ROUTE_TABLE",
    "SUPERVISOR_ROUTING_VERSION",
    "TASK_REQUIRED_INPUT_FIELDS",
    "AgentTaskKind",
    "SupervisorAgent",
    "SupervisorTool",
    "normalize_task_kind",
]
