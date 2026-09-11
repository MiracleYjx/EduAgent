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
from backend.app.ui.layout_view import (
    UiStatus,
    bind_confirmation,
    empty_state,
    feedback,
    status_badge,
    status_label,
    table_options,
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
    overview_section: gr.Column | None = None


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
        message = str(error) or "当前账号无权执行此操作。"
    elif isinstance(error, AdminServiceError):
        message = str(error) or _GENERIC_ERROR
    elif isinstance(error, SQLAlchemyError):
        message = "系统暂时无法连接数据库，请稍后重试。"
    elif isinstance(error, ValueError):
        message = f"输入有误：{error or '请检查输入内容。'}"
    else:
        message = _GENERIC_ERROR
    return feedback(message, "error")


def _user_rows(users: Sequence[AdminUserSummary]) -> list[list[str]]:
    """把用户摘要转换为 Gradio 表格行。"""

    return [
        [
            user.id,
            user.username,
            user.email,
            status_badge(
                UiStatus.ACTIVE if user.is_active else UiStatus.INACTIVE,
                entity="ui",
            ),
            "、".join(status_label(role, entity="role") for role in user.roles),
            user.created_at.isoformat(),
        ]
        for user in users
    ]


def _role_rows(roles: Sequence[AdminRoleSummary]) -> list[list[str | int]]:
    """把角色摘要转换为 Gradio 表格行。"""

    return [
        [
            status_label(role.name, entity="role"),
            role.description or "",
            role.user_count,
        ]
        for role in roles
    ]


def _status_markdown(system_status: AdminSystemStatus) -> str:
    """把运行状态摘要转换为中文 Markdown。"""

    overall_state = (
        UiStatus.HEALTHY if system_status.overall_healthy else UiStatus.UNHEALTHY
    )
    database_state = (
        UiStatus.HEALTHY if system_status.database.healthy else UiStatus.UNHEALTHY
    )
    redis_state = (
        UiStatus.HEALTHY if system_status.redis.healthy else UiStatus.UNHEALTHY
    )
    overall = status_label(overall_state, entity="ui")
    database = status_label(database_state, entity="ui")
    redis = status_label(redis_state, entity="ui")
    role_lines = (
        "、".join(
            f"{status_label(role, entity='role')} {count} 人"
            for role, count in system_status.users_by_role.items()
        )
        or "暂无角色统计"
    )
    return (
        f"### 系统状态：{overall}\n"
        f"- 检查时间：{system_status.checked_at.isoformat()}\n"
        f"- 数据库：{database} {status_badge(database_state, entity='ui')}（{system_status.database.detail}）\n"
        f"- Redis：{redis} {status_badge(redis_state, entity='ui')}（{system_status.redis.detail}）\n"
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
        return _user_rows(users), (
            feedback(f"已加载 {len(users)} 个用户。", "success")
            if users
            else empty_state("暂无用户。")
        )
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
        return _role_rows(roles), (
            feedback(f"已加载 {len(roles)} 个角色。", "success")
            if roles
            else empty_state("暂无角色。")
        )
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
        return _user_rows(users), feedback(
            f"用户“{created.username}”创建成功。", "success"
        )
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
        return _user_rows(users), feedback(
            f"用户“{updated.username}”更新成功。", "success"
        )
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
        return _user_rows(users), feedback(
            f"用户“{updated.username}”的角色已更新。", "success"
        )
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
        return _user_rows(users), feedback("用户删除成功。", "success")
    except (
        AdminServiceError,
        PermissionDeniedError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return [], _format_error(error)


def deactivate_admin_user(
    target_name: str,
    user_id: str,
    state: Mapping[str, Any],
) -> tuple[list[list[str]], str]:
    """在确认区中停用指定用户，并重新读取真实用户列表。"""

    del target_name
    try:
        _ensure_admin(state)
        with get_session_factory()() as session:
            service = AdminService(session)
            updated = service.update_user(user_id, is_active=False)
            users = service.list_users()
        return _user_rows(users), feedback(
            f"用户“{updated.username}”已停用。", "success"
        )
    except (
        AdminServiceError,
        PermissionDeniedError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return [], _format_error(error)


def _user_records(users: Sequence[AdminUserSummary]) -> list[dict[str, Any]]:
    """保存表格当前顺序对应的用户摘要，供选行事件绑定内部标识。"""

    return [user.model_dump(mode="json") for user in users]


def _filter_users(
    users: Sequence[AdminUserSummary],
    username: str | None,
    role: str | None,
    active: str | None,
) -> list[AdminUserSummary]:
    """只在已授权的服务结果上执行展示筛选，不扩展管理员查询权限。"""

    name_keyword = (username or "").strip().casefold()
    role_value = (role or "").strip()
    active_value = (active or "").strip()
    result = [
        user
        for user in users
        if (not name_keyword or name_keyword in user.username.casefold())
        and (not role_value or role_value in {str(item) for item in user.roles})
        and (
            not active_value
            or (active_value == "启用" and user.is_active)
            or (active_value == "停用" and not user.is_active)
        )
    ]
    return result


def _overview_markdown(system_status: AdminSystemStatus) -> str:
    """渲染系统概览首行统计，数值全部来自 AdminService。"""

    def value(item: int | None) -> str:
        return str(item) if item is not None else "未知"

    return (
        "### 系统概览\n"
        f"- 用户总数：**{value(system_status.user_count)}**　"
        f"启用用户数：**{value(system_status.active_user_count)}**　"
        f"角色数：**{value(system_status.role_count)}**"
    )


def _dependency_markdown(system_status: AdminSystemStatus) -> str:
    """渲染运行状态页的数据库、缓存依赖健康列表。"""

    database = "正常" if system_status.database.healthy else "异常"
    redis = "正常" if system_status.redis.healthy else "异常"
    return (
        "### 依赖运行状态\n"
        f"- 数据库：{database}（{system_status.database.detail}）\n"
        f"- 缓存：{redis}（{system_status.redis.detail}）\n"
        f"- 检查时间：{system_status.checked_at.isoformat()}"
    )


def create_admin_view(session_state: Any | None = None) -> AdminView:
    """创建管理员四个独立工作区，并把列表选中项绑定到编辑表单。"""

    state = session_state or gr.State(_empty_state())
    role_choices = [
        ("教师", UserRole.TEACHER.value),
        ("学生", UserRole.STUDENT.value),
        ("管理员", UserRole.ADMIN.value),
    ]

    with gr.Column(visible=False) as panel:
        with gr.Column() as overview_section:
            gr.Markdown("## 系统概览")
            with gr.Row():
                overview_users = gr.Textbox(
                    label="用户总数", value="尚未加载", interactive=False
                )
                overview_active = gr.Textbox(
                    label="启用用户数", value="尚未加载", interactive=False
                )
                overview_roles = gr.Textbox(
                    label="角色数", value="尚未加载", interactive=False
                )
            overview_health = gr.Markdown(empty_state("尚未加载依赖状态。"))
            overview_refresh = gr.Button("刷新概览", variant="primary")

        with gr.Column() as users_section:
            gr.Markdown("## 用户管理")
            user_records = gr.State([])
            with gr.Row():
                name_filter = gr.Textbox(label="姓名搜索", placeholder="输入用户名")
                role_filter = gr.Dropdown(
                    label="角色筛选",
                    choices=[("全部角色", ""), *role_choices],
                    value="",
                )
                active_filter = gr.Dropdown(
                    label="启用状态",
                    choices=[("全部状态", ""), ("启用", "启用"), ("停用", "停用")],
                    value="",
                )
                refresh_users = gr.Button("刷新用户", variant="secondary", scale=0)
                new_user_button = gr.Button("新建用户", variant="primary", scale=0)
            with gr.Row():
                with gr.Column(scale=65, min_width=420):
                    users_table = gr.Dataframe(
                        headers=list(USER_TABLE_HEADERS),
                        datatype=["str"] * len(USER_TABLE_HEADERS),
                        value=[],
                        interactive=False,
                        label="用户列表（点击行选择用户）",
                        **table_options(USER_TABLE_HEADERS),
                    )
                with gr.Column(scale=35, min_width=320):
                    gr.Markdown("### 选中用户编辑")
                    edit_status = gr.Markdown(empty_state("尚未选择用户。"))
                    edit_target_name = gr.Textbox(
                        label="确认目标用户", interactive=False
                    )
                    user_id = gr.Textbox(
                        label="用户 ID", interactive=False, visible=False
                    )
                    username = gr.Textbox(label="用户名")
                    email = gr.Textbox(label="邮箱")
                    password = gr.Textbox(
                        label="新密码（留空则不修改）", type="password"
                    )
                    edit_roles = gr.CheckboxGroup(
                        choices=role_choices, label="角色", value=[], interactive=False
                    )
                    gr.Markdown("角色调整请前往“角色管理”页面。")
                    is_active = gr.Checkbox(label="启用用户", value=True)
                    with gr.Row():
                        update_button = gr.Button(
                            "保存用户", variant="primary", interactive=False
                        )
                        deactivate_button = gr.Button(
                            "停用用户", variant="stop", interactive=False
                        )
                        delete_button = gr.Button(
                            "删除用户", variant="stop", interactive=False
                        )

            with gr.Column(visible=False) as create_form:
                gr.Markdown("### 创建用户\n填写新用户资料；当前选中标识已清空。")
                with gr.Row():
                    create_username = gr.Textbox(label="用户名")
                    create_email = gr.Textbox(label="邮箱")
                    create_password = gr.Textbox(label="初始密码", type="password")
                with gr.Row():
                    create_roles = gr.CheckboxGroup(
                        choices=role_choices,
                        label="角色",
                        value=[UserRole.STUDENT.value],
                    )
                    create_active = gr.Checkbox(label="启用用户", value=True)
                    create_button = gr.Button("创建用户", variant="primary")
                    clear_create_button = gr.Button("清空表单")
                    cancel_create_button = gr.Button("取消创建")

        with gr.Column() as roles_section:
            gr.Markdown("## 角色管理")
            refresh_roles = gr.Button("刷新角色", variant="secondary")
            roles_table = gr.Dataframe(
                headers=["角色", "说明", "用户数量"],
                datatype=["str", "str", "number"],
                value=[],
                interactive=False,
                label="角色说明表",
                **table_options(("角色", "说明", "用户数量")),
            )
            role_user_name = gr.Textbox(label="选中用户", interactive=False)
            role_user_id = gr.Textbox(label="用户 ID", interactive=False, visible=False)
            assigned_roles = gr.CheckboxGroup(
                choices=role_choices, label="选中用户角色", value=[]
            )
            roles_button = gr.Button("保存角色", variant="primary", interactive=False)

        with gr.Column() as status_section:
            gr.Markdown("## 运行状态")
            refresh_status = gr.Button("刷新依赖状态", variant="primary")
            status_panel = gr.Markdown(empty_state("尚未加载运行状态。"))

        message = gr.Markdown(empty_state("尚未加载用户或角色。"))

        def query_users(
            current_state: Mapping[str, Any],
            name: str = "",
            role: str = "",
            active: str = "",
        ) -> tuple[list[list[str]], list[dict[str, Any]], str]:
            """读取服务结果，再在界面层应用筛选。"""

            try:
                _ensure_admin(current_state)
                with get_session_factory()() as session:
                    users = AdminService(session).list_users()
                visible = _filter_users(users, name, role, active)
                return (
                    _user_rows(visible),
                    _user_records(visible),
                    (
                        feedback(f"已加载 {len(visible)} 个用户。", "success")
                        if visible
                        else empty_state("暂无符合条件的用户。")
                    ),
                )
            except (
                AdminServiceError,
                PermissionDeniedError,
                SQLAlchemyError,
                TypeError,
                ValueError,
            ) as error:
                return [], [], _format_error(error)

        def refresh_users_view(
            name: str, role: str, active: str, current_state: Mapping[str, Any]
        ) -> tuple[list[list[str]], list[dict[str, Any]], str]:
            return query_users(current_state, name, role, active)

        def select_user(
            records: Sequence[Mapping[str, Any]], event: gr.SelectData
        ) -> dict[Any, Any]:
            """按表格行顺序填充编辑和角色两个面板。"""

            index = (
                event.index[0]
                if isinstance(event.index, (tuple, list))
                else event.index
            )
            if (
                not event.selected
                or not isinstance(index, int)
                or not 0 <= index < len(records)
            ):
                raise gr.Error("用户选择已失效，请重新选择。")
            record = dict(records[index])
            roles_value = list(record.get("roles") or [])
            return {
                edit_status: status_label(
                    UiStatus.ACTIVE if record.get("is_active") else UiStatus.INACTIVE,
                    entity="ui",
                ),
                edit_target_name: record.get("username", ""),
                user_id: record.get("id", ""),
                username: record.get("username", ""),
                email: record.get("email", ""),
                password: "",
                edit_roles: gr.update(value=roles_value),
                is_active: bool(record.get("is_active")),
                role_user_name: record.get("username", ""),
                role_user_id: record.get("id", ""),
                assigned_roles: gr.update(value=roles_value),
                update_button: gr.update(interactive=True),
                deactivate_button: gr.update(interactive=bool(record.get("is_active"))),
                delete_button: gr.update(interactive=True),
                roles_button: gr.update(interactive=True),
                create_form: gr.update(visible=False),
            }

        def start_create() -> dict[Any, Any]:
            """开启独立创建表单，并清空编辑与角色页的选中标识。"""

            return {
                create_form: gr.update(visible=True),
                create_username: "",
                create_email: "",
                create_password: "",
                create_roles: [UserRole.STUDENT.value],
                create_active: True,
                user_id: "",
                edit_target_name: "",
                username: "",
                email: "",
                password: "",
                edit_roles: [],
                is_active: True,
                edit_status: empty_state("正在创建新用户。"),
                role_user_name: "",
                role_user_id: "",
                assigned_roles: [],
                update_button: gr.update(interactive=False),
                deactivate_button: gr.update(interactive=False),
                delete_button: gr.update(interactive=False),
                roles_button: gr.update(interactive=False),
            }

        def cancel_create() -> dict[Any, Any]:
            """取消创建并清空新建表单，避免残留敏感输入。"""

            result = start_create()
            result[create_form] = gr.update(visible=False)
            return result

        def action_result(
            result: tuple[list[list[str]], str],
            current_state: Mapping[str, Any],
            name: str,
            role: str,
            active: str,
        ) -> tuple[list[list[str]], list[dict[str, Any]], str]:
            """将服务操作结果与当前筛选后的用户顺序重新同步。"""

            _, operation_message = result
            refreshed, records, _ = query_users(current_state, name, role, active)
            return refreshed, records, operation_message

        def create_and_refresh(
            new_name: str,
            new_email: str,
            new_password: str,
            new_roles: Sequence[str] | str | None,
            new_active: bool,
            current_state: Mapping[str, Any],
            name: str,
            role: str,
            active: str,
        ) -> tuple[list[list[str]], list[dict[str, Any]], str]:
            return action_result(
                create_admin_user(
                    new_name,
                    new_email,
                    new_password,
                    new_roles,
                    new_active,
                    current_state,
                ),
                current_state,
                name,
                role,
                active,
            )

        def update_and_refresh(
            identifier: str,
            edit_name: str,
            edit_email: str,
            new_password: str,
            enabled: bool,
            current_state: Mapping[str, Any],
            name: str,
            role: str,
            active: str,
        ) -> tuple[list[list[str]], list[dict[str, Any]], str]:
            try:
                _ensure_admin(current_state)
                with get_session_factory()() as session:
                    service = AdminService(session)
                    latest = service.get_user(identifier)
                    if latest.is_active and not enabled:
                        raise ValueError("停用用户需要确认，请点击“停用用户”按钮。")
                    updated = service.update_user(
                        identifier,
                        username=edit_name or None,
                        email=edit_email or None,
                        password=new_password or None,
                        is_active=enabled,
                    )
                rows, records, _ = query_users(current_state, name, role, active)
                return (
                    rows,
                    records,
                    feedback(f"用户“{updated.username}”已保存。", "success"),
                )
            except (
                AdminServiceError,
                PermissionDeniedError,
                SQLAlchemyError,
                TypeError,
                ValueError,
            ) as error:
                rows, records, _ = query_users(current_state, name, role, active)
                return rows, records, _format_error(error)

        def roles_and_refresh(
            identifier: str,
            selected_roles: Sequence[str] | str | None,
            current_state: Mapping[str, Any],
            name: str,
            role: str,
            active: str,
        ) -> tuple[list[list[str]], list[dict[str, Any]], str]:
            return action_result(
                set_admin_user_roles(identifier, selected_roles, current_state),
                current_state,
                name,
                role,
                active,
            )

        def delete_and_refresh(
            target_name: str,
            identifier: str,
            current_state: Mapping[str, Any],
            name: str,
            role: str,
            active: str,
        ) -> tuple[list[list[str]], list[dict[str, Any]], str]:
            del target_name
            return action_result(
                delete_admin_user(identifier, current_state),
                current_state,
                name,
                role,
                active,
            )

        def deactivate_and_refresh(
            target_name: str,
            identifier: str,
            current_state: Mapping[str, Any],
            name: str,
            role: str,
            active: str,
        ) -> tuple[list[list[str]], list[dict[str, Any]], str]:
            return action_result(
                deactivate_admin_user(target_name, identifier, current_state),
                current_state,
                name,
                role,
                active,
            )

        def load_overview(
            current_state: Mapping[str, Any],
        ) -> tuple[str, str, str, str]:
            try:
                _ensure_admin(current_state)
                with get_session_factory()() as session:
                    snapshot = AdminService(session).get_system_status()
                return (
                    (
                        str(snapshot.user_count)
                        if snapshot.user_count is not None
                        else "未知"
                    ),
                    (
                        str(snapshot.active_user_count)
                        if snapshot.active_user_count is not None
                        else "未知"
                    ),
                    (
                        str(snapshot.role_count)
                        if snapshot.role_count is not None
                        else "未知"
                    ),
                    _dependency_markdown(snapshot),
                )
            except (
                AdminServiceError,
                PermissionDeniedError,
                SQLAlchemyError,
                TypeError,
                ValueError,
            ) as error:
                return "未知", "未知", "未知", _format_error(error)

        def load_dependencies(current_state: Mapping[str, Any]) -> str:
            try:
                _ensure_admin(current_state)
                with get_session_factory()() as session:
                    snapshot = AdminService(session).get_system_status()
                return _dependency_markdown(snapshot)
            except (
                AdminServiceError,
                PermissionDeniedError,
                SQLAlchemyError,
                TypeError,
                ValueError,
            ) as error:
                return _format_error(error)

        filter_inputs = [name_filter, role_filter, active_filter, state]
        filter_options: dict[str, Any] = {
            "outputs": [users_table, user_records, message],
            "show_progress": "hidden",
            "concurrency_id": "eduagent-ui",
            "concurrency_limit": 1,
        }
        refresh_users.click(refresh_users_view, inputs=filter_inputs, **filter_options)
        for control in (name_filter, role_filter, active_filter):
            control.input(refresh_users_view, inputs=filter_inputs, **filter_options)
        users_table.select(
            select_user,
            inputs=[user_records],
            outputs=[
                edit_status,
                edit_target_name,
                user_id,
                username,
                email,
                password,
                edit_roles,
                is_active,
                role_user_name,
                role_user_id,
                assigned_roles,
                update_button,
                deactivate_button,
                delete_button,
                roles_button,
                create_form,
            ],
            show_progress="hidden",
        )
        new_user_button.click(
            start_create,
            outputs=[
                create_form,
                create_username,
                create_email,
                create_password,
                create_roles,
                create_active,
                user_id,
                edit_target_name,
                username,
                email,
                password,
                edit_roles,
                is_active,
                edit_status,
                role_user_name,
                role_user_id,
                assigned_roles,
                update_button,
                deactivate_button,
                delete_button,
                roles_button,
            ],
            show_progress="hidden",
        )
        clear_create_button.click(
            lambda: ("", "", "", [UserRole.STUDENT.value], True),
            outputs=[
                create_username,
                create_email,
                create_password,
                create_roles,
                create_active,
            ],
            show_progress="hidden",
        )
        cancel_create_button.click(
            cancel_create,
            outputs=[
                create_form,
                create_username,
                create_email,
                create_password,
                create_roles,
                create_active,
                user_id,
                edit_target_name,
                username,
                email,
                password,
                edit_roles,
                is_active,
                edit_status,
                role_user_name,
                role_user_id,
                assigned_roles,
                update_button,
                deactivate_button,
                delete_button,
                roles_button,
            ],
            show_progress="hidden",
        )
        create_button.click(
            create_and_refresh,
            inputs=[
                create_username,
                create_email,
                create_password,
                create_roles,
                create_active,
                state,
                name_filter,
                role_filter,
                active_filter,
            ],
            outputs=[users_table, user_records, message],
            show_progress="minimal",
            concurrency_id="eduagent-ui",
            concurrency_limit=1,
        )
        update_button.click(
            update_and_refresh,
            inputs=[
                user_id,
                username,
                email,
                password,
                is_active,
                state,
                name_filter,
                role_filter,
                active_filter,
            ],
            outputs=[users_table, user_records, message],
            show_progress="minimal",
            concurrency_id="eduagent-ui",
            concurrency_limit=1,
        )
        roles_button.click(
            roles_and_refresh,
            inputs=[
                role_user_id,
                assigned_roles,
                state,
                name_filter,
                role_filter,
                active_filter,
            ],
            outputs=[users_table, user_records, message],
            show_progress="minimal",
            concurrency_id="eduagent-ui",
            concurrency_limit=1,
        )
        confirmation_outputs = [users_table, user_records, message]
        bind_confirmation(
            deactivate_button,
            action="停用用户",
            target=edit_target_name,
            callback=deactivate_and_refresh,
            inputs=[
                edit_target_name,
                user_id,
                state,
                name_filter,
                role_filter,
                active_filter,
            ],
            outputs=confirmation_outputs,
        )
        bind_confirmation(
            delete_button,
            action="删除用户",
            target=edit_target_name,
            callback=delete_and_refresh,
            inputs=[
                edit_target_name,
                user_id,
                state,
                name_filter,
                role_filter,
                active_filter,
            ],
            outputs=confirmation_outputs,
        )
        refresh_roles.click(
            fn=refresh_admin_roles,
            inputs=[state],
            outputs=[roles_table, message],
            show_progress="hidden",
        )
        overview_refresh.click(
            load_overview,
            inputs=[state],
            outputs=[overview_users, overview_active, overview_roles, overview_health],
            show_progress="hidden",
        )
        refresh_status.click(
            fn=load_dependencies,
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
        overview_section=overview_section,
    )


build_admin_view = create_admin_view


__all__ = [
    "ROLE_CHOICES",
    "USER_TABLE_HEADERS",
    "AdminView",
    "build_admin_view",
    "create_admin_user",
    "create_admin_view",
    "deactivate_admin_user",
    "delete_admin_user",
    "refresh_admin_roles",
    "refresh_admin_status",
    "refresh_admin_users",
    "set_admin_user_roles",
    "update_admin_user",
]
