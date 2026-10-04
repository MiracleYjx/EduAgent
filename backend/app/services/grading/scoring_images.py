"""把已核对的本场题图定位转换为授权原件字节；不新增图片理解轮次。"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session

from backend.app.ai.vision.base import VisionFailure, VisionImage
from backend.app.services.content_validation_service import (
    ContentValidationError,
    ContentValidationService,
)
from backend.app.services.file_storage_service import FileStorageError
from backend.app.services.grading.grading_context import (
    GradingContextError,
    SubjectiveGradingSource,
)


class GradingImageError(GradingContextError):
    """保留图片能力/文件来源错误；失败不是零分。"""

    def __init__(self, code: str, detail: str, *, retryable: bool = False) -> None:
        self.error_code = code  # type: ignore[misc]  # Actual upstream source code.
        self.retryable = retryable  # type: ignore[misc]  # Preserve source retry semantics.
        super().__init__(detail)


def prepare_scoring_images(
    session: Session,
    source: SubjectiveGradingSource,
    *,
    root: Path | None = None,
) -> tuple[VisionImage, ...]:
    fixed = source.scoring_input
    if fixed is None or not fixed.assets:
        return ()
    evidence = fixed.verified_image_conditions
    if evidence is None or source.teacher_id is None:
        raise GradingImageError(
            "VISION_REVIEW_REQUIRED", "本场题图缺少当前真实人工核对或课程教师身份。"
        )
    try:
        actor_id = UUID(str(source.teacher_id))
        actual_refs, prepared = ContentValidationService(
            session, root=root
        )._read_images(
            evidence.input_refs,
            actor_id,
        )
        if actual_refs != evidence.input_refs:
            raise GradingImageError(
                "FILE_CONTENT_CHANGED", "题图实际尺寸或格式与本场已核对输入不一致。"
            )
        return tuple(VisionImage.from_bytes(image.data) for image in prepared)
    except (ContentValidationError, FileStorageError) as error:
        raise GradingImageError(error.code, str(error)) from None
    except VisionFailure as error:
        raise GradingImageError(
            error.code, error.message, retryable=error.retryable
        ) from None
    except ValueError:
        raise GradingImageError(
            "VISION_REVIEW_REQUIRED", "本场题图的真实核对身份非法。"
        ) from None
