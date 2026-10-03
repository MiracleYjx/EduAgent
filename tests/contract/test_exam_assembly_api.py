"""T172 HTTP transport/auth/schema checks; actual selection transactions are tested on PG."""

from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.app.api.exams import get_exam_service
from backend.app.domain.enums import UserRole
from backend.app.schemas.exam_assembly import AssemblyResponse
from backend.app.services.auth_service import create_access_token
from backend.app.services.exam_assembly_service import AssemblyError
from tests.contract.test_exam_participation_contract import (
    client as client,  # noqa: PLC0414 - reused pytest fixture
)
from tests.contract.test_exam_participation_contract import (
    session as session,  # noqa: PLC0414 - reused pytest fixture
)
from tests.unit.services.test_submission_service import add_student, add_teacher
from tests.unit.settings_helpers import build_test_settings


class AssemblyDouble:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def assemble_exam(self, exam_id, payload, *, teacher_id):
        self.calls.append((exam_id, payload, teacher_id))
        if self.error:
            raise self.error
        return self.response


def _headers(user, role=UserRole.TEACHER):
    token = create_access_token(
        user.id, secret_key=build_test_settings().JWT_SECRET_KEY, roles=[role]
    )
    return {"Authorization": f"Bearer {token}"}


def _payload():
    return {
        "course_id": str(uuid4()),
        "question_count": 1,
        "type_distribution": [{"question_type": "SHORT_ANSWER", "count": 1}],
        "knowledge_coverage": [],
        "total_score": "7.25",
        "score_overrides": [],
    }


def test_assemble_preserves_decimal_and_authenticated_teacher(
    client: TestClient, session: Session
):
    teacher = add_teacher(session)
    exam_id = uuid4()
    qid = uuid4()
    response = AssemblyResponse.model_validate(
        {
            "exam_id": exam_id,
            "current_status": "Draft",
            "exam_questions": [
                {
                    "id": uuid4(),
                    "exam_id": exam_id,
                    "question_id": qid,
                    "order_index": 1,
                    "score": "7.25",
                    "effective_score": "7.25",
                }
            ],
            "conditions": [],
            "total_score": "7.25",
            "publication_checks": [],
            "assembly_constraints": None,
        }
    )
    double = AssemblyDouble(response=response)
    client.app.dependency_overrides[get_exam_service] = lambda: double
    result = client.post(
        f"/api/exams/{exam_id}/assemble", headers=_headers(teacher), json=_payload()
    )
    assert result.status_code == 200
    assert result.json()["total_score"] == "7.25"
    assert result.json()["exam_questions"][0]["score"] == "7.25"
    assert double.calls[0][0] == exam_id and double.calls[0][2] == teacher.id
    assert double.calls[0][1].total_score == Decimal("7.25")


@pytest.mark.parametrize("value", [7.25, 7, True, "7.251", "NaN", "0"])
def test_invalid_total_never_reaches_service(
    client: TestClient, session: Session, value
):
    teacher = add_teacher(session)
    double = AssemblyDouble()
    body = _payload()
    body["total_score"] = value
    client.app.dependency_overrides[get_exam_service] = lambda: double
    result = client.post(
        f"/api/exams/{uuid4()}/assemble", headers=_headers(teacher), json=body
    )
    assert result.status_code == 422
    assert double.calls == []


def test_student_and_anonymous_cannot_assemble(client: TestClient, session: Session):
    student = add_student(session)
    double = AssemblyDouble()
    client.app.dependency_overrides[get_exam_service] = lambda: double
    url = f"/api/exams/{uuid4()}/assemble"
    assert client.post(url, json=_payload()).status_code == 401
    assert (
        client.post(
            url, headers=_headers(student, UserRole.STUDENT), json=_payload()
        ).status_code
        == 403
    )
    assert double.calls == []


@pytest.mark.parametrize("saved", [True, False, None])
def test_business_failure_retains_structured_intent_status(
    client: TestClient, session: Session, saved
):
    teacher = add_teacher(session)
    error = AssemblyError(
        "EXAM_ASSEMBLY_UNSATISFIED",
        "真实组卷缺口",
        current_status="Draft",
        details={"intent_saved": saved, "gaps": [{"kind": "type", "missing": 1}]},
    )
    double = AssemblyDouble(error=error)
    client.app.dependency_overrides[get_exam_service] = lambda: double
    result = client.post(
        f"/api/exams/{uuid4()}/assemble", headers=_headers(teacher), json=_payload()
    )
    assert result.status_code == 409
    assert result.json()["detail"]["code"] == "EXAM_ASSEMBLY_UNSATISFIED"
    assert result.json()["detail"]["intent_saved"] is saved
    assert result.json()["detail"]["gaps"] == [{"kind": "type", "missing": 1}]
