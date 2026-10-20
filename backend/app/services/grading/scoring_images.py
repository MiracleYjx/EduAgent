"""把已核对的本场题图定位转换为授权原件字节；不新增图片理解轮次。"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session

from backend.app.ai.vision.base import VisionFailure, VisionImage
from backend.app.models.practice import PracticeItem
from backend.app.models.question_revision import QuestionRevision
from backend.app.schemas.content_blocks import RevisionContent
from backend.app.services.content_validation_service import (
    ContentValidationError,
    ContentValidationService,
)
from backend.app.services.file_storage_service import FileStorageError
from backend.app.services.grading.grading_context import (
    GradingContextError,
    SubjectiveGradingSource,
)
from backend.app.services.question_asset_access import PersonalContentAccess
from backend.app.services.subject_service import SubjectError


class GradingImageError(GradingContextError):
    """保留图片能力/文件来源错误；失败不是零分。"""

    def __init__(self, code: str, detail: str, *, retryable: bool = False) -> None:
        self.error_code = code  # type: ignore[misc]  # Actual upstream source code.
        self.retryable = retryable  # type: ignore[misc]  # Preserve source retry semantics.
        super().__init__(detail)


def prepare_personal_scoring_images(
    session: Session,
    item: PracticeItem,
    *,
    content: RevisionContent,
    profile: object,
) -> tuple[VisionImage, ...]:
    """读取本场冻结题目的受管题图原件；不新增图片理解轮次。

    只接受当前练习题目修订已绑定、来源可核验且学生可见的真实图片。
    图片缺失、归属变化或来源不明时显式失败，不回退为纯文本评分。
    """

    asset_ids = content.asset_ids(include_explanation=False)
    if not asset_ids:
        return ()
    revision = session.get(QuestionRevision, item.question_revision_id)
    if (
        revision is None
        or revision.id != item.question_revision_id
        or revision.question_id != item.question_id
        or revision.subject_id != item.subject_id
    ):
        raise GradingImageError(
            "CONTENT_NOT_FOUND", "本场主观题修订与练习题目不一致。"
        )
    access = PersonalContentAccess(session, profile)  # type: ignore[arg-type]
    images: list[VisionImage] = []
    try:
        for asset_id in sorted(asset_ids, key=str):
            asset = access._asset(revision, content, asset_id)
            source = access._asset_source(asset, revision)
            if source is None:
                raise GradingImageError(
                    "VISION_SOURCE_UNCONFIRMED", "题图缺少可核验的真实来源归属。"
                )
            if not access._answer_safe(asset):
                raise GradingImageError(
                    "VISION_IMAGE_STAGE_FORBIDDEN",
                    "题图不是当前学生可见的受管图片，拒绝用于评分。",
                )
            path, _metadata = access._image_path(asset)
            try:
                data = path.read_bytes()
            except OSError:
                raise GradingImageError(
                    "FILE_MISSING", "已登记题图不可读。", retryable=False
                ) from None
            images.append(VisionImage.from_bytes(data))
    except GradingImageError:
        raise
    except (SubjectError, FileStorageError) as error:
        raise GradingImageError(error.code, str(error)) from None
    except VisionFailure as error:
        raise GradingImageError(
            error.code, error.message, retryable=error.retryable
        ) from None
    if len(images) != len(asset_ids):
        raise GradingImageError(
            "VISION_IMAGE_MISSING", "本场题图数量与冻结内容不一致。"
        )
    return tuple(images)


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
