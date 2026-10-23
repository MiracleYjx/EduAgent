"""Personal generation endpoints; preserve existing URLs and budgets."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from backend.app.api.learning_operations import service as learning_operation_service
from backend.app.api.subjects import PersonalRoute, Subjects
from backend.app.services.learning_operations import LearningOperations

Operations = Annotated[LearningOperations, Depends(learning_operation_service)]
from backend.app.schemas.personal_learning import LearningOperationView
from backend.app.schemas.personal_practice import GenerationRequest, PracticePreviewView
from backend.app.services.personal_question_generation import PersonalQuestionGeneration

personal_generation_router = APIRouter(
    prefix="/api/learning/practice-previews", tags=["personal-generation"], route_class=PersonalRoute)


@personal_generation_router.post("/{identity}/generation-preview", response_model=LearningOperationView)
def personal_generation_preview(identity: UUID, body: GenerationRequest, space: Subjects, operations: Operations):
    return PersonalQuestionGeneration(space).prepare(identity,body,operations)


@personal_generation_router.post("/{identity}/generate/{operation_id}",
                                  response_model=PracticePreviewView | LearningOperationView)
async def personal_generate(identity: UUID,operation_id: UUID,space: Subjects,operations: Operations):
    return await PersonalQuestionGeneration(space).execute(identity,operation_id,operations)
