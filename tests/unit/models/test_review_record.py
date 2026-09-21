"""T062 ReviewRecord 模型单元测试。

覆盖范围：表结构与约束名、decision 复用 ReviewStatus 且限定为可操作取值、
复核前后事实（分数、理由、知识点）同时落库、Re-grade 尚无新结论时 `final_score` 为空、
`final_knowledge_points` 为空时保存为 SQL NULL、跨 Session 读回、以及评分更新后
复核记录中的历史事实不被改写。

测试运行在启用外键约束的内存 SQLite 上；PostgreSQL 侧的列精度、约束与外键行为由
`alembic upgrade/check` 与一次性验证库上的 pg_catalog 断言覆盖。
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import Enum as SAEnum
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.core.database import Base
from backend.app.domain.enums import (
    QuestionType,
    ReviewStatus,
    ValidationStatus,
)
from backend.app.models import GradingResult, ReviewRecord
from tests.unit.models.sqlite_support import (
    SubmissionFixture,
    create_sqlite_engine,
    seed_submission,
)

REVIEW_COLUMNS = [
    "grading_result_id",
    "reviewer_id",
    "decision",
    "review_round_id",
    "original_score",
    "original_reason",
    "original_knowledge_points",
    "final_score",
    "final_reason",
    "final_knowledge_points",
    "comment",
    "id",
    "created_at",
    "updated_at",
]

REVIEW_CONSTRAINTS = {
    "review_decision",
    "ck_review_records_decision_operable",
    "ck_review_records_original_score_non_negative",
    "ck_review_records_final_score_non_negative",
}

#: 本表只记录教师实际执行的复核操作。
OPERABLE_DECISIONS = (
    ReviewStatus.CONFIRMED,
    ReviewStatus.MODIFIED,
    ReviewStatus.RE_GRADE,
)
#: 非操作状态不得作为复核记录写入。
NON_OPERABLE_DECISIONS = (
    ReviewStatus.NOT_REQUIRED,
    ReviewStatus.PENDING_REVIEW,
    ReviewStatus.FINAL,
)


def _grading_result(
    fixture: SubmissionFixture,
    *,
    score: Decimal = Decimal("8.00"),
) -> GradingResult:
    """构造一条待复核的单题评分结果。"""

    return GradingResult(
        answer_id=fixture.subjective_answer_id,
        submission_id=fixture.submission_id,
        question_type=QuestionType.SHORT_ANSWER,
        score=score,
        max_score=Decimal("10.00"),
        reason="答案覆盖了主要要点。",
        correct_points=["变量"],
        missing_knowledge_points=["作用域"],
        knowledge_points=["变量"],
        suggestions=["补充作用域说明。"],
        retrieved_context_ids=["chunk-1"],
        confidence=0.42,
        validation_status=ValidationStatus.VALIDATED,
        review_status=ReviewStatus.PENDING_REVIEW,
    )


def _review(
    fixture: SubmissionFixture,
    grading_result_id: UUID,
    *,
    decision: ReviewStatus = ReviewStatus.CONFIRMED,
    original_score: Decimal = Decimal("8.00"),
    original_reason: str = "答案覆盖了主要要点。",
    original_knowledge_points: list[str] | None = None,
    final_score: Decimal | None = Decimal("8.00"),
    final_reason: str | None = None,
    final_knowledge_points: list[str] | None = None,
    comment: str | None = None,
) -> ReviewRecord:
    """构造一条复核记录；默认是“确认原评分”。"""

    return ReviewRecord(
        grading_result_id=grading_result_id,
        reviewer_id=fixture.teacher_id,
        decision=decision,
        original_score=original_score,
        original_reason=original_reason,
        original_knowledge_points=(
            ["变量"] if original_knowledge_points is None else original_knowledge_points
        ),
        final_score=final_score,
        final_reason=final_reason,
        final_knowledge_points=final_knowledge_points,
        comment=comment,
    )


def _seed_grading_result(
    session: Session, *, score: Decimal = Decimal("8.00")
) -> tuple[SubmissionFixture, GradingResult]:
    """落库一份答卷与一条待复核评分结果。"""

    fixture = seed_submission(session)
    grading_result = _grading_result(fixture, score=score)
    session.add(grading_result)
    session.commit()
    return fixture, grading_result


def test_table_structure_matches_plan() -> None:
    """列集合、索引与约束名必须与 plan §5.2 的 ReviewRecord 定义一致。"""

    table = Base.metadata.tables["review_records"]

    assert [column.name for column in table.columns] == REVIEW_COLUMNS
    assert {index.name for index in table.indexes} == {
        "ix_review_records_grading_result_id",
        "ix_review_records_reviewer_id",
    }
    assert REVIEW_CONSTRAINTS.issubset(
        {constraint.name for constraint in table.constraints}
    )
    assert all("uq_" not in str(constraint.name) for constraint in table.constraints)


def test_decision_column_reuses_review_status_enum() -> None:
    """复核操作类型必须复用 ReviewStatus，不新增重复枚举。"""

    decision_type = inspect(ReviewRecord).columns.decision.type

    assert isinstance(decision_type, SAEnum)
    assert decision_type.enums == [item.value for item in ReviewStatus]


def test_foreign_key_delete_behavior_is_explicit() -> None:
    """来源外键必须显式声明目标与删除行为。"""

    table = Base.metadata.tables["review_records"]

    assert {fk.parent.name: fk.ondelete for fk in table.foreign_keys} == {
        "grading_result_id": "CASCADE",
        "reviewer_id": "RESTRICT",
    }
    assert {fk.target_fullname for fk in table.foreign_keys} == {
        "grading_results.id",
        "users.id",
    }


@pytest.mark.parametrize("decision", OPERABLE_DECISIONS, ids=lambda item: item.name)
def test_operable_decisions_round_trip(decision: ReviewStatus) -> None:
    """确认、修改与重新评分三类操作的记录都必须可写入读回。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture, grading_result = _seed_grading_result(session)
        record = _review(
            fixture,
            grading_result.id,
            decision=decision,
            final_score=None if decision is ReviewStatus.RE_GRADE else Decimal("9.50"),
            final_reason=None if decision is ReviewStatus.RE_GRADE else "补充作用域。",
            comment="课堂已讲解。",
        )
        session.add(record)
        session.commit()
        record_id = record.id
        grading_result_id = grading_result.id

    with Session(engine) as session:
        stored = session.get(ReviewRecord, record_id)
        assert stored is not None
        assert stored.grading_result_id == grading_result_id
        assert stored.reviewer_id == fixture.teacher_id
        assert stored.decision is decision
        assert stored.original_score == Decimal("8.00")
        assert stored.original_reason == "答案覆盖了主要要点。"
        assert stored.original_knowledge_points == ["变量"]
        assert stored.comment == "课堂已讲解。"
        if decision is ReviewStatus.RE_GRADE:
            # 重新评分只是请求，尚无新结论时不得用 0 分冒充。
            assert stored.final_score is None
            assert stored.final_reason is None
        else:
            assert stored.final_score == Decimal("9.50")
            assert stored.final_reason == "补充作用域。"
        assert stored.created_at is not None


