import pytest

from backend.app.domain.enums import (
    MVP_QUESTION_TYPES,
    OBJECTIVE_QUESTION_TYPES,
    SUBJECTIVE_QUESTION_TYPES,
    QuestionType,
    SubmissionStatus,
    UserRole,
)
from backend.app.domain.permissions import (
    Permission,
    PermissionDeniedError,
    any_role_has_permission,
    has_permission,
    permissions_for,
    require_permission,
)
from backend.app.schemas.ai import QuestionType as SchemaQuestionType


def test_question_type_values_match_spec_and_schema_alias() -> None:
    assert {question_type.value for question_type in QuestionType} == {
        "SINGLE_CHOICE",
        "MULTIPLE_CHOICE",
        "TRUE_FALSE",
        "FILL_BLANK",
        "SHORT_ANSWER",
        "ESSAY",
    }
    assert SchemaQuestionType is QuestionType


def test_question_type_groups_keep_objective_and_subjective_separate() -> None:
    assert MVP_QUESTION_TYPES == {
        QuestionType.SINGLE_CHOICE,
        QuestionType.TRUE_FALSE,
        QuestionType.SHORT_ANSWER,
    }
    assert OBJECTIVE_QUESTION_TYPES.isdisjoint(SUBJECTIVE_QUESTION_TYPES)
    assert QuestionType.SHORT_ANSWER in SUBJECTIVE_QUESTION_TYPES


def test_submission_status_sequence_matches_business_flow() -> None:
    assert [status.value for status in SubmissionStatus] == [
        "Draft",
        "Submitted",
        "Graded",
        "Reviewed",
    ]


def test_role_permissions_separate_ai_business_from_admin_management() -> None:
    assert has_permission(UserRole.TEACHER, Permission.REVIEW_LOW_CONFIDENCE_GRADING)
    assert has_permission(UserRole.ADMIN, Permission.MANAGE_USERS)
    assert has_permission(UserRole.STUDENT, Permission.SUBMIT_EXAMS)
    assert not has_permission(UserRole.ADMIN, Permission.REVIEW_LOW_CONFIDENCE_GRADING)
    assert not has_permission(UserRole.STUDENT, Permission.MANAGE_USERS)


def test_multi_role_permission_allows_any_matching_role() -> None:
    assert any_role_has_permission(
        [UserRole.STUDENT, UserRole.TEACHER],
        Permission.PUBLISH_EXAMS,
    )


def test_permission_denial_message_is_chinese() -> None:
    with pytest.raises(PermissionDeniedError, match="学生无权执行“管理用户”。"):
        require_permission(UserRole.STUDENT, Permission.MANAGE_USERS)


def test_role_permissions_are_immutable() -> None:
    teacher_permissions = permissions_for("teacher")

    with pytest.raises(AttributeError):
        teacher_permissions.add(Permission.MANAGE_USERS)  # type: ignore[attr-defined]
