"""T170 旧考试入口通过明确关联实体写入，历史未知分数保持未知。"""

from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from backend.app.domain.enums import ExamStatus
from backend.app.models import Exam, Question
from backend.app.services.exam_service import ExamService
from tests.unit.services.test_exam_service import (
    add_course,
    add_question,
    add_teacher,
)
from tests.unit.services.test_exam_service import (
    session as session,  # noqa: PLC0414 - explicitly re-exported pytest fixture
)


def test_legacy_entry_writes_persistent_contiguous_order(session: Session) -> None:
    teacher = add_teacher(session)
    course = add_course(session, teacher)
    ids = [
        add_question(session, course, teacher, content=f"题目{i}", approved=True)
        for i in range(3)
    ]
    service = ExamService(session)
    initial = service.create_exam(
        course.id, "关联实体", question_ids=ids[:2][::-1], created_by=teacher.id
    )
    exam = session.get(Exam, UUID(initial.id))
    assert exam is not None
    assert [
        (link.question_id, link.order_index) for link in exam.exam_question_links
    ] == [(ids[0], 1), (ids[1], 2)]
    service.add_questions(exam.id, [ids[1], ids[2]], teacher_id=teacher.id)
    assert len(exam.exam_question_links) == 3
    removed = service.remove_questions(exam.id, [ids[0]], teacher_id=teacher.id)
    assert removed.question_ids == [str(ids[1]), str(ids[2])]
    assert [link.order_index for link in exam.exam_question_links] == [1, 2]
    assert [question.id for question in exam.questions] == ids[1:]
    assert all(link.score is None for link in exam.exam_question_links)


def test_summary_uses_explicit_score_and_preserves_unknown_history(
    session: Session,
) -> None:
    teacher = add_teacher(session)
    course = add_course(session, teacher)
    qid = add_question(session, course, teacher, approved=True)
    service = ExamService(session)
    summary = service.create_exam(
        course.id, "未知历史", question_ids=[qid], created_by=teacher.id
    )
    exam = session.get(Exam, UUID(summary.id))
    assert exam is not None
    assert summary.total_score == Decimal("10.00")
    exam.exam_question_links[0].score = Decimal("7.25")
    session.commit()
    assert service.get_exam(exam.id).total_score == Decimal("7.25")
    exam.exam_question_links[0].score = None
    exam.status = ExamStatus.PUBLISHED
    session.get(Question, qid).score = Decimal("99.00")
    session.commit()
    assert service.get_exam(exam.id).total_score is None
    assert service.get_exam(exam.id).question_ids == [str(qid)]
