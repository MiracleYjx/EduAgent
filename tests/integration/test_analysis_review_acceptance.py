"""T184 queries before/after actual durable review and original graph resume (TCR §39).

Controlled grading/diagnosis agents; SQLite here supplements the PostgreSQL frozen
population contract. No independent teacher/model quality is claimed.
"""

from decimal import Decimal

from sqlalchemy.orm import Session

from backend.app.api.results import ResultsQueryService
from backend.app.domain.enums import ReviewStatus, UserRole
from backend.app.services.grading.grading_repository import DatabaseGradingRepository
from tests.support.t184_statistics import save_evidence
from tests.unit.services import test_review_service as review

env = review.env


def test_modified_review_changes_actual_analysis_and_keeps_original_record(env):
    target_id = review._subjective_answer_ids(env)[0]
    agent = review._StubGradingAgent(
        submission_id=str(env.paper.submission_id),
        low_confidence_answer_ids={target_id},
    )
    workflow, _, _ = review._pause_run(env, agent=agent, saver=review._saver(env))
    repository = DatabaseGradingRepository(session_factory=lambda: Session(env.engine))
    with Session(env.engine) as session:
        query = ResultsQueryService(session=session, repository=repository)
        before = query.get_exam_summary(env.owner_id, review._snapshot(env).exam_id)
        assert before.final_count == 0 and before.average_of_final_scores is None
        assert before.pending_review_submission_count == 1
        snapshot = review._snapshot(env)
        target = next(a for a in snapshot.answers if a.answer_id == target_id)
        revised = review._grading_result(
            target,
            str(env.paper.submission_id),
            score=9.0,
            confidence=0.95,
            review_status=ReviewStatus.NOT_REQUIRED.value,
        )
        outcome = review._service(env, workflow=workflow).submit_decision(
            review._decision_payload(
                env, review_status=ReviewStatus.MODIFIED.value, revised_result=revised
            ),
            actor_id=env.owner_id,
            actor_role=UserRole.TEACHER,
            comment="T184受控运行复核，非教师标注",
        )
        session.expire_all()
        after = query.get_exam_summary(env.owner_id, review._snapshot(env).exam_id)
        assert outcome.resumed and after.final_count == 1
        assert after.average_of_final_scores == Decimal("25.00")
        assert after.pending_review_submission_count == 0
        records = review._records(env)
        assert (
            len(records) == 1
            and records[0].original_score == Decimal(6)
            and records[0].final_score == Decimal(9)
        )
        save_evidence(
            "review-before-after",
            {
                "before": before.model_dump(mode="json"),
                "after": after.model_dump(mode="json"),
                "review_record_id": str(records[0].id),
                "original_score": "6.00",
                "reviewed_score": "9.00",
                "expected_final": "25.00",
                "origin": "新增受控执行场景；真实持久复核/原图恢复；不改冻结参考",
            },
        )
