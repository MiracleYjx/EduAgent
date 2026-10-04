"""T177 fixed Reviewer, Workflow and teacher revisions (TCR 32)."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from backend.app.ai.agents.reviewer_agent import ReviewerAgent
from backend.app.api.reviews import (
    ReviewDecisionScoreError,
    ReviewDecisionService,
    ReviewDetailDTO,
    TeacherDecisionRequest,
)
from backend.app.domain.enums import ReviewStatus
from backend.app.schemas.ai import GradingResult
from tests.unit.agents.test_reviewer_agent import _decision
from tests.unit.workflows.test_fixed_scoring_state import scoring, snapshot, state


def fixed_input():
    return scoring(
        question_type="SHORT_ANSWER",
        effective_score="5.00",
        base_score="10.00",
        options=None,
        source_rubric="Original rubric: Condition X six points, Condition Y four points.",
        scoring_basis={
            "kind": "subjective",
            "additive": True,
            "rounding_delta": "0.00",
            "points": [
                {
                    "key": "x",
                    "label": "Condition X",
                    "base_points": "6.00",
                    "default_points": "3.00",
                    "confirmed_points": "3.00",
                },
                {
                    "key": "y",
                    "label": "Condition Y",
                    "base_points": "4.00",
                    "default_points": "2.00",
                    "confirmed_points": "2.00",
                },
            ],
        },
    )


def result(fixed, **changes):
    raw = {
        "question_type": fixed.question_type,
        "score": "2.50",
        "max_score": fixed.effective_score,
        "reason": "Condition X is present.",
        "correct_points": ["Condition X"],
        "missing_knowledge_points": ["Condition Y"],
        "knowledge_points": fixed.published_knowledge_points,
        "suggestions": ["Review Y."],
        "confidence": 0.9,
        "validation_status": "Validated",
        "review_status": "Not Required",
        "answer_id": fixed.answer_id,
        "submission_id": fixed.submission_id,
        "exam_question_id": fixed.exam_question_id,
    }
    raw.update(changes)
    return GradingResult.model_validate(raw)


def test_reviewer_consumes_fixed_standard_labels_and_published_tags_as_distinct_facts():
    fixed = fixed_input()
    invocation = ReviewerAgent().review(
        result(fixed), _decision(), request_id="review-controlled", scoring_input=fixed
    )
    assert invocation.output.review_outcome.decision.value == "accept"
    assert invocation.input.scoring_input == fixed
    assert invocation.input.grading_result.max_score == Decimal("5.00")


@pytest.mark.parametrize("change", ["link", "max", "tags", "answer"])
def test_reviewer_rejects_result_not_belonging_to_the_fixed_input(change):
    fixed = fixed_input()
    update = {
        "link": {"exam_question_id": "other-link"},
        "max": {"max_score": "10.00"},
        "tags": {"knowledge_points": ["Bank K"]},
        "answer": {"answer_id": "other-answer"},
    }[change]
    invocation = ReviewerAgent().review(
        result(fixed, **update),
        _decision(),
        request_id="review-controlled",
        scoring_input=fixed,
    )
    assert invocation.output.status.value == "failure"
    assert invocation.output.error.error_code == "REVIEWER_RESULT_MISMATCH"


def detail():
    return ReviewDetailDTO(
        submission_id=str(uuid4()),
        answer_id=str(uuid4()),
        course_id="course-controlled",
        exam_id="exam-controlled",
        exam_title="Current exam",
        student_id="student-controlled",
        student_name="Controlled",
        question_id="question-controlled",
        exam_question_id="link-controlled",
        question_number=1,
        question_type="SHORT_ANSWER",
        question_content="Controlled question",
        max_score=Decimal("5.00"),
        student_answer="Student response",
        score=Decimal("2.50"),
        confidence=0.3,
        validation_status="Validated",
        review_status=ReviewStatus.PENDING_REVIEW,
        requires_review=True,
        updated_at=datetime.now(UTC),
    )


@pytest.mark.parametrize("raw,expected", [("0.005", "0.01"), ("4.9999", "5.00")])
def test_teacher_revision_uses_decimal_half_up_with_actual_result_identity(
    raw, expected
):
    current = detail()
    revised = ReviewDecisionService._build_revision(
        current,
        TeacherDecisionRequest(
            submission_id=current.submission_id,
            answer_id=current.answer_id,
            action="modify",
            score=raw,
            reason="Actual correction",
        ),
    )
    assert isinstance(revised.score, Decimal)
    assert revised.score == Decimal(expected)
    assert revised.exam_question_id == current.exam_question_id
    assert revised.max_score == current.max_score


@pytest.mark.parametrize("raw", ["5.00000000000000000001", "-0.00000000000000000001"])
def test_teacher_revision_rejects_raw_out_of_range_before_rounding(raw):
    current = detail()
    with pytest.raises(ReviewDecisionScoreError):
        ReviewDecisionService._build_revision(
            current,
            TeacherDecisionRequest(
                submission_id=current.submission_id,
                answer_id=current.answer_id,
                action="modify",
                score=raw,
                reason="Actual correction",
            ),
        )


def test_workflow_preserves_actual_enriched_agent_input_and_passes_it_to_reviewer():
    import asyncio

    from backend.app.ai.agents.invocation import AgentInvocation
    from backend.app.ai.agents.state import AgentInput, AgentOutput
    from backend.app.ai.workflows.grading_workflow import (
        GradingWorkflow,
        GradingWorkflowDeps,
    )
    from backend.app.schemas.grading import ScoringSourceReference

    fixed = fixed_input()
    enriched = fixed.model_copy(
        update={
            "course_context": "Actually supplied course text",
            "source_references": [
                ScoringSourceReference(
                    chunk_id="actual-chunk",
                    course_id=fixed.course_id,
                    document_id="actual-document",
                )
            ],
        }
    )

    class Producer:
        async def grade_answer_async(self, current, target, **kwargs):
            return AgentInvocation(
                request_id=kwargs["request_id"],
                workflow_id=kwargs["workflow_id"],
                input=AgentInput(
                    agent_type="Grading",
                    request_id=kwargs["request_id"],
                    scoring_input=enriched,
                ),
                output=AgentOutput(
                    agent_type="Grading",
                    status="success",
                    question_type=fixed.question_type,
                    grading_result=result(fixed),
                    confidence_decision=_decision(),
                    confidence=0.9,
                    validation_status="Validated",
                    requires_review=False,
                ),
            )

    class Reviewer(ReviewerAgent):
        def review(self, *args, **kwargs):
            assert kwargs["scoring_input"].course_context == enriched.course_context
            assert (
                kwargs["scoring_input"].source_references == enriched.source_references
            )
            return super().review(*args, **kwargs)

    graph = GradingWorkflow(
        GradingWorkflowDeps(
            snapshot=snapshot(fixed), agent=Producer(), reviewer=Reviewer()
        )
    )
    patch = asyncio.run(
        graph._grade_current(state(fixed), current_node="subjective_retrieve_grade")
    )
    assert patch["scoring_input"] == enriched
    assert patch["scoring_inputs"][fixed.answer_id] == enriched
    checked = asyncio.run(graph._node_reviewer_agent({**state(fixed), **patch}))
    assert checked.get("error") is None


@pytest.mark.parametrize("decision", ["Confirmed", "Modified", "Re-grade"])
def test_workflow_teacher_actions_refuse_current_result_from_another_exam_link(
    decision,
):
    from langgraph.checkpoint.memory import InMemorySaver

    from backend.app.ai.agents.grading_agent import GradingAgent
    from backend.app.ai.workflows.grading_workflow import (
        GradingWorkflow,
        GradingWorkflowDeps,
        GradingWorkflowError,
        TeacherReviewDecision,
    )

    fixed = fixed_input()
    workflow = GradingWorkflow(
        GradingWorkflowDeps(snapshot=snapshot(fixed), agent=GradingAgent()),
        checkpointer=InMemorySaver(),
    )
    current = state(fixed)
    current["grading_results"] = {
        fixed.answer_id: result(
            fixed, exam_question_id="other-link", review_status="Pending Review"
        )
    }
    workflow._compiled.update_state(
        {"configurable": {"thread_id": "fixed-thread"}}, current
    )
    teacher = TeacherReviewDecision(
        workflow_id=current["workflow_id"],
        thread_id="fixed-thread",
        answer_id=fixed.answer_id,
        review_status=decision,
        revised_result=result(fixed) if decision == "Modified" else None,
    )
    with pytest.raises(GradingWorkflowError):
        workflow._apply_teacher_decision_state(teacher)
    assert (
        workflow._compiled.get_state({"configurable": {"thread_id": "fixed-thread"}})
        .values["grading_results"][fixed.answer_id]
        .exam_question_id
        == "other-link"
    )
