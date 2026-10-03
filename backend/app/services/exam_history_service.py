"""Explicit historical examination audit and evidence-based reconciliation.

A current administrator who also owns the course as a teacher may confirm actual
historical evidence. No current Question value is a source for historical facts.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.core.maintenance import require_admin_user
from backend.app.core.security import get_user_roles
from backend.app.domain.enums import ExamStatus, UserRole
from backend.app.models import Answer, Course, Exam, Question, Submission
from backend.app.models.exam_question import ExamQuestion
from backend.app.models.grading_result import GradingResult
from backend.app.schemas.exam_scoring import (
    Amount,
    ScoringBasis,
    ScoringConfirmation,
    money_text,
)
from backend.app.services.exam_scoring_rules import validate_scoring_basis

EXAM_HISTORY_COMMIT_UNKNOWN = "EXAM_HISTORY_COMMIT_UNKNOWN"

_FACT_FIELDS = ("score", "base_score", "published_knowledge_points", "scoring_basis")
_EVIDENCE_FIELDS = {"order_index", *_FACT_FIELDS}


class ExamHistoryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class HistoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HistoryEvidenceFile(HistoryModel):
    id: str = Field(min_length=1)
    path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("id")
    @classmethod
    def nonblank_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("证据标识不能为空。")
        return value.strip()


class HistoricalQuestionFacts(HistoryModel):
    question_id: UUID
    order_index: StrictInt = Field(ge=1)
    score: Amount
    base_score: Amount
    published_knowledge_points: list[str]
    scoring_basis: ScoringBasis
    evidence_refs: dict[str, list[str]]

    @field_validator("published_knowledge_points")
    @classmethod
    def validate_points(cls, value: list[str]) -> list[str]:
        if any(not point.strip() or point != point.strip() for point in value):
            raise ValueError(
                "历史知识点须为已核对的非空规范标签；真实无标签可用空数组。"
            )
        if len(set(value)) != len(value):
            raise ValueError("历史发布知识点不能重复。")
        return value

    @model_validator(mode="after")
    def validate_evidence_and_identity(self) -> Self:
        if self.scoring_basis.preparation_id is not None:
            raise ValueError("历史清单不能伪填当前准备轮次 preparation_id。")
        if self.scoring_basis.confirmation is not None:
            raise ValueError(
                "清单不能指定教师或确认时间；服务记录当前真实核对身份和时间。"
            )
        if set(self.evidence_refs) != _EVIDENCE_FIELDS:
            raise ValueError("题序和每项本场评分依据均须指定真实证据。")
        if any(
            not refs or any(not ref.strip() for ref in refs)
            for refs in self.evidence_refs.values()
        ):
            raise ValueError("证据引用不能为空。")
        return self


class HistoricalExamFacts(HistoryModel):
    exam_id: UUID
    reason: str = Field(min_length=1)
    questions: list[HistoricalQuestionFacts] = Field(min_length=1)

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("当前历史核对须给出真实说明。")
        return value.strip()

    @model_validator(mode="after")
    def validate_question_set(self) -> Self:
        if len({row.question_id for row in self.questions}) != len(self.questions):
            raise ValueError("同场题目不能重复。")
        if sorted(row.order_index for row in self.questions) != list(
            range(1, len(self.questions) + 1)
        ):
            raise ValueError("完整题序须连续为 1..题数。")
        return self


class ExamHistoryManifest(HistoryModel):
    format_version: Literal[1]
    evidence_files: list[HistoryEvidenceFile] = Field(min_length=1)
    exams: list[HistoricalExamFacts] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        evidence_ids = {item.id for item in self.evidence_files}
        if len(evidence_ids) != len(self.evidence_files):
            raise ValueError("证据文件标识不能重复。")
        if len({row.exam_id for row in self.exams}) != len(self.exams):
            raise ValueError("考试不能重复。")
        for exam in self.exams:
            for row in exam.questions:
                if any(
                    not set(refs).issubset(evidence_ids)
                    for refs in row.evidence_refs.values()
                ):
                    raise ValueError("清单引用了未登记的证据文件。")
        return self


class HistoricalExamObservation(HistoryModel):
    exam_id: UUID
    exam_status: str
    status: str
    questions: list[dict[str, Any]]


class ExamHistoryReport(HistoryModel):
    operation_id: UUID = Field(default_factory=uuid4)
    operation: Literal["audit", "apply"]
    actor_id: UUID
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    exams: list[HistoricalExamObservation]
    evidence_manifest: dict[str, Any] | None = None
    notice: str = (
        "评分行仅是运行时证据；完整历史依据须显式核对。当前核对时间不是旧发布时间或批准时间。"
        "证据摘要只证明本次文件完整性，历史真实性由当前核对者负责；本工具不重评或改写旧结果。"
    )


class ExamHistoryService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _clean_session(self) -> None:
        if self.session.new or self.session.dirty or self.session.deleted:
            raise ExamHistoryError(
                "EXAM_HISTORY_PENDING_CHANGES", "历史工具需要没有其他待写入修改的会话。"
            )

    def audit(
        self, *, actor_id: UUID, exam_ids: list[UUID] | None = None
    ) -> ExamHistoryReport:
        """Only read persisted fields and runtime evidence; never infer missing facts."""
        self._clean_session()
        require_admin_user(self.session, actor_id)
        query = select(Exam).order_by(Exam.id)
        if exam_ids is not None:
            query = query.where(Exam.id.in_(exam_ids))
        exams = list(self.session.scalars(query))
        if exam_ids is not None and {row.id for row in exams} != set(exam_ids):
            raise ExamHistoryError("EXAM_HISTORY_NOT_FOUND", "指定考试不存在。")
        return ExamHistoryReport(
            operation="audit",
            actor_id=actor_id,
            exams=[self._observe(exam) for exam in exams],
        )

    def _observe(self, exam: Exam) -> HistoricalExamObservation:
        links = list(
            self.session.scalars(
                select(ExamQuestion)
                .where(ExamQuestion.exam_id == exam.id)
                .order_by(ExamQuestion.order_index)
            )
        )
        rows: list[dict[str, Any]] = []
        for link in links:
            grades = self.session.scalars(
                select(GradingResult)
                .join(Answer, Answer.id == GradingResult.answer_id)
                .join(Submission, Submission.id == Answer.submission_id)
                .where(
                    Submission.exam_id == exam.id,
                    Answer.question_id == link.question_id,
                )
                .order_by(GradingResult.id)
            )
            rows.append(
                {
                    "exam_question_id": str(link.id),
                    "question_id": str(link.question_id),
                    "order_index": link.order_index,
                    "score": money_text(link.score) if link.score is not None else None,
                    "base_score": (
                        money_text(link.base_score)
                        if link.base_score is not None
                        else None
                    ),
                    "published_knowledge_points": link.published_knowledge_points,
                    "scoring_basis": link.scoring_basis,
                    "missing_fields": [
                        name for name in _FACT_FIELDS if getattr(link, name) is None
                    ],
                    "grading_evidence": [
                        {
                            "grading_result_id": str(grade.id),
                            "submission_id": str(grade.submission_id),
                            "max_score": money_text(grade.max_score),
                            "knowledge_points": grade.knowledge_points,
                            "recorded_at": grade.created_at.isoformat(),
                            "meaning": "runtime_evidence_only",
                        }
                        for grade in grades
                    ],
                }
            )
        status = (
            "history_unknown"
            if not rows or any(row["missing_fields"] for row in rows)
            else "fixed_facts_present"
        )
        return HistoricalExamObservation(
            exam_id=exam.id,
            exam_status=exam.status.value,
            status=status,
            questions=rows,
        )

    @staticmethod
    def _check_evidence(manifest: ExamHistoryManifest) -> None:
        for evidence in manifest.evidence_files:
            try:
                if not evidence.path.is_file():
                    raise OSError("not a regular file")
                with evidence.path.open("rb") as stream:
                    actual = hashlib.file_digest(stream, "sha256").hexdigest()
            except OSError as error:
                raise ExamHistoryError(
                    "EXAM_HISTORY_EVIDENCE_MISSING", f"证据文件不可读：{evidence.id}。"
                ) from error
            if actual != evidence.sha256:
                raise ExamHistoryError(
                    "EXAM_HISTORY_EVIDENCE_MISMATCH", f"证据文件已变化：{evidence.id}。"
                )

    def apply(
        self, *, actor_id: UUID, manifest: ExamHistoryManifest
    ) -> ExamHistoryReport:
        """Commit complete explicit facts atomically; retain conflicting/unknown history."""
        self._clean_session()
        actor = require_admin_user(self.session, actor_id)
        if UserRole.TEACHER not in get_user_roles(actor):
            raise ExamHistoryError(
                "EXAM_HISTORY_FORBIDDEN",
                "历史核对还需要当前账户具备教师角色及目标课程权限。",
            )
        self._check_evidence(manifest)
        observed: list[HistoricalExamObservation] = []
        checked_at = datetime.now(UTC)
        commit_started = False
        try:
            # Canonical exam order also prevents multi-exam callers taking locks inversely.
            for facts in sorted(manifest.exams, key=lambda row: str(row.exam_id)):
                exam = self.session.scalar(
                    select(Exam)
                    .where(Exam.id == facts.exam_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if exam is None:
                    raise ExamHistoryError("EXAM_HISTORY_NOT_FOUND", "指定考试不存在。")
                course = self.session.get(Course, exam.course_id)
                if course is None or course.created_by != actor_id:
                    raise ExamHistoryError(
                        "EXAM_HISTORY_FORBIDDEN",
                        "管理员不能替其他课程的教师确认历史依据。",
                    )
                if exam.status is ExamStatus.DRAFT:
                    raise ExamHistoryError(
                        "EXAM_HISTORY_NOT_HISTORICAL",
                        "草稿评分依据应通过正常考试编辑流程准备。",
                    )
                links = list(
                    self.session.scalars(
                        select(ExamQuestion)
                        .where(ExamQuestion.exam_id == exam.id)
                        .order_by(ExamQuestion.question_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                )
                if {row.question_id for row in links} != {
                    row.question_id for row in facts.questions
                }:
                    raise ExamHistoryError(
                        "EXAM_HISTORY_QUESTION_SET_CHANGED",
                        "证据须完整覆盖当前考试的全部原关联，不能增删题。",
                    )
                by_id = {row.question_id: row for row in links}
                prepared: list[
                    tuple[ExamQuestion, HistoricalQuestionFacts, dict[str, Any]]
                ] = []
                for supplied in facts.questions:
                    row = by_id[supplied.question_id]
                    question = self.session.get(Question, row.question_id)
                    if question is None or question.course_id != exam.course_id:
                        raise ExamHistoryError(
                            "EXAM_HISTORY_CONFLICT", "历史关联题目或课程不一致。"
                        )
                    basis = supplied.scoring_basis.model_copy(
                        update={
                            "confirmation": ScoringConfirmation(
                                teacher_id=actor_id,
                                confirmed_at=checked_at,
                                reason=f"历史证据核对（本次时间，非原批准时间）：{facts.reason}",
                            )
                        }
                    )
                    try:
                        validate_scoring_basis(
                            score=supplied.score,
                            base_score=supplied.base_score,
                            question_type=question.type,
                            basis=basis,
                            require_confirmation=True,
                        )
                    except ValueError as error:
                        raise ExamHistoryError(
                            "EXAM_HISTORY_INVALID_BASIS", str(error)
                        ) from error
                    value = basis.model_dump(mode="json")
                    for name in _FACT_FIELDS:
                        old = getattr(row, name)
                        new = (
                            value
                            if name == "scoring_basis"
                            else getattr(supplied, name)
                        )
                        if old is not None and not self._same_fact(name, old, new):
                            raise ExamHistoryError(
                                "EXAM_HISTORY_CONFLICT",
                                f"既有 {name} 与证据冲突，拒绝覆盖原事实。",
                            )
                    if (
                        row.scoring_basis is not None
                        and row.order_index != supplied.order_index
                    ):
                        raise ExamHistoryError(
                            "EXAM_HISTORY_CONFLICT",
                            "已核对评分关联题序不能由迁移工具重写。",
                        )
                    prepared.append((row, supplied, value))
                changed = any(
                    row.order_index != supplied.order_index
                    or any(getattr(row, name) is None for name in _FACT_FIELDS)
                    for row, supplied, _ in prepared
                )
                if changed:
                    # Positive staging positions avoid transient UNIQUE collisions when evidence
                    # establishes a stronger order than the migration's old-display baseline.
                    if any(
                        row.order_index != supplied.order_index
                        for row, supplied, _ in prepared
                    ):
                        offset = max(row.order_index for row in links) + len(links)
                        for i, (row, _, _) in enumerate(prepared, 1):
                            row.order_index = offset + i
                        self.session.flush()
                    for row, supplied, value in prepared:
                        row.order_index = supplied.order_index
                        for name in _FACT_FIELDS:
                            if getattr(row, name) is None:
                                setattr(
                                    row,
                                    name,
                                    (
                                        value
                                        if name == "scoring_basis"
                                        else getattr(supplied, name)
                                    ),
                                )
                    self.session.flush()
                observation = self._observe(exam)
                observation.status = "applied" if changed else "unchanged"
                observed.append(observation)
            commit_started = True
            self.session.commit()
        except BaseException as error:
            # Keep the original failure type/cause for callers and diagnostics. Once
            # COMMIT starts, a missing acknowledgement cannot prove server rollback.
            if commit_started:
                error.add_note(EXAM_HISTORY_COMMIT_UNKNOWN)
            try:
                self.session.rollback()
            except SQLAlchemyError:
                error.add_note("会话清理失败；保留原始错误。请重新只读盘点数据库事实。")
            raise
        return ExamHistoryReport(
            operation="apply",
            actor_id=actor_id,
            observed_at=checked_at,
            exams=observed,
            evidence_manifest=manifest.model_dump(mode="json"),
        )

    @staticmethod
    def _same_fact(name: str, old: Any, new: Any) -> bool:
        if name != "scoring_basis":
            return bool(old == new)
        # An identical retry keeps the first actual confirmation timestamp and identity.
        left = ScoringBasis.model_validate(old).model_dump(mode="json")
        right = dict(new)
        old_confirmation = left.pop("confirmation")
        new_confirmation = right.pop("confirmation")
        if left != right or old_confirmation is None or new_confirmation is None:
            return False
        return bool(
            old_confirmation["teacher_id"] == new_confirmation["teacher_id"]
            and old_confirmation["reason"] == new_confirmation["reason"]
        )


__all__ = [
    "ExamHistoryError",
    "ExamHistoryManifest",
    "ExamHistoryReport",
    "ExamHistoryService",
]
