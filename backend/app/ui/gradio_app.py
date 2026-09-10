"""EduAgent Gradio 应用外壳、登录状态和角色导航。"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from functools import partial, wraps
from html import escape
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
from backend.app.ui.exam_view import ExamView, create_exam_view
from backend.app.ui.layout_view import (
    ROLE_NAVIGATION,
    WORKSPACE_CSS,
    feedback,
    is_authorized_navigation,
    navigation_item,
    placeholder_page,
    resettable_components,
)
from backend.app.ui.layout_view import (
    LayoutNavigationItem as NavigationItem,
)
from backend.app.ui.question_view import QuestionView, create_question_view
from backend.app.ui.student_exam_view import (
    StudentExamView,
    create_student_exam_view,
)


class LoginState(TypedDict):
    """Gradio 会话中保存的最小登录状态。"""

    access_token: str
    user_id: str
    username: str
    email: str
    roles: list[str]


NAVIGATION_ITEMS: tuple[NavigationItem, ...] = ROLE_NAVIGATION

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
    username = escape(str(state.get("username") or "用户"))
    return f'<span title="{username}（{role_names}）">{username}</span>'


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

    return placeholder_page(item)


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


def logout_user() -> (
    tuple[LoginState, str, dict[str, Any], dict[str, Any], str, dict[str, Any], str]
):
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

    return selection in {"admin.users", "admin.roles", "admin.status"}


def _is_question_navigation(selection: str | None) -> bool:
    """判断当前导航选择是否应该展示教师题库视图。"""

    return selection == "teacher.questions"


def _is_exam_navigation(selection: str | None) -> bool:
    """判断当前导航选择是否应该展示教师考试视图。"""

    return selection == "teacher.exams"


def _is_student_exam_navigation(selection: str | None) -> bool:
    """判断当前导航选择是否应该展示学生考试视图。"""

    return selection == "student.exams"


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


def login_user_for_app_with_questions(
    identifier: str,
    password: str,
    *,
    session_factory: Callable[[], Any] | None = None,
    secret_key: str | None = None,
) -> tuple[Any, ...]:
    """登录并同步设置管理员和教师题库视图的显示状态。"""

    result = list(
        login_user_for_app(
            identifier,
            password,
            session_factory=session_factory,
            secret_key=secret_key,
        )
    )
    state = result[0]
    choices = _navigation_choices(state)
    first_selection = choices[0][1] if choices else None
    is_admin = bool(state.get("access_token")) and _is_admin_navigation(first_selection)
    is_question = bool(state.get("access_token")) and _is_question_navigation(
        first_selection,
    )
    page_update = result[6]
    page_value = (
        page_update.get("value", "") if isinstance(page_update, dict) else page_update
    )
    result[6] = gr.update(
        value=page_value,
        visible=not is_admin and not is_question,
    )
    return (*result, gr.update(visible=is_question))


def login_user_for_app_with_questions_and_exams(
    identifier: str,
    password: str,
    *,
    session_factory: Callable[[], Any] | None = None,
    secret_key: str | None = None,
) -> tuple[Any, ...]:
    """登录并同步设置管理员、题库和考试视图的显示状态。"""

    result = list(
        login_user_for_app_with_questions(
            identifier,
            password,
            session_factory=session_factory,
            secret_key=secret_key,
        )
    )
    state = result[0]
    choices = _navigation_choices(state)
    first_selection = choices[0][1] if choices else None
    is_admin = bool(state.get("access_token")) and _is_admin_navigation(first_selection)
    is_question = bool(state.get("access_token")) and _is_question_navigation(
        first_selection,
    )
    is_exam = bool(state.get("access_token")) and _is_exam_navigation(first_selection)
    page_update = result[6]
    page_value = (
        page_update.get("value", "") if isinstance(page_update, dict) else page_update
    )
    result[6] = gr.update(
        value=page_value,
        visible=not is_admin and not is_question and not is_exam,
    )
    return (*result, gr.update(visible=is_exam))


def logout_user_for_app() -> tuple[Any, ...]:
    """退出登录并隐藏管理员视图。"""

    result = list(logout_user())
    result[-1] = gr.update(value=result[-1], visible=True)
    return (*result, gr.update(visible=False))


def logout_user_for_app_with_questions() -> tuple[Any, ...]:
    """退出登录并隐藏管理员和教师题库视图。"""

    result = list(logout_user_for_app())
    return (*result, gr.update(visible=False))


def logout_user_for_app_with_questions_and_exams() -> tuple[Any, ...]:
    """退出登录并隐藏管理员、题库和考试视图。"""

    result = list(logout_user_for_app_with_questions())
    return (*result, gr.update(visible=False))


def login_user_for_app_with_questions_and_exams_and_student(
    identifier: str,
    password: str,
    *,
    session_factory: Callable[[], Any] | None = None,
    secret_key: str | None = None,
) -> tuple[Any, ...]:
    """登录并同步设置管理员、教师和学生视图的显示状态。"""

    result = list(
        login_user_for_app_with_questions_and_exams(
            identifier,
            password,
            session_factory=session_factory,
            secret_key=secret_key,
        )
    )
    state = result[0]
    choices = _navigation_choices(state)
    first_selection = choices[0][1] if choices else None
    is_student_exam = bool(state.get("access_token")) and _is_student_exam_navigation(
        first_selection,
    )
    page_update = result[6]
    page_value = (
        page_update.get("value", "") if isinstance(page_update, dict) else page_update
    )
    result[6] = gr.update(
        value=page_value,
        visible=not _is_admin_navigation(first_selection)
        and not _is_question_navigation(first_selection)
        and not _is_exam_navigation(first_selection)
        and not is_student_exam,
    )
    return (*result, gr.update(visible=is_student_exam))


def logout_user_for_app_with_questions_and_exams_and_student() -> tuple[Any, ...]:
    """退出登录并隐藏管理员、教师和学生视图。"""

    result = list(logout_user_for_app_with_questions_and_exams())
    return (*result, gr.update(visible=False))


def select_navigation_for_app(
    selection: str | None,
    state: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """切换导航时同步显示普通页面或管理员视图。"""

    content = select_navigation(selection, state)
    is_admin = _can_navigate(selection, state) and _is_admin_navigation(selection)
    return (
        gr.update(value=content, visible=not is_admin),
        gr.update(visible=is_admin),
    )


def select_navigation_for_app_with_questions(
    selection: str | None,
    state: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """切换导航时同步显示普通页面、管理员或教师题库视图。"""

    content = select_navigation(selection, state)
    is_admin = _can_navigate(selection, state) and _is_admin_navigation(selection)
    is_question = _can_navigate(selection, state) and _is_question_navigation(
        selection,
    )
    return (
        gr.update(value=content, visible=not is_admin and not is_question),
        gr.update(visible=is_admin),
        gr.update(visible=is_question),
    )


def select_navigation_for_app_with_questions_and_exams(
    selection: str | None,
    state: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """切换导航时同步显示普通页面、管理员、题库或考试视图。"""

    page_update, admin_update, question_update = (
        select_navigation_for_app_with_questions(selection, state)
    )
    is_exam = _can_navigate(selection, state) and _is_exam_navigation(selection)
    page_value = (
        page_update.get("value", "") if isinstance(page_update, dict) else page_update
    )
    return (
        gr.update(
            value=page_value,
            visible=not is_exam
            and not admin_update["visible"]
            and not question_update["visible"],
        ),
        admin_update,
        question_update,
        gr.update(visible=is_exam),
    )


def select_navigation_for_app_with_questions_and_exams_and_student(
    selection: str | None,
    state: Mapping[str, Any],
) -> tuple[Any, ...]:
    """切换导航时同步显示普通页面、管理员、教师或学生考试视图。"""

    result = list(select_navigation_for_app_with_questions_and_exams(selection, state))
    is_student_exam = _can_navigate(selection, state) and _is_student_exam_navigation(
        selection,
    )
    page_update = result[0]
    page_value = (
        page_update.get("value", "") if isinstance(page_update, dict) else page_update
    )
    result[0] = gr.update(
        value=page_value,
        visible=not any(update["visible"] for update in result[1:])
        and not is_student_exam,
    )
    return (*result, gr.update(visible=is_student_exam))


def _can_navigate(selection: str | None, state: Mapping[str, Any]) -> bool:
    """同时检查登录状态和角色权限，不能只依赖组件隐藏。"""

    return bool(state.get("access_token")) and is_authorized_navigation(
        selection, state.get("roles", [])
    )


def _authenticated_state(state: Mapping[str, Any]) -> LoginState:
    """复用认证服务核验有效期、启用状态及最新角色。"""

    token = state.get("access_token", "")
    if not token:
        raise AuthenticationError("请先登录。")
    with get_session_factory()() as session:
        user = AuthService(session, secret_key=_jwt_secret_key()).get_current_user(
            token
        )
        return _user_state(user, token)


def _guard_view_callback(
    fn: Callable[..., Any], state_index: int
) -> Callable[..., Any]:
    """业务视图每次操作前核验真实会话，再交回原有角色和资源守卫。"""

    @wraps(fn)
    def guarded(*args: Any, **kwargs: Any) -> Any:
        values = list(args)
        try:
            values[state_index] = _authenticated_state(values[state_index])
        except AuthenticationError:
            raise gr.Error("登录状态已失效，请退出后重新登录。") from None
        except SQLAlchemyError:
            raise gr.Error("系统暂时无法连接数据库，请稍后重试。") from None
        return fn(*values, **kwargs)

    return guarded


def create_gradio_app() -> gr.Blocks:
    """创建共享外壳；会话与各页面输入在当前浏览器会话内保持。"""

    with gr.Blocks(title="EduAgent 教学评测平台", fill_width=True) as demo:
        session_state = gr.State(empty_login_state())
        navigation_state = gr.State({"role": "", "pages": {}})
        with gr.Column(elem_id="edu-root"):
            # 样式随视图挂载，兼容 Gradio 5/6 的挂载参数差异。
            gr.HTML(f"<style>{WORKSPACE_CSS}</style>", elem_id="edu-style")
            with gr.Column(elem_id="edu-login") as login_panel:
                gr.Markdown("# EduAgent\n\n### 登录教学评测平台")
                identifier = gr.Textbox(
                    label="用户名或邮箱", placeholder="请输入用户名或邮箱"
                )
                password = gr.Textbox(
                    label="密码", placeholder="请输入密码", type="password"
                )
                login_button = gr.Button("登录", variant="primary")
                login_message = gr.HTML(elem_id="edu-login-message")

            with gr.Column(visible=False, elem_id="edu-workspace") as workspace:
                with gr.Row(elem_id="edu-topbar"):
                    gr.HTML("<strong>EduAgent</strong>", elem_id="edu-brand")
                    context = gr.HTML(elem_id="edu-context")
                    user_summary = gr.HTML(elem_id="edu-user")
                    role_selector = gr.Dropdown(
                        choices=[],
                        value=None,
                        label="当前角色",
                        show_label=False,
                        container=False,
                        min_width=0,
                        elem_id="edu-role",
                        interactive=True,
                    )
                    logout_button = gr.Button("退出登录", elem_id="edu-logout")
                with gr.Row(elem_id="edu-shell-row"):
                    with (
                        gr.Column(scale=0, min_width=0, elem_id="edu-sidebar"),
                        gr.Accordion(
                            "功能导航", open=False, elem_id="edu-menu"
                        ) as menu,
                    ):
                        groups: dict[tuple[UserRole, str], gr.Column] = {}
                        buttons: dict[str, gr.Button] = {}
                        for role in UserRole:
                            items = navigation_for_roles([role])
                            for group in dict.fromkeys(item.group for item in items):
                                with gr.Column(
                                    visible=False, elem_classes="edu-nav-group"
                                ) as group_panel:
                                    groups[role, group] = group_panel
                                    if group in {"教学准备", "AI 教学"}:
                                        gr.Markdown(
                                            group, elem_classes="edu-group-title"
                                        )
                                    for item in items:
                                        if item.group == group:
                                            buttons[item.key] = gr.Button(
                                                item.label,
                                                elem_classes=["edu-nav"],
                                                min_width=0,
                                                elem_id="edu-nav-"
                                                + item.key.replace(".", "-"),
                                            )
                    with gr.Column(min_width=0, elem_id="edu-content"):
                        workspace_message = gr.HTML(elem_id="edu-feedback")
                        page_content = gr.Markdown()
                        admin_view: AdminView = create_admin_view(session_state)
                        question_view: QuestionView = create_question_view(
                            session_state
                        )
                        exam_view: ExamView = create_exam_view(session_state)
                        student_exam_view: StudentExamView = create_student_exam_view(
                            session_state
                        )

        panels = {
            "teacher.questions": question_view.panel,
            "teacher.exams": exam_view.panel,
            "student.exams": student_exam_view.panel,
        }
        admin_sections = {
            "admin.users": admin_view.users_section,
            "admin.roles": admin_view.roles_section,
            "admin.status": admin_view.status_section,
        }
        resets = [
            entry
            for panel in [admin_view.panel, *panels.values()]
            for entry in resettable_components(panel)
        ]
        view_functions = list(demo.fns.values())
        for block_fn in view_functions:
            if block_fn.fn is not None and session_state in block_fn.inputs:
                block_fn.fn = _guard_view_callback(
                    block_fn.fn, block_fn.inputs.index(session_state)
                )

        def render_workspace(
            state: LoginState, nav: dict[str, Any], selected: str | None
        ) -> dict[Any, Any]:
            """单次更新所有导航和面板，保持当前角色只展示一页。"""

            role = nav.get("role", "")
            allowed = _can_navigate(selected, state) and is_authorized_navigation(
                selected, [role]
            )
            item = navigation_item(selected) if allowed else None
            result: dict[Any, Any] = {
                session_state: state,
                navigation_state: nav,
                login_panel: gr.update(visible=False),
                workspace: gr.update(visible=True),
                user_summary: _role_summary(state),
                context: f"<span>{item.label if item else '工作台'}</span>",
                role_selector: gr.update(
                    choices=[
                        (ROLE_DISPLAY_NAMES[r], r.value)
                        for r in normalize_ui_roles(state["roles"])
                    ],
                    value=role or None,
                    interactive=len(state["roles"]) > 1,
                ),
                page_content: gr.update(
                    value=placeholder_page(item),
                    visible=selected not in {*panels, *admin_sections} or not allowed,
                ),
                admin_view.panel: gr.update(
                    visible=allowed and selected in admin_sections
                ),
            }
            for key, panel in {**panels, **admin_sections}.items():
                result[panel] = gr.update(visible=allowed and key == selected)
            for (group_role, _), panel in groups.items():
                result[panel] = gr.update(
                    visible=group_role.value == role and role in state["roles"]
                )
            for key, button in buttons.items():
                result[button] = gr.update(
                    elem_classes=(
                        ["edu-nav", "edu-nav-active"]
                        if allowed and key == selected
                        else ["edu-nav"]
                    )
                )
            if not state["roles"]:
                result[page_content] = gr.update(
                    value="当前账号未分配角色，请联系管理员。", visible=True
                )
            return result

        def clear_workspace(
            message: str = "已退出登录。", kind: str = "success"
        ) -> dict[Any, Any]:
            """恢复所有页面初值，防止同一浏览器切换账号后残留数据。"""

            result = render_workspace(
                empty_login_state(), {"role": "", "pages": {}}, None
            )
            result.update(
                {
                    component: gr.update(value=deepcopy(value))
                    for component, value in resets
                }
            )
            result.update(
                {
                    login_panel: gr.update(visible=True),
                    workspace: gr.update(visible=False),
                    identifier: "",
                    password: "",
                    login_message: feedback(message, kind),
                    workspace_message: "",
                    context: "",
                    user_summary: "",
                    page_content: gr.update(value="", visible=True),
                    menu: gr.update(open=False),
                }
            )
            return result

        login_progress = gr.Progress()

        def sign_in(
            username: str, credential: str, progress: gr.Progress = login_progress
        ) -> dict[Any, Any]:
            """完成真实登录，清空旧页面并默认进入首个授权角色的概览。"""

            progress(0, desc="正在登录，请稍候。")
            response = login_user(username, credential)
            state = response[0]
            if not state["access_token"]:
                result = clear_workspace(response[1], "error")
                result[identifier] = username
                return result
            role = state["roles"][0] if state["roles"] else ""
            choices = navigation_for_roles([role])
            selected = choices[0].key if choices else None
            result = clear_workspace("")
            result.update(
                render_workspace(
                    state, {"role": role, "pages": {role: selected}}, selected
                )
            )
            result[workspace_message] = feedback(response[1], "success")
            return result

        def navigate(
            state: LoginState,
            nav: dict[str, Any],
            *,
            selected: str | None = None,
            role: str | None = None,
        ) -> dict[Any, Any]:
            """角色切换恢复上次页面；普通导航不重建视图或丢失草稿。"""

            try:
                current = _authenticated_state(state)
            except AuthenticationError:
                return clear_workspace("登录状态已失效，请重新登录。", "error")
            except SQLAlchemyError as error:
                return {
                    workspace_message: feedback(format_ui_error(error), "error"),
                    role_selector: gr.update(value=nav.get("role") or None),
                }
            if current["roles"] != state["roles"]:
                return clear_workspace("账号角色已变更，请重新登录。", "info")
            active_role = role if role is not None else nav.get("role", "")
            if active_role not in current["roles"]:
                return {
                    workspace_message: feedback("当前账号无权切换到该角色。", "error"),
                    role_selector: gr.update(value=nav.get("role") or None),
                }
            next_nav = deepcopy(nav)
            if role is not None:
                selected = (
                    next_nav["pages"].get(role) or navigation_for_roles([role])[0].key
                )
            if not is_authorized_navigation(selected, [active_role]):
                return {
                    workspace_message: feedback("当前账号无权访问该页面。", "error")
                }
            next_nav["role"] = active_role
            next_nav["pages"][active_role] = selected
            result = render_workspace(current, next_nav, selected)
            result[workspace_message] = ""
            result[menu] = gr.update(open=False)
            return result

        outputs = list(
            dict.fromkeys(
                [
                    session_state,
                    navigation_state,
                    login_panel,
                    workspace,
                    identifier,
                    password,
                    login_message,
                    workspace_message,
                    context,
                    user_summary,
                    role_selector,
                    page_content,
                    menu,
                    admin_view.panel,
                    *panels.values(),
                    *admin_sections.values(),
                    *groups.values(),
                    *buttons.values(),
                    *(component for component, _ in resets),
                ]
            )
        )
        # 同一队列串行处理数据操作和退出，避免慢请求在退出清空后重新填入旧数据。
        event_options: dict[str, Any] = {
            "outputs": outputs,
            "show_progress": "minimal",
            "concurrency_id": "eduagent-ui",
            "concurrency_limit": 1,
        }
        login_button.click(sign_in, inputs=[identifier, password], **event_options)
        password.submit(sign_in, inputs=[identifier, password], **event_options)
        logout_button.click(clear_workspace, inputs=[], **event_options)
        for key, button in buttons.items():
            button.click(
                partial(navigate, selected=key),
                inputs=[session_state, navigation_state],
                **event_options,
            )

        def switch_role(
            role: str, state: LoginState, nav: dict[str, Any]
        ) -> dict[Any, Any]:
            return navigate(state, nav, role=role)

        role_selector.input(
            switch_role,
            inputs=[role_selector, session_state, navigation_state],
            **event_options,
        )
        for block_fn in view_functions:
            block_fn.concurrency_id = "eduagent-ui"
            block_fn.concurrency_limit = 1
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
    "login_user_for_app_with_questions",
    "login_user_for_app_with_questions_and_exams",
    "login_user_for_app_with_questions_and_exams_and_student",
    "logout_user",
    "logout_user_for_app",
    "logout_user_for_app_with_questions",
    "logout_user_for_app_with_questions_and_exams",
    "logout_user_for_app_with_questions_and_exams_and_student",
    "navigation_for_roles",
    "normalize_ui_roles",
    "select_navigation",
    "select_navigation_for_app",
    "select_navigation_for_app_with_questions",
    "select_navigation_for_app_with_questions_and_exams",
    "select_navigation_for_app_with_questions_and_exams_and_student",
]
