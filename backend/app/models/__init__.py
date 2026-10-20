"""EduAgent 业务持久化模型。"""

from backend.app.core.database import Base
from backend.app.models.agent_run import AgentRun
from backend.app.models.answer import Answer
from backend.app.models.associations import exam_questions, user_roles
from backend.app.models.audit_log import AuditLog
from backend.app.models.chapter import Chapter
from backend.app.models.course import Course
from backend.app.models.diagnosis_report import DiagnosisReport
from backend.app.models.document import Document
from backend.app.models.document_chunk import DocumentChunk
from backend.app.models.duplicate_candidate import DuplicateCandidate
from backend.app.models.exam import Exam
from backend.app.models.exam_participant import ExamParticipant
from backend.app.models.exam_question import ExamQuestion
from backend.app.models.exam_result import ExamResult
from backend.app.models.export_file import ExportFile
from backend.app.models.extracted_question import ExtractedQuestion
from backend.app.models.grading_result import GradingResult
from backend.app.models.knowledge_base import KnowledgeBase
from backend.app.models.operation_run import OperationRun
from backend.app.models.paper_import import PaperImport
from backend.app.models.practice import (
    GradeAttempt,
    PracticeAnswer,
    PracticeItem,
    PracticeSession,
)
from backend.app.models.question import Question
from backend.app.models.question_asset import QuestionAsset
from backend.app.models.question_generation_metadata import QuestionGenerationMetadata
from backend.app.models.question_origin import QuestionOrigin
from backend.app.models.question_revision import QuestionRevision
from backend.app.models.question_revision_comment import QuestionRevisionComment
from backend.app.models.question_source_chunk import QuestionSourceChunk
from backend.app.models.question_source_paper import QuestionSourcePaper
from backend.app.models.question_validation_result import QuestionValidationResult
from backend.app.models.review_record import ReviewRecord
from backend.app.models.role import Role
from backend.app.models.score_dispute import ScoreDispute
from backend.app.models.source_page import SourcePage
from backend.app.models.subject import (
    KnowledgePoint,
    MaterialSeries,
    MaterialVersion,
    Subject,
)
from backend.app.models.submission import Submission
from backend.app.models.user import User
from backend.app.models.workflow_run import WorkflowRun


def register_models() -> None:
    """确保所有模型已导入并注册到统一元数据。"""


__all__ = [
    "AgentRun",
    "Answer",
    "AuditLog",
    "Base",
    "Chapter",
    "Course",
    "DiagnosisReport",
    "Document",
    "DocumentChunk",
    "DuplicateCandidate",
    "Exam",
    "ExamParticipant",
    "ExamQuestion",
    "ExamResult",
    "ExportFile",
    "ExtractedQuestion",
    "GradeAttempt",
    "GradingResult",
    "KnowledgeBase",
    "KnowledgePoint",
    "MaterialSeries",
    "MaterialVersion",
    "OperationRun",
    "PaperImport",
    "PracticeAnswer",
    "PracticeItem",
    "PracticeSession",
    "Question",
    "QuestionAsset",
    "QuestionGenerationMetadata",
    "QuestionOrigin",
    "QuestionRevision",
    "QuestionRevisionComment",
    "QuestionSourceChunk",
    "QuestionSourcePaper",
    "QuestionValidationResult",
    "ReviewRecord",
    "Role",
    "ScoreDispute",
    "SourcePage",
    "Subject",
    "Submission",
    "User",
    "WorkflowRun",
    "exam_questions",
    "register_models",
    "user_roles",
]
