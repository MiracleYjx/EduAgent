"""P3.1.1 真实复核事务的轮次校验与跨轮次同内容反例。"""

from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.api.reviews import (
    ReviewDecisionService,
    ReviewDecisionStaleError,
    ReviewQueryService,
    TeacherDecisionRequest,
)
from backend.app.domain.enums import ReviewStatus, UserRole
from backend.app.models import GradingResult
from backend.app.services.review_service import ReviewStaleRoundError
from tests.unit.services import test_review_service as support

env = support.env


def test_same_content_in_new_round_creates_record_and_rejects_late_retry(
    env: support.ReviewEnv,
) -> None:
    """第一轮已保存但恢复失败，再重评生成第二轮；相同修改必须各消费各轮。"""

    answer_id = str(env.paper.first_answer_id)
    agent = support._StubGradingAgent(
        submission_id=str(env.paper.submission_id),
        low_confidence_answer_ids={answer_id},
        regrade={answer_id: (7.0, 0.3)},
    )
    workflow, _, _ = support._pause_run(env, agent=agent, saver=support._saver(env))
    first_round = support._grading_row(env, answer_id).pending_review_round_id
    assert first_round is not None

    class BrokenResume:
        async def apply_teacher_decision_async(
            self, *_args: Any, **_kwargs: Any
        ) -> Any:
            raise RuntimeError("模拟决定已保存、图尚未恢复。")

    query = ReviewQueryService(session_factory=lambda: Session(env.engine))
    failed_service = support._service(env, workflow=BrokenResume())  # type: ignore[arg-type]
    api = ReviewDecisionService(query=query, review_service=failed_service)
    payload = TeacherDecisionRequest(
        submission_id=env.paper.submission_id,
        answer_id=UUID(answer_id),
        workflow_id=support.WORKFLOW_ID,
        action="modify",
        score=Decimal("8.00"),
        reason="完全相同的修改。",
        expected_review_status=ReviewStatus.PENDING_REVIEW,
        expected_review_round_id=first_round,
    )
    first = support._run(api.submit(teacher_id=env.owner_id, payload=payload))
    assert first.decision_saved and first.resume_status == "failed"
    assert first.review_round_id == first_round
    assert support._grading_row(env, answer_id).pending_review_round_id is None
    retry = support._run(api.submit(teacher_id=env.owner_id, payload=payload))
    assert retry.review_record_id == first.review_record_id
    assert len(support._records(env)) == 1

    service = support._service(env, workflow=workflow)
    # 图仍停在原 Pending；教师显式 Re-grade 覆盖已保存结论，然后重新进入 Pending。
    regraded = service.request_regrade(
        support.WORKFLOW_ID,
        support.THREAD_ID,
        answer_id,
        actor_id=env.owner_id,
        actor_role=UserRole.TEACHER,
        expected_review_status=ReviewStatus.PENDING_REVIEW.value,
    )
    assert regraded.resumed
    row = support._grading_row(env, answer_id)
    second_round = row.pending_review_round_id
    assert row.review_status is ReviewStatus.PENDING_REVIEW
    assert second_round is not None and second_round != first_round
    api = ReviewDecisionService(query=query, review_service=service)
    with pytest.raises(ReviewStaleRoundError):
        support._run(api.submit(teacher_id=env.owner_id, payload=payload))
    assert (
        len(support._records(env)) == 2
    )  # 第一轮 Modified + Re-grade，没有误消费第二轮
    second_payload = payload.model_copy(
        update={"expected_review_round_id": second_round}
    )
    second = support._run(api.submit(teacher_id=env.owner_id, payload=second_payload))
    assert second.review_record_id != first.review_record_id
    assert second.review_round_id == second_round
    assert second.idempotency_degraded is False
    modified = [r for r in support._records(env) if r.decision is ReviewStatus.MODIFIED]
    assert len(modified) == 2
    assert {r.review_round_id for r in modified} == {first_round, second_round}
    assert {r.final_score for r in modified} == {Decimal("8.00")}
    assert {r.final_reason for r in modified} == {"完全相同的修改。"}
    assert support._grading_row(env, answer_id).pending_review_round_id is None
    with pytest.raises(ReviewDecisionStaleError):
        support._run(api.submit(teacher_id=env.owner_id, payload=payload))
    repeated = support._run(api.submit(teacher_id=env.owner_id, payload=second_payload))
    assert repeated.review_record_id == second.review_record_id
    assert len(support._records(env)) == 3


