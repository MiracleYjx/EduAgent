"""Authorized whole-group image understanding commands."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from backend.app.api.file_storage import file_http_exception
from backend.app.core.config import AppSettings
from backend.app.core.database import get_db
from backend.app.core.security import CurrentUser, get_app_settings
from backend.app.schemas.image_assessment import ImageAssessmentView
from backend.app.services.file_storage_service import FileStorageError
from backend.app.services.image_understanding_service import ImageUnderstandingService

router = APIRouter(prefix="/api", tags=["图片理解"])


class ImageUnderstandingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: str = Field(default="提取题图中作答所需条件", min_length=1, max_length=2000)

    @field_validator("task")
    @classmethod
    def task_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("图片理解任务不能空白。")
        return value


async def _understand(owner_kind, owner_id, payload, actor, session, settings):
    try:
        return await ImageUnderstandingService(
            session,
            root=settings.storage_root,
            settings=settings,
        ).understand(owner_kind, owner_id, task=payload.task, actor_id=actor.id)
    except FileStorageError as exc:
        raise file_http_exception(exc) from None


@router.post(
    "/questions/{question_id}/image-understanding", response_model=ImageAssessmentView
)
async def understand_question_images(
    question_id: UUID,
    payload: ImageUnderstandingRequest,
    actor: CurrentUser,
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[AppSettings, Depends(get_app_settings)],
):
    return await _understand("question", question_id, payload, actor, session, settings)


@router.post(
    "/extracted-questions/{extracted_id}/image-understanding",
    response_model=ImageAssessmentView,
)
async def understand_extracted_images(
    extracted_id: UUID,
    payload: ImageUnderstandingRequest,
    actor: CurrentUser,
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[AppSettings, Depends(get_app_settings)],
):
    return await _understand(
        "extracted_question", extracted_id, payload, actor, session, settings
    )
