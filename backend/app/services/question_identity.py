"""Canonical graph decisions preserve source, revision, answer and grading rows."""
from itertools import combinations
from typing import Annotated, Literal, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models.duplicate_candidate import DuplicateCandidate
from backend.app.models.question import Question
from backend.app.models.question_revision import QuestionRevision
from backend.app.models.subject import Subject
from backend.app.schemas.content_blocks import RevisionContent
from backend.app.services.learning_material_service import MaterialError
from backend.app.services.subject_service import SubjectService


class DuplicateView(BaseModel):
    id: UUID
    left_id: UUID
    right_id: UUID
    revisions: dict[UUID, int]
    differences: list[str]
    decision: Literal["pending", "same", "different"]


class DuplicateResolve(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["same", "different"]
    expected_revisions: dict[UUID, Annotated[int, Field(ge=1, strict=True)]] = Field(min_length=2, max_length=2)
    target_id: UUID | None = None


def canonical_id(session: Session, identity: UUID, subject_id: UUID) -> UUID:
    seen = set()
    while identity not in seen:
        seen.add(identity)
        row = session.get(Question, identity)
        if row is None or row.subject_id != subject_id:
            raise MaterialError("QUESTION_NOT_FOUND", 404)
        if row.canonical_id is None:
            return row.id
        identity = row.canonical_id
    raise MaterialError("QUESTION_IDENTITY_CYCLE", 409)


class QuestionIdentity:
    def __init__(self, space: SubjectService):
        self.space = space
        self.sessions = space.sessions

    @staticmethod
    def _view(row: DuplicateCandidate, revisions: dict[UUID, QuestionRevision]) -> DuplicateView:
        left, right = revisions[row.left_id], revisions[row.right_id]
        fields = ("stem_blocks", "options", "reference_answer", "scoring_rubric", "explanation_blocks")
        return DuplicateView(
            id=row.id, left_id=row.left_id, right_id=row.right_id,
            revisions={row.left_id: row.left_revision, row.right_id: row.right_revision},
            differences=[key for key in fields if getattr(left, key) != getattr(right, key)],
            decision=cast(Literal["pending", "same", "different"], row.decision),
        )

    @staticmethod
    def _current(session: Session, question: Question | None) -> QuestionRevision:
        if question is None or question.subject_id is None or question.current_revision_id is None:
            raise MaterialError("QUESTION_NOT_FOUND", 404)
        revision = session.get(QuestionRevision, question.current_revision_id)
        if revision is None or revision.question_id != question.id:
            raise MaterialError("QUESTION_REVISION_NOT_FOUND", 404)
        return revision

    def candidates(self, subject_id: UUID) -> list[DuplicateView]:
        self.space._check_space()
        with self.sessions.begin() as session:
            if session.get(Subject, subject_id, with_for_update=True) is None:
                raise MaterialError("SUBJECT_NOT_FOUND", 404)
            questions = list(session.scalars(select(Question).where(
                Question.subject_id == subject_id,
            ).order_by(Question.id)))
            revisions = {q.id: self._current(session, q)
                         for q in questions if q.current_revision_id is not None}
            result = []
            for left, right in combinations(questions, 2):
                a, b = revisions.get(left.id), revisions.get(right.id)
                if a is None or b is None or canonical_id(session, left.id, subject_id) == canonical_id(session, right.id, subject_id):
                    continue
                # Exact stem projection is only a candidate. Options, images and answers
                # are still shown as differences and always require a human decision.
                ca = RevisionContent.model_validate({
                    k: getattr(a, k) for k in ("question_type", "stem_blocks", "options", "explanation_blocks")
                })
                cb = RevisionContent.model_validate({
                    k: getattr(b, k) for k in ("question_type", "stem_blocks", "options", "explanation_blocks")
                })
                stem_a = ca.model_copy(update={"options": []}).text_projection()
                stem_b = cb.model_copy(update={"options": []}).text_projection()
                if stem_a != stem_b:
                    continue
                row = session.scalar(select(DuplicateCandidate).where(
                    DuplicateCandidate.subject_id == subject_id,
                    DuplicateCandidate.left_id == left.id, DuplicateCandidate.right_id == right.id,
                    DuplicateCandidate.left_revision == a.revision, DuplicateCandidate.right_revision == b.revision,
                ))
                if row is None:
                    row = DuplicateCandidate(
                        id=uuid4(), subject_id=subject_id, left_id=left.id, right_id=right.id,
                        left_revision=a.revision, right_revision=b.revision,
                    )
                    session.add(row)
                    session.flush()
                if row.decision == "pending":
                    result.append(self._view(row, revisions))
            return result

    def resolve(self, identity: UUID, *, decision: Literal["same", "different"],
                expected_revisions: dict[UUID, int], target_id: UUID | None = None) -> DuplicateView:
        self.space._check_space()
        with self.sessions.begin() as session:
            row = session.get(DuplicateCandidate, identity)
            if row is None:
                raise MaterialError("DUPLICATE_NOT_FOUND", 404)
            # One subject lock serializes competing merges, even overlapping pairs.
            session.get(Subject, row.subject_id, with_for_update=True)
            session.refresh(row, with_for_update=True)
            questions = {qid: session.get(Question, qid, with_for_update=True)
                         for qid in sorted((row.left_id, row.right_id))}
            revisions = {qid: self._current(session, q) for qid, q in questions.items()}
            if expected_revisions != {row.left_id: row.left_revision, row.right_id: row.right_revision}:
                raise MaterialError("REVISION_CHANGED", 409)
            if row.decision != "pending":
                if row.decision != decision:
                    raise MaterialError("DUPLICATE_ALREADY_RESOLVED", 409)
                snapshots = {}
                for qid, number in expected_revisions.items():
                    stored = session.scalar(select(QuestionRevision).where(
                        QuestionRevision.question_id == qid, QuestionRevision.revision == number,
                    ))
                    if stored is None:
                        raise MaterialError("QUESTION_REVISION_NOT_FOUND", 404)
                    snapshots[qid] = stored
                return self._view(row, snapshots)
            if any(revisions[qid].revision != expected_revisions[qid] for qid in questions):
                raise MaterialError("REVISION_CHANGED", 409)
            if decision == "same":
                roots = {canonical_id(session, qid, row.subject_id) for qid in questions}
                target = canonical_id(session, target_id, row.subject_id) if target_id else min(roots)
                if target not in roots:
                    raise MaterialError("DUPLICATE_TARGET_INVALID", 409)
                for root in sorted(roots - {target}):
                    alias = session.get(Question, root, with_for_update=True)
                    if alias is None:
                        raise MaterialError("QUESTION_NOT_FOUND", 404)
                    alias.canonical_id = target
                session.flush()
            elif decision != "different":
                raise MaterialError("REQUEST_INVALID", 422)
            row.decision = decision
            return self._view(row, revisions)
