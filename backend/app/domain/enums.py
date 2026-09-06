"""EduAgent 领域枚举与状态值。"""

from __future__ import annotations

from enum import StrEnum


class UserRole(StrEnum):
    """平台支持的基础角色。"""

    TEACHER = "Teacher"
    STUDENT = "Student"
    ADMIN = "Admin"


class QuestionType(StrEnum):
    """题库支持的题型标识。"""

    SINGLE_CHOICE = "SINGLE_CHOICE"
    MULTIPLE_CHOICE = "MULTIPLE_CHOICE"
    TRUE_FALSE = "TRUE_FALSE"
    FILL_BLANK = "FILL_BLANK"
    SHORT_ANSWER = "SHORT_ANSWER"
    ESSAY = "ESSAY"


OBJECTIVE_QUESTION_TYPES = frozenset(
    {
        QuestionType.SINGLE_CHOICE,
        QuestionType.MULTIPLE_CHOICE,
        QuestionType.TRUE_FALSE,
        QuestionType.FILL_BLANK,
    },
)
SUBJECTIVE_QUESTION_TYPES = frozenset(
    {
        QuestionType.SHORT_ANSWER,
        QuestionType.ESSAY,
    },
)
MVP_QUESTION_TYPES = frozenset(
    {
        QuestionType.SINGLE_CHOICE,
        QuestionType.TRUE_FALSE,
        QuestionType.SHORT_ANSWER,
    },
)


class QuestionStatus(StrEnum):
    """题目从创建、候选生成到审核可用的状态。"""

    DRAFT = "Draft"
    CANDIDATE_GENERATION = "Candidate Generation"
    PENDING_REVIEW = "Pending Review"
    APPROVED = "Approved"
    NEEDS_REVISION = "Needs Revision"
    PUBLISHED = "Published"


class DocumentStatus(StrEnum):
    """课程资料摄取状态。"""

    UPLOADED = "Uploaded"
    PARSING = "Parsing"
    CHUNKING = "Chunking"
    EMBEDDING = "Embedding"
    READY = "Ready"
    FAILED = "Failed"


class ExamStatus(StrEnum):
    """考试生命周期状态。"""

    DRAFT = "Draft"
    PUBLISHED = "Published"
    CLOSED = "Closed"
    ARCHIVED = "Archived"


class SubmissionStatus(StrEnum):
    """学生答卷生命周期状态。"""

    DRAFT = "Draft"
    SUBMITTED = "Submitted"
    GRADED = "Graded"
    REVIEWED = "Reviewed"


class AnswerStatus(StrEnum):
    """单题答案处理状态。"""

    DRAFT = "Draft"
    SUBMITTED = "Submitted"
    GRADING = "Grading"
    GRADED = "Graded"
    FAILED = "Failed"


class GradingMode(StrEnum):
    """题型路由后的评分模式。"""

    OBJECTIVE = "Objective"
    SUBJECTIVE = "Subjective"


class ValidationStatus(StrEnum):
    """结构化输出或业务校验结果。"""

    PENDING = "Pending"
    VALIDATED = "Validated"
    FAILED = "Failed"


class GradingStatus(StrEnum):
    """单题评分结果在自动阅卷和复核中的状态。"""

    PENDING = "Pending"
    VALIDATED = "Validated"
    ACCEPTED = "Accepted"
    PENDING_REVIEW = "Pending Review"
    FINAL = "Final"
    FAILED = "Failed"


class ReviewStatus(StrEnum):
    """人工复核状态。"""

    NOT_REQUIRED = "Not Required"
    PENDING_REVIEW = "Pending Review"
    CONFIRMED = "Confirmed"
    MODIFIED = "Modified"
    RE_GRADE = "Re-grade"
    FINAL = "Final"


class WorkflowStatus(StrEnum):
    """自动阅卷工作流状态。"""

    QUEUED = "Queued"
    RUNNING = "Running"
    PAUSED = "Paused"
    FAILED = "Failed"
    COMPLETED = "Completed"


__all__ = [
    "MVP_QUESTION_TYPES",
    "OBJECTIVE_QUESTION_TYPES",
    "SUBJECTIVE_QUESTION_TYPES",
    "AnswerStatus",
    "DocumentStatus",
    "ExamStatus",
    "GradingMode",
    "GradingStatus",
    "QuestionStatus",
    "QuestionType",
    "ReviewStatus",
    "SubmissionStatus",
    "UserRole",
    "ValidationStatus",
    "WorkflowStatus",
]
