"""Authorize whole-group images, execute Vision, and persist every actual outcome."""
from __future__ import annotations

import asyncio
import math
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from backend.app.ai.vision.base import VisionFailure, VisionImage, VisionResult
from backend.app.ai.vision.provider import (
    ProviderVisionUnderstanding,
    create_vision_provider,
)
from backend.app.core.config import AppSettings, get_settings
from backend.app.schemas.image_assessment import (
    ImageAssessmentView,
    ImageIssue,
    ImageUnderstandingResult,
    MachineCondition,
    Observation,
    TechnicalError,
)
from backend.app.schemas.paper_import import PixelRegion
from backend.app.services.content_validation_service import (
    ContentValidationService,
    PreparedImage,
    PreparedImageRun,
)
from backend.app.services.file_storage_service import FileStorageError

OwnerKind = Literal["question", "extracted_question"]


def _map_region(region: PixelRegion | None, image: PreparedImage) -> PixelRegion | None:
    """Convert verified asset pixels to source pixels only when mapping is known."""
    if region is None:
        return None
    x0, y0, x1, y1 = region.bbox
    if not all(math.isfinite(value) for value in region.bbox) or x0 < 0 or y0 < 0 or x1 > image.width or y1 > image.height or x1 <= x0 or y1 <= y0:
        raise ValueError("输出框超出本次实际题图像素。")
    if image.source_page_id is None:
        return region
    if image.region is None:
        return None
    sx0, sy0, sx1, sy1 = image.region.bbox
    if sx1 - sx0 != image.width or sy1 - sy0 != image.height:
        return None
    return PixelRegion(bbox=(x0 + sx0, y0 + sy0, x1 + sx0, y1 + sy0))


def bind_vision_result(result: VisionResult, prepared: PreparedImageRun) -> ImageUnderstandingResult:
    """Identity and source mapping come exclusively from the authorized producer."""
    images = {image.image_index: image for image in prepared.images}
    observations = []
    conditions = []
    issues = []
    for observation in result.observations:
        image = images[observation.image_index]
        region = _map_region(PixelRegion(bbox=observation.bbox) if observation.bbox is not None else None, image)
        observations.append(Observation(image_index=observation.image_index, kind=observation.kind,
            description=observation.description, bbox=region.bbox if region else None))
    for condition in result.conditions:
        image = images[condition.image_index]
        conditions.append(MachineCondition(condition_id=uuid4(), asset_id=image.asset_id,
            image_index=condition.image_index, text=condition.text,
            evidence_region=_map_region(condition.evidence_region, image)))
    for issue in result.unresolved_issues:
        ids = None if issue.image_indices is None else [images[index].asset_id for index in issue.image_indices]
        issues.append(ImageIssue(issue_id=uuid4(), asset_ids=ids, message=issue.message))
    return ImageUnderstandingResult(observations=observations, conditions=conditions,
        unresolved_issues=issues, requires_manual_review=result.requires_manual_review)


class ImageUnderstandingService:
    def __init__(self, session: Session, *, root: Path | None = None, settings: AppSettings | None = None):
        self.persistence = ContentValidationService(session, root=root)
        self.settings = settings or get_settings()

    async def understand(self, owner_kind: OwnerKind, owner_id: UUID, *,
        task: str = "提取题图中作答所需条件", actor_id: UUID) -> ImageAssessmentView:
        # Commit running before file/capability preflight; no database lock spans a cloud call.
        run = self.persistence.start_image_run(owner_kind, owner_id, task=task,
            actor_id=actor_id, executor_name="vision")
        provider = None
        provenance = None
        vision = None
        bound = None
        failure = None
        close_error: BaseException | None = None
        try:
            prepared = self.persistence.prepare_image_run(owner_kind, owner_id, run.id, actor_id=actor_id)
            provider = create_vision_provider(self.settings)
            vision = ProviderVisionUnderstanding(provider)
            result = await vision.understand(context=prepared.context, task=task,
                images=[VisionImage(data=image.data, mime_type=image.mime_type,
                    width=image.width, height=image.height) for image in prepared.images])
            provenance = result.provenance
            try:
                bound = bind_vision_result(result, prepared)
            except (ValueError, KeyError) as exc:
                raise VisionFailure("VISION_OUTPUT_INVALID", "图片输出与本次实际资产/坐标不对应。",
                    stage="output", retryable=False, cause=str(exc), provenance=provenance) from exc
        except asyncio.CancelledError:
            provenance = vision.call_provenance if vision is not None else None
            self.persistence.finish_image_run(owner_kind, owner_id, run.id, actor_id=actor_id,
                error=TechnicalError(code="VISION_CALL_FAILED", message="图片调用已取消。",
                    stage="call", retryable=True, cause="CancelledError"), provenance=provenance)
            raise
        except VisionFailure as exc:
            provenance = exc.provenance
            failure = TechnicalError(code=exc.code, message=exc.message, stage=exc.stage,
                retryable=exc.retryable, cause=exc.cause)
        except FileStorageError as exc:
            failure = TechnicalError(code=exc.code, message=str(exc), stage="file" if exc.code.startswith("FILE_") else "input",
                retryable=False, cause=type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 — 执行边界保存真实失败，避免 SDK request 泄漏。
            # Keep the actual error type without echoing request bytes or credentials.
            provenance = vision.call_provenance if vision is not None else None
            failure = TechnicalError(code="VISION_CALL_FAILED", message="图片理解执行失败。",
                stage="execution", retryable=False, cause=type(exc).__name__)
        finally:
            if provider is not None:
                try:
                    await provider.aclose()
                except (asyncio.CancelledError, Exception) as exc:  # noqa: BLE001
                    # 即使关闭被取消也先保存已取得的真实调用结果，再传播清理失败。
                    close_error = exc
        # Persistence failures propagate as actual failures; never retry an ended run as an error.
        view = self.persistence.finish_image_run(owner_kind, owner_id, run.id, actor_id=actor_id,
            result=bound if failure is None else None, error=failure, provenance=provenance)
        if close_error is not None:
            raise close_error
        return view
