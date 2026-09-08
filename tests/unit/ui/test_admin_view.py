"""T021 管理员 Gradio 视图测试。"""

from __future__ import annotations

from datetime import UTC, datetime

import gradio as gr

from backend.app.domain.enums import UserRole
from backend.app.services.admin_service import (
    AdminComponentStatus,
    AdminSystemStatus,
    AdminUserSummary,
)
from backend.app.ui.admin_view import (
    _status_markdown,
    _user_rows,
    create_admin_view,
)


def test_admin_view_builds_and_formats_user_rows() -> None:
    """管理员视图应能构建并展示脱敏用户摘要。"""

    with gr.Blocks():
        view = create_admin_view()
    user = AdminUserSummary(
        id="user-id",
        username="admin",
        email="admin@example.com",
        is_active=True,
        roles=(UserRole.ADMIN.value,),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert view.panel.visible is False
    assert _user_rows([user]) == [
        [
            "user-id",
            "admin",
            "admin@example.com",
            "启用",
            "Admin",
            "2026-01-01T00:00:00+00:00",
        ]
    ]


def test_admin_view_formats_component_status_in_chinese() -> None:
    """运行状态面板应输出中文组件状态和用户统计。"""

    system_status = AdminSystemStatus(
        checked_at=datetime(2026, 1, 1, tzinfo=UTC),
        overall_healthy=False,
        database=AdminComponentStatus(name="数据库", healthy=True, detail="连接正常。"),
        redis=AdminComponentStatus(
            name="Redis", healthy=False, detail="Redis 未就绪。"
        ),
        user_count=3,
        active_user_count=2,
        role_count=3,
        users_by_role={"Teacher": 1, "Student": 1, "Admin": 1},
    )

    rendered = _status_markdown(system_status)

    assert "系统状态：异常" in rendered
    assert "数据库：正常" in rendered
    assert "Redis：异常" in rendered
    assert "Teacher 1 人" in rendered
