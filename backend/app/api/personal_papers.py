"""Personal paper endpoints extracted from the mixed legacy HTTP module."""
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse

from backend.app.ai.paper_extraction.schemas import PersonalPaperConfirm
from backend.app.api.learning_operations import service as operation_service
from backend.app.api.subjects import PersonalRoute, Subjects
from backend.app.schemas.personal_learning import (
    LearningOperationView,
    PersonalAnswerRequest,
    PersonalPaperCreate,
    PersonalPaperEdit,
    PersonalPaperExtractionPreview,
    PersonalPaperView,
    PersonalQuestionEdit,
    PersonalQuestionList,
    PersonalQuestionView,
)
from backend.app.services.answer_completion import AnswerCompletion
from backend.app.services.learning_operations import LearningOperations
from backend.app.services.paper_import_service import PersonalPaperImportService
from backend.app.services.personal_question_service import PersonalQuestionService
from backend.app.services.question_correction_service import PersonalQuestionCorrection

# Personal imports reuse the verified Local space and have no teacher/JWT dependency.
personal_router = APIRouter(
    prefix="/api/learning", tags=["personal-papers"], route_class=PersonalRoute,
)
PersonalOperations = Annotated[LearningOperations, Depends(operation_service)]


@personal_router.post("/subjects/{subject_id}/imports", response_model=PersonalPaperView, status_code=201)
def create_personal_import(subject_id: UUID, body: PersonalPaperCreate, space: Subjects):
    return PersonalPaperImportService(space).create(subject_id, body.document_id)


@personal_router.get("/imports/{identity}", response_model=PersonalPaperView)
def get_personal_import(identity: UUID, space: Subjects):
    return PersonalPaperImportService(space).get(identity)


@personal_router.post("/imports/{identity}/extraction-preview", response_model=LearningOperationView)
def preview_personal_extraction(
    identity: UUID, body: PersonalPaperExtractionPreview, space: Subjects, operations: PersonalOperations,
):
    return PersonalPaperImportService(space).prepare_extraction(
        identity, operations, body.idempotency_key,
        retry_of_id=body.retry_of_id, acknowledge_unknown=body.acknowledge_unknown,
    )


@personal_router.post("/imports/{identity}/extract/{operation_id}",
                      response_model=PersonalPaperView | LearningOperationView)
async def extract_personal(
    identity: UUID, operation_id: UUID, space: Subjects, operations: PersonalOperations,
):
    return await PersonalPaperImportService(space).execute_extraction(identity, operations, operation_id)


@personal_router.patch("/imports/{identity}/questions/{question_id}", response_model=PersonalPaperView)
def correct_personal(identity: UUID, question_id: UUID, body: PersonalPaperEdit, space: Subjects):
    return PersonalQuestionCorrection(space).patch_personal(identity, question_id, body)


@personal_router.post("/imports/{identity}/confirm", response_model=PersonalPaperView)
def confirm_personal(identity: UUID, body: PersonalPaperConfirm, space: Subjects):
    return PersonalQuestionCorrection(space).confirm(identity, body)


@personal_router.get("/subjects/{subject_id}/questions", response_model=PersonalQuestionList)
def personal_questions(
    subject_id: UUID, space: Subjects,
    source_kind: Literal["uploaded", "ai"] | None = None,
    admission_state: Literal["incomplete", "needs_validation", "eligible", "needs_correction"] | None = None,
    difficulty: str | None = None,
    knowledge_point_ids: Annotated[list[UUID] | None, Query()] = None,
):
    return PersonalQuestionList(questions=PersonalQuestionService(space).list(
        subject_id, source_kind=source_kind, admission_state=admission_state,
        difficulty=difficulty, knowledge_point_ids=knowledge_point_ids,
    ))


@personal_router.get("/questions/{identity}", response_model=PersonalQuestionView)
def personal_question(identity: UUID, space: Subjects):
    return PersonalQuestionService(space).get(identity)


@personal_router.post("/questions/{identity}/revisions", response_model=PersonalQuestionView)
def personal_revision(identity: UUID, body: PersonalQuestionEdit, space: Subjects):
    return PersonalQuestionService(space).revise(identity, body)


@personal_router.post("/questions/{identity}/answer-preview", response_model=LearningOperationView)
def personal_answer_preview(
    identity: UUID, body: PersonalAnswerRequest, space: Subjects, operations: PersonalOperations,
):
    return AnswerCompletion(space).prepare(identity, body, operations)


@personal_router.post("/questions/{identity}/answer/{operation_id}",
                      response_model=PersonalQuestionView | LearningOperationView)
async def personal_answer(
    identity: UUID, operation_id: UUID, space: Subjects, operations: PersonalOperations,
):
    return await AnswerCompletion(space).execute(identity, operation_id, operations)


@personal_router.get("/questions/{identity}/assets/{asset_id}", response_class=FileResponse)
def personal_question_asset(identity: UUID, asset_id: UUID, space: Subjects):
    from backend.app.models.question_asset import QuestionAsset
    from backend.app.services.learning_material_service import MaterialError
    from backend.app.services.question_asset_access import PersonalContentAccess

    bank = PersonalQuestionService(space)
    with space.sessions() as session:
        _, revision = bank.current(session, identity)
        if asset_id not in bank.content(revision).asset_ids():
            raise MaterialError("QUESTION_ASSET_NOT_FOUND", 404)
        asset = session.get(QuestionAsset, asset_id)
        if asset is None or asset.question_id != identity or space.profile is None:
            raise MaterialError("QUESTION_ASSET_NOT_FOUND", 404)
        path, metadata = PersonalContentAccess(session, space.profile)._image_path(asset)
        return FileResponse(path, media_type=metadata.media_type, headers={
            "Cache-Control":"no-store", "X-Content-Type-Options":"nosniff",
            "Content-Security-Policy":"default-src 'none'; sandbox",
        })


@personal_router.post("/imports/{identity}/local-ocr", response_model=PersonalPaperView)
async def personal_local_ocr(identity: UUID, space: Subjects, operations: PersonalOperations):
    from backend.app.services.settings.runtime_snapshot import RuntimeSnapshotService

    return await PersonalPaperImportService(space).process_local_ocr(
        identity, RuntimeSnapshotService(operations.config), limits=operations.limits,
    )
