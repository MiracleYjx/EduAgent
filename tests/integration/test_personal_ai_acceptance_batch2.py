"""T083 batch-two offline acceptance: P1/V1/R1/T1 with MockTransport.

The tests use the real isolated Test PostgreSQL schema, the production
ProviderRegistry/RuntimeSnapshot/LearningOperations path and, for P1, the real
local RapidOCR adapter.  Only the supplier HTTP transport is replaced by
``httpx.MockTransport``.  A passing run therefore proves request orchestration,
budgets, evidence and no-retry boundaries; it does **not** prove real model
quality or replace the separately authorized real API acceptance.

P1 uses the frozen ``cross-page-paper.pdf``.  V1 uses the frozen
``linear-function.png`` and reuses the already indexed linear-function
material in the real plan; the offline test indexes a synthetic material with
the same frozen text so it can exercise retrieval without an Embedding API
call.  R1 uses the frozen 50 m² -> 100 m² reference-answer revision.  T1 uses
the frozen slope-error first question.  The A2 "斜率含义" T1 is not accepted
as a substitute.  Because the existing G2 chunks contain 斜率/截距 but not
错误, the strict T1 prompt needs a local T1-material index request before its
one teacher text request; the offline test stands in for that index with a
synthetic vector index and does not consume a real request.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from backend.app.domain.enums import QuestionSourceType
from backend.app.models import Document, DocumentChunk
from backend.app.models.practice import (
    GradeAttempt,
    PracticeAnswer,
    PracticeItem,
    PracticeSession,
)
from backend.app.models.question import Question
from backend.app.models.question_asset import QuestionAsset
from backend.app.models.question_origin import QuestionOrigin
from backend.app.models.question_revision import (
    QuestionRevision,
    question_revision_assets,
)
from backend.app.schemas.personal_teacher import TeacherReply
from backend.app.services.grading.subjective_grader import SubjectiveGradingPayload
from backend.app.services.learning_operations import LearningOperations, ModelBudget
from backend.app.services.settings.runtime_snapshot import RuntimeSnapshotService
from tests.integration import test_learning_material_versions as material_tests
from tests.integration.test_personal_ai_acceptance import _grade_rows
from tests.integration.test_personal_practice_flow import add_history, seed_bank
from tests.support import runtime_consumer_fakes as runtime_fakes
from tests.support.learning_content import registration
from tests.support.personal_ai_acceptance_batch2 import (
    P1_PDF,
    R1_NEW_ANSWER,
    R1_OLD_ANSWER,
    R1_QUESTION,
    R1_RUBRIC,
    R1_STUDENT_ANSWER,
    SAMPLES,
    SCENARIO_PLAN,
    T1_MESSAGE,
    TOTAL_REQUESTS,
    TOTAL_REQUESTS_WITH_T1_MATERIAL,
    T1_NEW_MATERIAL_INDEX_REQUESTS,
    V1_FORBIDDEN_TEXT,
    V1_IMAGE_BYTES,
    V1_IMAGE_SHA256,
    V1_IMAGE_SIZE,
    V1_PNG,
    V1_QUESTION,
    V1_REFERENCE_ANSWER,
    V1_RUBRIC,
    V1_STUDENT_ANSWER,
    assert_frozen_inputs,
    embedding_vector,
    linear_function_docx,
    sha256_file,
)
from tests.support.personal_paper_runtime import configured_paper_runtime
from tests.support.runtime_settings_fakes import settings_harness

space = material_tests.space


def _budget(
    input_tokens: int,
    output_tokens: int | None = None,
    *,
    dimension: int | None = None,
    cost: str = "0.02",
) -> ModelBudget:
    return ModelBudget(
        input_token_limit=input_tokens,
        output_token_limit=output_tokens,
        embedding_dimension=dimension,
        estimated_cost=cost,
    )


def _chat_response(body: dict, payload: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "mock-batch2",
            "object": "chat.completion",
            "created": 0,
            "model": body["model"],
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(payload, ensure_ascii=False),
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        },
    )


def _embedding_response(body: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "object": "list",
            "model": body["model"],
            "data": [
                {
                    "object": "embedding",
                    "index": 0,
                    "embedding": embedding_vector(),
                }
            ],
            "usage": {"prompt_tokens": 10, "total_tokens": 10},
        },
    )


def _prompt_text(body: dict) -> str:
    content = body["messages"][-1]["content"]
    if isinstance(content, str):
        return content
    return next(
        part["text"] for part in content if part.get("type") == "text"
    )


def _section(text: str, label: str) -> str:
    start = text.index(label) + len(label)
    end = text.find("【", start)
    return text[start : end if end >= 0 else None]


def _image_bytes(body: dict) -> bytes | None:
    for message in body.get("messages") or []:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if part.get("type") != "image_url":
                continue
            url = str((part.get("image_url") or {}).get("url") or "")
            assert url.startswith("data:image/png;base64,"), url[:32]
            return base64.b64decode(url.split(",", 1)[1])
    return None


def _grading_payload(
    score: int = 10,
    *,
    reason: str | None = None,
    correct_points: list[str] | None = None,
    missing_knowledge_points: list[str] | None = None,
) -> dict:
    result = {
        "score": score,
        "confidence": 0.95,
        "reason": reason or "按冻结题目、答案与资料依据完成评分。",
        "correct_points": (
            correct_points
            if correct_points is not None
            else ["读出(0,1)", "读出(2,5)", "斜率2", "截距1"]
        ),
        "missing_knowledge_points": (
            missing_knowledge_points
            if missing_knowledge_points is not None
            else []
        ),
        "suggestions": ["继续练习从图像读取点位。"],
    }
    return SubjectiveGradingPayload.model_validate(result).model_dump(mode="json")


def _paper_payload(document_id: str, blocks: list[dict], assets: list[dict]) -> dict:
    page1 = next(item for item in blocks if item.get("anchor") == "第 1 页:ocr")
    page2 = next(item for item in blocks if item.get("anchor") == "第 2 页:ocr")
    image = next(
        item
        for item in blocks
        if item.get("type") == "image" and item.get("page") == 1
    )
    asset = next(item for item in assets if item.get("sha256") == image.get("sha256"))
    assert page1.get("page") == 1 and page2.get("page") == 2
    assert image.get("anchor") == "第 1 页"
    return {
        "questions": [
            {
                "question_type": "single_choice",
                "stem_blocks": [
                    {
                        "type": "text",
                        "text": (
                            "7. A rectangular school garden is 20 m long. Its width is "
                            "stated on page 2. Find the area in square metres (m²)."
                        ),
                    },
                    {"type": "image", "asset_id": asset["id"]},
                ],
                "options": [
                    {"id": "A", "blocks": [{"type": "text", "text": "100 m²"}]},
                    {"id": "B", "blocks": [{"type": "text", "text": "50 m²"}]},
                    {"id": "C", "blocks": [{"type": "text", "text": "25 m²"}]},
                    {"id": "D", "blocks": [{"type": "text", "text": "10 m²"}]},
                ],
                "explanation_blocks": [],
                "source_locations": [
                    {
                        "document_id": document_id,
                        "page": 1,
                        "anchor": "第 1 页:ocr",
                    },
                    {
                        "document_id": document_id,
                        "page": 1,
                        "anchor": "第 1 页",
                    },
                    {
                        "document_id": document_id,
                        "page": 2,
                        "anchor": "第 2 页:ocr",
                    },
                ],
                "question_number": "7",
                "reference_answer": "A",
                "scoring_rubric": None,
                "answer_origin": "paper",
                "evidence": {
                    "reference_answer": {
                        "source": {
                            "document_id": document_id,
                            "page": 2,
                            "anchor": "第 2 页:ocr",
                        },
                        "text": "Question 7: A",
                    }
                },
                "knowledge_point_ids": [],
                "uncertainties": [],
            }
        ]
    }


def _seed_r1(space) -> SimpleNamespace:
    sid = UUID(material_tests.subject(space, "R1 frozen 50 to 100"))
    question_id, revision_id = uuid4(), uuid4()
    practice_id, item_id, answer_id, attempt_id = uuid4(), uuid4(), uuid4(), uuid4()
    stem = [{"type": "text", "text": R1_QUESTION}]
    with space.subjects.sessions.begin() as session:
        question = Question(
            id=question_id,
            subject_id=sid,
            source_type=QuestionSourceType.PAPER_IMPORTED,
            course_id=None,
            created_by=None,
        )
        session.add(question)
        session.flush()
        revision = QuestionRevision(
            id=revision_id,
            question_id=question_id,
            subject_id=sid,
            revision=1,
            question_type="short_answer",
            stem_blocks=stem,
            options=[],
            reference_answer=R1_OLD_ANSWER,
            scoring_rubric=R1_RUBRIC,
            explanation_blocks=[],
            grounding="general_knowledge",
            grounding_refs={"answer_origin": "paper"},
            admission_state="eligible",
            default_score=10,
        )
        session.add(revision)
        session.flush()
        question.current_revision_id = revision_id
        practice = PracticeSession(
            id=practice_id, subject_id=sid, state="preview", conditions={}
        )
        session.add(practice)
        session.flush()
        scoring_input = {
            "question_type": "short_answer",
            "stem_blocks": stem,
            "options": [],
            "explanation_blocks": [],
            "reference_answer": R1_OLD_ANSWER,
            "scoring_rubric": R1_RUBRIC,
            "grounding": "general_knowledge",
            "answer_origin": "paper",
        }
        item = PracticeItem(
            id=item_id,
            session_id=practice_id,
            subject_id=sid,
            position=1,
            question_id=question_id,
            question_revision_id=revision_id,
            fixed_max_score=10,
            knowledge_point_ids=[],
            scoring_input=scoring_input,
        )
        session.add(item)
        session.flush()
        session.add(
            PracticeAnswer(
                id=answer_id,
                practice_item_id=item_id,
                answer_state="answered",
                submission_state="submitted",
                text_answer=R1_STUDENT_ANSWER,
            )
        )
        session.flush()
        session.add(
            GradeAttempt(
                id=attempt_id,
                answer_id=answer_id,
                round=1,
                validation_state="valid",
                execution_source="model",
                score=8,
                max_score=10,
                reason="原参考答案50平方米，按面积单位缺失扣分。",
                confidence=0.9,
                runtime_snapshot_id=uuid4(),
                scoring_input={
                    **scoring_input,
                    "question_revision_id": str(revision_id),
                },
                feedback={
                    "correct_points": ["20×5=100"],
                    "missing_knowledge_points": ["平方米"],
                    "suggestions": ["补单位"],
                    "retrieved_context_ids": [],
                },
            )
        )
        practice.state = "in_progress"
    return SimpleNamespace(
        subject_id=sid,
        question_id=question_id,
        revision_id=revision_id,
        practice_id=practice_id,
        item_id=item_id,
        answer_id=answer_id,
        attempt_id=attempt_id,
    )


def _revise_r1(space, state: SimpleNamespace) -> UUID:
    new_revision_id = uuid4()
    with space.subjects.sessions.begin() as session:
        old = session.get(QuestionRevision, state.revision_id)
        revision = QuestionRevision(
            id=new_revision_id,
            question_id=state.question_id,
            subject_id=state.subject_id,
            revision=2,
            question_type="short_answer",
            stem_blocks=old.stem_blocks,
            options=old.options,
            reference_answer=R1_NEW_ANSWER,
            scoring_rubric=R1_RUBRIC,
            explanation_blocks=[],
            grounding="general_knowledge",
            grounding_refs={"answer_origin": "paper"},
            admission_state="eligible",
            default_score=10,
        )
        session.add(revision)
        session.flush()
        session.get(Question, state.question_id).current_revision_id = new_revision_id
    return new_revision_id


def _prepare_v1(space, tmp_path, transport, calls: list[dict]) -> SimpleNamespace:
    sid = UUID(material_tests.subject(space, "V1 frozen image"))
    harness = settings_harness(
        tmp_path / "v1-runtime", environment_id=space.profile.environment_id
    )
    operations: LearningOperations | None = None
    try:
        asyncio.run(harness.activate_model("test-v1-text", with_credential=True))
        base = harness.service.active_revision()
        default_profile = next(iter(base.values.providers.values()))

        def mutate(values: dict) -> None:
            runtime_fakes.add_embedding_and_rerank(values)
            vision_id, _vision_revision = runtime_fakes.add_vision(
                values, model="deepseek-flash"
            )
            values["providers"][str(vision_id)]["base_url"] = (
                "https://api.deepseek.com/v1"
            )
            values["providers"][str(vision_id)]["credential_ref"] = (
                harness.provider_reference.model_dump(mode="json")
            )
            runtime_fakes.bind_task(
                values,
                "subjective_grading",
                default_profile.profile_id,
                default_profile.profile_revision,
                retrieval_mode="semantic",
            )

        config = runtime_fakes.SyntheticConfig(
            harness, runtime_fakes.synthetic_revision(harness, mutate)
        )
        embedding_profile = next(
            profile
            for profile in config.active_revision().values.providers.values()
            if "embedding" in profile.declared_capabilities
        )
        embedding = harness.service.provider_registry.create(
            embedding_profile, credential_store=harness.service.credential_store
        )
        identity = embedding.index_identity(dimension=1_024)
        asyncio.run(embedding.aclose())

        uploaded = material_tests.upload(
            space, str(sid), "linear-function.docx", linear_function_docx()
        )
        assert uploaded.status_code == 201, uploaded.text
        document_id = UUID(uploaded.json()["id"])
        material = space.materials.get(document_id)
        assert material.chunk_count >= 1
        material_tests.index(
            space,
            str(document_id),
            identity=identity,
            vectors=[embedding_vector()] * material.chunk_count,
        )
        with space.subjects.sessions() as session:
            facts = session.get(Document, document_id).parse_facts
            chunk_ids = {
                str(value)
                for value in session.scalars(
                    select(DocumentChunk.id).where(
                        DocumentChunk.document_id == document_id
                    )
                )
            }
        image_fact = next(
            item
            for item in facts["assets"]
            if item.get("sha256") == V1_IMAGE_SHA256
        )
        assert image_fact["stored"] is True

        question_id, revision_id, asset_id = uuid4(), uuid4(), uuid4()
        practice_id, item_id = uuid4(), uuid4()
        stem = [
            {"type": "text", "text": V1_QUESTION},
            {"type": "image", "asset_id": str(asset_id)},
        ]
        with space.subjects.sessions.begin() as session:
            question = Question(
                id=question_id,
                subject_id=sid,
                source_type=QuestionSourceType.PAPER_IMPORTED,
                course_id=None,
                created_by=None,
            )
            session.add(question)
            session.flush()
            asset = QuestionAsset(
                id=asset_id,
                question_id=question_id,
                asset_type="figure",
                width=V1_IMAGE_SIZE[0],
                height=V1_IMAGE_SIZE[1],
                file_path=image_fact["locator"],
                file_metadata=registration(V1_PNG.read_bytes()),
                order_index=1,
                source_document_id=document_id,
                source_anchor=image_fact["anchor"],
                student_visible=True,
            )
            session.add(asset)
            session.flush()
            reference_answer = V1_REFERENCE_ANSWER
            rubric = V1_RUBRIC
            revision = QuestionRevision(
                id=revision_id,
                question_id=question_id,
                subject_id=sid,
                revision=1,
                question_type="short_answer",
                stem_blocks=stem,
                options=[],
                reference_answer=reference_answer,
                scoring_rubric=rubric,
                explanation_blocks=[],
                grounding="uploaded_material",
                grounding_refs={"answer_origin": "paper"},
                admission_state="eligible",
                default_score=10,
            )
            session.add(revision)
            session.flush()
            question.current_revision_id = revision_id
            session.execute(
                question_revision_assets.insert().values(
                    revision_id=revision_id, asset_id=asset_id
                )
            )
            session.add(
                QuestionOrigin(
                    question_id=question_id,
                    subject_id=sid,
                    source_kind="uploaded",
                    document_id=document_id,
                    source_location={"anchor": image_fact["anchor"]},
                )
            )
            practice = PracticeSession(
                id=practice_id, subject_id=sid, state="preview", conditions={}
            )
            session.add(practice)
            session.flush()
            scoring_input = {
                "question_type": "short_answer",
                "stem_blocks": stem,
                "options": [],
                "explanation_blocks": [],
                "reference_answer": reference_answer,
                "scoring_rubric": rubric,
                "grounding": "uploaded_material",
                "scope_frozen": True,
                "scope_document_ids": [str(document_id)],
                "scope_material_version_ids": [],
                "scope_knowledge_point_names": ["一次函数"],
                "answer_origin": "paper",
            }
            session.add(
                PracticeItem(
                    id=item_id,
                    session_id=practice_id,
                    subject_id=sid,
                    position=1,
                    question_id=question_id,
                    question_revision_id=revision_id,
                    fixed_max_score=10,
                    knowledge_point_ids=[],
                    scoring_input=scoring_input,
                )
            )
            session.flush()
            session.add(
                PracticeAnswer(
                    id=uuid4(),
                    practice_item_id=item_id,
                    answer_state="answered",
                    submission_state="submitted",
                    text_answer=(
                        V1_STUDENT_ANSWER
                    ),
                )
            )
            practice.state = "in_progress"

        operations = LearningOperations(
            sessions=space.subjects.sessions,
            config=config,
            transport=transport,
            task_model_budgets={
                "vision": _budget(6_000, 1_500),
                "embedding": _budget(2_000, dimension=1_024, cost="0"),
            },
        )
        space.subjects.config = config
        snapshots = RuntimeSnapshotService(config)
        space.subjects.runtime_snapshots = snapshots
        space.subjects.grading_operations_required = True
        space.client.app.state.learning_operations = operations
        space.client.app.state.subject_service = space.subjects
        return SimpleNamespace(
            sid=sid,
            harness=harness,
            operations=operations,
            practice_id=practice_id,
            item_id=item_id,
            question_id=question_id,
            document_id=document_id,
            chunk_ids=chunk_ids,
            snapshots=snapshots,
            calls=calls,
        )
    except BaseException:
        if operations is not None:
            asyncio.run(operations.aclose())
        harness.close()
        raise


def _close_v1(value: SimpleNamespace) -> None:
    asyncio.run(value.operations.aclose())
    asyncio.run(value.snapshots.aclose())
    value.harness.close()


def _teacher_turn(client, operations, conversation_id: UUID, sid: UUID, body: dict):
    preview = client.post(
        f"/api/learning/conversations/{conversation_id}/messages/preview?subject_id={sid}",
        json=body,
    )
    assert preview.status_code == 200, preview.text
    turn = preview.json()
    operation = turn["operation"]
    assert operations.view(UUID(operation["id"])).actual_requests == 0
    confirmed = client.post(
        f"/api/learning/conversations/{conversation_id}/messages/{operation['id']}/confirm?subject_id={sid}",
        json={"preview_digest": operation["preview_digest"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    done = client.post(
        f"/api/learning/conversations/{conversation_id}/messages/{operation['id']}/execute?subject_id={sid}",
        json={},
    )
    assert done.status_code == 200, done.text
    return turn, done.json()


# ---------------------------------------------------------------------------
# Frozen inputs and plan
# ---------------------------------------------------------------------------


def test_batch2_frozen_inputs_and_request_plan_match_the_oracle():
    facts = assert_frozen_inputs()
    assert facts["p1"]["sha256"] == sha256_file(P1_PDF)
    assert facts["v1"]["sha256"] == V1_IMAGE_SHA256
    assert facts["v1"]["area_distractor_required"] is False
    assert SCENARIO_PLAN["V1"]["tasks"] == {"vision": 1, "embedding": 1}
    assert TOTAL_REQUESTS == 5
    assert T1_NEW_MATERIAL_INDEX_REQUESTS == 1
    assert TOTAL_REQUESTS_WITH_T1_MATERIAL == 6
    assert SAMPLES["vision_oracle"]["input_boundary"]["retrieved_material_ids"] == [
        "linear-function"
    ]


# ---------------------------------------------------------------------------
# P1: frozen two-page paper, real local OCR, one Mock extraction request
# ---------------------------------------------------------------------------


def test_p1_cross_page_paper_uses_real_local_ocr_and_one_mock_extraction(
    space, tmp_path
):
    assert sha256_file(P1_PDF) == "d647897413daf3e142b5be479e42eb9c62e57a149b5def3553bff075e13acdb1"
    model_dir = os.environ.get("EDUAGENT_TEST_OCR_MODEL_DIR")
    if not model_dir:
        pytest.skip("EDUAGENT_TEST_OCR_MODEL_DIR is not configured")
    assert Path(model_dir).is_dir(), model_dir

    sid = UUID(material_tests.subject(space, "P1 frozen cross page"))
    uploaded = material_tests.upload(
        space,
        str(sid),
        "cross-page-paper.pdf",
        P1_PDF.read_bytes(),
        purpose="exam_paper",
    )
    assert uploaded.status_code == 201, uploaded.text
    document_id = UUID(uploaded.json()["id"])
    imported = space.client.post(
        f"/api/learning/subjects/{sid}/imports",
        json={"document_id": str(document_id)},
    )
    assert imported.status_code == 201, imported.text
    paper = imported.json()
    assert [asset["source"]["page"] for asset in paper["assets"]] == [1, 2]

    harness, operations, _unused = asyncio.run(
        configured_paper_runtime(
            tmp_path / "p1-runtime",
            space.profile.environment_id,
            Path(model_dir),
            space.subjects.sessions,
        )
    )
    operations.task_model_budgets = {
        "paper_extraction": _budget(8_000, 2_000)
    }
    space.client.app.state.learning_operations = operations
    space.client.app.state.subject_service = space.subjects
    calls: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        payload = json.loads(body["messages"][-1]["content"])
        assert payload["document_id"] == str(document_id)
        blocks = payload["blocks"]
        assert any(
            block.get("anchor") == "第 1 页:ocr" and block.get("page") == 1
            for block in blocks
        )
        assert any(
            block.get("anchor") == "第 2 页:ocr" and block.get("page") == 2
            for block in blocks
        )
        result = _paper_payload(payload["document_id"], blocks, payload["assets"])
        return _chat_response(body, result)

    operations.transport = httpx.MockTransport(handler)
    try:
        ocr = space.client.post(
            f"/api/learning/imports/{paper['id']}/local-ocr", json={}
        )
        assert ocr.status_code == 200, ocr.text
        ocr_view = ocr.json()
        assert [asset["source"]["page"] for asset in ocr_view["assets"]] == [1, 2]
        assert all(
            asset["sha256"] and asset["media_type"] == "image/png"
            for asset in ocr_view["assets"]
        )

        preview = space.client.post(
            f"/api/learning/imports/{paper['id']}/extraction-preview",
            json={"idempotency_key": uuid4().hex},
        )
        assert preview.status_code == 200, preview.text
        operation = preview.json()
        assert operation["request_budget"] == 1
        assert operation["steps"][0]["task_kind"] == "paper_extraction"
        paper_budget = operation["steps"][0]["task_model_budgets"]["paper_extraction"]
        assert paper_budget["input_token_limit"] == 8_000
        assert paper_budget["output_token_limit"] == 2_000
        assert calls == []
        blocked = space.client.post(
            f"/api/learning/imports/{paper['id']}/extract/{operation['id']}",
            json={},
        )
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "CONFIRMATION_REQUIRED"
        assert calls == []
        confirmed = space.client.post(
            f"/api/learning/operations/{operation['id']}/confirm",
            json={"preview_digest": operation["preview_digest"]},
        )
        assert confirmed.status_code == 200, confirmed.text
        extracted = space.client.post(
            f"/api/learning/imports/{paper['id']}/extract/{operation['id']}",
            json={},
        )
        assert extracted.status_code == 200, extracted.text
        view = operations.view(UUID(operation["id"]))
        assert view.state == "succeeded" and view.actual_requests == 1
        assert len(calls) == 1
        # A single-task StepPlan keeps the legacy empty task_budgets map; the
        # effective per-task count is normalized by the operation view.
        assert view.steps[0]["task_budgets"] == {}
        assert view.steps[0]["task_actual_requests"] == {"paper_extraction": 1}
        assert view.steps[0]["requests"][0]["state"] == "response_received"

        result = extracted.json()
        assert result["status"] == "Pending Review"
        question = result["questions"][0]
        assert question["question_number"] == "7"
        assert question["question_type"] == "single_choice"
        assert {option["id"] for option in question["options"]} == {
            "A",
            "B",
            "C",
            "D",
        }
        option_text = {
            option["id"]: option["blocks"][0]["text"]
            for option in question["options"]
        }
        assert option_text == {
            "A": "100 m²",
            "B": "50 m²",
            "C": "25 m²",
            "D": "10 m²",
        }
        assert question["reference_answer"] == "A"
        assert question["answer_origin"] == "paper"
        assert question["evidence"]["reference_answer"]["source"]["page"] == 2
        pages = {source["page"] for source in question["source_locations"]}
        assert pages == {1, 2}
        image_block = next(
            block for block in question["stem_blocks"] if block["type"] == "image"
        )
        page_one_asset = next(
            asset for asset in result["assets"] if asset["source"]["page"] == 1
        )
        assert image_block["asset_id"] == page_one_asset["id"]
        assert "OCR_REQUIRES_REVIEW" in question["uncertainties"]
    finally:
        asyncio.run(operations.aclose())
        harness.close()


# ---------------------------------------------------------------------------
# V1: frozen image, one semantic query, one vision request
# ---------------------------------------------------------------------------


def test_v1_frozen_image_sends_exact_bytes_and_one_semantic_query(space, tmp_path):
    calls: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if "input" in body:
            assert body["model"] == "t213-embedding-a"
            return _embedding_response(body)
        blob = _image_bytes(body)
        assert blob == V1_PNG.read_bytes()
        assert len(blob) == V1_IMAGE_BYTES
        prompt = _prompt_text(body)
        assert V1_QUESTION in prompt
        # Method-style fixture (isolation option A): the reference answer states
        # only the method and the student answer is deliberately wrong, so the
        # whole sent prompt must stay free of the graph points, the numeric
        # slope/intercept and the function answer.  The frozen ``expected``
        # value is evaluation-only and never sent.
        for forbidden in V1_FORBIDDEN_TEXT:
            assert forbidden not in prompt, (forbidden, prompt)
        assert V1_REFERENCE_ANSWER in _section(prompt, "【标准答案】")
        assert V1_STUDENT_ANSWER in _section(prompt, "【学生答案】")
        assert body["max_tokens"] == 1_500
        return _chat_response(
            body,
            _grading_payload(
                0,
                reason=(
                    "学生答案与图像不符：由图读出的两点与斜率、截距均不同，"
                    "未得出正确解析式。"
                ),
                correct_points=[],
                missing_knowledge_points=["正确读点", "正确解析式"],
            ),
        )

    value = _prepare_v1(space, tmp_path, httpx.MockTransport(handler), calls)
    try:
        preview = space.client.post(
            f"/api/learning/practices/{value.practice_id}/grading-preview",
            json={"idempotency_key": uuid4().hex, "blank_confirmed": True},
        )
        assert preview.status_code == 200, preview.text
        operation = preview.json()
        assert operation["request_budget"] == 2
        step = operation["steps"][0]
        assert step["task_kind"] == "vision"
        assert step["task_budgets"] == {
            "vision": 1,
            "subjective_grading": 0,
            "embedding": 1,
        }
        assert step["task_model_budgets"]["vision"]["input_token_limit"] == 6_000
        assert step["task_model_budgets"]["embedding"]["embedding_dimension"] == 1_024
        assert calls == []
        blocked = space.client.post(
            f"/api/learning/practices/{value.practice_id}/grade/{operation['id']}",
            json={},
        )
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "CONFIRMATION_REQUIRED"
        assert calls == []
        confirmed = space.client.post(
            f"/api/learning/operations/{operation['id']}/confirm",
            json={"preview_digest": operation["preview_digest"]},
        )
        assert confirmed.status_code == 200, confirmed.text
        graded = space.client.post(
            f"/api/learning/practices/{value.practice_id}/grade/{operation['id']}",
            json={},
        )
        assert graded.status_code == 200, graded.text
        report = graded.json()
        item = report["items"][0]
        assert item["position"] == 1
        assert item["question_type"] == "short_answer"
        assert item["answer_state"] == "answered"
        assert Decimal(str(item["score"])) == Decimal(0)
        assert item["validation_state"] == "valid"
        assert item["execution_source"] == "model"
        assert item["feedback"]["retrieved_context_ids"]
        assert set(item["feedback"]["retrieved_context_ids"]) <= value.chunk_ids
        assert len(item["history"]) == 1
        assert item["history"][0]["operation_id"] == operation["id"]
        assert len(calls) == 2
        assert [("input" in body) for body in calls] == [True, False]
        view = value.operations.view(UUID(operation["id"]))
        assert view.state == "succeeded" and view.actual_requests == 2
        assert view.steps[0]["task_actual_requests"] == {
            "vision": 1,
            "embedding": 1,
        }
        assert [request["task_kind"] for request in view.steps[0]["requests"]] == [
            "embedding",
            "vision",
        ]
    finally:
        _close_v1(value)


def test_v1_vision_timeout_is_not_retried(space, tmp_path):
    calls: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if "input" in body:
            return _embedding_response(body)
        raise httpx.ReadTimeout("synthetic vision timeout", request=request)

    value = _prepare_v1(space, tmp_path, httpx.MockTransport(handler), calls)
    try:
        preview = space.client.post(
            f"/api/learning/practices/{value.practice_id}/grading-preview",
            json={"idempotency_key": uuid4().hex, "blank_confirmed": True},
        )
        operation = preview.json()
        confirmed = space.client.post(
            f"/api/learning/operations/{operation['id']}/confirm",
            json={"preview_digest": operation["preview_digest"]},
        )
        assert confirmed.status_code == 200
        graded = space.client.post(
            f"/api/learning/practices/{value.practice_id}/grade/{operation['id']}",
            json={},
        )
        assert graded.status_code == 200, graded.text
        item = graded.json()["items"][0]
        assert item["validation_state"] == "outcome_unknown"
        assert item["score"] is None
        assert len(calls) == 2
        view = value.operations.view(UUID(operation["id"]))
        assert view.state == "outcome_unknown"
        assert view.actual_requests == 2
        again = space.client.post(
            f"/api/learning/practices/{value.practice_id}/grade/{operation['id']}",
            json={},
        )
        assert again.status_code == 200
        assert again.json() == graded.json()
        assert len(calls) == 2
        assert value.operations.view(UUID(operation["id"])).actual_requests == 2
    finally:
        _close_v1(value)


# ---------------------------------------------------------------------------
# R1: frozen 50 m² -> 100 m² revision regrade
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("failed", [False, True], ids=["success", "failed"])
def test_r1_frozen_50_to_100_revision_regrade(space, tmp_path, failed):
    state = _seed_r1(space)
    dispute = space.client.post(
        f"/api/learning/practices/{state.practice_id}/disputes",
        json={
            "item_id": str(state.item_id),
            "reason": "参考答案写错了，应为20×5=100平方米。",
        },
    )
    assert dispute.status_code == 200, dispute.text
    dispute_id = dispute.json()["id"]
    new_revision_id = _revise_r1(space, state)

    harness = settings_harness(
        tmp_path / "r1-runtime", environment_id=space.profile.environment_id
    )
    asyncio.run(harness.activate_model("test-r1-text", with_credential=True))
    calls: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        prompt = body["messages"][-1]["content"]
        assert R1_NEW_ANSWER in prompt
        assert R1_OLD_ANSWER not in prompt
        assert body["max_tokens"] == 1_200
        if failed:
            return httpx.Response(503, json={"error": {"message": "synthetic"}})
        return _chat_response(
            body,
            _grading_payload(
                10, reason="按修订后的参考答案，20×5=100平方米完整正确。"
            ),
        )

    operations = LearningOperations(
        sessions=space.subjects.sessions,
        config=harness.service,
        transport=httpx.MockTransport(handler),
        task_model_budgets={"subjective_grading": _budget(4_000, 1_200)},
    )
    space.subjects.config = harness.service
    space.subjects.runtime_snapshots = RuntimeSnapshotService(harness.service)
    space.subjects.grading_operations_required = True
    space.client.app.state.learning_operations = operations
    space.client.app.state.subject_service = space.subjects
    try:
        preview = space.client.post(
            f"/api/learning/disputes/{dispute_id}/regrade-preview",
            json={
                "idempotency_key": uuid4().hex,
                "expected_attempt_id": str(state.attempt_id),
                "revision_id": str(new_revision_id),
            },
        )
        assert preview.status_code == 200, preview.text
        operation = preview.json()
        assert operation["request_budget"] == 1
        assert operation["steps"][0]["task_kind"] == "subjective_grading"
        grading_budget = operation["steps"][0]["task_model_budgets"][
            "subjective_grading"
        ]
        assert grading_budget["input_token_limit"] == 4_000
        assert grading_budget["output_token_limit"] == 1_200
        assert calls == []
        confirmed = space.client.post(
            f"/api/learning/operations/{operation['id']}/confirm",
            json={"preview_digest": operation["preview_digest"]},
        )
        assert confirmed.status_code == 200, confirmed.text
        assert calls == []
        regraded = space.client.post(
            f"/api/learning/disputes/{dispute_id}/regrade/{operation['id']}",
            json={},
        )
        assert regraded.status_code == 200, regraded.text
        assert len(calls) == 1
        view = operations.view(UUID(operation["id"]))
        assert view.actual_requests == 1
        assert view.state == ("failed" if failed else "succeeded")
        assert view.steps[0]["task_budgets"] == {"subjective_grading": 1}
        assert view.steps[0]["task_actual_requests"] == {"subjective_grading": 1}

        report = space.client.get(
            f"/api/learning/practices/{state.practice_id}/results"
        )
        assert report.status_code == 200, report.text
        item = report.json()["items"][0]
        assert item["position"] == 1
        assert item["disputed"] is True
        assert report.json()["group_score_rate"] is None
        assert [attempt["reference_answer"] for attempt in item["history"]] == [
            R1_OLD_ANSWER,
            R1_NEW_ANSWER,
        ]
        if failed:
            assert item["validation_state"] == "failed"
            assert item["score"] is None
            assert item["history"][-1]["score"] is None
            rejected = space.client.post(
                f"/api/learning/disputes/{dispute_id}/accept",
                json={"attempt_id": item["latest_attempt_id"]},
            )
            assert rejected.status_code == 409
        else:
            assert Decimal(str(item["score"])) == Decimal(10)
            assert item["validation_state"] == "valid"
            assert [attempt["score"] for attempt in item["history"]] == [
                "8.00",
                "10.00",
            ]
            accepted = space.client.post(
                f"/api/learning/disputes/{dispute_id}/accept",
                json={"attempt_id": item["latest_attempt_id"]},
            )
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["state"] == "resolved"
            after = space.client.get(
                f"/api/learning/practices/{state.practice_id}/results"
            ).json()
            accepted_item = after["items"][0]
            assert accepted_item["position"] == 1
            assert accepted_item["disputed"] is False
            assert Decimal(str(after["group_score_rate"])) == Decimal(1)
            assert [attempt["reference_answer"] for attempt in accepted_item["history"]] == [
                R1_OLD_ANSWER,
                R1_NEW_ANSWER,
            ]
        replay = space.client.post(
            f"/api/learning/disputes/{dispute_id}/regrade/{operation['id']}",
            json={},
        )
        assert replay.status_code == 200
        assert len(calls) == 1
        assert operations.view(UUID(operation["id"])).actual_requests == 1
    finally:
        asyncio.run(operations.aclose())
        asyncio.run(space.subjects.runtime_snapshots.aclose())
        harness.close()


# ---------------------------------------------------------------------------
# T1: frozen slope-error first question with an openable citation
# ---------------------------------------------------------------------------


def test_t1_frozen_slope_error_teacher_citation_is_openable(space, tmp_path):
    sid, _point, ids, _document = seed_bank(
        space, name="T1 frozen slope error", history=False, count=1
    )
    add_history(space, sid, ids[0], answered=True, score=10)
    # Keep the frozen linear-function text, but add the spaced keywords used by
    # the frozen slope-error question so the real keyword retriever can find it.
    material = material_tests.upload(
        space,
        str(sid),
        "linear-function-slope.md",
        (
            "一次函数 斜率 错误 1/2 y=2x+1 资料\n\n"
            + SAMPLES["materials"][0]["text"]
        ).encode("utf-8"),
    )
    assert material.status_code == 201, material.text
    material_id = material.json()["id"]
    # The strict "斜率错误" prompt needs a chunk containing "错误".  Existing
    # G2 chunks contain 斜率/截距 but not 错误, so real T1 needs one local
    # T1-material index request before the single teacher text request.  This
    # offline index is synthetic and is not a supplier request.
    material_tests.index(space, material_id)

    before = _grade_rows(space)
    created = space.client.post(
        f"/api/learning/subjects/{sid}/conversations",
        json={"title": "T1 frozen slope error"},
    )
    assert created.status_code == 201, created.text
    conversation_id = UUID(created.json()["id"])

    harness = settings_harness(
        tmp_path / "t1-runtime", environment_id=space.profile.environment_id
    )
    asyncio.run(harness.activate_model("test-t1-text", with_credential=True))
    calls: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        payload = json.loads(body["messages"][-1]["content"])
        assert payload["question"] == T1_MESSAGE
        sources = payload["context"]["sources"]
        assert sources, payload
        assert any(source["id"] for source in sources)
        result = TeacherReply.model_validate(
            {
                "answer_body": (
                    "斜率应为纵坐标变化量除以横坐标变化量；由(0,1)与(2,5)得"
                    "(5-1)/(2-0)=2，不是1/2。"
                ),
                "basis": "uploaded_material",
                "citations": [sources[0]["id"]],
                "uncertainties": [],
                "practice_knowledge_point_ids": [],
            }
        ).model_dump(mode="json")
        return _chat_response(body, result)

    operations = LearningOperations(
        sessions=space.subjects.sessions,
        config=harness.service,
        transport=httpx.MockTransport(handler),
        task_model_budgets={"default_llm": _budget(6_000, 1_500)},
    )
    space.subjects.config = harness.service
    space.subjects.runtime_snapshots = RuntimeSnapshotService(harness.service)
    space.client.app.state.learning_operations = operations
    space.client.app.state.subject_service = space.subjects
    try:
        turn, done = _teacher_turn(
            space.client,
            operations,
            conversation_id,
            sid,
            {
                "grounding": "uploaded_material",
                "retrieval_mode": "keyword_only",
                "text": T1_MESSAGE,
                "idempotency_key": uuid4().hex,
            },
        )
        assert turn["requires_confirmation"] is True
        operation = turn["operation"]
        assert operation["request_budget"] == 1
        assert operation["steps"][0]["task_kind"] == "default_llm"
        teacher_budget = operation["steps"][0]["task_model_budgets"]["default_llm"]
        assert teacher_budget["input_token_limit"] == 6_000
        assert teacher_budget["output_token_limit"] == 1_500
        assert done["operation"]["state"] == "succeeded", done["operation"]
        reply = done["conversation"]["messages"][-1]
        assert reply["reply"] is not None
        assert reply["reply"]["citations"]
        citation = reply["citations"][0]
        assert citation["document_id"] == material_id
        assert citation["location"]["anchor"]
        material_view = space.client.get(f"/api/learning/materials/{material_id}")
        assert material_view.status_code == 200, material_view.text
        anchors = {
            block["anchor"]
            for block in material_view.json()["blocks"]
            if block.get("anchor")
        }
        assert citation["location"]["anchor"] in anchors
        assert len(calls) == 1
        assert operations.view(UUID(operation["id"])).actual_requests == 1
        assert operations.view(UUID(operation["id"])).steps[0]["task_actual_requests"] == {
            "default_llm": 1
        }
        assert _grade_rows(space) == before
    finally:
        asyncio.run(operations.aclose())
        asyncio.run(space.subjects.runtime_snapshots.aclose())
        harness.close()
