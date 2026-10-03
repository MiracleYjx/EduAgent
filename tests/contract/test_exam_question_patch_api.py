"""T173 new teacher routes use explicit DTO and authenticated identity (TCR §28)."""

from uuid import uuid4

import pytest

from backend.app.api.exams import get_exam_service
from backend.app.domain.enums import UserRole
from backend.app.schemas.exam_assembly import AssemblyResponse
from tests.contract.test_exam_assembly_api import _headers
from tests.contract.test_exam_participation_contract import (
    client as client,  # noqa: PLC0414
)
from tests.contract.test_exam_participation_contract import (
    session as session,  # noqa: PLC0414
)
from tests.unit.services.test_submission_service import add_student, add_teacher


class PatchDouble:
    def __init__(self):
        self.calls = []

    def preview_assembly(self, exam_id, *, teacher_id):
        self.calls.append((exam_id, teacher_id))
        return AssemblyResponse(
            exam_id=exam_id,
            current_status="Draft",
            exam_questions=[],
            conditions=[],
            total_score="0.00",
            publication_checks=[],
            assembly_constraints=None,
        )

    def patch_exam_question(self, exam_id, qid, payload, *, teacher_id):
        self.calls.append((exam_id, qid, payload, teacher_id))
        return AssemblyResponse(
            exam_id=exam_id,
            current_status="Draft",
            exam_questions=[],
            conditions=[],
            total_score="0.00",
            publication_checks=[],
            assembly_constraints=None,
        )


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"order_index": None},
        {"replacement_question_id": None},
        {"order_index": True},
        {"score": 3.2},
        {"score": "0.00"},
        {"score": "1.001"},
    ],
)
def test_invalid_patch_never_reaches_service(client, session, body):
    teacher = add_teacher(session)
    double = PatchDouble()
    client.app.dependency_overrides[get_exam_service] = lambda: double
    response = client.patch(
        f"/api/exams/{uuid4()}/questions/{uuid4()}",
        headers=_headers(teacher),
        json=body,
    )
    assert response.status_code == 422 and double.calls == []


@pytest.mark.parametrize(
    "body", [{"score": None}, {"score": "3.20"}, {"order_index": 2}]
)
def test_patch_preserves_presence_and_actual_actor(client, session, body):
    teacher = add_teacher(session)
    double = PatchDouble()
    exam, qid = uuid4(), uuid4()
    client.app.dependency_overrides[get_exam_service] = lambda: double
    response = client.patch(
        f"/api/exams/{exam}/questions/{qid}", headers=_headers(teacher), json=body
    )
    assert response.status_code == 200
    called = double.calls[0]
    assert called[0] == exam and called[1] == qid and called[3] == teacher.id
    assert called[2].model_fields_set == set(body)
    assert called[2].model_dump(mode="json", exclude_unset=True) == body


def test_preview_teacher_only_and_student_cannot_patch(client, session):
    teacher = add_teacher(session)
    student = add_student(session)
    double = PatchDouble()
    exam, qid = uuid4(), uuid4()
    client.app.dependency_overrides[get_exam_service] = lambda: double
    url = f"/api/exams/{exam}/assembly-preview"
    assert client.get(url).status_code == 401
    assert (
        client.get(url, headers=_headers(student, UserRole.STUDENT)).status_code == 403
    )
    assert (
        client.patch(
            f"/api/exams/{exam}/questions/{qid}",
            headers=_headers(student, UserRole.STUDENT),
            json={"score": "1.00"},
        ).status_code
        == 403
    )
    assert double.calls == []
    assert client.get(url, headers=_headers(teacher)).status_code == 200
    assert double.calls == [(exam, teacher.id)]
