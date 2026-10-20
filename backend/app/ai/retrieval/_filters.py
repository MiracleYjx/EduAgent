"""SQL teaching scope shared by both recall routes.

Personal study materials are subject-owned, so the scope must validate the
subject/version boundary before either recall route runs. Legacy course scope is
preserved: a chunk belongs to exactly one of ``course_id`` or ``subject_id``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, Text, and_, cast, func, or_, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Session

from backend.app.ai.retrieval.base import (
    RetrievalFilters,
    RetrievalQuery,
    RetrievalScopeInvalidError,
    RetrievalScopeNotReadyError,
    RetrievalUnsupportedDialectError,
    resolve_dialect_name,
    resolve_filters,
)
from backend.app.domain.enums import DocumentPurpose, DocumentStatus
from backend.app.models import Chapter, Document, DocumentChunk
from backend.app.models.knowledge_base import KnowledgeBase
from backend.app.models.subject import MaterialVersion, Subject


@dataclass(frozen=True, slots=True)
class _SQLScope(RetrievalFilters):
    # Reference the immutable Query; do not create a second public chapter/tag input.
    query: RetrievalQuery | None = None


def resolve_retrieval_scope(
    session: Session,
    query: RetrievalQuery | None,
    filters: RetrievalFilters | None,
) -> RetrievalFilters:
    """Validate ownership/catalogue once before either recall route executes."""

    scope = resolve_filters(filters)
    if isinstance(scope, _SQLScope):
        if query is not None:
            stored = scope.query
            old = (
                (stored.chapter_ids, stored.section_range, stored.knowledge_points)
                if stored
                else ((), None, ())
            )
            new = (query.chapter_ids, query.section_range, query.knowledge_points)
            if old != new:
                raise RetrievalScopeInvalidError("已解析范围与当前 Query 不一致。")
        return scope
    logical = query is not None and query.has_scope
    check_documents = bool(scope.document_ids and (scope.course_ids or scope.subject_ids))
    subjects = _validate_subjects(session, scope)
    versions = _validate_versions(session, scope, subjects)
    if not logical and not check_documents and not versions:
        return _expanded(session, scope, subjects, versions, query)
    if logical and not (scope.course_ids or scope.subject_ids):
        raise RetrievalScopeInvalidError(
            "指定章节或知识点前必须限定已授权课程或科目。"
        )
    if check_documents:
        statement = select(Document.id).where(
            Document.id.in_(scope.document_ids),
            Document.purpose == DocumentPurpose.KNOWLEDGE_BASE,
        )
        if scope.course_ids:
            statement = statement.where(Document.course_id.in_(scope.course_ids))
        if scope.subject_ids:
            statement = statement.where(Document.subject_id.in_(scope.subject_ids))
        found = set(session.scalars(statement))
        if found != set(scope.document_ids):
            raise RetrievalScopeInvalidError(
                "所选资料不存在、不是教学资料或不属于当前授权范围。"
            )
    if query is not None:
        chapter_ids = set(query.chapter_ids)
        if query.section_range is not None:
            chapter_ids.add(query.section_range.chapter_id)
        if chapter_ids:
            chapters = {
                row.id: row
                for row in session.scalars(
                    select(Chapter).where(Chapter.id.in_(chapter_ids))
                )
            }
            if set(chapters) != chapter_ids:
                raise RetrievalScopeInvalidError("所选章节不存在。")
            for row in chapters.values():
                if scope.course_ids and row.course_id not in scope.course_ids:
                    raise RetrievalScopeInvalidError(
                        "所选章节不存在或不属于当前授权课程。"
                    )
                if scope.subject_ids and row.subject_id not in scope.subject_ids:
                    raise RetrievalScopeInvalidError(
                        "所选章节不存在或不属于当前授权科目。"
                    )
            if query.section_range is not None:
                chapter = chapters[query.section_range.chapter_id]
                if not chapter.sections:
                    raise RetrievalScopeNotReadyError(
                        "该章节尚无已确认小节目录，无法查询小节范围。"
                    )
                orders = {entry["section_order"] for entry in chapter.sections}
                if (
                    query.section_range.start_order not in orders
                    or query.section_range.end_order not in orders
                ):
                    raise RetrievalScopeInvalidError("小节范围端点不在当前章节目录中。")
        if query.knowledge_points and resolve_dialect_name(session) != "postgresql":
            raise RetrievalUnsupportedDialectError(
                "知识点范围依赖 PostgreSQL JSONB 精确成员查询。"
            )
    return _expanded(session, scope, subjects, versions, query)


def _validate_subjects(
    session: Session, scope: RetrievalFilters
) -> tuple[Any, ...]:
    if not scope.subject_ids:
        return ()
    rows = {
        row.id: row
        for row in session.scalars(
            select(Subject).where(Subject.id.in_(scope.subject_ids))
        )
    }
    if set(rows) != set(scope.subject_ids):
        raise RetrievalScopeInvalidError("所选科目不存在。")
    return tuple(rows.values())


def _validate_versions(
    session: Session, scope: RetrievalFilters, subjects: tuple[Any, ...]
) -> tuple[Any, ...]:
    if not scope.material_version_ids:
        return ()
    rows = {
        row.id: row
        for row in session.scalars(
            select(MaterialVersion).where(
                MaterialVersion.id.in_(scope.material_version_ids)
            )
        )
    }
    if set(rows) != set(scope.material_version_ids):
        raise RetrievalScopeInvalidError("所选资料版本不存在。")
    allowed = {row.id for row in subjects}
    for row in rows.values():
        if allowed and row.subject_id not in allowed:
            raise RetrievalScopeInvalidError("所选资料版本不属于当前授权科目。")
    return tuple(rows.values())


def _expanded(
    session: Session,
    scope: RetrievalFilters,
    subjects: tuple[Any, ...],
    versions: tuple[Any, ...],
    query: RetrievalQuery | None,
) -> RetrievalFilters:
    """Attach the personal knowledge bases so index identity is still checked."""

    knowledge_base_ids = list(scope.knowledge_base_ids)
    if subjects and not knowledge_base_ids:
        for kb_id in session.scalars(
            select(KnowledgeBase.id).where(
                KnowledgeBase.subject_id.in_([row.id for row in subjects])
            )
        ):
            if kb_id not in knowledge_base_ids:
                knowledge_base_ids.append(kb_id)
    del versions
    # 章节/小节/知识点范围必须随 SQL scope 一起下传；丢弃 Query 等于静默放宽范围。
    return _SQLScope(
        course_ids=scope.course_ids,
        knowledge_base_ids=tuple(knowledge_base_ids),
        document_ids=scope.document_ids,
        subject_ids=scope.subject_ids,
        material_version_ids=scope.material_version_ids,
        query=query,
    )


def apply_retrieval_filters(
    statement: Select[Any], scope: RetrievalFilters
) -> Select[Any]:
    """Apply all predicates before scores/ORDER BY/LIMIT; never trim recalled hits."""

    statement = statement.join(
        Document, Document.id == DocumentChunk.document_id
    ).where(
        # 教学检索只接受已索引资料与教学用途；试卷答案不默认进入教材 RAG。
        Document.status == DocumentStatus.READY,
        Document.purpose == DocumentPurpose.KNOWLEDGE_BASE,
    )
    if scope.course_ids:
        statement = statement.where(DocumentChunk.course_id.in_(scope.course_ids))
    if scope.subject_ids:
        statement = statement.where(
            DocumentChunk.subject_id.in_(scope.subject_ids),
            Document.subject_id.in_(scope.subject_ids),
        )
    if scope.knowledge_base_ids:
        statement = statement.where(
            DocumentChunk.knowledge_base_id.in_(scope.knowledge_base_ids)
        )
    if scope.document_ids:
        statement = statement.where(DocumentChunk.document_id.in_(scope.document_ids))
    if scope.material_version_ids:
        statement = statement.where(
            DocumentChunk.document_id.in_(
                select(MaterialVersion.document_id).where(
                    MaterialVersion.id.in_(scope.material_version_ids)
                )
            )
        )
    query = scope.query if isinstance(scope, _SQLScope) else None
    if query is not None:
        if query.chapter_ids:
            statement = statement.where(DocumentChunk.chapter_id.in_(query.chapter_ids))
        if query.section_range is not None:
            selected = query.section_range
            statement = statement.where(
                DocumentChunk.chapter_id == selected.chapter_id,
                DocumentChunk.section_order.between(
                    selected.start_order, selected.end_order
                ),
            )
        if query.knowledge_points:
            metadata = cast(DocumentChunk.chunk_metadata, JSONB)
            labels = metadata["knowledge_points"]
            confirmation = metadata["scope_confirmation"]["knowledge_points"]
            legacy_confirmed = and_(
                DocumentChunk.subject_id.is_(None),
                func.jsonb_typeof(confirmation["confirmed_by"]) == "string",
                func.length(func.btrim(confirmation["confirmed_by"].astext)) > 0,
                func.jsonb_typeof(confirmation["confirmed_at"]) == "string",
                func.length(func.btrim(confirmation["confirmed_at"].astext)) > 0,
            )
            personal_confirmed = and_(
                DocumentChunk.subject_id.is_not(None),
                or_(
                    *(
                        and_(
                            labels.has_key(point),
                            confirmation.contains({point: "confirmed"}),
                        )
                        for point in query.knowledge_points
                    )
                ),
            )
            statement = statement.where(
                func.jsonb_typeof(confirmation) == "object",
                func.jsonb_typeof(labels) == "array",
                labels.has_any(cast(list(query.knowledge_points), ARRAY(Text))),
                or_(legacy_confirmed, personal_confirmed),
            )
    return statement


__all__ = ["apply_retrieval_filters", "resolve_retrieval_scope"]
