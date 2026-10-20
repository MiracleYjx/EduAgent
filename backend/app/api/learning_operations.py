"""Personal operation API: independent from Setup tokens, exact source boundary."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.exc import SQLAlchemyError

from backend.app.core.settings_security import (
    SettingsSecurityError,
    check_personal_request,
)
from backend.app.schemas.personal_learning import (
    LearningApiError,
    LearningConfirmRequest,
    LearningOperationView,
    LearningPreviewRequest,
)
from backend.app.services.learning_operations import (
    LearningOperations,
    OperationError,
)
from backend.app.services.settings.config_service import ConfigServiceError
from backend.app.services.settings.credential_store import CredentialStoreError
from backend.app.services.settings.management import SettingsManagementError
from backend.app.services.settings.provider_registry import RegistryValidationError
from backend.app.services.settings.runtime_snapshot import RuntimeSnapshotError


def failure(code: str, status: int) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "code": code,
            "message": "操作未完成，请根据状态调整或明确重试。",
            "stage": "learning_operation",
            "retryable": False,
        },
    )


class LearningRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def safe(request: Request):
            try:
                await check_personal_request(request)
                return await original(request)
            except SettingsSecurityError as error:
                return failure(error.code, error.status_code)
            except RequestValidationError:
                return failure("REQUEST_INVALID", 422)
            except OperationError as error:
                status = (
                    404
                    if error.code == "OPERATION_NOT_FOUND"
                    else 503
                    if error.code
                    in {"OPERATION_NOT_AVAILABLE", "OPERATIONS_UNAVAILABLE"}
                    else 409
                )
                return failure(error.code, status)
            except (
                RuntimeSnapshotError,
                SettingsManagementError,
                ConfigServiceError,
                CredentialStoreError,
                RegistryValidationError,
            ):
                return failure("OPERATION_DEPENDENCY_UNAVAILABLE", 503)
            except SQLAlchemyError:
                return failure("OPERATIONS_UNAVAILABLE", 503)

        return safe


router = APIRouter(
    prefix="/api/learning/operations",
    tags=["personal-learning"],
    route_class=LearningRoute,
    responses={
        code: {"model": LearningApiError}
        for code in (403, 404, 409, 413, 415, 422, 503)
    },
)


def service(request: Request) -> LearningOperations:
    value = getattr(request.app.state, "learning_operations", None)
    if value is None:
        with request.app.state.learning_operations_lock:
            value = getattr(request.app.state, "learning_operations", None)
            if value is None:
                runtime = getattr(request.app.state, "settings_runtime", None)
                config = getattr(runtime, "config", None)
                if runtime is None or config is None:
                    raise OperationError("OPERATIONS_UNAVAILABLE")
                from backend.app.api.subjects import get_subject_service
                space = get_subject_service(request)
                if space.profile is None or config.environment_id != space.profile.environment_id:
                    raise OperationError("LEARNING_ENVIRONMENT_MISMATCH")
                # Reuse the selected and verified personal database; legacy
                # AppSettings/business assembly must not choose a second space.
                _store = runtime.credential_store
                value = LearningOperations(sessions=space.sessions, config=config)
                request.app.state.learning_operations = value
    return value


@router.post("/preview", response_model=LearningOperationView)
def preview(
    body: LearningPreviewRequest,
    operations: Annotated[LearningOperations, Depends(service)],
):
    return operations.preview(**body.model_dump())


@router.post("/{operation_id}/confirm", response_model=LearningOperationView)
def confirm(
    operation_id: UUID,
    body: LearningConfirmRequest,
    operations: Annotated[LearningOperations, Depends(service)],
):
    return operations.confirm(operation_id, preview_digest=body.preview_digest)


@router.get("/{operation_id}", response_model=LearningOperationView)
def get_operation(
    operation_id: UUID, operations: Annotated[LearningOperations, Depends(service)]
):
    return operations.view(operation_id)


def install_learning_operations(app):
    import threading

    if getattr(app.state, "learning_operations_installed", False):
        return
    app.state.learning_operations_installed = True

    security = getattr(app.state, "setup_security", None)
    app.state.learning_source_policy = getattr(security, "policy", None)
    app.state.learning_operations_lock = threading.Lock()
    app.include_router(router)
