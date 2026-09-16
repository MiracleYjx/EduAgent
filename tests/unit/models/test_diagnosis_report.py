"""T061 DiagnosisReport 模型单元测试。

覆盖范围：表结构与约束名、状态一致性 CHECK（Ready / Failed / Stale / Not Ready 边界）、
失败信息字段的可空性、JSON 内 Decimal 的定点字符串编码、跨 Session 读回、
必须关联真实 `ExamResult`、来源外键的删除行为与来源时间失效判定。

测试运行在启用外键约束的内存 SQLite 上；PostgreSQL 侧的列精度、约束与外键行为由
`alembic upgrade/check` 与一次性验证库上的 pg_catalog 断言覆盖。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Enum as SAEnum
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.core.database import Base
from backend.app.models import DiagnosisReport, ExamResult
from backend.app.schemas.grading import DiagnosisStatus, ExamResultStatus
from tests.unit.models.sqlite_support import (
    SubmissionFixture,
    create_sqlite_engine,
    seed_submission,
)

DIAGNOSIS_COLUMNS = [
    "exam_result_id",
    "submission_id",
    "student_id",
    "status",
    "mastery_by_knowledge_point",
    "weak_knowledge_points",
    "error_reasons",
    "learning_suggestions",
    "insufficient_evidence_answer_ids",
    "error_code",
    "retryable",
    "source_code",
    "attempt_count",
    "generated_at",
    "source_exam_result_updated_at",
    "id",
    "created_at",
    "updated_at",
]

DIAGNOSIS_CONSTRAINTS = {
    "diagnosis_status",
    "ck_diagnosis_reports_attempt_count_non_negative",
    "ck_diagnosis_reports_status_consistency",
}

#: 汇总时间基准：报告生成时消费的 aggregated_at。
CONSUMED_AT = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


def _exam_result(
    fixture: SubmissionFixture,
    *,
    aggregated_at: datetime = CONSUMED_AT,
) -> ExamResult:
    """构造一条最终成绩的整卷结果，供诊断报告关联。"""

    return ExamResult(
        submission_id=fixture.submission_id,
        exam_id=fixture.exam_id,
        student_id=fixture.student_id,
        result_status=ExamResultStatus.FINAL,
        is_final=True,
        final_total_score=Decimal("16.00"),
        confirmed_subtotal=Decimal("16.00"),
        total_max_score=Decimal("20.00"),
        aggregated_at=aggregated_at,
    )


def _report(
    fixture: SubmissionFixture,
    exam_result_id: UUID,
    *,
    status: DiagnosisStatus = DiagnosisStatus.READY,
    generated_at: datetime | None = CONSUMED_AT,
    error_code: str | None = None,
    retryable: bool | None = None,
    source_code: str | None = None,
    attempt_count: int | None = None,
    source_exam_result_updated_at: datetime | None = None,
) -> DiagnosisReport:
    """构造一条诊断报告；默认是 Ready 且不带任何失败信息。"""

    return DiagnosisReport(
        exam_result_id=exam_result_id,
        submission_id=fixture.submission_id,
        student_id=fixture.student_id,
        status=status,
        mastery_by_knowledge_point=[
            {
                "knowledge_point": "变量",
                "answered_count": 2,
                "correct_count": 1,
                "awarded_score": "8.00",
                "max_score": "20.00",
                "mastery": "0.40",
            }
        ],
        weak_knowledge_points=[
            {
                "knowledge_point": "变量",
                "reason": "掌握度 0.40 低于阈值 0.60",
                "error_count": 1,
                "awarded_score": "8.00",
                "max_score": "20.00",
                "mastery": "0.40",
            }
        ],
        error_reasons=["作用域说明缺失。"],
        learning_suggestions=["复习变量作用域。"],
        insufficient_evidence_answer_ids=[],
        error_code=error_code,
        retryable=retryable,
        source_code=source_code,
        attempt_count=attempt_count,
        generated_at=generated_at,
        source_exam_result_updated_at=source_exam_result_updated_at,
    )


def _seed_report(
    session: Session,
    *,
    aggregated_at: datetime = CONSUMED_AT,
    **report_kwargs: object,
) -> tuple[SubmissionFixture, ExamResult, UUID]:
    """落库一条整卷结果与一条诊断报告，返回 fixture、整卷结果与报告标识。"""

    fixture = seed_submission(session)
    exam_result = _exam_result(fixture, aggregated_at=aggregated_at)
    session.add(exam_result)
    session.commit()
    report = _report(fixture, exam_result.id, **report_kwargs)  # type: ignore[arg-type]
    session.add(report)
    session.commit()
    return fixture, exam_result, report.id


def test_table_structure_matches_plan() -> None:
    """列集合、索引与约束名必须与 plan §5.2 的 DiagnosisReport 定义一致。"""

    table = Base.metadata.tables["diagnosis_reports"]

    assert [column.name for column in table.columns] == DIAGNOSIS_COLUMNS
    assert {index.name for index in table.indexes} == {
        "ix_diagnosis_reports_exam_result_id",
        "ix_diagnosis_reports_submission_id",
        "ix_diagnosis_reports_student_id",
        "ix_diagnosis_reports_student_generated",
    }
    assert DIAGNOSIS_CONSTRAINTS.issubset(
        {constraint.name for constraint in table.constraints}
    )
    assert all("uq_" not in str(constraint.name) for constraint in table.constraints)


def test_status_column_reuses_diagnosis_status_enum() -> None:
    """诊断状态必须复用 schemas 中的 DiagnosisStatus，不新增重复枚举。"""

    status_type = inspect(DiagnosisReport).columns.status.type

    assert isinstance(status_type, SAEnum)
    assert status_type.enums == [item.value for item in DiagnosisStatus]


def test_foreign_key_delete_behavior_is_explicit() -> None:
    """来源外键必须显式声明目标与删除行为，不依赖数据库默认。"""

    table = Base.metadata.tables["diagnosis_reports"]

    assert {fk.parent.name: fk.ondelete for fk in table.foreign_keys} == {
        "exam_result_id": "CASCADE",
        "submission_id": "CASCADE",
        "student_id": "RESTRICT",
    }
    assert {fk.target_fullname for fk in table.foreign_keys} == {
        "exam_results.id",
        "submissions.id",
        "users.id",
    }


def test_ready_report_round_trips_full_payload() -> None:
    """Ready 报告的全部字段（含平台计算字段与来源时间）必须跨 Session 完整读回。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture, exam_result, report_id = _seed_report(
            session,
            status=DiagnosisStatus.READY,
            source_exam_result_updated_at=CONSUMED_AT,
        )
        exam_result_id = exam_result.id

    with Session(engine) as session:
        stored = session.get(DiagnosisReport, report_id)
        assert stored is not None
        assert stored.exam_result_id == exam_result_id
        assert stored.submission_id == fixture.submission_id
        assert stored.student_id == fixture.student_id
        assert stored.status is DiagnosisStatus.READY
        assert stored.error_code is None
        assert stored.retryable is None
        assert stored.source_code is None
        assert stored.attempt_count is None
        assert stored.generated_at == CONSUMED_AT.replace(tzinfo=None)
        assert stored.source_exam_result_updated_at == CONSUMED_AT.replace(
            tzinfo=None
        )
        assert stored.mastery_by_knowledge_point[0]["knowledge_point"] == "变量"
        assert stored.weak_knowledge_points[0]["error_count"] == 1
        assert stored.error_reasons == ["作用域说明缺失。"]
        assert stored.learning_suggestions == ["复习变量作用域。"]
        assert stored.insufficient_evidence_answer_ids == []


