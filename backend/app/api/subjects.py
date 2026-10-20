"""Personal entry, independent of legacy business and Setup sessions."""

import threading
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from backend.app.core.settings_security import (
    SettingsSecurityError,
    check_personal_request,
)
from backend.app.schemas.personal_learning import (
    LearningApiError,
    SubjectCreate,
    SubjectList,
    SubjectPatch,
    SubjectView,
)
from backend.app.services.file_storage_service import FileStorageError
from backend.app.services.knowledge_organization import KnowledgeError
from backend.app.services.learning_material_service import MaterialError
from backend.app.services.learning_operations import OperationError
from backend.app.services.settings.config_service import ConfigServiceError
from backend.app.services.settings.credential_store import CredentialStoreError
from backend.app.services.settings.management import SettingsManagementError
from backend.app.services.settings.provider_registry import RegistryValidationError
from backend.app.services.settings.runtime_snapshot import (
    RuntimeSnapshotError,
    RuntimeSnapshotService,
)
from backend.app.services.subject_service import SubjectError, SubjectService


def failure(code: str, status: int) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "code": code,
            "message": "个人学习操作未完成，请检查配置或请求。",
            "stage": "personal_learning",
            "retryable": False,
        },
        headers={"Cache-Control": "no-store"},
    )


class PersonalRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def safe(request: Request):
            try:
                await check_personal_request(request)
                response = await original(request)
                response.headers["Cache-Control"] = "no-store"
                return response
            except SettingsSecurityError as error:
                return failure(error.code, error.status_code)
            except SubjectError as error:
                return failure(error.code, error.status)
            except (MaterialError, KnowledgeError) as error:
                return failure(error.code, error.status)
            except OperationError as error:
                return failure(error.code, 404 if error.code == "OPERATION_NOT_FOUND" else 409)
            except RequestValidationError:
                return failure("REQUEST_INVALID", 422)
            except FileStorageError as error:
                return failure(error.code, error.http_status)
            except (ConfigServiceError, CredentialStoreError, SettingsManagementError,
                    RegistryValidationError, RuntimeSnapshotError):
                return failure("LEARNING_CONFIGURATION_UNAVAILABLE", 503)
            except SQLAlchemyError:
                return failure("LEARNING_DATABASE_UNAVAILABLE", 503)

        return safe


router = APIRouter(
    prefix="/api/learning/subjects",
    tags=["personal-learning"],
    route_class=PersonalRoute,
    responses={
        status: {"model": LearningApiError}
        for status in (403, 404, 409, 413, 415, 422, 503)
    },
)


def get_subject_service(request: Request) -> SubjectService:
    service = getattr(request.app.state, "subject_service", None)
    if service is None:
        with request.app.state.subject_service_lock:
            service = getattr(request.app.state, "subject_service", None)
            if service is None:
                runtime = getattr(request.app.state, "settings_runtime", None)
                if runtime is None:
                    raise SubjectError("LEARNING_CONFIGURATION_UNAVAILABLE")
                url, profile = runtime.personal_learning_database()
                security = getattr(request.app.state, "setup_security", None)
                if (
                    security is None
                    or security.environment_id != profile.environment_id
                ):
                    raise SubjectError("LEARNING_ENVIRONMENT_MISMATCH")
                engine = create_engine(
                    url, connect_args={"connect_timeout": 3}, pool_pre_ping=True
                )
                try:
                    from backend.app.services.settings.readiness import (
                        discover_migration_head,
                    )

                    with engine.connect() as connection:
                        head = connection.scalar(
                            text("SELECT version_num FROM alembic_version")
                        )
                        if head != discover_migration_head():
                            raise SubjectError("LEARNING_DATABASE_NOT_INITIALIZED")
                    service = SubjectService(
                        sessionmaker(engine, expire_on_commit=False),
                        storage_root=profile.storage_root,
                        owned_engine=engine,
                    )
                    service.profile = profile
                    service.config = getattr(runtime, "config", None)
                    if service.config is not None:
                        service.grading_operations_required = True
                        service.runtime_snapshots = RuntimeSnapshotService(
                            service.config
                        )
                    request.app.state.subject_service = service
                except BaseException:
                    engine.dispose()
                    raise
    return service


Subjects = Annotated[SubjectService, Depends(get_subject_service)]


@router.get("", response_model=SubjectList)
def list_subjects(service: Subjects):
    return service.list()


@router.post("", response_model=SubjectView, status_code=201)
def create_subject(body: SubjectCreate, service: Subjects):
    return service.create(body)


@router.get("/{subject_id}", response_model=SubjectView)
def get_subject(subject_id: UUID, service: Subjects):
    return service.get(subject_id)


@router.patch("/{subject_id}", response_model=SubjectView)
def patch_subject(subject_id: UUID, body: SubjectPatch, service: Subjects):
    return service.patch(subject_id, body)


def install_subjects(app: FastAPI) -> None:
    from backend.app.api.learning_operations import install_learning_operations

    install_learning_operations(app)
    from backend.app.api.learning_content import install_learning_content

    app.state.subject_service_lock = threading.Lock()
    security = getattr(app.state, "setup_security", None)
    app.state.learning_source_policy = getattr(security, "policy", None)
    app.include_router(router)
    install_learning_content(app)
    from backend.app.api.learning_materials import install_learning_materials

    install_learning_materials(app)
    from backend.app.api.paper_import import personal_router

    app.include_router(personal_router)
    from backend.app.api.question_identity import router as identity_router

    app.include_router(identity_router)

    from backend.app.api.practices import router as practice_router
    from backend.app.api.question_generation import personal_generation_router

    app.include_router(practice_router)
    app.include_router(personal_generation_router)
    from backend.app.api.score_disputes import install_score_disputes

    install_score_disputes(app)
