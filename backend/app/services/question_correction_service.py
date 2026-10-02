"""Teacher corrections and idempotent batch commits under the import lock."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm.attributes import flag_modified

from backend.app.domain.enums import ExtractedQuestionStatus as ES
from backend.app.domain.enums import PaperImportStatus as PS
from backend.app.domain.enums import QuestionSourceType, QuestionStatus, QuestionType
from backend.app.domain.question_options import options_equal
from backend.app.models import (
    ExtractedQuestion,
    PaperImport,
    Question,
    QuestionAsset,
    SourcePage,
)
from backend.app.schemas.image_assessment import (
    ImageAssessment,
    ImageInput,
    ImageInputRefs,
    ImportedImageReview,
    ImportedReviewSource,
    advance_image_context,
)
from backend.app.schemas.paper_import import (
    CommitQuestionView,
    CommitResponse,
    CorrectionPayload,
    ExtractedQuestionView,
    SourceRegion,
)
from backend.app.schemas.question_assets import StoredStagedAsset
from backend.app.services.file_storage_service import FileStorageError
from backend.app.services.paper_import_service import PaperImportService, question_view
from backend.app.services.question_asset_service import actual_image

IMAGE_TEXT_FIELDS = (
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


def completion_status(question: Question) -> Literal["complete", "needs_completion"]:
    if not question.reference_answer or not question.scoring_rubric:
        return "needs_completion"
    if question.assets:
        from sqlalchemy.orm import object_session

        from backend.app.schemas.image_assessment import (
            applicable_imported_review,
            current_image_check,
        )
        assessment = ImageAssessment.model_validate(question.image_assessment or {})
        check = current_image_check(assessment)
        binding = applicable_imported_review(assessment)
        refs = check.input_refs if check is not None and check.status == "confirmed" else None
        if refs is None and binding is not None:
            original = question.imported_extracted_question
            source = current_image_check(ImageAssessment.model_validate(original.image_assessment or {})) if original is not None else None
            if source is not None and source.id == binding.source_ref.check_id and source.status == "confirmed" and source.input_refs == binding.input_refs:
                refs = binding.input_refs
        if refs is None or {item.asset_id for item in refs.images} != {asset.id for asset in question.assets}:
            return "needs_completion"
        if object_session(question) is None:
            return "needs_completion"
        for index, asset in enumerate(question.assets, 1):
            ref = next((item for item in refs.images if item.asset_id == asset.id), None)
            if ref is None or ref.image_index != index or ref.file_id != asset.file_id or ref.width != asset.width or ref.height != asset.height or ref.source_page_id != asset.source_page_id or (ref.region.model_dump(mode="json") if ref.region else None) != asset.region:
                return "needs_completion"
    return "complete"


class QuestionCorrectionService(PaperImportService):
    def _commit(self) -> None:
        self.session.commit()

    def _all(self, identity: UUID) -> list[ExtractedQuestion]:
        return list(
            self.session.scalars(
                select(ExtractedQuestion)
                .where(ExtractedQuestion.paper_import_id == identity)
                .order_by(ExtractedQuestion.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )

    @staticmethod
    def _finish(paper: PaperImport, questions: list[ExtractedQuestion]) -> None:
        if questions and all(
            q.status in {ES.CORRECTED, ES.REJECTED} for q in questions
        ):
            paper.status = (
                PS.READY
                if any(q.status == ES.CORRECTED for q in questions)
                else PS.REJECTED
            )

    def _sources(
        self, record: ExtractedQuestion, *, readable: bool, actor_id: UUID
    ) -> None:
        if not record.source_page_ids:
            raise FileStorageError(
                "PAPER_SOURCE_INVALID",
                "来源页须为本次导入的真实非空页集合。",
                http_status=422,
            )
        pages = list(
            self.session.scalars(
                select(SourcePage)
                .where(
                    SourcePage.paper_import_id == record.paper_import_id,
                    SourcePage.id.in_([UUID(p) for p in record.source_page_ids]),
                )
                .order_by(SourcePage.page_number)
            )
        )
        if len(pages) != len(record.source_page_ids):
            raise FileStorageError(
                "PAPER_SOURCE_INVALID", "来源页不属于本次导入。", http_status=422
            )
        order = {str(page.id): page.page_number for page in pages}
        record.source_page_ids = [str(page.id) for page in pages]
        for region in record.source_regions or []:
            parsed = SourceRegion.model_validate(region)
            page = next((p for p in pages if p.id == parsed.source_page_id), None)
            if page is None:
                raise FileStorageError(
                    "PAPER_SOURCE_INVALID",
                    "题目区域必须属于已关联的来源页。",
                    http_status=422,
                )
            try:
                parsed.within(page.width, page.height)
            except ValueError as exc:
                raise FileStorageError(
                    "IMAGE_REGION_INVALID", str(exc), http_status=422
                ) from exc
        if record.source_regions is not None:
            record.source_regions = sorted(
                record.source_regions, key=lambda r: order[r["source_page_id"]]
            )
        for asset in record.assets or []:
            if asset["source_page_id"] not in order:
                raise FileStorageError(
                    "PAPER_SOURCE_INVALID",
                    "更改页来源时须同时处理失去来源的题图。",
                    http_status=422,
                )
        if readable:
            for page in pages:
                self.files.download("p_" + page.id.hex, actor_id=actor_id)

    def patch(
        self,
        identity: UUID,
        question_id: UUID,
        payload: CorrectionPayload,
        *,
        actor_id: UUID,
    ) -> ExtractedQuestionView:
        try:
            paper = self.get_record(identity, actor_id, lock=True)
            records = self._all(identity)
            record = next((q for q in records if q.id == question_id), None)
            if record is None:
                raise FileStorageError(
                    "PAPER_QUESTION_NOT_FOUND",
                    "暂存题不属于本次导入。",
                    http_status=404,
                )
            if (
                paper.status != PS.PENDING_REVIEW
                or record.status != ES.PENDING_CORRECTION
            ):
                raise FileStorageError(
                    "PAPER_STATE_CONFLICT",
                    "仅待校正题接受修改或拒绝。",
                    current_status=record.status.value,
                )
            if payload.action == "reject":
                if payload.model_fields_set - {"action", "correction_notes"}:
                    raise FileStorageError(
                        "PAPER_STATE_CONFLICT", "请先保存校正；拒绝命令只填写理由。"
                    )
                record.correction_notes = payload.correction_notes
                record.status = ES.REJECTED
            else:
                before = {
                    name: deepcopy(getattr(record, name)) for name in IMAGE_TEXT_FIELDS
                }
                old_assessment = deepcopy(record.image_assessment)
                data = payload.model_dump(
                    mode="json",
                    exclude_unset=True,
                    exclude={"action", "assets", "order_index"},
                )
                for name, value in data.items():
                    setattr(record, name, payload.score if name == "score" else value)
                options_changed = not options_equal(before["options"], record.options)
                if options_changed:
                    flag_modified(record, "options")
                    record.order_preserved = True
                if "assets" in payload.model_fields_set:
                    if payload.assets is None:
                        if record.assets is not None:
                            record.image_assessment = advance_image_context(
                                record.image_assessment
                            )
                        record.assets = None
                    else:
                        self.assets.apply_staged(
                            record, payload.assets, actor_id=actor_id
                        )
                self._sources(record, readable=False, actor_id=actor_id)
                if (
                    options_changed
                    or any(
                        before[name] != getattr(record, name)
                        for name in IMAGE_TEXT_FIELDS
                        if name != "options"
                    )
                ) and record.image_assessment == old_assessment:
                    record.image_assessment = advance_image_context(
                        record.image_assessment
                    )
                if "order_index" in payload.model_fields_set:
                    target = payload.order_index
                    if target != record.order_index:
                        other = next(
                            (
                                q
                                for q in records
                                if target is not None
                                and q.order_index == target
                                and q.id != record.id
                            ),
                            None,
                        )
                        if other is not None and other.status != ES.PENDING_CORRECTION:
                            raise FileStorageError(
                                "PAPER_STATE_CONFLICT",
                                "不能交换终态来源题序；请选择空闲题序。",
                            )
                        previous = record.order_index
                        record.order_index = None
                        if other is not None:
                            other.order_index = None
                        self.session.flush()
                        if other is not None:
                            other.order_index = previous
                        record.order_index = target
            self._finish(paper, records)
            self._commit()
            return question_view(record)
        except SQLAlchemyError as exc:
            self.session.rollback()
            raise FileStorageError(
                "PAPER_PERSISTENCE_FAILED",
                "校正保存失败，整次操作已回滚。",
                http_status=503,
            ) from exc
        except Exception:
            self.session.rollback()
            raise

    def _ready(
        self, record: ExtractedQuestion, actor_id: UUID
    ) -> list[tuple[StoredStagedAsset, int, int, str]]:
        if record.status != ES.PENDING_CORRECTION:
            raise FileStorageError(
                "PAPER_STATE_CONFLICT",
                "指定批次包含不可确认的题目。",
                current_status=record.status.value,
            )
        if (
            not record.content
            or record.question_type
            not in {
                QuestionType.SINGLE_CHOICE,
                QuestionType.TRUE_FALSE,
                QuestionType.SHORT_ANSWER,
            }
            or record.score is None
            or record.assets is None
            or record.order_index is None
        ):
            raise FileStorageError(
                "PAPER_CORRECTION_INCOMPLETE",
                "请核对题干、支持题型、分值、题序和题图关联（无图须明确为空）。",
            )
        options = record.options
        if options is not None and options != [] and options != {}:
            valid = isinstance(options, (list, dict))
            values = options.values() if isinstance(options, dict) else options
            valid = valid and all(
                isinstance(v, str) and bool(v.strip()) for v in values
            )
            if isinstance(options, dict):
                valid = valid and all(
                    isinstance(k, str) and bool(k.strip()) for k in options
                )
            if not valid:
                raise FileStorageError(
                    "PAPER_CORRECTION_INCOMPLETE",
                    "选项须沿用标签到文本的对象或按序文本数组。",
                )
        if record.question_type == QuestionType.SINGLE_CHOICE and (
            not options or len(options) < 2
        ):
            raise FileStorageError(
                "PAPER_CORRECTION_INCOMPLETE", "单选题须核对至少两个真实选项。"
            )
        self._sources(record, readable=True, actor_id=actor_id)
        images = []
        for raw in record.assets:
            item = StoredStagedAsset.model_validate(raw)
            path, _ = self.files.download(item.file_id, actor_id=actor_id)
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != item.file_meta.sha256:
                raise FileStorageError("FILE_CONTENT_CHANGED", "题图与登记内容不一致。")
            with actual_image(content) as image:
                mime = "image/png" if content.startswith(b"\x89PNG") else "image/jpeg"
                images.append((item, image.width, image.height, mime))
            self.assets._visibility_allowed(
                item.student_visible,
                page_id=item.source_page_id,
                region=item.region.model_dump(mode="json") if item.region else None,
                fingerprint=item.file_meta.sha256,
                locator=item.file_meta.storage_path,
            )
        return images

    @staticmethod
    def _binding(
        record: ExtractedQuestion,
        images: list[tuple[StoredStagedAsset, int, int, str]],
        actor_id: UUID,
    ) -> dict[str, Any] | None:
        if not images or record.image_assessment is None:
            return None
        assessment = ImageAssessment.model_validate(record.image_assessment)
        from backend.app.schemas.image_assessment import current_image_check
        check = current_image_check(assessment)
        if check is None or check.status != "confirmed":
            return None
        refs = ImageInputRefs(
            text_fields=[*IMAGE_TEXT_FIELDS, "caption"],
            images=[
                ImageInput(
                    asset_id=item.id,
                    file_id=item.file_id,
                    image_index=i,
                    asset_type=item.asset_type,
                    source_page_id=item.source_page_id,
                    region=item.region,
                    width=w,
                    height=h,
                    mime_type=mime,
                )
                for i, (item, w, h, mime) in enumerate(images, 1)
            ],
        )
        if check.input_refs != refs:
            return None
        binding = ImportedImageReview(
            id=uuid4(),
            context_revision=0,
            input_refs=refs,
            source_ref=ImportedReviewSource(owner_id=record.id, check_id=check.id),
            bound_by=actor_id,
            bound_at=datetime.now(UTC),
        )
        return ImageAssessment(imported_review=binding).model_dump(mode="json")

    def commit(
        self, identity: UUID, question_ids: list[UUID], *, actor_id: UUID
    ) -> CommitResponse:
        try:
            paper = self.get_record(identity, actor_id, lock=True)
            records = self._all(identity)
            by_id = {q.id: q for q in records}
            if not question_ids or len(set(question_ids)) != len(question_ids):
                raise FileStorageError(
                    "PAPER_BATCH_INVALID",
                    "请选择非空且不重复的题目集合。",
                    http_status=422,
                )
            if any(qid not in by_id for qid in question_ids):
                raise FileStorageError(
                    "PAPER_QUESTION_NOT_FOUND",
                    "指定题目不属于本次导入。",
                    http_status=404,
                )
            selected = [by_id[qid] for qid in question_ids]
            if paper.status not in {PS.PENDING_REVIEW, PS.READY} or any(
                q.status == ES.REJECTED for q in selected
            ):
                raise FileStorageError(
                    "PAPER_STATE_CONFLICT",
                    "当前导入或指定题目不可确认。",
                    current_status=paper.status.value,
                )
            prepared = {}
            for record in selected:
                if record.status == ES.CORRECTED:
                    continue
                if paper.status == PS.READY:
                    raise FileStorageError(
                        "PAPER_STATE_CONFLICT", "终态导入只能返回既有入库结果。"
                    )
                prepared[record.id] = self._ready(record, actor_id)
            for record in selected:
                if record.status == ES.CORRECTED:
                    continue
                images = prepared[record.id]
                question = Question(
                    id=uuid4(),
                    course_id=paper.course_id,
                    created_by=actor_id,
                    type=record.question_type,
                    content=record.content,
                    options=deepcopy(record.options),
                    order_preserved=record.order_preserved,
                    reference_answer=record.reference_answer,
                    scoring_rubric=record.scoring_rubric,
                    score=record.score,
                    analysis=record.analysis,
                    knowledge_points=deepcopy(record.knowledge_points) or [],
                    source_type=QuestionSourceType.PAPER_IMPORTED,
                    status=QuestionStatus.DRAFT,
                    image_assessment=self._binding(record, images, actor_id),
                )
                self.session.add(question)
                record.question = question
                record.status = ES.CORRECTED
                for i, (item, width, height, _mime) in enumerate(images, 1):
                    self.session.add(
                        QuestionAsset(
                            id=item.id,
                            question=question,
                            asset_type=item.asset_type,
                            width=width,
                            height=height,
                            source_page_id=item.source_page_id,
                            region=item.region.model_dump(mode="json")
                            if item.region
                            else None,
                            caption=item.caption,
                            student_visible=item.student_visible,
                            order_index=i,
                        )
                    )
            self._finish(paper, records)
            self.session.flush()
            result = CommitResponse(
                paper_import_id=paper.id,
                status=paper.status,
                questions=[
                    CommitQuestionView(
                        extracted_question_id=record.id,
                        question_id=record.question.id,
                        question_status=record.question.status,
                        order_preserved=record.question.order_preserved,
                        completion_status=completion_status(record.question),
                    )
                    for record in selected
                    if record.question is not None
                ],
            )
            self._commit()
            return result
        except SQLAlchemyError as exc:
            self.session.rollback()
            raise FileStorageError(
                "PAPER_PERSISTENCE_FAILED",
                "本批入库失败，全部新增题目与关联已回滚。",
                http_status=503,
            ) from exc
        except ValidationError as exc:
            self.session.rollback()
            raise FileStorageError(
                "PAPER_CORRECTION_INCOMPLETE",
                "已保存的来源或图像证据不合法，请重新校正。",
            ) from exc
        except Exception:
            self.session.rollback()
            raise
