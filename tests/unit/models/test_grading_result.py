"""T060 GradingResult 与 ExamResult 模型单元测试。

覆盖范围：表结构与约束名、数值精度、唯一性、分数与置信度范围、决策快照
“全空或全非空”、跨 Session 读回（含 0.999999 浮点边界）、整卷最终状态自洽、
按答卷读取逐题结果的只读关系、删除答案时的级联行为，以及“缺结果不得落库为
零分行”的边界。

测试全部运行在启用外键约束的内存 SQLite 上；PostgreSQL 侧的列精度、约束与外键
行为由 `alembic upgrade/check` 与一次性验证库上的 pg_catalog 断言覆盖。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

import pytest
from sqlalchemy import Enum as SAEnum
from sqlalchemy import Float, func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.core.database import Base
from backend.app.domain.enums import (
    GradingStatus,
    QuestionType,
    ReviewStatus,
    ValidationStatus,
)
from backend.app.models import Answer, ExamResult, GradingResult
from backend.app.schemas.grading import ConfidenceDecisionDTO, ExamResultStatus
from tests.unit.models.sqlite_support import (
    DEFAULT_MAX_SCORE,
    create_sqlite_engine,
    seed_submission,
    sqlite_foreign_keys_enabled,
)

GRADING_RESULT_COLUMNS = [
    "answer_id",
    "submission_id",
    "question_type",
    "score",
    "max_score",
    "reason",
    "correct_points",
    "missing_knowledge_points",
    "knowledge_points",
    "suggestions",
    "retrieved_context_ids",
    "confidence",
    "validation_status",
    "review_status",
    "pending_review_round_id",
    "decision_confidence",
    "decision_threshold",
    "decision_requires_review",
    "decision_review_status",
    "decision_grading_status",
    "decision_reason",
    "id",
    "created_at",
    "updated_at",
]

EXAM_RESULT_COLUMNS = [
    "submission_id",
    "exam_id",
    "student_id",
    "result_status",
    "is_final",
    "final_total_score",
    "confirmed_subtotal",
    "total_max_score",
    "aggregated_at",
    "id",
    "created_at",
    "updated_at",
]

GRADING_RESULT_CONSTRAINTS = {
    "uq_grading_results_answer",
    "question_type",
    "grading_validation_status",
    "grading_review_status",
    "grading_decision_review_status",
    "grading_decision_grading_status",
    "ck_grading_results_score_non_negative",
    "ck_grading_results_max_score_positive",
    "ck_grading_results_score_range",
    "ck_grading_results_confidence_range",
    "ck_grading_results_decision_confidence_range",
    "ck_grading_results_decision_threshold_range",
    "ck_grading_results_decision_snapshot",
}

EXAM_RESULT_CONSTRAINTS = {
    "uq_exam_results_submission",
    "exam_result_status",
    "ck_exam_results_total_max_score_positive",
    "ck_exam_results_confirmed_subtotal_non_negative",
    "ck_exam_results_final_total_score_non_negative",
    "ck_exam_results_final_state",
}


def _grading_result(
    fixture_answer_id,
    fixture_submission_id,
    *,
    question_type: QuestionType = QuestionType.SHORT_ANSWER,
    score: Decimal = Decimal("8.00"),
    max_score: Decimal = DEFAULT_MAX_SCORE,
    confidence: float = 0.95,
    validation_status: ValidationStatus = ValidationStatus.VALIDATED,
    review_status: ReviewStatus = ReviewStatus.NOT_REQUIRED,
    with_decision: bool = False,
) -> GradingResult:
    """构造一条单题评分结果；默认不携带置信度决策快照。"""

    row = GradingResult(
        answer_id=fixture_answer_id,
        submission_id=fixture_submission_id,
        question_type=question_type,
        score=score,
        max_score=max_score,
        reason="答案覆盖了主要要点。",
        correct_points=["变量"],
        missing_knowledge_points=["作用域"],
        knowledge_points=["变量"],
        suggestions=["补充作用域说明。"],
        retrieved_context_ids=["chunk-1", "chunk-1", "chunk-2"],
        confidence=confidence,
        validation_status=validation_status,
        review_status=review_status,
    )
    if with_decision:
        row.decision_confidence = confidence
        row.decision_threshold = 0.8
        row.decision_requires_review = False
        row.decision_review_status = ReviewStatus.NOT_REQUIRED
        row.decision_grading_status = GradingStatus.ACCEPTED
        row.decision_reason = "置信度不低于阈值。"
    return row


def _exam_result(
    fixture_submission_id,
    fixture_exam_id,
    fixture_student_id,
    *,
    is_final: bool = True,
    result_status: ExamResultStatus = ExamResultStatus.FINAL,
    final_total_score: Decimal | None = Decimal("16.00"),
) -> ExamResult:
    """构造一条整卷结果。"""

    return ExamResult(
        submission_id=fixture_submission_id,
        exam_id=fixture_exam_id,
        student_id=fixture_student_id,
        result_status=result_status,
        is_final=is_final,
        final_total_score=final_total_score,
        confirmed_subtotal=Decimal("16.00"),
        total_max_score=Decimal("20.00"),
        aggregated_at=datetime.now(UTC),
    )


def test_sqlite_test_engine_enforces_foreign_keys() -> None:
    """测试库必须真正启用外键约束，否则外键用例只是假通过。"""

    assert sqlite_foreign_keys_enabled(create_sqlite_engine()) is True


def test_grading_result_table_structure_matches_data_model() -> None:
    """列集合、索引与约束名必须与 data-model.md 的 GradingResult 字段一致。"""

    table = Base.metadata.tables["grading_results"]

    assert [column.name for column in table.columns] == GRADING_RESULT_COLUMNS
    assert {index.name for index in table.indexes} == {
        "ix_grading_results_submission_id",
        "ix_grading_results_submission_review_status",
        "ix_grading_results_pending_review_round_id",
    }
    assert GRADING_RESULT_CONSTRAINTS.issubset(
        {constraint.name for constraint in table.constraints}
    )


def test_exam_result_table_structure_matches_plan_5_2() -> None:
    """整卷结果列集合、索引与约束名必须与 plan §5.2 一致。"""

    table = Base.metadata.tables["exam_results"]

    assert [column.name for column in table.columns] == EXAM_RESULT_COLUMNS
    assert {index.name for index in table.indexes} == {"ix_exam_results_exam_student"}
    assert EXAM_RESULT_CONSTRAINTS.issubset(
        {constraint.name for constraint in table.constraints}
    )


def test_numeric_precision_and_nullability_are_explicit() -> None:
    """分数使用 Decimal 精度，置信度使用可完整往返 float 的 Float(53)。"""

    grading = Base.metadata.tables["grading_results"]
    exam = Base.metadata.tables["exam_results"]

    assert grading.c.score.type.precision == 8
    assert grading.c.score.type.scale == 2
    assert grading.c.max_score.type.precision == 8
    confidence_type = cast(Float, grading.c.confidence.type)
    assert isinstance(confidence_type, Float)
    assert confidence_type.precision == 53
    assert isinstance(grading.c.decision_confidence.type, Float)
    assert grading.c.decision_confidence.type.precision == 53
    assert exam.c.total_max_score.type.precision == 10
    assert exam.c.final_total_score.type.scale == 2

    assert grading.c.reason.nullable is False
    assert grading.c.confidence.nullable is False
    assert grading.c.decision_confidence.nullable is True
    assert exam.c.final_total_score.nullable is True
    assert exam.c.confirmed_subtotal.nullable is False
    assert exam.c.total_max_score.nullable is False


def test_status_columns_reuse_existing_domain_enums() -> None:
    """校验/复核状态与整卷结果状态必须复用既有枚举，不新增重复枚举。"""

    grading = inspect(GradingResult).columns
    exam = inspect(ExamResult).columns

    question_type = cast(SAEnum, grading.question_type.type)
    validation_status = cast(SAEnum, grading.validation_status.type)
    review_status = cast(SAEnum, grading.review_status.type)
    decision_review_status = cast(SAEnum, grading.decision_review_status.type)
    decision_grading_status = cast(SAEnum, grading.decision_grading_status.type)
    result_status = cast(SAEnum, exam.result_status.type)

    assert question_type.enums == [item.value for item in QuestionType]
    assert validation_status.enums == [item.value for item in ValidationStatus]
    assert review_status.enums == [item.value for item in ReviewStatus]
    assert decision_review_status.enums == [item.value for item in ReviewStatus]
    assert decision_grading_status.enums == [item.value for item in GradingStatus]
    assert result_status.enums == [item.value for item in ExamResultStatus]


def test_grading_result_rejects_duplicate_answer() -> None:
    """一道题只能有一条评分结果。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        session.add(
            _grading_result(fixture.subjective_answer_id, fixture.submission_id)
        )
        session.commit()

        session.add(
            _grading_result(fixture.subjective_answer_id, fixture.submission_id)
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_exam_result_rejects_duplicate_submission() -> None:
    """一份答卷只保留一条当前整卷结果。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        session.add(
            _exam_result(
                fixture.submission_id, fixture.exam_id, fixture.student_id
            )
        )
        session.commit()

        session.add(
            _exam_result(
                fixture.submission_id, fixture.exam_id, fixture.student_id
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


@pytest.mark.parametrize(
    ("score", "max_score", "confidence"),
    [
        (Decimal("11.00"), Decimal("10.00"), 0.9),
        (Decimal("-0.50"), Decimal("10.00"), 0.9),
        (Decimal("5.00"), Decimal("0.00"), 0.9),
        (Decimal("5.00"), Decimal("10.00"), 1.5),
        (Decimal("5.00"), Decimal("10.00"), -0.1),
    ],
)
def test_score_and_confidence_range_constraints_reject_invalid_rows(
    score: Decimal,
    max_score: Decimal,
    confidence: float,
) -> None:
    """得分不得越界、满分必须为正、置信度必须位于 [0, 1]。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        session.add(
            _grading_result(
                fixture.subjective_answer_id,
                fixture.submission_id,
                score=score,
                max_score=max_score,
                confidence=confidence,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_decision_snapshot_must_be_all_present_or_all_absent() -> None:
    """决策快照部分为空时拒绝写入，避免出现无法复原的半截决策。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        row = _grading_result(fixture.subjective_answer_id, fixture.submission_id)
        row.decision_confidence = 0.9
        session.add(row)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_decision_snapshot_rejects_out_of_range_threshold() -> None:
    """决策快照内的阈值同样受 [0, 1] 约束。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        row = _grading_result(
            fixture.subjective_answer_id,
            fixture.submission_id,
            with_decision=True,
        )
        row.decision_threshold = 1.2
        session.add(row)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_objective_result_round_trips_across_sessions_without_decision() -> None:
    """客观题结果不携带决策快照，跨 Session 读回后字段完整。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        row = _grading_result(
            fixture.objective_answer_id,
            fixture.submission_id,
            question_type=QuestionType.SINGLE_CHOICE,
            score=Decimal("10.00"),
            confidence=1.0,
        )
        session.add(row)
        session.commit()
        row_id = row.id

    with Session(engine) as session:
        stored = session.get(GradingResult, row_id)
        assert stored is not None
        assert stored.question_type is QuestionType.SINGLE_CHOICE
        assert stored.score == Decimal("10.00")
        assert stored.confidence == 1.0
        assert stored.validation_status is ValidationStatus.VALIDATED
        assert stored.review_status is ReviewStatus.NOT_REQUIRED
        assert stored.decision_confidence is None
        assert stored.decision_review_status is None
        # 列表字段保留原始顺序与重复：共享构造器写入 3 个片段标识（含重复项）。
        assert stored.retrieved_context_ids == ["chunk-1", "chunk-1", "chunk-2"]


def test_subjective_auto_accepted_result_round_trips_decision_snapshot() -> None:
    """主观题自动接受必须保留当次决策，并能复原为 ConfidenceDecisionDTO。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        row = _grading_result(
            fixture.subjective_answer_id,
            fixture.submission_id,
            confidence=0.999999,
            with_decision=True,
        )
        row.decision_confidence = 0.999999
        row.decision_grading_status = GradingStatus.ACCEPTED
        session.add(row)
        session.commit()
        row_id = row.id

    with Session(engine) as session:
        stored = session.get(GradingResult, row_id)
        assert stored is not None
        # 浮点置信度必须原值往返，不得被量化成 1.0000 而改变历史结论。
        assert stored.confidence == 0.999999
        assert stored.decision_confidence == 0.999999
        assert stored.confidence != 1.0
        decision = ConfidenceDecisionDTO(
            confidence=stored.decision_confidence,
            threshold=stored.decision_threshold,
            requires_review=stored.decision_requires_review,
            review_status=stored.decision_review_status.value,
            grading_status=stored.decision_grading_status.value,
            reason=stored.decision_reason,
        )
        assert decision.confidence == 0.999999
        assert decision.threshold == 0.8
        assert decision.requires_review is False
        assert decision.review_status == ReviewStatus.NOT_REQUIRED.value
        assert decision.grading_status == GradingStatus.ACCEPTED.value


def test_pending_review_and_human_final_rows_keep_original_decision() -> None:
    """待复核与人工确认结果都必须保留自动评分时的原始决策快照。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        pending = _grading_result(
            fixture.subjective_answer_id,
            fixture.submission_id,
            confidence=0.42,
            review_status=ReviewStatus.PENDING_REVIEW,
            with_decision=True,
        )
        pending.decision_confidence = 0.42
        pending.decision_threshold = 0.8
        pending.decision_requires_review = True
        pending.decision_review_status = ReviewStatus.PENDING_REVIEW
        session.add(pending)
        session.commit()
        pending_id = pending.id

    with Session(engine) as session:
        stored = session.get(GradingResult, pending_id)
        assert stored is not None
        assert stored.review_status is ReviewStatus.PENDING_REVIEW
        assert stored.decision_requires_review is True
        assert stored.decision_review_status is ReviewStatus.PENDING_REVIEW
        # 教师确认后只更新复核状态，历史决策不被重算。
        stored.review_status = ReviewStatus.CONFIRMED
        session.commit()

    with Session(engine) as session:
        stored = session.get(GradingResult, pending_id)
        assert stored is not None
        assert stored.review_status is ReviewStatus.CONFIRMED
        assert stored.decision_confidence == 0.42
        assert stored.decision_requires_review is True


def test_exam_result_rejects_final_state_inconsistency() -> None:
    """最终状态、结果状态与最终总分必须自洽，不得用 0 分或空值互相冒充。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        inconsistent_final = _exam_result(
            fixture.submission_id,
            fixture.exam_id,
            fixture.student_id,
            is_final=True,
            result_status=ExamResultStatus.FINAL,
            final_total_score=None,
        )
        session.add(inconsistent_final)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        inconsistent_pending = _exam_result(
            fixture.submission_id,
            fixture.exam_id,
            fixture.student_id,
            is_final=False,
            result_status=ExamResultStatus.PENDING_REVIEW,
            final_total_score=Decimal("16.00"),
        )
        session.add(inconsistent_pending)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_exam_result_round_trips_aggregation_facts() -> None:
    """整卷结果保存当次汇总事实：最终总分、已确认小计与整卷满分。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        row = _exam_result(fixture.submission_id, fixture.exam_id, fixture.student_id)
        session.add(row)
        session.commit()
        row_id = row.id

    with Session(engine) as session:
        stored = session.get(ExamResult, row_id)
        assert stored is not None
        assert stored.result_status is ExamResultStatus.FINAL
        assert stored.is_final is True
        assert stored.final_total_score == Decimal("16.00")
        assert stored.confirmed_subtotal == Decimal("16.00")
        assert stored.total_max_score == Decimal("20.00")
        assert stored.aggregated_at is not None
        assert stored.student_id == fixture.student_id


def test_exam_result_grading_results_relationship_is_readonly() -> None:
    """按答卷读取逐题结果的关系必须只读，避免与评分行互相改写。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        session.add(_exam_result(fixture.submission_id, fixture.exam_id, fixture.student_id))
        session.add(
            _grading_result(
                fixture.objective_answer_id,
                fixture.submission_id,
                question_type=QuestionType.SINGLE_CHOICE,
            )
        )
        session.add(
            _grading_result(fixture.subjective_answer_id, fixture.submission_id)
        )
        session.commit()
        result_id = session.scalar(select(ExamResult.id))

    assert ExamResult.grading_results.property.viewonly is True

    with Session(engine) as session:
        stored = session.get(ExamResult, result_id)
        assert stored is not None
        answer_ids = {row.answer_id for row in stored.grading_results}
        assert answer_ids == {
            fixture.objective_answer_id,
            fixture.subjective_answer_id,
        }
        session.commit()

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(GradingResult)) == 2


def test_missing_result_is_absent_and_never_stored_as_zero_row() -> None:
    """缺结果只表现为没有评分行，不得落库为 0 分占位行。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        session.add(
            _grading_result(
                fixture.objective_answer_id,
                fixture.submission_id,
                question_type=QuestionType.SINGLE_CHOICE,
            )
        )
        session.commit()

    with Session(engine) as session:
        rows = session.scalars(select(GradingResult)).all()
        assert len(rows) == 1
        assert rows[0].answer_id == fixture.objective_answer_id
        assert rows[0].score != Decimal("0.00")


def test_deleting_answer_cascades_its_grading_result() -> None:
    """删除答案时其评分结果级联删除，不残留无法归属的结果行。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        session.add(
            _grading_result(fixture.subjective_answer_id, fixture.submission_id)
        )
        session.commit()

        answer = session.get(Answer, fixture.subjective_answer_id)
        assert answer is not None
        session.delete(answer)
        session.commit()

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(GradingResult)) == 0
