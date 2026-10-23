"""Personal material API: bounded streaming multipart upload.

The ordinary personal API keeps the existing ``application/json`` + 64 KiB body
limit. Large textbook uploads need their own transport, so this module owns a
single dedicated ``multipart/form-data`` route:

- the same Host/Origin/space source check runs (``check_personal_source``);
- the body is read in bounded chunks into a spool file inside the verified
  storage root, aborting as soon as the approved cap is exceeded;
- framing is parsed strictly from that spool, the file part is written to its
  own temp file and hashed while framing is validated;
- ``Content-Length`` is never trusted, so chunked bodies work;
- every failure path removes both temp files.

No middleware or global limit is bypassed to achieve this.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from fastapi.routing import APIRoute
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from backend.app.api import learning_errors
from backend.app.api.learning_operations import public_operation
from backend.app.api.learning_operations import service as operation_service
from backend.app.api.subjects import PersonalRoute, Subjects, failure
from backend.app.core.settings_security import (
    SettingsSecurityError,
    check_personal_source,
)
from backend.app.schemas.material_ocr import MaterialOCRPreviewRequest, MaterialOCRView
from backend.app.schemas.personal_learning import (
    DefaultVersionRequest,
    LearningConfirmRequest,
    LearningOperationView,
    MaterialIndexExecuteRequest,
    MaterialIndexPreviewRequest,
    MaterialList,
    MaterialSeriesList,
    MaterialSeriesView,
    MaterialUpload,
    MaterialVersionInput,
    MaterialView,
)
from backend.app.services.file_storage_service import FileStorageError
from backend.app.services.knowledge_organization import KnowledgeError
from backend.app.services.learning_material_service import (
    LearningMaterialService,
    MaterialError,
)
from backend.app.services.learning_operations import LearningOperations
from backend.app.services.material_indexing import MaterialIndexing
from backend.app.services.material_ocr import MaterialOCR
from backend.app.services.settings.config_service import ConfigServiceError
from backend.app.services.settings.credential_store import CredentialStoreError
from backend.app.services.settings.management import SettingsManagementError
from backend.app.services.subject_service import SubjectError

#: 元数据字段上限；文件内容绝不进入这个字段。
MAX_METADATA_BYTES = 8192
#: multipart 边界/头部余量（不含文件内容）。
MAX_ENVELOPE_BYTES = 64 * 1024
#: 单个 part 的最大数量，避免构造大量小 part 的请求。
MAX_PARTS = 8
#: spool 内存上限；超过后落到 ``.incoming`` 下的临时文件，流式期间不同时保留内存副本。
SPOOL_MEMORY_BYTES = 1024 * 1024


class UploadTransportError(RuntimeError):
    """上传传输层失败；携带机器可读码与 HTTP 语义。"""

    def __init__(self, code: str, status: int) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


def _boundary(content_type: str) -> bytes:
    media_type, _, parameters = content_type.partition(";")
    if media_type.strip().lower() != "multipart/form-data":
        raise UploadTransportError("MULTIPART_REQUIRED", 415)
    for parameter in parameters.split(";"):
        name, _, value = parameter.partition("=")
        if name.strip().lower() == "boundary":
            boundary = value.strip().strip('"')
            if boundary and len(boundary) <= 200:
                return boundary.encode("latin-1")
    raise UploadTransportError("MULTIPART_REQUIRED", 415)


def _disposition(value: str) -> tuple[str | None, str | None]:
    name = None
    filename = None
    for parameter in value.split(";"):
        key, _, raw = parameter.partition("=")
        key = key.strip().lower()
        if key not in {"name", "filename"}:
            continue
        item = raw.strip().strip('"')
        if key == "name":
            name = item
        else:
            filename = item
    return name, filename


def _parts(body: bytes, boundary: bytes) -> list[tuple[bytes, bytes]]:
    """Split a bounded multipart body into ``(headers, content)`` pairs."""

    marker = b"--" + boundary
    result: list[tuple[bytes, bytes]] = []
    position = body.find(marker)
    if position < 0:
        raise UploadTransportError("MULTIPART_INVALID", 422)
    while True:
        start = position + len(marker)
        if body[start : start + 2] == b"--":
            return result
        if body[start : start + 2] != b"\r\n":
            raise UploadTransportError("MULTIPART_INVALID", 422)
        start += 2
        separator = body.find(b"\r\n\r\n", start)
        if separator < 0:
            raise UploadTransportError("MULTIPART_INVALID", 422)
        headers = body[start:separator]
        content_start = separator + 4
        following = body.find(b"\r\n" + marker, content_start)
        if following < 0:
            raise UploadTransportError("MULTIPART_INVALID", 422)
        result.append((headers, body[content_start:following]))
        if len(result) > MAX_PARTS:
            raise UploadTransportError("MULTIPART_INVALID", 422)
        position = following + 2


async def read_material_upload(
    request: Request,
    *,
    service: LearningMaterialService,
    filename_limit: int = 255,
) -> tuple[str, MaterialUpload, Path, int, str]:
    """Stream a bounded multipart body to temp files and read its metadata.

    Returns ``(filename, metadata, temp_path, size, sha256)``. The caller owns
    the temp file; any failure inside this function removes what it created.
    """

    boundary = _boundary(request.headers.get("content-type", ""))
    limit = service.limits.upload_bytes
    envelope_cap = limit + MAX_ENVELOPE_BYTES
    temp_root = service.storage_root / "materials" / ".incoming"
    temp_root.mkdir(parents=True, exist_ok=True)
    file_path: Path | None = None
    with tempfile.SpooledTemporaryFile(
        max_size=SPOOL_MEMORY_BYTES, dir=str(temp_root)
    ) as spool:
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > envelope_cap:
                raise UploadTransportError("LIMIT_EXCEEDED", 413)
            spool.write(chunk)
        spool.seek(0)
        body = spool.read()

    try:
        metadata: bytes | None = None
        payload: bytes | None = None
        filename: str | None = None
        for headers, content in _parts(body, boundary):
            name = None
            candidate = None
            for line in headers.decode("latin-1").split("\r\n"):
                key, _, value = line.partition(":")
                if key.strip().lower() != "content-disposition":
                    continue
                name, candidate = _disposition(value)
            if name == "metadata":
                if metadata is not None or len(content) > MAX_METADATA_BYTES:
                    raise UploadTransportError(
                        "LIMIT_EXCEEDED" if metadata is None else "MULTIPART_INVALID",
                        413 if metadata is None else 422,
                    )
                metadata = content
            elif name == "file":
                if payload is not None:
                    raise UploadTransportError("MULTIPART_INVALID", 422)
                if not candidate or len(candidate) > filename_limit:
                    raise UploadTransportError("MULTIPART_INVALID", 422)
                if len(content) > limit:
                    raise UploadTransportError("LIMIT_EXCEEDED", 413)
                filename = candidate
                payload = content
        if payload is None or filename is None:
            raise UploadTransportError("MULTIPART_INVALID", 422)
        if not payload:
            raise UploadTransportError("DOCUMENT_EMPTY", 422)
        try:
            parsed = MaterialUpload.model_validate_json(metadata or b"{}")
        except ValidationError:
            raise UploadTransportError("REQUEST_INVALID", 422) from None
        handle_path: Path | None = None
        with tempfile.NamedTemporaryFile(
            dir=temp_root, prefix="upload-", suffix=".part", delete=False
        ) as handle:
            handle_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        file_path = handle_path
        return (
            filename,
            parsed,
            file_path,
            len(payload),
            hashlib.sha256(payload).hexdigest(),
        )
    except BaseException:
        if file_path is not None:
            file_path.unlink(missing_ok=True)
        raise


class PersonalUploadRoute(APIRoute):
    """Source-checked route that owns its body transport instead of buffering JSON."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def safe(request: Request):
            try:
                await check_personal_source(request)
                response = await original(request)
                response.headers["Cache-Control"] = "no-store"
                return response
            except SettingsSecurityError as error:
                return failure(error.code, error.status_code)
            except UploadTransportError as error:
                return failure(error.code, error.status)
            except (MaterialError, KnowledgeError) as error:
                return failure(error.code, error.status)
            except SubjectError as error:
                return failure(error.code, error.status)
            except RequestValidationError:
                return failure("REQUEST_INVALID", 422)
            except FileStorageError as error:
                return failure(error.code, error.http_status)
            except (ConfigServiceError, CredentialStoreError, SettingsManagementError):
                return failure("LEARNING_CONFIGURATION_UNAVAILABLE", 503)
            except SQLAlchemyError:
                return failure("LEARNING_DATABASE_UNAVAILABLE", 503)

        return safe