def test_mastery_decimals_are_stored_as_fixed_point_strings() -> None:
    """JSON 内的 Decimal 以定点字符串保存，读回仍是字符串而不是浮点近似值。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        _, _, report_id = _seed_report(
            session, source_exam_result_updated_at=CONSUMED_AT
        )

    with Session(engine) as session:
        stored = session.get(DiagnosisReport, report_id)
        assert stored is not None
        mastery = stored.mastery_by_knowledge_point[0]["mastery"]
        awarded = stored.mastery_by_knowledge_point[0]["awarded_score"]
        assert mastery == "0.40"
        assert awarded == "8.00"
        assert isinstance(mastery, str)
        assert not isinstance(mastery, float)


def test_failed_report_requires_error_code_and_round_trips() -> None:
    """Failed 报告必须携带 error_code，并与重试信息一起读回。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        _, _, report_id = _seed_report(
            session,
            status=DiagnosisStatus.FAILED,
            error_code="DIAGNOSIS_PROVIDER_FAILED",
            retryable=True,
            source_code="rate_limit_exceeded",
            attempt_count=2,
        )

    with Session(engine) as session:
        stored = session.get(DiagnosisReport, report_id)
        assert stored is not None
        assert stored.status is DiagnosisStatus.FAILED
        assert stored.error_code == "DIAGNOSIS_PROVIDER_FAILED"
        assert stored.retryable is True
        assert stored.source_code == "rate_limit_exceeded"
        assert stored.attempt_count == 2


