"""Explicit local OCR preview and result review for study materials."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MaterialOCRView(BaseModel):
    id: UUID
    material_id: UUID
    preview_digest: str
    runtime_snapshot_id: UUID
    state: Literal["prepared", "confirmed", "running", "review_required", "succeeded", "partial", "failed"]
    page_count: int
    completed_pages: int = 0
    adapter: Literal["rapidocr"] = "rapidocr"
    model: str
    request_budget: Literal[0] = 0
    error_code: str | None = None


class MaterialOCRPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=160)
