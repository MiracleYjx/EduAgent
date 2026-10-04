"""T176 fixed scoring checkpoint state and restoration (TCR §31)."""

import pytest

from backend.app.ai.agents.state import AGENT_STATE_VERSION
from backend.app.ai.workflows.grading_workflow import cleared_slots
from backend.app.ai.workflows.state import (
    ANSWER_SLOT_FIELDS,
    SUBMISSION_COLLECTION_FIELDS,
    WORKFLOW_STATE_PAYLOAD_KIND,
    WORKFLOW_STATE_PAYLOAD_VERSION,
    WorkflowStateError,
    workflow_state_from_checkpoint_payload,
    workflow_state_to_checkpoint_payload,
)


def identity():
    return {
        "workflow_id": "workflow-controlled",
        "request_id": "request-controlled",
        "submission_id": "submission-controlled",
    }


def test_new_payload_and_agent_versions_register_fixed_input_fields():
    assert AGENT_STATE_VERSION == "2"
    assert WORKFLOW_STATE_PAYLOAD_VERSION == "2"
    assert "scoring_input" in ANSWER_SLOT_FIELDS
    assert "scoring_inputs" in SUBMISSION_COLLECTION_FIELDS
    assert workflow_state_to_checkpoint_payload(identity())["version"] == "2"


def test_current_slot_is_cleared_without_erasing_submission_inputs():
    cleared = cleared_slots()
    assert "scoring_input" in cleared and cleared["scoring_input"] is None
    assert "scoring_inputs" not in cleared


def test_known_v1_payload_preserves_missing_fixed_facts_without_invention():
    restored = workflow_state_from_checkpoint_payload(
        {"kind": WORKFLOW_STATE_PAYLOAD_KIND, "version": "1", "state": identity()}
    )
    assert restored == identity()
    assert "scoring_input" not in restored and "scoring_inputs" not in restored


def test_v1_cannot_claim_fields_from_new_fixed_input_contract():
    with pytest.raises(WorkflowStateError):
        workflow_state_from_checkpoint_payload(
            {
                "kind": WORKFLOW_STATE_PAYLOAD_KIND,
                "version": "1",
                "state": {**identity(), "scoring_inputs": {}},
            }
        )


@pytest.mark.parametrize("version", ["3", "unknown", 2, None])
def test_unknown_payload_versions_remain_rejected(version):
    with pytest.raises(WorkflowStateError):
        workflow_state_from_checkpoint_payload(
            {
                "kind": WORKFLOW_STATE_PAYLOAD_KIND,
                "version": version,
                "state": identity(),
            }
        )


def scoring(**changes):
    from backend.app.schemas.grading import ScoringInput

    values = {
        "exam_id": "exam-controlled",
        "exam_question_id": "link-controlled",
        "question_id": "question-controlled",
        "submission_id": "submission-controlled",
        "answer_id": "answer-controlled",
        "student_id": "student-controlled",
        "course_id": "course-controlled",
        "order": 1,
        "question_validation_revision": 1,
        "question_type": "SINGLE_CHOICE",
        "question_content": "Controlled choice",
        "options": {"C": "Third", "A": "First", "B": "Second"},
        "order_preserved": True,
        "reference_answer": "A",
        "source_rubric": "Correct choice receives two points.",
        "effective_score": "2.00",
        "base_score": "2.00",
        "scoring_basis": {
            "kind": "objective",
            "points": [
                {
                    "key": "answer",
                    "label": "Correct answer",
                    "base_points": "2.00",
                    "default_points": "2.00",
                    "confirmed_points": "2.00",
                }
            ],
            "additive": True,
            "rounding_delta": "0.00",
        },
        "published_knowledge_points": ["Fixed K"],
        "student_answer": "A",
        "assets": [],
    }
    values.update(changes)
    return ScoringInput.model_validate(values)


def snapshot(fixed):
    from backend.app.services.grading.grading_task_service import (
        GradingTargetAnswer,
        SubmissionSnapshot,
    )

    target = GradingTargetAnswer(
        order=fixed.order,
        answer_id=fixed.answer_id,
        question_id=fixed.question_id,
        question_type=fixed.question_type,
        max_score=fixed.effective_score,
        knowledge_points=tuple(fixed.published_knowledge_points),
        content=fixed.question_content,
        reference_answer=fixed.reference_answer,
        scoring_rubric=fixed.source_rubric,
        student_answer=fixed.student_answer,
        scoring_input=fixed,
    )
    return SubmissionSnapshot(
        submission_id=fixed.submission_id,
        exam_id=fixed.exam_id,
        student_id=fixed.student_id,
        course_id=fixed.course_id,
        status="Submitted",
        answers=(target,),
        scoring_basis_error=None,
    )


