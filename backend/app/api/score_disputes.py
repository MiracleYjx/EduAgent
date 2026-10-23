"""Personal score disputes: mark, re-grade on request, accept a newer round."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter

from backend.app.api import learning_errors
from backend.app.api.learning_operations import public_operation
from backend.app.api.practices import Operations
from backend.app.api.subjects import PersonalRoute, Subjects
from backend.app.schemas.personal_learning import (
    LearningOperationView,
)
from backend.app.schemas.personal_practice import (
    PracticeGradingPreviewRequest,
    ScoreDisputeAcceptRequest,
    ScoreDisputeInput,
    ScoreDisputeRegradeRequest,
    ScoreDisputeView,
)
from backend.app.services.personal_grading_operations import PersonalGradingOperations
from backend.app.services.score_dispute_service import ScoreDisputeService

router = APIRouter(
    prefix="/api/learning",
    tags=["personal-grading"],
    route_class=PersonalRoute,
    responses=learning_errors(403, 404, 409, 422, 503),
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


@router.post("/disputes/{dispute_id}/regrade-preview", response_model=LearningOperationView)
def regrade_preview(dispute_id: UUID, body: ScoreDisputeRegradeRequest,
                    space: Subjects, operations: Operations):
    from backend.app.models.practice import PracticeItem
    service = PersonalGradingOperations(space, operations)
    dispute = service.view(dispute_id)
    with space.sessions() as session:
        item = session.get(PracticeItem, dispute.item_id)
        if item is None:
            from backend.app.services.learning_material_service import MaterialError
            raise MaterialError("PRACTICE_ITEM_NOT_FOUND", 404)
        practice_id = item.session_id
    return public_operation(service.prepare(practice_id, PracticeGradingPreviewRequest(
        idempotency_key=body.idempotency_key or f"regrade:{dispute_id}:{body.expected_attempt_id}",
        blank_confirmed=True), dispute_id=dispute_id, regrade=body))


@router.post("/disputes/{dispute_id}/regrade/{operation_id}",
             response_model=ScoreDisputeView | LearningOperationView)
def execute_regrade(dispute_id: UUID, operation_id: UUID, space: Subjects, operations: Operations):
    result = PersonalGradingOperations(space, operations).execute(operation_id, dispute_id=dispute_id)
    return public_operation(result) if isinstance(result, LearningOperationView) else result


@router.post("/disputes/{dispute_id}/accept", response_model=ScoreDisputeView)
def accept(dispute_id: UUID, body: ScoreDisputeAcceptRequest, space: Subjects):
    return ScoreDisputeService(space).accept(dispute_id, attempt_id=body.attempt_id)


@router.get("/disputes/{dispute_id}", response_model=ScoreDisputeView)
def get_dispute(dispute_id: UUID, space: Subjects):
    return ScoreDisputeService(space).view(dispute_id)


def install_score_disputes(app) -> None:
    app.include_router(router)


__all__ = ["install_score_disputes", "router"]
