"""T166 explicit semantic command accepts identities, never caller-created facts; TCR section 20."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from backend.app.domain.enums import DocumentStatus, QuestionStatus
from backend.app.models import DocumentChunk, Question, QuestionValidationResult
from backend.app.services.question_service import QuestionService
from tests.contract.test_file_storage_api import files_api as _files_api
from tests.support.semantic_validation_doubles import StubSemanticProvider

files_api = _files_api


def setup_command(files_api, monkeypatch, *, provider=None):
    _client, session, _files, document, users, _headers = files_api
    question_dto = QuestionService(session).create_question(
        document.course_id,
        "SHORT_ANSWER",
        "Actual current candidate text",
        created_by=users[0].id,
        reference_answer="Actual answer",
        scoring_rubric="Actual criterion",
        score="1",
    )
    question = session.get(Question, UUID(question_dto.id))
    question.status = QuestionStatus.PENDING_REVIEW
    document.status = DocumentStatus.READY
    chunk = DocumentChunk(
        document_id=document.id,
        course_id=document.course_id,
        knowledge_base_id=document.knowledge_base_id,
        chunk_index=0,
        content="Actual ready teaching source",
        chunk_metadata={"location": "page 2"},
    )
    session.add(chunk)
    session.commit()
    provider = provider if provider is not None else StubSemanticProvider()
    monkeypatch.setattr(
        "backend.app.ai.llm.factory.create_llm_provider", lambda _settings: provider
    )
    return question, chunk, provider


def test_explicit_command_persists_real_inputs_and_provider_facts(
    files_api, monkeypatch
):
    client, session, _files, document, users, headers = files_api
    question, chunk, provider = setup_command(files_api, monkeypatch)
    response = client.post(
        f"/api/questions/{question.id}/validations",
        headers=headers[0],
        json={"teaching_chunk_ids": [str(chunk.id)]},
    )
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["outcome"] == "passed" and view["can_review"]
    assert view["requested_by"] == str(users[0].id)
    evidence = view["input_refs"]["evidence"][0]
    assert (
        evidence["source_id"] == str(chunk.id)
        and evidence["source_data"]["content_snapshot"] == chunk.content
    )
    assert evidence["source_data"]["source_file"] == document.original_filename
    assert provider.calls[0]["fields"]["content"] == question.content
    session.expire_all()
    assert session.get(QuestionValidationResult, UUID(view["id"])).outcome == "passed"


@pytest.mark.parametrize(
    "forged",
    [
        {"fields": {"content": "Invented"}},
        {"snapshots": []},
        {"provenance": {}},
        {"can_review": True},
        {"requested_by": str(uuid4())},
    ],
)
def test_command_cannot_spoof_current_content_or_machine_authority(
    files_api, monkeypatch, forged
):
    client, session, _files, _document, _users, headers = files_api
    question, chunk, provider = setup_command(files_api, monkeypatch)
    response = client.post(
        f"/api/questions/{question.id}/validations",
        headers=headers[0],
        json={"teaching_chunk_ids": [str(chunk.id)]} | forged,
    )
    assert response.status_code == 422, response.text
    assert (
        provider.calls == []
        and session.scalar(select(QuestionValidationResult)) is None
    )


def test_command_requires_authenticated_course_teacher(files_api, monkeypatch):
    client, session, _files, _document, _users, headers = files_api
    question, chunk, provider = setup_command(files_api, monkeypatch)
    route, payload = f"/api/questions/{question.id}/validations", {
        "teaching_chunk_ids": [str(chunk.id)]
    }
    assert client.post(route, json=payload).status_code == 401
    for denied in headers[1:]:
        assert client.post(route, json=payload, headers=denied).status_code == 403
    assert (
        provider.calls == []
        and session.scalar(select(QuestionValidationResult)) is None
    )


@pytest.mark.parametrize("status", [QuestionStatus.DRAFT, QuestionStatus.APPROVED])
def test_command_starts_only_submitted_current_candidate(
    files_api, monkeypatch, status
):
    client, session, _files, _document, _users, headers = files_api
    question, chunk, provider = setup_command(files_api, monkeypatch)
    question.status = status
    session.commit()
    response = client.post(
        f"/api/questions/{question.id}/validations",
        json={"teaching_chunk_ids": [str(chunk.id)]},
        headers=headers[0],
    )
    assert response.status_code == 409, response.text
    assert (
        provider.calls == []
        and session.scalar(select(QuestionValidationResult)) is None
    )


def test_image_command_requires_current_real_whole_group_review(files_api, monkeypatch):
    from tests.unit.services.test_question_asset_service import png

    client, _session, _files, _document, users, headers = files_api
    question, chunk, provider = setup_command(files_api, monkeypatch)
    base = f"/api/questions/{question.id}"
    uploaded = client.post(
        base + "/assets/upload",
        headers=headers[0],
        files={"file": ("actual.png", png(), "image/png")},
    )
    assert uploaded.status_code == 201, uploaded.text
    payload = {"teaching_chunk_ids": [str(chunk.id)]}
    pending = client.post(base + "/validations", json=payload, headers=headers[0])
    assert (
        pending.status_code == 409
        and pending.json()["detail"]["code"] == "VISION_REVIEW_REQUIRED"
    )
    assert provider.calls == []
    view = client.get(base + "/image-assessment", headers=headers[0]).json()
    checked = client.post(
        base + "/image-manual-checks",
        headers=headers[0],
        json={
            "expected_context_revision": view["context_revision"],
            "expected_run_no": view["run_no"],
            "expected_check_no": view["check_no"],
            "status": "confirmed",
            "image_findings": [
                {
                    "asset_id": uploaded.json()["id"],
                    "finding": "no_conditions_needed",
                    "reason": "Actual controlled blank-image role fixture",
                }
            ],
            "explanation": "Authentic role fixture, not model quality measurement",
        },
    )
    assert checked.status_code == 200, checked.text
    done = client.post(base + "/validations", json=payload, headers=headers[0])
    assert done.status_code == 200 and done.json()["can_review"], done.text
    image = next(
        item
        for item in done.json()["input_refs"]["evidence"]
        if item["kind"] == "question_asset"
    )
    assert (
        image["source_data"]["image_review_ref"]["check_id"]
        == checked.json()["current_check"]["id"]
    )
    assert checked.json()["current_check"]["teacher_id"] == str(users[0].id)


@pytest.mark.parametrize(
    "missing,code",
    [(False, "CONTENT_INPUT_INCOMPLETE"), (True, "CONTENT_SOURCE_INVALID")],
)
def test_public_command_preserves_basis_error_without_starting_a_report(
    files_api, monkeypatch, missing, code
):
    client, session, _files, _document, _users, headers = files_api
    question, _chunk, provider = setup_command(files_api, monkeypatch)
    response = client.post(
        f"/api/questions/{question.id}/validations",
        headers=headers[0],
        json={"teaching_chunk_ids": [str(uuid4())] if missing else []},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == code
    assert (
        provider.calls == []
        and session.scalar(select(QuestionValidationResult)) is None
    )
