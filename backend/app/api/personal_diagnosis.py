from typing import Literal
from uuid import UUID

from fastapi import APIRouter

from backend.app.api.subjects import PersonalRoute, Subjects
from backend.app.schemas.personal_diagnosis import PersonalDiagnosis
from backend.app.services.diagnosis_service import PersonalDiagnosisService

router = APIRouter(
    prefix="/api/learning", tags=["personal-diagnosis"], route_class=PersonalRoute
)


@router.get("/subjects/{subject_id}/diagnosis", response_model=PersonalDiagnosis)
def diagnosis(
    subject_id: UUID,
    space: Subjects,
    source_kind: Literal["uploaded", "ai"] | None = None,
    answer_origin: Literal["paper", "ai_completion", "user_correction", "ai_generated"]
    | None = None,
):
    return PersonalDiagnosisService(space).get(
        subject_id, source_kind=source_kind, answer_origin=answer_origin
    )
