"""T171 execution boundaries preserve readable historical facts and reject writes."""

import asyncio
from copy import deepcopy
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.api import grading, reviews, workflow
from backend.app.api.reviews import ReviewQueryService
from backend.app.core.retry_policy import ProviderErrorInfo, ProviderExecutionError
from backend.app.domain.enums import ReviewStatus, UserRole, WorkflowStatus
from backend.app.models import (
    AuditLog,
    DiagnosisReport,
    ExamQuestion,
    ExamResult,
    GradingResult,
    ReviewRecord,
    Submission,
    WorkflowRun,
)
from backend.app.services.grading.grading_task_service import (
    GradingNotAllowedError,
    SubmissionSnapshot,
)
from backend.app.services.review_service import ReviewServiceError
from tests.contract import test_workflow_api_contract as workflow_cases
from tests.unit.services import test_review_service as review_cases

workflow_env = workflow_cases.env
review_env = review_cases.env


def _remove_known_basis(engine, exam_id):
    with Session(engine) as session:
        link = session.scalars(
            select(ExamQuestion)
            .where(ExamQuestion.exam_id == UUID(str(exam_id)))
            .order_by(ExamQuestion.order_index)
        ).first()
        assert link is not None and link.scoring_basis is not None
        link.scoring_basis = None
        session.commit()


def _stored_facts(engine):
    with Session(engine) as session:
        return deepcopy(
            [
                list(
                    session.execute(
                        select(model.__table__).order_by(model.id)
                    ).mappings()
                )
                for model in (
                    WorkflowRun,
                    GradingResult,
                    ReviewRecord,
                    ExamResult,
                    Submission,
                    AuditLog,
                    DiagnosisReport,
                )
            ]
        )


def test_workflow_start_refuses_unknown_history_before_new_run_or_grading(workflow_env):
    env = workflow_env
    agent = workflow_cases._StubGradingAgent(
        submission_id=str(env["fixture"].submission_id)
    )
    service = workflow_cases._service(env, agent=agent)
    _remove_known_basis(env["engine"], env["fixture"].exam_id)
    before = _stored_facts(env["engine"])
    with pytest.raises(workflow.WorkflowRunError) as raised:
        asyncio.run(
            service.start_run(
                submission_id=str(env["fixture"].submission_id),
                actor_id=str(env["fixture"].teacher_id),
                request_id="t171-unknown-start",
            )
        )
    assert raised.value.error_code == "EXAM_SCORING_BASIS_MISSING"
    assert agent.calls == []
    assert _stored_facts(env["engine"]) == before


@pytest.mark.parametrize("entry", ["workflow_for_run", "resume_run"])
def test_reconstruct_or_resume_refuses_unknown_history_but_state_stays_readable(
    workflow_env, entry
):
    env = workflow_env
    agent = workflow_cases._AcceptedSubjectiveAgent(
        submission_id=str(env["fixture"].submission_id)
    )
    failure = ProviderExecutionError(
        ProviderErrorInfo(
            code="ProviderTimeout",
            message="Controlled diagnosis timeout",
            attempt_count=1,
            retryable=True,
            status="ProviderTimeout",
        )
    )
    diagnosis = workflow_cases._FailingDiagnosisService(failure)
    service = workflow_cases._service(
        env,
        agent=agent,
        diagnosis_service=workflow_cases._diagnosis_adapter(env, diagnosis),
    )
    started = asyncio.run(
        service.start_run(
            submission_id=str(env["fixture"].submission_id),
            actor_id=str(env["fixture"].teacher_id),
            request_id="t171-persisted-pause",
        )
    )
    assert started.status is WorkflowStatus.PAUSED
    run = workflow_cases._runs(env)[0]
    assert run.resumable
    assert workflow_cases._store(env).runtime_ready(run.workflow_id, started.thread_id)
    _remove_known_basis(env["engine"], env["fixture"].exam_id)
    before = _stored_facts(env["engine"])
    calls = list(agent.calls)
    diagnosis_calls = len(diagnosis.calls)
    assert (
        service.get_run(
            workflow_id=run.workflow_id,
            actor_id=str(env["fixture"].teacher_id),
            roles=[UserRole.TEACHER],
        ).status
        is WorkflowStatus.PAUSED
    )
    with pytest.raises(workflow.WorkflowRunError) as raised:
        if entry == "workflow_for_run":
            service.workflow_for_run(run, workflow_cases._store(env).restore_state(run))
        else:
            asyncio.run(
                service.resume_run(
                    workflow_id=run.workflow_id, actor_id=str(env["fixture"].teacher_id)
                )
            )
    assert raised.value.error_code == "EXAM_SCORING_BASIS_MISSING"
    assert agent.calls == calls
    assert len(diagnosis.calls) == diagnosis_calls
    assert _stored_facts(env["engine"]) == before


