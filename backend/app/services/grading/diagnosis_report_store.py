"""T057 诊断报告持久化与只读读取（T061 模型）。

本模块把 T055 :class:`~backend.app.services.diagnosis_service.DiagnosisService` 的计算结果
落到 ``diagnosis_reports`` 表，并提供**只读**读取；不重复实现 T054/T055 的汇总与诊断逻辑。

契约要点：

- **真实主键**：``exam_result_id`` 由 ``submission_id`` 在库中解析真实 ``ExamResult.id``，
  不使用 ``DiagnosisService`` 返回的 ``exam-result:{submission_id}`` 应用层标识。
- **来源时间**：``source_exam_result_updated_at`` 保存生成时消费的
  ``ExamResultDTO.aggregated_at``；读取时与当前 ``ExamResult.aggregated_at`` 比较，
  不一致返回 ``Stale``，**不允许旧结果生成的报告覆盖新结果**（写入前同样校验）。
- **Not Ready 不落库**：整卷尚未形成最终成绩时 ``DiagnosisService`` 返回 ``Not Ready``，
  该状态没有可落库的最终结果，只在响应中返回。
- **失败保留成绩**：诊断生成失败（``Failed``）时如实记录错误码与 ``retryable``，
  已提交的成绩与汇总事实不受影响；GET 路径永远只读，不触发生成与 LLM 调用。
- JSON 中的 Decimal（掌握度、得分）按既有约定编码为定点字符串（两位小数），读回后仍是
  ``Decimal``，不用浮点近似值掩盖精度。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.models import DiagnosisReport, ExamResult, Submission
from backend.app.schemas.grading import (
    DiagnosisReportDTO,
    DiagnosisStatus,
    ExamResultDTO,
    MasteryByKnowledgePointDTO,
    WeakKnowledgePointDTO,
)
from backend.app.services.diagnosis_service import (
    DIAGNOSIS_NOT_READY,
    DiagnosisService,
)
from backend.app.services.grading.grading_task_service import (
    GradingSubmissionNotFoundError,
    GradingTaskError,
)

#: 诊断读写未接通（表缺失或不可连接）。
DIAGNOSIS_STORE_NOT_READY: Final[str] = "DIAGNOSIS_STORE_NOT_READY"
#: 待写入的报告状态不允许落库。
DIAGNOSIS_REPORT_STATE_NOT_STORABLE: Final[str] = "DIAGNOSIS_REPORT_STATE_NOT_STORABLE"

#: 可以落库的诊断状态；``Not Ready`` 没有最终结果，``Stale`` 由读取时判定。
STORABLE_STATUSES: Final[frozenset[DiagnosisStatus]] = frozenset(
    {DiagnosisStatus.READY, DiagnosisStatus.FAILED}
)


class DiagnosisStoreNotReadyError(GradingTaskError):
    """诊断持久化未就绪（表缺失或数据库不可连接）。"""

    error_code = DIAGNOSIS_STORE_NOT_READY


class DiagnosisReportStateError(GradingTaskError):
    """尝试落库不可持久化的诊断状态。"""

    error_code = DIAGNOSIS_REPORT_STATE_NOT_STORABLE


class DiagnosisReportStore:
    """诊断报告读写存储；每次调用自建并关闭会话。

    :param session_factory: 会话工厂；不复用请求作用域会话。
    :param clock: 时间来源，便于测试固定时间。
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    @contextmanager
    def _use_session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
        finally:
            session.close()

    def read(
        self,
        submission_id: str,
        exam_result: ExamResultDTO | None,
    ) -> DiagnosisReportDTO:
        """读取该答卷的诊断报告；不产生写入与 LLM 调用。

        - 无整卷结果或无报告：返回 ``Not Ready`` 空态；
        - 报告存在但来源汇总时间与当前 ``ExamResult`` 不一致：返回 ``Stale``；
        - 其余情况按落库状态返回（``Ready`` / ``Failed``）。
        """

        if exam_result is None:
            with self._use_session() as session:
                return _not_ready(
                    submission_id,
                    self._student_id(session, _as_uuid(submission_id)),
                    None,
                )
        try:
            with self._use_session() as session:
                submission_uuid = _as_uuid(submission_id)
                row = self._latest_row(session, submission_uuid)
                current = self._exam_result_row(session, submission_uuid)
        except SQLAlchemyError as error:
            raise DiagnosisStoreNotReadyError(
                f"诊断存储不可用（{type(error).__name__}）。",
                retryable=False,
            ) from None
        if row is None or current is None:
            return _not_ready(
                submission_id, exam_result.student_id, exam_result.aggregated_at
            )
        return _report_from_row(
            row,
            current_aggregated_at=current.aggregated_at,
            exam_result=exam_result,
        )

    def save(self, report: DiagnosisReportDTO) -> DiagnosisReportDTO:
        """写入 ``Ready``/``Failed`` 报告；来源过期时拒绝覆盖新结果。

        报告中由 :class:`DiagnosisService` 填写的应用层 ``exam_result_id`` 在此替换为真实
        主键；若库中当前结果已更新（``aggregated_at`` 变化），返回 ``Stale`` 状态且不写入。
        """

        if report.status not in STORABLE_STATUSES:
            raise DiagnosisReportStateError(
                f"诊断状态 {report.status} 不可落库（Not Ready 没有最终结果）。"
            )
        try:
            with self._use_session() as session, session.begin():
                submission_uuid = _as_uuid(report.submission_id)
                current = self._exam_result_row(session, submission_uuid)
                if current is None:
                    raise GradingSubmissionNotFoundError(
                        f"答卷 {report.submission_id} 尚无整卷结果，拒绝写入诊断报告。"
                    )
                source_matches = (
                    report.source_exam_result_updated_at is not None
                    and _normalize(report.source_exam_result_updated_at)
                    == _normalize(current.aggregated_at)
                )
                if not source_matches:
                    return _stale_from_row(
                        self._latest_row(session, submission_uuid),
                        current_aggregated_at=current.aggregated_at,
                        submission_id=report.submission_id,
                        student_id=report.student_id,
                    )
                row = self._latest_row(session, submission_uuid)
                if row is None or row.exam_result_id != current.id:
                    row = DiagnosisReport(
                        exam_result_id=current.id,
                        submission_id=submission_uuid,
                        student_id=current.student_id,
                        status=report.status,
                        generated_at=self._clock(),
                    )
                    session.add(row)
                self._apply(row, report, exam_result_id=current.id)
                session.flush()
                return _report_from_row(
                    row,
                    current_aggregated_at=current.aggregated_at,
                    exam_result=None,
                )
        except SQLAlchemyError as error:
            raise DiagnosisStoreNotReadyError(
                f"诊断存储不可用（{type(error).__name__}）。",
                retryable=False,
            ) from None

    def _apply(
        self,
        row: DiagnosisReport,
        report: DiagnosisReportDTO,
        *,
        exam_result_id: UUID,
    ) -> None:
        """把 DTO 写入模型行；Decimal 统一编码为定点字符串。"""

        row.exam_result_id = exam_result_id
        row.status = report.status
        row.mastery_by_knowledge_point = [
            item.model_dump(mode="json") for item in report.mastery_by_knowledge_point
        ]
        row.weak_knowledge_points = [
            item.model_dump(mode="json") for item in report.weak_knowledge_points
        ]
        row.error_reasons = list(report.error_reasons)
        row.learning_suggestions = list(report.learning_suggestions)
        row.insufficient_evidence_answer_ids = list(
            report.insufficient_evidence_answer_ids
        )
        row.error_code = report.error_code
        row.retryable = report.retryable
        row.source_code = report.source_code
        row.attempt_count = report.attempt_count
        row.generated_at = report.generated_at or self._clock()
        row.source_exam_result_updated_at = report.source_exam_result_updated_at

    @staticmethod
    def _latest_row(session: Session, submission_id: UUID) -> DiagnosisReport | None:
        """返回该答卷最近一条诊断报告；一次答卷保留一条当前报告。"""

        return session.scalars(
            select(DiagnosisReport)
            .where(DiagnosisReport.submission_id == submission_id)
            .order_by(DiagnosisReport.created_at.desc())
        ).first()

    @staticmethod
    def _student_id(session: Session, submission_id: UUID) -> str:
        """返回答卷所属学生标识；答卷不存在时显式失败。"""

        student_id = session.scalars(
            select(Submission.student_id).where(Submission.id == submission_id)
        ).one_or_none()
        if student_id is None:
            raise GradingSubmissionNotFoundError(
                f"答卷 {submission_id} 不存在，无法读取诊断。"
            )
        return str(student_id)

    @staticmethod
    def _exam_result_row(session: Session, submission_id: UUID) -> ExamResult | None:
        return session.scalars(
            select(ExamResult).where(ExamResult.submission_id == submission_id)
        ).one_or_none()


