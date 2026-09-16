"""T057 诊断持久化测试：真实主键、来源时间、过期判定与失败保真。

TCR（2026-09-16，T057 / B05）：诊断必须复用 T055 计算后落库，``exam_result_id`` 取真实主键，
``source_exam_result_updated_at`` 取当次消费的汇总时间；旧结果生成的报告不得覆盖新结果，
``Not Ready`` 不落库，``Failed`` 保留错误码且不影响已提交成绩。

TCR（B02）：补充保存时成绩已退出最终态的拒绝用例，避免只相信生成时的 DTO 状态。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from backend.app.domain.enums import QuestionType
from backend.app.models import DiagnosisReport, ExamResult
from backend.app.schemas.ai import GradingResult as GradingResultPayload
from backend.app.schemas.grading import (
    DiagnosisReportDTO,
    DiagnosisStatus,
    ExamResultDTO,
    ExamResultStatus,
    MasteryByKnowledgePointDTO,
    SubmissionContext,
    WeakKnowledgePointDTO,
)
from backend.app.services.grading.confidence_policy import ConfidenceDecision
from backend.app.services.grading.diagnosis_report_store import (
    DiagnosisRecorder,
    DiagnosisReportStateError,
    DiagnosisReportStore,
)
from backend.app.services.grading.grading_repository import DatabaseGradingRepository
from backend.app.services.grading.grading_task_service import GradingOutcome
from backend.app.services.grading.result_aggregator import ResultAggregator
from tests.unit.models.sqlite_support import (
    SubmissionFixture,
    create_sqlite_engine,
    seed_submission,
)

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
SUGGESTIONS = ("复习变量的引用方式。",)


class StubDiagnosisService:
    """返回预设诊断结果的替身；不调用 LLM。"""

    def __init__(self, report: DiagnosisReportDTO | None = None) -> None:
        self.report = report
        self.calls: list[ExamResultDTO] = []

    async def generate(self, exam_result: ExamResultDTO) -> DiagnosisReportDTO:
        self.calls.append(exam_result)
        if self.report is not None:
            return self.report
        return _ready_report(exam_result)


@pytest.fixture
def engine() -> Iterator[Engine]:
    """内存 SQLite 引擎（启用外键约束并建好全部业务表）。"""

    active = create_sqlite_engine()
    try:
        yield active
    finally:
        active.dispose()


@pytest.fixture
def fixture(engine: Engine) -> SubmissionFixture:
    """一份含客观题与主观题的最小答卷。"""

    with Session(engine) as session:
        return seed_submission(session)


def _repository(engine: Engine) -> DatabaseGradingRepository:
    return DatabaseGradingRepository(
        session_factory=lambda: Session(engine), clock=lambda: NOW
    )


def _store(engine: Engine) -> DiagnosisReportStore:
    return DiagnosisReportStore(
        session_factory=lambda: Session(engine), clock=lambda: NOW
    )


def _context(fixture: SubmissionFixture) -> SubmissionContext:
    """按考试题序构造汇总上下文。"""

    from backend.app.schemas.grading import ExpectedAnswer

    return SubmissionContext(
        submission_id=str(fixture.submission_id),
        exam_id=str(fixture.exam_id),
        student_id=str(fixture.student_id),
        expected_answers=[
            ExpectedAnswer(
                order=1,
                answer_id=str(fixture.objective_answer_id),
                question_id=str(fixture.objective_question_id),
                question_type=QuestionType.SINGLE_CHOICE,
                max_score=Decimal("10.00"),
                knowledge_points=["数据类型"],
            ),
            ExpectedAnswer(
                order=2,
                answer_id=str(fixture.subjective_answer_id),
                question_id=str(fixture.subjective_question_id),
                question_type=QuestionType.SHORT_ANSWER,
                max_score=Decimal("10.00"),
                knowledge_points=["变量"],
            ),
        ],
    )


def _final_exam_result(
    engine: Engine,
    fixture: SubmissionFixture,
) -> ExamResultDTO:
    """写入一份全部题目可接受的最终整卷结果，并返回该 DTO。"""

    decision = ConfidenceDecision(
        confidence=0.9,
        threshold=0.8,
        requires_review=False,
        review_status="Not Required",
        grading_status="Accepted",
        reason="置信度不低于阈值，自动接受。",
    )
    payloads = (
        GradingResultPayload(
            question_type=QuestionType.SINGLE_CHOICE,
            score=10.0,
            max_score=10.0,
            reason="参考答案一致。",
            correct_points=["tuple 是不可变类型"],
            missing_knowledge_points=[],
            knowledge_points=["数据类型"],
            suggestions=["继续保持。"],
            confidence=1.0,
            validation_status="Validated",
            review_status="Not Required",
            retrieved_context_ids=[],
            answer_id=str(fixture.objective_answer_id),
            submission_id=str(fixture.submission_id),
        ),
        GradingResultPayload(
            question_type=QuestionType.SHORT_ANSWER,
            score=10.0,
            max_score=10.0,
            reason="说明了保存与引用作用。",
            correct_points=["保存数据", "引用数据"],
            missing_knowledge_points=[],
            knowledge_points=["变量"],
            suggestions=["继续保持。"],
            confidence=0.9,
            validation_status="Validated",
            review_status="Not Required",
            retrieved_context_ids=["chunk-1"],
            answer_id=str(fixture.subjective_answer_id),
            submission_id=str(fixture.submission_id),
        ),
    )
    exam_result = ResultAggregator().aggregate(
        _context(fixture),
        results=list(payloads),
        decisions={str(fixture.subjective_answer_id): decision},
    )
    assert exam_result.is_final is True
    _repository(engine).save_outcome(
        str(fixture.submission_id),
        GradingOutcome(
            results=payloads,
            decisions={str(fixture.subjective_answer_id): decision},
            exam_result=exam_result,
        ),
    )
    return exam_result


def _ready_report(exam_result: ExamResultDTO) -> DiagnosisReportDTO:
    """构造 Ready 诊断报告（平台字段 + 建议）。"""

    return DiagnosisReportDTO(
        exam_result_id=f"exam-result:{exam_result.submission_id}",
        submission_id=exam_result.submission_id,
        student_id=exam_result.student_id,
        status=DiagnosisStatus.READY,
        mastery_by_knowledge_point=[
            MasteryByKnowledgePointDTO(
                knowledge_point="变量",
                answered_count=1,
                correct_count=1,
                awarded_score=Decimal("10.00"),
                max_score=Decimal("10.00"),
                mastery=Decimal("1.00"),
            )
        ],
        weak_knowledge_points=[
            WeakKnowledgePointDTO(
                knowledge_point="数据类型",
                reason="掌握度 0.50 低于阈值 0.60。",
                error_count=1,
                awarded_score=Decimal("5.00"),
                max_score=Decimal("10.00"),
                mastery=Decimal("0.50"),
            )
        ],
        error_reasons=["q-1：缺少要点 引用数据（得分 5.00/10.00）。"],
        learning_suggestions=list(SUGGESTIONS),
        insufficient_evidence_answer_ids=[],
        generated_at=NOW,
        source_exam_result_updated_at=exam_result.aggregated_at,
    )


def test_recorder_saves_ready_report_with_real_exam_result_id(
    engine: Engine,
    fixture: SubmissionFixture,
) -> None:
    """诊断按真实主键落库，来源时间取当次消费的汇总时间。"""

    exam_result = _final_exam_result(engine, fixture)
    recorder = DiagnosisRecorder(
        store=_store(engine), service=StubDiagnosisService()
    )

    saved = recorder.record(exam_result)

    assert saved.status is DiagnosisStatus.READY
    assert saved.learning_suggestions == list(SUGGESTIONS)
    with Session(engine) as session:
        row = session.scalars(select(DiagnosisReport)).one()
        result_row = session.scalars(select(ExamResult)).one()
        assert row.exam_result_id == result_row.id
        assert row.source_exam_result_updated_at is not None
        assert row.generated_at is not None
        assert row.mastery_by_knowledge_point[0]["awarded_score"] == "10.00"

    stored = _store(engine).read(str(fixture.submission_id), exam_result)
    assert stored.status is DiagnosisStatus.READY
    assert stored.mastery_by_knowledge_point[0].mastery == Decimal("1.00")
    assert stored.weak_knowledge_points[0].mastery == Decimal("0.50")
    assert stored.exam_result_id is not None


def test_read_returns_stale_when_exam_result_updates(
    engine: Engine,
    fixture: SubmissionFixture,
) -> None:
    """整卷重新汇总后，旧诊断被判定为过期。"""

    exam_result = _final_exam_result(engine, fixture)
    DiagnosisRecorder(store=_store(engine), service=StubDiagnosisService()).record(
        exam_result
    )
    with Session(engine) as session, session.begin():
        row = session.scalars(select(ExamResult)).one()
        row.aggregated_at = exam_result.aggregated_at + timedelta(minutes=5)
        updated_at = row.aggregated_at
    refreshed = exam_result.model_copy(update={"aggregated_at": updated_at})

    stored = _store(engine).read(str(fixture.submission_id), refreshed)

    assert stored.status is DiagnosisStatus.STALE
    assert stored.generated_at is not None
    assert stored.learning_suggestions == list(SUGGESTIONS)


def test_recorder_does_not_store_not_ready_result(
    engine: Engine,
    fixture: SubmissionFixture,
) -> None:
    """非最终成绩不生成、不落库诊断。"""

    exam_result = _final_exam_result(engine, fixture)
    not_final = exam_result.model_copy(
        update={
            "is_final": False,
            "result_status": "Pending Review",
            "final_total_score": None,
        }
    )
    service = StubDiagnosisService(
        report=DiagnosisReportDTO(
            submission_id=not_final.submission_id,
            student_id=not_final.student_id,
            status=DiagnosisStatus.NOT_READY,
            error_code="DIAGNOSIS_NOT_READY",
            retryable=False,
            source_exam_result_updated_at=not_final.aggregated_at,
        )
    )

    saved = DiagnosisRecorder(store=_store(engine), service=service).record(not_final)

    assert saved.status is DiagnosisStatus.NOT_READY
    with Session(engine) as session:
        assert list(session.scalars(select(DiagnosisReport))) == []


def test_save_refuses_to_overwrite_newer_result(
    engine: Engine,
    fixture: SubmissionFixture,
) -> None:
    """旧结果生成的报告不得覆盖新结果，返回 Stale 且不写入。"""

    exam_result = _final_exam_result(engine, fixture)
    store = _store(engine)
    store.save(_ready_report(exam_result))
    with Session(engine) as session, session.begin():
        row = session.scalars(select(ExamResult)).one()
        row.aggregated_at = exam_result.aggregated_at + timedelta(minutes=3)

    stale = store.save(_ready_report(exam_result))

    assert stale.status is DiagnosisStatus.STALE
    with Session(engine) as session:
        row = session.scalars(select(DiagnosisReport)).one()
        _stored_result = session.scalars(select(ExamResult)).one()
        assert row.source_exam_result_updated_at is not None
        assert row.source_exam_result_updated_at.replace(tzinfo=None) == (
            exam_result.aggregated_at.replace(tzinfo=None)
        )


def test_save_rejects_result_no_longer_final(engine: Engine, fixture: SubmissionFixture) -> None:
    exam = _final_exam_result(engine, fixture)
    with Session(engine) as session, session.begin():
        row = session.scalars(select(ExamResult)).one()
        row.is_final = False
        row.result_status = ExamResultStatus.PENDING_REVIEW
        row.final_total_score = None
    with pytest.raises(DiagnosisReportStateError):
        _store(engine).save(_ready_report(exam))
    with Session(engine) as session:
        assert session.scalars(select(DiagnosisReport)).all() == []


def test_failed_report_keeps_error_code_and_scores(
    engine: Engine,
    fixture: SubmissionFixture,
) -> None:
    """诊断失败按 Failed 落库并保留错误码，已提交成绩不受影响。"""

    exam_result = _final_exam_result(engine, fixture)
    failed = _ready_report(exam_result).model_copy(
        update={
            "status": DiagnosisStatus.FAILED,
            "learning_suggestions": [],
            "error_code": "DIAGNOSIS_PROVIDER_NOT_READY",
            "retryable": False,
            "generated_at": NOW,
        }
    )
    recorder = DiagnosisRecorder(
        store=_store(engine), service=StubDiagnosisService(report=failed)
    )

    saved = recorder.record(exam_result)

    assert saved.status is DiagnosisStatus.FAILED
    with Session(engine) as session:
        row = session.scalars(select(DiagnosisReport)).one()
        assert row.error_code == "DIAGNOSIS_PROVIDER_NOT_READY"
        assert session.scalars(select(ExamResult)).one().is_final is True

    stored = _store(engine).read(str(fixture.submission_id), exam_result)
    assert stored.status is DiagnosisStatus.FAILED
    assert stored.error_code == "DIAGNOSIS_PROVIDER_NOT_READY"


def test_read_without_report_returns_not_ready_without_write(
    engine: Engine,
    fixture: SubmissionFixture,
) -> None:
    """未生成报告时返回明确空态，且读取不写库。"""

    exam_result = _final_exam_result(engine, fixture)

    stored = _store(engine).read(str(fixture.submission_id), exam_result)

    assert stored.status is DiagnosisStatus.NOT_READY
    assert stored.error_code == "DIAGNOSIS_NOT_READY"
    assert stored.source_exam_result_updated_at == exam_result.aggregated_at
    with Session(engine) as session:
        assert list(session.scalars(select(DiagnosisReport))) == []


def test_read_without_exam_result_returns_not_ready(engine: Engine) -> None:
    """尚无整卷结果时同样返回未就绪空态，不抛异常。"""

    with Session(engine) as session:
        fixture = seed_submission(session)

    stored = _store(engine).read(str(fixture.submission_id), None)

    assert stored.status is DiagnosisStatus.NOT_READY
    assert stored.source_exam_result_updated_at is None
