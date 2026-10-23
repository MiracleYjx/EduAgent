"""Score disputes: an explicit user request, then an explicit acceptance.

Rules (FR-026):

- a dispute always names one concrete grading round, never a replacement score;
- re-grading appends a new round; the disputed round stays readable;
- only a newer *valid* round of the same answer can be accepted;
- a stale or failed acceptance leaves the dispute unresolved, so it keeps the
  item out of effective diagnosis.
"""

from __future__ import annotations

from typing import Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models.practice import (
    GradeAttempt,
    PracticeAnswer,
    PracticeItem,
)
from backend.app.models.question import Question
from backend.app.models.question_revision import QuestionRevision
from backend.app.models.score_dispute import ScoreDispute
from backend.app.schemas.personal_practice import (
    ScoreDisputeRegradeRequest,
    ScoreDisputeView,
)
from backend.app.services.learning_material_service import MaterialError
from backend.app.services.personal_grading import grade_practice
from backend.app.services.practice_service import PracticeService

DisputeState = Literal["pending", "awaiting_acceptance", "resolved"]


def _view(row: ScoreDispute, item_id: UUID) -> ScoreDisputeView:
    return ScoreDisputeView(
        id=row.id,
        item_id=item_id,
        answer_id=row.answer_id,
        state=cast(DisputeState, row.state),
        reason=row.reason,
        disputed_attempt_id=row.disputed_attempt_id,
        accepted_attempt_id=row.accepted_attempt_id,
    )


