"""Deterministic actions backed only by confirmed, real material associations."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.domain.enums import DocumentPurpose
from backend.app.models.document import Document
from backend.app.models.document_chunk import DocumentChunk
from backend.app.models.subject import MaterialVersion
from backend.app.schemas.personal_diagnosis import LearningSuggestion, ReviewSource
from backend.app.schemas.personal_practice import PracticeConditions


def learning_suggestion(
    session: Session, subject_id: UUID, point_id: UUID, *, reinforce: bool
) -> LearningSuggestion:
    rows = session.execute(
        select(DocumentChunk, Document, MaterialVersion.id)
        .join(Document, Document.id == DocumentChunk.document_id)
        .outerjoin(MaterialVersion, MaterialVersion.document_id == Document.id)
        .where(
            Document.subject_id == subject_id,
            DocumentChunk.subject_id == subject_id,
            Document.purpose == DocumentPurpose.KNOWLEDGE_BASE,
        )
        .order_by(Document.created_at, DocumentChunk.chunk_index)
    ).all()
    sources = []
    for chunk, document, version in rows:
        meta = chunk.chunk_metadata or {}
        if (
            meta.get("scope_confirmation", {})
            .get("knowledge_points", {})
            .get(str(point_id))
            != "confirmed"
        ):
            continue
        location = meta.get("location")
        if isinstance(location, str):
            location = {"anchor": location}
        if not isinstance(location, dict) or not (
            location.get("page") or location.get("anchor")
        ):
            continue
        sources.append(
            ReviewSource(
                document_id=document.id,
                chunk_id=chunk.id,
                material_version_id=version,
                title=document.original_filename,
                location=location,
                excerpt=chunk.content[:800],
            )
        )
        if len(sources) == 5:
            break
    return LearningSuggestion(
        knowledge_point_id=point_id,
        message="可复习以下真实关联资料，再预览针对性练习。"
        if sources
        else "没有已确认关联的学习资料；可上传并确认资料，或自行选择练习条件。",
        sources=sources,
        practice_conditions=PracticeConditions(
            knowledge_point_ids=[point_id], mode="weak" if reinforce else "balanced"
        ),
    )
