"""Personal practice grading: deterministic rules plus an explicit local zero.

FR-022/FR-023 rules enforced here:

- single/multiple choice never call a model; multi-select scores full marks only
  when the selected set equals the reference set (no partial credit);
- an unanswered item is graded locally as zero and stored with
  ``execution_source='local_unanswered'`` so it stays distinguishable from an
  answered item that scored zero;
- an answered subjective item is graded only through an explicitly supplied
  port. Without that port the round fails honestly instead of inventing a score;
- local results never carry a runtime snapshot, confidence or usage.

This module writes append-only ``GradeAttempt`` rows: a re-grade adds a new
round and never overwrites the previous one.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.models.practice import (
    GradeAttempt,
    PracticeAnswer,
    PracticeItem,
    PracticeSession,
)
from backend.app.services.grading.objective_grader import (
    ObjectiveGrader,
    ObjectiveGradingError,
)
from backend.app.services.learning_material_service import MaterialError

OBJECTIVE_TYPES = frozenset({"single_choice", "multiple_choice", "true_false"})
SUBJECTIVE_TYPE = "short_answer"
BLANK_ANSWERS_CODE = "PRACTICE_BLANK_ANSWERS"
SUBJECTIVE_UNAVAILABLE = "SUBJECTIVE_GRADER_UNAVAILABLE"

ValidationState = Literal["valid", "failed", "needs_review", "outcome_unknown"]
ExecutionSource = Literal["local_objective", "local_unanswered", "model"]


class BlankAnswersPending(MaterialError):
    """Whole-group submit needs the user's explicit confirmation first."""

    def __init__(self, item_ids: Sequence[UUID]) -> None:
        super().__init__(BLANK_ANSWERS_CODE, 409)
        self.item_ids = tuple(item_ids)


@dataclass(frozen=True)
class SubjectiveResult:
    """What a subjective grading port is allowed to return."""

    score: Decimal | None
    reason: str
    validation_state: ValidationState = "valid"
    confidence: float | None = None
    usage: dict[str, Any] | None = None
    runtime_snapshot_id: UUID | None = None
    feedback: dict[str, Any] | None = None
    error_code: str | None = None


SubjectivePort = Callable[[PracticeItem, PracticeAnswer], SubjectiveResult]


@dataclass(frozen=True)
class AttemptFacts:
    item_id: UUID
    answer_id: UUID
    position: int
    question_type: str
    score: Decimal | None
    max_score: Decimal
    validation_state: ValidationState
    execution_source: ExecutionSource
    reason: str
    feedback: dict[str, Any] | None
    runtime_snapshot_id: UUID | None
    error_code: str | None
    scoring_input: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PracticeGrade:
    round: int
    facts: tuple[AttemptFacts, ...] = field(default=())
    blank_item_ids: tuple[UUID, ...] = field(default=())

    @property
    def all_valid(self) -> bool:
        return bool(self.facts) and all(
            item.validation_state == "valid" for item in self.facts
        )


def _latest_round(session: Session, answer_id: UUID) -> int:
    current = session.scalar(
        select(func.max(GradeAttempt.round)).where(GradeAttempt.answer_id == answer_id)
    )
    return int(current or 0)


def latest_attempts(session: Session, practice_id: UUID) -> tuple[GradeAttempt, ...]:
    """Return the newest stored round per answer, in item order."""

    items = list(
        session.scalars(
            select(PracticeItem)
            .where(PracticeItem.session_id == practice_id)
            .order_by(PracticeItem.position)
        )
    )
    attempts: list[GradeAttempt] = []
    for item in items:
        answer_id = session.scalar(
            select(PracticeAnswer.id).where(PracticeAnswer.practice_item_id == item.id)
        )
        if answer_id is None:
            continue
        row = session.scalar(
            select(GradeAttempt)
            .where(GradeAttempt.answer_id == answer_id)
            .order_by(GradeAttempt.round.desc())
        )
        if row is not None:
            attempts.append(row)
    return tuple(attempts)


def stored_facts(session: Session, practice_id: UUID) -> tuple[AttemptFacts, ...]:
    """Project the newest round per item into read-only facts."""

    items = list(
        session.scalars(
            select(PracticeItem)
            .where(PracticeItem.session_id == practice_id)
            .order_by(PracticeItem.position)
        )
    )
    by_answer: dict[UUID, GradeAttempt] = {}
    for item in items:
        answer_id = session.scalar(
            select(PracticeAnswer.id).where(PracticeAnswer.practice_item_id == item.id)
        )
        if answer_id is None:
            continue
        row = session.scalar(
            select(GradeAttempt)
            .where(GradeAttempt.answer_id == answer_id)
            .order_by(GradeAttempt.round.desc())
        )
        if row is not None:
            by_answer[item.id] = row
    facts: list[AttemptFacts] = []
    for item in items:
        row = by_answer.get(item.id)
        if row is None:
            continue
        facts.append(
            AttemptFacts(
                item_id=item.id,
                answer_id=row.answer_id,
                position=item.position,
                question_type=str((item.scoring_input or {}).get("question_type", "")),
                score=row.score,
                max_score=row.max_score or item.fixed_max_score,
                validation_state=cast(ValidationState, row.validation_state),
                execution_source=cast(ExecutionSource, row.execution_source),
                reason=row.reason,
                feedback=row.feedback,
                runtime_snapshot_id=row.runtime_snapshot_id,
                error_code=row.error_code,
                scoring_input=dict(row.scoring_input or {}),
            )
        )
    return tuple(facts)


