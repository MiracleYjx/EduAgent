"""Authenticated file bytes; no public/static storage directory."""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from backend.app.core.config import AppSettings
from backend.app.core.database import get_db
from backend.app.core.security import CurrentUser, get_app_settings
from backend.app.services.file_storage_service import (
    FileStorageError,
    FileStorageService,
)

router = APIRouter(prefix="/api/files", tags=["文件"])


def get_file_storage_service(
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[AppSettings, Depends(get_app_settings)],
) -> FileStorageService:
    return FileStorageService(session, root=settings.storage_root)


def file_http_exception(error: FileStorageError) -> HTTPException:
    return HTTPException(
        status_code=error.http_status,
        detail={"code": error.code, "message": str(error), "current_status": error.current_status},
    )


@router.get("/{file_id}")
def get_file(
    file_id: str,
    actor: CurrentUser,
    service: Annotated[FileStorageService, Depends(get_file_storage_service)],
) -> FileResponse:
    try:
        path, view = service.download(file_id, actor_id=actor.id)
        return FileResponse(
            path=path, filename=view.original_filename,
            media_type=view.media_type or "application/octet-stream",
        )
    except FileStorageError as exc:
        raise file_http_exception(exc) from None
