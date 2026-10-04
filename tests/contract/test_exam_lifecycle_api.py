"""T175 old exam writes preserve structured immutable errors (TCR §30)."""

from uuid import uuid4

import pytest

from backend.app.api.exams import get_exam_service
from backend.app.domain.enums import ExamStatus
from backend.app.services.exam_service import ExamPublishedImmutableError
from tests.contract.test_exam_assembly_api import _headers
from tests.contract.test_exam_participation_contract import (
    client as client,  # noqa: PLC0414
)
from tests.contract.test_exam_participation_contract import (
    session as session,  # noqa: PLC0414
)
from tests.unit.services.test_submission_service import add_teacher


class FrozenExam:
    def update_exam(self, *args, **kwargs):
        raise ExamPublishedImmutableError(ExamStatus.ARCHIVED)

    add_questions = update_exam
    remove_questions = update_exam


@pytest.mark.parametrize(
    "method,suffix", [("PATCH", ""), ("POST", "/questions"), ("DELETE", "/questions")]
)
def test_old_exam_writes_return_same_frozen_contract(client, session, method, suffix):
    teacher = add_teacher(session)
    client.app.dependency_overrides[get_exam_service] = lambda: FrozenExam()
    payload = (
        {"title": "Late title"} if not suffix else {"question_ids": [str(uuid4())]}
    )
    response = client.request(
        method, f"/api/exams/{uuid4()}{suffix}", headers=_headers(teacher), json=payload
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "EXAM_PUBLISHED_IMMUTABLE"
    assert response.json()["detail"]["current_status"] == "Archived"
    assert "不能修改" in response.json()["detail"]["message"]
