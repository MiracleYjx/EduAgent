"""EduAgent 业务持久化模型。"""

from backend.app.core.database import Base
from backend.app.models.answer import Answer
from backend.app.models.associations import exam_questions, user_roles
from backend.app.models.course import Course
from backend.app.models.document import Document
from backend.app.models.exam import Exam
from backend.app.models.exam_participant import ExamParticipant
from backend.app.models.knowledge_base import KnowledgeBase
from backend.app.models.question import Question
from backend.app.models.role import Role
from backend.app.models.submission import Submission
from backend.app.models.user import User


def register_models() -> None:
    """确保所有模型已导入并注册到统一元数据。"""


__all__ = [
    "Answer",
    "Base",
    "Course",
    "Document",
    "Exam",
    "ExamParticipant",
    "KnowledgeBase",
    "Question",
    "Role",
    "Submission",
    "User",
    "exam_questions",
    "register_models",
    "user_roles",
]