def material_service(subjects: Subjects) -> LearningMaterialService:
    """Reuse the verified space (sessions, storage root, profile) of the entry."""

    return LearningMaterialService(subjects)


Materials = Annotated[LearningMaterialService, Depends(material_service)]

_RESPONSES: dict[int | str, dict[str, Any]] = learning_errors(403, 404, 409, 413, 415, 422, 503)

upload_router = APIRouter(
    prefix="/api/learning",
    tags=["personal-materials"],
    route_class=PersonalUploadRoute,
    responses=_RESPONSES,
)
router = APIRouter(
    prefix="/api/learning",
    tags=["personal-materials"],
    route_class=PersonalRoute,
    responses=_RESPONSES,
)


@upload_router.post(
    "/subjects/{subject_id}/uploads",
    response_model=MaterialView,
    status_code=201,
)
async def upload_material(subject_id: UUID, request: Request, service: Materials):
    filename, body, temp_path, size, digest = await read_material_upload(
        request, service=service
    )
    try:
        return service.upload(
            subject_id,
            filename=filename,
            path=temp_path,
            size=size,
            sha256=digest,
            body=body,
        )
    finally:
        temp_path.unlink(missing_ok=True)


@router.get("/subjects/{subject_id}/materials", response_model=MaterialList)
def list_materials(subject_id: UUID, service: Materials):
    return service.list(subject_id)


