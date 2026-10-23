"""Teacher output is structured and bounded; citations must be actual inputs."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.app.schemas.personal_teacher import TeacherReply, TeacherSend
from backend.app.services.teacher_context import validate_reply


def test_reply_cannot_write_a_score_or_pretend_to_cite_an_unknown_source():
    with pytest.raises(ValidationError):
        TeacherReply(answer_body="explanation", basis="model_knowledge", score=10)
    reply = TeacherReply(
        answer_body="explanation", basis="uploaded_material", citations=["invented"]
    )
    with pytest.raises(ValueError, match="TEACHER_CITATION_INVALID"):
        validate_reply(reply, {"sources": [], "knowledge_points": []})


def test_model_knowledge_does_not_invent_textbook_evidence():
    reply = TeacherReply(
        answer_body="A general explanation",
        basis="model_knowledge",
        uncertainties=["No uploaded evidence"],
    )
    assert validate_reply(reply, {"sources": [], "knowledge_points": []}) == reply
    with pytest.raises(ValueError, match="TEACHER_CITATION_INVALID"):
        validate_reply(
            TeacherReply(answer_body="From the textbook", basis="uploaded_material"),
            {"sources": [], "knowledge_points": []},
        )


def test_actual_citations_and_practice_points_are_whitelisted():
    point = uuid4()
    reply = TeacherReply(
        answer_body="Explanation",
        basis="mixed",
        citations=["chunk-1"],
        practice_knowledge_point_ids=[point],
    )
    assert (
        validate_reply(
            reply,
            {"sources": [{"id": "chunk-1"}], "knowledge_points": [{"id": str(point)}]},
        )
        == reply
    )
    with pytest.raises(ValueError, match="TEACHER_PRACTICE_SCOPE_INVALID"):
        validate_reply(reply, {"sources": [{"id": "chunk-1"}], "knowledge_points": []})


@pytest.mark.parametrize("text", ["", "   ", "a" * 4001])
def test_send_rejects_blank_or_overlarge_input(text):
    with pytest.raises(ValidationError):
        TeacherSend(text=text, idempotency_key=uuid4().hex)
