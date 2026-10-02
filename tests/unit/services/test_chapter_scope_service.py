"""T161 trusted scope writes and directory invalidation; TCR §16."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.domain.enums import DocumentStatus, UserRole
from backend.app.models import (
    Base,
    Course,
    Document,
    DocumentChunk,
    KnowledgeBase,
    Role,
    User,
)
from backend.app.services.knowledge_base_service import (
    KnowledgeBasePermissionError,
    KnowledgeBaseService,
    KnowledgeBaseValidationError,
)


@pytest.fixture
def scope_rows():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        teacher = User(
            username="scope_teacher",
            email="scope@example.invalid",
            password_hash="unused",
            roles=[Role(name=UserRole.TEACHER)],
        )
        session.add(teacher)
        session.flush()
        course = Course(name="scope", created_by=teacher.id)
        session.add(course)
        session.flush()
        kb = KnowledgeBase(name="scope", course_id=course.id)
        session.add(kb)
        session.flush()
        document = Document(
            course_id=course.id,
            knowledge_base_id=kb.id,
            uploaded_by=teacher.id,
            original_filename="scope.md",
            file_format="md",
            status=DocumentStatus.READY,
        )
        session.add(document)
        session.flush()
        chunk = DocumentChunk(
            document_id=document.id,
            course_id=course.id,
            knowledge_base_id=kb.id,
            chunk_index=0,
            content="完整章内内容",
            embedding=[0.0] * 1024,
            chunk_metadata={
                "original_filename": "scope.md",
                "knowledge_points": ["legacy"],
                "other": {"v": 1},
            },
        )
        session.add(chunk)
        session.commit()
        yield session, teacher, course, kb, document, chunk
    engine.dispose()


def test_real_teacher_confirmation_and_independent_unknown_dimensions(scope_rows):
    session, teacher, course, _kb, document, chunk = scope_rows
    service = KnowledgeBaseService(session)
    assert callable(getattr(service, "create_chapter", None)), (
        "T161 missing chapter production entry"
    )
    from backend.app.schemas.chapter_scope import ChapterWrite, ChunkScopeUpdate

    chapter = service.create_chapter(
        course.id,
        ChapterWrite(title="第一章", sections=[{"section_order": 1, "title": "概念"}]),
        teacher.id,
    )
    another = service.create_chapter(
        course.id, ChapterWrite(title="第一章"), teacher.id
    )
    assert chapter["id"] != another["id"]
    before = (chunk.id, chunk.content, list(chunk.embedding))
    result = service.confirm_chunk_scope(
        chunk.id,
        ChunkScopeUpdate(chapter_id=chapter["id"], section_order=1),
        teacher.id,
    )
    assert result["metadata"]["knowledge_points"] == ["legacy"]
    assert result["metadata"]["scope_confirmation"]["location"]["confirmed_by"] == str(
        teacher.id
    )
    assert "knowledge_points" not in result["metadata"]["scope_confirmation"]
    result = service.confirm_chunk_scope(
        chunk.id, ChunkScopeUpdate(knowledge_points=[" A ", "A", "a"]), teacher.id
    )
    assert result["metadata"]["knowledge_points"] == ["A", "a"]
    assert result["chapter_id"] == chapter["id"]
    assert (chunk.id, chunk.content, list(chunk.embedding)) == before
    session.expire_all()
    assert service.list_document_chunks(document.id, teacher.id)[0]["metadata"][
        "other"
    ] == {"v": 1}
    result = service.confirm_chunk_scope(
        chunk.id, ChunkScopeUpdate(knowledge_points=[]), teacher.id
    )
    assert result["metadata"]["knowledge_points"] == []
    assert "knowledge_points" in result["metadata"]["scope_confirmation"]
    result = service.confirm_chunk_scope(
        chunk.id, ChunkScopeUpdate(knowledge_points=None), teacher.id
    )
    assert result["metadata"]["knowledge_points"] is None
    assert "knowledge_points" not in result["metadata"]["scope_confirmation"]


def test_cross_course_directory_and_identity_are_rejected(scope_rows):
    session, teacher, course, _kb, document, chunk = scope_rows
    service = KnowledgeBaseService(session)
    assert callable(getattr(service, "confirm_chunk_scope", None)), (
        "T161 missing teacher confirmation entry"
    )
    from backend.app.schemas.chapter_scope import ChapterWrite, ChunkScopeUpdate

    other = Course(name="other", created_by=teacher.id)
    session.add(other)
    session.commit()
    chapter = service.create_chapter(other.id, ChapterWrite(title="other"), teacher.id)
    with pytest.raises(KnowledgeBaseValidationError):
        service.confirm_chunk_scope(
            chunk.id, ChunkScopeUpdate(chapter_id=chapter["id"]), teacher.id
        )
    local = service.create_chapter(
        course.id,
        ChapterWrite(title="local", sections=[{"section_order": 1, "title": "one"}]),
        teacher.id,
    )
    with pytest.raises(KnowledgeBaseValidationError):
        service.confirm_chunk_scope(
            chunk.id,
            ChunkScopeUpdate(chapter_id=local["id"], section_order=2),
            teacher.id,
        )
    with pytest.raises(KnowledgeBasePermissionError):
        service.create_chapter(course.id, ChapterWrite(title="forged"), None)
    stranger = User(
        username="stranger", email="stranger@test.invalid", password_hash="unused"
    )
    session.add(stranger)
    session.commit()
    with pytest.raises(KnowledgeBasePermissionError):
        service.create_chapter(course.id, ChapterWrite(title="forged"), stranger.id)
    assert (
        service.list_document_chunks(document.id, teacher.id)[0]["metadata"].get(
            "scope_confirmation"
        )
        is None
    )


def test_directory_change_invalidates_location_without_erasing_tag_evidence(scope_rows):
    session, teacher, course, _kb, _document, chunk = scope_rows
    service = KnowledgeBaseService(session)
    assert callable(getattr(service, "update_chapter", None)), (
        "T161 missing directory maintenance entry"
    )
    from backend.app.schemas.chapter_scope import ChapterWrite, ChunkScopeUpdate

    chapter = service.create_chapter(
        course.id,
        ChapterWrite(
            title="chapter", sections=[{"section_order": 1, "title": "first"}]
        ),
        teacher.id,
    )
    service.confirm_chunk_scope(
        chunk.id,
        ChunkScopeUpdate(
            chapter_id=chapter["id"], section_order=1, knowledge_points=["tag"]
        ),
        teacher.id,
    )
    tag_confirmation = dict(
        chunk.chunk_metadata["scope_confirmation"]["knowledge_points"]
    )
    service.update_chapter(
        chapter["id"],
        ChapterWrite(
            title="renamed", sections=[{"section_order": 1, "title": "first"}]
        ),
        teacher.id,
    )
    assert chunk.chapter_id is not None
    service.update_chapter(
        chapter["id"],
        ChapterWrite(
            title="renamed", sections=[{"section_order": 1, "title": "second"}]
        ),
        teacher.id,
    )
    session.refresh(chunk)
    assert chunk.chapter_id is None and chunk.section_order is None
    assert "location" not in chunk.chunk_metadata["scope_confirmation"]
    assert (
        chunk.chunk_metadata["scope_confirmation"]["knowledge_points"]
        == tag_confirmation
    )
    assert chunk.chunk_metadata["knowledge_points"] == ["tag"]


def test_teacher_explicit_title_correction_preserves_mapping_but_cannot_reorder(
    scope_rows,
):
    session, teacher, course, _kb, _document, chunk = scope_rows
    from backend.app.schemas.chapter_scope import ChapterWrite, ChunkScopeUpdate

    service = KnowledgeBaseService(session)
    chapter = service.create_chapter(
        course.id,
        ChapterWrite(title="chapter", sections=[{"section_order": 1, "title": "typo"}]),
        teacher.id,
    )
    service.confirm_chunk_scope(
        chunk.id,
        ChunkScopeUpdate(chapter_id=chapter["id"], section_order=1),
        teacher.id,
    )
    confirmation = dict(chunk.chunk_metadata["scope_confirmation"]["location"])
    service.update_chapter(
        chapter["id"],
        ChapterWrite(
            title="chapter", sections=[{"section_order": 1, "title": "corrected"}]
        ),
        teacher.id,
        preserve_section_meaning=True,
    )
    session.refresh(chunk)
    assert str(chunk.chapter_id) == chapter["id"] and chunk.section_order == 1
    assert chunk.chunk_metadata["scope_confirmation"]["location"] == confirmation
    with pytest.raises(KnowledgeBaseValidationError):
        service.update_chapter(
            chapter["id"],
            ChapterWrite(title="chapter", sections=[]),
            teacher.id,
            preserve_section_meaning=True,
        )
    session.refresh(chunk)
    assert str(chunk.chapter_id) == chapter["id"] and chunk.section_order == 1
