"""Student image grants reuse exam participation and personal submitted answers."""
from __future__ import annotations

from uuid import UUID

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
