"""Personal knowledge organization API: suggestions are not confirmations."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from backend.app.api import learning_errors
from backend.app.api.subjects import PersonalRoute, Subjects
from backend.app.schemas.personal_learning import (
    KnowledgeConfirmRequest,
    KnowledgeView,
)
from backend.app.services.knowledge_organization import (
    KnowledgeOrganizationService,
)

router = APIRouter(
    prefix="/api/learning/subjects/{subject_id}/knowledge",
    tags=["personal-knowledge"],
    route_class=PersonalRoute,
    responses=learning_errors(403, 404, 409, 422, 503),
)


def knowledge_service(subjects: Subjects) -> KnowledgeOrganizationService:
    return KnowledgeOrganizationService(subjects)


Knowledge = Annotated[KnowledgeOrganizationService, Depends(knowledge_service)]


@router.get("", response_model=KnowledgeView)
def get_knowledge(subject_id: UUID, service: Knowledge):
    return service.view(subject_id)


@router.post("/suggest", response_model=KnowledgeView)
def suggest_knowledge(subject_id: UUID, service: Knowledge):
    """Derive pending suggestions from real headings; never auto-confirms."""

    return service.suggest(subject_id)


@router.post("/confirm", response_model=KnowledgeView)
def confirm_knowledge(
    subject_id: UUID, body: KnowledgeConfirmRequest, service: Knowledge
):
    return service.confirm(subject_id, body)


def install_knowledge(app) -> None:
    app.include_router(router)


__all__ = ["Knowledge", "install_knowledge", "knowledge_service", "router"]
