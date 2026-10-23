"""Real HTTP/PG journey; extraction/admission are explicitly Fake HTTP.

Objective scoring uses production deterministic rules and sends no model request.
"""

import asyncio
import json
from uuid import uuid4

import httpx

from backend.app.services.learning_operations import LearningOperations
from tests.integration import test_learning_material_versions as materials
from tests.integration.test_personal_duplicate_identity import payload
from tests.integration.test_personal_paper_flow import answer_result
from tests.support.runtime_settings_fakes import settings_harness

space = materials.space


def test_upload_confirm_practice_submit_and_reload(space, tmp_path):
    sid = materials.subject(space, "Journey synthetic mathematics")
    doc = materials.upload(
        space, sid, "journey.txt", b"1. Choose A or B. Answer: A", purpose="exam_paper"
    ).json()
    source = {"document_id": doc["id"], "anchor": doc["blocks"][0]["anchor"]}
    candidate = payload(
        doc,
        reference_answer="A",
        answer_origin="paper",
        evidence={"reference_answer": {"source": source, "text": "Answer: A"}},
    )
    calls = []
    harness = settings_harness(tmp_path / "journey-runtime")
    asyncio.run(harness.activate_model("journey-fake", with_credential=True))

    async def handler(request):
        calls.append(json.loads(request.content))
        result = {"questions": [candidate]} if len(calls) == 1 else answer_result()
        return httpx.Response(
            200,
            json={
                "id": "fake-journey",
                "object": "chat.completion",
                "created": 0,
                "model": "journey-fake",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": json.dumps(result)},
                    }
                ],
            },
        )

    operations = LearningOperations(
        sessions=space.subjects.sessions,
        config=harness.service,
        transport=httpx.MockTransport(handler),
    )
    space.client.app.state.learning_operations = operations
    client = space.client

    def post(path, body):
        response = client.post("/api/learning" + path, json=body)
        assert response.status_code in (200, 201), response.text
        return response.json()

    def execute(preview, path):
        post(
            f"/operations/{preview['id']}/confirm",
            {"preview_digest": preview["preview_digest"]},
        )
        return post(path + "/" + preview["id"], {})

    try:
        paper = post(f"/subjects/{sid}/imports", {"document_id": doc["id"]})
        preview = post(
            f"/imports/{paper['id']}/extraction-preview",
            {"idempotency_key": uuid4().hex},
        )
        assert calls == []
        extracted = execute(preview, f"/imports/{paper['id']}/extract")
        draft = extracted["questions"][0]
        confirmed = post(
            f"/imports/{paper['id']}/confirm",
            {
                "question_ids": [draft["id"]],
                "expected_revisions": {draft["id"]: draft["revision"]},
            },
        )
        qid = confirmed["questions"][0]["question_id"]
        preview = post(
            f"/questions/{qid}/answer-preview",
            {"mode": "review", "expected_revision": 1, "idempotency_key": uuid4().hex},
        )
        question = execute(preview, f"/questions/{qid}/answer")
        assert question["admission_state"] == "eligible"
        assert question["source_kind"] == "uploaded"
        sends = len(calls)
        preview = post(
            f"/subjects/{sid}/practices/preview", {"count": 1, "source": "uploaded"}
        )
        practice = post(
            f"/practice-previews/{preview['id']}/start",
            {"preview_digest": preview["preview_digest"]},
        )
        assert "reference_answer" not in practice["items"][0]["content"]
        item = practice["items"][0]
        saved = client.put(
            f"/api/learning/practices/{practice['id']}/items/{item['id']}/answer",
            json={"choice_ids": ["A"]},
        )
        assert saved.status_code == 200, saved.text
        result = post(f"/practices/{practice['id']}/submit", {})
        assert result["items"][0]["score"] == "10.00"
        assert result["items"][0]["validation_state"] == "valid"
        assert (
            len(calls) == sends == 2
        )  # only Fake extraction and explicit Fake admission
        reloaded = client.get(f"/api/learning/practices/{practice['id']}/results")
        assert reloaded.status_code == 200 and reloaded.json() == result
        assert post(f"/practices/{practice['id']}/submit", {}) == result
        assert len(calls) == sends and operations.snapshots.active_lease_count == 0
    finally:
        asyncio.run(operations.aclose())
        harness.close()
