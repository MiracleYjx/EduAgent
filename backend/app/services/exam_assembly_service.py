"""Transactional, bounded rule-based assembly of existing reviewed questions."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from backend.app.domain.enums import ExamStatus, QuestionStatus
from backend.app.models import Course, Exam, ExamQuestion, Question, Submission
from backend.app.schemas.exam_assembly import (
    AssemblyCondition,
    AssemblyConstraints,
    AssemblyRequest,
    AssemblyResponse,
    ExamQuestionView,
    PublicationCheck,
)
from backend.app.schemas.exam_scoring import ScoringBasis, money_text
from backend.app.services.content_validation_service import (
    ContentValidationError,
    ContentValidationService,
)
from backend.app.services.exam_scoring_rules import validate_scoring_basis
from backend.app.services.file_storage_service import (
    FileStorageError,
    FileStorageService,
)
from backend.app.services.question_asset_service import QuestionAssetService


class AssemblyError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 409,
        current_status: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.current_status = current_status
        self.details = details or {}

    def as_detail(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "current_status": self.current_status,
            **self.details,
        }


@dataclass(frozen=True)
class _Candidate:
    question: Question
    score: Decimal
    labels: frozenset[str]


class ExamAssemblyService:
    """The caller supplies a request-scoped session; this command owns its commit."""

    def __init__(
        self, session: Session, *, root: Path | None = None, search_limit: int = 50_000
    ):
        if (
            isinstance(search_limit, bool)
            or not isinstance(search_limit, int)
            or search_limit < 1
        ):
            raise ValueError("搜索预算必须是正整数。")
        self.session = session
        self.files = FileStorageService(session, root=root)
        self.validation = ContentValidationService(session, root=root)
        self.assets = QuestionAssetService(session, root=root)
        self.search_limit = search_limit

    def _load_exam(self, exam_id: UUID, actor_id: UUID) -> Exam:
        # Discover ownership, then lock in Course -> Exam -> ordered Question order.
        course_id = self.session.scalar(
            select(Exam.course_id).where(Exam.id == exam_id)
        )
        if course_id is None:
            raise AssemblyError("EXAM_NOT_FOUND", "考试不存在。", http_status=404)
        course = self.session.scalars(
            select(Course)
            .where(Course.id == course_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
        if course is None:
            raise AssemblyError(
                "EXAM_NOT_FOUND", "考试所属课程不存在。", http_status=404
            )
        actor = self.files._actor(actor_id)
        if not self.files._manages(actor, course):
            raise AssemblyError(
                "EXAM_FORBIDDEN", "仅课程管理教师可管理本场组卷。", http_status=403
            )
        exam = self.session.scalars(
            select(Exam)
            .where(Exam.id == exam_id)
            .options(
                selectinload(Exam.exam_question_links).selectinload(
                    ExamQuestion.question
                )
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
        if exam is None:
            raise AssemblyError("EXAM_NOT_FOUND", "考试不存在。", http_status=404)
        if exam.course_id != course_id:
            raise AssemblyError(
                "EXAM_COURSE_CONFLICT",
                "考试归属已改变，请重新加载。",
                current_status=exam.status.value,
            )
        return exam

    def require_editable(self, exam: Exam) -> None:
        if (
            exam.status != ExamStatus.DRAFT
            or self.session.scalar(
                select(Submission.id).where(Submission.exam_id == exam.id).limit(1)
            )
            is not None
        ):
            raise AssemblyError(
                "EXAM_PUBLISHED_IMMUTABLE",
                "仅无答卷及历史保护的草稿考试可以修改。",
                current_status=exam.status.value,
            )

    @staticmethod
    def load_intent(exam: Exam) -> AssemblyConstraints | None:
        if exam.assembly_constraints is None:
            return None
        try:
            intent = AssemblyConstraints.model_validate(exam.assembly_constraints)
            if intent.request.course_id != exam.course_id:
                raise ValueError("意图课程与考试归属不一致。")
            return intent
        except (ValidationError, ValueError) as exc:
            raise AssemblyError(
                "EXAM_ASSEMBLY_CONSTRAINTS_INVALID",
                "已保存的组卷要求无效，请核对后重新提交完整要求。",
                current_status=exam.status.value,
            ) from exc

    def _admit(
        self, exam_id: UUID, payload: AssemblyRequest, actor_id: UUID
    ) -> tuple[Exam, list[Question]]:
        exam = self._load_exam(exam_id, actor_id)
        self.require_editable(exam)
        if payload.course_id != exam.course_id:
            raise AssemblyError(
                "EXAM_COURSE_CONFLICT",
                "组卷课程必须与考试所属课程一致。",
                http_status=422,
                current_status=exam.status.value,
            )
        override_ids = [item.question_id for item in payload.score_overrides]
        kinds = [item.question_type for item in payload.type_distribution]
        candidate_scope = and_(
            Question.course_id == exam.course_id,
            Question.status == QuestionStatus.APPROVED,
            Question.type.in_(kinds),
        )
        # One global Question lock order includes overrides outside requested types.
        rows = list(
            self.session.scalars(
                select(Question)
                .where(or_(candidate_scope, Question.id.in_(override_ids)))
                .options(selectinload(Question.assets))
                .order_by(Question.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).all()
        )
        overrides = {
            question.id: question for question in rows if question.id in override_ids
        }
        if len(overrides) != len(override_ids) or any(
            question.course_id != exam.course_id
            or question.status != QuestionStatus.APPROVED
            for question in overrides.values()
        ):
            raise AssemblyError(
                "EXAM_OVERRIDE_INVALID",
                "显式分值必须指向同课程已审核的真实题目。",
                http_status=422,
                current_status=exam.status.value,
            )
        candidates = [
            question
            for question in rows
            if question.course_id == exam.course_id
            and question.status == QuestionStatus.APPROVED
            and question.type.value in kinds
        ]
        return exam, candidates

    def assemble(
        self, exam_id: UUID, payload: AssemblyRequest, *, actor_id: UUID
    ) -> AssemblyResponse:
        failure: AssemblyError | None = None
        try:
            exam, locked_candidates = self._admit(exam_id, payload, actor_id)
        except FileStorageError as exc:
            failure = AssemblyError(
                exc.code,
                str(exc),
                http_status=exc.http_status,
                current_status=exc.current_status,
            )
            self._rollback(failure)
            raise failure from exc
        except AssemblyError as exc:
            self._rollback(exc)
            raise
        except SQLAlchemyError as exc:
            failure = AssemblyError(
                "EXAM_ASSEMBLY_FAILED",
                "准入读取失败，本次组卷要求尚未保存。",
                http_status=503,
                details={
                    "intent_saved": False,
                    "error": self._technical_error(exc, "考试或候选身份读取失败。"),
                },
            )
            self._rollback(failure)
            raise failure from exc
        status = exam.status.value
        intent = AssemblyConstraints(
            request=payload, recorded_by=actor_id, recorded_at=datetime.now(UTC)
        )
        try:
            exam.assembly_constraints = intent.model_dump(mode="json")
            self.session.flush()
        except SQLAlchemyError as exc:
            failure = AssemblyError(
                "EXAM_ASSEMBLY_FAILED",
                "组卷要求保存失败，原卷未修改。",
                http_status=503,
                current_status=status,
                details={
                    "intent_saved": False,
                    "intent_save_error": self._technical_error(exc, "意图写入失败。"),
                },
            )
            self._rollback(failure)
            raise failure from exc
        failure = None
        response: AssemblyResponse | None = None
        try:
            with self.session.begin_nested():
                candidates, excluded = self._candidates(
                    locked_candidates, payload, actor_id
                )
                selected, reason, gaps = self._search(candidates, payload)
                if selected is None:
                    raise AssemblyError(
                        "EXAM_ASSEMBLY_UNSATISFIED",
                        (
                            "现有可用题目不满足要求，请调整条件或补充已审核题。"
                            if reason == "definite_conflict"
                            else "当前有界选题策略未找到满足组合，请调整条件后重试。"
                        ),
                        current_status=status,
                        details={
                            "reason": reason,
                            "gaps": gaps,
                            "excluded_candidates": excluded,
                        },
                    )
                self._replace_questions(exam, selected)
                response = self._preview(exam, actor_id)
        except AssemblyError as exc:
            failure = exc
        except (SQLAlchemyError, FileStorageError, ValueError, OSError) as exc:
            failure = AssemblyError(
                "EXAM_ASSEMBLY_FAILED",
                "组卷执行失败，已回滚本次选题修改。",
                http_status=503,
                current_status=status,
                details={"error": self._technical_error(exc, "选题或保存失败。")},
            )
        if failure is not None:
            failure.details["intent_saved"] = None
        try:
            self.session.commit()
        except SQLAlchemyError as exc:
            # A commit may have reached PostgreSQL before its acknowledgement was lost.
            # Never retry the command or claim that the saved intent is absent/present.
            if failure is None:
                failure = AssemblyError(
                    "EXAM_ASSEMBLY_COMMIT_UNKNOWN",
                    "提交结果未知，请重新加载考试核对实际记录。",
                    http_status=503,
                    current_status=status,
                )
            failure.details.update(
                intent_saved=None,
                intent_save_error=self._technical_error(
                    exc, "提交回执失败，保存结果未知。"
                ),
            )
            self._rollback(failure)
            raise failure from exc
        if failure is not None:
            failure.details.update(
                intent_saved=True, assembly_constraints=intent.model_dump(mode="json")
            )
            raise failure
        assert response is not None
        return response

    def _rollback(self, failure: AssemblyError) -> None:
        """Keep the original persistence/commit outcome if cleanup also fails."""
        try:
            self.session.rollback()
        except SQLAlchemyError as exc:
            failure.details["cleanup_error"] = self._technical_error(
                exc, "事务清理失败。"
            )

    @staticmethod
    def _technical_error(exc: Exception, message: str) -> dict[str, str]:
        return {
            "code": getattr(exc, "code", None) or type(exc).__name__,
            "cause": type(exc).__name__,
            "message": message,
        }

    def _candidates(
        self, rows: list[Question], payload: AssemblyRequest, actor_id: UUID
    ) -> tuple[list[_Candidate], list[dict[str, str]]]:
        overrides = {item.question_id: item.score for item in payload.score_overrides}
        candidates: list[_Candidate] = []
        excluded: list[dict[str, str]] = []
        for question in rows:
            try:
                self.validation.require_exam_eligible(question, actor_id)
                if not isinstance(question.knowledge_points, list) or any(
                    not isinstance(label, str) or not label.strip()
                    for label in question.knowledge_points
                ):
                    raise ContentValidationError(
                        "CONTENT_SOURCE_INVALID", "知识点标签无效。"
                    )
                candidates.append(
                    _Candidate(
                        question,
                        overrides.get(question.id, question.score),
                        frozenset(question.knowledge_points),
                    )
                )
            except (ContentValidationError, FileStorageError) as exc:
                excluded.append(
                    {
                        "question_id": str(question.id),
                        "code": exc.code,
                        "message": str(exc),
                    }
                )
        return candidates, excluded

    def _search(
        self, candidates: list[_Candidate], request: AssemblyRequest
    ) -> tuple[list[_Candidate] | None, str, list[dict[str, Any]]]:
        quotas = {item.question_type: item.count for item in request.type_distribution}
        groups = {
            kind: [item for item in candidates if item.question.type.value == kind]
            for kind in sorted(quotas)
        }
        gaps: list[dict[str, Any]] = []
        for kind, group in groups.items():
            if len(group) < quotas[kind]:
                gaps.append(
                    {
                        "kind": "type",
                        "question_type": kind,
                        "required": quotas[kind],
                        "available": len(group),
                        "missing": quotas[kind] - len(group),
                    }
                )
        for requirement in request.knowledge_coverage:
            available = sum(
                min(
                    quotas[kind],
                    sum(requirement.knowledge_point in item.labels for item in group),
                )
                for kind, group in groups.items()
            )
            if available < requirement.min_questions:
                gaps.append(
                    {
                        "kind": "knowledge",
                        "knowledge_point": requirement.knowledge_point,
                        "required": requirement.min_questions,
                        "available": available,
                        "missing": requirement.min_questions - available,
                    }
                )
        if gaps:
            return None, "definite_conflict", gaps
        low = sum(
            (
                sum(sorted(item.score for item in group)[: quotas[kind]], Decimal(0))
                for kind, group in groups.items()
            ),
            Decimal(0),
        )
        high = sum(
            (
                sum(
                    sorted((item.score for item in group), reverse=True)[
                        : quotas[kind]
                    ],
                    Decimal(0),
                )
                for kind, group in groups.items()
            ),
            Decimal(0),
        )
        total_gap = {
            "kind": "total_score",
            "target": money_text(request.total_score),
            "actual": None,
            "delta": None,
            "minimum": money_text(low),
            "maximum": money_text(high),
        }
        if not low <= request.total_score <= high:
            return None, "definite_conflict", [total_gap]
        ordered_kinds = list(groups)
        selected: list[_Candidate] = []
        coverage: Counter[str] = Counter()
        explored = 0
        exhausted = False

        def visit(
            group_index: int, start: int, remaining: int, total: Decimal
        ) -> list[_Candidate] | None:
            nonlocal explored, exhausted
            if explored >= self.search_limit:
                exhausted = True
                return None
            explored += 1
            if group_index == len(ordered_kinds):
                if total == request.total_score and all(
                    coverage[item.knowledge_point] >= item.min_questions
                    for item in request.knowledge_coverage
                ):
                    return list(selected)
                return None
            kind = ordered_kinds[group_index]
            pool = groups[kind][start:]
            if remaining == 0:
                next_index = group_index + 1
                return visit(
                    next_index,
                    0,
                    (
                        quotas[ordered_kinds[next_index]]
                        if next_index < len(ordered_kinds)
                        else 0
                    ),
                    total,
                )
            future = [(pool, remaining)] + [
                (groups[next_kind], quotas[next_kind])
                for next_kind in ordered_kinds[group_index + 1 :]
            ]
            if any(len(items) < count for items, count in future):
                return None
            minimum = total + sum(
                (
                    sum(sorted(item.score for item in items)[:count], Decimal(0))
                    for items, count in future
                ),
                Decimal(0),
            )
            maximum = total + sum(
                (
                    sum(
                        sorted((item.score for item in items), reverse=True)[:count],
                        Decimal(0),
                    )
                    for items, count in future
                ),
                Decimal(0),
            )
            if not minimum <= request.total_score <= maximum:
                return None
            for requirement in request.knowledge_coverage:
                possible = coverage[requirement.knowledge_point] + sum(
                    min(
                        count,
                        sum(
                            requirement.knowledge_point in item.labels for item in items
                        ),
                    )
                    for items, count in future
                )
                if possible < requirement.min_questions:
                    return None
            for index in range(start, len(groups[kind]) - remaining + 1):
                item = groups[kind][index]
                selected.append(item)
                coverage.update(item.labels)
                found = visit(group_index, index + 1, remaining - 1, total + item.score)
                coverage.subtract(item.labels)
                selected.pop()
                if found is not None:
                    return found
                if exhausted:
                    return None
            return None

        result = visit(0, 0, quotas[ordered_kinds[0]], Decimal(0))
        return (
            result,
            "strategy_not_found" if exhausted else "definite_conflict",
            (
                []
                if result is not None
                else [
                    total_gap,
                    {
                        "kind": "combination",
                        "reason": (
                            "search_budget_exhausted"
                            if exhausted
                            else "all_combinations_excluded"
                        ),
                        "explored_states": explored,
                    },
                ]
            ),
        )

    def _replace_questions(self, exam: Exam, selected: list[_Candidate]) -> None:
        # Flush deletes before inserts to keep both immediate unique constraints.
        exam.exam_question_links.clear()
        self.session.flush()
        exam.exam_question_links.extend(
            ExamQuestion(question=item.question, order_index=index, score=item.score)
            for index, item in enumerate(selected, 1)
        )
        self.session.flush()

    def conditions(
        self, exam: Exam, intent: AssemblyConstraints | None
    ) -> list[AssemblyCondition]:
        if intent is None:
            return []
        request = intent.request
        links = exam.exam_question_links
        results = [
            AssemblyCondition(
                kind="count",
                target=request.question_count,
                actual=len(links),
                satisfied=len(links) == request.question_count,
            )
        ]
        types = Counter(link.question.type.value for link in links)
        for item in request.type_distribution:
            actual = types[item.question_type]
            results.append(
                AssemblyCondition(
                    kind=f"type:{item.question_type}",
                    target=item.count,
                    actual=actual,
                    satisfied=actual == item.count,
                )
            )
        for requirement in request.knowledge_coverage:
            known = all(
                exam.status == ExamStatus.DRAFT
                or link.published_knowledge_points is not None
                for link in links
            )
            count = (
                sum(
                    requirement.knowledge_point
                    in (
                        link.question.knowledge_points
                        if exam.status == ExamStatus.DRAFT
                        else link.published_knowledge_points or []
                    )
                    for link in links
                )
                if known
                else None
            )
            results.append(
                AssemblyCondition(
                    kind=f"knowledge:{requirement.knowledge_point}",
                    target=requirement.min_questions,
                    actual=count,
                    satisfied=count is not None and count >= requirement.min_questions,
                    reason=None if known else "历史知识点依据未知。",
                )
            )
        total = self._total(exam)
        results.append(
            AssemblyCondition(
                kind="total_score",
                target=money_text(request.total_score),
                actual=money_text(total) if total is not None else None,
                satisfied=total == request.total_score,
                reason=None if total is not None else "历史本场分值未知。",
            )
        )
        return results

    @staticmethod
    def _total(exam: Exam) -> Decimal | None:
        if exam.status != ExamStatus.DRAFT and any(
            link.score is None for link in exam.exam_question_links
        ):
            return None
        return sum(
            (link.effective_score for link in exam.exam_question_links), Decimal(0)
        )

    def _preview(self, exam: Exam, actor_id: UUID) -> AssemblyResponse:
        intent = self.load_intent(exam)
        checks: list[PublicationCheck] = []
        views: list[ExamQuestionView] = []
        links = sorted(exam.exam_question_links, key=lambda item: item.order_index)
        if not links:
            checks.append(
                PublicationCheck(code="EXAM_QUESTIONS_EMPTY", message="考试尚未选题。")
            )
        if [link.order_index for link in links] != list(range(1, len(links) + 1)):
            checks.append(
                PublicationCheck(
                    code="EXAM_ORDER_INVALID", message="题序必须从 1 开始连续且唯一。"
                )
            )
        for link in links:
            question = link.question
            if question.course_id != exam.course_id:
                checks.append(
                    PublicationCheck(
                        code="EXAM_QUESTION_COURSE_CONFLICT",
                        message="题目不属于本场课程。",
                        question_id=question.id,
                    )
                )
                continue
            try:
                self.validation.require_exam_eligible(question, actor_id)
            except (ContentValidationError, FileStorageError) as exc:
                checks.append(
                    PublicationCheck(
                        code=exc.code, message=str(exc), question_id=question.id
                    )
                )
            basis = (
                ScoringBasis.model_validate(link.scoring_basis)
                if link.scoring_basis is not None
                else None
            )
            if (
                link.score is None
                or link.base_score is None
                or link.published_knowledge_points is None
                or basis is None
            ):
                checks.append(
                    PublicationCheck(
                        code="EXAM_SCORING_BASIS_MISSING",
                        message="本场分值、原始满分、知识点及评分依据尚未完整固定。",
                        question_id=question.id,
                    )
                )
            else:
                try:
                    validate_scoring_basis(
                        score=link.score,
                        base_score=link.base_score,
                        question_type=question.type,
                        basis=basis,
                    )
                    if exam.status == ExamStatus.DRAFT and (
                        link.base_score != question.score
                        or link.published_knowledge_points != question.knowledge_points
                    ):
                        raise ValueError("草稿评分依据与当前题目不一致。")
                except ValueError as exc:
                    checks.append(
                        PublicationCheck(
                            code="EXAM_SCORING_BASIS_INVALID",
                            message=str(exc),
                            question_id=question.id,
                        )
                    )
            effective = (
                link.score
                if link.score is not None
                else question.score
                if exam.status == ExamStatus.DRAFT
                else None
            )
            views.append(
                ExamQuestionView(
                    id=link.id,
                    exam_id=exam.id,
                    question_id=question.id,
                    question_type=question.type,
                    content=question.content,
                    options=question.options,
                    reference_answer=question.reference_answer,
                    scoring_rubric=question.scoring_rubric,
                    analysis=question.analysis,
                    knowledge_points=list(question.knowledge_points or []),
                    order_index=link.order_index,
                    score=link.score,
                    effective_score=effective,
                    base_score=link.base_score,
                    published_knowledge_points=link.published_knowledge_points,
                    scoring_basis=basis,
                    assets=self.assets.list_question(question.id, actor_id=actor_id),
                )
            )
        conditions = self.conditions(exam, intent)
        if any(not item.satisfied for item in conditions):
            checks.append(
                PublicationCheck(
                    code="EXAM_ASSEMBLY_UNSATISFIED",
                    message="当前试卷仍有未满足的已保存组卷要求。",
                )
            )
        return AssemblyResponse(
            exam_id=exam.id,
            current_status=exam.status.value,
            exam_questions=views,
            conditions=conditions,
            total_score=self._total(exam),
            publication_checks=checks,
            assembly_constraints=intent,
        )

    def preview(self, exam_id: UUID, *, actor_id: UUID) -> AssemblyResponse:
        try:
            exam = self._load_exam(exam_id, actor_id)
            ids = [link.question_id for link in exam.exam_question_links]
            if ids:
                self.session.scalars(
                    select(Question)
                    .where(Question.id.in_(ids))
                    .order_by(Question.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                ).all()
            return self._preview(exam, actor_id)
        except FileStorageError as exc:
            failure = AssemblyError(
                exc.code,
                str(exc),
                http_status=exc.http_status,
                current_status=exc.current_status,
            )
            self._rollback(failure)
            raise failure from exc
        except AssemblyError as exc:
            self._rollback(exc)
            raise
        except SQLAlchemyError as exc:
            failure = AssemblyError(
                "EXAM_PREVIEW_FAILED",
                "考试预览读取失败。",
                http_status=503,
                details={"error": self._technical_error(exc, "预览数据库读取失败。")},
            )
            self._rollback(failure)
            raise failure from exc


__all__ = ["AssemblyError", "ExamAssemblyService"]