class DiagnosisRecorder:
    """最终整卷结果提交成功后的诊断生成与保存入口（B05）。

    :param store: 诊断读写存储。
    :param service: T055 诊断服务；复用其平台计算与校验，不改其逻辑。
    """

    def __init__(self, *, store: DiagnosisReportStore, service: DiagnosisService) -> None:
        self._store = store
        self._service = service

    def record(self, exam_result: ExamResultDTO) -> DiagnosisReportDTO:
        """生成并保存最终成绩对应的诊断报告。

        非最终结果不生成、不落库（返回 ``Not Ready``）；生成失败时按 ``Failed`` 落库，
        **已提交的成绩与汇总事实不受影响**。
        """

        report = asyncio.run(self._service.generate(exam_result))
        if report.status not in STORABLE_STATUSES:
            return report
        return self._store.save(report)


def _not_ready(
    submission_id: str,
    student_id: str,
    aggregated_at: datetime | None,
) -> DiagnosisReportDTO:
    """构造 ``Not Ready`` 空态；不冒充成功、不落库。"""

    return DiagnosisReportDTO(
        submission_id=submission_id,
        student_id=student_id,
        status=DiagnosisStatus.NOT_READY,
        error_code=DIAGNOSIS_NOT_READY,
        retryable=False,
        source_exam_result_updated_at=aggregated_at,
    )


