"""T177: real review writes reject changed inputs and unknown stored identity (TCR 32)."""

from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.domain.enums import UserRole
from backend.app.models import GradingResult, ReviewRecord
from backend.app.services.review_service import ReviewServiceError
from tests.unit.services.test_review_service import (
    PENDING_REVIEW,
    THREAD_ID,
    WORKFLOW_ID,
    _decision_payload,
    _pause_run,
    _run,
    _saver,
    _service,
    _store,
    _StubGradingAgent,
    _subjective_answer_ids,
)
from tests.unit.services.test_review_service import (
    env as env,  # noqa: PLC0414 - explicit shared pytest fixture re-export
)


@pytest.mark.parametrize("action", ["Confirmed", "Modified", "Re-grade"])
@pytest.mark.parametrize(
    "changed",
    ["checkpoint-revision", "result-link", "stored-unknown-link", "stored-max"],
)
def test_all_teacher_actions_reject_changed_or_unknown_actual_scoring_facts(
    env, action, changed
):
    answer_id = _subjective_answer_ids(env)[0]
    workflow, current, _ = _pause_run(
        env,
        agent=_StubGradingAgent(
            submission_id=str(env.paper.submission_id),
            low_confidence_answer_ids={answer_id},
        ),
        saver=_saver(env),
    )
    original = current["grading_results"][answer_id]
    revision = original if action == "Modified" else None
    if changed == "checkpoint-revision":
        fixed = current["scoring_inputs"][answer_id]
        altered = fixed.model_copy(
            update={
                "question_validation_revision": fixed.question_validation_revision + 1
            }
        )
        current["scoring_inputs"] = {**current["scoring_inputs"], answer_id: altered}
        current["scoring_input"] = altered
        _store(env).save_checkpoint(
            WORKFLOW_ID,
            current,
            PENDING_REVIEW,
            pause_reason=current["pause_reason"],
            thread_id=THREAD_ID,
        )
    elif changed == "result-link":
        current["grading_results"] = {
            **current["grading_results"],
            answer_id: original.model_copy(update={"exam_question_id": "another-link"}),
        }
        _store(env).save_checkpoint(
            WORKFLOW_ID,
            current,
            PENDING_REVIEW,
            pause_reason=current["pause_reason"],
            thread_id=THREAD_ID,
        )
    else:
        with Session(env.engine) as session:
            row = session.scalars(
                select(GradingResult).where(GradingResult.answer_id == UUID(answer_id))
            ).one()
            if changed == "stored-unknown-link":
                row.exam_question_id = None
            else:
                row.max_score = row.max_score + 1
            session.commit()
    service = _service(env, workflow=workflow)
    decision = _decision_payload(
        env, answer_id=answer_id, review_status=action, revised_result=revision
    )
    with pytest.raises(ReviewServiceError) as rejected:
        _run(
            service.submit_decision_async(
                decision,
                actor_id=str(env.paper.teacher_id),
                actor_role=UserRole.TEACHER,
            )
        )
    expected = (
        "GRADING_RESULT_OWNERSHIP_MISMATCH"
        if changed == "checkpoint-revision"
        else "REVIEW_SERVICE_IDENTITY_MISMATCH"
    )
    assert rejected.value.error_code == expected
    with Session(env.engine) as session:
        assert not session.scalars(select(ReviewRecord)).all()
        row = session.scalars(
            select(GradingResult).where(GradingResult.answer_id == UUID(answer_id))
        ).one()
        assert row.review_status.value == "Pending Review"
