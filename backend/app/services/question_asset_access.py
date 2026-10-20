"""Student image grants reuse exam participation and personal submitted answers."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from backend.app.core.security import get_user_roles
from backend.app.domain.enums import DocumentPurpose, SubmissionStatus, UserRole
from backend.app.models import (
    Answer,
    Document,
    Exam,
    Question,
    QuestionAsset,
    SourcePage,
    Submission,
    User,
)
from backend.app.schemas.paper_import import PixelRegion
from backend.app.services.submission_service import (
    SubmissionNotAvailableError,
    SubmissionNotFoundError,
    SubmissionPermissionError,
    SubmissionService,
)

if TYPE_CHECKING:
    from backend.app.models.question_revision import QuestionRevision
    from backend.app.schemas.content_blocks import (
        QuestionContentView,
        RevisionContent,
        SourceLocation,
    )
    from backend.app.schemas.file_storage import FileMetadata
    from backend.app.schemas.settings import EnvironmentProfile


def permitted_student_image(
    session: Session, *, source_page_id: UUID | None, region: dict | None,
    fingerprint: str | None, locator: str | None,
) -> bool:
    """A whole source page stays private, including registered byte/path aliases."""
    if source_page_id is not None:
        page = session.get(SourcePage, source_page_id)
        if page is None or region is None:
            return False
        bounds = PixelRegion.model_validate(region)
        bounds.within(page.width, page.height)
        if bounds.bbox == (0, 0, page.width, page.height):
            return False
    page_conditions = []
    document_conditions = []
    if fingerprint:
        page_conditions.append(SourcePage.file_metadata["sha256"].as_string() == fingerprint)
        document_conditions.append(Document.file_metadata["sha256"].as_string() == fingerprint)
    if locator:
        page_conditions.append(SourcePage.image_path == locator)
        document_conditions.append(Document.storage_path == locator)
    if page_conditions and session.scalar(select(SourcePage.id).where(or_(*page_conditions)).limit(1)) is not None:
        return False
    return not (document_conditions and session.scalar(select(Document.id).where(
        Document.purpose == DocumentPurpose.PAPER_SOURCE, or_(*document_conditions),
    ).limit(1)) is not None)


def student_asset_allowed(session: Session, asset: QuestionAsset) -> bool:
    return asset.student_visible is True and permitted_student_image(
        session, source_page_id=asset.source_page_id, region=asset.region,
        fingerprint=(asset.file_metadata or {}).get("sha256"), locator=asset.storage_path,
    )


def visible_question_assets(session: Session, question: Question) -> list[QuestionAsset]:
    return [asset for asset in question.assets if student_asset_allowed(session, asset)]


def student_can_read_question(session: Session, question: Question, actor: User) -> bool:
    if not actor.is_active or UserRole.STUDENT not in get_user_roles(actor):
        return False
    # Historical feedback uses the actual question in this student's submitted answer.
    own_answer = session.scalar(select(Answer.id).join(Submission).join(Exam).where(
        Answer.question_id == question.id, Submission.student_id == actor.id,
        Submission.status.in_((SubmissionStatus.SUBMITTED, SubmissionStatus.GRADED, SubmissionStatus.REVIEWED)),
        Exam.course_id == question.course_id,
    ).limit(1))
    if own_answer is not None:
        return True
    submissions = SubmissionService(session)
    for exam in question.exams:
        try:
            submissions.get_available_exam(exam.id, student_id=actor.id)
        except (SubmissionNotAvailableError, SubmissionPermissionError, SubmissionNotFoundError):
            continue
        return True
    return False


class PersonalContentAccess:
    """Read a fixed practice revision; the database determines visibility.

    No caller-supplied phase, owner, path or current-revision substitution.
    This is a content seam, not practice selection/submission/grading.
    """

    def __init__(self, session: Session, profile: EnvironmentProfile):
        from backend.app.services.file_storage_service import FileStorageService

        self.session = session
        self.storage = FileStorageService(session, profile=profile)

    def context(self, subject_id: UUID, practice_id: UUID, item_id: UUID) -> tuple[QuestionRevision, RevisionContent, Literal["answering", "review"]]:
        from backend.app.models.practice import PracticeItem, PracticeSession
        from backend.app.models.question_origin import QuestionOrigin
        from backend.app.models.question_revision import QuestionRevision
        from backend.app.schemas.content_blocks import OriginalBlock, RevisionContent
        from backend.app.services.subject_service import SubjectError

        practice = self.session.get(PracticeSession, practice_id)
        item = self.session.get(PracticeItem, item_id)
        if (practice is None or item is None or practice.subject_id != subject_id
                or item.subject_id != subject_id or item.session_id != practice_id):
            raise SubjectError("CONTENT_NOT_FOUND", 404)
        revision = self.session.get(QuestionRevision, item.question_revision_id)
        if (revision is None or revision.question_id != item.question_id
                or revision.subject_id != subject_id):
            raise SubjectError("CONTENT_NOT_FOUND", 404)
        try:
            content = RevisionContent.model_validate({
                "question_type": revision.question_type, "stem_blocks": revision.stem_blocks,
                "options": revision.options, "explanation_blocks": revision.explanation_blocks,
            })
        except ValidationError:
            raise SubjectError("CONTENT_INVALID", 409) from None
        for block in content.blocks():
            if isinstance(block, OriginalBlock):
                origins = self.session.scalars(select(QuestionOrigin).where(
                    QuestionOrigin.question_id == revision.question_id,
                    QuestionOrigin.subject_id == subject_id,
                    QuestionOrigin.document_id == block.source.document_id,
                )).all()
                location = block.source.model_dump(exclude={"document_id"}, exclude_none=True)
                if not any(all(origin.source_location.get(k) == v for k, v in location.items()) for origin in origins):
                    raise SubjectError("CONTENT_SOURCE_INVALID", 409)
                asset = self._asset(revision, content, block.asset_id)
                if self._asset_source(asset, revision) != block.source:
                    raise SubjectError("CONTENT_SOURCE_INVALID", 409)
        phase: Literal["answering", "review"] = "review" if practice.state in {"submitted", "grading", "completed", "partial_failed"} else "answering"
        return revision, content, phase

    def _asset(self, revision: QuestionRevision, content: RevisionContent, asset_id: UUID) -> QuestionAsset:
        from backend.app.models.paper_import import PaperImport
        from backend.app.models.question_origin import QuestionOrigin
        from backend.app.models.question_revision import question_revision_assets
        from backend.app.services.subject_service import SubjectError

        bound = self.session.scalar(select(question_revision_assets.c.asset_id).where(
            question_revision_assets.c.revision_id == revision.id,
            question_revision_assets.c.asset_id == asset_id,
        ))
        asset = self.session.get(QuestionAsset, asset_id) if bound is not None else None
        if (asset is None or asset.question_id != revision.question_id
                or asset_id not in content.asset_ids()):
            raise SubjectError("CONTENT_ASSET_NOT_FOUND", 404)
        if asset.source_page_id is not None:
            source = self.session.scalar(select(SourcePage.id).join(
                PaperImport, PaperImport.id == SourcePage.paper_import_id,
            ).join(QuestionOrigin, QuestionOrigin.document_id == PaperImport.document_id).where(
                SourcePage.id == asset.source_page_id,
                QuestionOrigin.question_id == revision.question_id,
                QuestionOrigin.subject_id == revision.subject_id,
            ).limit(1))
            if source is None:
                raise SubjectError("CONTENT_SOURCE_INVALID", 409)
        self._asset_source(asset, revision)
        return asset

    def _asset_source(self, asset: QuestionAsset, revision: QuestionRevision) -> SourceLocation | None:
        from backend.app.models.paper_import import PaperImport
        from backend.app.models.question_origin import QuestionOrigin
        from backend.app.schemas.content_blocks import SourceLocation
        from backend.app.services.subject_service import SubjectError

        if asset.source_document_id is not None:
            if asset.source_page_id is not None:
                raise SubjectError("CONTENT_SOURCE_INVALID", 409)
            document = self.session.get(Document, asset.source_document_id)
            origin = self.session.scalar(select(QuestionOrigin.id).where(
                QuestionOrigin.question_id == revision.question_id,
                QuestionOrigin.subject_id == revision.subject_id,
                QuestionOrigin.document_id == asset.source_document_id,
                QuestionOrigin.source_location["anchor"].as_string() == asset.source_anchor,
            ).limit(1))
            if document is None or document.subject_id != revision.subject_id or origin is None:
                raise SubjectError("CONTENT_SOURCE_INVALID", 409)
            try:
                return SourceLocation(
                    document_id=document.id, anchor=asset.source_anchor,
                    page=next((block.get("page") for block in (document.parse_facts or {}).get("blocks", [])
                               if block.get("anchor")==asset.source_anchor),None),
                )
            except ValidationError:
                raise SubjectError("CONTENT_SOURCE_INVALID", 409) from None
        if asset.source_page_id is not None:
            page = self.session.get(SourcePage, asset.source_page_id)
            paper = self.session.get(PaperImport, page.paper_import_id) if page else None
            if page is not None and paper is not None:
                return SourceLocation(document_id=paper.document_id, page=page.page_number)
        return None

    def _answer_safe(self, asset: QuestionAsset) -> bool:
        try:
            return permitted_student_image(
                self.session, source_page_id=asset.source_page_id, region=asset.region,
                fingerprint=(asset.file_metadata or {}).get("sha256"), locator=asset.storage_path,
            )
        except ValueError:
            return False

    def asset_path(self, subject_id: UUID, practice_id: UUID, item_id: UUID, asset_id: UUID) -> tuple[Path, FileMetadata]:
        from backend.app.services.subject_service import SubjectError

        revision, content, phase = self.context(subject_id, practice_id, item_id)
        if asset_id not in content.asset_ids(include_explanation=phase == "review"):
            raise SubjectError("CONTENT_ASSET_NOT_FOUND", 404)
        asset = self._asset(revision, content, asset_id)
        if phase == "answering" and not self._answer_safe(asset):
            raise SubjectError("CONTENT_ASSET_STAGE_FORBIDDEN", 403)
        return self._image_path(asset)

    def _image_path(self, asset: QuestionAsset) -> tuple[Path, FileMetadata]:
        from backend.app.services.file_storage_service import FileStorageError
        from backend.app.services.subject_service import SubjectError

        path, metadata = self.storage.personal_managed_path(asset.storage_path, asset.file_metadata)
        if metadata.media_type not in {"image/png", "image/jpeg"}:
            raise SubjectError("CONTENT_ASSET_MEDIA_UNSUPPORTED", 409)
        # A registered MIME alone must not turn HTML/SVG into an active image.
        try:
            with path.open("rb") as stream:
                signature = stream.read(8)
        except OSError:
            raise FileStorageError("FILE_MISSING", "已登记文件不可读。", http_status=404) from None
        if not (metadata.media_type == "image/png" and signature == b"\x89PNG\r\n\x1a\n"
                or metadata.media_type == "image/jpeg" and signature.startswith(b"\xff\xd8\xff")):
            raise SubjectError("CONTENT_ASSET_MEDIA_INVALID", 409)
        return path, metadata

    @staticmethod
    def chart_ready(content, revision) -> bool:
        from backend.app.schemas.content_blocks import ChartBlock, OriginalBlock
        from backend.app.schemas.learning_charts import review_chart
        from backend.app.schemas.personal_learning import PersonalQuality
        if any(isinstance(block, OriginalBlock) for block in content.blocks()):
            return False
        charts=[(i,block) for i,block in enumerate(content.blocks()) if isinstance(block,ChartBlock)]
        if not charts:
            return True
        try:
            quality=PersonalQuality.model_validate(revision.grounding_refs.get("quality"))
        except (ValueError,TypeError):
            return False
        return all(review_chart(
            block.chart,answer_points=quality.chart_answer_points.get(str(index)),
            claimed_consistent=all(check.verdict=="pass" for check in quality.assessment.checks),
        )=="consistent" for index,block in charts)

    def view(self, subject_id: UUID, practice_id: UUID, item_id: UUID) -> QuestionContentView:
        from backend.app.schemas.content_blocks import (
            QuestionContentView,
        )
        from backend.app.services.file_storage_service import FileStorageError
        from backend.app.services.subject_service import SubjectError

        revision, content, phase = self.context(subject_id, practice_id, item_id)
        issues = []
        sources = {}
        ready = (revision.admission_state == "eligible" and self.chart_ready(content, revision)
                 and bool(revision.reference_answer and revision.reference_answer.strip())
                 and (content.question_type != "short_answer" or bool(revision.scoring_rubric and revision.scoring_rubric.strip())))
        for asset_id in content.asset_ids():
            try:
                asset = self._asset(revision, content, asset_id)
                self._image_path(asset)
                source = self._asset_source(asset, revision)
                if source is None:
                    ready = False
                    if asset_id in content.asset_ids(include_explanation=phase == "review"):
                        issues.append("CONTENT_SOURCE_UNCONFIRMED")
                elif asset_id in content.asset_ids(include_explanation=phase == "review"):
                    sources[str(asset_id)] = source
                if not self._answer_safe(asset) and asset_id in content.asset_ids(include_explanation=False):
                    ready = False
            except (SubjectError, FileStorageError, ValueError, OSError):
                ready = False
                if asset_id in content.asset_ids(include_explanation=phase == "review"):
                    issues.append("CONTENT_RESOURCE_UNAVAILABLE")
        return QuestionContentView(
            question_id=revision.question_id, revision_id=revision.id,
            question_type=content.question_type, phase=phase,
            stem_blocks=content.stem_blocks, options=content.options,
            explanation_blocks=content.explanation_blocks if phase == "review" else None,
            automatic_grading_ready=ready, issues=sorted(set(issues)), asset_sources=sources,
        )
