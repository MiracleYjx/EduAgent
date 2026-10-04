"""T175 real JWT course cascade rejection transport (TCR §30)."""

from backend.app.domain.enums import ExamStatus
from backend.app.models import Exam
from tests.contract.test_exam_assembly_api import _headers
from tests.contract.test_exam_participation_contract import (
    client as client,  # noqa: PLC0414
)
from tests.contract.test_exam_participation_contract import (
    session as session,  # noqa: PLC0414
)
from tests.unit.services.test_submission_service import add_course, add_teacher


def test_course_delete_reports_current_protection_without_erasing_exam(client, session):
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
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "COURSE_REFERENCED_IMMUTABLE"
    assert (
        response.json()["detail"]["current_status"] is None
    )  # Course has no status enum.
    assert session.get(Exam, identity).status == ExamStatus.PUBLISHED
