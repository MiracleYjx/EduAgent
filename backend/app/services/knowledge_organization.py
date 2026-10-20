"""Knowledge organization: rule-based suggestions, user confirmation, stable IDs.

Suggestions are derived from real parse evidence only — the heading paths and
page/paragraph locations that the material chunks actually carry. Nothing here
invents a chapter from a filename or a knowledge point from a model guess, and
a suggestion is never treated as a confirmation.

Confirmation is the only path that writes ``chapter_knowledge_points`` links and
the retrieval metadata on chunks, so unconfirmed suggestions cannot silently
become teaching scope.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Engine, delete, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from backend.app.domain.enums import DocumentPurpose
from backend.app.models.chapter import Chapter
from backend.app.models.document import Document
from backend.app.models.document_chunk import DocumentChunk
from backend.app.models.subject import (
    KnowledgePoint,
    Subject,
    chapter_knowledge_points,
)
from backend.app.schemas.personal_learning import (
    KnowledgeChapterView,
    KnowledgeConfirmRequest,
    KnowledgePointView,
    KnowledgeView,
)


class KnowledgeError(RuntimeError):
    """知识组织操作失败；携带机器可读码与真实 HTTP 语义。"""

    def __init__(self, code: str, status: int = 422) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


def _chapter_view(
    chapter: Chapter, knowledge_point_ids: Iterable[UUID]
) -> KnowledgeChapterView:
    sections = chapter.sections or []
    document_id = None
    anchors: list[str] = []
    for entry in sections:
        if not isinstance(entry, dict):
            continue
        if document_id is None and entry.get("document_id"):
            document_id = UUID(str(entry["document_id"]))
        anchors.extend(str(anchor) for anchor in entry.get("anchors") or [])
    return KnowledgeChapterView(
        id=chapter.id,
        title=chapter.title,
        parent_id=chapter.parent_id,
        confirmation_state=(
            "confirmed" if chapter.confirmed_at is not None else "suggested"
        ),
        knowledge_point_ids=list(knowledge_point_ids),
        document_id=document_id,
        anchors=anchors,
    )


class KnowledgeOrganizationService:
    """Subject knowledge tree on the verified personal space."""

    def __init__(self, space: Any, *, owned_engine: Engine | None = None) -> None:
        self.space = space
        self.sessions: sessionmaker[Session] = space.sessions
        self.profile = space.profile
        self.owned_engine = owned_engine

    # ------------------------------------------------------------------ read
    def view(self, subject_id: UUID) -> KnowledgeView:
        self.space._check_space()
        with self.sessions() as session:
            self._require_subject(session, subject_id)
            chapters = session.scalars(
                select(Chapter)
                .where(Chapter.subject_id == subject_id)
                .order_by(Chapter.title, Chapter.id)
            ).all()
            links = self._links(session, subject_id)
            points = session.scalars(
                select(KnowledgePoint)
                .where(KnowledgePoint.subject_id == subject_id)
                .order_by(KnowledgePoint.name, KnowledgePoint.id)
            ).all()
            return KnowledgeView(
                subject_id=subject_id,
                confirmed=bool(chapters) and all(chapters_confirmed(chapters)),
                chapters=[
                    _chapter_view(chapter, links.get(chapter.id, []))
                    for chapter in chapters
                ],
                knowledge_points=[
                    KnowledgePointView(
                        id=point.id,
                        name=point.name,
                        confirmation_state=point.confirmation_state,
                    )
                    for point in points
                ],
            )

    # ------------------------------------------------------------- suggestion
    def suggest(self, subject_id: UUID) -> KnowledgeView:
        """Create pending suggestions from real heading/location evidence."""

        self.space._check_space()
        with self.sessions.begin() as session:
            self._require_subject(session, subject_id)
            documents = session.scalars(
                select(Document).where(
                    Document.subject_id == subject_id,
                    Document.purpose == DocumentPurpose.KNOWLEDGE_BASE,
                )
            ).all()
            existing_titles = set(
                session.scalars(
                    select(Chapter.title).where(Chapter.subject_id == subject_id)
                )
            )
            existing_points = set(
                session.scalars(
                    select(KnowledgePoint.name).where(
                        KnowledgePoint.subject_id == subject_id
                    )
                )
            )
            for document in documents:
                rows = session.execute(
                    select(DocumentChunk.chunk_metadata).where(
                        DocumentChunk.document_id == document.id
                    )
                ).all()
                grouped: dict[str, list[str]] = {}
                deeper: set[str] = set()
                for (metadata,) in rows:
                    facts = metadata or {}
                    location = str(facts.get("location") or "")
                    if not location:
                        continue
                    heading_path = [
                        str(part) for part in facts.get("heading_path") or []
                    ]
                    title = " / ".join(heading_path) or document.original_filename
                    bucket = grouped.setdefault(title, [])
                    if location not in bucket:
                        bucket.append(location)
                    if len(heading_path) >= 2:
                        deeper.add(heading_path[-1])
                for title, anchors in grouped.items():
                    if title in existing_titles:
                        continue
                    session.add(
                        Chapter(
                            id=uuid4(),
                            subject_id=subject_id,
                            course_id=None,
                            parent_id=None,
                            title=title[:160],
                            sections=[
                                {
                                    "document_id": str(document.id),
                                    "anchors": sorted(anchors),
                                    "suggestion": "material_heading",
                                }
                            ],
                            confirmed_by=None,
                            confirmed_at=None,
                        )
                    )
                    existing_titles.add(title)
                for name in sorted(deeper):
                    if name in existing_points:
                        continue
                    session.add(
                        KnowledgePoint(
                            id=uuid4(),
                            subject_id=subject_id,
                            name=name[:160],
                            confirmation_state="pending",
                        )
                    )
                    existing_points.add(name)
        return self.view(subject_id)

    # ----------------------------------------------------------- confirmation
    def confirm(
        self, subject_id: UUID, body: KnowledgeConfirmRequest
    ) -> KnowledgeView:
        self.space._check_space()
        try:
            with self.sessions.begin() as session:
                self._require_subject(session, subject_id)
                points = self._confirm_points(session, subject_id, body)
                chapters = self._confirm_chapters(session, subject_id, body)
                self._link(session, subject_id, body, chapters, points)
                self._apply_chunk_scope(session, subject_id, chapters)
        except KnowledgeError:
            raise
        except IntegrityError as error:
            raise KnowledgeError("KNOWLEDGE_CONFLICT", 409) from error
        except DBAPIError as error:
            message = str(getattr(error, "orig", error)).upper()
            if "CYCLE" in message:
                raise KnowledgeError("KNOWLEDGE_HIERARCHY_CYCLE", 409) from error
            raise KnowledgeError("KNOWLEDGE_CONFLICT", 409) from error
        return self.view(subject_id)

    def _confirm_points(
        self,
        session: Session,
        subject_id: UUID,
        body: KnowledgeConfirmRequest,
    ) -> dict[UUID, KnowledgePoint]:
        """Confirm/modify points; ``None`` id creates a new stable point."""

        points: dict[UUID, KnowledgePoint] = {}
        for item in body.knowledge_points:
            if item.id is None:
                point = KnowledgePoint(
                    id=uuid4(),
                    subject_id=subject_id,
                    name=item.name,
                    confirmation_state="confirmed",
                )
                session.add(point)
                session.flush()
            else:
                existing_point = session.get(KnowledgePoint, item.id)
                if existing_point is None or existing_point.subject_id != subject_id:
                    raise KnowledgeError("KNOWLEDGE_POINT_NOT_FOUND", 404)
                existing_point.name = item.name
                existing_point.confirmation_state = "confirmed"
                session.flush()
                point = existing_point
            points[point.id] = point
        return points

    def _confirm_chapters(
        self,
        session: Session,
        subject_id: UUID,
        body: KnowledgeConfirmRequest,
    ) -> list[tuple[Any, Chapter]]:
        """Confirm/modify chapters in payload order; parents must already exist."""

        result: list[tuple[Any, Chapter]] = []
        for item in body.chapters:
            anchors = [str(anchor) for anchor in item.anchors]
            sections = (
                [
                    {
                        "document_id": str(item.document_id),
                        "anchors": anchors,
                        "suggestion": "user_confirmed",
                    }
                ]
                if item.document_id is not None
                else []
            )
            if item.id is None:
                chapter = Chapter(
                    id=uuid4(),
                    subject_id=subject_id,
                    course_id=None,
                    parent_id=None,
                    title=item.title,
                    sections=sections,
                    confirmed_by=None,
                    confirmed_at=datetime.now(UTC),
                )
                session.add(chapter)
                session.flush()
            else:
                existing_chapter = session.get(Chapter, item.id)
                if existing_chapter is None or existing_chapter.subject_id != subject_id:
                    raise KnowledgeError("KNOWLEDGE_CHAPTER_NOT_FOUND", 404)
                existing_chapter.title = item.title
                if item.document_id is not None or anchors:
                    existing_chapter.sections = sections
                existing_chapter.confirmed_at = datetime.now(UTC)
                session.flush()
                chapter = existing_chapter
            if item.parent_id is not None:
                parent = session.get(Chapter, item.parent_id)
                if parent is None or parent.subject_id != subject_id:
                    raise KnowledgeError("KNOWLEDGE_PARENT_NOT_FOUND", 404)
                # 先给出确定性的循环错误；数据库触发器仍是最后一道守卫。
                cursor: Chapter | None = parent
                seen: set[UUID] = set()
                while cursor is not None and cursor.id not in seen:
                    seen.add(cursor.id)
                    if cursor.id == chapter.id:
                        raise KnowledgeError("KNOWLEDGE_HIERARCHY_CYCLE", 409)
                    cursor = (
                        session.get(Chapter, cursor.parent_id)
                        if cursor.parent_id is not None
                        else None
                    )
                chapter.parent_id = parent.id
            result.append((item, chapter))
        session.flush()
        return result

    def _link(
        self,
        session: Session,
        subject_id: UUID,
        body: KnowledgeConfirmRequest,
        chapters: list[tuple[Any, Chapter]],
        points: dict[UUID, KnowledgePoint],
    ) -> None:
        known = set(
            session.scalars(
                select(KnowledgePoint.id).where(
                    KnowledgePoint.subject_id == subject_id
                )
            )
        )
        for item, chapter in chapters:
            session.execute(
                delete(chapter_knowledge_points).where(
                    chapter_knowledge_points.c.chapter_id == chapter.id
                )
            )
            for point_id in item.knowledge_point_ids:
                if point_id not in points and point_id not in known:
                    raise KnowledgeError("KNOWLEDGE_POINT_NOT_FOUND", 404)
                session.execute(
                    chapter_knowledge_points.insert().values(
                        chapter_id=chapter.id,
                        knowledge_point_id=point_id,
                        subject_id=subject_id,
                    )
                )
        session.flush()
        del body

    def _apply_chunk_scope(
        self,
        session: Session,
        subject_id: UUID,
        chapters: list[tuple[Any, Chapter]],
    ) -> None:
        """Only confirmed anchors grant retrieval scope to real chunks."""

        links = self._links(session, subject_id)
        for _item, chapter in chapters:
            sections = chapter.sections or []
            document_ids = {
                UUID(str(entry["document_id"]))
                for entry in sections
                if isinstance(entry, dict) and entry.get("document_id")
            }
            anchors = {
                str(anchor)
                for entry in sections
                if isinstance(entry, dict)
                for anchor in entry.get("anchors") or []
            }
            if not document_ids or not anchors:
                continue
            point_ids = [str(value) for value in links.get(chapter.id, [])]
            chunks = session.scalars(
                select(DocumentChunk).where(
                    DocumentChunk.subject_id == subject_id,
                    DocumentChunk.document_id.in_(document_ids),
                )
            ).all()
            for chunk in chunks:
                metadata = dict(chunk.chunk_metadata or {})
                if str(metadata.get("location") or "") not in anchors:
                    continue
                chunk.chapter_id = chapter.id
                metadata["chapter_id"] = str(chapter.id)
                metadata["knowledge_points"] = point_ids
                metadata["scope_confirmation"] = {
                    "knowledge_points": {value: "confirmed" for value in point_ids}
                }
                chunk.chunk_metadata = metadata
                flag_modified(chunk, "chunk_metadata")
        session.flush()

    # ---------------------------------------------------------------- helpers
    def _require_subject(self, session: Session, subject_id: UUID) -> Subject:
        row = session.get(Subject, subject_id)
        if row is None:
            raise KnowledgeError("SUBJECT_NOT_FOUND", 404)
        return row

    def _links(self, session: Session, subject_id: UUID) -> dict[UUID, list[UUID]]:
        rows = session.execute(
            select(
                chapter_knowledge_points.c.chapter_id,
                chapter_knowledge_points.c.knowledge_point_id,
            ).where(chapter_knowledge_points.c.subject_id == subject_id)
        ).all()
        links: dict[UUID, list[UUID]] = {}
        for chapter_id, point_id in rows:
            links.setdefault(chapter_id, []).append(point_id)
        return links

    def close(self) -> None:
        if self.owned_engine is not None:
            self.owned_engine.dispose()


def chapters_confirmed(chapters: Iterable[Chapter]) -> list[bool]:
    """Return the confirmation fact per chapter; never inferred from a title."""

    return [chapter.confirmed_at is not None for chapter in chapters]


__all__ = [
    "KnowledgeError",
    "KnowledgeOrganizationService",
    "chapters_confirmed",
]
