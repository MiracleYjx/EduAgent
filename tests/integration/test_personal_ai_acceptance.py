"""T083 batch-one acceptance (G1, A1, S1-S4, T1-T2) with MockTransport.

The offline checks below use the same real PostgreSQL-backed personal services,
runtime snapshots and learning-operation ledger as production. Only the model
HTTP transport is replaced. The eight business calls are proven to be exactly
eight Mock responses; no supplier endpoint, credential or real send is used.

This file does not claim batch two (G2, P1, V1, R1), the browser entry or T083
completion. Those remain explicit follow-up work.
"""

from __future__ import annotations

import asyncio
import json
import re
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from backend.app.domain.enums import QuestionSourceType
from backend.app.models.practice import GradeAttempt
from backend.app.models.question import Question
from backend.app.models.question_revision import (
    QuestionRevision,
    question_revision_knowledge_points,
)
from backend.app.models.subject import KnowledgePoint
from backend.app.schemas.personal_learning import PersonalAnswerResult
from backend.app.schemas.personal_practice import PersonalGenerationResult
from backend.app.schemas.personal_teacher import TeacherReply
from backend.app.services.grading.subjective_grader import SubjectiveGradingPayload
from backend.app.services.learning_operations import LearningOperations
from tests.integration import test_learning_material_versions as material_tests
from tests.integration.test_personal_practice_flow import add_history, seed_bank
from tests.support.runtime_settings_fakes import settings_harness

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / "tests" / "fixtures" / "personal_ai_acceptance" / "samples.json"
EVIDENCE = ROOT / ".cache" / "t083-a2" / "evidence" / "activation-final.json"
RECEIPT = ROOT / ".cache" / "t083-a2" / "test-resource-profile.json"

ENVIRONMENT_ID = UUID("8b0d683a-afed-438b-b53c-5e636cccc68d")
REQUIRED_CAPABILITY_KEYS = {
    "default_llm:text",
    "default_llm:json_object",
    "embedding:embedding",
    "ocr:ocr",
    "question_generation:text",
    "question_generation:json_object",
    "subjective_grading:text",
    "subjective_grading:json_object",
}
SCENARIO_IDS = ("G1", "G2", "A1", "P1", "S1", "S2", "S3", "S4", "R1", "V1", "T1", "T2")
CHECK_KINDS = (
    "answer_correctness",
    "condition_sufficiency",
    "option_ambiguity",
    "rubric_clarity",
)

space = material_tests.space


def test_acceptance_budget_matches_the_approved_amendment():
    samples = json.loads(SAMPLES.read_text(encoding="utf-8"))
    fields = samples["gp_fields_pending"]
    a2 = fields["amendment_a2"]
    assert a2["approved_total_requests"] == 25
    assert a2["approved_total_input_tokens"] == 120_000
    assert a2["approved_total_output_tokens"] == 30_000
    assert a2["approved_total_amount"] == 1.01
    assert samples["scenarios"] and tuple(item["id"] for item in samples["scenarios"]) == SCENARIO_IDS
    primary = sum(item["primary_requests"] for item in samples["scenarios"])
    plan = samples["proposed_request_plan"]
    assert primary == plan["primary"] == 12
    assert plan["index_embedding_requests"] + plan["query_embedding_requests"] == 3
    sent = a2["requests_sent"]
    assert sent + primary + 3 <= a2["approved_total_requests"]


def test_test_receipt_keeps_the_credential_environment():
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    assert UUID(receipt["environment_id"]) == ENVIRONMENT_ID
    identity = receipt["database_identity"]
    assert identity["database"] == "eduagent_test"
    assert identity["user"] == "eduagent_test_runner"
    assert identity["schema_name"] == "public"
    assert identity["instance_id"].strip()


def test_activated_revision_targets_real_providers():
    summary = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert summary["status"] == "active"
    assert REQUIRED_CAPABILITY_KEYS <= set(summary["activation_keys"])
    assert summary["remote_requests_total"] == 4
    assert summary["local_probe"]["status"] == "succeeded"
    assert summary["local_probe"]["external_requests"] == 0


