"""Scoped content/media consumers shared by later practice and result screens."""

from uuid import UUID

from fastapi import APIRouter, FastAPI
from fastapi.responses import FileResponse

from backend.app.api import learning_errors
from backend.app.api.subjects import PersonalRoute, Subjects
from backend.app.schemas.content_blocks import QuestionContentView
from backend.app.services.question_asset_access import PersonalContentAccess
from backend.app.services.subject_service import SubjectError

router = APIRouter(
    prefix="/api/learning/subjects/{subject_id}/practices/{practice_id}/items/{item_id}",
    tags=["personal-content"], route_class=PersonalRoute,
    responses=learning_errors(403, 404, 409, 422, 503),
)


def access(service: Subjects, session):
    service._check_space()
    if service.profile is None:
        raise SubjectError("LEARNING_ENVIRONMENT_MISMATCH")
    return PersonalContentAccess(session, service.profile)


@router.get("/content", response_model=QuestionContentView, response_model_exclude_none=True)
def content(subject_id: UUID, practice_id: UUID, item_id: UUID, service: Subjects):
    with service.sessions() as session:
        return access(service, session).view(subject_id, practice_id, item_id)


@router.get("/assets/{asset_id}", response_class=FileResponse)
def image(subject_id: UUID, practice_id: UUID, item_id: UUID, asset_id: UUID, service: Subjects):
    with service.sessions() as session:
        path, metadata = access(service, session).asset_path(subject_id, practice_id, item_id, asset_id)
        return FileResponse(path, media_type=metadata.media_type, headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        })


def install_learning_content(app: FastAPI) -> None:
    app.include_router(router)
