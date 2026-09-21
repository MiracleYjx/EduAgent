"""P3.1.1 模型轮次生命周期：同轮次稳定、新 Pending 换号、历史 NULL 保留。"""

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.app.domain.enums import ReviewStatus
from backend.app.models import GradingResult
from tests.unit.models.sqlite_support import create_sqlite_engine, seed_submission
from tests.unit.models.test_grading_result import _grading_result


def test_pending_round_is_stable_until_a_new_pending_transition() -> None:
    engine = create_sqlite_engine()
    try:
        with Session(engine) as session:
            fixture = seed_submission(session)
            row = _grading_result(
                fixture.subjective_answer_id,
                fixture.submission_id,
                review_status=ReviewStatus.PENDING_REVIEW,
            )
            session.add(row)
            session.commit()
            first = row.pending_review_round_id
            assert first is not None and first.version == 4
            row.review_status = ReviewStatus.PENDING_REVIEW
            row.reason = "保存同轮评分。"
            session.commit()
            assert row.pending_review_round_id == first
            row.review_status = ReviewStatus.MODIFIED
            session.commit()
            assert row.pending_review_round_id is None
        with Session(engine) as session:
            row = session.scalars(select(GradingResult)).one()
            # 过期属性也必须读取旧状态，不能把 Pending 的重复保存当新轮次。
            session.expire(row)
            row.review_status = ReviewStatus.PENDING_REVIEW
            session.commit()
            second = row.pending_review_round_id
            assert second is not None and second.version == 4 and second != first
            session.expire(row)
            row.review_status = ReviewStatus.PENDING_REVIEW
            session.commit()
            assert row.pending_review_round_id == second
            row.review_status = ReviewStatus.CONFIRMED
            session.flush()
            row.review_status = ReviewStatus.PENDING_REVIEW
            session.flush()
            assert row.pending_review_round_id != second
            session.rollback()
            assert row.pending_review_round_id == second
    finally:
        engine.dispose()


def test_legacy_pending_round_stays_null_until_a_new_transition() -> None:
    engine = create_sqlite_engine()
    try:
        with Session(engine) as session:
            fixture = seed_submission(session)
            row = _grading_result(
                fixture.subjective_answer_id,
                fixture.submission_id,
                review_status=ReviewStatus.PENDING_REVIEW,
            )
            session.add(row)
            session.commit()
            # 模拟迁移前数据：不通过 ORM 状态事件，保留历史 NULL。
            session.execute(update(GradingResult).values(pending_review_round_id=None))
            session.commit()
        with Session(engine) as session:
            row = session.scalars(select(GradingResult)).one()
            assert row.pending_review_round_id is None
            row.review_status = ReviewStatus.PENDING_REVIEW
            session.commit()
            assert row.pending_review_round_id is None
            row.review_status = ReviewStatus.NOT_REQUIRED
            session.commit()
            row.review_status = ReviewStatus.PENDING_REVIEW
            session.commit()
            assert row.pending_review_round_id is not None
    finally:
        engine.dispose()
