"""Authenticated paper import HTTP boundary and background wiring."""

from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.ai.ingestion.paper_pipeline import PaperInputError
from backend.app.api.file_storage import file_http_exception
from backend.app.core.config import AppSettings, ConfigurationError, get_settings
from backend.app.core.database import get_db, get_session_factory
from backend.app.core.security import CurrentUser, get_app_settings
from backend.app.schemas.paper_import import (
    CommitRequest,
    CommitResponse,
    CorrectionPayload,
    ExtractedQuestionView,
    PaperImportView,
)
from backend.app.services.file_storage_service import FileStorageError
from backend.app.services.paper_import_service import (
    PaperImportRunner,
    PaperImportService,
)
from backend.app.services.question_correction_service import QuestionCorrectionService

router = APIRouter(prefix="/api/paper-imports", tags=["试卷导入"])


def get_paper_import_service(
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[AppSettings, Depends(get_app_settings)],
) -> PaperImportService:
    return PaperImportService(session, root=settings.storage_root)


ImportService = Annotated[PaperImportService, Depends(get_paper_import_service)]


def get_paper_import_runner(request: Request) -> PaperImportRunner:
    runner = getattr(request.app.state, "paper_import_runner", None)
    if runner is None:
        runner = PaperImportRunner(
            get_session_factory(), settings=request.app.state.settings
        )
        request.app.state.paper_import_runner = runner
    return runner


def recover_interrupted_paper_imports() -> int:
    try:
        return PaperImportRunner(
            get_session_factory(), settings=get_settings()
        ).recover_interrupted()
    except (SQLAlchemyError, ConfigurationError):
        # Existing v1 installations may not yet have the E2 tables.
        return 0


@router.post("", response_model=PaperImportView, status_code=201)
def upload_paper(
    actor: CurrentUser,
    service: ImportService,
    background: BackgroundTasks,
    runner: Annotated[PaperImportRunner, Depends(get_paper_import_runner)],
    course_id: Annotated[UUID, Form()],
    file: Annotated[UploadFile, File()],
):
    try:
        result = service.upload(
            course_id,
            filename=file.filename or "",
            content=file.file.read(),
            actor_id=actor.id,
        )
        background.add_task(runner.run, result.id)
        return result
    except FileStorageError as exc:
        raise file_http_exception(exc) from None
    except PaperInputError as exc:
        raise HTTPException(
            422, detail={"code": exc.code, "message": str(exc)}
        ) from None
    finally:
        file.file.close()


@router.get("", response_model=list[PaperImportView])
def list_papers(course_id: UUID, actor: CurrentUser, service: ImportService):
    try:
        return service.list(course_id, actor_id=actor.id)
    except FileStorageError as exc:
        raise file_http_exception(exc) from None


@router.get("/{identity}", response_model=PaperImportView)
def get_paper(identity: UUID, actor: CurrentUser, service: ImportService):
    try:
        return service.get(identity, actor_id=actor.id)
    except FileStorageError as exc:
        raise file_http_exception(exc) from None


@router.get("/{identity}/questions", response_model=list[ExtractedQuestionView])
def get_questions(identity: UUID, actor: CurrentUser, service: ImportService):
    try:
        return service.questions(identity, actor_id=actor.id)
    except FileStorageError as exc:
        raise file_http_exception(exc) from None


def get_correction_service(
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[AppSettings, Depends(get_app_settings)],
) -> QuestionCorrectionService:
    return QuestionCorrectionService(session, root=settings.storage_root)


CorrectionService = Annotated[
    QuestionCorrectionService, Depends(get_correction_service)
]


@router.patch(
    "/{identity}/questions/{question_id}", response_model=ExtractedQuestionView
)
def patch_question(
    identity: UUID,
    question_id: UUID,
    payload: CorrectionPayload,
    actor: CurrentUser,
    service: CorrectionService,
):
    try:
        return service.patch(identity, question_id, payload, actor_id=actor.id)
    except FileStorageError as exc:
        raise file_http_exception(exc) from None


@router.post("/{identity}/commit", response_model=CommitResponse)
def commit_questions(
    identity: UUID,
    payload: CommitRequest,
    actor: CurrentUser,
    service: CorrectionService,
):
    try:
        return service.commit(identity, payload.question_ids, actor_id=actor.id)
    except FileStorageError as exc:
        raise file_http_exception(exc) from None
