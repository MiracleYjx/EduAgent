"""EduAgent Gradio 应用外壳、登录状态和角色导航。"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, TypedDict

import gradio as gr
from sqlalchemy.exc import SQLAlchemyError

from backend.app.core.database import get_session_factory
from backend.app.domain.enums import UserRole
from backend.app.domain.permissions import (
    ROLE_DISPLAY_NAMES,
    PermissionDeniedError,
    normalize_role,
)
from backend.app.models import User
from backend.app.services.auth_service import AuthenticationError, AuthService
from backend.app.ui.admin_view import AdminView, create_admin_view


class LoginState(TypedDict):
    """Gradio 会话中保存的最小登录状态。"""

    access_token: str
    user_id: str
    username: str
    email: str
    roles: list[str]


@dataclass(frozen=True)
class NavigationItem:
    """一个按角色控制可见性的导航项。"""

    key: str
    label: str
    allowed_roles: frozenset[UserRole]
    description: str


NAVIGATION_ITEMS: tuple[NavigationItem, ...] = (
    NavigationItem(
        key="teacher.courses",
        label="课程与资料",
        allowed_roles=frozenset({UserRole.TEACHER}),
        description="教师课程、知识库和资料入口。",
    ),
    NavigationItem(
        key="teacher.questions",
        label="题库与审核",
        allowed_roles=frozenset({UserRole.TEACHER}),
        description="教师题目管理和审核入口。",
    ),
    NavigationItem(
        key="teacher.exams",
        label="考试与阅卷",
        allowed_roles=frozenset({UserRole.TEACHER}),
        description="教师组卷、发布和阅卷入口。",
    ),
    NavigationItem(
        key="student.exams",
        label="参加考试",
        allowed_roles=frozenset({UserRole.STUDENT}),
        description="学生可参加的考试入口。",
    ),
    NavigationItem(
        key="student.results",
        label="我的成绩",
        allowed_roles=frozenset({UserRole.STUDENT}),
        description="学生成绩、错题和诊断入口。",
    ),
    NavigationItem(
        key="admin.users",
        label="用户与角色",
        allowed_roles=frozenset({UserRole.ADMIN}),
        description="管理员用户和角色管理入口。",
    ),
    NavigationItem(
        key="admin.status",
        label="运行状态",
        allowed_roles=frozenset({UserRole.ADMIN}),
        description="管理员系统运行状态入口。",
    ),
)

_EMPTY_LOGIN_STATE: LoginState = {
    "access_token": "",
    "user_id": "",
    "username": "",
    "email": "",
    "roles": [],
}
_UNAUTHENTICATED_MESSAGE = "请先登录。"
_GENERIC_ERROR_MESSAGE = "操作失败，请稍后重试。"


def empty_login_state() -> LoginState:
    """返回新的空登录状态，避免不同浏览器会话共享可变对象。"""

    return {
        "access_token": _EMPTY_LOGIN_STATE["access_token"],
        "user_id": _EMPTY_LOGIN_STATE["user_id"],
        "username": _EMPTY_LOGIN_STATE["username"],
        "email": _EMPTY_LOGIN_STATE["email"],
        "roles": [],
    }


def normalize_ui_roles(
    roles: Iterable[UserRole | str | Any] | UserRole | str,
) -> tuple[UserRole, ...]:
    """将数据库角色、字符串和枚举统一成稳定排序的角色元组。"""

    if isinstance(roles, (str, UserRole)):
        roles = (roles,)

    normalized: set[UserRole] = set()
    for role in roles:
        candidate = getattr(role, "name", role)
        try:
            normalized.add(normalize_role(candidate))
        except (TypeError, ValueError):
            # 异常角色只影响导航显示，不能意外放开任何入口。
            continue
    return tuple(sorted(normalized, key=lambda item: item.value))


def navigation_for_roles(
    roles: Iterable[UserRole | str | Any] | UserRole | str,
) -> tuple[NavigationItem, ...]:
    """返回当前角色能够看到的导航项。"""

    role_set = set(normalize_ui_roles(roles))
    return tuple(
        item for item in NAVIGATION_ITEMS if item.allowed_roles.intersection(role_set)
    )


def format_ui_error(error: BaseException | None) -> str:
    """将内部异常转换成统一且不泄露敏感信息的中文提示。"""

    if error is None:
        return ""

    message = str(error).strip()
    if isinstance(error, PermissionDeniedError):
        return message or "当前账号无权执行此操作。"
    if isinstance(error, AuthenticationError):
        if "JWT 密钥未配置" in message:
            return "认证服务暂时不可用，请联系管理员。"
        return "登录失败：用户名、邮箱或密码错误。"
    if isinstance(error, SQLAlchemyError):
        return "系统暂时无法连接数据库，请稍后重试。"
    if isinstance(error, ValueError):
        return f"输入有误：{message or '请检查输入内容。'}"
    return _GENERIC_ERROR_MESSAGE


def _jwt_secret_key() -> str | None:
    """只从环境变量读取 JWT 密钥，不把密钥写入 UI 或日志。"""

    value = os.getenv("JWT_SECRET_KEY", "").strip()
    return value or None


def _user_state(user: User, access_token: str) -> LoginState:
    """构造不含密码等敏感字段的 Gradio 登录状态。"""

    roles = normalize_ui_roles(user.roles)
    return {
        "access_token": access_token,
        "user_id": str(user.id),
        "username": user.username,
        "email": user.email,
        "roles": [role.value for role in roles],
    }


def _navigation_choices(
    state: Mapping[str, Any],
) -> list[tuple[str, str]]:
    """把登录状态转换为 Gradio 单选导航的显示值和内部键。"""

    return [
        (item.label, item.key) for item in navigation_for_roles(state.get("roles", []))
    ]


def _role_summary(state: Mapping[str, Any]) -> str:
    """构造登录后的用户摘要，只显示公开身份和角色。"""

    roles = normalize_ui_roles(state.get("roles", []))
    role_names = "、".join(ROLE_DISPLAY_NAMES[role] for role in roles) or "未分配角色"
    username = state.get("username") or "用户"
    return f"### {username}\n角色：{role_names}"


def _page_for_navigation(
    selection: str | None,
    state: Mapping[str, Any],
) -> str:
    """渲染已登录用户当前导航项对应的页面占位内容。"""

    if not state.get("access_token"):
        return _UNAUTHENTICATED_MESSAGE

    allowed_items = navigation_for_roles(state.get("roles", []))
    item = next(
        (candidate for candidate in allowed_items if candidate.key == selection), None
    )
    if item is None:
        return "当前账号没有可访问的导航项。"

    return f"## {item.label}\n\n{item.description}\n\n模块已准备就绪。"


def login_user(
    identifier: str,
    password: str,
    *,
    session_factory: Callable[[], Any] | None = None,
    secret_key: str | None = None,
) -> tuple[LoginState, str, dict[str, Any], dict[str, Any], str, dict[str, Any], str]:
    """登录并返回 Gradio 需要更新的状态、面板和导航结果。"""

    try:
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError("用户名或邮箱不能为空。")
        if not isinstance(password, str) or not password:
            raise ValueError("密码不能为空。")

        factory = session_factory or get_session_factory()
        with factory() as session:
            service = AuthService(
                session,
                secret_key=secret_key if secret_key is not None else _jwt_secret_key(),
            )
            user = service.authenticate(identifier.strip(), password)
            access_token = service.issue_access_token(user)
            state = _user_state(user, access_token)

        choices = _navigation_choices(state)
        first_key = choices[0][1] if choices else None
        return (
            state,
            f"登录成功，欢迎 {state['username']}。",
            gr.update(visible=False),
            gr.update(visible=True),
            _role_summary(state),
            gr.update(choices=choices, value=first_key, visible=bool(choices)),
            _page_for_navigation(first_key, state),
        )
    except (
        AuthenticationError,
        PermissionDeniedError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as error:
        return (
            empty_login_state(),
            format_ui_error(error),
            gr.update(visible=True),
            gr.update(visible=False),
            "",
            gr.update(choices=[], value=None, visible=False),
            _UNAUTHENTICATED_MESSAGE,
        )


def logout_user() -> tuple[
    LoginState, str, dict[str, Any], dict[str, Any], str, dict[str, Any], str
]:
    """清理当前 Gradio 会话并回到登录面板。"""

    return (
        empty_login_state(),
        "已退出登录。",
        gr.update(visible=True),
        gr.update(visible=False),
        "",
        gr.update(choices=[], value=None, visible=False),
        _UNAUTHENTICATED_MESSAGE,
    )


def select_navigation(
    selection: str | None,
    state: Mapping[str, Any],
) -> str:
    """校验角色边界后渲染导航内容。"""

    try:
        return _page_for_navigation(selection, state)
    except (
        PermissionDeniedError,
        SQLAlchemyError,
        TypeError,
        ValueError,
        AttributeError,
    ) as error:
        return format_ui_error(error)


def _is_admin_navigation(selection: str | None) -> bool:
    """判断当前导航选择是否应该展示管理员视图。"""

    return selection in {"admin.users", "admin.status"}


def login_user_for_app(
    identifier: str,
    password: str,
    *,
    session_factory: Callable[[], Any] | None = None,
    secret_key: str | None = None,
) -> tuple[Any, ...]:
    """登录并同步设置管理员视图的显示状态。"""

    result: list[Any] = list(
        login_user(
            identifier,
            password,
            session_factory=session_factory,
            secret_key=secret_key,
        )
    )
    state = result[0]
    first_selection = _navigation_choices(state)
    is_admin = bool(first_selection) and _is_admin_navigation(first_selection[0][1])
    result[-1] = gr.update(value=result[-1], visible=not is_admin)
    return (*result, gr.update(visible=is_admin))


def logout_user_for_app() -> tuple[Any, ...]:
    """退出登录并隐藏管理员视图。"""

    result = list(logout_user())
    result[-1] = gr.update(value=result[-1], visible=True)
    return (*result, gr.update(visible=False))


def select_navigation_for_app(
    selection: str | None,
    state: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """切换导航时同步显示普通页面或管理员视图。"""

    content = select_navigation(selection, state)
    is_admin = bool(state.get("access_token")) and _is_admin_navigation(selection)
    return (
        gr.update(value=content, visible=not is_admin),
        gr.update(visible=is_admin),
    )


def create_gradio_app() -> gr.Blocks:
    """创建包含登录、会话状态和角色导航的 Gradio 应用。"""

    with gr.Blocks(title="EduAgent 教学评测平台") as demo:
        session_state = gr.State(empty_login_state())

        gr.Markdown("# EduAgent 教学评测平台")
        login_panel = gr.Column(visible=True)
        with login_panel:
            gr.Markdown("### 登录")
            identifier = gr.Textbox(
                label="用户名或邮箱",
                placeholder="请输入用户名或邮箱",
            )
            password = gr.Textbox(
                label="密码",
                placeholder="请输入密码",
                type="password",
            )
            login_button = gr.Button("登录", variant="primary")
            login_message = gr.Markdown()

        workspace = gr.Column(visible=False)
        with workspace:
            with gr.Row():
                user_summary = gr.Markdown()
                logout_button = gr.Button("退出登录", variant="secondary")
            navigation = gr.Radio(
                choices=[],
                label="功能导航",
                type="value",
                visible=False,
            )
            page_content = gr.Markdown(_UNAUTHENTICATED_MESSAGE)
            admin_view: AdminView = create_admin_view(session_state)

        login_button.click(
            fn=login_user_for_app,
            inputs=[identifier, password],
            outputs=[
                session_state,
                login_message,
                login_panel,
                workspace,
                user_summary,
                navigation,
                page_content,
                admin_view.panel,
            ],
            show_progress="hidden",
        )
        password.submit(
            fn=login_user_for_app,
            inputs=[identifier, password],
            outputs=[
                session_state,
                login_message,
                login_panel,
                workspace,
                user_summary,
                navigation,
                page_content,
                admin_view.panel,
            ],
            show_progress="hidden",
        )
        logout_button.click(
            fn=logout_user_for_app,
            outputs=[
                session_state,
                login_message,
                login_panel,
                workspace,
                user_summary,
                navigation,
                page_content,
                admin_view.panel,
            ],
            show_progress="hidden",
        )
        navigation.change(
            fn=select_navigation_for_app,
            inputs=[navigation, session_state],
            outputs=[page_content, admin_view.panel],
            show_progress="hidden",
        )

    return demo


__all__ = [
    "NAVIGATION_ITEMS",
    "LoginState",
    "NavigationItem",
    "create_gradio_app",
    "empty_login_state",
    "format_ui_error",
    "login_user",
    "login_user_for_app",
    "logout_user",
    "logout_user_for_app",
    "navigation_for_roles",
    "normalize_ui_roles",
    "select_navigation",
    "select_navigation_for_app",
]
