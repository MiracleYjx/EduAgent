"""Agent 调用包装：承载跨 Agent 的追溯标识（T069/T070 共用）。

T065 的 :class:`~backend.app.ai.agents.state.AgentOutput` 是结果契约，不含 ``request_id`` /
``workflow_id``；而 T063 ``AgentRun`` 与 T072/T074 的 Workflow 都需要这两个标识。本模块提供
不可变包装，保证追溯标识在 Agent 边界不被静默丢弃，同时**不修改** T065 的状态定义。

约定：

- ``request_id`` 必填且非空（plan §7 要求贯穿请求）；空白即 :class:`AgentInvocationError`。
- ``workflow_id`` 允许为 ``None``（独立 Agent 调用），但必须原样带回，不自动生成。
- 本模块不落库：``AgentRun`` 由调用方（T072/T074/T063）按同一组 ID 创建。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Final

from backend.app.ai.agents.state import AgentOutput

#: 缺少贯穿请求的追溯标识。
AGENT_MISSING_REQUEST_ID: Final[str] = "AGENT_MISSING_REQUEST_ID"


class AgentInvocationError(RuntimeError):
    """追溯上下文缺失或非法；属于调用方编程错误，不做静默兜底。"""

    error_code: ClassVar[str] = AGENT_MISSING_REQUEST_ID

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.error_code}：{detail}")
        self.detail = detail


def _normalize_request_id(value: object) -> str:
    """校验并规范化 ``request_id``；空白或非文本显式失败。"""

    if not isinstance(value, str) or not value.strip():
        raise AgentInvocationError("缺少贯穿请求的 request_id，拒绝丢弃追溯标识。")
    return value.strip()


def _normalize_workflow_id(value: object) -> str | None:
    """校验并规范化 ``workflow_id``；空白视为 ``None``（独立 Agent 调用）。"""

    if value is None:
        return None
    if not isinstance(value, str):
        raise AgentInvocationError("workflow_id 必须是文本或 None。")
    text = value.strip()
    return text or None


def normalize_trace_context(
    request_id: str,
    workflow_id: str | None,
) -> tuple[str, str | None]:
    """校验并规范化追溯上下文，返回 ``(request_id, workflow_id)``。"""

    return _normalize_request_id(request_id), _normalize_workflow_id(workflow_id)


@dataclass(frozen=True, slots=True)
class AgentInvocation:
    """一次 Agent 调用的结果与追溯标识。"""

    request_id: str
    workflow_id: str | None
    output: AgentOutput

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _normalize_request_id(self.request_id))
        object.__setattr__(self, "workflow_id", _normalize_workflow_id(self.workflow_id))


__all__ = [
    "AGENT_MISSING_REQUEST_ID",
    "AgentInvocation",
    "AgentInvocationError",
    "normalize_trace_context",
]
