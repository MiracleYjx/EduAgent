"""User-owned duplicate decisions behind the personal request boundary."""
from uuid import UUID

from fastapi import APIRouter

from backend.app.api.subjects import PersonalRoute, Subjects
from backend.app.services.question_identity import (
    DuplicateResolve,
    DuplicateView,
    QuestionIdentity,
)

router = APIRouter(prefix="/api/learning", tags=["personal-identity"], route_class=PersonalRoute)


@router.get("/subjects/{subject_id}/duplicate-candidates", response_model=list[DuplicateView])
def candidates(subject_id: UUID, space: Subjects):
    return QuestionIdentity(space).candidates(subject_id)


@router.post("/duplicate-candidates/{identity}/resolve", response_model=DuplicateView)
def resolve(identity: UUID, body: DuplicateResolve, space: Subjects):
    return QuestionIdentity(space).resolve(identity, **body.model_dump())
