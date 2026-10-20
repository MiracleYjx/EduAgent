"""Personal grading contract: deterministic rules, blank prompt, zero model.

Real Test PostgreSQL and the real HTTP routes are used; no model is called.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from backend.app.models.practice import GradeAttempt, PracticeAnswer
from tests.integration import test_learning_material_versions as material_tests
from tests.integration.test_personal_practice_flow import seed_bank

space = material_tests.space


def start_practice(space, sid, count: int = 3):
    preview = space.client.post(
        f"/api/learning/subjects/{sid}/practices/preview", json={"count": count}
    )
    assert preview.status_code == 200, preview.text
    value = preview.json()
    started = space.client.post(
        f"/api/learning/practice-previews/{value['id']}/start",
        json={"preview_digest": value["preview_digest"], "scores": {}},
    )
    assert started.status_code == 200, started.text
    return started.json()


def item_of(practice, question_type: str) -> dict:
    for item in practice["items"]:
        if item["content"]["question_type"] == question_type:
            return item
    raise AssertionError(f"missing {question_type}")


def answer(space, practice, item, *, choice_ids=None, text_answer=None):
    body: dict[str, object] = {}
    if choice_ids is not None:
        body["choice_ids"] = choice_ids
    if text_answer is not None:
        body["text_answer"] = text_answer
    response = space.client.put(
        f"/api/learning/practices/{practice['id']}/items/{item['id']}/answer", json=body
    )
    assert response.status_code == 200, response.text


def submit(space, practice, *, blank_confirmed: bool):
    return space.client.post(
        f"/api/learning/practices/{practice['id']}/submit",
        json={"blank_confirmed": blank_confirmed},
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


@pytest.fixture(autouse=True)
def no_model_provider(monkeypatch):
    """任何本地评分路径都不得构建 LLM Provider。"""

    def forbidden(*_args, **_kwargs):
        raise AssertionError("客观题与未作答路径不得创建模型 Provider。")

    monkeypatch.setattr(
        "backend.app.ai.llm.factory.create_llm_provider", forbidden
    )


def test_objective_rules_and_plan_a_report_with_unanswered_zero(space):
    sid, _point, _ids, _document = seed_bank(space, history=False, count=3)
    practice = start_practice(space, sid)
    single = item_of(practice, "single_choice")
    multiple = item_of(practice, "multiple_choice")
    short = item_of(practice, "short_answer")

    answer(space, practice, single, choice_ids=["A"])
    answer(space, practice, multiple, choice_ids=["A"])  # 漏选 C
    # short 留空：必须由用户确认后才继续

    blank = submit(space, practice, blank_confirmed=False)
    assert blank.status_code == 409, blank.text
    assert blank.json()["code"] == "PRACTICE_BLANK_ANSWERS"
    assert blank.json()["item_ids"] == [short["id"]]
    assert attempts(space, practice) == []

    report = submit(space, practice, blank_confirmed=True)
    assert report.status_code == 200, report.text
    value = report.json()
    by_item = {row["item_id"]: row for row in value["items"]}

    # seed_bank 给第一题固定分值 8，满分即 8.00。
    assert by_item[single["id"]]["score"] == "8.00"
    assert by_item[single["id"]]["execution_source"] == "local_objective"
    assert by_item[multiple["id"]]["score"] == "0.00"
    assert by_item[multiple["id"]]["validation_state"] == "valid"
    assert by_item[short["id"]]["score"] == "0.00"
    assert by_item[short["id"]]["execution_source"] == "local_unanswered"

    assert value["state"] == "completed"
    assert value["total_count"] == 3
    assert value["answered_count"] == 2
    assert value["unanswered_count"] == 1
    assert value["valid_count"] == 3
    assert value["completion_rate"] == "0.6667"
    # 逐题得分率简单平均：1.0、0.0、0.0 → 0.3333（不含总分加权）。
    assert value["group_score_rate"] == "0.3333"
    # 只统计有效已答选择题：single 正确、multiple 漏选 → 1/2。
    assert value["choice_sample_count"] == 2
    assert value["choice_correct_rate"] == "0.5000"

    stored = attempts(space, practice)
    assert {row.execution_source for row in stored} == {
        "local_objective",
        "local_unanswered",
    }
    assert all(row.runtime_snapshot_id is None for row in stored)
    assert all(row.confidence is None and row.usage is None for row in stored)


def test_multiple_choice_requires_every_option_for_full_marks(space):
    sid, _point, _ids, _document = seed_bank(space, history=False, count=3)
    practice = start_practice(space, sid)
    multiple = item_of(practice, "multiple_choice")
    single = item_of(practice, "single_choice")
    short = item_of(practice, "short_answer")

    answer(space, practice, single, choice_ids=["B"])  # 错选
    answer(space, practice, multiple, choice_ids=["C", "A"])  # 顺序不同，集合一致
    answer(space, practice, short, text_answer="   ")  # 仅空白 → 仍是未作答

    confirmed = submit(space, practice, blank_confirmed=True)
    assert confirmed.status_code == 200, confirmed.text
    value = confirmed.json()
    by_item = {row["item_id"]: row for row in value["items"]}

    assert by_item[single["id"]]["score"] == "0.00"
    assert by_item[multiple["id"]]["score"] == "10.00"
    assert by_item[short["id"]]["answer_state"] == "unanswered"
    assert by_item[short["id"]]["execution_source"] == "local_unanswered"


def test_repeated_submit_is_idempotent_and_keeps_the_first_round(space):
    sid, _point, _ids, _document = seed_bank(space, history=False, count=3)
    practice = start_practice(space, sid)
    single = item_of(practice, "single_choice")
    answer(space, practice, single, choice_ids=["A"])

    first = submit(space, practice, blank_confirmed=True)
    assert first.status_code == 200, first.text
    rows_after_first = attempts(space, practice)

    second = submit(space, practice, blank_confirmed=True)
    assert second.status_code == 200, second.text
    assert second.json()["round"] == first.json()["round"]
    assert second.json()["items"] == first.json()["items"]
    assert len(attempts(space, practice)) == len(rows_after_first)
    assert max(row.round for row in rows_after_first) == 1


def test_answered_short_answer_without_a_capability_fails_honestly(space):
    sid, _point, _ids, _document = seed_bank(space, history=False, count=3)
    practice = start_practice(space, sid)
    single = item_of(practice, "single_choice")
    short = item_of(practice, "short_answer")
    answer(space, practice, single, choice_ids=["A"])
    answer(space, practice, short, text_answer="4")

    report = submit(space, practice, blank_confirmed=True)
    assert report.status_code == 200, report.text
    value = report.json()
    by_item = {row["item_id"]: row for row in value["items"]}

    assert value["state"] == "partial_failed"
    assert value["invalid_count"] == 1
    assert value["group_score_rate"] is None
    short_row = by_item[short["id"]]
    assert short_row["score"] is None
    assert short_row["validation_state"] == "failed"
    assert short_row["error_code"] == "SUBJECTIVE_GRADER_UNAVAILABLE"

    stored = {row.execution_source for row in attempts(space, practice)}
    assert "model" not in stored


def test_dispute_regrade_appends_a_round_and_only_a_newer_valid_round_resolves(space):
    sid, _point, _ids, _document = seed_bank(space, history=False, count=3)
    practice = start_practice(space, sid)
    single = item_of(practice, "single_choice")
    answer(space, practice, single, choice_ids=["B"])  # 错选 → 0 分
    assert submit(space, practice, blank_confirmed=True).status_code == 200

    raised = space.client.post(
        f"/api/learning/practices/{practice['id']}/disputes",
        json={"item_id": single["id"], "reason": "评分点与参考答案不一致，请复核。"},
    )
    assert raised.status_code == 200, raised.text
    dispute = raised.json()
    assert dispute["state"] == "pending"

    pending_report = space.client.get(
        f"/api/learning/practices/{practice['id']}/report"
    ).json()
    assert {
        row["item_id"]: row for row in pending_report["items"]
    }[single["id"]]["disputed"] is True
    assert pending_report["unresolved_dispute_count"] == 1
    assert pending_report["group_score_rate"] is None

    regraded = space.client.post(
        f"/api/learning/disputes/{dispute['id']}/regrade", json={}
    )
    assert regraded.status_code == 200, regraded.text
    assert regraded.json()["state"] == "awaiting_acceptance"

    rows = sorted(
        (
            row
            for row in attempts(space, practice)
            if str(row.answer_id) == dispute["answer_id"]
        ),
        key=lambda row: row.round,
    )
    assert [row.round for row in rows] == [1, 2]

    stale = space.client.post(
        f"/api/learning/disputes/{dispute['id']}/accept",
        json={"attempt_id": str(rows[0].id)},
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["code"] == "DISPUTE_ROUND_STALE"

    accepted = space.client.post(
        f"/api/learning/disputes/{dispute['id']}/accept",
        json={"attempt_id": str(rows[1].id)},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["state"] == "resolved"
    assert accepted.json()["accepted_attempt_id"] == str(rows[1].id)

    resolved_report = space.client.get(
        f"/api/learning/practices/{practice['id']}/report"
    ).json()
    assert {
        row["item_id"]: row for row in resolved_report["items"]
    }[single["id"]]["disputed"] is False
    assert resolved_report["unresolved_dispute_count"] == 0
    assert resolved_report["group_score_rate"] is not None


def test_report_is_readable_after_reload_and_scoped_to_its_practice(space):
    sid, _point, _ids, _document = seed_bank(space, history=False, count=3)
    practice = start_practice(space, sid)
    single = item_of(practice, "single_choice")
    answer(space, practice, single, choice_ids=["A"])
    assert submit(space, practice, blank_confirmed=True).status_code == 200

    reloaded = space.client.get(f"/api/learning/practices/{practice['id']}/report")
    assert reloaded.status_code == 200, reloaded.text
    assert reloaded.json()["total_count"] == 3

    other = space.client.get(
        f"/api/learning/practices/{practice['id']}/report",
        headers={"Origin": "http://evil.invalid"},
    )
    assert other.status_code == 403

    with space.subjects.sessions() as session:
        answer_rows = list(
            session.scalars(
                select(PracticeAnswer).where(
                    PracticeAnswer.practice_item_id.in_(
                        [item["id"] for item in practice["items"]]
                    )
                )
            )
        )
    assert all(row.submission_state == "submitted" for row in answer_rows)
    assert all(
        row.score <= row.max_score
        for row in attempts(space, practice)
        if row.score is not None and row.max_score is not None
    )


def test_unanswered_choices_are_not_accuracy_samples(space):
    sid, _point, _ids, _document = seed_bank(space, history=False, count=3)
    practice = start_practice(space, sid)
    single = item_of(practice, "single_choice")
    answer(space, practice, single, choice_ids=["A"])
    value = submit(space, practice, blank_confirmed=True).json()
    assert value["choice_sample_count"] == 1
    assert value["choice_correct_rate"] == "1.0000"
    assert value["unanswered_count"] == 2


def test_all_blank_choices_have_no_accuracy_sample(space):
    sid, _point, _ids, _document = seed_bank(space, history=False, count=3)
    practice = start_practice(space, sid)
    value = submit(space, practice, blank_confirmed=True).json()
    assert value["choice_sample_count"] == 0
    assert value["choice_correct_rate"] is None
    assert value["group_score_rate"] == "0.0000"


def test_accept_rejects_a_superseded_valid_regrade(space):
    from uuid import UUID

    from backend.app.services.personal_grading import grade_practice

    sid, _point, _ids, _document = seed_bank(space, history=False, count=3)
    practice = start_practice(space, sid)
    single = item_of(practice, "single_choice")
    answer(space, practice, single, choice_ids=["A"])
    submit(space, practice, blank_confirmed=True)
    raised = space.client.post(
        f"/api/learning/practices/{practice['id']}/disputes",
        json={"item_id": single["id"], "reason": "Review the answer"},
    ).json()
    space.client.post(f"/api/learning/disputes/{raised['id']}/regrade", json={}).raise_for_status()
    older = next(row for row in attempts(space, practice) if row.round == 2)
    with space.subjects.sessions.begin() as session:
        grade_practice(session, practice_id=UUID(practice["id"]), blank_confirmed=True,
                       regrade=True, item_ids=(UUID(single["id"]),))
    rejected = space.client.post(f"/api/learning/disputes/{raised['id']}/accept",
                                json={"attempt_id": str(older.id)})
    assert rejected.status_code == 409, rejected.text
    value = space.client.get(f"/api/learning/disputes/{raised['id']}").json()
    assert value["state"] != "resolved"


def test_repeated_regrade_request_keeps_the_same_round(space):
    sid, _point, _ids, _document = seed_bank(space, history=False, count=3)
    practice = start_practice(space, sid)
    single = item_of(practice, "single_choice")
    answer(space, practice, single, choice_ids=["B"])
    submit(space, practice, blank_confirmed=True)
    raised = space.client.post(
        f"/api/learning/practices/{practice['id']}/disputes",
        json={"item_id": single["id"], "reason": "Review the answer"},
    ).json()
    url = f"/api/learning/disputes/{raised['id']}/regrade"
    first = space.client.post(url, json={})
    assert first.status_code == 200, first.text
    before = [(row.id, row.round) for row in attempts(space, practice)]
    second = space.client.post(url, json={})
    assert second.status_code == 200, second.text
    assert [(row.id, row.round) for row in attempts(space, practice)] == before


def test_explicit_corrected_revision_regrades_without_changing_practice_history(space):
    from uuid import UUID, uuid4

    from backend.app.models.practice import PracticeItem
    from backend.app.models.question import Question
    from backend.app.models.question_revision import QuestionRevision

    sid, _point, _ids, _document = seed_bank(space, history=False, count=3)
    practice = start_practice(space, sid)
    single = item_of(practice, "single_choice")
    answer(space, practice, single, choice_ids=["B"])
    submit(space, practice, blank_confirmed=True)
    raised = space.client.post(f"/api/learning/practices/{practice['id']}/disputes",
        json={"item_id": single["id"], "reason": "Reference answer corrected"}).json()
    with space.subjects.sessions.begin() as session:
        item = session.get(PracticeItem, UUID(single["id"]))
        frozen = dict(item.scoring_input)
        question = session.get(Question, item.question_id)
        old = session.get(QuestionRevision, item.question_revision_id)
        revision = QuestionRevision(id=uuid4(), question_id=question.id, subject_id=old.subject_id,
            revision=old.revision + 1, question_type=old.question_type, stem_blocks=old.stem_blocks,
            options=old.options, reference_answer="B", scoring_rubric=old.scoring_rubric,
            explanation_blocks=old.explanation_blocks, grounding=old.grounding,
            grounding_refs=old.grounding_refs, admission_state="eligible", default_score=old.default_score)
        session.add(revision); session.flush()
        question.current_revision_id = revision.id
        revision_id = str(revision.id)
    body = {"revision_id": revision_id, "idempotency_key": uuid4().hex}
    response = space.client.post(f"/api/learning/disputes/{raised['id']}/regrade", json=body)
    assert response.status_code == 200, response.text
    report = space.client.get(f"/api/learning/practices/{practice['id']}/results").json()
    result = next(row for row in report["items"] if row["item_id"] == single["id"])
    assert result["score"] == "8.00"
    assert result["reference_answer"] == "B"
    assert [row["reference_answer"] for row in result["history"]] == ["A", "B"]
    assert result["history"][-1]["question_revision_id"] == revision_id
    with space.subjects.sessions() as session:
        assert session.get(PracticeItem, UUID(single["id"])).scoring_input == frozen
    before = {row.id for row in attempts(space, practice)}
    space.client.post(f"/api/learning/disputes/{raised['id']}/regrade", json=body).raise_for_status()
    assert {row.id for row in attempts(space, practice)} == before