def _objective_result(
    *,
    item: PracticeItem,
    answer: PracticeAnswer,
    grader: ObjectiveGrader,
) -> tuple[Decimal | None, str, str | None]:
    frozen = item.scoring_input or {}
    question_type = str(frozen.get("question_type") or "")
    options = [
        str(option.get("id"))
        for option in frozen.get("options") or []
        if isinstance(option, dict) and option.get("id")
    ]
    student = answer.choice_ids if question_type in OBJECTIVE_TYPES else answer.text_answer
    try:
        result = grader.grade(
            question_type=question_type,
            reference_answer=frozen.get("reference_answer"),
            student_answer=student,
            max_score=item.fixed_max_score,
            option_keys=options or None,
        )
    except (ObjectiveGradingError, ValueError) as exc:
        code = str(getattr(exc, "error_code", "GRADING_INPUT_INVALID"))
        return None, f"客观题评分失败：{code}。", code
    return result.score, result.reason, None


def grade_practice(
    session: Session,
    *,
    practice_id: UUID,
    blank_confirmed: bool,
    subjective: SubjectivePort | None = None,
    regrade: bool = False,
    item_ids: Sequence[UUID] | None = None,
    grader: ObjectiveGrader | None = None,
    grading_items: dict[UUID, PracticeItem] | None = None,
) -> PracticeGrade:
    """Grade one practice session inside the caller's transaction.

    ``regrade=False`` is idempotent: existing rounds are returned unchanged.
    ``regrade=True`` appends exactly one new round per answer.
    """

    practice = session.get(PracticeSession, practice_id, with_for_update=True)
    if practice is None:
        raise MaterialError("PRACTICE_NOT_FOUND", 404)
    items = list(
        session.scalars(
            select(PracticeItem)
            .where(PracticeItem.session_id == practice_id)
            .order_by(PracticeItem.position)
        )
    )
    if item_ids is not None:
        selected_ids = set(item_ids)
        items = [item for item in items if item.id in selected_ids]
    if not items:
        raise MaterialError("PRACTICE_EMPTY", 409)
    answers = {
        answer.practice_item_id: answer
        for answer in session.scalars(
            select(PracticeAnswer).where(
                PracticeAnswer.practice_item_id.in_([item.id for item in items])
            )
        )
    }
    if any(item.id not in answers for item in items):
        raise MaterialError("PRACTICE_ANSWER_NOT_FOUND", 409)

    blanks = tuple(
        item.id for item in items if answers[item.id].answer_state != "answered"
    )

    if not regrade:
        existing = stored_facts(session, practice_id)
        if existing:
            return PracticeGrade(
                round=int(latest_attempts(session, practice_id)[0].round),
                facts=existing,
                blank_item_ids=blanks,
            )

    subjective_blanks = tuple(item.id for item in items if item.id in blanks
        and (item.scoring_input or {}).get("question_type") == SUBJECTIVE_TYPE)
    if subjective_blanks and not blank_confirmed:
        raise BlankAnswersPending(subjective_blanks)

    engine = grader or ObjectiveGrader()
    facts: list[AttemptFacts] = []
    round_number = 0
    for original in items:
        item = (grading_items or {}).get(original.id, original)
        answer = answers[item.id]
        round_number = max(round_number, _latest_round(session, answer.id) + 1)
        frozen = dict(item.scoring_input or {})
        question_type = str(frozen.get("question_type") or "")

        state: ValidationState
        source: ExecutionSource
        if answer.answer_state != "answered":
            score: Decimal | None = Decimal("0.00")
            reason = (
                "未作答：按本地规则记 0 分，不调用模型；"
                "该记录不计为知识掌握失败证据。"
            )
            state, source, error = "valid", "local_unanswered", None
            confidence = usage = runtime_snapshot_id = feedback = None
        elif question_type in OBJECTIVE_TYPES:
            score, reason, error = _objective_result(
                item=item, answer=answer, grader=engine
            )
            state = "failed" if error else "valid"
            source = "local_objective"
            confidence = usage = runtime_snapshot_id = feedback = None
        elif question_type == SUBJECTIVE_TYPE and subjective is not None:
            outcome = subjective(item, answer)
            score, reason = outcome.score, outcome.reason
            state, error = outcome.validation_state, outcome.error_code
            source = "model"
            confidence = outcome.confidence
            usage = outcome.usage
            runtime_snapshot_id = outcome.runtime_snapshot_id
            feedback = outcome.feedback
        elif question_type == SUBJECTIVE_TYPE:
            score = None
            reason = "主观题需要已授权的评分能力；本轮未配置，未生成分数。"
            state, source, error = "failed", "local_objective", SUBJECTIVE_UNAVAILABLE
            confidence = usage = runtime_snapshot_id = feedback = None
        else:
            score = None
            reason = f"未知题型 {question_type or '未声明'}，无法评分。"
            state, source, error = "failed", "local_objective", "GRADING_MODE_MISMATCH"
            confidence = usage = runtime_snapshot_id = feedback = None

        session.add(
            GradeAttempt(
                answer_id=answer.id,
                round=round_number,
                validation_state=state,
                execution_source=source,
                score=score,
                max_score=item.fixed_max_score if state == "valid" else None,
                reason=reason,
                scoring_input=frozen,
                runtime_snapshot_id=runtime_snapshot_id,
                confidence=confidence,
                usage=usage,
                feedback=feedback,
                error_code=error,
            )
        )
        facts.append(
            AttemptFacts(
                item_id=item.id,
                answer_id=answer.id,
                position=item.position,
                question_type=question_type,
                score=score,
                max_score=item.fixed_max_score,
                validation_state=state,
                execution_source=source,
                reason=reason,
                feedback=feedback,
                runtime_snapshot_id=runtime_snapshot_id,
                error_code=error,
                scoring_input=frozen,
            )
        )
    session.flush()
    return PracticeGrade(
        round=round_number, facts=tuple(facts), blank_item_ids=blanks
    )
