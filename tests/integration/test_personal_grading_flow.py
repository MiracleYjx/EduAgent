"""Personal grading flow with a Fake subjective port: history, failure, recovery.

No paid model is called. The Fake only proves protocol/state handling; it never
stands in for real grading quality.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select

from backend.app.models.practice import GradeAttempt, PracticeAnswer
from backend.app.models.question_revision import QuestionRevision
from backend.app.services.personal_grading import SubjectiveResult, grade_practice
from backend.app.services.practice_service import PracticeService
from backend.app.services.settings.runtime_snapshot import RuntimeSnapshotService
from tests.integration import test_learning_material_versions as material_tests
from tests.integration.test_personal_practice_flow import seed_bank
from tests.support import runtime_consumer_fakes as fakes
from tests.support import runtime_settings_fakes as settings_fakes

space = material_tests.space


def _general_knowledge_subjective(space, ids) -> None:
    with space.subjects.sessions.begin() as session:
        revision = session.scalar(
            select(QuestionRevision).where(QuestionRevision.question_id == ids[2])
        )
        assert revision is not None
        revision.grounding = "general_knowledge"


def _runtime_env(space, tmp_path, provider):
    harness = settings_fakes.settings_harness(
        tmp_path / "grading-runtime", environment_id=space.profile.environment_id
    )
    values = harness.values("t053-model-a")
    default = values["bindings"]["default_llm"]
    fakes.bind_task(
        values,
        "subjective_grading",
        UUID(default["profile_id"]),
        UUID(default["profile_revision"]),
        retrieval_mode="keyword",
    )
    candidate = harness.draft(values)
    asyncio.run(harness.activate(candidate))
    factory = fakes.RecordingClientFactory(
        {"subjective_grading": lambda profile: provider}
    )
    service = RuntimeSnapshotService(harness.service, client_factory=factory)
    space.subjects.config = harness.service
    space.subjects.runtime_snapshots = service
    return harness, factory, service


def start_practice(space, sid, count: int = 3) -> dict:
    preview = space.client.post(
        f"/api/learning/subjects/{sid}/practices/preview", json={"count": count}
    ).json()
    return space.client.post(
        f"/api/learning/practice-previews/{preview['id']}/start",
        json={"preview_digest": preview["preview_digest"], "scores": {}},
    ).json()


def answer(space, practice, item, *, choice_ids=None, text_answer=None) -> None:
    body: dict[str, object] = {}
    if choice_ids is not None:
        body["choice_ids"] = choice_ids
    if text_answer is not None:
        body["text_answer"] = text_answer
    response = space.client.put(
        f"/api/learning/practices/{practice['id']}/items/{item['id']}/answer", json=body
    )
    assert response.status_code == 200, response.text


def item_of(practice, question_type: str) -> dict:
    return next(
        item
        for item in practice["items"]
        if item["content"]["question_type"] == question_type
    )


def attempts(space, practice) -> list[GradeAttempt]:
    with space.subjects.sessions() as session:
        answers = list(
            session.scalars(
                select(PracticeAnswer).where(
                    PracticeAnswer.practice_item_id.in_(
                        [item["id"] for item in practice["items"]]
                    )
                )
            )
        )
        return list(
            session.scalars(
                select(GradeAttempt).where(
                    GradeAttempt.answer_id.in_([row.id for row in answers])
                )
            )
        )


def submit(space, practice) -> dict:
    response = space.client.post(
        f"/api/learning/practices/{practice['id']}/submit",
        json={"blank_confirmed": True},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_fake_subjective_success_records_model_facts_and_partial_credit(space):
    sid, _point, _ids, _document = seed_bank(space, history=False, count=3)
    practice = start_practice(space, sid)
    single = item_of(practice, "single_choice")
    short = item_of(practice, "short_answer")
    answer(space, practice, single, choice_ids=["A"])
    answer(space, practice, short, text_answer="四")

    def fake(item, answer_row):
        assert str(item.id) == short["id"]
        assert answer_row.text_answer == "四"
        return SubjectiveResult(
            score=Decimal("6.00"),
            reason="命中要点一，缺少要点二。",
            confidence=0.8,
            usage={"prompt_tokens": 10, "completion_tokens": 5},
            runtime_snapshot_id=uuid4(),
        )

    with space.subjects.sessions.begin() as session:
        grade = grade_practice(
            session,
            practice_id=practice["id"],
            blank_confirmed=True,
            subjective=fake,
        )
    assert grade.all_valid

    report = submit(space, practice)
    by_item = {row["item_id"]: row for row in report["items"]}
    assert by_item[short["id"]]["execution_source"] == "model"
    assert by_item[short["id"]]["score"] == "6.0000" or by_item[short["id"]]["score"] == "6.00"
    assert by_item[short["id"]]["validation_state"] == "valid"

    model_rows = [
        row for row in attempts(space, practice) if row.execution_source == "model"
    ]
    assert len(model_rows) == 1
    assert model_rows[0].confidence == 0.8
    assert model_rows[0].usage == {"prompt_tokens": 10, "completion_tokens": 5}
    assert model_rows[0].runtime_snapshot_id is not None
    assert report["state"] == "completed"


def test_fake_subjective_failure_keeps_the_round_honest(space):
    sid, _point, _ids, _document = seed_bank(space, history=False, count=3)
    practice = start_practice(space, sid)
    single = item_of(practice, "single_choice")
    short = item_of(practice, "short_answer")
    answer(space, practice, single, choice_ids=["A"])
    answer(space, practice, short, text_answer="四")

    def failing(item, answer_row):
        return SubjectiveResult(
            score=None,
            reason="模型超时，未产生可核验分数。",
            validation_state="failed",
            error_code="MODEL_TIMEOUT",
        )

    with space.subjects.sessions.begin() as session:
        grade = grade_practice(
            session,
            practice_id=practice["id"],
            blank_confirmed=True,
            subjective=failing,
        )
    assert not grade.all_valid

    report = submit(space, practice)
    by_item = {row["item_id"]: row for row in report["items"]}
    assert by_item[short["id"]]["score"] is None
    assert by_item[short["id"]]["error_code"] == "MODEL_TIMEOUT"
    assert report["state"] == "partial_failed"
    assert report["group_score_rate"] is None
    failed_rows = [row for row in attempts(space, practice) if row.error_code == "MODEL_TIMEOUT"]
    assert failed_rows and failed_rows[0].score is None


def test_snapshot_runtime_success_records_feedback_and_releases_client(space, tmp_path):
    sid, _point, ids, _document = seed_bank(space, history=False, count=3)
    _general_knowledge_subjective(space, ids)
    provider = fakes.FakeLLM(
        "t053-model-a", payload=fakes.scoring_payload(score=6.0, confidence=0.9)
    )
    harness, factory, service = _runtime_env(space, tmp_path, provider)
    try:
        practice = start_practice(space, sid)
        short = item_of(practice, "short_answer")
        answer(space, practice, short, text_answer="四")
        report = submit(space, practice)
        row = next(item for item in report["items"] if item["item_id"] == short["id"])

        assert row["execution_source"] == "model"
        assert row["validation_state"] == "valid"
        assert row["score"] == "6.00"
        assert row["feedback"]["correct_points"] == ["保存数据"]
        assert row["feedback"]["missing_knowledge_points"] == ["引用数据"]
        assert row["latest_attempt_id"] == row["history"][-1]["id"]
        assert row["history"][-1]["runtime_snapshot_id"] is not None
        assert row["history"][-1]["confidence"] == 0.9
        assert provider.closed is True
        assert factory.created[0][0] == "subjective_grading"
        assert factory.created[0][1] == "t053-model-a"
        assert service.active_lease_count == 0
    finally:
        service.close()
        harness.close()


def test_snapshot_runtime_failure_is_honest_and_releases_client(space, tmp_path):
    sid, _point, ids, _document = seed_bank(space, history=False, count=3)
    _general_knowledge_subjective(space, ids)
    provider = fakes.FakeLLM(
        "t053-model-a", error=RuntimeError("synthetic provider failure")
    )
    harness, _factory, service = _runtime_env(space, tmp_path, provider)
    try:
        practice = start_practice(space, sid)
        short = item_of(practice, "short_answer")
        answer(space, practice, short, text_answer="四")
        report = submit(space, practice)
        row = next(item for item in report["items"] if item["item_id"] == short["id"])

        assert report["state"] == "partial_failed"
        assert report["group_score_rate"] is None
        assert row["score"] is None
        assert row["validation_state"] == "failed"
        assert row["error_code"] == "GRADING_PROVIDER_FAILED"
        assert row["history"][-1]["runtime_snapshot_id"] is not None
        assert provider.closed is True
        assert service.active_lease_count == 0
    finally:
        service.close()
        harness.close()


def test_invalid_structured_payload_is_not_turned_into_a_score(space, tmp_path):
    sid, _point, ids, _document = seed_bank(space, history=False, count=3)
    _general_knowledge_subjective(space, ids)
    provider = fakes.FakeLLM(
        "t053-model-a", payload={"reason": "not a structured grading payload"}
    )
    harness, _factory, service = _runtime_env(space, tmp_path, provider)
    try:
        practice = start_practice(space, sid)
        short = item_of(practice, "short_answer")
        answer(space, practice, short, text_answer="四")
        report = submit(space, practice)
        row = next(item for item in report["items"] if item["item_id"] == short["id"])

        assert report["state"] == "partial_failed"
        assert row["score"] is None
        assert row["validation_state"] == "failed"
        assert row["error_code"] == "GRADING_INVALID_LLM_RESPONSE"
        assert row["history"][-1]["runtime_snapshot_id"] is not None
        assert provider.closed is True
        assert service.active_lease_count == 0
    finally:
        service.close()
        harness.close()


def test_snapshot_prevents_config_switch_from_changing_the_model(space, tmp_path):
    sid, _point, ids, _document = seed_bank(space, history=False, count=3)
    _general_knowledge_subjective(space, ids)
    provider = fakes.FakeLLM(
        "t053-model-a", payload=fakes.scoring_payload(score=6.0, confidence=0.9)
    )
    harness, factory, service = _runtime_env(space, tmp_path, provider)
    real_open = service.open_run

    def open_then_switch(**kwargs):
        runtime = real_open(**kwargs)
        expected = harness.service.active_revision().revision_id
        asyncio.run(harness.activate_model("t053-model-b", expected=expected))
        return runtime

    service.open_run = open_then_switch  # type: ignore[method-assign]
    try:
        practice = start_practice(space, sid)
        short = item_of(practice, "short_answer")
        answer(space, practice, short, text_answer="四")
        report = submit(space, practice)
        assert report["state"] == "completed"
        assert factory.created[0][1] == "t053-model-a"
        assert provider.model_name == "t053-model-a"
        assert provider.closed is True
        assert service.active_lease_count == 0
    finally:
        service.close()
        harness.close()


def test_regrade_only_appends_the_disputed_item_round(space, tmp_path):
    sid, _point, ids, _document = seed_bank(space, history=False, count=3)
    _general_knowledge_subjective(space, ids)
    provider = fakes.FakeLLM(
        "t053-model-a", payload=fakes.scoring_payload(score=6.0, confidence=0.9)
    )
    harness, _factory, service = _runtime_env(space, tmp_path, provider)
    try:
        practice = start_practice(space, sid)
        single = item_of(practice, "single_choice")
        short = item_of(practice, "short_answer")
        answer(space, practice, single, choice_ids=["B"])
        answer(space, practice, short, text_answer="四")
        report = submit(space, practice)
        assert report["state"] == "completed"
        calls_before = len(provider.calls)

        raised = space.client.post(
            f"/api/learning/practices/{practice['id']}/disputes",
            json={"item_id": single["id"], "reason": "本地客观分有误，请复核。"},
        ).json()
        regraded = space.client.post(
            f"/api/learning/disputes/{raised['id']}/regrade", json={}
        )
        assert regraded.status_code == 200, regraded.text
        assert len(provider.calls) == calls_before

        rows = attempts(space, practice)
        with space.subjects.sessions() as session:
            answer_ids = dict(
                session.execute(
                    select(PracticeAnswer.practice_item_id, PracticeAnswer.id).where(
                        PracticeAnswer.practice_item_id.in_([single["id"], short["id"]])
                    )
                ).all()
            )
        single_answer_id = answer_ids[UUID(single["id"])]
        short_answer_id = answer_ids[UUID(short["id"])]
        rounds = {}
        for row in rows:
            rounds.setdefault(row.answer_id, []).append(row.round)
        # The disputed objective answer has two rounds; the unrelated short
        # answer keeps exactly one round and is not re-sent to the model.
        assert sorted(rounds[single_answer_id]) == [1, 2]
        assert sorted(rounds[short_answer_id]) == [1]
        assert len(provider.calls) == calls_before
    finally:
        service.close()
        harness.close()


def test_concurrent_regrade_is_serialized_into_unique_rounds(space):
    sid, _point, _ids, _document = seed_bank(space, history=False, count=3)
    practice = start_practice(space, sid)
    single = item_of(practice, "single_choice")
    answer(space, practice, single, choice_ids=["B"])
    submit(space, practice)

    def regrade_once():
        with space.subjects.sessions.begin() as session:
            return grade_practice(
                session,
                practice_id=practice["id"],
                blank_confirmed=True,
                regrade=True,
            ).round

    with ThreadPoolExecutor(max_workers=2) as pool:
        rounds = list(pool.map(lambda _: regrade_once(), range(2)))
    assert len(set(rounds)) == 2
    rows = attempts(space, practice)
    single_rounds = [
        row.round
        for row in rows
        if row.answer_id
        == next(
            row.answer_id
            for row in rows
            if row.round == max(item.round for item in rows)
        )
    ]
    assert sorted(single_rounds) == [1, 2, 3]


def test_regrade_appends_history_and_keeps_the_first_round_readable(space):
    sid, _point, _ids, _document = seed_bank(space, history=False, count=3)
    practice = start_practice(space, sid)
    single = item_of(practice, "single_choice")
    answer(space, practice, single, choice_ids=["B"])
    submit(space, practice)

    first_round = {
        (row.answer_id, row.round): row.score for row in attempts(space, practice)
    }

    with space.subjects.sessions.begin() as session:
        grade_practice(
            session, practice_id=practice["id"], blank_confirmed=True, regrade=True
        )

    rounds = {(row.answer_id, row.round) for row in attempts(space, practice)}
    assert all((answer_id, 1) in rounds for answer_id, _round in rounds)
    assert all((answer_id, 2) in rounds for answer_id, _round in rounds)
    # 原轮次分数保持不变，新轮次独立存在。
    for (answer_id, round_no), score in first_round.items():
        if round_no == 1:
            current = next(
                row
                for row in attempts(space, practice)
                if row.answer_id == answer_id and row.round == 1
            )
            assert current.score == score

    report = PracticeService(space.subjects).report(practice["id"])
    assert report.round == 2
    assert report.items


def test_production_grading_cannot_send_without_a_confirmed_operation(space, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from backend.app.api.subjects import get_subject_service

    sid, _point, ids, _document = seed_bank(space, history=False, count=3)
    _general_knowledge_subjective(space, ids)
    provider = fakes.FakeLLM("t053-model-a", payload=fakes.scoring_payload(score=6.0, confidence=0.9))
    harness, _factory, service = _runtime_env(space, tmp_path, provider)
    practice = start_practice(space, sid)
    answer(space, practice, item_of(practice, "short_answer"), text_answer="four")
    app = space.client.app
    schema = space.engine.eduagent_test_manifest.database_identity.schema_name
    url = space.engine.url.update_query_dict({"options": f"-csearch_path={schema},public"}).render_as_string(hide_password=False)
    app.state.settings_runtime = SimpleNamespace(config=harness.service,
        personal_learning_database=lambda: (url, space.profile))
    monkeypatch.setattr("backend.app.api.subjects.RuntimeSnapshotService", lambda _config: service)
    app.dependency_overrides.pop(get_subject_service)
    try:
        response = space.client.post(f"/api/learning/practices/{practice['id']}/submit", json={"blank_confirmed": True})
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "GRADING_OPERATION_REQUIRED"
        assert provider.calls == []
        assert attempts(space, practice) == []
        assert service.active_lease_count == 0
    finally:
        service.close()
        harness.close()