def test_stale_report_keeps_generated_at() -> None:
    """Stale 报告表示旧报告已过期，但仍必须保留生成时间。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        _, _, report_id = _seed_report(
            session, status=DiagnosisStatus.STALE
        )

    with Session(engine) as session:
        stored = session.get(DiagnosisReport, report_id)
        assert stored is not None
        assert stored.status is DiagnosisStatus.STALE
        assert stored.generated_at is not None
        assert stored.source_exam_result_updated_at is None


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param(
            {"status": DiagnosisStatus.FAILED},
            id="failed-without-error-code",
        ),
        pytest.param(
            {
                "status": DiagnosisStatus.READY,
                "error_code": "DIAGNOSIS_PROVIDER_FAILED",
            },
            id="ready-with-error-code",
        ),
        pytest.param(
            {"status": DiagnosisStatus.READY, "generated_at": None},
            id="ready-without-generated-at",
        ),
        pytest.param(
            {"status": DiagnosisStatus.STALE, "generated_at": None},
            id="stale-without-generated-at",
        ),
        pytest.param(
            {"status": DiagnosisStatus.NOT_READY},
            id="not-ready-never-persisted",
        ),
    ],
)
def test_status_consistency_constraint_rejects_invalid_reports(
    overrides: dict[str, object],
) -> None:
    """状态与错误码、生成时间必须自洽；Not Ready 不落库，失败不得伪装成就绪。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        exam_result = _exam_result(fixture)
        session.add(exam_result)
        session.commit()

        session.add(_report(fixture, exam_result.id, **overrides))  # type: ignore[arg-type]
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_negative_attempt_count_is_rejected() -> None:
    """尝试次数是计数事实，不接受负数。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        exam_result = _exam_result(fixture)
        session.add(exam_result)
        session.commit()

        session.add(
            _report(
                fixture,
                exam_result.id,
                status=DiagnosisStatus.FAILED,
                error_code="DIAGNOSIS_PROVIDER_FAILED",
                attempt_count=-1,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_report_requires_existing_exam_result() -> None:
    """exam_result_id 必须指向真实整卷结果，不能用应用层标识或凭空 UUID 冒充。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        fixture = seed_submission(session)
        session.add(_report(fixture, uuid4()))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_deleting_exam_result_cascades_its_reports() -> None:
    """删除整卷结果时其诊断报告级联删除，不残留失去来源的报告。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        _, exam_result, _ = _seed_report(
            session, source_exam_result_updated_at=CONSUMED_AT
        )
        exam_result_id = exam_result.id

    with Session(engine) as session:
        stored = session.get(ExamResult, exam_result_id)
        assert stored is not None
        session.delete(stored)
        session.commit()

    with Session(engine) as session:
        assert (
            session.scalar(select(func.count()).select_from(DiagnosisReport)) == 0
        )


def test_source_timestamp_tracks_consumed_aggregation_time() -> None:
    """来源时间必须保存生成时消费的 aggregated_at；汇总时间变化后旧报告即过期。"""

    engine = create_sqlite_engine()
    with Session(engine) as session:
        _, exam_result, report_id = _seed_report(
            session, source_exam_result_updated_at=CONSUMED_AT
        )
        exam_result_id = exam_result.id

    with Session(engine) as session:
        stored = session.get(ExamResult, exam_result_id)
        assert stored is not None
        stored.aggregated_at = CONSUMED_AT + timedelta(minutes=5)
        session.commit()

    with Session(engine) as session:
        report = session.get(DiagnosisReport, report_id)
        exam_result = session.get(ExamResult, exam_result_id)
        assert report is not None
        assert exam_result is not None
        # 报告保留被消费的那一次汇总时间，而不是 ORM updated_at。
        assert report.source_exam_result_updated_at == CONSUMED_AT.replace(
            tzinfo=None
        )
        assert exam_result.aggregated_at > report.source_exam_result_updated_at