def state(fixed):
    return {
        **identity(),
        "submission_context": snapshot(fixed).to_context(),
        "current_answer_id": fixed.answer_id,
        "current_answer_order": fixed.order,
        "scoring_input": fixed.model_copy(deep=True),
        "scoring_inputs": {fixed.answer_id: fixed.model_copy(deep=True)},
    }


def graph(fixed, agent=None, checkpointer=None):
    from backend.app.ai.agents.grading_agent import GradingAgent
    from backend.app.ai.workflows.grading_workflow import (
        GradingWorkflow,
        GradingWorkflowDeps,
    )

    return GradingWorkflow(
        GradingWorkflowDeps(snapshot=snapshot(fixed), agent=agent or GradingAgent()),
        checkpointer=checkpointer,
    )


def test_full_fixed_inputs_round_trip_preserves_options_and_decimal_facts():
    fixed = scoring()
    encoded = workflow_state_to_checkpoint_payload(state(fixed))
    assert "options" not in encoded["state"]["scoring_input"]
    assert encoded["state"]["scoring_input"]["ordered_options"] == [
        ["C", "Third"],
        ["A", "First"],
        ["B", "Second"],
    ]
    restored = workflow_state_from_checkpoint_payload(encoded)
    assert list(restored["scoring_input"].options) == ["C", "A", "B"]
    assert restored["scoring_input"].effective_score == fixed.effective_score
    assert restored["scoring_inputs"][fixed.answer_id] == fixed


@pytest.mark.parametrize(
    "mutation", ["answer-key", "submission", "exam", "student", "slot"]
)
def test_checkpoint_rejects_cross_context_fixed_inputs(mutation):
    fixed = scoring()
    raw = state(fixed)
    changed = fixed.model_copy(update={"exam_id": "other-exam"})
    if mutation == "answer-key":
        raw["scoring_inputs"] = {"other-answer": fixed}
    elif mutation == "slot":
        raw["scoring_input"] = changed
    else:
        raw["scoring_inputs"] = {
            fixed.answer_id: fixed.model_copy(
                update={mutation + "_id": "other-context"}
            )
        }
    with pytest.raises(WorkflowStateError):
        workflow_state_to_checkpoint_payload(raw)


def test_load_and_classify_save_actual_inputs_and_isolate_current_slot():
    import asyncio

    fixed = scoring()
    workflow = graph(fixed)
    loaded = asyncio.run(workflow._node_load_submission(identity()))
    assert loaded["scoring_inputs"][fixed.answer_id] == fixed
    assert loaded["scoring_inputs"][fixed.answer_id] is not fixed
    classified = asyncio.run(workflow._node_classify_question({**identity(), **loaded}))
    assert classified["scoring_input"] == fixed
    assert classified["scoring_input"] is not fixed
    assert "scoring_inputs" not in cleared_slots()


@pytest.mark.parametrize("phase", ["grade", "regrade"])
def test_each_grade_and_regrade_rejects_changed_link_before_call(phase):
    import asyncio

    fixed = scoring()
    workflow = graph(fixed)
    raw = state(fixed)
    raw["scoring_input"] = fixed.model_copy(
        update={"exam_question_id": "other-association"}
    )
    if phase == "grade":
        result = asyncio.run(
            workflow._grade_current(raw, current_node="objective_rule_grade")
        )
    else:
        result = asyncio.run(workflow._node_regrade(raw))
    assert result["error"].error_code == "GRADING_RESULT_OWNERSHIP_MISMATCH"
    assert result["status"].value == "Failed"


def test_missing_v1_fixed_context_and_option_reordering_cannot_resume_current_input():
    from backend.app.ai.workflows.grading_workflow import GradingWorkflowError

    fixed = scoring()
    workflow = graph(fixed)
    with pytest.raises(GradingWorkflowError) as caught:
        workflow._require_fixed_context(identity())
    assert caught.value.error_code == "GRADING_RESULT_OWNERSHIP_MISMATCH"
    raw = state(fixed)
    raw["scoring_inputs"][fixed.answer_id] = fixed.model_copy(
        update={"options": {"A": "First", "B": "Second", "C": "Third"}}
    )
    with pytest.raises(GradingWorkflowError):
        workflow._require_fixed_context(raw)


