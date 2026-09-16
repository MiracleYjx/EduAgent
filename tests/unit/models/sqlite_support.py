"""M3 结果、诊断、复核与运行模型测试共用的 SQLite 测试库辅助。

测试统一使用内存 SQLite，用于回归表结构、约束、关系与跨 Session 读回行为。
真实 PostgreSQL 上的列精度、约束与外键行为由 `alembic upgrade/check` 与一次性
验证库上的 pg_catalog 断言覆盖，不由 SQLite 结果替代。

SQLite 默认不启用外键约束，本模块在每条连接上显式执行 `PRAGMA foreign_keys=ON`，
避免“外键未被验证却被当作通过”的假证据。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from backend.app.core.database import Base
from backend.app.domain.enums import (
    AnswerStatus,
    ExamStatus,
    QuestionStatus,
    QuestionType,
    SubmissionStatus,
    UserRole,
)
from backend.app.models import Answer, Course, Exam, Question, Role, Submission, User

#: 结果类模型测试使用的标准单题满分。
DEFAULT_MAX_SCORE: Final[Decimal] = Decimal("10.00")


def create_sqlite_engine() -> Engine:
    """创建启用外键约束的内存 SQLite 引擎，并建好全部业务表。"""

    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
        """每条连接启用外键约束，让外键拒绝与级联路径真实生效。"""

        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return engine


def sqlite_foreign_keys_enabled(engine: Engine) -> bool:
    """返回当前连接是否真的启用了外键约束（防止假通过）。"""

    with engine.connect() as connection:
        from sqlalchemy import text

        return bool(connection.scalar(text("PRAGMA foreign_keys")))


@dataclass(frozen=True, slots=True)
class SubmissionFixture:
    """一份含客观题与主观题的最小答卷，供结果类模型测试复用。"""

    teacher_id: UUID
    student_id: UUID
    course_id: UUID
    objective_question_id: UUID
    subjective_question_id: UUID
    exam_id: UUID
    submission_id: UUID
    objective_answer_id: UUID
    subjective_answer_id: UUID


def seed_submission(
    session: Session,
    *,
    max_score: Decimal = DEFAULT_MAX_SCORE,
    status: SubmissionStatus = SubmissionStatus.SUBMITTED,
) -> SubmissionFixture:
    """创建教师、课程、客观题与主观题、考试、答卷和答案，并提交事务。"""

    teacher = User(
        username="teacher",
        email="teacher@example.com",
        password_hash="hashed-password",
    )
    teacher.roles.append(Role(name=UserRole.TEACHER))
    student = User(
        username="student",
        email="student@example.com",
        password_hash="hashed-password",
    )
    student.roles.append(Role(name=UserRole.STUDENT))
    course = Course(name="Python 基础", creator=teacher)
    objective = Question(
        course=course,
        creator=teacher,
        type=QuestionType.SINGLE_CHOICE,
        content="下列哪个是不可变类型？",
        options=["list", "tuple"],
        reference_answer="tuple",
        knowledge_points=["数据类型"],
        score=max_score,
        status=QuestionStatus.APPROVED,
    )
    subjective = Question(
        course=course,
        creator=teacher,
        type=QuestionType.SHORT_ANSWER,
        content="解释变量的作用。",
        reference_answer="变量用于保存数据。",
        scoring_rubric="说明保存和引用数据即可。",
        knowledge_points=["变量"],
        score=max_score,
        status=QuestionStatus.APPROVED,
    )
    exam = Exam(
        course=course,
        creator=teacher,
        title="第一章测验",
        questions=[objective, subjective],
        status=ExamStatus.PUBLISHED,
    )
    submission = Submission(exam=exam, student=student, status=status)
    objective_answer = Answer(
        submission=submission,
        question=objective,
        content="tuple",
        status=AnswerStatus.GRADED,
    )
    subjective_answer = Answer(
        submission=submission,
        question=subjective,
        content="变量用于保存数据。",
        status=AnswerStatus.GRADED,
    )
    session.add(submission)
    session.commit()
    return SubmissionFixture(
        teacher_id=teacher.id,
        student_id=student.id,
        course_id=course.id,
        objective_question_id=objective.id,
        subjective_question_id=subjective.id,
        exam_id=exam.id,
        submission_id=submission.id,
        objective_answer_id=objective_answer.id,
        subjective_answer_id=subjective_answer.id,
    )


__all__ = [
    "DEFAULT_MAX_SCORE",
    "SubmissionFixture",
    "create_sqlite_engine",
    "seed_submission",
    "sqlite_foreign_keys_enabled",
]
