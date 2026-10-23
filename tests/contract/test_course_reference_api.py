"""T175 real JWT course cascade rejection transport (TCR §30)."""

from backend.app.domain.enums import ExamStatus
from backend.app.models import Exam
from tests.contract.test_exam_assembly_api import _headers
from tests.support.retained_exam_fixtures import (
    client as client,  # noqa: PLC0414
)
from tests.support.retained_exam_fixtures import (
    session as session,  # noqa: PLC0414
)
from tests.unit.services.test_submission_service import add_course, add_teacher


def test_course_delete_is_retired_and_never_erases_exam(client, session):
    actor = add_teacher(session)
    course = add_course(session, actor)
    exam = Exam(
        course_id=course.id,
        created_by=actor.id,
        title="Retained publication",
        status=ExamStatus.PUBLISHED,
    )
    session.add(exam)
    session.commit()
    identity = exam.id
    response = client.delete(f"/api/courses/{course.id}", headers=_headers(actor))
    # TCR（T081）：旧课程删除入口已随 T077 退役（生产工厂不再挂载），保留组件套件
    # 断言退役 404 而不是旧 409 合同；“已发布试卷不得被删除”仍由下方持久化事实验证。
    assert response.status_code == 404
    assert session.get(Exam, identity).status == ExamStatus.PUBLISHED