def _report_from_row(
    row: DiagnosisReport,
    *,
    current_aggregated_at: datetime,
    exam_result: ExamResultDTO | None,
) -> DiagnosisReportDTO:
    """把诊断行还原为 DTO；来源过期时返回 ``Stale``。"""

    stale = (
        row.source_exam_result_updated_at is None
        or _normalize(row.source_exam_result_updated_at)
        != _normalize(current_aggregated_at)
    )
    return DiagnosisReportDTO(
        exam_result_id=str(row.exam_result_id),
        submission_id=str(row.submission_id),
        student_id=str(row.student_id),
        status=DiagnosisStatus.STALE if stale else row.status,
        mastery_by_knowledge_point=[
            MasteryByKnowledgePointDTO.model_validate(item)
            for item in row.mastery_by_knowledge_point
        ],
        weak_knowledge_points=[
            WeakKnowledgePointDTO.model_validate(item)
            for item in row.weak_knowledge_points
        ],
        error_reasons=list(row.error_reasons),
        learning_suggestions=list(row.learning_suggestions),
        insufficient_evidence_answer_ids=list(row.insufficient_evidence_answer_ids),
        generated_at=row.generated_at,
        source_exam_result_updated_at=row.source_exam_result_updated_at,
        error_code=row.error_code,
        retryable=row.retryable,
        source_code=row.source_code,
        attempt_count=row.attempt_count,
    )


def _stale_from_row(
    row: DiagnosisReport | None,
    *,
    current_aggregated_at: datetime,
    submission_id: str,
    student_id: str,
) -> DiagnosisReportDTO:
    """拒绝覆盖时的返回：保留已有报告事实并标记 ``Stale``。"""

    if row is None:
        return DiagnosisReportDTO(
            submission_id=submission_id,
            student_id=student_id,
            status=DiagnosisStatus.STALE,
            source_exam_result_updated_at=current_aggregated_at,
            generated_at=current_aggregated_at,
        )
    return _report_from_row(
        row,
        current_aggregated_at=current_aggregated_at,
        exam_result=None,
    )


def _normalize(value: datetime) -> datetime:
    """统一到 UTC 后比较；SQLite 读回的时间可能是 naive。"""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _as_uuid(value: str) -> UUID:
    """把字符串标识转换为 UUID；非法标识按未找到处理。"""

    try:
        return UUID(value)
    except ValueError as error:
        raise GradingSubmissionNotFoundError("答卷标识非法。") from error


__all__ = [
    "DIAGNOSIS_STORE_NOT_READY",
    "STORABLE_STATUSES",
    "DiagnosisRecorder",
    "DiagnosisReportStateError",
    "DiagnosisReportStore",
    "DiagnosisStoreNotReadyError",
]