def test_g1_prompt_pins_mandatory_chart_quality_scope_and_uncertainty():
    from backend.app.ai.agents.question_agent import personal_generation_messages

    system = personal_generation_messages(
        {"count": 1}, PersonalGenerationResult.model_json_schema()
    )[0]["content"]
    assert "quality.chart_answer_points" in system
    assert "zero-based index" in system
    assert "Every question must include at least one chart block" in system
    assert "never omit the chart" in system
    assert "those points must exactly match" in system
    assert "General knowledge is valid grounding" in system
    assert "Do not add uncertainty merely because general knowledge" in system
    assert "Only add uncertainty for genuine missing conditions" in system
    assert "at least two options with unique IDs" in system
    assert "short_answer must not include options" in system


# ---------------------------------------------------------------------------
# Mock payloads and the one shared offline transport
# ---------------------------------------------------------------------------


def _quality(basis: str, *, chart_points: list[dict[str, float]] | None = None) -> dict:
    return {
        "answer_basis": basis,
        "assessment": {
            "checks": [
                {"kind": kind, "verdict": "pass", "reason": "Synthetic acceptance basis"}
                for kind in CHECK_KINDS
            ],
            "issues": [],
        },
        "chart_answer_points": {"1": chart_points} if chart_points else {},
    }


def _generation_payload() -> dict:
    points = [{"x": 0.0, "y": 1.0}, {"x": 2.0, "y": 5.0}]
    chart = {
        "type": "chart",
        "chart": {
            "kind": "line",
            "title": "一次函数图像",
            "x_axis": {"label": "x", "unit": "单位"},
            "y_axis": {"label": "y", "unit": "单位"},
            "points": points,
            "basis": "题目所给坐标 (0,1) 与 (2,5)",
        },
    }
    result = {
        "questions": [
            {
                "question_type": "single_choice",
                "stem_blocks": [
                    {"type": "text", "text": "根据图中两点，选择正确的函数表达式。"},
                    chart,
                ],
                "options": [
                    {"id": "A", "blocks": [{"type": "text", "text": "y=2x+1"}]},
                    {"id": "B", "blocks": [{"type": "text", "text": "y=x+2"}]},
                ],
                "explanation_blocks": [],
                "reference_answer": "A",
                "scoring_rubric": None,
                "quality": _quality(
                    "由 (0,1) 与 (2,5) 得斜率 2，截距 1。",
                    chart_points=points,
                ),
                "knowledge_point_ids": [],
                "evidence_chunk_ids": [],
                "difficulty": None,
                "uncertainties": [],
            }
        ]
    }
    return PersonalGenerationResult.model_validate(result).model_dump(mode="json")


def _answer_payload() -> dict:
    result = {
        "reference_answer": "A",
        "scoring_rubric": None,
        "explanation_blocks": [
            {"type": "text", "text": "按题给条件，A 是唯一满足的选项。"}
        ],
        "quality": _quality("由题给条件逐项排除后得到 A。"),
    }
    return PersonalAnswerResult.model_validate(result).model_dump(mode="json")


def _section(text: str, label: str) -> str:
    match = re.search(
        re.escape(label) + r"\n(.*?)(?=\n【|\Z)",
        text,
        flags=re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"grading request is missing section {label}")
    return match.group(1).strip()


def _grading_payload(body: dict) -> dict:
    prompt = body["messages"][-1]["content"]
    answer = _section(prompt, "【学生答案】")
    if answer == "20×5=100平方米":
        score, correct, missing = 10, ["长乘宽", "100", "平方米"], []
    elif answer == "20×5=100":
        score, correct, missing = 8, ["长乘宽", "100"], ["平方米"]
    elif answer == "20+5=25米":
        score, correct, missing = 0, [], ["长乘宽", "100", "平方米"]
    else:
        raise AssertionError(f"unexpected S-scenario answer: {answer!r}")
    result = {
        "score": score,
        "confidence": 0.95,
        "reason": f"Synthetic rubric result for {answer}",
        "correct_points": correct,
        "missing_knowledge_points": missing,
        "suggestions": ["按评分标准逐项核对。"],
    }
    return SubjectiveGradingPayload.model_validate(result).model_dump(mode="json")