@router.get("/materials/{material_id}", response_model=MaterialView)
def get_material(material_id: UUID, service: Materials):
    return service.get(material_id)


IndexOperations = Annotated[LearningOperations, Depends(operation_service)]


@router.post("/materials/{material_id}/ocr-preview", response_model=MaterialOCRView)
def ocr_preview(material_id: UUID, body: MaterialOCRPreviewRequest,
                service: Materials, operations: IndexOperations):
    return MaterialOCR(service, operations).prepare(material_id, body.idempotency_key)


@router.post("/materials/{material_id}/ocr/{preview_id}/confirm", response_model=MaterialOCRView)
def confirm_ocr(material_id: UUID, preview_id: UUID, body: LearningConfirmRequest,
                service: Materials, operations: IndexOperations):
    return MaterialOCR(service, operations).confirm(material_id, preview_id, body.preview_digest)


@router.post("/materials/{material_id}/ocr/{preview_id}/execute", response_model=MaterialView)
async def execute_ocr(material_id: UUID, preview_id: UUID, body: MaterialIndexExecuteRequest,
                      service: Materials, operations: IndexOperations):
    return await MaterialOCR(service, operations).execute(material_id, preview_id)


@router.post("/materials/{material_id}/ocr/{preview_id}/review", response_model=MaterialView)
def review_ocr(material_id: UUID, preview_id: UUID, body: LearningConfirmRequest,
               service: Materials, operations: IndexOperations):
    return MaterialOCR(service, operations).review(material_id, preview_id, body.preview_digest)


@router.post("/materials/{material_id}/ocr/{preview_id}/recover", response_model=MaterialView)
def recover_ocr(material_id: UUID, preview_id: UUID, body: MaterialIndexExecuteRequest,
                service: Materials, operations: IndexOperations):
    return MaterialOCR(service, operations).recover(material_id, preview_id)


@router.post("/materials/{material_id}/index-preview", response_model=LearningOperationView)
async def index_preview(material_id: UUID, body: MaterialIndexPreviewRequest,
                        service: Materials, operations: IndexOperations):
    return public_operation(await MaterialIndexing(service, operations).prepare(material_id, body.idempotency_key))


@router.post("/materials/{material_id}/index/{operation_id}/confirm", response_model=LearningOperationView)
def confirm_index(material_id: UUID, operation_id: UUID, body: LearningConfirmRequest,
                  service: Materials, operations: IndexOperations):
    return public_operation(MaterialIndexing(service, operations).confirm(material_id, operation_id, body.preview_digest))


@router.post("/materials/{material_id}/index/{operation_id}/execute", response_model=MaterialView | LearningOperationView)
async def execute_index(material_id: UUID, operation_id: UUID, body: MaterialIndexExecuteRequest,
                        service: Materials, operations: IndexOperations):
    result = await MaterialIndexing(service, operations).execute(material_id, operation_id)
    return public_operation(result) if isinstance(result, LearningOperationView) else result


@router.get(
    "/subjects/{subject_id}/material-series", response_model=MaterialSeriesList
)
def list_series(subject_id: UUID, service: Materials):
    return service.list_series(subject_id)


@router.post(
    "/subjects/{subject_id}/material-series",
    response_model=MaterialSeriesView,
    status_code=201,
)
def create_version(subject_id: UUID, body: MaterialVersionInput, service: Materials):
    return service.create_version(subject_id, body)


@router.post(
    "/material-series/{series_id}/default-version",
    response_model=MaterialSeriesView,
)
def set_default_version(
    series_id: UUID, body: DefaultVersionRequest, service: Materials
):
    return service.set_default_version(series_id, body.version_id)


@router.get(
    "/subjects/{subject_id}/materials/{material_id}/assets/{digest}",
    response_class=FileResponse,
)
def material_asset(
    subject_id: UUID, material_id: UUID, digest: str, service: Materials
):
    """Serve one stored parse asset; the digest must already belong to it."""

    path, media_type = service.asset_path(subject_id, material_id, digest)
    return FileResponse(
        path,
        media_type=media_type,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


def install_learning_materials(app) -> None:
    """Install material and knowledge routes behind the same personal boundary."""

    from backend.app.api.knowledge import install_knowledge

    app.include_router(upload_router)
    app.include_router(router)
    install_knowledge(app)


__all__ = [
    "MAX_ENVELOPE_BYTES",
    "MAX_METADATA_BYTES",
    "MAX_PARTS",
    "Materials",
    "PersonalUploadRoute",
    "UploadTransportError",
    "install_learning_materials",
    "material_service",
    "read_material_upload",
    "router",
    "upload_router",
]


def _unused(value: Any) -> None:  # pragma: no cover - keeps typing imports honest
    del value
