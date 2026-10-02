"""Authenticated teacher reports and image commands; machine completion stays internal."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.app.api.file_storage import file_http_exception
from backend.app.core.config import AppSettings
from backend.app.core.database import get_db
from backend.app.core.security import CurrentUser, get_app_settings
from backend.app.schemas.content_validation import (
    ManualDispositionRequest,
    ValidationReportView,
)
from backend.app.schemas.image_assessment import (
    ImageAssessmentView,
    ImageManualCheckRequest,
)
from backend.app.services.content_validation_service import ContentValidationService
from backend.app.services.file_storage_service import FileStorageError

router = APIRouter(prefix="/api", tags=["内容核验"])


def get_content_validation_service(
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[AppSettings, Depends(get_app_settings)],
) -> ContentValidationService:
    return ContentValidationService(session, root=settings.storage_root)


ValidationService = Annotated[
    ContentValidationService, Depends(get_content_validation_service)
]


@router.get(
    "/questions/{question_id}/validations", response_model=list[ValidationReportView]
)
def list_validations(question_id: UUID, actor: CurrentUser, service: ValidationService):
    try:
        return service.list_validations(question_id, actor_id=actor.id)
    except FileStorageError as exc:
        raise file_http_exception(exc) from None


@router.get(
    "/questions/{question_id}/validations/{report_id}",
    response_model=ValidationReportView,
)
def get_validation(
    question_id: UUID, report_id: UUID, actor: CurrentUser, service: ValidationService
):
    try:
        return service.get_validation(question_id, report_id, actor_id=actor.id)
    except FileStorageError as exc:
        raise file_http_exception(exc) from None


@router.post(
    "/questions/{question_id}/validations/{report_id}/dispositions",
    response_model=ValidationReportView,
)
def dispose_validation(
    question_id: UUID,
    report_id: UUID,
    payload: ManualDispositionRequest,
    actor: CurrentUser,
    service: ValidationService,
):
    try:
        return service.dispose_validation(
            question_id, report_id, payload, actor_id=actor.id
        )
    except FileStorageError as exc:
        raise file_http_exception(exc) from None


@router.get(
    "/questions/{question_id}/image-assessment", response_model=ImageAssessmentView
)
def get_question_image_assessment(
    question_id: UUID, actor: CurrentUser, service: ValidationService
):
    try:
        return service.get_image_assessment("question", question_id, actor_id=actor.id)
    except FileStorageError as exc:
        raise file_http_exception(exc) from None


@router.get(
    "/extracted-questions/{extracted_id}/image-assessment",
    response_model=ImageAssessmentView,
)
def get_extracted_image_assessment(
    extracted_id: UUID, actor: CurrentUser, service: ValidationService
):
    try:
        return service.get_image_assessment(
            "extracted_question", extracted_id, actor_id=actor.id
        )
    except FileStorageError as exc:
        raise file_http_exception(exc) from None


@router.post(
    "/questions/{question_id}/image-manual-checks", response_model=ImageAssessmentView
)
def check_question_images(
    question_id: UUID,
    payload: ImageManualCheckRequest,
    actor: CurrentUser,
    service: ValidationService,
):
    try:
        return service.manual_image_check(
            "question", question_id, payload, actor_id=actor.id
        )
    except FileStorageError as exc:
        raise file_http_exception(exc) from None


@router.post(
    "/extracted-questions/{extracted_id}/image-manual-checks",
    response_model=ImageAssessmentView,
)
def check_extracted_images(
    extracted_id: UUID,
    payload: ImageManualCheckRequest,
    actor: CurrentUser,
    service: ValidationService,
):
    try:
        return service.manual_image_check(
            "extracted_question", extracted_id, payload, actor_id=actor.id
        )
    except FileStorageError as exc:
        raise file_http_exception(exc) from None