def _teacher_payload(payload: dict) -> dict:
    sources = payload["context"]["sources"]
    if not sources:
        raise AssertionError("teacher request must carry the actual keyword source")
    result = {
        "answer_body": "斜率表示每单位 x 变化时 y 的变化量，本题资料已给出该定义。",
        "basis": "uploaded_material",
        "citations": [sources[0]["id"]],
        "uncertainties": [],
        "practice_knowledge_point_ids": [],
    }
    return TeacherReply.model_validate(result).model_dump(mode="json")


def _mock_handler(calls: list[dict]):
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        raw = body["messages"][-1]["content"]
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict) and "conditions" in payload and "practice_id" in payload:
            kind, output = "generation", _generation_payload()
        elif isinstance(payload, dict) and "mode" in payload and "question_id" in payload:
            kind, output = "answer", _answer_payload()
        elif isinstance(payload, dict) and "context" in payload and "question" in payload:
            kind, output = "teacher", _teacher_payload(payload)
        else:
            kind, output = "grading", _grading_payload(body)
        calls.append({"kind": kind, "body": body, "payload": payload})
        return httpx.Response(
            200,
            json={
                "id": f"mock-batch1-{len(calls)}",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(output, ensure_ascii=False),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 10,
                    "total_tokens": 30,
                },
            },
        )

    return handler


@pytest.fixture
def batch(space, tmp_path):
    harness = settings_harness(
        tmp_path / "batch1-runtime",
        environment_id=space.profile.environment_id,
    )
    asyncio.run(harness.activate_model("batch1-mock", with_credential=True))
    calls: list[dict] = []
    transport = httpx.MockTransport(_mock_handler(calls))
    operations = LearningOperations(
        sessions=space.subjects.sessions,
        config=harness.service,
        transport=transport,
    )
    space.subjects.config = harness.service
    space.client.app.state.subject_service = space.subjects
    space.client.app.state.learning_operations = operations
    value = SimpleNamespace(
        space=space,
        harness=harness,
        operations=operations,
        calls=calls,
        client=space.client,
    )
    try:
        yield value
    finally:
        asyncio.run(operations.aclose())
        harness.close()


def _seed_missing_answer(space, name: str) -> tuple[UUID, UUID, UUID]:
    sid = UUID(material_tests.subject(space, name))
    point = uuid4()
    question_id = uuid4()
    with space.subjects.sessions.begin() as session:
        session.add(
            KnowledgePoint(
                id=point,
                subject_id=sid,
                name="补答案",
                confirmation_state="confirmed",
            )
        )
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
            id=uuid4(),
            question_id=question.id,
            subject_id=sid,
            revision=1,
            question_type="single_choice",
            stem_blocks=[{"type": "text", "text": "缺答案题：选出正确选项。"}],
            options=[
                {"id": "A", "blocks": [{"type": "text", "text": "正确项"}]},
                {"id": "B", "blocks": [{"type": "text", "text": "干扰项"}]},
            ],
            reference_answer=None,
            scoring_rubric=None,
            explanation_blocks=[],
            grounding="general_knowledge",
            grounding_refs={},
            admission_state="incomplete",
            default_score=10,
        )
        session.add(revision)
        session.flush()
        question.current_revision_id = revision.id
        session.execute(
            question_revision_knowledge_points.insert().values(
                revision_id=revision.id, subject_id=sid, knowledge_point_id=point
            )
        )
    return sid, question_id, point


