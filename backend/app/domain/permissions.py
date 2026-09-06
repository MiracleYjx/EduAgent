"""角色权限定义与 RBAC 辅助函数。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from types import MappingProxyType

from backend.app.domain.enums import UserRole


class Permission(StrEnum):
    """平台基础权限标识。"""

    MANAGE_USERS = "users:manage"
    MANAGE_ROLES = "roles:manage"
    VIEW_SYSTEM_STATUS = "system:status:view"
    VIEW_COURSES = "courses:view"
    CREATE_COURSE = "courses:create"
    UPDATE_COURSE = "courses:update"
    DELETE_COURSE = "courses:delete"
    MANAGE_KNOWLEDGE_BASES = "knowledge_bases:manage"
    UPLOAD_DOCUMENTS = "documents:upload"
    CREATE_MANUAL_QUESTIONS = "questions:create_manual"
    GENERATE_QUESTION_CANDIDATES = "questions:generate_candidates"
    REVIEW_QUESTIONS = "questions:review"
    MANAGE_QUESTION_BANK = "questions:manage"
    CREATE_EXAMS = "exams:create"
    PUBLISH_EXAMS = "exams:publish"
    VIEW_EXAM_RESULTS = "exams:results:view"
    TRIGGER_GRADING = "grading:trigger"
    VIEW_GRADING_RESULTS = "grading:results:view"
    REVIEW_LOW_CONFIDENCE_GRADING = "grading:low_confidence:review"
    VIEW_AVAILABLE_EXAMS = "exams:available:view"
    TAKE_EXAMS = "exams:take"
    SUBMIT_EXAMS = "submissions:submit"
    VIEW_OWN_RESULTS = "results:own:view"


TEACHER_PERMISSIONS = frozenset(
    {
        Permission.VIEW_COURSES,
        Permission.CREATE_COURSE,
        Permission.UPDATE_COURSE,
        Permission.DELETE_COURSE,
        Permission.MANAGE_KNOWLEDGE_BASES,
        Permission.UPLOAD_DOCUMENTS,
        Permission.CREATE_MANUAL_QUESTIONS,
        Permission.GENERATE_QUESTION_CANDIDATES,
        Permission.REVIEW_QUESTIONS,
        Permission.MANAGE_QUESTION_BANK,
        Permission.CREATE_EXAMS,
        Permission.PUBLISH_EXAMS,
        Permission.VIEW_EXAM_RESULTS,
        Permission.TRIGGER_GRADING,
        Permission.VIEW_GRADING_RESULTS,
        Permission.REVIEW_LOW_CONFIDENCE_GRADING,
    },
)
STUDENT_PERMISSIONS = frozenset(
    {
        Permission.VIEW_AVAILABLE_EXAMS,
        Permission.TAKE_EXAMS,
        Permission.SUBMIT_EXAMS,
        Permission.VIEW_OWN_RESULTS,
    },
)
ADMIN_PERMISSIONS = frozenset(
    {
        Permission.MANAGE_USERS,
        Permission.MANAGE_ROLES,
        Permission.VIEW_SYSTEM_STATUS,
    },
)

ROLE_PERMISSIONS: Mapping[UserRole, frozenset[Permission]] = MappingProxyType(
    {
        UserRole.TEACHER: TEACHER_PERMISSIONS,
        UserRole.STUDENT: STUDENT_PERMISSIONS,
        UserRole.ADMIN: ADMIN_PERMISSIONS,
    },
)

ROLE_DISPLAY_NAMES: Mapping[UserRole, str] = MappingProxyType(
    {
        UserRole.TEACHER: "教师",
        UserRole.STUDENT: "学生",
        UserRole.ADMIN: "管理员",
    },
)

PERMISSION_DISPLAY_NAMES: Mapping[Permission, str] = MappingProxyType(
    {
        Permission.MANAGE_USERS: "管理用户",
        Permission.MANAGE_ROLES: "管理角色",
        Permission.VIEW_SYSTEM_STATUS: "查看系统运行状态",
        Permission.VIEW_COURSES: "查看课程",
        Permission.CREATE_COURSE: "创建课程",
        Permission.UPDATE_COURSE: "修改课程",
        Permission.DELETE_COURSE: "删除课程",
        Permission.MANAGE_KNOWLEDGE_BASES: "管理知识库",
        Permission.UPLOAD_DOCUMENTS: "上传课程资料",
        Permission.CREATE_MANUAL_QUESTIONS: "人工创建题目",
        Permission.GENERATE_QUESTION_CANDIDATES: "生成 AI 候选题目",
        Permission.REVIEW_QUESTIONS: "审核题目",
        Permission.MANAGE_QUESTION_BANK: "管理题库",
        Permission.CREATE_EXAMS: "创建考试",
        Permission.PUBLISH_EXAMS: "发布考试",
        Permission.VIEW_EXAM_RESULTS: "查看考试结果",
        Permission.TRIGGER_GRADING: "触发阅卷",
        Permission.VIEW_GRADING_RESULTS: "查看阅卷结果",
        Permission.REVIEW_LOW_CONFIDENCE_GRADING: "复核低置信度阅卷结果",
        Permission.VIEW_AVAILABLE_EXAMS: "查看可参加考试",
        Permission.TAKE_EXAMS: "参加考试",
        Permission.SUBMIT_EXAMS: "提交答卷",
        Permission.VIEW_OWN_RESULTS: "查看本人结果",
    },
)


class PermissionDeniedError(PermissionError):
    """角色权限不足时抛出的领域异常。"""


RoleInput = UserRole | str
PermissionInput = Permission | str


def normalize_role(role: RoleInput) -> UserRole:
    """将角色枚举、枚举名或枚举值统一为 UserRole。"""

    candidate = str(role).strip()
    for supported_role in UserRole:
        if candidate in {supported_role.name, supported_role.value}:
            return supported_role
        if candidate.lower() in {
            supported_role.name.lower(),
            supported_role.value.lower(),
        }:
            return supported_role
    raise ValueError(f"未知角色：{candidate}")


def normalize_permission(permission: PermissionInput) -> Permission:
    """将权限枚举、枚举名或枚举值统一为 Permission。"""

    candidate = str(permission).strip()
    for supported_permission in Permission:
        if candidate in {supported_permission.name, supported_permission.value}:
            return supported_permission
        if candidate.lower() in {
            supported_permission.name.lower(),
            supported_permission.value.lower(),
        }:
            return supported_permission
    raise ValueError(f"未知权限：{candidate}")


def permissions_for(role: RoleInput) -> frozenset[Permission]:
    """返回指定角色拥有的不可变权限集合。"""

    return ROLE_PERMISSIONS[normalize_role(role)]


def has_permission(role: RoleInput, permission: PermissionInput) -> bool:
    """判断单个角色是否拥有指定权限。"""

    return normalize_permission(permission) in permissions_for(role)


def _normalize_roles(roles: RoleInput | Iterable[RoleInput]) -> frozenset[UserRole]:
    """将单角色或多角色输入统一成角色集合。"""

    if isinstance(roles, str | UserRole):
        return frozenset({normalize_role(roles)})
    return frozenset(normalize_role(role) for role in roles)


def any_role_has_permission(
    roles: RoleInput | Iterable[RoleInput],
    permission: PermissionInput,
) -> bool:
    """判断任一角色是否拥有指定权限。"""

    normalized_permission = normalize_permission(permission)
    return any(
        normalized_permission in permissions_for(role)
        for role in _normalize_roles(roles)
    )


def describe_permission_denial(
    roles: RoleInput | Iterable[RoleInput],
    permission: PermissionInput,
) -> str:
    """生成面向 API/UI 的中文权限拒绝提示。"""

    normalized_roles = _normalize_roles(roles)
    normalized_permission = normalize_permission(permission)
    role_names = "、".join(
        ROLE_DISPLAY_NAMES[role] for role in sorted(normalized_roles)
    )
    permission_name = PERMISSION_DISPLAY_NAMES[normalized_permission]
    return f"{role_names}无权执行“{permission_name}”。"


def require_permission(
    roles: RoleInput | Iterable[RoleInput],
    permission: PermissionInput,
) -> None:
    """没有指定权限时抛出中文提示的领域异常。"""

    if not any_role_has_permission(roles, permission):
        raise PermissionDeniedError(describe_permission_denial(roles, permission))


__all__ = [
    "ADMIN_PERMISSIONS",
    "PERMISSION_DISPLAY_NAMES",
    "ROLE_DISPLAY_NAMES",
    "ROLE_PERMISSIONS",
    "STUDENT_PERMISSIONS",
    "TEACHER_PERMISSIONS",
    "Permission",
    "PermissionDeniedError",
    "any_role_has_permission",
    "describe_permission_denial",
    "has_permission",
    "normalize_permission",
    "normalize_role",
    "permissions_for",
    "require_permission",
]