@pytest.mark.parametrize("action", ["confirm", "modify", "regrade"])
def test_review_actions_refuse_unknown_history_without_rewriting_readable_results(
    review_env, action
):
    env = review_env
    subjectives = review_cases._subjective_answer_ids(env)
    agent = review_cases._StubGradingAgent(
        submission_id=str(env.paper.submission_id),
        low_confidence_answer_ids={subjectives[0]},
    )
    graph, _state, _result = review_cases._pause_run(
        env, agent=agent, saver=review_cases._saver(env)
    )
    target_id = review_cases._paused_answer(env)
    target = next(
        item
        for item in review_cases._snapshot(env).answers
        if item.answer_id == target_id
    )
    revised = (
        review_cases._grading_result(
            target,
            str(env.paper.submission_id),
            score=8.0,
            confidence=0.95,
            review_status=ReviewStatus.NOT_REQUIRED.value,
        )
        if action == "modify"
        else None
    )
    decision = review_cases._decision_payload(
        env,
        answer_id=target_id,
        review_status=(
            ReviewStatus.MODIFIED.value
            if action == "modify"
            else ReviewStatus.CONFIRMED.value
        ),
        revised_result=revised,
    )
    service = review_cases._service(env, workflow=graph)
    stored_grade = review_cases._grading_row(env, target_id)
    _remove_known_basis(env.engine, review_cases._snapshot(env).exam_id)
    before = _stored_facts(env.engine)
    calls = list(agent.calls)
    detail = ReviewQueryService(
        session_factory=lambda: Session(env.engine)
    ).get_answer_detail(env.owner_id, str(env.paper.submission_id), target_id)
    assert (
        detail.score == stored_grade.score
        and detail.max_score == stored_grade.max_score
    )
    with pytest.raises(ReviewServiceError) as raised:
        if action == "regrade":
            service.request_regrade(
                review_cases.WORKFLOW_ID,
                review_cases.THREAD_ID,
                target_id,
                actor_id=env.owner_id,
                actor_role=UserRole.TEACHER,
            )
        else:
            service.submit_decision(
                decision,
                actor_id=env.owner_id,
                actor_role=UserRole.TEACHER,
                comment="Synthetic attempted review",
            )
    assert raised.value.error_code == "EXAM_SCORING_BASIS_MISSING"
    assert agent.calls == calls
    assert _stored_facts(env.engine) == before


@pytest.mark.parametrize(
    "code", ["EXAM_SCORING_BASIS_MISSING", "EXAM_SCORING_INPUT_NOT_SUPPORTED"]
)
@pytest.mark.parametrize("boundary", ["grading", "workflow", "reviews"])
def test_execution_basis_errors_keep_business_code_and_http_conflict(code, boundary):
    snapshot = SubmissionSnapshot(
        submission_id="s",
        exam_id="e",
        student_id="u",
        course_id="c",
        status="Submitted",
        answers=(),
        scoring_basis_error=code,
    )
    with pytest.raises(GradingNotAllowedError) as raised:
        snapshot.require_scoring_ready()
    if boundary == "grading":
        response = grading._grading_http_exception(raised.value)
    elif boundary == "workflow":
        response = workflow._workflow_http_exception(
            workflow._grading_error(raised.value)
        )
    else:
        response = reviews._review_http_exception(
            ReviewServiceError(raised.value.detail, error_code=code)
        )
    assert response.detail["error_code"] == code
    assert response.status_code == 409
