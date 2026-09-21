"""T019 Gradio 应用外壳、登录状态和角色导航测试。"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

import gradio as gr
import pytest

import backend.app.ui.gradio_app as gradio_app_module
from backend.app.domain.enums import UserRole
from backend.app.domain.permissions import PermissionDeniedError
from backend.app.ui.gradio_app import (
    create_gradio_app,
    empty_login_state,
    format_ui_error,
    navigation_for_roles,
    select_navigation,
)


@pytest.fixture(scope="module")
def built_app() -> gr.Blocks:
    """构建一次完整应用，供真实 Gradio 注册回调断言复用。"""

    return create_gradio_app()


def _registered_callback(app: gr.Blocks, name: str) -> Callable[..., Any]:
    callbacks = [
        block_fn.fn
        for block_fn in app.fns.values()
        if block_fn.fn is not None and getattr(block_fn.fn, "__name__", None) == name
    ]
    assert len(callbacks) == 1
    return callbacks[0]


def _teacher_state() -> dict[str, Any]:
    state: dict[str, Any] = dict(empty_login_state())
    state.update(
        {
            "access_token": "test-token",
            "user_id": "00000000-0000-0000-0000-000000000001",
            "username": "teacher",
            "email": "teacher@example.com",
            "display_name": "测试教师",
            "roles": [UserRole.TEACHER.value],
        }
    )
    return state


def test_navigation_is_limited_by_role() -> None:
    """教师、学生和管理员只能看到各自角色导航。"""

    teacher_items = navigation_for_roles([UserRole.TEACHER])
    student_items = navigation_for_roles([UserRole.STUDENT])
    admin_items = navigation_for_roles([UserRole.ADMIN])

    assert [item.key for item in teacher_items] == [
        "teacher.home",
        "teacher.courses",
        "teacher.knowledge",
        "teacher.questions",
        "teacher.exams",
        "teacher.generate",
        "teacher.review",
        "teacher.analytics",
    ]
    assert [item.key for item in student_items] == [
        "student.home",
        "student.exams",
        "student.results",
    ]
    assert [item.key for item in admin_items] == [
        "admin.home",
        "admin.users",
        "admin.roles",
        "admin.status",
    ]


def test_navigation_rejects_unauthenticated_or_forbidden_selection() -> None:
    """未登录和越权导航都必须返回统一的中文边界提示。"""

    assert select_navigation("teacher.courses", empty_login_state()) == "请先登录。"

    student_state = empty_login_state()
    student_state.update(
        {
            "access_token": "test-token",
            "username": "student",
            "roles": [UserRole.STUDENT.value],
        }
    )
    assert select_navigation("admin.users", student_state) == (
        "当前账号没有可访问的导航项。"
    )


def test_ui_errors_are_safe_and_chinese() -> None:
    """认证、权限和未知错误都不能把敏感内部信息直接展示给用户。"""

    assert format_ui_error(RuntimeError("内部数据库密码=secret")) == (
        "操作失败，请稍后重试。"
    )
    assert format_ui_error(PermissionDeniedError("学生无权执行“管理用户”。")) == (
        "学生无权执行“管理用户”。"
    )


def test_create_gradio_app_builds_login_shell(built_app: gr.Blocks) -> None:
    """应用工厂应能构建包含登录和角色导航事件的 Gradio Blocks。"""

    assert built_app.title == "EduAgent 教学评测平台"
    assert len(built_app.fns) >= 3


def test_registered_async_callbacks_keep_coroutine_semantics_and_execute(
    built_app: gr.Blocks,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """认证包装后异步回调仍由 Gradio 识别并在等待后执行正文。"""

    callback_names = (
        "generate_candidates",
        "confirm_review",
        "save_review_changes",
    )
    callbacks = {
        name: _registered_callback(built_app, name) for name in callback_names
    }
    assert all(inspect.iscoroutinefunction(callback) for callback in callbacks.values())

    monkeypatch.setattr(
        gradio_app_module,
        "_authenticated_state",
        lambda state: state,
    )
    result = asyncio.run(
        callbacks["generate_candidates"]("", None, None, None, 1, _teacher_state())
    )

    assert result[0] == []
    assert "请先选择课程后再生成。" in result[2]


def test_registered_sync_callback_behavior_is_unchanged(
    built_app: gr.Blocks,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """认证包装后的同步回调保持同步并返回原正文结果。"""

    callback = _registered_callback(built_app, "next_review_item")
    assert not inspect.iscoroutinefunction(callback)

    monkeypatch.setattr(
        gradio_app_module,
        "_authenticated_state",
        lambda state: state,
    )
    result = callback(None, [], _teacher_state())

    assert len(result) == 12
    assert result[8] is None
    assert "没有可复核的评分记录。" in result[-1]


@pytest.mark.parametrize(
    ("callback_name", "args", "is_async"),
    [
        ("generate_candidates", ("", None, None, None, 1, empty_login_state()), True),
        ("next_review_item", (None, [], empty_login_state()), False),
    ],
)
def test_registered_callbacks_keep_authentication_error_for_expired_session(
    built_app: gr.Blocks,
    callback_name: str,
    args: tuple[Any, ...],
    is_async: bool,
) -> None:
    """异步和同步注册回调都把失效会话转换为既有 UI 错误。"""

    callback = _registered_callback(built_app, callback_name)
    with pytest.raises(gr.Error) as error_info:
        if is_async:
            asyncio.run(callback(*args))
        else:
            callback(*args)

    assert error_info.value.args == ("登录状态已失效，请退出后重新登录。",)
