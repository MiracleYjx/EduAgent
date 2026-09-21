"""P3.3：真实 PostgreSQL 最终结果写入事件与 Submission 生命周期的原子性。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.domain.enums import ReviewStatus, SubmissionStatus
from backend.app.models import (
    ExamResult,
    GradingResult,
    ReviewRecord,
    Submission,
    WorkflowRun,
)
from backend.app.services.grading.grading_repository import DatabaseGradingRepository
from tests.integration import test_langgraph_grading_workflow as support

env = support.env
solo_env = support.solo_env


def _lifecycle(env: support.ReviewEnv) -> tuple[SubmissionStatus, datetime | None, bool]:
    """始终从新 Session 读取持久化事实，不复用 ORM 缓存。"""

    with Session(env.engine) as session:
        submission = session.get(Submission, UUID(env.paper.submission_id))
        assert submission is not None
        result = session.scalars(select(ExamResult)).one()
        return submission.status, submission.reviewed_at, result.is_final


def _review(
    app: support.ApiHarness, env: support.ReviewEnv, body: dict[str, Any]
) -> Any:
    return app.client.post(
        "/api/reviews/decisions", headers=support._teacher_headers(env), json=body
    )


@pytest.mark.parametrize("path", ["automatic", "confirm", "modify"])
def test_final_result_marks_reviewed_and_retries_preserve_timestamp(
    solo_env: support.ReviewEnv, path: str
) -> None:
    """自动最终化或最后一题复核最终化均进入 Reviewed，跨 Session 重试保持原时间。"""

    scoring = support.SequenceScoringProvider([0.95 if path == "automatic" else 0.3])
    with support._api_client(solo_env, scoring_provider=scoring) as app:
        started = support._start(app, solo_env)
        assert started.status_code == 200
        if path != "automatic":
            assert _lifecycle(solo_env) == (SubmissionStatus.GRADED, None, False)
            body = support._confirm_body(solo_env, started.json()["workflow_id"])
            body["action"] = path
            if path == "modify":
                body.update(score="8.00", reason="教师修订后的最终结论。")
            detail = app.client.get(
                f"/api/reviews/queue/{solo_env.paper.submission_id}/answers/{body['answer_id']}",
                headers=support._teacher_headers(solo_env),
            ).json()
            body["expected_review_round_id"] = detail["pending_review_round_id"]
            saved = _review(app, solo_env, body)
            assert saved.status_code == 200
            assert saved.json()["decision_saved"] is True

        status, reviewed_at, is_final = _lifecycle(solo_env)
        assert status is SubmissionStatus.REVIEWED
        assert reviewed_at is not None
        assert is_final is True

        if path != "automatic":
            with support._api_client(
                solo_env, scoring_provider=support.SequenceScoringProvider([])
            ) as new_app:
                retry = _review(new_app, solo_env, body)
            assert retry.status_code == 200
            assert retry.json()["review_record_id"] == saved.json()["review_record_id"]
            with Session(solo_env.engine) as session:
                assert len(list(session.scalars(select(ReviewRecord)))) == 1

    # 使用不同的时钟重复写入 final，不能把 reviewed_at 刷新成重试时间。
    repository = DatabaseGradingRepository(
        session_factory=lambda: Session(solo_env.engine),
        clock=lambda: support.FIXED_NOW + timedelta(days=1),
    )
    final = repository.get_exam_result(solo_env.paper.submission_id)
    assert final is not None and final.is_final
    repository.save_exam_result(final)
    assert _lifecycle(solo_env) == (SubmissionStatus.REVIEWED, reviewed_at, True)


def test_remaining_pending_review_prevents_reviewed_until_last_decision(
    env: support.ReviewEnv,
) -> None:
    """第一题复核后还有另一题 Pending，最后一题复核才最终化。"""

    with support._api_client(
        env, scoring_provider=support.SequenceScoringProvider([0.3, 0.3])
    ) as app:
        started = support._start(app, env)
        assert started.status_code == 200
        body = support._confirm_body(env, started.json()["workflow_id"])
        first = _review(app, env, body)
        assert first.status_code == 200
        assert first.json()["pending_review_count"] == 1
        assert _lifecycle(env) == (SubmissionStatus.GRADED, None, False)
        with Session(env.engine) as session:
            pending = list(session.scalars(select(GradingResult).where(
                GradingResult.review_status == ReviewStatus.PENDING_REVIEW
            )))
            assert [str(row.answer_id) for row in pending] == [env.paper.subjective_answer_ids[1]]

        body["answer_id"] = env.paper.subjective_answer_ids[1]
        last = _review(app, env, body)
        assert last.status_code == 200
        assert last.json()["pending_review_count"] == 0
    status, reviewed_at, is_final = _lifecycle(env)
    assert (status, is_final) == (SubmissionStatus.REVIEWED, True)
    assert reviewed_at is not None


def test_resumed_graph_finalization_marks_reviewed(env: support.ReviewEnv) -> None:
    """教师决定事务尚非 final，继续自动评分后通过同一最终化事件推进生命周期。"""

    class ObserveNextAnswer(support.SequenceScoringProvider):
        async def generate_structured(self, *args: Any, **kwargs: Any) -> Any:
            if len(self.calls) == 1:
                # 第二题模型调用发生在教师决定提交后：此时不能提前 Reviewed。
                assert _lifecycle(env) == (SubmissionStatus.GRADED, None, False)
                with Session(env.engine) as session:
                    assert len(list(session.scalars(select(ReviewRecord)))) == 1
            return await super().generate_structured(*args, **kwargs)

    scoring = ObserveNextAnswer([0.3, 0.95])
    with support._api_client(env, scoring_provider=scoring) as app:
        started = support._start(app, env)
        assert started.status_code == 200
        response = _review(app, env, support._confirm_body(env, started.json()["workflow_id"]))
        assert response.status_code == 200
        assert response.json()["resume_status"] == "succeeded"
    assert len(scoring.calls) == 2
    status, reviewed_at, is_final = _lifecycle(env)
    assert (status, is_final) == (SubmissionStatus.REVIEWED, True)
    assert reviewed_at is not None


@pytest.mark.parametrize("phase", ["review", "resume"])
def test_finalization_commit_failure_rolls_back_reviewed_and_final_result(
    env: support.ReviewEnv, phase: str
) -> None:
    """仅在 final 真正提交前失败；生命周期与 final 同回滚，已提交的前置决定不抹掉。"""

    final_transactions: list[int] = []

    def fail_final_commit(session: Session) -> None:
        if session.get_bind() is not env.engine:
            return
        session.flush()
        result = session.scalars(select(ExamResult)).one_or_none()
        if result is None or not result.is_final:
            return
        submission = session.get(Submission, UUID(env.paper.submission_id))
        assert submission is not None
        assert submission.status is SubmissionStatus.REVIEWED
        assert submission.reviewed_at is not None
        # 此处读到了同一事务中已经 flush 的 final + Reviewed + 审计记录，但尚未提交。
        final_transactions.append(len(list(session.scalars(select(ReviewRecord)))))
        raise SQLAlchemyError("P3.3 测试注入最终化事务提交失败")

    scoring = support.SequenceScoringProvider([0.3, 0.3 if phase == "review" else 0.95])
    with support._api_client(
        env, scoring_provider=scoring, raise_server_exceptions=False
    ) as app:
        started = support._start(app, env)
        assert started.status_code == 200
        body = support._confirm_body(env, started.json()["workflow_id"])
        if phase == "review":
            assert _review(app, env, body).status_code == 200
            body["answer_id"] = env.paper.subjective_answer_ids[1]
        assert _lifecycle(env) == (SubmissionStatus.GRADED, None, False)
        with Session(env.engine) as session:
            run_before = session.scalars(select(WorkflowRun)).one().checkpoint
            result_before = session.scalars(select(ExamResult)).one()
            result_facts = (result_before.final_total_score, result_before.confirmed_subtotal)

        event.listen(Session, "before_commit", fail_final_commit)
        try:
            response = _review(app, env, body)
        finally:
            event.remove(Session, "before_commit", fail_final_commit)

    assert response.status_code == 500
    assert final_transactions == [2 if phase == "review" else 1]
    assert _lifecycle(env) == (SubmissionStatus.GRADED, None, False)
    with Session(env.engine) as session:
        records = list(session.scalars(select(ReviewRecord)))
        assert len(records) == 1  # 复核阶段回滚最后一条；恢复阶段保留已提交的第一条。
        assert str(records[0].grading_result.answer_id) == env.paper.subjective_answer_ids[0]
        result = session.scalars(select(ExamResult)).one()
        assert result.final_total_score is None
        if phase == "review":
            assert (result.final_total_score, result.confirmed_subtotal) == result_facts
            target = session.scalars(select(GradingResult).where(
                GradingResult.answer_id == UUID(env.paper.subjective_answer_ids[1])
            )).one()
            assert target.review_status is ReviewStatus.PENDING_REVIEW
            assert session.scalars(select(WorkflowRun)).one().checkpoint == run_before
        else:
            # 最终事务回滚新自动评分，但前置教师确认不回滚。
            rows = list(session.scalars(select(GradingResult)))
            assert len(rows) == 2  # 客观题 + 已确认的第一道主观题。
            first = next(row for row in rows if str(row.answer_id) == env.paper.subjective_answer_ids[0])
            assert first.review_status is ReviewStatus.CONFIRMED
