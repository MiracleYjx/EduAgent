"""Explicit per-exam rubric preparation and authentic teacher confirmation."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.domain.enums import (
    OBJECTIVE_QUESTION_TYPES,
    ExamStatus,
    QuestionStatus,
)
from backend.app.models import Exam, ExamQuestion, Question, Submission
from backend.app.schemas.exam_scoring import (
    ScoringBasis,
    ScoringBasisView,
    ScoringConfirmation,
    ScoringConfirmRequest,
    ScoringPoint,
    ScoringPrepareRequest,
)
from backend.app.services.content_validation_service import ContentValidationError
from backend.app.services.exam_assembly_service import (
    AssemblyError,
    ExamAssemblyService,
)
from backend.app.services.exam_scoring_rules import (
    default_points,
    validate_scoring_basis,
)
from backend.app.services.file_storage_service import FileStorageError


class ExamScoringError(AssemblyError):
    """A structured scoring business/persistence failure."""


def require_scoring_ready(exam: Exam, link: ExamQuestion) -> ScoringBasis:
    """Check real per-exam facts; the publishing caller owns locks and freezing."""
    if link.base_score is None or link.scoring_basis is None:
        raise ExamScoringError(
            "EXAM_SCORING_BASIS_MISSING",
            "请先准备并核对本场评分依据。",
            current_status=exam.status.value,
        )
    if exam.status != ExamStatus.DRAFT and (
        link.score is None or link.published_knowledge_points is None
    ):
        raise ExamScoringError(
            "EXAM_SCORING_BASIS_MISSING",
            "历史本场固定分值或知识点尚未核对。",
            current_status=exam.status.value,
        )
    try:
        basis = ScoringBasis.model_validate(link.scoring_basis)
        score = link.score if link.score is not None else link.question.score
        if exam.status == ExamStatus.DRAFT and link.base_score != link.question.score:
            raise ValueError("原始满分已经变化，请重新准备评分依据。")
        validate_scoring_basis(
            score=score,
            base_score=link.base_score,
            question_type=link.question.type,
            basis=basis,
            require_confirmation=False,
        )
    except ValueError as error:
        raise ExamScoringError(
            "RUBRIC_REVIEW_REQUIRED", str(error), current_status=exam.status.value
        ) from error
    changed = any(
        point.confirmed_points != point.default_points for point in basis.points
    )
    if basis.confirmation is None:
        if basis.additive and basis.rounding_delta != Decimal(0):
            raise ExamScoringError(
                "RUBRIC_ROUNDING_UNCONFIRMED",
                "本场独立舍入存在尾差，请教师明确处置后确认。",
                current_status=exam.status.value,
            )
        if not basis.additive or changed:
            raise ExamScoringError(
                "RUBRIC_REVIEW_REQUIRED",
                "定性、非加总或人工改点须完成真实教师确认。",
                current_status=exam.status.value,
            )
    try:
        validate_scoring_basis(
            score=score,
            base_score=link.base_score,
            question_type=link.question.type,
            basis=basis,
        )
    except ValueError as error:
        raise ExamScoringError(
            "RUBRIC_REVIEW_REQUIRED", str(error), current_status=exam.status.value
        ) from error
    return basis


class ExamScoringService:
    def __init__(self, session: Session, *, root: Path | None = None):
        self.session = session
        self.assembly = ExamAssemblyService(session, root=root)

    @contextmanager
    def _transaction(self, *, commit: bool) -> Iterator[None]:
        committing = False
        try:
            yield
            if commit:
                committing = True
                self.session.commit()
        except AssemblyError as error:
            self.assembly._rollback(error)
            raise
        except (ContentValidationError, FileStorageError) as error:
            failure = ExamScoringError(
                error.code,
                str(error),
                http_status=error.http_status,
                current_status=error.current_status,
            )
            self.assembly._rollback(failure)
            raise failure from error
        except (ValidationError, ValueError) as error:
            failure = ExamScoringError(
                "RUBRIC_INPUT_INVALID", str(error), http_status=422
            )
            self.assembly._rollback(failure)
            raise failure from error
        except SQLAlchemyError as error:
            failure = ExamScoringError(
                (
                    "EXAM_SCORING_COMMIT_UNKNOWN"
                    if committing
                    else "EXAM_SCORING_PERSISTENCE_FAILED"
                ),
                (
                    "提交回执未知，请重新加载本场依据核对；不要自动重试。"
                    if committing
                    else "本场评分依据操作失败，修改未提交。"
                ),
                http_status=503,
                details={
                    "database_committed": None if committing else False,
                    "error": {
                        "code": type(error).__name__,
                        "cause": type(error).__name__,
                    },
                },
            )
            self.assembly._rollback(failure)
            raise failure from error

    def _load(
        self, exam_id: UUID, question_id: UUID, teacher_id: UUID, *, writing: bool
    ) -> tuple[Exam, ExamQuestion]:
        exam = self.assembly._load_exam(exam_id, teacher_id)
        if writing:
            self.assembly.require_editable(exam)
        if question_id not in {link.question_id for link in exam.exam_question_links}:
            raise ExamScoringError(
                "EXAM_QUESTION_NOT_FOUND",
                "当前考试不存在此题关联，请重新加载。",
                http_status=404,
                current_status=exam.status.value,
            )
        question = self.session.scalar(
            select(Question)
            .where(Question.id == question_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if question is None or question.course_id != exam.course_id:
            raise ExamScoringError(
                "EXAM_QUESTION_COURSE_CONFLICT",
                "关联题目不属于本场课程。",
                current_status=exam.status.value,
            )
        # The question lock may have waited for an edit that invalidated the earlier
        # selectinloaded association. Read the actual row again after obtaining it.
        link = self.session.scalar(
            select(ExamQuestion)
            .where(
                ExamQuestion.exam_id == exam.id, ExamQuestion.question_id == question_id
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if link is None:
            raise ExamScoringError(
                "EXAM_QUESTION_NOT_FOUND",
                "当前考试题目已替换，请重新加载。",
                http_status=404,
                current_status=exam.status.value,
            )
        if writing:
            self.assembly.validation.require_exam_eligible(question, teacher_id)
        return exam, link

    def _view(self, exam: Exam, link: ExamQuestion) -> ScoringBasisView:
        question = link.question
        return ScoringBasisView(
            exam_id=exam.id,
            exam_question_id=link.id,
            question_id=question.id,
            question_type=question.type,
            source_rubric=question.scoring_rubric,
            question_validation_revision=question.validation_revision,
            explicit_score=link.score,
            effective_score=(
                link.score
                if link.score is not None
                else question.score if exam.status == ExamStatus.DRAFT else None
            ),
            question_score=question.score,
            base_score=link.base_score,
            basis=(
                ScoringBasis.model_validate(link.scoring_basis)
                if link.scoring_basis is not None
                else None
            ),
            editable=exam.status == ExamStatus.DRAFT
            and question.status == QuestionStatus.APPROVED
            and self.session.scalar(
                select(Submission.id).where(Submission.exam_id == exam.id).limit(1)
            )
            is None,
        )

    @staticmethod
    def _context(
        link: ExamQuestion,
        payload: ScoringPrepareRequest | ScoringConfirmRequest,
        *,
        check_base: bool = True,
    ) -> None:
        if (
            link.question.validation_revision
            != payload.expected_question_validation_revision
            or link.effective_score != payload.expected_effective_score
            or (check_base and link.base_score != payload.expected_base_score)
        ):
            raise ExamScoringError(
                "EXAM_SCORING_CONTEXT_CHANGED",
                "题目修订、基准或本场分值已经变化，请重新加载后核对。",
                current_status=link.exam.status.value,
            )

    @staticmethod
    def _structure(basis: ScoringBasis) -> dict:
        raw = basis.model_dump(mode="json", exclude={"preparation_id", "confirmation"})
        for point in raw["points"]:
            point.pop("confirmed_points")
        return raw

    def get_scoring_basis(
        self, exam_id: UUID, question_id: UUID, *, teacher_id: UUID
    ) -> ScoringBasisView:
        with self._transaction(commit=False):
            return self._view(
                *self._load(exam_id, question_id, teacher_id, writing=False)
            )

    def prepare_scoring_basis(
        self,
        exam_id: UUID,
        question_id: UUID,
        payload: ScoringPrepareRequest,
        *,
        teacher_id: UUID,
    ) -> ScoringBasisView:
        with self._transaction(commit=True):
            exam, link = self._load(exam_id, question_id, teacher_id, writing=True)
            self._context(link, payload, check_base=False)
            points = []
            for supplied in payload.points:
                amount = default_points(
                    base_points=supplied.base_points,
                    score=link.effective_score,
                    base_score=link.question.score,
                )
                points.append(
                    ScoringPoint(
                        **supplied.model_dump(),
                        default_points=amount,
                        confirmed_points=amount,
                    )
                )
            with localcontext() as context:
                context.prec = max(context.prec, 28)
                delta = (
                    link.effective_score
                    - sum((point.default_points for point in points), Decimal(0))
                    if payload.additive
                    else None
                )
            proposed = ScoringBasis(
                preparation_id=uuid4(),
                kind=(
                    "objective"
                    if link.question.type in OBJECTIVE_QUESTION_TYPES
                    else "subjective"
                ),
                points=points,
                additive=payload.additive,
                rounding_delta=delta,
            )
            validate_scoring_basis(
                score=link.effective_score,
                base_score=link.question.score,
                question_type=link.question.type,
                basis=proposed,
                require_confirmation=False,
            )
            old = (
                ScoringBasis.model_validate(link.scoring_basis)
                if link.scoring_basis is not None
                else None
            )
            if (
                old is not None
                and old.preparation_id is not None
                and link.base_score == link.question.score
                and self._structure(old) == self._structure(proposed)
                and payload.expected_base_score in (None, link.base_score)
            ):
                # Includes an acknowledged or retried initial request whose base was
                # still None, preserving the first authentic preparation/confirmation.
                return self._view(exam, link)
            self._context(link, payload)
            if payload.expected_basis != old:
                raise ExamScoringError(
                    "EXAM_SCORING_CONTEXT_CHANGED",
                    "本场评分依据已被其他操作准备或确认，请重新加载。",
                    current_status=exam.status.value,
                )
            link.base_score = link.question.score
            link.scoring_basis = proposed.model_dump(mode="json")
            link.published_knowledge_points = None
            self.session.flush()
            return self._view(exam, link)

    def confirm_scoring_basis(
        self,
        exam_id: UUID,
        question_id: UUID,
        payload: ScoringConfirmRequest,
        *,
        teacher_id: UUID,
    ) -> ScoringBasisView:
        with self._transaction(commit=True):
            exam, link = self._load(exam_id, question_id, teacher_id, writing=True)
            self._context(link, payload)
            if link.scoring_basis is None or link.base_score is None:
                raise ExamScoringError(
                    "EXAM_SCORING_BASIS_MISSING",
                    "请先准备本场标准再确认。",
                    current_status=exam.status.value,
                )
            current = ScoringBasis.model_validate(link.scoring_basis)
            if (
                current.preparation_id is None
                or current.preparation_id != payload.preparation_id
                or payload.expected_basis.preparation_id != current.preparation_id
            ):
                raise ExamScoringError(
                    "EXAM_SCORING_CONTEXT_CHANGED",
                    "本场准备轮次已变化，旧页面不能确认当前标准。",
                    current_status=exam.status.value,
                )
            supplied = {point.key: point.points for point in payload.confirmed_points}
            if set(supplied) != {point.key for point in current.points}:
                raise ExamScoringError(
                    "RUBRIC_CONFIRMATION_INVALID",
                    "须恰好确认本次全部要点一次。",
                    http_status=422,
                    current_status=exam.status.value,
                )
            if (
                self._structure(payload.expected_basis) == self._structure(current)
                and current.confirmation is not None
                and current.confirmation.teacher_id == teacher_id
                and current.confirmation.reason == payload.reason
                and all(
                    point.confirmed_points == supplied[point.key]
                    for point in current.points
                )
            ):
                return self._view(exam, link)
            if payload.expected_basis != current:
                raise ExamScoringError(
                    "EXAM_SCORING_CONTEXT_CHANGED",
                    "评分依据已被其他操作修改，请重新加载后确认。",
                    current_status=exam.status.value,
                )
            confirmed = current.model_copy(
                update={
                    "points": [
                        point.model_copy(
                            update={"confirmed_points": supplied[point.key]}
                        )
                        for point in current.points
                    ],
                    "confirmation": ScoringConfirmation(
                        teacher_id=teacher_id,
                        confirmed_at=datetime.now(UTC),
                        reason=payload.reason,
                    ),
                }
            )
            validate_scoring_basis(
                score=link.effective_score,
                base_score=link.base_score,
                question_type=link.question.type,
                basis=confirmed,
            )
            link.scoring_basis = confirmed.model_dump(mode="json")
            self.session.flush()
            return self._view(exam, link)


__all__ = ["ExamScoringError", "ExamScoringService", "require_scoring_ready"]
