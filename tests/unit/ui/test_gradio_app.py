"""T019 Gradio 应用外壳、登录状态和角色导航测试。"""

from __future__ import annotations

from backend.app.domain.enums import UserRole
from backend.app.domain.permissions import PermissionDeniedError
from backend.app.ui.gradio_app import (
    create_gradio_app,
    empty_login_state,
    format_ui_error,
    navigation_for_roles,
    select_navigation,
)


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


def test_create_gradio_app_builds_login_shell() -> None:
    """应用工厂应能构建包含登录和角色导航事件的 Gradio Blocks。"""

    app = create_gradio_app()

    assert app.title == "EduAgent 教学评测平台"
    assert len(app.fns) >= 3
