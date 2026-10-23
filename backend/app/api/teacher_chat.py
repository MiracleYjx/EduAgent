from uuid import UUID

from fastapi import APIRouter

from backend.app.api.learning_operations import public_operation
from backend.app.api.practices import Operations
from backend.app.api.subjects import PersonalRoute, Subjects
from backend.app.schemas.personal_learning import (
    LearningConfirmRequest,
    LearningOperationView,
)
from backend.app.schemas.personal_teacher import (
    ConversationCreate,
    ConversationSummary,
    ConversationView,
    TeacherSend,
    TeacherTurn,
)
from backend.app.services.conversation_service import ConversationService
from backend.app.services.teacher_service import TeacherService

router = APIRouter(
    prefix="/api/learning", tags=["personal-teacher"], route_class=PersonalRoute
)


@router.get(
    "/subjects/{subject_id}/conversations", response_model=list[ConversationSummary]
)
def conversations(subject_id: UUID, space: Subjects):
    return ConversationService(space).list(subject_id)


@router.post(
    "/subjects/{subject_id}/conversations",
    response_model=ConversationSummary,
    status_code=201,
)
def create(subject_id: UUID, body: ConversationCreate, space: Subjects):
    return ConversationService(space).create(subject_id, body)


@router.get("/conversations/{identity}", response_model=ConversationView)
def conversation(identity: UUID, subject_id: UUID, space: Subjects):
    return ConversationService(space).get(identity, subject_id)


@router.delete("/conversations/{identity}", response_model=ConversationSummary)
def delete_conversation(identity: UUID, subject_id: UUID, space: Subjects):
    service = ConversationService(space)
    before = service.get(identity, subject_id)
    service.delete(identity, subject_id)
    return before


@router.post("/conversations/{identity}/messages/preview", response_model=TeacherTurn)
def preview(
    identity: UUID,
    subject_id: UUID,
    body: TeacherSend,
    space: Subjects,
    operations: Operations,
):
    return TeacherService(space, operations).prepare(identity, subject_id, body)


@router.post(
    "/conversations/{identity}/messages/{operation_id}/confirm",
    response_model=LearningOperationView,
)
def confirm(
    identity: UUID,
    subject_id: UUID,
    operation_id: UUID,
    body: LearningConfirmRequest,
    space: Subjects,
    operations: Operations,
):
    service = TeacherService(space, operations)
    service.turn(identity, subject_id, operation_id)
    return public_operation(service.confirm(operation_id, body.preview_digest))


@router.post(
    "/conversations/{identity}/messages/{operation_id}/execute",
    response_model=TeacherTurn,
)
async def execute(
    identity: UUID,
    subject_id: UUID,
    operation_id: UUID,
    space: Subjects,
    operations: Operations,
):
    return await TeacherService(space, operations).execute(
        identity, subject_id, operation_id
    )


@router.post(
    "/conversations/{identity}/messages/{operation_id}/cancel",
    response_model=ConversationView,
)
def cancel(
    identity: UUID,
    subject_id: UUID,
    operation_id: UUID,
    space: Subjects,
    operations: Operations,
):
    return TeacherService(space, operations).cancel(identity, subject_id, operation_id)
