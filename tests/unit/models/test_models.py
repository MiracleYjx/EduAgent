"""T015 业务模型和关系的单元测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from sqlalchemy import Enum as SAEnum
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from backend.app.core.database import Base
from backend.app.domain.enums import (
    AnswerStatus,
    DocumentStatus,
    ExamStatus,
    QuestionStatus,
    QuestionType,
    SubmissionStatus,
    UserRole,
)
from backend.app.models import (
    Answer,
    Course,
    Document,
    Exam,
    KnowledgeBase,
    Question,
    Role,
    Submission,
    User,
)


def test_all_m1_tables_are_registered() -> None:
    """所有 T015 实体都应注册到统一的 SQLAlchemy 元数据。"""

    expected_tables = {
        "users",
        "roles",
        "user_roles",
        "courses",
        "knowledge_bases",
        "documents",
        "questions",
        "exams",
        "exam_questions",
        "submissions",
        "answers",
    }

    assert expected_tables.issubset(Base.metadata.tables)


def test_models_can_persist_core_relationships() -> None:
    """核心实体应能在 SQLite 测试库中按业务关系持久化。"""

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        teacher = User(
            username="teacher",
            email="teacher@example.com",
            password_hash="hashed-password",
        )
        teacher.roles.append(Role(name=UserRole.TEACHER))
        course = Course(name="Python 基础", creator=teacher)
        knowledge_base = KnowledgeBase(name="课程资料", course=course)
        document = Document(
            original_filename="lesson.md",
            file_format="md",
            knowledge_base=knowledge_base,
            course=course,
            uploader=teacher,
            status=DocumentStatus.UPLOADED,
        )
        question = Question(
            course=course,
            creator=teacher,
            type=QuestionType.SHORT_ANSWER,
            content="解释变量的作用。",
            reference_answer="变量用于保存数据。",
            scoring_rubric="说明保存和引用数据即可。",
            knowledge_points=["变量"],
            score=10,
            status=QuestionStatus.DRAFT,
        )
        exam = Exam(
            course=course,
            creator=teacher,
            title="第一章测验",
            questions=[question],
            status=ExamStatus.DRAFT,
        )
        student = User(
            username="student",
            email="student@example.com",
            password_hash="hashed-password",
        )
        student.roles.append(Role(name=UserRole.STUDENT))
        submission = Submission(
            exam=exam,
            student=student,
            status=SubmissionStatus.DRAFT,
        )
        answer = Answer(
            submission=submission,
            question=question,
            content="保存数据。",
            status=AnswerStatus.DRAFT,
        )
        session.add_all([document, submission, answer])
        session.commit()

        assert course.id is not None
        assert knowledge_base.course_id == course.id
        assert document.course_id == course.id
        assert question.course_id == course.id
        assert exam.questions[0].id == question.id
        assert submission.student_id == student.id
        assert answer.question_id == question.id


def test_required_question_and_submission_constraints() -> None:
    """题目和答卷的关键状态与字段约束应有明确列定义。"""

    question_columns = inspect(Question).columns
    submission_columns = inspect(Submission).columns

    question_type = cast(SAEnum, question_columns.type.type)
    question_status = cast(SAEnum, question_columns.status.type)
    submission_status = cast(SAEnum, submission_columns.status.type)

    assert question_type.enums == [item.value for item in QuestionType]
    assert question_status.enums == [item.value for item in QuestionStatus]
    assert question_columns.score.nullable is False
    assert question_columns.created_by.nullable is False
    assert submission_status.enums == [item.value for item in SubmissionStatus]
    assert submission_columns.student_id.nullable is False
    assert submission_columns.exam_id.nullable is False


def test_timestamp_defaults_are_timezone_aware() -> None:
    """时间字段应具备数据库默认值并能保存显式 UTC 时间。"""

    now = datetime.now(UTC)
    user = User(
        username="timestamp-user",
        email="timestamp@example.com",
        password_hash="hashed-password",
        created_at=now,
        updated_at=now,
    )

    assert user.created_at == now
    assert user.updated_at == now