def test_modified_review_keeps_before_and_after_facts() -> None:
    """修改操作必须同时保存修改前后的分数、理由与知识点。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture, grading_result = _seed_grading_result(session)
        record = _review(
            fixture,
            grading_result.id,
            decision=ReviewStatus.MODIFIED,
            original_reason="答案覆盖了主要要点。",
            original_knowledge_points=["变量", "作用域"],
            final_score=Decimal("9.50"),
            final_reason="作用域说明补充后给满分。",
            final_knowledge_points=["变量", "作用域", "提升"],
        )
        session.add(record)
        session.commit()
        record_id = record.id

    with Session(engine) as session:
        stored = session.get(ReviewRecord, record_id)
        assert stored is not None
        assert stored.original_score == Decimal("8.00")
        assert stored.final_score == Decimal("9.50")
        assert stored.original_reason == "答案覆盖了主要要点。"
        assert stored.final_reason == "作用域说明补充后给满分。"
        # 列表按原顺序保存，不去重、不排序。
        assert stored.original_knowledge_points == ["变量", "作用域"]
        assert stored.final_knowledge_points == ["变量", "作用域", "提升"]


def test_review_record_survives_grading_result_update() -> None:
    """后续评分更新只改评分行，复核记录中的修改前后事实保持不变。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture, grading_result = _seed_grading_result(session)
        session.add(
            _review(
                fixture,
                grading_result.id,
                decision=ReviewStatus.MODIFIED,
                final_score=Decimal("9.50"),
                final_reason="作用域说明补充后给满分。",
            )
        )
        session.commit()
        record_id = session.scalar(select(ReviewRecord.id))
        grading_result_id = grading_result.id

    with Session(engine) as session:
        stored_result = session.get(GradingResult, grading_result_id)
        assert stored_result is not None
        stored_result.score = Decimal("10.00")
        stored_result.review_status = ReviewStatus.MODIFIED
        session.commit()

    with Session(engine) as session:
        record = session.get(ReviewRecord, record_id)
        result = session.get(GradingResult, grading_result_id)
        assert record is not None
        assert result is not None
        assert result.score == Decimal("10.00")
        assert record.original_score == Decimal("8.00")
        assert record.final_score == Decimal("9.50")
        assert record.final_reason == "作用域说明补充后给满分。"