def _seed_area_question(space, name: str) -> tuple[UUID, UUID, UUID]:
    sid = UUID(material_tests.subject(space, name))
    point = uuid4()
    question_id = uuid4()
    with space.subjects.sessions.begin() as session:
        session.add(
            KnowledgePoint(
                id=point,
                subject_id=sid,
                name="长方形面积",
                confirmation_state="confirmed",
            )
        )
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
            id=uuid4(),
            question_id=question.id,
            subject_id=sid,
            revision=1,
            question_type="short_answer",
            stem_blocks=[
                {
                    "type": "text",
                    "text": "长方形长20米、宽5米，求面积，写出计算式、结果和单位。",
                }
            ],
            options=[],
            reference_answer="20×5=100平方米。",
            scoring_rubric=(
                "选择长乘宽并写出20×5得4分；计算结果100得4分；"
                "单位为平方米得2分。"
            ),
            explanation_blocks=[],
            grounding="general_knowledge",
            grounding_refs={"answer_origin": "paper"},
            admission_state="eligible",
            default_score=10,
        )
        session.add(revision)
        session.flush()
        question.current_revision_id = revision.id
        session.execute(
            question_revision_knowledge_points.insert().values(
                revision_id=revision.id, subject_id=sid, knowledge_point_id=point
            )
        )
    return sid, question_id, point


def _start_one(batch, sid: UUID) -> dict:
    preview = batch.client.post(
        f"/api/learning/subjects/{sid}/practices/preview",
        json={"source": "uploaded", "count": 1, "question_types": ["short_answer"]},
    )
    assert preview.status_code == 200, preview.text
    value = preview.json()
    assert value["selected_count"] == 1 and value["can_start"], value
    started = batch.client.post(
        f"/api/learning/practice-previews/{value['id']}/start",
        json={"preview_digest": value["preview_digest"], "scores": {}},
    )
    assert started.status_code == 200, started.text
    practice = started.json()
    assert len(practice["items"]) == 1
    return practice


def _answer_one(batch, practice: dict, text: str) -> None:
    item = practice["items"][0]
    response = batch.client.put(
        f"/api/learning/practices/{practice['id']}/items/{item['id']}/answer",
        json={"text_answer": text},
    )
    assert response.status_code == 200, response.text
    assert response.json()["answer_state"] == "answered"


