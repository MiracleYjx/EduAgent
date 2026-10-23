"""Real PG projection: accepted rounds, canonical identities and real sources."""

from uuid import UUID, uuid4

from sqlalchemy import select

from backend.app.models.document_chunk import DocumentChunk
from backend.app.models.practice import GradeAttempt
from backend.app.models.question import Question
from backend.app.models.score_dispute import ScoreDispute
from backend.app.services.practice_service import PracticeService
from tests.integration import test_learning_material_versions as materials
from tests.integration.test_personal_practice_flow import add_history, seed_bank

space = materials.space


def diagnosis(space, sid, query=""):
    response = space.client.get(f"/api/learning/subjects/{sid}/diagnosis{query}")
    assert response.status_code == 200, response.text
    return response.json()


def test_valid_zero_unanswered_failed_and_unresolved_do_not_share_a_denominator(space):
    sid, point, ids, _ = seed_bank(space, history=False, count=5)
    zero = add_history(space, sid, ids[0], score=0)
    add_history(space, sid, ids[1], answered=False, score=0)
    failed = add_history(space, sid, ids[2], score=0)
    disputed = add_history(space, sid, ids[3], score=0)
    with space.subjects.sessions.begin() as session:
        grade = session.scalar(
            select(GradeAttempt).where(GradeAttempt.answer_id == failed)
        )
        session.add(
            GradeAttempt(
                id=uuid4(),
                answer_id=failed,
                round=2,
                validation_state="failed",
                execution_source="local_objective",
                score=None,
                max_score=10,
                reason="synthetic failure",
                scoring_input=grade.scoring_input,
            )
        )
        grade = session.scalar(
            select(GradeAttempt).where(GradeAttempt.answer_id == disputed)
        )
        session.add(
            ScoreDispute(
                id=uuid4(),
                answer_id=disputed,
                disputed_attempt_id=grade.id,
                reason="test",
            )
        )
    result = diagnosis(space, sid)
    stats = result["statistics"]
    assert (
        stats["valid_count"] == 1
        and stats["unanswered_count"] == 1
        and stats["excluded_count"] == 2
    )
    assert stats["first"]["score_rate"] == "0" and stats["first"]["count"] == 1
    assert stats["exclusion_counts"] == {"failed": 1, "disputed": 1}
    assert (
        result["knowledge_points"][0]["statistics"]["status"] == "insufficient_evidence"
    )
    assert str(zero) in {
        r["answer_id"] for r in result["evidence"] if r["exclusion"] is None
    }
    assert result["knowledge_points"][0]["suggestion"]["sources"] == []
    assert result["knowledge_points"][0]["suggestion"]["practice_conditions"][
        "knowledge_point_ids"
    ] == [str(point)]


def test_unanswered_later_first_repractice_merge_and_regrade_do_not_refresh_time(space):
    sid, _, ids, _ = seed_bank(space, history=False, count=3)
    add_history(space, sid, ids[0], answered=False, score=0)
    first = add_history(space, sid, ids[0], score=0)
    add_history(space, sid, ids[1], score=10)
    before = diagnosis(space, sid)
    stamp = next(
        e["submitted_at"] for e in before["evidence"] if e["answer_id"] == str(first)
    )
    with space.subjects.sessions.begin() as session:
        alias = session.get(Question, ids[1])
        alias.canonical_id = ids[0]
        old = session.scalar(
            select(GradeAttempt).where(GradeAttempt.answer_id == first)
        )
        new = GradeAttempt(
            id=uuid4(),
            answer_id=first,
            round=2,
            score=10,
            max_score=10,
            reason="resolved correction",
            validation_state="valid",
            execution_source="local_objective",
            scoring_input=old.scoring_input,
        )
        session.add(new)
        session.flush()
        dispute = ScoreDispute(
            id=uuid4(),
            answer_id=first,
            disputed_attempt_id=old.id,
            reason="test",
            state="awaiting_acceptance",
        )
        session.add(dispute)
        session.flush()
        did = dispute.id
        newid = new.id
    pending = diagnosis(space, sid)
    assert (
        pending["statistics"]["valid_count"] == 1
        and pending["statistics"]["excluded_count"] == 1
    )
    with space.subjects.sessions.begin() as session:
        dispute = session.get(ScoreDispute, did)
        dispute.state = "resolved"
        dispute.accepted_attempt_id = newid
    after = diagnosis(space, sid)
    assert (
        after["statistics"]["first"]["count"] == 1
        and after["statistics"]["repractice"]["count"] == 1
    )
    evidence = next(e for e in after["evidence"] if e["answer_id"] == str(first))
    assert evidence["submitted_at"] == stamp and evidence["attempt_id"] == str(newid)
    with space.subjects.sessions() as session:
        assert (
            len(
                list(
                    session.scalars(
                        select(GradeAttempt).where(GradeAttempt.answer_id == first)
                    )
                )
            )
            == 2
        )


def test_recent_twenty_changes_selection_weakness_and_preserves_difficulty(space):
    sid, point, ids, _ = seed_bank(space, history=False, count=40)
    for n, qid in enumerate(ids):
        add_history(space, sid, qid, score=0 if n < 20 else 10)
    result = diagnosis(space, sid)
    stats = result["knowledge_points"][0]["statistics"]
    assert stats["recent"] == {
        "count": 20,
        "score_rate": "1",
        "choice_count": 13,
        "choice_correct_rate": "1",
    }
    assert stats["previous"]["count"] == 20 and stats["previous"]["score_rate"] == "0"
    assert stats["status"] == "observed"
    with space.subjects.sessions() as session:
        assert point not in PracticeService(space.subjects)._history(session, sid)[2]


def test_real_confirmed_material_reference_excludes_pending_and_other_subject(space):
    sid, point, ids, _ = seed_bank(space, history=False, count=1)
    add_history(space, sid, ids[0], score=0)
    doc = materials.upload(
        space, str(sid), "review.md", b"# Review\n\n2 plus 2 equals 4."
    ).json()
    assert diagnosis(space, sid)["knowledge_points"][0]["suggestion"]["sources"] == []
    with space.subjects.sessions.begin() as session:
        chunk = session.scalar(
            select(DocumentChunk).where(DocumentChunk.document_id == UUID(doc["id"]))
        )
        chunk.chunk_metadata = {
            **chunk.chunk_metadata,
            "knowledge_points": [str(point)],
            "scope_confirmation": {"knowledge_points": {str(point): "confirmed"}},
        }
        chunkid = chunk.id
    value = diagnosis(space, sid)["knowledge_points"][0]["suggestion"]
    assert len(value["sources"]) == 1 and value["sources"][0]["chunk_id"] == str(
        chunkid
    )
    assert value["sources"][0]["location"]["anchor"] in {
        b["anchor"] for b in doc["blocks"]
    }
    opened = space.client.get(f"/api/learning/materials/{doc['id']}")
    assert opened.status_code == 200, opened.text
    other = materials.subject(space, "Other subject")
    assert diagnosis(space, other)["statistics"]["valid_count"] == 0
    assert (
        space.client.get(f"/api/learning/subjects/{uuid4()}/diagnosis").status_code
        == 404
    )
    assert (
        space.client.get(
            f"/api/learning/subjects/{sid}/diagnosis?source_kind=untrusted"
        ).status_code
        == 422
    )
