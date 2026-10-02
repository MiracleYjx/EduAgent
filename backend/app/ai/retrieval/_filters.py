"""SQL teaching scope shared by both recall routes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, Text, cast, func, select
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
    check_documents = bool(scope.document_ids and scope.course_ids)
    if not logical and not check_documents:
        return scope
    if logical and not scope.course_ids:
        raise RetrievalScopeInvalidError("指定章节或知识点前必须限定已授权课程。")
    if check_documents:
        found = set(
            session.scalars(
                select(Document.id).where(
                    Document.id.in_(scope.document_ids),
                    Document.course_id.in_(scope.course_ids),
                    Document.purpose == DocumentPurpose.KNOWLEDGE_BASE,
                )
            )
        )
        if found != set(scope.document_ids):
            raise RetrievalScopeInvalidError(
                "所选资料不存在、不是教学资料或不属于当前授权课程。"
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
            if set(chapters) != chapter_ids or any(
                row.course_id not in scope.course_ids for row in chapters.values()
            ):
                raise RetrievalScopeInvalidError("所选章节不存在或不属于当前授权课程。")
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
    return _SQLScope(
        course_ids=scope.course_ids,
        knowledge_base_ids=scope.knowledge_base_ids,
        document_ids=scope.document_ids,
        query=query,
    )


def apply_retrieval_filters(
    statement: Select[Any], scope: RetrievalFilters
) -> Select[Any]:
    """Apply all predicates before scores/ORDER BY/LIMIT; never trim recalled hits."""
    statement = statement.join(
        Document, Document.id == DocumentChunk.document_id
    ).where(
        Document.status == DocumentStatus.READY,
        Document.purpose == DocumentPurpose.KNOWLEDGE_BASE,
    )
    if scope.course_ids:
        statement = statement.where(DocumentChunk.course_id.in_(scope.course_ids))
    if scope.knowledge_base_ids:
        statement = statement.where(
            DocumentChunk.knowledge_base_id.in_(scope.knowledge_base_ids)
        )
    if scope.document_ids:
        statement = statement.where(DocumentChunk.document_id.in_(scope.document_ids))
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
            statement = statement.where(
                func.jsonb_typeof(metadata["scope_confirmation"]["knowledge_points"])
                == "object",
                func.jsonb_typeof(labels) == "array",
                labels.has_any(cast(list(query.knowledge_points), ARRAY(Text))),
            )
    return statement
