"""Personal operation API: independent from Setup tokens, exact source boundary."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.exc import SQLAlchemyError

from backend.app.api import learning_errors
from backend.app.core.settings_security import (
    SettingsSecurityError,
    check_personal_request,
)
from backend.app.schemas.personal_learning import (
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


def public_operation(value: LearningOperationView) -> LearningOperationView:
    """A fee preview must not disclose frozen answers while drafts are editable."""
    if value.purpose.startswith("material_index:"):
        return value.model_copy(update={"steps":[{k:v for k,v in step.items()
            if k not in {"material_input", "embedding_inputs", "result", "messages"}} for step in value.steps]})
    return value.model_copy(update={"steps": [
        {k: v for k, v in step.items() if k not in (
            {"grading_input", "messages", "result"} if value.purpose.startswith("teacher:") else {"grading_input", "messages"})}
        if step.get("grading_input") is not None or value.purpose.startswith("teacher:") else step for step in value.steps
    ]})


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
    responses=learning_errors(403, 404, 409, 413, 415, 422, 503),
)


def service(request: Request) -> LearningOperations:
    from backend.app.services.learning_test_validation import refresh_test_validation
    try:
        refresh_test_validation(request.app.state)
    except (ValueError, OSError):
        raise OperationError("PAID_SEND_NOT_AUTHORIZED") from None
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
                policy = getattr(request.app.state, "learning_paid_validation", None)
                from backend.app.services.learning_paid_validation import (
                    PaidValidationPolicy,
                )
                if policy is not None and not isinstance(policy, PaidValidationPolicy):
                    raise OperationError("PAID_SEND_NOT_AUTHORIZED")
                value = LearningOperations(sessions=space.sessions, config=config,
                    token_counter=policy.token_counter if policy is not None else None,
                    task_model_budgets=policy.manifest.task_model_budgets if policy is not None else None)
                request.app.state.learning_operations = value
    # A reviewed manifest may be installed after no-send previews have frozen
    # their operation IDs and snapshots. Cached services still apply that exact
    # manifest at the next preflight/send; no route manufactures authorization.
    policy = getattr(request.app.state, "learning_paid_validation", None)
    if policy is not None:
        from backend.app.services.learning_paid_validation import PaidValidationPolicy
        if not isinstance(policy, PaidValidationPolicy):
            raise OperationError("PAID_SEND_NOT_AUTHORIZED")
        value.paid_authorization = lambda view: policy.authorize(value, view)
        value.token_counter = policy.token_counter
        value.task_model_budgets = dict(policy.manifest.task_model_budgets)
    elif getattr(value, "_paid_validation_installed", False):
        value.paid_authorization = None
        value.token_counter = None
        value.task_model_budgets = {}
    value._paid_validation_installed = policy is not None
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
    request: Request,
    operations: Annotated[LearningOperations, Depends(service)],
):
    if operations.view(operation_id).purpose.startswith("material_index:"):
        from backend.app.api.subjects import get_subject_service
        from backend.app.services.learning_material_service import (
            LearningMaterialService,
        )
        from backend.app.services.material_indexing import MaterialIndexing

        identity = UUID(operations.view(operation_id).purpose.split(":",1)[1])
        return public_operation(MaterialIndexing(LearningMaterialService(get_subject_service(request)), operations).confirm(
            identity, operation_id, body.preview_digest))
    if operations.view(operation_id).purpose.startswith("teacher:"):
        from backend.app.api.subjects import get_subject_service
        from backend.app.services.teacher_service import TeacherService

        return public_operation(TeacherService(get_subject_service(request),operations).confirm(operation_id,body.preview_digest))
    if operations.view(operation_id).purpose.startswith(("practice_grading:", "dispute_regrade:")):
        from backend.app.api.subjects import get_subject_service
        from backend.app.services.personal_grading_operations import (
            PersonalGradingOperations,
        )
        return public_operation(PersonalGradingOperations(get_subject_service(request), operations).confirm(
            operation_id, body.preview_digest))
    return operations.confirm(operation_id, preview_digest=body.preview_digest)


@router.get("/{operation_id}", response_model=LearningOperationView)
def get_operation(
    operation_id: UUID, operations: Annotated[LearningOperations, Depends(service)]
):
    return public_operation(operations.view(operation_id))


def install_learning_operations(app):
    import threading

    if getattr(app.state, "learning_operations_installed", False):
        return
    app.state.learning_operations_installed = True

    security = getattr(app.state, "setup_security", None)
    app.state.learning_source_policy = getattr(security, "policy", None)
    app.state.learning_operations_lock = threading.Lock()
    app.include_router(router)