def test_actual_grading_agent_transmits_same_fixed_input_in_invocation():
    import asyncio

    from backend.app.ai.agents.grading_agent import GradingAgent
    from backend.app.schemas.grading import same_fixed_scoring_input

    fixed = scoring()
    current = snapshot(fixed)
    invocation = asyncio.run(
        GradingAgent().grade_answer_async(
            current,
            current.answers[0],
            request_id="request-controlled",
            workflow_id="workflow-controlled",
        )
    )
    assert invocation.input is not None
    assert invocation.input.answer_id == fixed.answer_id
    assert same_fixed_scoring_input(invocation.input.scoring_input, fixed)
    assert invocation.output.status.value == "success"


def test_api_reconstruction_checks_fixed_inputs_before_building_workflow():
    from types import SimpleNamespace

    from backend.app.api.workflow import WorkflowRunError, WorkflowService

    fixed = scoring()
    service = object.__new__(WorkflowService)
    service._reader = SimpleNamespace(load=lambda _: snapshot(fixed))
    service._checkpointer = lambda: pytest.fail(
        "Must reject before building the workflow."
    )
    run = SimpleNamespace(submission_id=fixed.submission_id)
    with pytest.raises(WorkflowRunError) as caught:
        service.workflow_for_run(run, identity())
    assert caught.value.error_code == "GRADING_RESULT_OWNERSHIP_MISMATCH"


@pytest.mark.parametrize("checkpoint_mode", ["v1-missing", "other-exam", "other-link"])
def test_actual_memory_checkpoint_resume_checks_fixed_facts_before_any_agent(
    checkpoint_mode,
):
    import asyncio

    from langgraph.checkpoint.memory import InMemorySaver

    from backend.app.ai.workflows.grading_workflow import GradingWorkflowError

    class NoCalls:
        async def grade_answer_async(self, *args, **kwargs):
            pytest.fail("Mismatch must stop before the Agent.")

    fixed = scoring()
    saved = identity() if checkpoint_mode == "v1-missing" else state(fixed)
    if checkpoint_mode == "other-exam":
        changed = fixed.model_copy(update={"exam_id": "other-exam"})
        saved = state(changed)
    elif checkpoint_mode == "other-link":
        changed = fixed.model_copy(update={"exam_question_id": "other-link"})
        saved = state(changed)
    checkpoint = InMemorySaver()
    writer = graph(fixed, agent=NoCalls(), checkpointer=checkpoint)
    writer._compiled.update_state(
        {"configurable": {"thread_id": "fixed-thread"}}, saved
    )
    restored = graph(fixed, agent=NoCalls(), checkpointer=checkpoint)
    with pytest.raises(GradingWorkflowError) as caught:
        asyncio.run(restored.resume_async(thread_id="fixed-thread"))
    assert caught.value.error_code == "GRADING_RESULT_OWNERSHIP_MISMATCH"


@pytest.mark.parametrize(
    "error_code", ["EXAM_SCORING_BASIS_MISSING", "EXAM_SCORING_INPUT_NOT_SUPPORTED"]
)
def test_direct_agent_preserves_real_scoring_gate_before_any_grader(error_code):
    import asyncio
    from dataclasses import replace

    from backend.app.ai.agents.grading_agent import GradingAgent

    class NoGrader:
        def grade(self, **kwargs):
            pytest.fail("Missing or unsupported basis must stop before grading.")

    current = replace(snapshot(scoring()), scoring_basis_error=error_code)
    invocation = asyncio.run(
        GradingAgent(objective_grader=NoGrader()).grade_answer_async(
            current, current.answers[0], request_id="request-controlled"
        )
    )
    assert invocation.output.status.value == "failure"
    assert invocation.output.error.error_code == error_code


