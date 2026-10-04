"""T178 review display keeps database scores and proven saved context (TCR33)."""

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.ai.workflows.state import CHECKPOINT_STATE_KEY
from backend.app.models import GradingResult, WorkflowRun
from tests.integration.test_exam_scoring_consumers import (
    ConfirmedPointProvider,
    environment,  # noqa: F401 -- shared real PostgreSQL fixture
)
from tests.integration.test_langgraph_grading_workflow import (
    _api_client,
    _start,
    _teacher_headers,
)


@pytest.mark.parametrize("change", [None, "foreign_input", "legacy_identity"])
def test_review_display_uses_only_matching_actual_input(
    environment,  # noqa: F811 -- pytest imported fixture
    change,
):
    provider = ConfirmedPointProvider([0.3], score=Decimal("1.005"))
    with _api_client(environment, scoring_provider=provider) as harness:
        started = _start(harness, environment)
        assert started.status_code == 200
        answer_id = environment.paper.subjective_answer_ids[0]
        with Session(environment.engine) as session:
            row = session.scalars(select(GradingResult)).one()
            run = session.scalars(select(WorkflowRun)).one()
            checkpoint = dict(run.checkpoint)
            state = dict(checkpoint[CHECKPOINT_STATE_KEY])
            inputs = dict(state["scoring_inputs"])
            recorded = dict(inputs[answer_id])
            assert recorded["course_context"] and recorded["source_references"]
            if change == "foreign_input":
                recorded["question_content"] = "不同题目的依据"
                inputs[answer_id] = recorded
                state["scoring_inputs"] = inputs
                checkpoint[CHECKPOINT_STATE_KEY] = state
                run.checkpoint = checkpoint
            elif change == "legacy_identity":
                row.exam_question_id = None
            session.commit()
        response = harness.client.get(
            f"/api/reviews/queue/{environment.paper.submission_id}/answers/{answer_id}",
            headers=_teacher_headers(environment),
        )
        assert response.status_code == 200
        data = response.json()
        assert Decimal(data["max_score"]) == Decimal("3.33")
        assert Decimal(data["score"]) == Decimal("1.01")
        assert data["knowledge_points"] == ["发布标签"]
        if change is None:
            assert data["scoring_input_error"] is None
            assert data["scoring_input"]["course_context"] == recorded["course_context"]
            assert (
                data["scoring_input"]["source_references"]
                == recorded["source_references"]
            )
        else:
            assert data["scoring_input_error"]
            if change == "legacy_identity":
                assert data["scoring_input"] is None
                assert data["exam_question_id"] is None
            else:
                assert data["scoring_input"]["course_context"] is None
                assert (
                    data["scoring_input"]["question_content"]
                    != recorded["question_content"]
                )


@pytest.mark.parametrize("resume_fails", [False, True])
def test_saved_model_context_survives_real_manual_score_change(
    environment,  # noqa: F811 -- imported pytest PostgreSQL fixture
    monkeypatch,
    resume_fails,
):
    from backend.app.ai.workflows.grading_workflow import GradingWorkflow
    from tests.integration.test_langgraph_grading_workflow import _confirm_body

    async def fail_resume(*args, **kwargs):
        raise RuntimeError("T178 controlled resume failure after saved decision")

    with _api_client(
        environment,
        scoring_provider=ConfirmedPointProvider([0.3], score=Decimal("1.005")),
    ) as harness:
        started = _start(harness, environment)
        answer_id = environment.paper.subjective_answer_ids[0]
        url = (
            f"/api/reviews/queue/{environment.paper.submission_id}/answers/{answer_id}"
        )
        before = harness.client.get(url, headers=_teacher_headers(environment)).json()
        actual_context = before["scoring_input"]["course_context"]
        assert actual_context
        if resume_fails:
            monkeypatch.setattr(
                GradingWorkflow, "apply_teacher_decision_async", fail_resume
            )
        body = _confirm_body(environment, started.json()["workflow_id"])
        body.update(
            action="modify",
            score="2.345",
            reason="T178 actual synthetic manual revision",
        )
        saved = harness.client.post(
            "/api/reviews/decisions", headers=_teacher_headers(environment), json=body
        )
        assert saved.status_code == 200 and saved.json()["decision_saved"] is True
        if resume_fails:
            assert saved.json()["resume_status"] == "failed"
        after = harness.client.get(url, headers=_teacher_headers(environment)).json()
        assert Decimal(after["score"]) == Decimal("2.35")
        assert after["reason"] == body["reason"]
        assert after["scoring_input_error"] is None
        assert after["scoring_input"]["course_context"] == actual_context
        assert (
            after["scoring_input"]["source_references"]
            == before["scoring_input"]["source_references"]
        )