@pytest.mark.parametrize("strict", [False, True])
def test_locked_round_check_rejects_pending_aba(
    env: support.ReviewEnv, strict: bool
) -> None:
    """即使复核状态仍为 Pending，事务内轮次已换也不能沿用预检通过的请求。"""

    answer_id = str(env.paper.first_answer_id)
    agent = support._StubGradingAgent(
        submission_id=str(env.paper.submission_id),
        low_confidence_answer_ids={answer_id},
    )
    workflow, _, _ = support._pause_run(env, agent=agent, saver=support._saver(env))
    service = support._service(env, workflow=workflow)
    round_id = support._grading_row(env, answer_id).pending_review_round_id
    assert round_id is not None
    decision = support._decision_payload(
        env, review_status=ReviewStatus.CONFIRMED.value
    )
    prepared = service._prepare(
        decision,
        actor_id=env.owner_id,
        actor_role=UserRole.TEACHER,
        expected_review_round_id=round_id if strict else None,
    )
    with Session(env.engine) as session:
        row = session.scalars(
            select(GradingResult).where(GradingResult.answer_id == UUID(answer_id))
        ).one()
        row.review_status = ReviewStatus.CONFIRMED
        session.flush()
        row.review_status = ReviewStatus.PENDING_REVIEW
        session.commit()
        new_round = row.pending_review_round_id
    assert new_round != round_id
    with pytest.raises(ReviewStaleRoundError):
        service._apply_decision(prepared, decision, actor_id=env.owner_id, comment=None)
    assert support._records(env) == []
    assert support._grading_row(env, answer_id).pending_review_round_id == new_round


def test_service_rejects_wrong_round_before_writing(env: support.ReviewEnv) -> None:
    answer_id = str(env.paper.first_answer_id)
    agent = support._StubGradingAgent(
        submission_id=str(env.paper.submission_id),
        low_confidence_answer_ids={answer_id},
    )
    workflow, _, _ = support._pause_run(env, agent=agent, saver=support._saver(env))
    service = support._service(env, workflow=workflow)
    with pytest.raises(ReviewStaleRoundError):
        service.submit_decision(
            support._decision_payload(env, review_status=ReviewStatus.CONFIRMED.value),
            actor_id=env.owner_id,
            actor_role=UserRole.TEACHER,
            expected_review_round_id=uuid4(),
        )
    assert support._records(env) == []


def test_legacy_null_round_is_recorded_without_fake_backfill(
    env: support.ReviewEnv,
) -> None:
    answer_id = str(env.paper.first_answer_id)
    agent = support._StubGradingAgent(
        submission_id=str(env.paper.submission_id),
        low_confidence_answer_ids={answer_id},
    )
    workflow, _, _ = support._pause_run(env, agent=agent, saver=support._saver(env))
    with Session(env.engine) as session:
        row = session.scalars(
            select(GradingResult).where(GradingResult.answer_id == UUID(answer_id))
        ).one()
        row.pending_review_round_id = None
        session.commit()
    api = ReviewDecisionService(
        query=ReviewQueryService(session_factory=lambda: Session(env.engine)),
        review_service=support._service(env, workflow=workflow),
    )
    payload = TeacherDecisionRequest(
        submission_id=env.paper.submission_id,
        answer_id=UUID(answer_id),
        action="confirm",
        workflow_id=support.WORKFLOW_ID,
        expected_review_round_id=None,
    )
    with pytest.raises(ReviewStaleRoundError):
        support._run(
            api.submit(
                teacher_id=env.owner_id,
                payload=payload.model_copy(
                    update={"expected_review_round_id": uuid4()}
                ),
            )
        )
    saved = support._run(api.submit(teacher_id=env.owner_id, payload=payload))
    assert saved.idempotency_degraded is True
    assert saved.review_round_id is None
    assert len(support._records(env)) == 1
    assert support._records(env)[0].review_round_id is None
    retry = support._run(api.submit(teacher_id=env.owner_id, payload=payload))
    assert retry.review_record_id == saved.review_record_id
