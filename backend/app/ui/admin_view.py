"""管理员 Gradio 视图及其用户、角色和运行状态操作。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import gradio as gr
from sqlalchemy.exc import SQLAlchemyError

from backend.app.core.database import get_session_factory
from backend.app.domain.enums import UserRole
from backend.app.domain.permissions import PermissionDeniedError, normalize_role
from backend.app.services.admin_service import (
    AdminRoleSummary,
    AdminService,
    AdminServiceError,
    AdminSystemStatus,
    AdminUserSummary,
)

USER_TABLE_HEADERS = ("用户 ID", "用户名", "邮箱", "状态", "角色", "创建时间")
ROLE_CHOICES = [role.value for role in UserRole]
_GENERIC_ERROR = "操作失败，请稍后重试。"


@dataclass(frozen=True)
class AdminView:
    """管理员视图中可被主应用控制的组件集合。"""

    panel: gr.Column
    users_table: gr.Dataframe
    status_panel: gr.Markdown
    message: gr.Markdown
    users_section: gr.Column
    roles_section: gr.Column
    status_section: gr.Column


def _empty_state() -> dict[str, Any]:
    """返回可供独立视图使用的空登录状态。"""

    return {"access_token": "", "roles": [], "username": ""}


def _ensure_admin(state: Mapping[str, Any]) -> None:
    """检查 Gradio 会话是否已登录且拥有管理员角色。"""

    if not state.get("access_token"):
        raise PermissionDeniedError("请先登录。")
    try:
        roles = {normalize_role(role) for role in state.get("roles", [])}
    except (TypeError, ValueError) as exc:
        raise PermissionDeniedError("当前账号无权访问管理员功能。") from exc
    if UserRole.ADMIN not in roles:
        raise PermissionDeniedError("当前账号无权访问管理员功能。")


def _format_error(error: BaseException) -> str:
    """将内部异常转换成管理员可理解且不泄露敏感信息的提示。"""

    if isinstance(error, PermissionDeniedError):
        return str(error) or "当前账号无权执行此操作。"
    if isinstance(error, AdminServiceError):
        return str(error) or _GENERIC_ERROR
    if isinstance(error, SQLAlchemyError):
        return "系统暂时无法连接数据库，请稍后重试。"
    if isinstance(error, ValueError):
        return f"输入有误：{error or '请检查输入内容。'}"
    return _GENERIC_ERROR


def _user_rows(users: Sequence[AdminUserSummary]) -> list[list[str]]:
    """把用户摘要转换为 Gradio 表格行。"""

    return [
        [
            user.id,
            user.username,
            user.email,
            "启用" if user.is_active else "停用",
            "、".join(user.roles),
            user.created_at.isoformat(),
        ]
        for user in users
    ]


def _role_rows(roles: Sequence[AdminRoleSummary]) -> list[list[str | int]]:
    """把角色摘要转换为 Gradio 表格行。"""

    return [
        [role.name.value, role.description or "", role.user_count] for role in roles
    ]


def _status_markdown(system_status: AdminSystemStatus) -> str:
    """把运行状态摘要转换为中文 Markdown。"""

    overall = "正常" if system_status.overall_healthy else "异常"
    database = "正常" if system_status.database.healthy else "异常"
    redis = "正常" if system_status.redis.healthy else "异常"
    role_lines = (
        "、".join(
            f"{role} {count} 人" for role, count in system_status.users_by_role.items()
        )
        or "暂无角色统计"
    )
    return (
        f"### 系统状态：{overall}\n"
        f"- 检查时间：{system_status.checked_at.isoformat()}\n"
        f"- 数据库：{database}（{system_status.database.detail}）\n"
        f"- Redis：{redis}（{system_status.redis.detail}）\n"
        f"- 用户：{system_status.user_count if system_status.user_count is not None else '未知'}"
        f"，启用用户：{system_status.active_user_count if system_status.active_user_count is not None else '未知'}\n"
        f"- 角色：{system_status.role_count if system_status.role_count is not None else '未知'}"
        f"，分布：{role_lines}"
    )


def refresh_admin_users(state: Mapping[str, Any]) -> tuple[list[list[str]], str]:
    """读取用户列表并返回表格数据和操作提示。"""

    try:
        _ensure_admin(state)
        with get_session_factory()() as session:
            users = AdminService(session).list_users()
        return _user_rows(users), f"已加载 {len(users)} 个用户。"
    except (
        AdminServiceError,
        PermissionDeniedError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return [], _format_error(error)


def refresh_admin_roles(state: Mapping[str, Any]) -> tuple[list[list[str | int]], str]:
    """读取角色列表并返回表格数据和操作提示。"""

    try:
        _ensure_admin(state)
        with get_session_factory()() as session:
            roles = AdminService(session).list_roles()
        return _role_rows(roles), f"已加载 {len(roles)} 个角色。"
    except (
        AdminServiceError,
        PermissionDeniedError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return [], _format_error(error)


def refresh_admin_status(state: Mapping[str, Any]) -> str:
    """读取系统状态并返回中文状态面板。"""

    try:
        _ensure_admin(state)
        with get_session_factory()() as session:
            system_status = AdminService(session).get_system_status()
        return _status_markdown(system_status)
    except (
        AdminServiceError,
        PermissionDeniedError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return _format_error(error)


def create_admin_user(
    username: str,
    email: str,
    password: str,
    roles: Sequence[str] | str | None,
    is_active: bool,
    state: Mapping[str, Any],
) -> tuple[list[list[str]], str]:
    """创建用户并刷新管理员用户表格。"""

    try:
        _ensure_admin(state)
        with get_session_factory()() as session:
            service = AdminService(session)
            created = service.create_user(
                username=username,
                email=email,
                password=password,
                roles=roles or [],
                is_active=is_active,
            )
            users = service.list_users()
        return _user_rows(users), f"用户“{created.username}”创建成功。"
    except (
        AdminServiceError,
        PermissionDeniedError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return [], _format_error(error)


def update_admin_user(
    user_id: str,
    username: str,
    email: str,
    password: str,
    is_active: bool,
    state: Mapping[str, Any],
) -> tuple[list[list[str]], str]:
    """更新用户资料并刷新管理员用户表格。"""

    try:
        _ensure_admin(state)
        with get_session_factory()() as session:
            service = AdminService(session)
            updated = service.update_user(
                user_id,
                username=username or None,
                email=email or None,
                password=password or None,
                is_active=is_active,
            )
            users = service.list_users()
        return _user_rows(users), f"用户“{updated.username}”更新成功。"
    except (
        AdminServiceError,
        PermissionDeniedError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return [], _format_error(error)


def set_admin_user_roles(
    user_id: str,
    roles: Sequence[str] | str | None,
    state: Mapping[str, Any],
) -> tuple[list[list[str]], str]:
    """替换用户角色并刷新管理员用户表格。"""

    try:
        _ensure_admin(state)
        with get_session_factory()() as session:
            service = AdminService(session)
            updated = service.set_user_roles(user_id, roles or [])
            users = service.list_users()
        return _user_rows(users), f"用户“{updated.username}”的角色已更新。"
    except (
        AdminServiceError,
        PermissionDeniedError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return [], _format_error(error)


def delete_admin_user(
    user_id: str,
    state: Mapping[str, Any],
) -> tuple[list[list[str]], str]:
    """删除用户并刷新管理员用户表格。"""

    try:
        _ensure_admin(state)
        with get_session_factory()() as session:
            service = AdminService(session)
            service.delete_user(user_id)
            users = service.list_users()
        return _user_rows(users), "用户删除成功。"
    except (
        AdminServiceError,
        PermissionDeniedError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return [], _format_error(error)


def create_admin_view(session_state: Any | None = None) -> AdminView:
    """创建管理员用户管理、角色管理和运行状态面板。"""

    state = session_state or gr.State(_empty_state())
    with gr.Column(visible=False) as panel:
        with gr.Column() as users_section:
            gr.Markdown("## 用户管理")
            refresh_users = gr.Button("刷新用户", variant="secondary")
            users_table = gr.Dataframe(
                headers=list(USER_TABLE_HEADERS),
                datatype=["str"] * len(USER_TABLE_HEADERS),
                value=[],
                interactive=False,
                label="用户列表",
            )
            with gr.Row():
                user_id = gr.Textbox(label="用户 ID")
                username = gr.Textbox(label="用户名")
                email = gr.Textbox(label="邮箱")
            with gr.Row():
                password = gr.Textbox(label="密码", type="password")
                roles = gr.CheckboxGroup(
                    choices=[
                        ("教师", UserRole.TEACHER.value),
                        ("学生", UserRole.STUDENT.value),
                        ("管理员", UserRole.ADMIN.value),
                    ],
                    label="新用户角色",
                    value=[UserRole.STUDENT.value],
                )
                is_active = gr.Checkbox(label="启用用户", value=True)
            with gr.Row():
                create_button = gr.Button("创建用户", variant="primary")
                update_button = gr.Button("保存用户")
                delete_button = gr.Button("删除用户", variant="stop")
        with gr.Column() as roles_section:
            gr.Markdown("## 角色管理")
            refresh_roles = gr.Button("刷新角色", variant="secondary")
            roles_table = gr.Dataframe(
                headers=["角色", "说明", "用户数量"],
                datatype=["str", "str", "number"],
                value=[],
                interactive=False,
                label="角色列表",
            )
            role_user_id = gr.Textbox(label="用户 ID")
            assigned_roles = gr.CheckboxGroup(
                choices=[
                    ("教师", UserRole.TEACHER.value),
                    ("学生", UserRole.STUDENT.value),
                    ("管理员", UserRole.ADMIN.value),
                ],
                label="分配角色",
                value=[],
            )
            roles_button = gr.Button("保存角色", variant="primary")
        with gr.Column() as status_section:
            gr.Markdown("## 运行状态")
            refresh_status = gr.Button("刷新运行状态", variant="primary")
            status_panel = gr.Markdown("尚未加载运行状态。")
        message = gr.Markdown()
        refresh_users.click(
            fn=refresh_admin_users,
            inputs=[state],
            outputs=[users_table, message],
            show_progress="hidden",
        )
        refresh_roles.click(
            fn=refresh_admin_roles,
            inputs=[state],
            outputs=[roles_table, message],
            show_progress="hidden",
        )
        create_button.click(
            fn=create_admin_user,
            inputs=[username, email, password, roles, is_active, state],
            outputs=[users_table, message],
            show_progress="hidden",
        )
        update_button.click(
            fn=update_admin_user,
            inputs=[user_id, username, email, password, is_active, state],
            outputs=[users_table, message],
            show_progress="hidden",
        )
        roles_button.click(
            fn=set_admin_user_roles,
            inputs=[role_user_id, assigned_roles, state],
            outputs=[users_table, message],
            show_progress="hidden",
        )
        delete_button.click(
            fn=delete_admin_user,
            inputs=[user_id, state],
            outputs=[users_table, message],
            show_progress="hidden",
        )
        refresh_status.click(
            fn=refresh_admin_status,
            inputs=[state],
            outputs=status_panel,
            show_progress="hidden",
        )

    return AdminView(
        panel=panel,
        users_table=users_table,
        status_panel=status_panel,
        message=message,
        users_section=users_section,
        roles_section=roles_section,
        status_section=status_section,
    )


build_admin_view = create_admin_view


__all__ = [
    "ROLE_CHOICES",
    "USER_TABLE_HEADERS",
    "AdminView",
    "build_admin_view",
    "create_admin_user",
    "create_admin_view",
    "delete_admin_user",
    "refresh_admin_roles",
    "refresh_admin_status",
    "refresh_admin_users",
    "set_admin_user_roles",
    "update_admin_user",
]
