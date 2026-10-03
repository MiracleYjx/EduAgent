"""T166 generation orchestration and every server approval route; TCR section 20.

Controlled provider outputs prove persistence and authorization, not measured semantic quality.
"""

from __future__ import annotations

import asyncio
from io import BytesIO
from typing import Any
from uuid import UUID

import pytest
from PIL import Image
from sqlalchemy import func, select

from backend.app.ai.agents.question_agent import QuestionAgent
from backend.app.api.question_generation import QuestionGenerationService
from backend.app.domain.enums import QuestionStatus, UserRole
from backend.app.models import (
    Question,
    QuestionRevisionComment,
    QuestionValidationResult,
)
from backend.app.schemas.image_assessment import ImageManualCheckRequest
from backend.app.services.content_validation_service import ContentValidationService
from backend.app.services.question_asset_service import QuestionAssetService
from tests.contract.test_question_generation_api_contract import _headers, _seed_chunk
from tests.contract.test_question_generation_api_contract import (
    client_factory as _client_factory,
)
from tests.contract.test_question_generation_api_contract import scenario as _scenario
from tests.contract.test_question_generation_api_contract import session as _session
from tests.support.question_generation_doubles import (
    StubEmbeddingProvider,
    StubQuestionProvider,
    StubRetriever,
    make_candidate,
    make_chunk,
)
from tests.support.semantic_validation_doubles import StubSemanticProvider
from tests.unit.settings_helpers import build_test_settings

session = _session
scenario = _scenario
client_factory = _client_factory


def service_case(
    scenario: dict[str, Any],
    semantic: StubSemanticProvider | None = None,
    *,
    count: int = 1,
):
    session, teacher, course = (
        scenario["session"],
        scenario["teacher"],
        scenario["course"],
    )
    template = make_chunk()
    chunk = _seed_chunk(
        session, course=course, teacher=teacher, content=template.content
    )
    provider = semantic if semantic is not None else StubSemanticProvider()
    service = QuestionGenerationService(
        session=session,
        agent=QuestionAgent(
            provider=StubQuestionProvider(
                candidates=[
                    make_candidate(
                        content=f"Actual controlled candidate {index}",
                        source_context_ids=[str(chunk.id)],
                    )
                    for index in range(count)
                ]
            )
        ),
        retriever=StubRetriever(
            [
                make_chunk(
                    str(chunk.id),
                    course_id=str(course.id),
                    document_id=str(chunk.document_id),
                )
            ]
        ),
        embedding_provider=StubEmbeddingProvider(),
        settings=build_test_settings(),
        semantic_provider=provider,
    )
    return service, provider


def generate(client, scenario, count=1):
    response = client.post(
        "/api/question-generation/candidates",
        headers=_headers(scenario["teacher"], UserRole.TEACHER),
        json={"course_id": str(scenario["course"].id), "count": count},
    )
    assert response.status_code == 201, response.text
    return response.json()["candidates"]


@pytest.mark.parametrize(
    "verdict,error,outcome,status",
    [
        ("pass", None, "passed", QuestionStatus.PENDING_REVIEW),
        ("fail", None, "failed", QuestionStatus.NEEDS_REVISION),
        (
            "pass",
            TimeoutError("provider fixture timeout"),
            "technical_error",
            QuestionStatus.PENDING_REVIEW,
        ),
    ],
)
def test_auto_generation_persists_actual_semantic_result_and_current_detail(
    scenario, client_factory, verdict, error, outcome, status
):
    service, provider = service_case(
        scenario, StubSemanticProvider(verdict=verdict, error=error)
    )
    client = client_factory(service)
    candidate = generate(client, scenario)[0]
    report = candidate["current_validation"]
    assert candidate["status"] == status.value
    assert report["outcome"] == outcome and report["is_current"]
    assert candidate["can_review"] is (outcome == "passed")
    assert len(provider.calls) == 1
    assert report["provenance"]["provider_name"] == "controlled-semantic-fixture"
    assert report["provenance"]["model"] == "fixture-four-checks"
    headers = _headers(scenario["teacher"], UserRole.TEACHER)
    for path in ("/api/question-generation/candidates/", "/api/questions/"):
        detail = client.get(path + candidate["candidate_id"], headers=headers)
        assert detail.status_code == 200, detail.text
        assert detail.json()["current_validation"]["id"] == report["id"]
        assert detail.json()["can_review"] is (outcome == "passed")
    session = scenario["session"]
    row = session.get(Question, UUID(candidate["candidate_id"]))
    persisted = session.get(QuestionValidationResult, UUID(report["id"]))
    assert row.status is status and row.frozen_at is None
    assert (
        persisted.outcome == outcome
        and persisted.input_revision == row.validation_revision
    )
    assert session.scalar(select(QuestionRevisionComment)) is None
    if outcome == "technical_error":
        assert (
            report["checks"] is None
            and report["issues"] is None
            and report["error"]["code"] == "ProviderTimeout"
        )


