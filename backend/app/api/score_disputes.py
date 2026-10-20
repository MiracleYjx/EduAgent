"""Personal score disputes: mark, re-grade on request, accept a newer round."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter

from backend.app.api.subjects import PersonalRoute, Subjects
from backend.app.schemas.personal_learning import LearningApiError
from backend.app.schemas.personal_practice import (
    ScoreDisputeAcceptRequest,
    ScoreDisputeInput,
    ScoreDisputeRegradeRequest,
    ScoreDisputeView,
)
from backend.app.services.score_dispute_service import ScoreDisputeService

router = APIRouter(
    prefix="/api/learning",
    tags=["personal-grading"],
    route_class=PersonalRoute,
    responses={s: {"model": LearningApiError} for s in (403, 404, 409, 422, 503)},
)


@router.post(
    "/practices/{practice_id}/disputes", response_model=ScoreDisputeView
)
def raise_dispute(practice_id: UUID, body: ScoreDisputeInput, space: Subjects):
    return ScoreDisputeService(space).raise_dispute(
        practice_id, item_id=body.item_id, reason=body.reason
    )


@router.post("/disputes/{dispute_id}/regrade", response_model=ScoreDisputeView)
def regrade(dispute_id: UUID, space: Subjects, body: ScoreDisputeRegradeRequest | None = None):
    return ScoreDisputeService(space).regrade(dispute_id, body=body)


@router.post("/disputes/{dispute_id}/accept", response_model=ScoreDisputeView)
def accept(dispute_id: UUID, body: ScoreDisputeAcceptRequest, space: Subjects):
    return ScoreDisputeService(space).accept(dispute_id, attempt_id=body.attempt_id)


@router.get("/disputes/{dispute_id}", response_model=ScoreDisputeView)
def get_dispute(dispute_id: UUID, space: Subjects):
    return ScoreDisputeService(space).view(dispute_id)


def install_score_disputes(app) -> None:
    app.include_router(router)


__all__ = ["install_score_disputes", "router"]