def test_direct_agent_unknown_historical_max_retains_original_failure_and_no_envelope():
    import asyncio
    from dataclasses import replace

    from backend.app.ai.agents.grading_agent import GradingAgent

    current = snapshot(scoring())
    target = replace(current.answers[0], max_score=None, scoring_input=None)
    current = replace(
        current, answers=(target,), scoring_basis_error="EXAM_SCORING_BASIS_MISSING"
    )
    invocation = asyncio.run(
        GradingAgent().grade_answer_async(
            current, target, request_id="request-controlled"
        )
    )
    assert invocation.output.status.value == "failure"
    assert invocation.output.error.error_code == "EXAM_SCORING_BASIS_MISSING"
    assert invocation.input is None


@pytest.mark.parametrize("envelope", ["missing", "changed-link"])
def test_workflow_rejects_agent_that_drops_or_changes_the_actual_fixed_envelope(
    envelope,
):
    import asyncio
    from dataclasses import replace

    from backend.app.ai.agents.grading_agent import GradingAgent

    fixed = scoring()

    class AlteredEnvelope:
        async def grade_answer_async(self, snap, target, **kwargs):
            invocation = await GradingAgent().grade_answer_async(snap, target, **kwargs)
            if envelope == "missing":
                return replace(invocation, input=None)
            changed = fixed.model_copy(update={"exam_question_id": "different-link"})
            return replace(
                invocation,
                input=invocation.input.model_copy(update={"scoring_input": changed}),
            )

    result = asyncio.run(
        graph(fixed, agent=AlteredEnvelope())._grade_current(
            state(fixed), current_node="objective_rule_grade"
        )
    )
    assert result["status"].value == "Failed"
    assert result["error"].error_code == "GRADING_RESULT_OWNERSHIP_MISMATCH"
    assert "grading_result" not in result


@pytest.mark.parametrize("checkpoint_mode", ["v1-missing", "changed-link"])
def test_actual_api_resume_rejects_stale_input_before_teacher_or_runtime_resume(
    checkpoint_mode,
):
    import asyncio
    from types import SimpleNamespace

    from backend.app.api.workflow import WorkflowRunError, WorkflowService

    fixed = scoring()
    saved = (
        identity()
        if checkpoint_mode == "v1-missing"
        else state(fixed.model_copy(update={"exam_question_id": "other-link"}))
    )
    service = object.__new__(WorkflowService)
    service._ensure_ready = lambda: None
    service._outcome_repository = lambda: None
    row = SimpleNamespace(
        submission_id=fixed.submission_id,
        workflow_id="workflow-controlled",
        checkpoint={"thread_id": "fixed-thread"},
        resumable=True,
    )
    service._require_run = lambda _: row
    service._load_for_teacher = lambda *_: snapshot(fixed)
    service._checkpoints = SimpleNamespace(
        restore_state=lambda _: saved,
        runtime_ready=lambda *_: pytest.fail("Must reject before runtime recovery."),
    )

    async def no_teacher_resume(**kwargs):
        pytest.fail("Must reject before accepting a teacher decision.")

    service._resume_with_teacher_decision = no_teacher_resume
    with pytest.raises(WorkflowRunError) as caught:
        asyncio.run(
            service.resume_run(
                workflow_id=row.workflow_id, actor_id="teacher-controlled"
            )
        )
    assert caught.value.error_code == "GRADING_RESULT_OWNERSHIP_MISMATCH"


@pytest.mark.parametrize(
    "mutation", ["other-answer", "changed-link", "changed-content"]
)
def test_direct_agent_rejects_foreign_or_changed_fixed_target_before_grader(mutation):
    import asyncio
    from dataclasses import replace

    from backend.app.ai.agents.grading_agent import GradingAgent

    class NoGrader:
        def grade(self, **kwargs):
            pytest.fail("Foreign fixed input must be rejected before grading.")

    fixed = scoring()
    current = snapshot(fixed)
    update = (
        {"answer_id": "other-answer"}
        if mutation == "other-answer"
        else {"exam_question_id": "other-link"} if mutation == "changed-link" else {}
    )
    changed = fixed.model_copy(update=update)
    target = replace(
        current.answers[0], answer_id=changed.answer_id, scoring_input=changed
    )
    if mutation == "changed-content":
        target = replace(target, content="Unrecorded content")
    invocation = asyncio.run(
        GradingAgent(objective_grader=NoGrader()).grade_answer_async(
            current, target, request_id="request-controlled"
        )
    )
    assert invocation.output.status.value == "failure"
    assert invocation.output.error.error_code == "GRADING_RESULT_OWNERSHIP_MISMATCH"