def _grade_one(batch, practice: dict) -> tuple[dict, dict]:
    preview = batch.client.post(
        f"/api/learning/practices/{practice['id']}/grading-preview",
        json={"idempotency_key": uuid4().hex, "blank_confirmed": True},
    )
    assert preview.status_code == 200, preview.text
    operation = preview.json()
    assert operation["request_budget"] == 1
    assert batch.calls == [] or batch.calls[-1]["kind"] == "grading"
    blocked = batch.client.post(
        f"/api/learning/practices/{practice['id']}/grade/{operation['id']}",
        json={},
    )
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "CONFIRMATION_REQUIRED"
    assert batch.operations.view(UUID(operation["id"])).actual_requests == 0
    confirmed = batch.client.post(
        f"/api/learning/operations/{operation['id']}/confirm",
        json={"preview_digest": operation["preview_digest"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    result = batch.client.post(
        f"/api/learning/practices/{practice['id']}/grade/{operation['id']}",
        json={},
    )
    assert result.status_code == 200, result.text
    assert batch.operations.view(UUID(operation["id"])).actual_requests == 1
    return operation, result.json()


def _fixed_sections(prompt: str) -> dict[str, str]:
    return {
        label: _section(prompt, label)
        for label in ("【题目】", "【标准答案】", "【原始评分标准】", "【学生答案】")
    }


def _grade_rows(space) -> list[tuple]:
    with space.subjects.sessions() as session:
        return list(
            session.execute(
                select(
                    GradeAttempt.id,
                    GradeAttempt.score,
                    GradeAttempt.validation_state,
                    GradeAttempt.round,
                ).order_by(GradeAttempt.id)
            ).all()
        )


def test_g1_general_generation_with_chart(batch):
    sid = UUID(material_tests.subject(batch.space, "G1 general knowledge"))
    preview = batch.client.post(
        f"/api/learning/subjects/{sid}/practices/preview",
        json={"source": "ai", "count": 1},
    )
    assert preview.status_code == 200, preview.text
    practice_preview = preview.json()
    assert practice_preview["selected_count"] == 0

    operation = batch.client.post(
        f"/api/learning/practice-previews/{practice_preview['id']}/generation-preview",
        json={"grounding": "general_knowledge", "idempotency_key": uuid4().hex},
    )
    assert operation.status_code == 200, operation.text
    value = operation.json()
    assert value["request_budget"] == 1 and batch.calls == []
    blocked = batch.client.post(
        f"/api/learning/practice-previews/{practice_preview['id']}/generate/{value['id']}",
        json={},
    )
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "CONFIRMATION_REQUIRED"
    confirmed = batch.client.post(
        f"/api/learning/operations/{value['id']}/confirm",
        json={"preview_digest": value["preview_digest"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    generated = batch.client.post(
        f"/api/learning/practice-previews/{practice_preview['id']}/generate/{value['id']}",
        json={},
    )
    assert generated.status_code == 200, generated.text
    result = generated.json()
    assert result["items"][0]["question_type"] == "single_choice"
    assert batch.calls[0]["kind"] == "generation"
    assert batch.calls[0]["payload"]["grounding"] == "general_knowledge"
    assert batch.calls[0]["payload"]["evidence"] == []
    assert batch.operations.view(UUID(value["id"])).actual_requests == 1

    question_id = result["items"][0]["question_id"]
    question = batch.client.get(f"/api/learning/questions/{question_id}")
    assert question.status_code == 200, question.text
    blocks = question.json()["stem_blocks"]
    assert [block["type"] for block in blocks] == ["text", "chart"]
    chart = blocks[1]["chart"]
    assert chart["x_axis"]["unit"] == "单位"
    assert chart["points"] == [{"x": 0.0, "y": 1.0}, {"x": 2.0, "y": 5.0}]
    assert len(batch.calls) == 1


def test_a1_missing_answer_completion(batch):
    _sid, question_id, _point = _seed_missing_answer(batch.space, "A1 missing answer")
    operation = batch.client.post(
        f"/api/learning/questions/{question_id}/answer-preview",
        json={
            "expected_revision": 1,
            "mode": "complete",
            "idempotency_key": uuid4().hex,
        },
    )
    assert operation.status_code == 200, operation.text
    value = operation.json()
    assert value["request_budget"] == 1 and batch.calls == []
    blocked = batch.client.post(
        f"/api/learning/questions/{question_id}/answer/{value['id']}", json={}
    )
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "CONFIRMATION_REQUIRED"
    confirmed = batch.client.post(
        f"/api/learning/operations/{value['id']}/confirm",
        json={"preview_digest": value["preview_digest"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    completed = batch.client.post(
        f"/api/learning/questions/{question_id}/answer/{value['id']}", json={}
    )
    assert completed.status_code == 200, completed.text
    result = completed.json()
    assert result["revision"] == 2
    assert result["reference_answer"] == "A"
    assert result["answer_origin"] == "ai_completion"
    assert batch.calls[0]["kind"] == "answer"
    assert batch.calls[0]["payload"]["mode"] == "complete"
    assert batch.operations.view(UUID(value["id"])).actual_requests == 1
    assert len(batch.calls) == 1


def test_s1_full_score(batch):
    sid, _question_id, _point = _seed_area_question(batch.space, "S1 full score")
    practice = _start_one(batch, sid)
    _answer_one(batch, practice, "20×5=100平方米")
    _operation, report = _grade_one(batch, practice)
    item = report["items"][0]
    assert Decimal(str(item["score"])) == Decimal(10)
    assert item["validation_state"] == "valid"
    assert item["execution_source"] == "model"
    assert report["answered_zero_count"] == 0
    assert len(batch.calls) == 1
    assert batch.calls[0]["kind"] == "grading"


def test_s2_and_s4_same_fixed_input(batch):
    sid2, _question2, _point2 = _seed_area_question(batch.space, "S2 partial score")
    practice2 = _start_one(batch, sid2)
    _answer_one(batch, practice2, "20×5=100")
    _operation2, report2 = _grade_one(batch, practice2)
    assert Decimal(str(report2["items"][0]["score"])) == Decimal(8)

    sid4, _question4, _point4 = _seed_area_question(batch.space, "S4 repeated input")
    practice4 = _start_one(batch, sid4)
    _answer_one(batch, practice4, "20×5=100")
    _operation4, report4 = _grade_one(batch, practice4)
    assert Decimal(str(report4["items"][0]["score"])) == Decimal(8)

    assert len(batch.calls) == 2
    first = _fixed_sections(batch.calls[0]["body"]["messages"][-1]["content"])
    second = _fixed_sections(batch.calls[1]["body"]["messages"][-1]["content"])
    assert first == second
    assert all(value for value in first.values())


def test_s3_answered_zero(batch):
    sid, _question_id, _point = _seed_area_question(batch.space, "S3 answered zero")
    practice = _start_one(batch, sid)
    _answer_one(batch, practice, "20+5=25米")
    _operation, report = _grade_one(batch, practice)
    item = report["items"][0]
    assert Decimal(str(item["score"])) == Decimal(0)
    assert item["validation_state"] == "valid"
    assert item["execution_source"] == "model"
    assert report["answered_zero_count"] == 1
    assert len(batch.calls) == 1


def _teacher_turn(batch, conversation_id: UUID, sid: UUID, body: dict) -> tuple[dict, dict]:
    preview = batch.client.post(
        f"/api/learning/conversations/{conversation_id}/messages/preview?subject_id={sid}",
        json=body,
    )
    assert preview.status_code == 200, preview.text
    turn = preview.json()
    operation = turn["operation"]
    assert batch.operations.view(UUID(operation["id"])).actual_requests == 0
    confirmed = batch.client.post(
        f"/api/learning/conversations/{conversation_id}/messages/{operation['id']}/confirm?subject_id={sid}",
        json={"preview_digest": operation["preview_digest"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    done = batch.client.post(
        f"/api/learning/conversations/{conversation_id}/messages/{operation['id']}/execute?subject_id={sid}",
        json={},
    )
    assert done.status_code == 200, done.text
    return turn, done.json()


def test_t1_t2_teacher_citations_and_read_only_grades(batch):
    sid, point, ids, _document = seed_bank(
        batch.space, name="T1 T2 teacher", history=False, count=1
    )
    add_history(batch.space, sid, ids[0], answered=True, score=10)

    material = material_tests.upload(
        batch.space,
        str(sid),
        "teacher-batch1.md",
        "一次函数 斜率 含义 截距 简单 说明\n\n斜率 表示 每单位 x 变化时 y 的变化量。".encode(),
    )
    assert material.status_code == 201, material.text
    material_tests.index(batch.space, material.json()["id"])

    before = _grade_rows(batch.space)
    assert len(before) == 1 and before[0][1] == Decimal(10)
    created = batch.client.post(
        f"/api/learning/subjects/{sid}/conversations", json={"title": "T1/T2"}
    )
    assert created.status_code == 201, created.text
    conversation_id = UUID(created.json()["id"])
    base = {
        "grounding": "uploaded_material",
        "retrieval_mode": "keyword_only",
    }
    first, first_done = _teacher_turn(
        batch,
        conversation_id,
        sid,
        {
            **base,
            "text": "一次函数 斜率 含义",
            "idempotency_key": uuid4().hex,
        },
    )
    assert first["requires_confirmation"] is True
    assert first_done["operation"]["state"] == "succeeded", first_done["operation"]
    first_reply = first_done["conversation"]["messages"][-1]
    assert first_reply["reply"] is not None, first_reply
    assert first_reply["reply"]["citations"]
    assert first_reply["citations"][0]["document_id"] == material.json()["id"]
    assert first_reply["citations"][0]["location"]

    second, second_done = _teacher_turn(
        batch,
        conversation_id,
        sid,
        {
            **base,
            "text": "一次函数 斜率 简单",
            "idempotency_key": uuid4().hex,
        },
    )
    assert second["requires_confirmation"] is False
    assert len(second_done["conversation"]["messages"]) == 4
    assert len(batch.calls) == 2
    assert [call["kind"] for call in batch.calls] == ["teacher", "teacher"]
    assert len(batch.calls[1]["payload"]["context"]["history"]) >= 2

    after = _grade_rows(batch.space)
    assert after == before
    assert point is not None
    assert batch.operations.snapshots.active_lease_count == 0
