"""阅卷工作流运行记录持久化模型。

``WorkflowRun`` 保存自动阅卷工作流的运行事实，供检查点、暂停与恢复使用：

- ``workflow_id``：工作流实例标识，唯一。
- ``request_id``：plan §7 要求贯穿并写入运行记录的追踪标识，非空。
- ``submission_id``：本次运行处理的答卷；答卷删除时运行记录一并级联删除。
- ``current_node``/``current_answer_id``/``exam_result_id``：流程尚未到达对应阶段时为空；
  所指的 ``Answer`` 或 ``ExamResult`` 被删除时只清空指针（``ON DELETE SET NULL``），
  保留运行历史与检查点。
- ``status`` 复用 :class:`backend.app.domain.enums.WorkflowStatus`，与 Trace 状态
  （``AgentRun.status``）分离，避免把“任务执行完成”当作“成绩已最终确认”。
- ``checkpoint``、``pause_reason``、``retry_count``、``resumable`` 如实记录恢复所需事实；
  不可得的信息保存 NULL，不填占位值。
- 时间戳由 :class:`backend.app.models.base.TimestampMixin` 提供。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.domain.enums import WorkflowStatus
from backend.app.models.base import (
    Base,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    enum_type,
)


class WorkflowRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一次阅卷工作流运行：当前阶段、检查点与恢复状态。"""

    __tablename__ = "workflow_runs"
    __table_args__ = (
        UniqueConstraint("workflow_id", name="uq_workflow_runs_workflow_id"),
        CheckConstraint(
            "retry_count >= 0",
            name="ck_workflow_runs_retry_count_non_negative",
        ),
    )

    workflow_id: Mapped[str] = mapped_column(String(64), nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    submission_id: Mapped[UUID] = mapped_column(
        ForeignKey("submissions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    current_node: Mapped[str | None] = mapped_column(String(64))
    current_answer_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("answers.id", ondelete="SET NULL")
    )
    status: Mapped[WorkflowStatus] = mapped_column(
        enum_type(WorkflowStatus, "workflow_status"),
        default=WorkflowStatus.QUEUED,
        nullable=False,
    )
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    pause_reason: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    resumable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    exam_result_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("exam_results.id", ondelete="SET NULL")
    )


__all__ = ["WorkflowRun"]
