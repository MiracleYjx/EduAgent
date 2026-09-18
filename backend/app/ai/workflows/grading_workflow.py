"""LangGraph 阅卷工作流（T072）。

契约依据：``.specify/plan.md`` §5（状态图与控制逻辑）、``.specify/contracts/agent-workflow.md``
（Grading Workflow States / Required State / Contract Rules）、FR-029/FR-030（题型分流与客观题
确定性）、FR-033/FR-034（统一汇总与结构化校验）、FR-035/FR-036（阈值、待复核与教师确认）与
FR-038（诊断只消费已确认结果）。

职责与边界：

- **契约唯一来源**：节点 id 与顺序取 :data:`~backend.app.ai.workflows.grading_handoff.\
WORKFLOW_NODE_ORDER`，条件边取 ``CLASSIFY_EDGES``/``regrade_target_node``，状态增量取
  ``grading_handoff``/``reviewer_handoff``/``unified_result_patch``，诊断门槛取 ``diagnosis_allowed``；
  本模块不重复定义节点名，也不复制评分、汇总或诊断判定。
- **只编排**：逐题调用 T069 :meth:`GradingAgent.grade_answer_async`（每题一次），**不调用**整卷
  ``score_async``；结构化校验与置信度检查只**复核** T069 已产出的事实，不重新解析评分载荷。
- **人工复核不可自动解除（B01）**：Reviewer 只产出建议；仍在待复核的题必须停在当前答案，不进入
  ``next_answer``、汇总或诊断。已记录的待复核决策在重评后**不被覆盖**，因此重评带来的置信度提升
  不会自动放行；教师结论由 T074/T077 写入（本模块提供 :meth:`GradingWorkflow.mark_teacher_decision`
  作为交接缝，供 T074 落地前的手工/测试注入）。
- **真实暂停（B02）**：暂停使用 LangGraph 原生 ``interrupt()``，检查点由调用方注入（单测用内存实现）；
  未注入检查点时 ``resumable=False`` 并明确报告“恢复支撑未就绪”，不假称可恢复。T073 的数据库检查点
  存储不在本批范围。
- **有限重评（B05）**：``retry_count`` 只计业务重评次数（与 Provider 内部重试无关），预算由
  ``max_regrades`` 固定（默认 1，允许 0）；零预算或超限即有界失败
  ``GRADING_WORKFLOW_REGRADE_BUDGET_EXHAUSTED``。
- **终止状态（B06）**：评分/校验/复核/身份/缺题失败 → ``Failed`` + ``error``；需人工复核 → 实际暂停；
  最终结果与有效诊断均生成 → ``Completed``；诊断未就绪或失败 → 保留 ``exam_result`` 并写 ``error``
  与 ``pause_reason``（状态 ``Paused``），不通过“跳过诊断”报成功。
- **交付边界**：本批交付内存图组件；正常结果落库仍属既有 T060/T061 存储边界（本批不接线），
  T073 检查点持久化与 T074 复核服务均未实现。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, cast

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt

from backend.app.ai.agents.reviewer_agent import ReviewerAgent
from backend.app.ai.agents.state import (
    AgentError,
    AgentOutput,
    confidence_decision_from_snapshot,
)
from backend.app.ai.workflows.grading_handoff import (
    ACCEPT,
    ACCEPTED_REVIEW_STATES,
    CLASSIFY_EDGES,
    CLASSIFY_QUESTION,
    CONFIDENCE_CHECK,
    GENERATE_DIAGNOSIS,
    GRADING_HANDOFF_INVALID_INPUT,
    LOAD_SUBMISSION,
    NEXT_ANSWER,
    OBJECTIVE_RULE_GRADE,
    PENDING_REVIEW,
    REGRADE,
    REVIEWER_AGENT,
    STRUCTURED_VALIDATION,
    SUBJECTIVE_RETRIEVE_GRADE,
    UNIFIED_RESULT,
    WORKFLOW_NODE_ORDER,
    GradingHandoffError,
    diagnosis_allowed,
    grading_handoff,
    may_enter_final_results,
    regrade_target_node,
    reviewer_handoff,
    unified_result_patch,
)
from backend.app.ai.workflows.state import ANSWER_SLOT_FIELDS, GradingWorkflowState
from backend.app.core.config import AppSettings
from backend.app.domain.enums import (
    GradingMode,
    ReviewStatus,
    ValidationStatus,
    WorkflowStatus,
)
from backend.app.schemas.ai import GradingResult
from backend.app.schemas.grading import ConfidenceDecisionDTO
from backend.app.services.grading.grading_task_service import (
    GradingTargetAnswer,
    SubmissionSnapshot,
)
from backend.app.services.grading.question_router import (
    GradingRoutingError,
    QuestionRouter,
    normalize_question_type,
)
from backend.app.services.grading.result_aggregator import ResultAggregator

#: 输入/身份不合法（空白标识或与注入快照不一致）。
GRADING_WORKFLOW_INVALID_INPUT: Final[str] = "GRADING_WORKFLOW_INVALID_INPUT"
#: 答卷与考试题目集合不一致（缺题或重复）。
GRADING_WORKFLOW_MISSING_ANSWER: Final[str] = "GRADING_WORKFLOW_MISSING_ANSWER"
#: 本题的答案标识与答卷快照不一致。
GRADING_WORKFLOW_IDENTITY_MISMATCH: Final[str] = "GRADING_WORKFLOW_IDENTITY_MISMATCH"
#: 题型无法分流，不能进入评分节点。
GRADING_WORKFLOW_UNSUPPORTED_QUESTION_TYPE: Final[str] = "GRADING_WORKFLOW_UNSUPPORTED_QUESTION_TYPE"
#: 结果未通过结构化校验（T069 应已拒绝，此处为图节点兜底）。
GRADING_WORKFLOW_RESULT_NOT_VALIDATED: Final[str] = "GRADING_WORKFLOW_RESULT_NOT_VALIDATED"
#: 主观题缺少本次置信度决策快照，不得默认按已接受处理。
GRADING_WORKFLOW_MISSING_DECISION: Final[str] = "GRADING_WORKFLOW_MISSING_DECISION"
#: 重评预算已用尽（有界失败，避免无限重评）。
GRADING_WORKFLOW_REGRADE_BUDGET_EXHAUSTED: Final[str] = "GRADING_WORKFLOW_REGRADE_BUDGET_EXHAUSTED"
#: 整卷汇总失败；不返回部分成绩。
GRADING_WORKFLOW_AGGREGATION_FAILED: Final[str] = "GRADING_WORKFLOW_AGGREGATION_FAILED"
#: 诊断未接线或生成失败；保留已形成的整卷结果。
GRADING_WORKFLOW_DIAGNOSIS_FAILED: Final[str] = "GRADING_WORKFLOW_DIAGNOSIS_FAILED"
#: 当前线程已有事件循环，必须使用异步入口。
GRADING_WORKFLOW_ASYNC_REQUIRED: Final[str] = "GRADING_WORKFLOW_ASYNC_REQUIRED"

#: 逐题槽位中需要按列表清空的字段（其余槽位置 ``None``）。
LIST_SLOT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "retrieved_context_ids",
        "retrieved_context",
    }
)

#: 默认重评预算：每题最多 1 次业务重评（0 表示禁止重评）。
DEFAULT_MAX_REGRADES_PER_ANSWER: Final[int] = 1

#: 图版本；节点集合、边或状态映射变化时必须递增。
GRADING_WORKFLOW_VERSION: Final[str] = "1"


class GradingWorkflowError(RuntimeError):
    """工作流边界错误；保留脱敏错误码与可重试语义。"""

    #: 脱敏错误码；默认非法输入，可按调用场景覆盖。
    error_code: str = GRADING_WORKFLOW_INVALID_INPUT
    #: 是否可重试；本模块的错误均为不可重试的业务问题。
    retryable: bool = False
    #: 上游来源错误码；无来源时保持 ``None``。
    source_code: str | None = None

    def __init__(self, detail: str, *, error_code: str | None = None) -> None:
        if error_code is not None:
            self.error_code = error_code
        super().__init__(f"{self.error_code}：{detail}")
        self.detail = detail


def cleared_slots() -> dict[str, object]:
    """按 T065 ``ANSWER_SLOT_FIELDS`` 清空逐题槽位，避免沿用上一题或上一次重评的产物。"""

    return {field: ([] if field in LIST_SLOT_FIELDS else None) for field in ANSWER_SLOT_FIELDS}


@dataclass(frozen=True, slots=True)
class GradingWorkflowDeps:
    """运行依赖：快照、Agent、诊断服务与可选组件（不进入可序列化状态）。

    :param snapshot: 权威答卷快照；题序以 ``answers[].order``（1 基）为准。
    :param agent: T069 阅卷 Agent；必须实现 ``grade_answer_async``。
    :param reviewer: T070 复核 Agent；``None`` 时按默认构造。
    :param diagnosis_service: M3 诊断服务；``None`` 时诊断节点显式报告未接线。
    :param aggregator: 整卷汇总服务；``None`` 时使用 M3 ``ResultAggregator``。
    :param session_factory: 主观题自建会话工厂；自建会话一定释放。
    :param session: 借入会话；由调用方负责释放，本模块不关闭。
    :param settings: 与 T069 同一个 ``AppSettings``（显式组件 > 逐次设置 > 构造设置 > 全局）。
    :param max_regrades: 每题业务重评预算；``0`` 表示禁止重评。
    """

    snapshot: SubmissionSnapshot
    agent: Any
    reviewer: Any = None
    diagnosis_service: Any = None
    aggregator: Any = None
    session_factory: Callable[[], Any] | None = None
    session: Any = None
    settings: AppSettings | None = None
    max_regrades: int = DEFAULT_MAX_REGRADES_PER_ANSWER


@dataclass(frozen=True, slots=True)
class GradingWorkflowResult:
    """一次运行（或恢复）的结果：完整状态、是否中断与仍在待复核的答案。"""

    state: Mapping[str, Any]
    interrupted: bool
    pending_answer_ids: tuple[str, ...]


class GradingWorkflow:
    """LangGraph 阅卷工作流：节点与条件边全部按 T071 契约装配。"""

    def __init__(
        self,
        deps: GradingWorkflowDeps,
        *,
        checkpointer: Any | None = None,
        allow_interrupt: bool | None = None,
    ) -> None:
        self._deps = deps
        self._checkpointer = checkpointer
        # 只有注入了检查点才具备恢复支撑；否则不得声明可恢复。
        self._interrupt_enabled = (
            checkpointer is not None if allow_interrupt is None else bool(allow_interrupt)
        )
        handlers: dict[str, Callable[[Mapping[str, Any]], Any]] = {}
        for node_id in WORKFLOW_NODE_ORDER:
            handler = getattr(self, f"_node_{node_id}", None)
            if handler is None:
                raise GradingWorkflowError(
                    f"节点 {node_id} 缺少实现，契约与图实现已分叉。"
                )
            handlers[node_id] = handler
        self._nodes = MappingProxyType(handlers)
        self._router = QuestionRouter()
        self._aggregator = deps.aggregator if deps.aggregator is not None else ResultAggregator()
        self._compiled = self._build_graph().compile(checkpointer=checkpointer)

    # ------------------------------------------------------------------ 公开接口

    @property
    def deps(self) -> GradingWorkflowDeps:
        """返回运行依赖。"""

        return self._deps

    @property
    def nodes(self) -> Mapping[str, Callable[[Mapping[str, Any]], Any]]:
        """返回节点 id 到处理函数的只读映射（供测试与 T072 之后的节点级复用）。"""

        return self._nodes

    @property
    def compiled(self) -> Any:
        """返回编译后的图（T073 保存检查点时复用同一编译产物）。"""

        return self._compiled

    async def run_async(
        self,
        *,
        request_id: str,
        workflow_id: str,
        submission_id: str,
        thread_id: str | None = None,
    ) -> GradingWorkflowResult:
        """异步运行整卷工作流；中断时返回被中断的状态与待复核答案。"""

        if self._checkpointer is not None and not thread_id:
            raise GradingWorkflowError("注入检查点时必须提供 thread_id 才能运行或恢复。")
        initial: dict[str, Any] = {
            "workflow_id": workflow_id,
            "request_id": request_id,
            "submission_id": submission_id,
            "current_answer_order": self._first_order(),
            "retry_count": 0,
            "status": WorkflowStatus.RUNNING,
            "current_node": LOAD_SUBMISSION,
        }
        raw = await self._compiled.ainvoke(initial, self._config(thread_id))
        return self._to_result(raw)

    def run(
        self,
        *,
        request_id: str,
        workflow_id: str,
        submission_id: str,
        thread_id: str | None = None,
    ) -> GradingWorkflowResult:
        """同步入口：无运行中事件循环时包装 ``asyncio.run``，否则显式报错。"""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.run_async(
                    request_id=request_id,
                    workflow_id=workflow_id,
                    submission_id=submission_id,
                    thread_id=thread_id,
                )
            )
        raise GradingWorkflowError(
            "当前线程已有事件循环，请使用 await run_async(...)。",
            error_code=GRADING_WORKFLOW_ASYNC_REQUIRED,
        )

    async def resume_async(
        self,
        *,
        thread_id: str,
        resume_value: Any = None,
    ) -> GradingWorkflowResult:
        """按原 ``thread_id`` 恢复被中断的运行（原运行与复核轨迹保持不变，FR-036）。

        ``resume_value`` 为 ``None`` 时使用显式确认载荷：LangGraph 要求传入具体恢复值，
        本批不解释该值（教师结论由 T074 写入状态，而不是靠恢复值语义）。
        """

        if self._checkpointer is None:
            raise GradingWorkflowError("未注入检查点时无法恢复：本批不具备恢复支撑。")
        payload = resume_value if resume_value is not None else {"acknowledged": True}
        raw = await self._compiled.ainvoke(
            Command(resume=payload),
            self._config(thread_id),
        )
        return self._to_result(raw)

    def mark_teacher_decision(
        self,
        *,
        thread_id: str,
        answer_id: str,
        review_status: str,
    ) -> None:
        """把教师结论写入检查点状态（**T074 落地前的交接缝**）。

        T074 的复核服务将负责真实持久化与权限校验；本方法只把教师结论写进本题结果与决策快照，
        用于让待复核题解除暂停并进入最终成绩，避免用字段写入代替人工授权。
        """

        if self._checkpointer is None:
            raise GradingWorkflowError("未注入检查点时无法写入教师结论。")
        config = self._config(thread_id)
        if config is None:  # pragma: no cover - 上方已拒绝无检查点情形
            raise GradingWorkflowError("未注入检查点时无法写入教师结论。")
        current = dict(self._compiled.get_state(config).values)
        results = dict(current.get("grading_results") or {})
        result = results.get(answer_id)
        if not isinstance(result, GradingResult):
            raise GradingWorkflowError(
                "教师结论只能写入已有评分结果的题目，不存在的结果不得凭空确认。"
            )
        results[answer_id] = result.model_copy(update={"review_status": review_status})
        updates: dict[str, Any] = {"grading_results": results, "review_status": review_status}
        decisions = dict(current.get("confidence_decisions") or {})
        decision = decisions.get(answer_id)
        if isinstance(decision, ConfidenceDecisionDTO):
            decisions[answer_id] = decision.model_copy(
                update={"requires_review": False, "review_status": review_status}
            )
            updates["confidence_decisions"] = decisions
        self._compiled.update_state(config, updates)

    # ------------------------------------------------------------------ 图装配

    def _build_graph(self) -> StateGraph:
        """按 ``WORKFLOW_NODE_ORDER`` 注册节点，并按契约声明条件边。"""

        graph: StateGraph = StateGraph(GradingWorkflowState)
        for node_id, handler in self._nodes.items():
            graph.add_node(node_id, cast("Any", handler))
        graph.set_entry_point(LOAD_SUBMISSION)

        graph.add_conditional_edges(
            LOAD_SUBMISSION,
            self._route_after_load,
            {CLASSIFY_QUESTION: CLASSIFY_QUESTION, END: END},
        )
        graph.add_conditional_edges(
            CLASSIFY_QUESTION,
            self._route_by_question_type,
            {
                OBJECTIVE_RULE_GRADE: OBJECTIVE_RULE_GRADE,
                SUBJECTIVE_RETRIEVE_GRADE: SUBJECTIVE_RETRIEVE_GRADE,
                END: END,
            },
        )
        for grading_node in (OBJECTIVE_RULE_GRADE, SUBJECTIVE_RETRIEVE_GRADE):
            graph.add_conditional_edges(
                grading_node,
                self._route_after_grading,
                {STRUCTURED_VALIDATION: STRUCTURED_VALIDATION, END: END},
            )
        graph.add_conditional_edges(
            STRUCTURED_VALIDATION,
            self._route_after_validation,
            {CONFIDENCE_CHECK: CONFIDENCE_CHECK, END: END},
        )
        graph.add_conditional_edges(
            CONFIDENCE_CHECK,
            self._route_by_confidence,
            {ACCEPT: ACCEPT, PENDING_REVIEW: PENDING_REVIEW, END: END},
        )
        graph.add_edge(ACCEPT, NEXT_ANSWER)
        graph.add_conditional_edges(
            PENDING_REVIEW,
            self._route_after_pending,
            {REVIEWER_AGENT: REVIEWER_AGENT, NEXT_ANSWER: NEXT_ANSWER},
        )
        graph.add_conditional_edges(
            REVIEWER_AGENT,
            self._route_after_reviewer,
            {
                REGRADE: REGRADE,
                PENDING_REVIEW: PENDING_REVIEW,
                NEXT_ANSWER: NEXT_ANSWER,
                END: END,
            },
        )
        graph.add_conditional_edges(
            REGRADE,
            self._route_after_regrade,
            {
                OBJECTIVE_RULE_GRADE: OBJECTIVE_RULE_GRADE,
                SUBJECTIVE_RETRIEVE_GRADE: SUBJECTIVE_RETRIEVE_GRADE,
                END: END,
            },
        )
        graph.add_conditional_edges(
            NEXT_ANSWER,
            self._route_after_next_answer,
            {CLASSIFY_QUESTION: CLASSIFY_QUESTION, UNIFIED_RESULT: UNIFIED_RESULT},
        )
        graph.add_conditional_edges(
            UNIFIED_RESULT,
            self._route_after_unified,
            {GENERATE_DIAGNOSIS: GENERATE_DIAGNOSIS, END: END},
        )
        graph.add_edge(GENERATE_DIAGNOSIS, END)
        return graph

    # ------------------------------------------------------------------ 运行辅助

    def _config(self, thread_id: str | None) -> RunnableConfig | None:
        """构造 LangGraph 运行配置；无检查点时不需要线程配置。"""

        if self._checkpointer is None:
            return None
        return {"configurable": {"thread_id": thread_id}}

    def _to_result(self, raw: Mapping[str, Any]) -> GradingWorkflowResult:
        """裁剪 LangGraph 私有键，汇总中断信息与待复核答案。"""

        state = {key: value for key, value in raw.items() if not str(key).startswith("__")}
        decisions = state.get("confidence_decisions") or {}
        pending = tuple(
            sorted(
                str(answer_id)
                for answer_id, decision in decisions.items()
                if isinstance(decision, ConfidenceDecisionDTO)
                and decision.review_status not in ACCEPTED_REVIEW_STATES
            )
        )
        return GradingWorkflowResult(
            state=state,
            interrupted=bool(raw.get("__interrupt__")),
            pending_answer_ids=pending,
        )

    def _targets(self) -> Iterator[GradingTargetAnswer]:
        return iter(self._deps.snapshot.answers)

    def _first_order(self) -> int | None:
        """返回答卷第一题的 1 基题序；空答卷返回 ``None``。"""

        answers = self._deps.snapshot.answers
        return answers[0].order if answers else None

    def _has_duplicate_orders(self) -> bool:
        """题序必须唯一：重复题序会让“下一题”无法推进，必须在入口拒绝。"""

        orders = [target.order for target in self._targets()]
        return len(set(orders)) != len(orders)

    def _has_duplicate_answers(self) -> bool:
        """答案标识必须唯一：重复答案会让逐题槽位与集合无法按答案对齐。"""

        answer_ids = [target.answer_id for target in self._targets()]
        return len(set(answer_ids)) != len(answer_ids)

    def _target_for_order(self, order: object) -> GradingTargetAnswer | None:
        """按 1 基题序（``GradingTargetAnswer.order``）取目标；不使用列表下标语义。"""

        for target in self._targets():
            if target.order == order:
                return target
        return None

    def _target_for_answer(
        self,
        answer_id: object,
        *,
        order: object = None,
    ) -> GradingTargetAnswer | None:
        """按（可选）答案标识与题序取目标：两者都给时必须同时匹配。

        重评时保持当前目标，不依赖已清空的槽位；同一答案必须同时锁定题序，
        避免题序与答案标识错配导致逐题推进回退。
        """

        for target in self._targets():
            if target.answer_id != answer_id:
                continue
            if order is not None and target.order != order:
                continue
            return target
        return None

    def _mode_for(self, question_type: object) -> GradingMode:
        """复用 M3 分流器解析评分模式（未知题型显式失败）。"""

        try:
            return self._router.route_type(
                normalize_question_type(cast("Any", question_type))
            )
        except (TypeError, ValueError, GradingRoutingError) as error:
            raise GradingWorkflowError(
                "题型无法分流，无法确定评分节点。",
                error_code=GRADING_WORKFLOW_UNSUPPORTED_QUESTION_TYPE,
            ) from error

    def _failure(
        self,
        code: str,
        message: str,
        *,
        current_node: str,
        keep_results: bool = False,
    ) -> dict[str, Any]:
        """构造失败状态增量：清除槽位与旧诊断（除非已形成真实整卷结果）。"""

        patch: dict[str, Any] = {
            "status": WorkflowStatus.FAILED,
            "current_node": current_node,
            "error": AgentError(error_code=code, message=message, retryable=False),
            "review_status": None,
            "pause_reason": None,
            "resumable": False,
        }
        if not keep_results:
            patch.update(
                {
                    "exam_result": None,
                    "final_results": [],
                    "diagnosis": None,
                }
            )
        return patch

    def _identity_failure(self, state: Mapping[str, Any]) -> dict[str, Any] | None:
        """校验身份字段与注入快照一致；不一致返回失败增量。"""

        workflow_id = state.get("workflow_id")
        request_id = state.get("request_id")
        submission_id = state.get("submission_id")
        if not all(isinstance(value, str) and value.strip() for value in (workflow_id, request_id)):
            return self._failure(
                GRADING_WORKFLOW_INVALID_INPUT,
                "缺少贯穿请求的 workflow_id 或 request_id。",
                current_node=LOAD_SUBMISSION,
            )
        if not isinstance(submission_id, str) or not submission_id.strip():
            return self._failure(
                GRADING_WORKFLOW_INVALID_INPUT,
                "缺少答卷标识 submission_id。",
                current_node=LOAD_SUBMISSION,
            )
        if submission_id != self._deps.snapshot.submission_id:
            return self._failure(
                GRADING_WORKFLOW_IDENTITY_MISMATCH,
                "运行提交的答卷标识与注入快照不一致，拒绝写入不属于该答卷的结果。",
                current_node=LOAD_SUBMISSION,
            )
        return None

    def _pending_decision(self, state: Mapping[str, Any]) -> ConfidenceDecisionDTO | None:
        """返回当前题已记录的待复核决策（重评不得覆盖它，B01）。"""

        answer_id = state.get("current_answer_id")
        decisions = state.get("confidence_decisions") or {}
        decision = decisions.get(answer_id) if isinstance(decisions, Mapping) else None
        if isinstance(decision, ConfidenceDecisionDTO) and (
            decision.review_status not in ACCEPTED_REVIEW_STATES
        ):
            return decision
        return None

    def _needs_human_review(self, state: Mapping[str, Any]) -> bool:
        """按权威复核状态判断是否仍需人工复核（缺依据时保守暂停）。"""

        pending = self._pending_decision(state)
        if pending is not None:
            return True
        answer_id = state.get("current_answer_id")
        decisions = state.get("confidence_decisions") or {}
        decision = decisions.get(answer_id) if isinstance(decisions, Mapping) else None
        if isinstance(decision, ConfidenceDecisionDTO):
            return decision.review_status not in ACCEPTED_REVIEW_STATES
        result = state.get("grading_result")
        if isinstance(result, GradingResult):
            return result.review_status not in ACCEPTED_REVIEW_STATES
        return True

    def _record_result(
        self,
        state: Mapping[str, Any],
        output: AgentOutput,
        *,
        answer_id: str,
    ) -> dict[str, Any]:
        """把逐题结果与决策写入整卷集合：按 ``answer_id`` 替换，禁止追加重复计分。"""

        result = output.grading_result
        if not isinstance(result, GradingResult):
            return {}
        results = dict(state.get("grading_results") or {})
        results[answer_id] = result
        patch: dict[str, Any] = {"grading_results": results}
        decision = output.confidence_decision
        if isinstance(decision, ConfidenceDecisionDTO):
            decisions = dict(state.get("confidence_decisions") or {})
            # 已记录的待复核决策不被覆盖：重评不得自动解除人工复核要求。
            if decisions.get(answer_id) is None or not self._pending_decision(
                {**state, "current_answer_id": answer_id}
            ):
                decisions[answer_id] = decision
            patch["confidence_decisions"] = decisions
        return patch

    def _acquire_session(self, mode: GradingMode) -> tuple[Any, bool]:
        """解析本次评分使用的会话：借入不关闭，自建由调用方释放。"""

        if mode is not GradingMode.SUBJECTIVE:
            return None, False
        if self._deps.session is not None:
            return self._deps.session, False
        factory = self._deps.session_factory
        if factory is None:
            return None, False
        return factory(), True

    # ------------------------------------------------------------------ 节点实现

    async def _node_load_submission(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """`Load Submission`：校验身份、载入权威题目集合并清理上一轮残留。"""

        patch: dict[str, Any] = {
            **cleared_slots(),
            "current_node": LOAD_SUBMISSION,
            "current_answer_order": self._first_order(),
            "retry_count": 0,
            "status": WorkflowStatus.RUNNING,
            "grading_results": {},
            "confidence_decisions": {},
            "final_results": [],
            "exam_result": None,
            "diagnosis": None,
            "error": None,
            "pause_reason": None,
            "resumable": False,
        }
        failure = self._identity_failure(state)
        if failure is not None:
            return {**patch, **failure}
        if not self._deps.snapshot.answers:
            return {
                **patch,
                **self._failure(
                    GRADING_WORKFLOW_MISSING_ANSWER,
                    "答卷没有任何题目，无法建立评分目标。",
                    current_node=LOAD_SUBMISSION,
                ),
            }
        if self._has_duplicate_orders():
            return {
                **patch,
                **self._failure(
                    GRADING_WORKFLOW_INVALID_INPUT,
                    "答卷题目题序重复，无法确定逐题推进顺序。",
                    current_node=LOAD_SUBMISSION,
                ),
            }
        if self._has_duplicate_answers():
            return {
                **patch,
                **self._failure(
                    GRADING_WORKFLOW_INVALID_INPUT,
                    "答卷答案标识重复，无法按答案对齐逐题结果。",
                    current_node=LOAD_SUBMISSION,
                ),
            }
        return {**patch, "submission_context": self._deps.snapshot.to_context()}

    async def _node_classify_question(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """`Classify Question`：按题序选中目标并写回身份（不参与分数判定）。"""

        target = self._target_for_order(state.get("current_answer_order"))
        if target is None:
            return {
                **cleared_slots(),
                **self._failure(
                    GRADING_WORKFLOW_MISSING_ANSWER,
                    "按题序找不到对应题目，拒绝继续评分。",
                    current_node=CLASSIFY_QUESTION,
                ),
            }
        try:
            self._mode_for(target.question_type)
        except GradingWorkflowError as error:
            return {
                **cleared_slots(),
                **self._failure(
                    str(error.error_code),
                    str(error),
                    current_node=CLASSIFY_QUESTION,
                ),
            }
        return {
            **cleared_slots(),
            "current_answer_id": target.answer_id,
            "current_answer_order": target.order,
            "question_type": target.question_type,
            "current_node": CLASSIFY_QUESTION,
            "status": WorkflowStatus.RUNNING,
        }

    async def _node_objective_rule_grade(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """`Objective Rule Grade`：只调用 T069 逐题入口的确定性客观题路径。"""

        return await self._grade_current(state, current_node=OBJECTIVE_RULE_GRADE)

    async def _node_subjective_retrieve_grade(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """`Subjective Retrieve/Grade`：检索、重排与结构化评分由 T069/评分器独占完成。"""

        return await self._grade_current(state, current_node=SUBJECTIVE_RETRIEVE_GRADE)

    async def _grade_current(
        self,
        state: Mapping[str, Any],
        *,
        current_node: str,
    ) -> dict[str, Any]:
        """逐题评分：调用 ``grade_answer_async``，并把输出交接进工作流状态。"""

        target = self._target_for_answer(
            state.get("current_answer_id"),
            order=state.get("current_answer_order"),
        )
        if target is None:
            return self._failure(
                GRADING_WORKFLOW_MISSING_ANSWER,
                "当前答案标识与答卷快照不一致，拒绝评分。",
                current_node=current_node,
            )
        try:
            mode = self._mode_for(target.question_type)
        except GradingWorkflowError as error:
            return self._failure(
                str(error.error_code),
                str(error),
                current_node=current_node,
            )
        session, owned = self._acquire_session(mode)
        try:
            invocation = await self._deps.agent.grade_answer_async(
                self._deps.snapshot,
                target,
                request_id=str(state.get("request_id") or ""),
                workflow_id=str(state.get("workflow_id") or ""),
                session=session,
                settings=self._deps.settings,
            )
        finally:
            if owned and session is not None:
                close = getattr(session, "close", None)
                if callable(close):
                    close()
        try:
            patch = dict(
                grading_handoff(
                    invocation,
                    submission_id=self._deps.snapshot.submission_id,
                    current_answer_order=target.order,
                    current_node=current_node,
                )
            )
        except GradingHandoffError as error:
            return self._failure(
                GRADING_HANDOFF_INVALID_INPUT,
                str(error),
                current_node=current_node,
            )
        patch.update(
            self._record_result(
                state,
                invocation.output,
                answer_id=target.answer_id,
            )
        )
        return patch

    async def _node_structured_validation(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """`Structured Validation`：只复核 T069 已完成的校验结论，不重新解析载荷。"""

        result = state.get("grading_result")
        if not isinstance(result, GradingResult) or (
            result.validation_status != ValidationStatus.VALIDATED.value
        ):
            return self._failure(
                GRADING_WORKFLOW_RESULT_NOT_VALIDATED,
                "评分结果未通过结构化校验，不得进入成绩。",
                current_node=STRUCTURED_VALIDATION,
            )
        return {
            "validation_status": ValidationStatus.VALIDATED,
            "current_node": STRUCTURED_VALIDATION,
            "status": WorkflowStatus.RUNNING,
        }

    async def _node_confidence_check(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """`Confidence Check`：客观题直接接受；主观题必须有本次决策快照。"""

        result = state.get("grading_result")
        try:
            mode = self._mode_for(getattr(result, "question_type", None))
        except GradingWorkflowError as error:
            return self._failure(
                str(error.error_code),
                str(error),
                current_node=CONFIDENCE_CHECK,
            )
        if mode is GradingMode.OBJECTIVE:
            return {
                "review_status": ReviewStatus.NOT_REQUIRED,
                "current_node": CONFIDENCE_CHECK,
                "status": WorkflowStatus.RUNNING,
            }
        decision = state.get("confidence_decision")
        if not isinstance(decision, ConfidenceDecisionDTO):
            return self._failure(
                GRADING_WORKFLOW_MISSING_DECISION,
                "主观题缺少本次置信度决策快照，无法判断是否需要人工复核。",
                current_node=CONFIDENCE_CHECK,
            )
        if self._needs_human_review(state) or decision.requires_review:
            # 暂停事实必须在中断之前写入：`interrupt()` 所在节点的返回增量会被丢弃。
            answer_id = str(state.get("current_answer_id") or "")
            return {
                "review_status": ReviewStatus.PENDING_REVIEW,
                "current_node": PENDING_REVIEW,
                "status": WorkflowStatus.PAUSED,
                "pause_reason": f"第 {answer_id} 题评分结果需要教师复核，工作流已暂停。",
                "resumable": self._interrupt_enabled,
                "error": None,
            }
        return {
            "review_status": ReviewStatus.NOT_REQUIRED,
            "current_node": CONFIDENCE_CHECK,
            "status": WorkflowStatus.RUNNING,
        }

    async def _node_accept(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """`Accept`：高置信度结果直接接受（集合更新已在评分节点完成）。"""

        return {
            "current_node": ACCEPT,
            "review_status": ReviewStatus.NOT_REQUIRED,
            "status": WorkflowStatus.RUNNING,
            "pause_reason": None,
            "resumable": False,
            "error": None,
        }

    async def _node_pending_review(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """`Pending Review`：真正中断等待人工复核；已有教师结论时才继续推进。"""

        answer_id = str(state.get("current_answer_id") or "")
        if not self._needs_human_review(state):
            decisions = state.get("confidence_decisions") or {}
            decision = decisions.get(answer_id) if isinstance(decisions, Mapping) else None
            authoritative = (
                decision.review_status
                if isinstance(decision, ConfidenceDecisionDTO)
                else ReviewStatus.NOT_REQUIRED
            )
            return {
                "current_node": PENDING_REVIEW,
                "status": WorkflowStatus.RUNNING,
                "review_status": authoritative,
                "pause_reason": None,
                "resumable": False,
                "error": None,
            }
        result = state.get("grading_result")
        reason = state.get("pause_reason") or (
            f"第 {answer_id} 题评分结果需要教师复核，工作流已暂停。"
        )
        if not isinstance(reason, str):  # pragma: no cover - 状态校验保证为文本
            reason = f"第 {answer_id} 题评分结果需要教师复核，工作流已暂停。"
        if self._interrupt_enabled:
            # 原生中断：首次经过不返回；恢复时返回教师/调度侧传入的值（本批不解释，T074 负责）。
            interrupt(
                {
                    "answer_id": answer_id,
                    "pause_reason": reason,
                    "confidence": getattr(result, "confidence", None),
                    "review_status": ReviewStatus.PENDING_REVIEW.value,
                }
            )
        return {
            "current_node": PENDING_REVIEW,
            "status": WorkflowStatus.PAUSED,
            "review_status": ReviewStatus.PENDING_REVIEW,
            "pause_reason": reason,
            "resumable": self._interrupt_enabled,
            "error": None,
        }

    async def _node_reviewer_agent(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """`Reviewer Agent`：只产出复核建议，**不**解除人工复核要求。"""

        reviewer = self._deps.reviewer if self._deps.reviewer is not None else ReviewerAgent()
        answer_id = str(state.get("current_answer_id") or "")
        target = self._target_for_answer(answer_id)
        invocation = reviewer.review(
            state.get("grading_result"),
            state.get("confidence_decision"),
            request_id=str(state.get("request_id") or ""),
            workflow_id=str(state.get("workflow_id") or ""),
            question_type=getattr(target, "question_type", None),
            answer_id=answer_id or None,
            submission_id=self._deps.snapshot.submission_id,
            max_score=getattr(target, "max_score", None),
        )
        output = invocation.output
        if output.error is not None:
            return self._failure(
                str(output.error.error_code),
                str(output.error.message),
                current_node=REVIEWER_AGENT,
                keep_results=True,
            )
        try:
            patch = dict(
                reviewer_handoff(
                    invocation,
                    submission_id=self._deps.snapshot.submission_id,
                    current_answer_id=answer_id,
                )
            )
        except GradingHandoffError as error:
            return self._failure(
                GRADING_HANDOFF_INVALID_INPUT,
                str(error),
                current_node=REVIEWER_AGENT,
                keep_results=True,
            )
        patch["error"] = None
        return patch

    async def _node_regrade(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """`Re-grade`：在预算内回到该题的评分节点；重评不清除待复核事实。"""

        retry_count = int(state.get("retry_count") or 0)
        if retry_count >= self._deps.max_regrades:
            return self._failure(
                GRADING_WORKFLOW_REGRADE_BUDGET_EXHAUSTED,
                (
                    f"重评预算（每题 {self._deps.max_regrades} 次）已用尽，"
                    "仍无法形成可接受结果。"
                ),
                current_node=REGRADE,
                keep_results=True,
            )
        target = self._target_for_answer(
            state.get("current_answer_id"),
            order=state.get("current_answer_order"),
        )
        if target is None:
            return self._failure(
                GRADING_WORKFLOW_MISSING_ANSWER,
                "重评时找不到对应题目，拒绝继续。",
                current_node=REGRADE,
                keep_results=True,
            )
        try:
            regrade_target_node(target.question_type)
        except GradingHandoffError as error:
            return self._failure(
                GRADING_WORKFLOW_UNSUPPORTED_QUESTION_TYPE,
                str(error),
                current_node=REGRADE,
                keep_results=True,
            )
        return {
            **cleared_slots(),
            "current_answer_id": target.answer_id,
            "current_answer_order": target.order,
            "question_type": target.question_type,
            "retry_count": retry_count + 1,
            "current_node": REGRADE,
            "status": WorkflowStatus.RUNNING,
            "pause_reason": None,
            "resumable": False,
            "error": None,
        }

    async def _node_next_answer(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """`Next Answer`：换题时清空槽位并推进题序；无下一题则进入统一结果。"""

        orders = [target.order for target in self._targets()]
        current = state.get("current_answer_order")
        position = orders.index(current) if current in orders else -1
        next_order = orders[position + 1] if 0 <= position < len(orders) - 1 else None
        return {
            **cleared_slots(),
            "current_answer_order": next_order,
            "current_node": NEXT_ANSWER,
            "status": WorkflowStatus.RUNNING,
            "pause_reason": None,
            "resumable": False,
            "error": None,
        }

    async def _node_unified_result(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """`Unified Result`：汇总整卷（``aggregate`` 一次）；未形成最终成绩时明确暂停。"""

        try:
            patch = dict(
                unified_result_patch(
                    state,
                    self._deps.snapshot,
                    aggregator=self._aggregator,
                )
            )
        except GradingHandoffError as error:
            return self._failure(
                GRADING_WORKFLOW_AGGREGATION_FAILED,
                str(error),
                current_node=UNIFIED_RESULT,
            )
        except Exception:  # noqa: BLE001 - M3 汇总错误统一收敛为脱敏失败，不返回部分成绩
            return self._failure(
                GRADING_WORKFLOW_AGGREGATION_FAILED,
                "整卷汇总失败，不返回部分成绩。",
                current_node=UNIFIED_RESULT,
            )
        patch.update({"current_node": UNIFIED_RESULT, "status": WorkflowStatus.RUNNING, "error": None})
        if not diagnosis_allowed({**state, **patch}):
            patch.update(
                {
                    "status": WorkflowStatus.PAUSED,
                    "pause_reason": "整卷尚未形成最终成绩（存在待复核或未接受题目）。",
                    "resumable": self._interrupt_enabled,
                }
            )
        return patch

    async def _node_generate_diagnosis(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """`Generate Diagnosis`：M3 诊断服务；未就绪/失败时保留整卷结果并显式报告。"""

        exam_result = state.get("exam_result")
        service = self._deps.diagnosis_service
        if not diagnosis_allowed(state) or service is None or exam_result is None:
            return {
                "current_node": GENERATE_DIAGNOSIS,
                "status": WorkflowStatus.PAUSED,
                "error": AgentError(
                    error_code=GRADING_WORKFLOW_DIAGNOSIS_FAILED,
                    message="诊断服务未就绪或缺少最终整卷结果，未生成诊断报告。",
                    retryable=False,
                ),
                "pause_reason": "诊断未就绪：整卷结果尚未最终确认或诊断服务未接线。",
                "resumable": self._interrupt_enabled,
                "diagnosis": None,
            }
        try:
            report = await service.generate(exam_result)
        except Exception:  # noqa: BLE001 - 统一收敛为脱敏失败，保留整卷结果
            return {
                "current_node": GENERATE_DIAGNOSIS,
                "status": WorkflowStatus.PAUSED,
                "error": AgentError(
                    error_code=GRADING_WORKFLOW_DIAGNOSIS_FAILED,
                    message="诊断生成失败，已保留已形成的整卷结果。",
                    retryable=False,
                ),
                "pause_reason": "诊断生成失败，整卷结果保留，等待重试。",
                "resumable": self._interrupt_enabled,
                "diagnosis": None,
            }
        return {
            "diagnosis": report,
            "current_node": GENERATE_DIAGNOSIS,
            "status": WorkflowStatus.COMPLETED,
            "error": None,
            "pause_reason": None,
            "resumable": False,
            "final_results": list(exam_result.items) if exam_result.is_final else [],
        }

    # ------------------------------------------------------------------ 条件边

    def _route_after_load(self, state: Mapping[str, Any]) -> str:
        return END if state.get("error") is not None else CLASSIFY_QUESTION

    def _route_by_question_type(self, state: Mapping[str, Any]) -> str:
        """客观题走规则评分，主观题走检索与评分（``CLASSIFY_EDGES``）。"""

        if state.get("error") is not None:
            return END
        try:
            mode = self._mode_for(state.get("question_type"))
        except GradingWorkflowError:
            return END
        return CLASSIFY_EDGES[mode]

    def _route_after_grading(self, state: Mapping[str, Any]) -> str:
        return END if state.get("error") is not None else STRUCTURED_VALIDATION

    def _route_after_validation(self, state: Mapping[str, Any]) -> str:
        return END if state.get("error") is not None else CONFIDENCE_CHECK

    def _route_by_confidence(self, state: Mapping[str, Any]) -> str:
        if state.get("error") is not None:
            return END
        return (
            PENDING_REVIEW
            if state.get("review_status") == ReviewStatus.PENDING_REVIEW
            else ACCEPT
        )

    def _route_after_pending(self, state: Mapping[str, Any]) -> str:
        """仍需人工复核时先取复核建议；已有教师结论时直接推进。"""

        return NEXT_ANSWER if not self._needs_human_review(state) else REVIEWER_AGENT

    def _route_after_reviewer(self, state: Mapping[str, Any]) -> str:
        """复核建议的分支：重评 → 回到评分；仍需人工复核 → 停在待复核。"""

        if state.get("error") is not None:
            return END
        if state.get("current_node") == REGRADE:
            return REGRADE
        answer_id = str(state.get("current_answer_id") or "")
        decisions = state.get("confidence_decisions") or {}
        decision = decisions.get(answer_id) if isinstance(decisions, Mapping) else None
        result = state.get("grading_result")
        accepted = isinstance(result, GradingResult) and may_enter_final_results(
            result,
            decision=decision if isinstance(decision, ConfidenceDecisionDTO) else None,
        )
        if accepted and not self._needs_human_review({**state, "current_answer_id": None}):
            return NEXT_ANSWER
        # 无恢复支撑时不重新抬起中断（否则会在待复核与复核之间无限往返），直接结束本次运行。
        return PENDING_REVIEW if self._interrupt_enabled else END

    def _route_after_regrade(self, state: Mapping[str, Any]) -> str:
        if state.get("error") is not None:
            return END
        try:
            return regrade_target_node(state.get("question_type"))
        except GradingHandoffError:
            return END

    def _route_after_next_answer(self, state: Mapping[str, Any]) -> str:
        if state.get("error") is not None:
            return END
        return UNIFIED_RESULT if state.get("current_answer_order") is None else CLASSIFY_QUESTION

    def _route_after_unified(self, state: Mapping[str, Any]) -> str:
        if state.get("error") is not None:
            return END
        return GENERATE_DIAGNOSIS if diagnosis_allowed(state) else END


__all__ = [
    "DEFAULT_MAX_REGRADES_PER_ANSWER",
    "GRADING_WORKFLOW_AGGREGATION_FAILED",
    "GRADING_WORKFLOW_ASYNC_REQUIRED",
    "GRADING_WORKFLOW_DIAGNOSIS_FAILED",
    "GRADING_WORKFLOW_IDENTITY_MISMATCH",
    "GRADING_WORKFLOW_INVALID_INPUT",
    "GRADING_WORKFLOW_MISSING_ANSWER",
    "GRADING_WORKFLOW_MISSING_DECISION",
    "GRADING_WORKFLOW_REGRADE_BUDGET_EXHAUSTED",
    "GRADING_WORKFLOW_RESULT_NOT_VALIDATED",
    "GRADING_WORKFLOW_UNSUPPORTED_QUESTION_TYPE",
    "GRADING_WORKFLOW_VERSION",
    "LIST_SLOT_FIELDS",
    "GradingWorkflow",
    "GradingWorkflowDeps",
    "GradingWorkflowError",
    "GradingWorkflowResult",
    "cleared_slots",
    "confidence_decision_from_snapshot",
]
