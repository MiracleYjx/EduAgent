"""Single query projection for selection, diagnosis and teacher read context.

Rounds replace an answer's effective result, never its original submission time.
Canonical merging and first/repractice classification precede source filtering.
"""

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models.practice import GradeAttempt, PracticeAnswer, PracticeItem
from backend.app.models.question import Question
from backend.app.models.question_revision import QuestionRevision
from backend.app.models.score_dispute import ScoreDispute
from backend.app.schemas.personal_diagnosis import DiagnosisStatistics, Performance
from backend.app.services.question_identity import canonical_id


@dataclass(frozen=True)
class Observation:
    answer_id: UUID
    item_id: UUID
    practice_id: UUID
    question_id: UUID
    revision_id: UUID
    attempt_id: UUID | None
    submitted_at: datetime
    knowledge_point_ids: tuple[UUID, ...]
    question_type: str
    source_kind: str
    answer_origin: str | None
    score: Decimal | None
    max_score: Decimal
    exclusion: str | None = None

    def payload(self):
        return asdict(self)


def observations(session: Session, subject_id: UUID) -> list[Observation]:
    rows = session.execute(
        select(PracticeAnswer, PracticeItem, QuestionRevision, Question)
        .join(PracticeItem, PracticeItem.id == PracticeAnswer.practice_item_id)
        .join(
            QuestionRevision, QuestionRevision.id == PracticeItem.question_revision_id
        )
        .join(Question, Question.id == PracticeItem.question_id)
        .where(
            PracticeItem.subject_id == subject_id,
            PracticeAnswer.submission_state == "submitted",
        )
        .order_by(
            PracticeAnswer.submitted_at, PracticeAnswer.created_at, PracticeAnswer.id
        )
    ).all()
    ids = [a.id for a, *_ in rows]
    latest = {
        g.answer_id: g
        for g in session.scalars(
            select(GradeAttempt)
            .where(GradeAttempt.answer_id.in_(ids))
            .order_by(GradeAttempt.round)
        )
    }
    disputes: dict[UUID, list[ScoreDispute]] = {}
    for d in session.scalars(
        select(ScoreDispute)
        .where(ScoreDispute.answer_id.in_(ids))
        .order_by(ScoreDispute.created_at, ScoreDispute.id)
    ):
        disputes.setdefault(d.answer_id, []).append(d)
    roots: dict[UUID, UUID] = {}
    result = []
    for answer, item, revision, question in rows:
        attempt = latest.get(answer.id)
        history = disputes.get(answer.id, [])
        if answer.answer_state != "answered":
            exclusion = "unanswered"
        elif any(d.state != "resolved" for d in history):
            exclusion = "disputed"
        elif attempt is None:
            exclusion = "ungraded"
        elif attempt.validation_state != "valid":
            exclusion = attempt.validation_state
        elif history and history[-1].accepted_attempt_id != attempt.id:
            exclusion = "unaccepted"
        else:
            exclusion = None
        if item.question_id not in roots:
            roots[item.question_id] = canonical_id(
                session, item.question_id, subject_id
            )
        fixed = item.scoring_input
        effective = attempt.scoring_input if attempt else fixed
        refs = effective.get("grounding_refs", fixed.get("grounding_refs", {}))
        source = fixed.get("source_kind") or (
            "ai" if question.source_type.value == "ai_generated" else "uploaded"
        )
        result.append(
            Observation(
                answer_id=answer.id,
                item_id=item.id,
                practice_id=item.session_id,
                question_id=roots[item.question_id],
                revision_id=item.question_revision_id,
                attempt_id=attempt.id if attempt else None,
                submitted_at=answer.submitted_at or answer.created_at,
                knowledge_point_ids=tuple(UUID(p) for p in item.knowledge_point_ids),
                question_type=fixed.get("question_type", revision.question_type),
                source_kind=source,
                answer_origin=refs.get("answer_origin"),
                score=attempt.score if attempt else None,
                max_score=item.fixed_max_score,
                exclusion=exclusion,
            )
        )
    return result


def performance(rows: list[Observation]) -> Performance:
    choices = [
        r for r in rows if r.question_type in {"single_choice", "multiple_choice"}
    ]
    return Performance(
        count=len(rows),
        score_rate=sum(
            (r.score / r.max_score for r in rows if r.score is not None), Decimal(0)
        )
        / len(rows)
        if rows
        else None,
        choice_count=len(choices),
        choice_correct_rate=Decimal(sum(r.score == r.max_score for r in choices))
        / len(choices)
        if choices
        else None,
    )


def summarize(
    rows: list[Observation],
    *,
    source_kind: str | None = None,
    answer_origin: str | None = None,
    knowledge_point_id: UUID | None = None,
) -> DiagnosisStatistics:
    first_ids: set[UUID] = set()
    first: list[Observation] = []
    repeats: list[Observation] = []
    selected: list[Observation] = []
    for row in sorted(rows, key=lambda r: (r.submitted_at, str(r.answer_id))):
        initial = row.question_id not in first_ids
        if row.exclusion is None:
            first_ids.add(row.question_id)
        if (
            (source_kind and row.source_kind != source_kind)
            or (answer_origin and row.answer_origin != answer_origin)
            or (
                knowledge_point_id and knowledge_point_id not in row.knowledge_point_ids
            )
        ):
            continue
        selected.append(row)
        if row.exclusion is None:
            (first if initial else repeats).append(row)
    recent = first[-20:]
    prior = first[-40:-20]
    metrics = performance(recent)
    errors = Counter(
        r.question_id
        for r in first + repeats
        if r.score is not None and r.score < r.max_score
    )
    exclusions = Counter(
        r.exclusion for r in selected if r.exclusion and r.exclusion != "unanswered"
    )
    return DiagnosisStatistics(
        practice_count=len({r.practice_id for r in selected}),
        valid_count=len(first) + len(repeats),
        excluded_count=sum(exclusions.values()),
        unanswered_count=sum(r.exclusion == "unanswered" for r in selected),
        exclusion_counts=dict(exclusions),
        first=performance(first),
        repractice=performance(repeats),
        recent=metrics,
        previous=performance(prior),
        status="insufficient_evidence"
        if len(recent) < 5
        else "reinforce"
        if metrics.score_rate is not None and metrics.score_rate < Decimal("0.6")
        else "observed",
        repeated_wrong_question_ids=sorted(
            (q for q, n in errors.items() if n >= 2), key=str
        ),
    )
