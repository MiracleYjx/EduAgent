"""T165 teacher authorization and persistent adaptation source API contract."""

from typing import Any
from uuid import uuid4

from backend.app.domain.enums import (
    QuestionSourceType,
    QuestionStatus,
    QuestionType,
    UserRole,
)
from backend.app.models import Question
from tests.contract.test_question_generation_api_contract import (
    _headers,
    _seed_chunk,
    _service,
)
from tests.contract.test_question_generation_api_contract import (
    client_factory as _client_factory,
)
from tests.contract.test_question_generation_api_contract import (
    scenario as _scenario,
)
from tests.contract.test_question_generation_api_contract import (
    session as _session,
)
from tests.support.question_generation_doubles import make_candidate, make_chunk

session = _session
scenario = _scenario
client_factory = _client_factory


def setup(scenario: Any) -> tuple[Any, Any]:
    session, course, teacher = (
        scenario["session"],
        scenario["course"],
        scenario["teacher"],
    )
    parent = Question(
        course_id=course.id,
        type=QuestionType.SINGLE_CHOICE,
        content="Original API parent",
        options={"C": "three", "A": "one"},
        reference_answer="C",
        score=2,
        created_by=teacher.id,
        status=QuestionStatus.DRAFT,
        source_type=QuestionSourceType.MANUAL,
    )
    session.add(parent)
    session.commit()
    chunk = _seed_chunk(
        session, course=course, teacher=teacher, content="Variables store data."
    )
    service = _service(
        session,
        candidates=[
            make_candidate(
                source_context_ids=[str(chunk.id)], analysis="Independent explanation"
            )
        ],
        chunks=[
            make_chunk(
                str(chunk.id),
                course_id=str(course.id),
                document_id=str(chunk.document_id),
            )
        ],
    )
    return parent, service


def test_adaptation_route_and_persistent_parent_detail(
    scenario: Any, client_factory: Any
) -> None:
    parent, service = setup(scenario)
    client = client_factory(service)
    body = {
        "course_id": str(parent.course_id),
        "source_question_id": str(parent.id),
        "adaptation_type": "rewrite",
        "target_score": "2",
    }
    headers = _headers(scenario["teacher"], UserRole.TEACHER)
    response = client.post(
        "/api/question-generation/adaptations", json=body, headers=headers
    )
    assert response.status_code == 201, response.text
    candidate = response.json()["candidates"][0]
    assert candidate["source_type"] == "adapted" and candidate["parent_sources"][0][
        "source_question_id"
    ] == str(parent.id)
    assert (
        candidate["analysis"] == "Independent explanation"
        and candidate["source_status"] == "persisted"
    )
    detail = client.get("/api/questions/" + candidate["candidate_id"], headers=headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["parent_sources"][0]["source_question_id"] == str(parent.id)
    assert detail.json()["paper_source"] is None
    original = client.get(
        "/api/question-generation/candidates/" + str(parent.id), headers=headers
    )
    assert original.status_code == 200, original.text
    assert list(original.json()["options"]) == ["C", "A"]


def test_adaptation_permission_and_parent_boundaries(
    scenario: Any, client_factory: Any
) -> None:
    parent, service = setup(scenario)
    client = client_factory(service)
    body = {"course_id": str(parent.course_id), "source_question_id": str(parent.id)}
    assert (
        client.post("/api/question-generation/adaptations", json=body).status_code
        == 401
    )
    assert (
        client.post(
            "/api/question-generation/adaptations",
            json=body,
            headers=_headers(scenario["student"], UserRole.STUDENT),
        ).status_code
        == 403
    )
    headers = _headers(scenario["teacher"], UserRole.TEACHER)
    body["source_question_id"] = str(uuid4())
    response = client.post(
        "/api/question-generation/adaptations", json=body, headers=headers
    )
    assert (
        response.status_code == 422
        and response.json()["detail"]["error_code"]
        == "QUESTION_ADAPTATION_PARENT_INVALID"
    )
    body["source_question_id"] = str(parent.id)
    body["target_score"] = "2.001"
    assert (
        client.post(
            "/api/question-generation/adaptations", json=body, headers=headers
        ).status_code
        == 422
    )
