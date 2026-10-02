"""教师题图管理；字节读取复用统一授权文件入口。"""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from backend.app.api.file_storage import file_http_exception
from backend.app.core.config import AppSettings
from backend.app.core.database import get_db
from backend.app.core.security import CurrentUser, get_app_settings
from backend.app.schemas.paper_import import StagedAsset
from backend.app.schemas.question_assets import AssetLinkRequest, QuestionAssetView
from backend.app.services.file_storage_service import FileStorageError
from backend.app.services.question_asset_service import QuestionAssetService

router = APIRouter(prefix="/api", tags=["题图"])


def get_question_asset_service(session: Annotated[Session, Depends(get_db)], settings: Annotated[AppSettings, Depends(get_app_settings)]) -> QuestionAssetService:
    return QuestionAssetService(session, root=settings.storage_root)


AssetService = Annotated[QuestionAssetService, Depends(get_question_asset_service)]


class AssetOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_ids: list[UUID] = Field(max_length=5)


@router.get("/extracted-questions/{extracted_id}/assets", response_model=list[StagedAsset])
def list_staged(extracted_id: UUID, actor: CurrentUser, service: AssetService):
    try:
        return service.list_staged(extracted_id, actor_id=actor.id)
    except FileStorageError as exc:
        raise file_http_exception(exc) from None


@router.post("/extracted-questions/{extracted_id}/assets", response_model=StagedAsset, status_code=201)
def create_staged(extracted_id: UUID, payload: AssetLinkRequest, actor: CurrentUser, service: AssetService):
    try:
        return service.create_staged(extracted_id, payload, actor_id=actor.id)
    except FileStorageError as exc:
        raise file_http_exception(exc) from None


@router.put("/extracted-questions/{extracted_id}/assets", response_model=list[StagedAsset])
def replace_staged(extracted_id: UUID, payload: list[StagedAsset], actor: CurrentUser, service: AssetService):
    try:
        return service.replace_staged(extracted_id, payload, actor_id=actor.id)
    except FileStorageError as exc:
        raise file_http_exception(exc) from None


@router.delete("/extracted-questions/{extracted_id}/assets/{asset_id}", status_code=204)
def remove_staged(extracted_id: UUID, asset_id: UUID, actor: CurrentUser, service: AssetService):
    try:
        service.remove_staged(extracted_id, asset_id, actor_id=actor.id)
        return Response(status_code=204)
    except FileStorageError as exc:
        raise file_http_exception(exc) from None


@router.get("/questions/{question_id}/assets", response_model=list[QuestionAssetView])
def list_question(question_id: UUID, actor: CurrentUser, service: AssetService):
    try:
        return service.list_question(question_id, actor_id=actor.id)
    except FileStorageError as exc:
        raise file_http_exception(exc) from None


@router.post("/questions/{question_id}/assets", response_model=QuestionAssetView, status_code=201)
def link_question(question_id: UUID, payload: AssetLinkRequest, actor: CurrentUser, service: AssetService):
    try:
        return service.link_question(question_id, payload, actor_id=actor.id)
    except FileStorageError as exc:
        raise file_http_exception(exc) from None


@router.post("/questions/{question_id}/assets/upload", response_model=QuestionAssetView, status_code=201)
async def upload_question(question_id: UUID, actor: CurrentUser, service: AssetService, file: Annotated[UploadFile, File()], asset_type: Annotated[str, Form()] = "figure", caption: Annotated[str | None, Form()] = None):
    try:
        return service.upload_question(question_id, content=await file.read(), asset_type=asset_type, caption=caption, actor_id=actor.id)
    except FileStorageError as exc:
        raise file_http_exception(exc) from None
    finally:
        await file.close()


@router.put("/questions/{question_id}/assets/order", response_model=list[QuestionAssetView])
def reorder_question(question_id: UUID, payload: AssetOrderRequest, actor: CurrentUser, service: AssetService):
    try:
        return service.reorder_question(question_id, payload.asset_ids, actor_id=actor.id)
    except FileStorageError as exc:
        raise file_http_exception(exc) from None


@router.delete("/questions/{question_id}/assets/{asset_id}", status_code=204)
def remove_question(question_id: UUID, asset_id: UUID, actor: CurrentUser, service: AssetService):
    try:
        service.remove_question(question_id, asset_id, actor_id=actor.id)
        return Response(status_code=204)
    except FileStorageError as exc:
        raise file_http_exception(exc) from None
