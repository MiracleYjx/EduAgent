"""EduAgent 业务持久化模型。"""

from backend.app.core.database import Base
from backend.app.models.agent_run import AgentRun
from backend.app.models.answer import Answer
from backend.app.models.associations import exam_questions, user_roles
from backend.app.models.course import Course
from backend.app.models.diagnosis_report import DiagnosisReport
from backend.app.models.document import Document
from backend.app.models.document_chunk import DocumentChunk
from backend.app.models.exam import Exam
from backend.app.models.exam_participant import ExamParticipant
from backend.app.models.exam_result import ExamResult
from backend.app.models.grading_result import GradingResult
from backend.app.models.knowledge_base import KnowledgeBase
from backend.app.models.question import Question
from backend.app.models.review_record import ReviewRecord
from backend.app.models.role import Role
from backend.app.models.submission import Submission
from backend.app.models.user import User
from backend.app.models.workflow_run import WorkflowRun


def register_models() -> None:
    """确保所有模型已导入并注册到统一元数据。"""


__all__ = [
    "AgentRun",
    "Answer",
    "Base",
    "Course",
    "DiagnosisReport",
    "Document",
    "DocumentChunk",
    "Exam",
    "ExamParticipant",
    "ExamResult",
    "GradingResult",
    "KnowledgeBase",
    "Question",
    "ReviewRecord",
    "Role",
    "Submission",
    "User",
    "WorkflowRun",
    "exam_questions",
    "register_models",
    "user_roles",
]
