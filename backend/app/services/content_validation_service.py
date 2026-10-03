"""Durable report facts and authenticated whole-group image checks."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from pydantic import ValidationError
from pydantic_core import to_jsonable_python
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from backend.app.ai.llm.base import BaseLLMProvider
from backend.app.domain.enums import (
    DocumentPurpose,
    DocumentStatus,
    ExtractedQuestionStatus,
    PaperImportStatus,
    QuestionStatus,
)
from backend.app.models import (
    AgentRun,
    Document,
    DocumentChunk,
    ExtractedQuestion,
    PaperImport,
    Question,
    QuestionRevisionComment,
    QuestionSourceChunk,
    QuestionValidationResult,
    SourcePage,
)
from backend.app.schemas.content_validation import (
    ManualContextReference,
    ManualDisposition,
    ManualDispositionRequest,
    SemanticQuestionFields,
    SemanticValidationInput,
    SemanticValidationRequest,
    ValidationEvidence,
    ValidationInputRefs,
    ValidationIssue,
    ValidationOutput,
    ValidationProvenance,
    ValidationReportView,
)
from backend.app.schemas.file_storage import FileMetadata
from backend.app.schemas.image_assessment import (
    ConfirmedCondition,
    ImageAssessment,
    ImageAssessmentView,
    ImageInput,
    ImageInputRefs,
    ImageIssue,
    ImageManualCheck,
    ImageManualCheckRequest,
    ImageUnderstandingResult,
    ImageUnderstandingRun,
    Provenance,
    TechnicalError,
    applicable_imported_review,
    current_image_check,
    current_image_run,
    global_check_no,
    global_run_no,
)
from backend.app.schemas.paper_import import PixelRegion
from backend.app.schemas.question_assets import StoredStagedAsset
from backend.app.services.file_storage_service import (
    FileStorageError,
    FileStorageService,
)
from backend.app.services.question_asset_service import actual_image
from backend.app.services.question_scoring_invalidation import bump_question_validation

OwnerKind = Literal["question", "extracted_question"]
Owner = Question | ExtractedQuestion
FORMAL_FIELDS = (
    "type",
    "content",
    "options",
    "reference_answer",
    "scoring_rubric",
    "analysis",
    "score",
)
STAGED_FIELDS = (
    "question_type",
    "content",
    "options",
    "reference_answer",
    "scoring_rubric",
    "analysis",
    "score",
    "source_page_ids",
    "source_regions",
)


class ContentValidationError(FileStorageError):
    """I01 persistent content error."""


@dataclass(frozen=True)
class PreparedValidationRun:
    report: ValidationReportView
    input: SemanticValidationInput


@dataclass(frozen=True)
class PreparedImage:
    asset_id: UUID
    file_id: str
    image_index: int
    data: bytes = field(repr=False)
    mime_type: str
    width: int
    height: int
    source_page_id: UUID | None
    region: PixelRegion | None


@dataclass(frozen=True)
class PreparedImageRun:
    run: ImageUnderstandingRun
    context: dict[str, Any]
    images: tuple[PreparedImage, ...]


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class ContentValidationService:
    def __init__(
        self,
        session: Session,
        *,
        root: Path | None = None,
        provider_factory: Callable[[], BaseLLMProvider] | None = None,
    ):
        self.session = session
        self.files = FileStorageService(session, root=root)
        self._semantic_provider_factory = provider_factory

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        try:
            yield
        except (ValidationError, ValueError) as exc:
            self.session.rollback()
            raise ContentValidationError(
                "CONTENT_SOURCE_INVALID",
                "保存的输入或输出不符合持久证据约束。",
                http_status=422,
            ) from exc
        except SQLAlchemyError as exc:
            self.session.rollback()
            raise ContentValidationError(
                "CONTENT_VALIDATION_PERSISTENCE_FAILED",
                "核验保存失败，操作已回滚。",
                http_status=503,
            ) from exc
        except Exception:
            self.session.rollback()
            raise

    def _owner(
        self, kind: OwnerKind, identity: UUID, actor_id: UUID, *, writing: bool = False
    ) -> Owner:
        if kind not in {"question", "extracted_question"}:
            raise ContentValidationError(
                "CONTENT_OWNER_INVALID", "归属类型无效。", http_status=422
            )
        model = Question if kind == "question" else ExtractedQuestion
        record = cast(Owner | None, self.session.get(model, identity))
        if record is None:
            raise ContentValidationError(
                "CONTENT_NOT_FOUND", "题目不存在。", http_status=404
            )
        course_id = (
            record.course_id
            if isinstance(record, Question)
            else record.paper_import.course_id
        )
        actor = self.files._actor(actor_id)
        if not self.files._manages(actor, self.files._course(course_id)):
            raise ContentValidationError(
                "CONTENT_FORBIDDEN", "仅课程管理教师可核对内容。", http_status=403
            )
        if isinstance(record, ExtractedQuestion):
            paper = self.session.scalars(
                select(PaperImport)
                .where(PaperImport.id == record.paper_import_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).one()
            record = self.session.scalars(
                select(ExtractedQuestion)
                .where(ExtractedQuestion.id == identity)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).one()
            if writing and (
                paper.status != PaperImportStatus.PENDING_REVIEW
                or record.status != ExtractedQuestionStatus.PENDING_CORRECTION
            ):
                raise ContentValidationError(
                    "VISION_STATE_CONFLICT",
                    "仅待校正题接受新调用或核对。",
                    current_status=record.status.value,
                )
        else:
            record = self.session.scalars(
                select(Question)
                .options(
                    selectinload(Question.assets),
                    selectinload(Question.imported_extracted_question),
                )
                .where(Question.id == identity)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).one()
            if writing:
                from backend.app.services.question_asset_service import (
                    QuestionAssetService,
                )

                QuestionAssetService(self.session, root=self.files.root)._question(
                    identity, actor_id, writing=True
                )
        return record

    @staticmethod
    def _assessment(owner: Owner) -> ImageAssessment:
        try:
            return ImageAssessment.model_validate(owner.image_assessment or {})
        except ValidationError as exc:
            raise ContentValidationError(
                "IMAGE_ASSESSMENT_INVALID", "已保存图像证据无效。"
            ) from exc

    @staticmethod
    def _save_assessment(owner: Owner, assessment: ImageAssessment) -> None:
        owner.image_assessment = assessment.model_dump(mode="json")

    def _bump_validation(self, owner: Owner) -> None:
        if isinstance(owner, Question):
            bump_question_validation(self.session, owner)

    def _image_refs(self, owner: Owner) -> ImageInputRefs:
        fields = list(
            STAGED_FIELDS if isinstance(owner, ExtractedQuestion) else FORMAL_FIELDS
        )
        if (
            isinstance(owner, Question)
            and owner.imported_extracted_question is not None
        ):
            fields += ["source_page_ids", "source_regions"]
        fields.append("caption")
        images: list[ImageInput] = []
        if isinstance(owner, ExtractedQuestion):
            if owner.assets is None:
                raise ContentValidationError(
                    "VISION_STATE_CONFLICT", "请先核对整组题图关联。"
                )
            for index, raw in enumerate(owner.assets, 1):
                asset = StoredStagedAsset.model_validate(raw)
                if asset.id is None:
                    raise ContentValidationError(
                        "FILE_REFERENCE_CONFLICT", "题图身份未知。"
                    )
                page = self.session.get(SourcePage, asset.source_page_id)
                if (
                    page is None
                    or page.paper_import_id != owner.paper_import_id
                    or str(page.id) not in owner.source_page_ids
                ):
                    raise ContentValidationError(
                        "FILE_REFERENCE_CONFLICT", "题图原页须属于本次导入。"
                    )
                images.append(
                    ImageInput(
                        asset_id=asset.id,
                        file_id=asset.file_id,
                        image_index=index,
                        asset_type=asset.asset_type,
                        source_page_id=asset.source_page_id,
                        region=asset.region,
                    )
                )
        else:
            if [asset.order_index for asset in owner.assets] != list(
                range(1, len(owner.assets) + 1)
            ):
                raise ContentValidationError(
                    "VISION_STATE_CONFLICT", "题图顺序未知，请先校正。"
                )
            for index, formal_asset in enumerate(owner.assets, 1):
                if formal_asset.source_page_id is not None:
                    page = self.session.get(SourcePage, formal_asset.source_page_id)
                    if page is None or page.paper_import.course_id != owner.course_id:
                        raise ContentValidationError(
                            "FILE_REFERENCE_CONFLICT", "题图原页不属于本课程。"
                        )
                images.append(
                    ImageInput(
                        asset_id=formal_asset.id,
                        file_id=formal_asset.file_id,
                        image_index=index,
                        asset_type=cast(
                            Literal["figure", "table", "diagram"],
                            formal_asset.asset_type,
                        ),
                        source_page_id=formal_asset.source_page_id,
                        region=(
                            PixelRegion.model_validate(formal_asset.region)
                            if formal_asset.region
                            else None
                        ),
                    )
                )
        if not images:
            raise ContentValidationError(
                "VISION_STATE_CONFLICT", "无题图不能伪造空调用或核对。"
            )
        return ImageInputRefs(text_fields=fields, images=images)

    def _read_images(
        self, refs: ImageInputRefs, actor_id: UUID
    ) -> tuple[ImageInputRefs, tuple[PreparedImage, ...]]:
        facts: list[ImageInput] = []
        prepared: list[PreparedImage] = []
        for ref in refs.images:
            path, _view = self.files.download(ref.file_id, actor_id=actor_id)
            try:
                data = path.read_bytes()
            except FileNotFoundError as exc:
                raise ContentValidationError(
                    "FILE_MISSING", "题图缺失。", http_status=404
                ) from exc
            except OSError as exc:
                raise ContentValidationError(
                    "FILE_UNREADABLE", "题图不可读。", http_status=503
                ) from exc
            resource = self.files._resource(ref.file_id)
            if resource.file_metadata is not None:
                metadata = FileMetadata.model_validate(resource.file_metadata)
                if (
                    metadata.size_bytes != len(data)
                    or metadata.sha256 != hashlib.sha256(data).hexdigest()
                ):
                    raise ContentValidationError(
                        "FILE_CONTENT_CHANGED", "题图字节与登记内容不一致。"
                    )
            with actual_image(data) as image:
                width, height = image.size
                mime = "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"
            updated = ref.model_copy(
                update={"width": width, "height": height, "mime_type": mime}
            )
            facts.append(updated)
            prepared.append(
                PreparedImage(
                    ref.asset_id,
                    ref.file_id,
                    ref.image_index,
                    data,
                    mime,
                    width,
                    height,
                    ref.source_page_id,
                    ref.region,
                )
            )
        return ImageInputRefs(text_fields=refs.text_fields, images=facts), tuple(
            prepared
        )

    @staticmethod
    def _same_identity(left: ImageInputRefs, right: ImageInputRefs) -> bool:
        return left.text_fields == right.text_fields and [
            image.model_dump(exclude={"width", "height", "mime_type"})
            for image in left.images
        ] == [
            image.model_dump(exclude={"width", "height", "mime_type"})
            for image in right.images
        ]

    @staticmethod
    def _context(owner: Owner) -> dict[str, Any]:
        names = STAGED_FIELDS if isinstance(owner, ExtractedQuestion) else FORMAL_FIELDS
        context = {name: getattr(owner, name) for name in names}
        context["caption"] = (
            [asset.get("caption") for asset in owner.assets or []]
            if isinstance(owner, ExtractedQuestion)
            else [asset.caption for asset in owner.assets]
        )
        if (
            isinstance(owner, Question)
            and owner.imported_extracted_question is not None
        ):
            context["source_page_ids"] = (
                owner.imported_extracted_question.source_page_ids
            )
            context["source_regions"] = owner.imported_extracted_question.source_regions
        return cast(dict[str, Any], to_jsonable_python(context))

    def _region(self, ref: ImageInput, region: PixelRegion | None) -> None:
        if region is None:
            return
        if ref.source_page_id is not None:
            if ref.region is None:
                raise ContentValidationError(
                    "VISION_OUTPUT_INVALID",
                    "原页映射未知时不能猜测条件区域。",
                    http_status=422,
                )
            x0, y0, x1, y1 = ref.region.bbox
            a0, b0, a1, b1 = region.bbox
            if not (x0 <= a0 < a1 <= x1 and y0 <= b0 < b1 <= y1):
                raise ContentValidationError(
                    "VISION_OUTPUT_INVALID",
                    "条件区域超出真实题图范围。",
                    http_status=422,
                )
        else:
            if ref.width is None or ref.height is None:
                raise ContentValidationError(
                    "VISION_OUTPUT_INVALID", "题图尺寸未知。", http_status=422
                )
            region.within(ref.width, ref.height)

    def _validate_image_result(
        self, refs: ImageInputRefs, result: ImageUnderstandingResult
    ) -> None:
        by_id = {image.asset_id: image for image in refs.images}
        for observation in result.observations:
            if observation.image_index > len(refs.images):
                raise ContentValidationError(
                    "VISION_OUTPUT_INVALID", "观察索引不属于本轮图像。", http_status=422
                )
            if observation.bbox is not None:
                ref = refs.images[observation.image_index - 1]
                self._region(ref, PixelRegion(bbox=observation.bbox))
        if len({condition.condition_id for condition in result.conditions}) != len(
            result.conditions
        ) or len({issue.issue_id for issue in result.unresolved_issues}) != len(
            result.unresolved_issues
        ):
            raise ContentValidationError(
                "VISION_OUTPUT_INVALID", "条件或问题身份重复。", http_status=422
            )
        for condition in result.conditions:
            if (
                condition.asset_id not in by_id
                or by_id[condition.asset_id].image_index != condition.image_index
            ):
                raise ContentValidationError(
                    "VISION_OUTPUT_INVALID", "条件不对应本轮题图。", http_status=422
                )
            self._region(by_id[condition.asset_id], condition.evidence_region)
        for issue in result.unresolved_issues:
            if issue.asset_ids is not None and (
                not issue.asset_ids
                or len(set(issue.asset_ids)) != len(issue.asset_ids)
                or not set(issue.asset_ids) <= by_id.keys()
            ):
                raise ContentValidationError(
                    "VISION_OUTPUT_INVALID", "问题对应图像非法。", http_status=422
                )
        if result.unresolved_issues and not result.requires_manual_review:
            raise ContentValidationError(
                "VISION_OUTPUT_INVALID", "未解决问题须保留待人工核对。", http_status=422
            )

    def start_image_run(
        self,
        owner_kind: OwnerKind,
        owner_id: UUID,
        *,
        task: str,
        actor_id: UUID,
        executor_name: str = "vision",
        executor_kind: Literal["service", "agent"] = "service",
    ) -> ImageUnderstandingRun:
        with self._transaction():
            owner = self._owner(owner_kind, owner_id, actor_id, writing=True)
            assessment = self._assessment(owner)
            previous = current_image_run(assessment)
            if previous is not None and previous.outcome == "running":
                raise ContentValidationError(
                    "VISION_STATE_CONFLICT", "当前上下文已有运行中图片调用。"
                )
            refs = self._image_refs(owner)
            run = ImageUnderstandingRun(
                id=uuid4(),
                run_no=global_run_no(assessment) + 1,
                context_revision=assessment.context_revision,
                task=task,
                input_refs=refs,
                outcome="running",
                result=None,
                error=None,
                executor_kind=executor_kind,
                executor_name=executor_name,
                requested_by=actor_id,
                agent_run_id=None,
                provenance=Provenance(
                    provider_name=None,
                    model=None,
                    model_version=None,
                    prompt_version=None,
                ),
                started_at=datetime.now(UTC),
                completed_at=None,
            )
            assessment.runs.append(run)
            self._save_assessment(owner, assessment)
            self._bump_validation(owner)
            self.session.commit()
            return run

    def prepare_image_run(
        self, owner_kind: OwnerKind, owner_id: UUID, run_id: UUID, *, actor_id: UUID
    ) -> PreparedImageRun:
        with self._transaction():
            owner = self._owner(owner_kind, owner_id, actor_id)
            assessment = self._assessment(owner)
            run = next((run for run in assessment.runs if run.id == run_id), None)
            if run is None or run.outcome != "running":
                raise ContentValidationError(
                    "VISION_STATE_CONFLICT", "图片轮次不存在或已结束。"
                )
            if (
                run.context_revision != assessment.context_revision
                or run.run_no != global_run_no(assessment)
                or not self._same_identity(run.input_refs, self._image_refs(owner))
            ):
                raise ContentValidationError(
                    "IMAGE_ASSESSMENT_STALE", "本轮原输入已变化。"
                )
            refs, images = self._read_images(run.input_refs, actor_id)
            run.input_refs = refs
            context = self._context(owner)
            self._save_assessment(owner, assessment)
            self.session.commit()
            return PreparedImageRun(run=run, context=context, images=images)

    def finish_image_run(
        self,
        owner_kind: OwnerKind,
        owner_id: UUID,
        run_id: UUID,
        *,
        actor_id: UUID,
        result: ImageUnderstandingResult | None = None,
        error: TechnicalError | None = None,
        provenance: Provenance | None = None,
        agent_run_id: UUID | None = None,
    ) -> ImageAssessmentView:
        with self._transaction():
            owner = self._owner(owner_kind, owner_id, actor_id)
            assessment = self._assessment(owner)
            run = next((run for run in assessment.runs if run.id == run_id), None)
            if run is None or run.outcome != "running":
                raise ContentValidationError(
                    "VISION_STATE_CONFLICT", "已结束轮次不能改写。"
                )
            if (result is None) == (error is None):
                raise ContentValidationError(
                    "VISION_OUTPUT_INVALID",
                    "须保存合法输出或真实错误之一。",
                    http_status=422,
                )
            if result is not None:
                self._validate_image_result(run.input_refs, result)
            if (
                agent_run_id is not None
                and self.session.get(AgentRun, agent_run_id) is None
            ):
                raise ContentValidationError(
                    "CONTENT_SOURCE_INVALID", "真实Trace不存在。", http_status=422
                )
            run.result, run.error = result, error
            run.outcome = "completed" if result is not None else "technical_error"
            run.completed_at = datetime.now(UTC)
            run.provenance = provenance or run.provenance
            run.agent_run_id = agent_run_id
            run = ImageUnderstandingRun.model_validate(run.model_dump())
            assessment.runs = [
                run if old.id == run.id else old for old in assessment.runs
            ]
            self._save_assessment(owner, assessment)
            self.session.flush()
            view = self.get_image_assessment(owner_kind, owner_id, actor_id=actor_id)
            self.session.commit()
            return view

    def _open_issues(self, assessment: ImageAssessment) -> dict[UUID, ImageIssue]:
        issues: dict[UUID, ImageIssue] = {}
        for run in assessment.runs:
            if (
                run.context_revision == assessment.context_revision
                and run.result is not None
            ):
                issues.update(
                    (issue.issue_id, issue) for issue in run.result.unresolved_issues
                )
        for check in assessment.manual_checks:
            if check.context_revision != assessment.context_revision:
                continue
            issues.update((issue.issue_id, issue) for issue in check.issues)
            for resolution in check.issue_resolutions:
                if resolution.resolution == "resolved":
                    issues.pop(resolution.issue_id, None)
        return issues

    def manual_image_check(
        self,
        owner_kind: OwnerKind,
        owner_id: UUID,
        payload: ImageManualCheckRequest,
        *,
        actor_id: UUID,
    ) -> ImageAssessmentView:
        with self._transaction():
            owner = self._owner(owner_kind, owner_id, actor_id, writing=True)
            assessment = self._assessment(owner)
            if (
                payload.expected_context_revision,
                payload.expected_run_no,
                payload.expected_check_no,
            ) != (
                assessment.context_revision,
                global_run_no(assessment),
                global_check_no(assessment),
            ):
                raise ContentValidationError(
                    "IMAGE_ASSESSMENT_STALE", "核对计数已变化，请重新读取。"
                )
            run = current_image_run(assessment)
            if run is not None and run.outcome == "running":
                raise ContentValidationError(
                    "VISION_STATE_CONFLICT", "当前调用运行中，不能确认未知输出。"
                )
            refs, _images = self._read_images(self._image_refs(owner), actor_id)
            ids = {image.asset_id for image in refs.images}
            machine_conditions = {
                condition.condition_id: condition
                for old in assessment.runs
                if old.context_revision == assessment.context_revision
                and old.result is not None
                for condition in old.result.conditions
            }
            conditions: list[ConfirmedCondition] = []
            for value in payload.confirmed_conditions:
                if value.asset_id not in ids:
                    raise ContentValidationError(
                        "FILE_REFERENCE_CONFLICT", "条件不属于当前题图。"
                    )
                self._region(
                    next(ref for ref in refs.images if ref.asset_id == value.asset_id),
                    value.evidence_region,
                )
                if value.source_condition_id is not None:
                    source = machine_conditions.get(value.source_condition_id)
                    if source is None or source.asset_id != value.asset_id:
                        raise ContentValidationError(
                            "CONTENT_SOURCE_INVALID",
                            "机器条件引用不是当前上下文真实条件。",
                            http_status=422,
                        )
                conditions.append(
                    ConfirmedCondition(condition_id=uuid4(), **value.model_dump())
                )
            open_issues = self._open_issues(assessment)
            resolution_ids = [
                resolution.issue_id for resolution in payload.issue_resolutions
            ]
            if (
                len(set(resolution_ids)) != len(resolution_ids)
                or set(resolution_ids) != open_issues.keys()
            ):
                raise ContentValidationError(
                    "CONTENT_SOURCE_INVALID",
                    "须逐项处理已登记未解决问题。",
                    http_status=422,
                )
            issues: list[ImageIssue] = []
            for issue_value in payload.issues:
                if issue_value.asset_ids is not None and (
                    not issue_value.asset_ids
                    or len(set(issue_value.asset_ids)) != len(issue_value.asset_ids)
                    or not set(issue_value.asset_ids) <= ids
                ):
                    raise ContentValidationError(
                        "FILE_REFERENCE_CONFLICT", "问题对应非当前题图。"
                    )
                issues.append(ImageIssue(issue_id=uuid4(), **issue_value.model_dump()))
            event = ImageManualCheck(
                id=uuid4(),
                check_no=global_check_no(assessment) + 1,
                context_revision=assessment.context_revision,
                run_no=global_run_no(assessment),
                run_id=run.id if run is not None else None,
                input_refs=refs,
                status=payload.status,
                confirmed_conditions=conditions,
                image_findings=payload.image_findings,
                issues=issues,
                issue_resolutions=payload.issue_resolutions,
                teacher_id=actor_id,
                checked_at=datetime.now(UTC),
                explanation=payload.explanation,
            )
            assessment.manual_checks.append(event)
            self._save_assessment(owner, assessment)
            self._bump_validation(owner)
            self.session.flush()
            view = self.get_image_assessment(owner_kind, owner_id, actor_id=actor_id)
            self.session.commit()
            return view

    def _bound_check(
        self, owner: Question, assessment: ImageAssessment, refs: ImageInputRefs
    ) -> ImageManualCheck | None:
        binding = applicable_imported_review(assessment)
        source = owner.imported_extracted_question
        if (
            binding is None
            or source is None
            or source.id != binding.source_ref.owner_id
            or binding.input_refs.images != refs.images
            or [
                ("type" if name == "question_type" else name)
                for name in binding.input_refs.text_fields
            ]
            != refs.text_fields
        ):
            return None
        original = self._assessment(source)
        check = current_image_check(original)
        if (
            check is None
            or check.id != binding.source_ref.check_id
            or check.status != "confirmed"
        ):
            return None
        source_refs = refs.model_copy(
            update={"text_fields": [*STAGED_FIELDS, "caption"]}
        )
        return (
            check
            if check.input_refs == source_refs
            and binding.input_refs.images == refs.images
            else None
        )

    def get_image_assessment(
        self, owner_kind: OwnerKind, owner_id: UUID, *, actor_id: UUID
    ) -> ImageAssessmentView:
        with self._transaction():
            return self._image_assessment_view(owner_kind, owner_id, actor_id=actor_id)

    def _image_assessment_view(
        self, owner_kind: OwnerKind, owner_id: UUID, *, actor_id: UUID
    ) -> ImageAssessmentView:
        owner = self._owner(owner_kind, owner_id, actor_id)
        assessment = self._assessment(owner)
        no_images = owner.assets == [] or (
            isinstance(owner, Question) and not owner.assets
        )
        refs = None
        read_error = None
        readable = no_images
        try:
            if not no_images:
                refs, _ = self._read_images(self._image_refs(owner), actor_id)
                readable = True
        except (FileStorageError, ValidationError, ValueError) as exc:
            read_error = TechnicalError(
                code=getattr(exc, "code", "IMAGE_ASSESSMENT_INVALID"),
                message=str(exc),
                stage="image_read",
                retryable=False,
                cause=type(exc).__name__,
            )
        check = current_image_check(assessment)
        if check is not None and check.input_refs != refs:
            check = None
        binding = None
        if isinstance(owner, Question) and refs is not None and check is None:
            bound = self._bound_check(owner, assessment, refs)
            if bound is not None:
                check, binding = bound, assessment.imported_review
        status: Literal["pending", "confirmed", "unresolved", "not_required"] = (
            "not_required"
            if no_images
            else check.status if check is not None and readable else "pending"
        )
        return ImageAssessmentView(
            owner_kind=owner_kind,
            owner_id=owner_id,
            assessment=assessment if owner.image_assessment is not None else None,
            context_revision=assessment.context_revision,
            run_no=global_run_no(assessment),
            check_no=global_check_no(assessment),
            input_refs=refs,
            current_run=current_image_run(assessment),
            current_check=check,
            imported_review=binding,
            status=status,
            confirmed_conditions=(
                check.confirmed_conditions
                if check is not None and status == "confirmed"
                else []
            ),
            requires_manual_review=status not in {"confirmed", "not_required"},
            evidence_readable=readable,
            error=read_error,
            open_issues=list(self._open_issues(assessment).values()),
        )

    def _chunk_data(self, question: Question, chunk_id: UUID) -> dict[str, Any]:
        chunk = self.session.scalars(
            select(DocumentChunk)
            .where(DocumentChunk.id == chunk_id)
            .options(
                selectinload(DocumentChunk.document).selectinload(
                    Document.knowledge_base
                )
            )
            .execution_options(populate_existing=True)
        ).one_or_none()
        if (
            chunk is None
            or chunk.course_id != question.course_id
            or chunk.document.course_id != question.course_id
            or chunk.document.purpose != DocumentPurpose.KNOWLEDGE_BASE
            or chunk.document.status != DocumentStatus.READY
            or chunk.document.knowledge_base_id != chunk.knowledge_base_id
            or chunk.document.knowledge_base is None
            or chunk.document.knowledge_base.course_id != question.course_id
        ):
            raise ContentValidationError(
                "CONTENT_SOURCE_INVALID",
                "Teaching chunks must belong to a Ready document and a valid knowledge base in this course.",
                http_status=422,
            )
        return {
            "chunk_id": str(chunk.id),
            "document_id": str(chunk.document_id),
            "course_id": str(chunk.course_id),
            "source_file": chunk.document.original_filename,
            "location": deepcopy((chunk.chunk_metadata or {}).get("location")),
            "content_snapshot": chunk.content,
        }

    def _evidence(
        self, question: Question, evidence: list[ValidationEvidence], actor_id: UUID
    ) -> None:
        for item in evidence:
            data = item.source_data
            if item.kind == "chunk":
                expected = self._chunk_data(question, item.source_id)
            elif item.kind == "question_source_chunk":
                source = self.session.get(QuestionSourceChunk, item.source_id)
                if (
                    source is None
                    or source.question_id != question.id
                    or source.course_id != question.course_id
                ):
                    raise ContentValidationError(
                        "CONTENT_SOURCE_INVALID",
                        "来源快照不属于本题。",
                        http_status=422,
                    )
                expected = {
                    "chunk_id": str(source.chunk_id),
                    "document_id": str(source.document_id),
                    "course_id": str(source.course_id),
                    "source_file": source.source_file,
                    "location": None,
                    "content_snapshot": source.content_snapshot,
                }
            else:
                asset = next(
                    (asset for asset in question.assets if asset.id == item.source_id),
                    None,
                )
                if asset is None:
                    raise ContentValidationError(
                        "CONTENT_SOURCE_INVALID",
                        "图片证据不属于本题。",
                        http_status=422,
                    )
                view = self._image_assessment_view(
                    "question", question.id, actor_id=actor_id
                )
                if view.status != "confirmed" or view.current_check is None:
                    raise ContentValidationError(
                        "VISION_REVIEW_REQUIRED", "图片条件尚未当前可靠核对。"
                    )
                check = view.current_check
                expected_ref = {
                    "owner_kind": (
                        "extracted_question" if view.imported_review else "question"
                    ),
                    "owner_id": str(
                        view.imported_review.source_ref.owner_id
                        if view.imported_review
                        else question.id
                    ),
                    "check_id": str(check.id),
                    "binding_id": (
                        str(view.imported_review.id) if view.imported_review else None
                    ),
                }
                expected = {
                    "file_id": asset.file_id,
                    "source_page_id": (
                        str(asset.source_page_id) if asset.source_page_id else None
                    ),
                    "region": asset.region,
                    "image_review_ref": expected_ref,
                    "confirmed_conditions": [
                        condition.model_dump(mode="json")
                        for condition in view.confirmed_conditions
                        if condition.asset_id == asset.id
                    ],
                }
            if data != expected:
                raise ContentValidationError(
                    "CONTENT_SOURCE_INVALID",
                    "证据必须来自本轮真实持久输入。",
                    http_status=422,
                )

    def _manual_context(self, question: Question, refs: ValidationInputRefs) -> None:
        """Explicit authentic historical context is data, never inherited current qualification."""
        for item in refs.manual_context:
            report = self.session.get(
                QuestionValidationResult, item.validation_result_id
            )
            if (
                report is None
                or report.question_id != question.id
                or not any(
                    value["id"] == str(item.disposition_id)
                    for value in report.manual_dispositions
                )
            ):
                raise ContentValidationError(
                    "CONTENT_SOURCE_INVALID",
                    "人工上下文不是当前修订实际处置。",
                    http_status=422,
                )

    def _report(self, question_id: UUID, report_id: UUID) -> QuestionValidationResult:
        report = self.session.scalars(
            select(QuestionValidationResult)
            .where(
                QuestionValidationResult.id == report_id,
                QuestionValidationResult.question_id == question_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
        if report is None:
            raise ContentValidationError(
                "CONTENT_NOT_FOUND", "核验报告不属于本题或不存在。", http_status=404
            )
        return report

    def _report_view(
        self, question: Question, report: QuestionValidationResult, actor_id: UUID
    ) -> ValidationReportView:
        latest = self.session.scalar(
            select(func.max(QuestionValidationResult.run_no)).where(
                QuestionValidationResult.question_id == question.id
            )
        )
        is_current = (
            report.run_no == latest
            and report.input_revision == question.validation_revision
        )
        dispositions = [
            ManualDisposition.model_validate(value)
            for value in report.manual_dispositions
        ]
        pending = any(
            item.action in {"provide_evidence", "resolve_issue"}
            for item in dispositions
        )
        refs = ValidationInputRefs.model_validate(report.input_refs)
        ready = False
        if (
            is_current
            and report.outcome == "passed"
            and not pending
            and question.status == QuestionStatus.PENDING_REVIEW
        ):
            try:
                self._validation_ready(question, refs, actor_id)
                self._evidence(question, refs.evidence, actor_id)
                ready = True
            except ContentValidationError:
                ready = False
        can_review = ready
        return ValidationReportView.model_validate(
            {
                "id": report.id,
                "question_id": report.question_id,
                "input_revision": report.input_revision,
                "run_no": report.run_no,
                "outcome": report.outcome,
                "input_refs": refs,
                "checks": report.checks,
                "issues": report.issues,
                "error": report.error,
                "executor_kind": report.executor_kind,
                "executor_name": report.executor_name,
                "requested_by": report.requested_by,
                "agent_run_id": report.agent_run_id,
                "provenance": report.provenance,
                "manual_dispositions": dispositions,
                "created_at": _utc(report.created_at),
                "completed_at": (
                    _utc(report.completed_at) if report.completed_at else None
                ),
                "is_current": is_current,
                "stale": not is_current,
                "can_review": can_review,
                "requires_manual_review": not can_review,
            }
        )

    def _validation_ready(
        self, question: Question, refs: ValidationInputRefs, actor_id: UUID
    ) -> None:
        required = {
            "type",
            "content",
            "options",
            "reference_answer",
            "scoring_rubric",
            "analysis",
            "score",
        }
        complete = bool(
            required <= set(refs.fields)
            and question.content.strip()
            and question.reference_answer
            and question.reference_answer.strip()
            and question.scoring_rubric
            and question.scoring_rubric.strip()
            and question.score > 0
            and any(
                item.kind in {"chunk", "question_source_chunk"}
                for item in refs.evidence
            )
        )
        if question.type.value == "SINGLE_CHOICE":
            values = (
                question.options.values()
                if isinstance(question.options, dict)
                else question.options or []
            )
            complete = (
                complete
                and len(values) >= 2
                and all(isinstance(value, str) and value.strip() for value in values)
            )
        if not complete:
            raise ContentValidationError(
                "CONTENT_INPUT_INCOMPLETE",
                "请补齐真实题目、答案、评分标准、分值、输入字段与教学依据后核验。",
                http_status=422,
            )
        from backend.app.services.question_validator import validate_semantic_fields

        fields = SemanticQuestionFields.model_validate(
            {name: deepcopy(getattr(question, name)) for name in FORMAL_FIELDS}
        )
        if validate_semantic_fields(fields):
            raise ContentValidationError(
                "CONTENT_INPUT_INCOMPLETE",
                "Current answer encoding or options are not usable.",
                http_status=422,
            )
        if question.assets:
            view = self._image_assessment_view("question", question.id, actor_id=actor_id)
            if view.status != "confirmed" or not {
                asset.id for asset in question.assets
            } <= {
                item.source_id
                for item in refs.evidence
                if item.kind == "question_asset"
            }:
                raise ContentValidationError(
                    "VISION_REVIEW_REQUIRED", "每张题图须有当前可靠核对与真实核验引用。"
                )

    def require_exam_eligible(
        self, question: Question, actor_id: UUID
    ) -> QuestionValidationResult:
        """Check current Approved content without owning the caller's transaction.

        Assembly holds Course/Exam/Question locks. Unlike the approval command this
        accepts only already Approved questions, and never commits or rolls back.
        """
        locked = cast(Question, self._owner("question", question.id, actor_id))
        if locked.status != QuestionStatus.APPROVED:
            raise ContentValidationError(
                "CONTENT_VALIDATION_NOT_READY",
                "组卷题目必须已审核。",
                current_status=locked.status.value,
            )
        report = self.session.scalars(
            select(QuestionValidationResult)
            .where(QuestionValidationResult.question_id == locked.id)
            .order_by(QuestionValidationResult.run_no.desc())
            .limit(1)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        if report is None:
            raise ContentValidationError(
                "CONTENT_VALIDATION_REQUIRED", "组卷题目缺少当前语义核验报告。"
            )
        try:
            if (
                report.input_revision != locked.validation_revision
                or report.outcome != "passed"
            ):
                raise ContentValidationError(
                    "CONTENT_VALIDATION_NOT_READY", "最新语义核验未通过或已过期。"
                )
            output = ValidationOutput.model_validate(
                {"checks": report.checks, "issues": report.issues}
            )
            dispositions = [
                ManualDisposition.model_validate(item)
                for item in report.manual_dispositions
            ]
            if (
                any(check.verdict != "pass" for check in output.checks)
                or any(
                    issue.severity in {"warning", "error"} for issue in output.issues
                )
                or any(
                    item.action
                    in {"provide_evidence", "resolve_issue", "request_revision"}
                    for item in dispositions
                )
            ):
                raise ContentValidationError(
                    "CONTENT_VALIDATION_NOT_READY",
                    "语义核验仍有未解决问题或人工处置待复核。",
                )
            refs = ValidationInputRefs.model_validate(report.input_refs)
            known_evidence = {item.evidence_id for item in refs.evidence}
            if any(
                not set(check.evidence_refs) <= known_evidence
                for check in output.checks
            ) or any(
                not set(issue.evidence_refs) <= known_evidence
                for issue in output.issues
            ):
                raise ValueError("核验输出引用不存在的证据。")
            self._validation_ready(locked, refs, actor_id)
            self._evidence(locked, refs.evidence, actor_id)
        except (ValidationError, ValueError) as exc:
            raise ContentValidationError(
                "CONTENT_SOURCE_INVALID",
                "已保存语义核验或来源证据无效。",
                http_status=422,
            ) from exc
        return report

    def require_can_approve(
        self, question: Question, actor_id: UUID
    ) -> ValidationReportView:
        """Shared approval gate; caller keeps the current Question lock and owns commit."""
        locked = cast(Question, self._owner("question", question.id, actor_id))
        report = self.session.scalars(
            select(QuestionValidationResult)
            .where(QuestionValidationResult.question_id == locked.id)
            .order_by(QuestionValidationResult.run_no.desc())
            .limit(1)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        if report is None:
            raise ContentValidationError(
                "CONTENT_VALIDATION_REQUIRED",
                "A current semantic validation report is required before approval.",
            )
        view = self._report_view(locked, report, actor_id)
        if not view.can_review:
            raise ContentValidationError(
                "CONTENT_VALIDATION_NOT_READY",
                "The latest semantic report and current content do not permit approval.",
                current_status=locked.status.value,
            )
        return view

    def _production_refs(
        self, question: Question, teaching_chunk_ids: list[UUID] | None = None
    ) -> ValidationInputRefs:
        """Explicit live chunks or existing generation snapshots; paper lineage is not teaching evidence."""
        if teaching_chunk_ids is not None:
            return ValidationInputRefs(
                fields=list(FORMAL_FIELDS),
                evidence=[
                    ValidationEvidence(
                        evidence_id=uuid4(),
                        kind="chunk",
                        source_id=identity,
                        source_data=self._chunk_data(question, identity),
                    )
                    for identity in teaching_chunk_ids
                ],
            )
        evidence = [
            ValidationEvidence(
                evidence_id=uuid4(),
                kind="question_source_chunk",
                source_id=source.id,
                source_data={
                    "chunk_id": str(source.chunk_id),
                    "document_id": str(source.document_id),
                    "course_id": str(source.course_id),
                    "source_file": source.source_file,
                    "location": None,
                    "content_snapshot": source.content_snapshot,
                },
            )
            for source in self.session.scalars(
                select(QuestionSourceChunk)
                .where(QuestionSourceChunk.question_id == question.id)
                .order_by(QuestionSourceChunk.source_order)
            ).all()
        ]
        return ValidationInputRefs(fields=list(FORMAL_FIELDS), evidence=evidence)

    @staticmethod
    def _teaching_basis(refs: ValidationInputRefs) -> list[dict[str, Any]]:
        """Compare material source identity and snapshot; fresh report evidence IDs are not input changes."""
        return [
            {
                "kind": item.kind,
                "source_id": str(item.source_id),
                "source_data": item.source_data,
            }
            for item in sorted(
                refs.evidence, key=lambda item: (item.kind, str(item.source_id))
            )
            if item.kind in {"chunk", "question_source_chunk"}
        ]

    async def validate_current(
        self,
        question_id: UUID,
        *,
        actor_id: UUID,
        teaching_chunk_ids: list[UUID] | None = None,
        manual_context: list[ManualContextReference] | None = None,
        provider: BaseLLMProvider | None = None,
        provider_factory: Callable[[], BaseLLMProvider] | None = None,
    ) -> ValidationReportView:
        """Explicit teacher command; all fields, evidence snapshots, identities and current gates are server facts."""
        return await self.run_validation(
            question_id,
            actor_id=actor_id,
            teaching_chunk_ids=teaching_chunk_ids,
            manual_context=manual_context or [],
            provider=provider,
            provider_factory=provider_factory,
        )

    def prepare_validation(
        self,
        question_id: UUID,
        *,
        actor_id: UUID,
        input_refs: ValidationInputRefs | None = None,
        teaching_chunk_ids: list[UUID] | None = None,
        manual_context: list[ManualContextReference] | None = None,
        executor_name: str = "question_semantic_validator",
    ) -> PreparedValidationRun:
        """Project fields and actual evidence under the same lock that allocates the round."""
        with self._transaction():
            question = cast(
                Question, self._owner("question", question_id, actor_id, writing=True)
            )
            if input_refs is not None and (
                teaching_chunk_ids is not None or manual_context is not None
            ):
                raise ContentValidationError(
                    "CONTENT_SOURCE_INVALID",
                    "Do not combine internal snapshots and public evidence selections.",
                    http_status=422,
                )
            selection = SemanticValidationRequest(
                teaching_chunk_ids=teaching_chunk_ids,
                manual_context=manual_context or [],
            )
            refs = (
                input_refs.model_copy(deep=True)
                if input_refs is not None
                else self._production_refs(question, selection.teaching_chunk_ids)
            )
            if manual_context is not None:
                refs.manual_context = selection.manual_context
            if question.assets:
                view = self.get_image_assessment(
                    "question", question.id, actor_id=actor_id
                )
                if view.status != "confirmed" or view.current_check is None:
                    raise ContentValidationError(
                        "VISION_REVIEW_REQUIRED",
                        "Current reliable image checks are required before semantic validation.",
                    )
                for asset in question.assets:
                    if any(
                        item.kind == "question_asset" and item.source_id == asset.id
                        for item in refs.evidence
                    ):
                        continue
                    refs.evidence.append(
                        ValidationEvidence(
                            evidence_id=uuid4(),
                            kind="question_asset",
                            source_id=asset.id,
                            source_data={
                                "file_id": asset.file_id,
                                "source_page_id": (
                                    str(asset.source_page_id)
                                    if asset.source_page_id
                                    else None
                                ),
                                "region": asset.region,
                                "image_review_ref": {
                                    "owner_kind": (
                                        "extracted_question"
                                        if view.imported_review
                                        else "question"
                                    ),
                                    "owner_id": str(
                                        view.imported_review.source_ref.owner_id
                                        if view.imported_review
                                        else question.id
                                    ),
                                    "check_id": str(view.current_check.id),
                                    "binding_id": (
                                        str(view.imported_review.id)
                                        if view.imported_review
                                        else None
                                    ),
                                },
                                "confirmed_conditions": [
                                    condition.model_dump(mode="json")
                                    for condition in view.confirmed_conditions
                                    if condition.asset_id == asset.id
                                ],
                            },
                        )
                    )
                refs.fields = list(
                    dict.fromkeys([*refs.fields, "images", "image_conditions"])
                )
            self._evidence(question, refs.evidence, actor_id)
            self._manual_context(question, refs)
            self._validation_ready(question, refs, actor_id)
            latest = self.session.scalars(
                select(QuestionValidationResult)
                .where(QuestionValidationResult.question_id == question.id)
                .order_by(QuestionValidationResult.run_no.desc())
                .limit(1)
            ).first()
            if latest is not None and self._teaching_basis(
                ValidationInputRefs.model_validate(latest.input_refs)
            ) != self._teaching_basis(refs):
                bump_question_validation(self.session, question)
                # Production sessions disable autoflush; startup reload must see this same transaction's revision.
                self.session.flush()
            fields = SemanticQuestionFields.model_validate(
                {name: deepcopy(getattr(question, name)) for name in FORMAL_FIELDS}
            )
            manual: list[dict[str, Any]] = []
            for manual_selection in refs.manual_context:
                previous = self.session.get(
                    QuestionValidationResult, manual_selection.validation_result_id
                )
                if previous is not None:
                    manual.extend(
                        deepcopy(value)
                        for value in previous.manual_dispositions
                        if value["id"] == str(manual_selection.disposition_id)
                    )
            report = self.start_validation(
                question_id,
                actor_id=actor_id,
                input_refs=refs,
                executor_name=executor_name,
                executor_kind="agent",
            )
            return PreparedValidationRun(
                report=report,
                input=SemanticValidationInput(
                    question_id=question_id,
                    input_revision=report.input_revision,
                    run_no=report.run_no,
                    fields=fields,
                    evidence=refs.evidence,
                    manual_context=manual,
                ),
            )

    async def run_validation(
        self,
        question_id: UUID,
        *,
        actor_id: UUID,
        input_refs: ValidationInputRefs | None = None,
        teaching_chunk_ids: list[UUID] | None = None,
        manual_context: list[ManualContextReference] | None = None,
        provider: BaseLLMProvider | None = None,
        provider_factory: Callable[[], BaseLLMProvider] | None = None,
    ) -> ValidationReportView:
        """Same one-shot command for explicit review and automatically persisted text candidates."""
        from backend.app.ai.llm.factory import create_llm_provider
        from backend.app.ai.semantic_validation import (
            ProviderSemanticValidator,
            SemanticExecutionFailure,
        )
        from backend.app.core.config import get_settings

        prepared = self.prepare_validation(
            question_id,
            actor_id=actor_id,
            input_refs=input_refs,
            teaching_chunk_ids=teaching_chunk_ids,
            manual_context=manual_context,
        )
        owned = provider is None
        executor = None
        try:
            if provider is None:
                try:
                    provider = (
                        provider_factory
                        or self._semantic_provider_factory
                        or (lambda: create_llm_provider(get_settings()))
                    )()
                except Exception as error:  # noqa: BLE001 -- Provider initialization.
                    return self.finish_validation(
                        prepared.report.id,
                        actor_id=actor_id,
                        error=TechnicalError(
                            code="CONTENT_PROVIDER_NOT_READY",
                            message="The configured semantic provider could not be initialized.",
                            stage="configuration",
                            cause=type(error).__name__,
                            retryable=False,
                        ),
                    )
            executor = ProviderSemanticValidator(provider)
            machine = await executor.validate(prepared.input)
            output = ValidationOutput(
                checks=machine.checks,
                issues=[
                    ValidationIssue(issue_id=uuid4(), **item.model_dump())
                    for item in machine.issues
                ],
            )
            return self.finish_validation(
                prepared.report.id,
                actor_id=actor_id,
                output=output,
                provenance=executor.call_provenance,
            )
        except SemanticExecutionFailure as error:
            return self.finish_validation(
                prepared.report.id,
                actor_id=actor_id,
                error=error.error,
                provenance=error.provenance,
            )
        except asyncio.CancelledError:
            self.finish_validation(
                prepared.report.id,
                actor_id=actor_id,
                error=TechnicalError(
                    code="CONTENT_CANCELLED",
                    message="The semantic validation call was cancelled.",
                    stage="call",
                    retryable=False,
                ),
                provenance=executor.call_provenance if executor else None,
            )
            raise
        finally:
            if owned and provider is not None:
                await provider.aclose()

    def start_validation(
        self,
        question_id: UUID,
        *,
        actor_id: UUID,
        input_refs: ValidationInputRefs,
        executor_name: str,
        executor_kind: Literal["service", "agent"] = "service",
        agent_run_id: UUID | None = None,
    ) -> ValidationReportView:
        with self._transaction():
            question = cast(
                Question, self._owner("question", question_id, actor_id, writing=True)
            )
            if question.status != QuestionStatus.PENDING_REVIEW:
                raise ContentValidationError(
                    "CONTENT_STATE_CONFLICT",
                    "正式核验仅对待审核题启动。",
                    current_status=question.status.value,
                )
            if not set(input_refs.fields) <= set(
                FORMAL_FIELDS + ("images", "image_conditions", "teaching_evidence")
            ):
                raise ContentValidationError(
                    "CONTENT_SOURCE_INVALID",
                    "核验字段目录不属于实际输入。",
                    http_status=422,
                )
            self._evidence(question, input_refs.evidence, actor_id)
            self._manual_context(question, input_refs)
            self._validation_ready(question, input_refs, actor_id)
            if (
                agent_run_id is not None
                and self.session.get(AgentRun, agent_run_id) is None
            ):
                raise ContentValidationError(
                    "CONTENT_SOURCE_INVALID", "执行Trace不存在。", http_status=422
                )
            number = (
                self.session.scalar(
                    select(func.max(QuestionValidationResult.run_no)).where(
                        QuestionValidationResult.question_id == question.id
                    )
                )
                or 0
            )
            report = QuestionValidationResult(
                id=uuid4(),
                question_id=question.id,
                input_revision=question.validation_revision,
                run_no=number + 1,
                outcome="running",
                input_refs=input_refs.model_dump(mode="json"),
                executor_kind=executor_kind,
                executor_name=executor_name,
                requested_by=actor_id,
                agent_run_id=agent_run_id,
                provenance=ValidationProvenance().model_dump(mode="json"),
                manual_dispositions=[],
                created_at=datetime.now(UTC),
            )
            self.session.add(report)
            self.session.flush()
            view = self._report_view(question, report, actor_id)
            self.session.commit()
            return view

    def finish_validation(
        self,
        report_id: UUID,
        *,
        actor_id: UUID,
        output: ValidationOutput | None = None,
        error: TechnicalError | None = None,
        provenance: ValidationProvenance | None = None,
        agent_run_id: UUID | None = None,
    ) -> ValidationReportView:
        with self._transaction():
            candidate = self.session.get(QuestionValidationResult, report_id)
            if candidate is None:
                raise ContentValidationError(
                    "CONTENT_NOT_FOUND", "核验报告不存在。", http_status=404
                )
            question = cast(
                Question, self._owner("question", candidate.question_id, actor_id)
            )
            report = self._report(question.id, report_id)
            if report.outcome != "running":
                raise ContentValidationError(
                    "CONTENT_STATE_CONFLICT", "已结束报告不可改写原结果。"
                )
            if (output is None) == (error is None):
                raise ContentValidationError(
                    "CONTENT_OUTPUT_INVALID",
                    "须保存四项合法结果或真实错误之一。",
                    http_status=422,
                )
            if output is not None:
                evidence_ids = {
                    item.evidence_id
                    for item in ValidationInputRefs.model_validate(
                        report.input_refs
                    ).evidence
                }
                if any(
                    not set(check.evidence_refs) <= evidence_ids
                    for check in output.checks
                ) or any(
                    not set(issue.evidence_refs) <= evidence_ids
                    for issue in output.issues
                ):
                    raise ContentValidationError(
                        "CONTENT_SOURCE_INVALID",
                        "结果引用非当轮证据。",
                        http_status=422,
                    )
                report.checks = [
                    check.model_dump(mode="json") for check in output.checks
                ]
                report.issues = [
                    issue.model_dump(mode="json") for issue in output.issues
                ]
                passed = all(
                    check.verdict == "pass" for check in output.checks
                ) and not any(
                    issue.severity in {"warning", "error"} for issue in output.issues
                )
                report.outcome = "passed" if passed else "failed"
            else:
                report.outcome = "technical_error"
                report.error = cast(TechnicalError, error).model_dump(mode="json")
            report.completed_at = datetime.now(UTC)
            if agent_run_id is not None:
                if self.session.get(AgentRun, agent_run_id) is None:
                    raise ContentValidationError(
                        "CONTENT_SOURCE_INVALID", "执行Trace不存在。", http_status=422
                    )
                report.agent_run_id = agent_run_id
            if provenance is not None:
                report.provenance = provenance.model_dump(mode="json")
            latest = self.session.scalar(
                select(func.max(QuestionValidationResult.run_no)).where(
                    QuestionValidationResult.question_id == question.id
                )
            )
            if (
                report.outcome == "failed"
                and report.run_no == latest
                and report.input_revision == question.validation_revision
                and question.status == QuestionStatus.PENDING_REVIEW
            ):
                question.status = QuestionStatus.NEEDS_REVISION
                question.frozen_at = None
            self.session.flush()
            view = self._report_view(question, report, actor_id)
            self.session.commit()
            return view

    def list_validations(
        self, question_id: UUID, *, actor_id: UUID
    ) -> list[ValidationReportView]:
        with self._transaction():
            question = cast(Question, self._owner("question", question_id, actor_id))
            reports = self.session.scalars(
                select(QuestionValidationResult)
                .where(QuestionValidationResult.question_id == question_id)
                .order_by(QuestionValidationResult.run_no)
            ).all()
            return [self._report_view(question, report, actor_id) for report in reports]

    def get_validation(
        self, question_id: UUID, report_id: UUID, *, actor_id: UUID
    ) -> ValidationReportView:
        with self._transaction():
            question = cast(Question, self._owner("question", question_id, actor_id))
            return self._report_view(
                question, self._report(question_id, report_id), actor_id
            )

    def dispose_validation(
        self,
        question_id: UUID,
        report_id: UUID,
        payload: ManualDispositionRequest,
        *,
        actor_id: UUID,
    ) -> ValidationReportView:
        with self._transaction():
            question = cast(
                Question, self._owner("question", question_id, actor_id, writing=True)
            )
            report = self._report(question_id, report_id)
            latest = self.session.scalar(
                select(func.max(QuestionValidationResult.run_no)).where(
                    QuestionValidationResult.question_id == question_id
                )
            )
            if (
                report.run_no != latest
                or report.input_revision != question.validation_revision
                or report.outcome not in {"passed", "failed"}
            ):
                raise ContentValidationError(
                    "CONTENT_VALIDATION_STALE", "仅当前最新已结束内容报告可处置。"
                )
            refs = ValidationInputRefs.model_validate(report.input_refs)
            if not set(payload.issue_ids) <= {
                UUID(value["issue_id"]) for value in report.issues or []
            } or not set(payload.evidence_refs) <= {
                item.evidence_id for item in refs.evidence
            }:
                raise ContentValidationError(
                    "CONTENT_SOURCE_INVALID",
                    "处置引用不属于当前报告。",
                    http_status=422,
                )
            self._evidence(question, payload.additional_evidence, actor_id)
            comment_id = None
            if payload.action == "request_revision":
                if question.status not in {
                    QuestionStatus.PENDING_REVIEW,
                    QuestionStatus.NEEDS_REVISION,
                }:
                    raise ContentValidationError(
                        "CONTENT_STATE_CONFLICT",
                        "当前题目不可退回修订。",
                        current_status=question.status.value,
                    )
                comment = QuestionRevisionComment(
                    id=uuid4(),
                    question_id=question.id,
                    comment=payload.reason,
                    commented_by=actor_id,
                    commented_at=datetime.now(UTC),
                )
                self.session.add(comment)
                comment_id = comment.id
                question.status = QuestionStatus.NEEDS_REVISION
                question.frozen_at = None
            event = ManualDisposition(
                id=uuid4(),
                input_revision=question.validation_revision,
                handled_by=actor_id,
                handled_at=datetime.now(UTC),
                revision_comment_id=comment_id,
                **payload.model_dump(),
            )
            report.manual_dispositions = [
                *report.manual_dispositions,
                event.model_dump(mode="json"),
            ]
            self.session.flush()
            view = self._report_view(question, report, actor_id)
            self.session.commit()
            return view