def test_batch_is_persisted_before_any_semantic_model_request(scenario, client_factory):
    session = scenario["session"]
    seen = []

    class ObservingProvider(StubSemanticProvider):
        async def generate_structured(self, messages, schema, model=None):
            seen.append(session.scalar(select(func.count()).select_from(Question)))
            return await super().generate_structured(messages, schema, model)

    service, provider = service_case(scenario, ObservingProvider(), count=2)
    candidates = generate(client_factory(service), scenario, count=2)
    assert seen == [2, 2] and len(provider.calls) == 2
    assert len({candidate["current_validation"]["id"] for candidate in candidates}) == 2
    assert all(
        candidate["current_validation"]["run_no"] == 1 for candidate in candidates
    )


@pytest.mark.parametrize(
    "route", ["approve", "patch_status", "post_status", "candidate_review"]
)
def test_each_approval_route_rejects_pending_without_current_semantic_report(
    scenario, client_factory, route
):
    service, _provider = service_case(scenario)
    session, teacher, course = (
        scenario["session"],
        scenario["teacher"],
        scenario["course"],
    )
    question = Question(
        course_id=course.id,
        created_by=teacher.id,
        type="SHORT_ANSWER",
        content="Unvalidated formal pending question",
        reference_answer="Actual answer",
        scoring_rubric="Actual scoring criterion",
        score=1,
        status=QuestionStatus.PENDING_REVIEW,
    )
    session.add(question)
    session.commit()
    identity = str(question.id)
    client, headers = client_factory(service), _headers(teacher, UserRole.TEACHER)
    if route == "approve":
        response = client.post(f"/api/questions/{identity}/approve", headers=headers)
    elif route == "patch_status":
        response = client.patch(
            f"/api/questions/{identity}/status",
            headers=headers,
            json={"status": "Approved"},
        )
    elif route == "post_status":
        response = client.post(
            f"/api/questions/{identity}/status",
            headers=headers,
            json={"status": "Approved"},
        )
    else:
        response = client.post(
            f"/api/question-generation/candidates/{identity}/review",
            headers=headers,
            json={"action": "approve"},
        )
    assert response.status_code in {409, 422}, response.text
    session.rollback()
    session.expire_all()
    assert session.get(Question, question.id).status is QuestionStatus.PENDING_REVIEW
    assert session.get(Question, question.id).frozen_at is None
    assert session.scalar(select(QuestionValidationResult)) is None


def test_candidate_approval_records_real_freeze_and_manual_revision_records_real_comment(
    scenario, client_factory
):
    service, _provider = service_case(scenario)
    client = client_factory(service)
    candidate = generate(client, scenario)[0]
    identity, headers = candidate["candidate_id"], _headers(
        scenario["teacher"], UserRole.TEACHER
    )
    approved = client.post(
        f"/api/question-generation/candidates/{identity}/review",
        headers=headers,
        json={"action": "approve"},
    )
    assert approved.status_code == 200, approved.text
    session = scenario["session"]
    session.expire_all()
    assert session.get(Question, UUID(identity)).frozen_at is not None

    pending = generate(client, scenario)[0]
    revision = client.post(
        f"/api/question-generation/candidates/{pending['candidate_id']}/review",
        headers=headers,
        json={
            "action": "request_revision",
            "comment": "Actual teacher identified the needed condition",
        },
    )
    assert revision.status_code == 200, revision.text
    session.expire_all()
    row = session.get(Question, UUID(pending["candidate_id"]))
    comment = session.scalar(
        select(QuestionRevisionComment).where(
            QuestionRevisionComment.question_id == row.id
        )
    )
    assert row.status is QuestionStatus.NEEDS_REVISION and row.frozen_at is None
    assert (
        comment.commented_by == scenario["teacher"].id
        and comment.comment == "Actual teacher identified the needed condition"
    )


