"""Authenticated Chapter/Chunk operations use the KnowledgeBase service boundary."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from backend.app.core.config import get_settings
from backend.app.core.database import get_session_factory
from backend.app.domain.enums import UserRole
from backend.app.domain.permissions import PermissionDeniedError
from backend.app.schemas.chapter_scope import (
    ChapterWrite,
    ChunkScopeUpdate,
    SourceSplit,
)
from backend.app.services.auth_service import AuthService
from backend.app.services.knowledge_base_service import KnowledgeBaseService


@contextmanager
def _scope(state: Mapping[str, Any]) -> Iterator[tuple[KnowledgeBaseService, UUID]]:
    settings = get_settings()
    with get_session_factory()() as session:
        user = AuthService(
            session, secret_key=settings.JWT_SECRET_KEY.get_secret_value()
        ).get_current_user(str(state.get("access_token") or ""))
        if str(user.id) != state.get("user_id") or UserRole.TEACHER not in {
            role.name for role in user.roles
        }:
            raise PermissionDeniedError("请使用当前教师账号重新登录。")
        try:
            yield (
                KnowledgeBaseService(session, storage_root=settings.storage_root),
                user.id,
            )
        except Exception:
            session.rollback()
            raise


def list_context(course_id: str, knowledge_base_id: str, state):
    with _scope(state) as (service, actor):
        chapters = service.list_chapters(course_id, actor)
        documents = (
            service.list_documents(knowledge_base_id, teacher_id=actor)
            if knowledge_base_id
            else []
        )
        if any(document.course_id != course_id for document in documents):
            raise PermissionDeniedError("知识库不属于当前课程。")
        return chapters, documents


def save_chapter(
    course_id: str,
    chapter_id: str | None,
    payload: dict[str, Any],
    state,
    *,
    preserve_section_meaning: bool = False,
):
    request = ChapterWrite.model_validate(payload)
    with _scope(state) as (service, actor):
        if chapter_id:
            chapters = service.list_chapters(course_id, actor)
            if not any(chapter["id"] == chapter_id for chapter in chapters):
                raise PermissionDeniedError("章节不属于当前课程。")
            return service.update_chapter(
                chapter_id,
                request,
                actor,
                preserve_section_meaning=preserve_section_meaning,
            )
        if preserve_section_meaning:
            raise ValueError("仅修正标题需先选择已有章节。")
        return service.create_chapter(course_id, request, actor)


def remove_chapter(course_id: str, chapter_id: str, state):
    with _scope(state) as (service, actor):
        if not any(
            chapter["id"] == chapter_id
            for chapter in service.list_chapters(course_id, actor)
        ):
            raise PermissionDeniedError("章节不属于当前课程。")
        service.delete_chapter(chapter_id, actor)


def chunks(course_id: str, knowledge_base_id: str, document_id: str, state):
    with _scope(state) as (service, actor):
        document = service.get_document(document_id, teacher_id=actor)
        if (
            document.course_id != course_id
            or document.knowledge_base_id != knowledge_base_id
        ):
            raise PermissionDeniedError("资料不属于当前课程和知识库。")
        return service.list_document_chunks(document_id, actor)


def confirm(
    course_id: str,
    knowledge_base_id: str,
    document_id: str,
    chunk_id: str,
    payload,
    state,
):
    request = ChunkScopeUpdate.model_validate(payload)
    with _scope(state) as (service, actor):
        document = service.get_document(document_id, teacher_id=actor)
        if (
            document.course_id != course_id
            or document.knowledge_base_id != knowledge_base_id
        ):
            raise PermissionDeniedError("资料不属于当前课程和知识库。")
        if not any(
            chunk["id"] == chunk_id
            for chunk in service.list_document_chunks(document_id, actor)
        ):
            raise PermissionDeniedError("片段不属于当前资料。")
        return service.confirm_chunk_scope(chunk_id, request, actor)


def sources(course_id: str, knowledge_base_id: str, document_id: str, state):
    with _scope(state) as (service, actor):
        document = service.get_document(document_id, teacher_id=actor)
        if (
            document.course_id != course_id
            or document.knowledge_base_id != knowledge_base_id
        ):
            raise PermissionDeniedError("资料不属于当前课程和知识库。")
        return service.document_source_sections(document_id, actor)


def resplit(course_id: str, knowledge_base_id: str, document_id: str, payload, state):
    requests = [SourceSplit.model_validate(value) for value in payload]
    with _scope(state) as (service, actor):
        document = service.get_document(document_id, teacher_id=actor)
        if (
            document.course_id != course_id
            or document.knowledge_base_id != knowledge_base_id
        ):
            raise PermissionDeniedError("资料不属于当前课程和知识库。")
        return service.resplit_document(document_id, requests, actor)