def test_empty_final_knowledge_points_is_stored_as_sql_null() -> None:
    """final_knowledge_points 为空时保存为 SQL NULL，而不是 JSON null 文本。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture, grading_result = _seed_grading_result(session)
        session.add(
            _review(
                fixture,
                grading_result.id,
                final_knowledge_points=None,
            )
        )
        session.commit()
        record_id = session.scalar(select(ReviewRecord.id))

    with Session(engine) as session:
        is_sql_null = session.scalar(
            select(func.count())
            .select_from(ReviewRecord)
            .where(ReviewRecord.id == record_id, text("final_knowledge_points IS NULL"))
        )
        assert is_sql_null == 1
        stored = session.get(ReviewRecord, record_id)
        assert stored is not None
        assert stored.final_knowledge_points is None


@pytest.mark.parametrize(
    "decision", NON_OPERABLE_DECISIONS, ids=lambda item: item.name
)
def test_non_operable_decisions_are_rejected(decision: ReviewStatus) -> None:
    """Not Required、Pending Review 与 Final 不是教师操作，必须被拒绝。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture, grading_result = _seed_grading_result(session)
        session.add(_review(fixture, grading_result.id, decision=decision))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


@pytest.mark.parametrize("original_score", [Decimal("-0.50"), Decimal("-10.00")])
def test_negative_original_score_is_rejected(original_score: Decimal) -> None:
    """复核记录中的原始分数不得为负。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture, grading_result = _seed_grading_result(session)
        session.add(
            _review(fixture, grading_result.id, original_score=original_score)
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_negative_final_score_is_rejected() -> None:
    """复核后的分数同样不得为负。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture, grading_result = _seed_grading_result(session)
        session.add(
            _review(
                fixture,
                grading_result.id,
                decision=ReviewStatus.MODIFIED,
                final_score=Decimal("-1.00"),
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_re_grade_zero_score_differs_from_missing_conclusion() -> None:
    """0 分是合法的新结论，与“尚未有新结论”的 NULL 必须可区分。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture, grading_result = _seed_grading_result(session)
        zero_record = _review(
            fixture,
            grading_result.id,
            decision=ReviewStatus.RE_GRADE,
            final_score=Decimal("0.00"),
        )
        session.add(zero_record)
        session.commit()
        zero_id = zero_record.id

    with Session(engine) as session:
        stored = session.get(ReviewRecord, zero_id)
        assert stored is not None
        assert stored.final_score == Decimal("0.00")
        assert stored.final_score is not None


def test_review_requires_existing_grading_result() -> None:
    """复核记录必须关联真实评分行，不能凭空引用。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        session.add(_review(fixture, fixture.subjective_answer_id))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_deleting_grading_result_cascades_its_reviews() -> None:
    """删除评分行时其复核记录级联删除，不残留失去来源的复核事实。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture, grading_result = _seed_grading_result(session)
        session.add(_review(fixture, grading_result.id))
        session.commit()
        grading_result_id = grading_result.id

    with Session(engine) as session:
        stored = session.get(GradingResult, grading_result_id)
        assert stored is not None
        session.delete(stored)
        session.commit()

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(ReviewRecord)) == 0