def test_pending_list_status_does_not_mask_new_running_report_or_revision(
    scenario, client_factory
):
    service, _provider = service_case(scenario)
    client = client_factory(service)
    candidate = generate(client, scenario)[0]
    headers, identity = _headers(scenario["teacher"], UserRole.TEACHER), UUID(
        candidate["candidate_id"]
    )
    content = ContentValidationService(scenario["session"])
    first = content.get_validation(
        identity,
        UUID(candidate["current_validation"]["id"]),
        actor_id=scenario["teacher"].id,
    )
    running = content.start_validation(
        identity,
        actor_id=scenario["teacher"].id,
        input_refs=first.input_refs,
        executor_name="actual controlled new round",
    )
    listing = client.get(
        "/api/question-generation/candidates",
        headers=headers,
        params={"course_id": str(scenario["course"].id)},
    )
    assert listing.status_code == 200
    assert listing.json()["items"][0]["status"] == "Pending Review"
    detail = client.get(
        f"/api/question-generation/candidates/{identity}", headers=headers
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["current_validation"]["id"] == str(running.id)
    assert (
        detail.json()["current_validation"]["outcome"] == "running"
        and not detail.json()["can_review"]
    )
    changed = client.patch(
        f"/api/questions/{identity}",
        headers=headers,
        json={"content": "Actual teacher changed the input"},
    )
    assert changed.status_code == 200, changed.text
    fresh = client.get(f"/api/questions/{identity}", headers=headers).json()
    assert fresh["current_validation"]["stale"] and not fresh["can_review"]
    denied = client.post(
        f"/api/question-generation/candidates/{identity}/review",
        headers=headers,
        json={"action": "approve"},
    )
    assert denied.status_code in {409, 422}, denied.text


def test_text_adaptation_uses_its_own_report_and_never_inherits_parent_pass(
    scenario, client_factory
):
    service, provider = service_case(scenario)
    client = client_factory(service)
    parent = generate(client, scenario)[0]
    headers = _headers(scenario["teacher"], UserRole.TEACHER)
    response = client.post(
        "/api/question-generation/adaptations",
        headers=headers,
        json={
            "course_id": str(scenario["course"].id),
            "source_question_id": parent["candidate_id"],
            "adaptation_type": "rewrite",
        },
    )
    assert response.status_code == 201, response.text
    child = response.json()["candidates"][0]
    assert len(provider.calls) == 2
    assert child["current_validation"]["id"] != parent["current_validation"]["id"]
    assert (
        child["current_validation"]["question_id"] == child["candidate_id"]
        and child["can_review"]
    )
    assert (
        scenario["session"].get(Question, UUID(child["candidate_id"])).frozen_at is None
    )


def test_image_adaptation_keeps_original_pixels_but_requires_its_own_image_and_semantic_review(
    scenario, client_factory
):
    service, provider = service_case(scenario)
    client = client_factory(service)
    parent_dto = generate(client, scenario)[0]
    identity, teacher = UUID(parent_dto["candidate_id"]), scenario["teacher"]
    session = scenario["session"]
    stream = BytesIO()
    Image.new("RGB", (32, 24), "white").save(stream, format="PNG")
    asset = QuestionAssetService(session).upload_question(
        identity,
        content=stream.getvalue(),
        asset_type="diagram",
        caption="Actual controlled original pixels",
        actor_id=teacher.id,
    )
    content = ContentValidationService(session)
    view = content.get_image_assessment("question", identity, actor_id=teacher.id)
    content.manual_image_check(
        "question",
        identity,
        ImageManualCheckRequest(
            expected_context_revision=view.context_revision,
            expected_run_no=view.run_no,
            expected_check_no=view.check_no,
            status="confirmed",
            confirmed_conditions=[
                {
                    "asset_id": asset.id,
                    "text": "Actual teacher fixture condition",
                    "evidence_region": None,
                }
            ],
            image_findings=[
                {
                    "asset_id": asset.id,
                    "finding": "conditions_confirmed",
                    "reason": "Controlled fixture reviewed its original pixels",
                }
            ],
            explanation="Actual role fixture; not teacher quality measurement",
        ),
        actor_id=teacher.id,
    )
    parent_report = asyncio.run(
        content.run_validation(identity, actor_id=teacher.id, provider=provider)
    )
    assert parent_report.can_review
    calls_before = len(provider.calls)
    headers = _headers(teacher, UserRole.TEACHER)
    response = client.post(
        "/api/question-generation/adaptations",
        headers=headers,
        json={
            "course_id": str(scenario["course"].id),
            "source_question_id": str(identity),
            "adaptation_type": "rewrite",
        },
    )
    assert response.status_code == 201, response.text
    child_dto = response.json()["candidates"][0]
    assert len(provider.calls) == calls_before
    assert child_dto["current_validation"] is None and not child_dto["can_review"]
    child = session.get(Question, UUID(child_dto["candidate_id"]))
    assert (
        child.image_assessment["runs"] == []
        and child.image_assessment["manual_checks"] == []
    )
    assert child.assets[0].id != asset.id
    assert (
        child.assets[0].storage_path
        == session.get(Question, identity).assets[0].storage_path
    )
    assert (
        session.scalar(
            select(QuestionValidationResult).where(
                QuestionValidationResult.question_id == child.id
            )
        )
        is None
    )
    denied = client.post(f"/api/questions/{child.id}/approve", headers=headers)
    assert denied.status_code in {409, 422}, denied.text
