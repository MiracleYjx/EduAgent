"""Agent 运行追踪持久化模型。

``AgentRun`` 保存一次 Agent（或节点）调用的追踪事实，供后续采集、追踪视图与保留策略使用：

- ``request_id``：plan §7 要求必填并贯穿请求，独立 Agent 调用同样不得缺省。
- ``user_id``、``workflow_id`` 可为空：前者记录执行发起者（认证后填充），后者只在调用属于
  某次工作流时存在；缺少时保存 NULL，不填假值。``workflow_id`` 指向
  ``workflow_runs.workflow_id``（唯一列），该运行记录被删除时只清空指针，保留追踪历史。
- ``status``：Trace 状态取值 ``success``/``failure``/``pending_review``，由字符串列加 CHECK
  固化，刻意不复用 ``WorkflowStatus``，避免把“任务执行完成”当作“成绩已最终确认”。
- ``validation_status`` 复用 :class:`backend.app.domain.enums.ValidationStatus`，
  记录结构化输出校验结果。
- ``model``、``prompt_version``、Token 用量与耗时不可得时保存 NULL；耗时与 Token 计数不得为负。
- ``input_summary``、``output_summary``、``error_message`` 只保存脱敏摘要，
  不写 API Key、Authorization Header、完整 Prompt 或学生答案原文。
- 本批只定义存储契约，不提前实现采集、清理与脱敏框架；``created_at`` 供保留策略使用。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, foreign, mapped_column, relationship

from backend.app.domain.enums import ValidationStatus
from backend.app.models.base import (
    Base,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    enum_type,
)
from backend.app.models.workflow_run import WorkflowRun


class AgentRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一次 Agent 调用的追踪记录：上下文、校验状态、用量与脱敏错误摘要。"""

    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('success', 'failure', 'pending_review')",
            name="ck_agent_runs_status_trace_value",
        ),
        CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="ck_agent_runs_latency_non_negative",
        ),
        CheckConstraint(
            "(input_tokens IS NULL OR input_tokens >= 0) "
            "AND (output_tokens IS NULL OR output_tokens >= 0) "
            "AND (total_tokens IS NULL OR total_tokens >= 0)",
            name="ck_agent_runs_token_counts_non_negative",
        ),
    )

    agent_type: Mapped[str] = mapped_column(String(64), nullable=False)
    workflow_id: Mapped[str | None] = mapped_column(
        ForeignKey("workflow_runs.workflow_id", ondelete="SET NULL"), index=True
    )
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    input_summary: Mapped[str | None] = mapped_column(Text)
    output_summary: Mapped[str | None] = mapped_column(Text)
    validation_status: Mapped[ValidationStatus | None] = mapped_column(
        enum_type(ValidationStatus, "agent_validation_status")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    model: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    error_retryable: Mapped[bool | None] = mapped_column(Boolean)
    workflow: Mapped[WorkflowRun | None] = relationship(
        WorkflowRun,
        primaryjoin=lambda: foreign(AgentRun.workflow_id) == WorkflowRun.workflow_id,
        viewonly=True,
    )


__all__ = ["AgentRun"]
