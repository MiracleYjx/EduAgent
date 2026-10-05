"""T181 deterministic same-course recommendations under an own-final-submission grant.

No public file grant, model call, persisted recommendation or inferred scope is created.
The caller authorizes the actual submission before using this projection.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path, PureWindowsPath
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import Text, cast, func, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Session

from backend.app.ai.retrieval._filters import (
    apply_retrieval_filters,
    resolve_retrieval_scope,
)
from backend.app.ai.retrieval.base import (
    RetrievalFilters,
    RetrievalQuery,
    RetrievalUnsupportedDialectError,
)
from backend.app.domain.enums import QuestionStatus
from backend.app.models import (
    Document,
    DocumentChunk,
    KnowledgeBase,
    Question,
    QuestionAsset,
    QuestionSourceChunk,
    QuestionValidationResult,
)
from backend.app.schemas.content_validation import ValidationInputRefs, ValidationOutput
from backend.app.schemas.file_storage import FileMetadata
from backend.app.schemas.grading import (
    FeedbackAssetDTO,
    KnowledgePointRecommendationDTO,
    LearningSourceDTO,
    MaterialRecommendationDTO,
    PracticeRecommendationDTO,
    QuestionResultDTO,
)
from backend.app.schemas.image_assessment import current_image_check
from backend.app.services.content_validation_service import (
    ContentValidationError,
    ContentValidationService,
)
from backend.app.services.file_storage_service import (
    FileStorageError,
    FileStorageService,
)
from backend.app.services.question_asset_access import visible_question_assets
from backend.app.services.question_asset_service import actual_image


class LearningRecommendationService:
    """Current source reads; authorization is tied to the final answer context."""

    def __init__(self, session: Session, *, root: Path | None = None):
        self.session = session
        self.files = FileStorageService(session, root=root)

    def asset_views(self, question: Question, prefix: str) -> list[FeedbackAssetDTO]:
        return [
            FeedbackAssetDTO(
                asset_id=str(asset.id),
                asset_type=asset.asset_type,
                width=asset.width,
                height=asset.height,
                order=asset.order_index,
                caption=asset.caption,
                url=f"{prefix}/{asset.id}",
            )
            for asset in visible_question_assets(self.session, question)
            if asset.order_index is not None
        ]

    def _source(self, chunk: DocumentChunk) -> LearningSourceDTO:
        return LearningSourceDTO(
            kind="current_chunk",
            source_id=str(chunk.id),
            course_id=str(chunk.course_id),
            document_id=str(chunk.document_id),
            chunk_id=str(chunk.id),
            source_file=PureWindowsPath(chunk.document.original_filename).name,
            chapter_id=str(chunk.chapter_id) if chunk.chapter_id else None,
            section_order=chunk.section_order,
            chunk_index=chunk.chunk_index,
        )

    def _material_rows(self, course_id: UUID, point: str) -> list[DocumentChunk]:
        query = RetrievalQuery(text=point, embedding=(), knowledge_points=(point,))
        scope = resolve_retrieval_scope(
            self.session, query, RetrievalFilters(course_ids=(course_id,))
        )
        statement = (
            apply_retrieval_filters(select(DocumentChunk), scope)
            .join(KnowledgeBase, KnowledgeBase.id == DocumentChunk.knowledge_base_id)
            .where(
                Document.course_id == course_id,
                KnowledgeBase.course_id == course_id,
                Document.knowledge_base_id == DocumentChunk.knowledge_base_id,
            )
            .order_by(
                DocumentChunk.document_id, DocumentChunk.chunk_index, DocumentChunk.id
            )
        )
        return list(self.session.scalars(statement.limit(3)))

    def _practice_sources(
        self, question: Question, report: QuestionValidationResult
    ) -> list[LearningSourceDTO]:
        """Only real current approval evidence; a source-paper lineage is not teaching basis."""
        if (
            not question.reference_answer
            or not question.scoring_rubric
            or question.score <= 0
        ):
            return []
        try:
            refs = ValidationInputRefs.model_validate(report.input_refs)
            output = ValidationOutput.model_validate(
                {"checks": report.checks, "issues": report.issues}
            )
            if (
                any(check.verdict != "pass" for check in output.checks)
                or any(
                    issue.severity in {"warning", "error"} for issue in output.issues
                )
                or any(
                    item.get("action")
                    in {"provide_evidence", "resolve_issue", "request_revision"}
                    for item in report.manual_dispositions
                )
            ):
                return []
            known = {evidence.evidence_id for evidence in refs.evidence}
            if any(not set(check.evidence_refs) <= known for check in output.checks):
                return []
            validation = ContentValidationService(self.session, root=self.files.root)
            result = []
            for evidence in refs.evidence:
                if evidence.kind == "question_asset":
                    continue
                data = evidence.source_data
                if evidence.kind == "chunk":
                    # Read-only current course/Ready/source comparison; no teacher impersonation.
                    if validation._chunk_data(question, evidence.source_id) != data:
                        return []
                    chunk = self.session.get(DocumentChunk, evidence.source_id)
                    assert chunk is not None
                    result.append(self._source(chunk))
                else:
                    source = self.session.get(QuestionSourceChunk, evidence.source_id)
                    if (
                        source is None
                        or source.question_id != question.id
                        or source.course_id != question.course_id
                    ):
                        return []
                    expected = {
                        "chunk_id": str(source.chunk_id),
                        "document_id": str(source.document_id),
                        "course_id": str(source.course_id),
                        "source_file": source.source_file,
                        "location": None,
                        "content_snapshot": source.content_snapshot,
                    }
                    if expected != data:
                        return []
                    result.append(
                        LearningSourceDTO(
                            kind="saved_question_source_chunk",
                            source_id=str(source.id),
                            course_id=str(source.course_id),
                            document_id=str(source.document_id),
                            chunk_id=str(source.chunk_id),
                            source_file=PureWindowsPath(source.source_file).name,
                            chunk_index=source.chunk_index,
                        )
                    )
            return result
        except (ContentValidationError, ValidationError, ValueError):
            # Malformed/unknown sources are ineligible, never synthesized as a replacement.
            return []

    def _practice_images_available(self, question: Question) -> bool:
        if not question.assets:
            return True
        if any(asset.order_index is None for asset in question.assets):
            return False
        validation = ContentValidationService(self.session, root=self.files.root)
        assessment = validation._assessment(question)
        refs = validation._image_refs(question)
        check = current_image_check(assessment)
        if check is None and assessment.imported_review is not None:
            populated = refs.model_copy(
                update={
                    "images": [
                        image.model_copy(
                            update={
                                "width": asset.width,
                                "height": asset.height,
                                "mime_type": (asset.file_metadata or {}).get(
                                    "media_type"
                                ),
                            }
                        )
                        for image, asset in zip(
                            refs.images, question.assets, strict=True
                        )
                    ]
                }
            )
            check = validation._bound_check(question, assessment, populated)
        elif check is not None and not validation._same_identity(
            check.input_refs, refs
        ):
            return False
        if check is None or check.status != "confirmed":
            return False
        required = {condition.asset_id for condition in check.confirmed_conditions}
        required.update(
            finding.asset_id
            for finding in check.image_findings
            if finding.finding == "conditions_confirmed"
        )
        visible = {
            asset.id for asset in visible_question_assets(self.session, question)
        }
        return required <= visible

    def _practice_rows(
        self, course_id: UUID, point: str
    ) -> list[tuple[Question, list[LearningSourceDTO]]]:
        if self.session.get_bind().dialect.name != "postgresql":
            raise RetrievalUnsupportedDialectError(
                "练习知识点范围依赖 PostgreSQL JSONB 精确成员查询。"
            )
        latest = (
            select(func.max(QuestionValidationResult.run_no))
            .where(QuestionValidationResult.question_id == Question.id)
            .correlate(Question)
            .scalar_subquery()
        )
        labels = cast(Question.knowledge_points, JSONB)
        statement = (
            select(Question, QuestionValidationResult)
            .join(
                QuestionValidationResult,
                QuestionValidationResult.question_id == Question.id,
            )
            .where(
                Question.course_id == course_id,
                Question.status == QuestionStatus.APPROVED,
                QuestionValidationResult.input_revision == Question.validation_revision,
                QuestionValidationResult.run_no == latest,
                QuestionValidationResult.outcome == "passed",
                func.jsonb_typeof(labels) == "array",
                labels.has_any(cast([point], ARRAY(Text))),
            )
        )
        result = []
        for question, report in self.session.execute(statement.order_by(Question.id)):
            sources = self._practice_sources(question, report)
            if sources and self._practice_images_available(question):
                result.append((question, sources))
            if len(result) == 3:
                break
        return result

    def recommendations(
        self, course_id: UUID, submission_id: str, items: list[QuestionResultDTO]
    ) -> list[KnowledgePointRecommendationDTO]:
        """Recommend only labels with actual final loss, retaining per-point source identities."""
        points = {
            p
            for item in items
            if (item.effective_score or Decimal(0)) < item.max_score
            for p in item.knowledge_points
        }
        groups = []
        prefix = f"/api/results/me/submissions/{submission_id}/learning"
        for point in sorted(points):
            related = [item for item in items if point in item.knowledge_points]
            awarded = sum(
                (item.effective_score or Decimal(0) for item in related), Decimal(0)
            )
            maximum = sum((item.max_score for item in related), Decimal(0))
            losses = [
                item
                for item in related
                if (item.effective_score or Decimal(0)) < item.max_score
            ]
            reason = f"本人最终答卷在知识点「{point}」失分 {maximum-awarded}/{maximum}，关联 {len(losses)} 个失分答案。"
            materials = [
                MaterialRecommendationDTO(
                    chunk_id=str(chunk.id),
                    knowledge_points=list(chunk.chunk_metadata["knowledge_points"]),
                    source=self._source(chunk),
                    reason=reason,
                    url=f"{prefix}/materials/{chunk.id}",
                )
                for chunk in self._material_rows(course_id, point)
            ]
            practices = [
                PracticeRecommendationDTO(
                    question_id=str(question.id),
                    knowledge_points=question.knowledge_points,
                    sources=sources,
                    reason=reason,
                    url=f"{prefix}/practices/{question.id}",
                    assets=self.asset_views(
                        question, f"{prefix}/practices/{question.id}/assets"
                    ),
                )
                for question, sources in self._practice_rows(course_id, point)
            ]
            groups.append(
                KnowledgePointRecommendationDTO(
                    knowledge_point=point,
                    answer_ids=[item.answer_id for item in losses],
                    exam_question_ids=[str(item.exam_question_id) for item in losses],
                    awarded_score=awarded,
                    maximum_score=maximum,
                    lost_score=maximum - awarded,
                    materials=materials,
                    practices=practices,
                    material_not_ready_reason=(
                        None
                        if materials
                        else "没有同课程、Ready 且已确认精确标签的复习片段。"
                    ),
                    practice_not_ready_reason=(
                        None
                        if practices
                        else "没有当前已审核、来源完整且必要题图已开放的匹配练习；未知/待补全/未审核题不作补位。"
                    ),
                )
            )
        return groups

    def read_asset(self, asset: QuestionAsset) -> tuple[bytes, str]:
        """Caller has just verified the actual submission/practice grant and visibility."""
        path = self.files._existing_path(
            asset.storage_path, legacy=self.files._legacy_metadata(asset.file_metadata)
        )
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise FileStorageError(
                "FILE_MISSING", "题图文件缺失。", http_status=404
            ) from None
        except OSError:
            raise FileStorageError(
                "FILE_UNREADABLE", "题图文件不可读。", http_status=503
            ) from None
        if asset.file_metadata is not None:
            metadata = FileMetadata.model_validate(asset.file_metadata)
            if (
                metadata.size_bytes != len(data)
                or metadata.sha256 != hashlib.sha256(data).hexdigest()
            ):
                raise FileStorageError(
                    "FILE_CONTENT_CHANGED", "原图字节与文件登记不一致。"
                )
        with actual_image(data) as image:
            if image.size != (asset.width, asset.height):
                raise FileStorageError("FILE_CONTENT_CHANGED", "原图尺寸与登记不一致。")
            mime = (
                "image/png" if data.startswith(b"\x89PNG\r\n\x1a\n") else "image/jpeg"
            )
        return data, mime