class ScoreDisputeService(PracticeService):
    """Reuse the verified practice space, sessions and grading entry point."""

    def _item(self, session: Session, practice_id: UUID, item_id: UUID) -> PracticeItem:
        item = session.get(PracticeItem, item_id)
        if item is None or item.session_id != practice_id:
            raise MaterialError("PRACTICE_ITEM_NOT_FOUND", 404)
        return item

    def _answer(self, session: Session, item: PracticeItem) -> PracticeAnswer:
        answer = session.scalar(
            select(PracticeAnswer).where(PracticeAnswer.practice_item_id == item.id)
        )
        if answer is None:
            raise MaterialError("PRACTICE_ANSWER_NOT_FOUND", 409)
        return answer

    def _latest(self, session: Session, answer_id: UUID) -> GradeAttempt:
        row = session.scalar(
            select(GradeAttempt)
            .where(GradeAttempt.answer_id == answer_id)
            .order_by(GradeAttempt.round.desc())
        )
        if row is None:
            raise MaterialError("PRACTICE_NOT_GRADED", 409)
        return row

    def grading_item(self, session: Session, item: PracticeItem, latest: GradeAttempt,
                     body: ScoreDisputeRegradeRequest, request: dict) -> PracticeItem:
        """Select explicit revision without changing the original practice item."""
        frozen = dict(latest.scoring_input or item.scoring_input or {})
        revision_id = UUID(frozen.get("question_revision_id", str(item.question_revision_id)))
        if body.revision_id is not None:
            revision = session.get(QuestionRevision, body.revision_id)
            question = session.get(Question, item.question_id)
            if revision is None or revision.question_id != item.question_id or revision.subject_id != item.subject_id:
                raise MaterialError("QUESTION_REVISION_NOT_FOUND", 404)
            if question is None or question.current_revision_id != revision.id:
                raise MaterialError("REVISION_CHANGED", 409)
            if revision.admission_state != "eligible" or not revision.reference_answer or (
                revision.question_type == "short_answer" and not revision.scoring_rubric
            ):
                raise MaterialError("QUESTION_NOT_ELIGIBLE", 409)
            if revision.question_type != item.scoring_input.get("question_type"):
                raise MaterialError("REGRADING_QUESTION_TYPE_CHANGED", 409)
            frozen.update(**self.content(revision).model_dump(mode="json"),
                          reference_answer=revision.reference_answer, scoring_rubric=revision.scoring_rubric,
                          grounding=revision.grounding, grounding_refs=revision.grounding_refs,
                          **self._frozen_material_scope(session, item.subject_id, revision))
            revision_id = revision.id
        frozen.update(question_revision_id=str(revision_id), regrade_request=request)
        return PracticeItem(id=item.id, session_id=item.session_id, subject_id=item.subject_id,
            position=item.position, question_id=item.question_id, question_revision_id=revision_id,
            fixed_max_score=item.fixed_max_score, knowledge_point_ids=list(item.knowledge_point_ids),
            scoring_input=frozen)

    def raise_dispute(
        self, practice_id: UUID, *, item_id: UUID, reason: str
    ) -> ScoreDisputeView:
        with self.sessions.begin() as session:
            practice = self._practice(session, practice_id, lock=True)
            item = self._item(session, practice.id, item_id)
            answer = self._answer(session, item)
            current = self._latest(session, answer.id)
            if current.validation_state != "valid":
                raise MaterialError("DISPUTE_ROUND_NOT_VALID", 409)
            existing = session.scalar(
                select(ScoreDispute).where(
                    ScoreDispute.answer_id == answer.id,
                    ScoreDispute.state != "resolved",
                )
            )
            if existing is not None:
                return _view(existing, item.id)
            row = ScoreDispute(
                id=uuid4(),
                answer_id=answer.id,
                disputed_attempt_id=current.id,
                state="pending",
                reason=reason,
            )
            session.add(row)
            session.flush()
            return _view(row, item.id)

    def view(self, dispute_id: UUID) -> ScoreDisputeView:
        with self.sessions() as session:
            dispute = session.get(ScoreDispute, dispute_id)
            if dispute is None:
                raise MaterialError("DISPUTE_NOT_FOUND", 404)
            answer = session.get(PracticeAnswer, dispute.answer_id)
            if answer is None:
                raise MaterialError("PRACTICE_ANSWER_NOT_FOUND", 409)
            item = session.get(PracticeItem, answer.practice_item_id)
            if item is None:
                raise MaterialError("PRACTICE_ITEM_NOT_FOUND", 404)
            result = _view(dispute, item.id)
            practice = self._practice(session, item.session_id)
            operation_id = practice.conditions.get("grading_previews", {}).get(str(dispute.id))
            result.regrade_operation_id = UUID(operation_id) if operation_id else None
            return result

    def regrade(self, dispute_id: UUID, *, body: ScoreDisputeRegradeRequest | None = None) -> ScoreDisputeView:
        """Append one explicitly requested round, preserving its exact input."""
        body = body or ScoreDisputeRegradeRequest()
        with self.sessions.begin() as session:
            dispute = session.get(ScoreDispute, dispute_id)
            if dispute is None:
                raise MaterialError("DISPUTE_NOT_FOUND", 404)
            answer = session.get(PracticeAnswer, dispute.answer_id)
            if answer is None:
                raise MaterialError("PRACTICE_ANSWER_NOT_FOUND", 409)
            item = session.get(PracticeItem, answer.practice_item_id)
            if item is None:
                raise MaterialError("PRACTICE_ITEM_NOT_FOUND", 404)
            self._practice(session, item.session_id, lock=True)
            session.refresh(dispute, with_for_update=True)
            request = {"dispute_id": str(dispute_id), **body.model_dump(mode="json")}
            request["idempotency_key"] = body.idempotency_key or f"legacy:{dispute_id}"
            prior = list(session.scalars(select(GradeAttempt).where(GradeAttempt.answer_id == answer.id)))
            for attempt in prior:
                saved = (attempt.scoring_input or {}).get("regrade_request", {})
                if saved.get("dispute_id") == str(dispute_id) and saved.get("idempotency_key") == request["idempotency_key"]:
                    if saved != request:
                        raise MaterialError("IDEMPOTENCY_CONFLICT", 409)
                    return _view(dispute, item.id)
            if dispute.state == "resolved":
                raise MaterialError("DISPUTE_ALREADY_RESOLVED", 409)
            latest = self._latest(session, answer.id)
            if body.expected_attempt_id is not None and body.expected_attempt_id != latest.id:
                raise MaterialError("DISPUTE_ROUND_STALE", 409)
            grading_item = self.grading_item(session, item, latest, body, request)
            port, runtime = self._grading_port(session, item.session_id, regrade=True, item_ids=(item.id,))
            try:
                grade_practice(session, practice_id=item.session_id, blank_confirmed=True, regrade=True,
                               item_ids=(item.id,), subjective=port, grading_items={item.id: grading_item})
            finally:
                if runtime is not None:
                    runtime.release_all()
            latest = self._latest(session, answer.id)
            dispute.state = "awaiting_acceptance" if latest.validation_state == "valid" else "pending"
            return _view(dispute, item.id)

    def accept(self, dispute_id: UUID, *, attempt_id: UUID) -> ScoreDisputeView:
        """Accept a newer valid round; stale/failed acceptance stays unresolved."""

        with self.sessions.begin() as session:
            dispute = session.get(ScoreDispute, dispute_id)
            if dispute is None:
                raise MaterialError("DISPUTE_NOT_FOUND", 404)
            answer = session.get(PracticeAnswer, dispute.answer_id)
            if answer is None:
                raise MaterialError("PRACTICE_ANSWER_NOT_FOUND", 409)
            item = session.get(PracticeItem, answer.practice_item_id)
            if item is None:
                raise MaterialError("PRACTICE_ITEM_NOT_FOUND", 404)
            practice = self._practice(session, item.session_id, lock=True)
            session.refresh(dispute, with_for_update=True)
            if dispute.state == "resolved":
                if dispute.accepted_attempt_id == attempt_id:
                    return _view(dispute, item.id)
                raise MaterialError("DISPUTE_ALREADY_RESOLVED", 409)
            active = practice.conditions.get("grading_executions", {}).get(str(dispute_id))
            if active and session.scalar(select(GradeAttempt.id).where(
                GradeAttempt.answer_id == answer.id,
                GradeAttempt.scoring_input["operation_id"].as_string() == active,
            ).limit(1)) is None:
                raise MaterialError("GRADING_OPERATION_IN_PROGRESS", 409)
            candidate = session.get(GradeAttempt, attempt_id)
            if candidate is None or candidate.answer_id != answer.id:
                raise MaterialError("DISPUTE_ATTEMPT_NOT_FOUND", 404)
            disputed = session.get(GradeAttempt, dispute.disputed_attempt_id)
            if disputed is None:
                raise MaterialError("DISPUTE_ROUND_MISSING", 409)
            if candidate.validation_state != "valid":
                raise MaterialError("DISPUTE_ROUND_NOT_VALID", 409)
            if candidate.round <= disputed.round or candidate.id != self._latest(session, answer.id).id:
                raise MaterialError("DISPUTE_ROUND_STALE", 409)
            dispute.state = "resolved"
            dispute.accepted_attempt_id = candidate.id
            return _view(dispute, item.id)
