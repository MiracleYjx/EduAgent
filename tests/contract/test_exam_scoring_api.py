"""T174 grading-basis transport and teacher identity (TCR §29)."""

from uuid import uuid4

import pytest

from backend.app.api.exams import get_exam_scoring_service
from backend.app.domain.enums import UserRole
from backend.app.schemas.exam_scoring import ScoringBasisView
from backend.app.services.exam_assembly_service import AssemblyError
from tests.contract.test_exam_assembly_api import _headers
from tests.contract.test_exam_participation_contract import (
    client as client,  # noqa: PLC0414
)
from tests.contract.test_exam_participation_contract import (
    session as session,  # noqa: PLC0414
)
from tests.unit.services.test_submission_service import add_student, add_teacher


class ScoringDouble:
    def __init__(self):
        self.calls = []
        self.error = None

    def get_scoring_basis(self, exam_id, question_id, *, teacher_id):
        self.calls.append((exam_id, question_id, teacher_id))
        if self.error:
            raise self.error
        return ScoringBasisView(
            exam_id=exam_id,
            exam_question_id=uuid4(),
            question_id=question_id,
            question_type="SHORT_ANSWER",
            source_rubric="Real rubric",
            question_validation_revision=1,
            explicit_score=None,
            effective_score="3.00",
            question_score="3.00",
            base_score=None,
            basis=None,
            editable=True,
        )

    def prepare_scoring_basis(self, exam_id, question_id, payload, *, teacher_id):
        result = self.get_scoring_basis(exam_id, question_id, teacher_id=teacher_id)
        self.calls.append(payload)
        return result

    def confirm_scoring_basis(self, exam_id, question_id, payload, *, teacher_id):
        result = self.get_scoring_basis(exam_id, question_id, teacher_id=teacher_id)
        self.calls.append(payload)
        return result


def body():
    return {
        "expected_question_validation_revision": 1,
        "expected_effective_score": "3.00",
        "expected_base_score": None,
        "expected_basis": None,
        "additive": False,
        "points": [],
    }


def test_get_and_prepare_use_actual_actor_and_decimal_strings(client, session):
    actor = add_teacher(session)
    double = ScoringDouble()
    exam, qid = uuid4(), uuid4()
    client.app.dependency_overrides[get_exam_scoring_service] = lambda: double
    url = f"/api/exams/{exam}/questions/{qid}/scoring-basis"
    result = client.get(url, headers=_headers(actor))
    assert result.status_code == 200 and result.json()["effective_score"] == "3.00"
    assert result.json()["base_score"] is None
    assert (
        client.post(url + "/prepare", headers=_headers(actor), json=body()).status_code
        == 200
    )
    assert double.calls[0] == (exam, qid, actor.id)
    assert double.calls[-1].model_dump(mode="json") == body()


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_effective_score", 3.0),
        ("expected_effective_score", "3.001"),
        ("additive", 1),
        ("expected_question_validation_revision", True),
    ],
)
def test_invalid_inputs_do_not_reach_service(client, session, field, value):
    actor = add_teacher(session)
    double = ScoringDouble()
    payload = body()
    payload[field] = value
    client.app.dependency_overrides[get_exam_scoring_service] = lambda: double
    response = client.post(
        f"/api/exams/{uuid4()}/questions/{uuid4()}/scoring-basis/prepare",
        headers=_headers(actor),
        json=payload,
    )
    assert response.status_code == 422 and double.calls == []


def test_basis_endpoints_deny_student_and_keep_domain_error(client, session):
    student = add_student(session)
    teacher = add_teacher(session)
    double = ScoringDouble()
    client.app.dependency_overrides[get_exam_scoring_service] = lambda: double
    url = f"/api/exams/{uuid4()}/questions/{uuid4()}/scoring-basis"
    for suffix in ["", "/prepare", "/confirm"]:
        response = (
            client.get(url, headers=_headers(student, UserRole.STUDENT))
            if not suffix
            else client.post(
                url + suffix, headers=_headers(student, UserRole.STUDENT), json=body()
            )
        )
        assert response.status_code == 403
    assert double.calls == []
    double.error = AssemblyError(
        "RUBRIC_REVIEW_REQUIRED", "需要真实核对", current_status="Draft"
    )
    response = client.get(url, headers=_headers(teacher))
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "RUBRIC_REVIEW_REQUIRED"


def test_confirm_transports_current_basis_and_actual_actor(client, session):
    actor = add_teacher(session)
    double = ScoringDouble()
    exam, qid, preparation = uuid4(), uuid4(), uuid4()
    client.app.dependency_overrides[get_exam_scoring_service] = lambda: double
    payload = body()
    payload.pop("additive")
    payload.pop("points")
    payload["expected_base_score"] = "3.00"
    payload["expected_basis"] = {
        "kind": "subjective",
        "rounding_mode": "ROUND_HALF_UP",
        "points": [],
        "additive": False,
        "rounding_delta": None,
        "confirmation": None,
        "preparation_id": str(preparation),
    }
    payload.update(
        preparation_id=str(preparation),
        confirmed_points=[],
        reason="核对原文定性标准适用于本场。",
    )
    response = client.post(
        f"/api/exams/{exam}/questions/{qid}/scoring-basis/confirm",
        headers=_headers(actor),
        json=payload,
    )
    assert response.status_code == 200
    assert double.calls[0] == (exam, qid, actor.id)
    assert double.calls[-1].model_dump(mode="json") == payload
