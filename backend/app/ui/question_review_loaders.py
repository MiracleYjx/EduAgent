"""T167 authenticated detail, image and report commands; UI never retains Sessions."""

from __future__ import annotations

import base64
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from html import escape
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from backend.app.api.questions import QuestionDetailDTO, get_question
from backend.app.core.config import get_settings
from backend.app.core.database import get_session_factory
from backend.app.domain.enums import DocumentStatus, UserRole
from backend.app.domain.permissions import PermissionDeniedError
from backend.app.models import SourcePage, User
from backend.app.schemas.content_validation import ManualDispositionRequest
from backend.app.schemas.image_assessment import ImageManualCheckRequest
from backend.app.services.auth_service import AuthService
from backend.app.services.content_validation_service import ContentValidationService
from backend.app.services.file_storage_service import FileStorageService
from backend.app.services.image_understanding_service import ImageUnderstandingService
from backend.app.services.knowledge_base_service import KnowledgeBaseService
from backend.app.services.question_asset_service import QuestionAssetService
from backend.app.services.question_service import QuestionService


@contextmanager
def _scope(state: Mapping[str, Any]) -> Iterator[tuple[Session, User]]:
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
            yield session, user
        except Exception:
            session.rollback()
            raise


def detail(question_id: str, state: Mapping[str, Any]) -> QuestionDetailDTO:
    with _scope(state) as (session, user):
        return get_question(
            UUID(question_id), user, QuestionService(session), session, get_settings()
        )


def history(question_id: str, state: Mapping[str, Any]):
    with _scope(state) as (session, user):
        return ContentValidationService(
            session, root=get_settings().storage_root
        ).list_validations(UUID(question_id), actor_id=user.id)


def image_assessment(question_id: str, state: Mapping[str, Any]):
    with _scope(state) as (session, user):
        return ContentValidationService(
            session, root=get_settings().storage_root
        ).get_image_assessment("question", UUID(question_id), actor_id=user.id)


def image_html(question_id: str, asset_id: str, state: Mapping[str, Any]) -> str:
    with _scope(state) as (session, user):
        root = get_settings().storage_root
        assets = QuestionAssetService(session, root=root).list_question(
            question_id, actor_id=user.id
        )
        asset = next((item for item in assets if str(item.id) == asset_id), None)
        if asset is None:
            raise ValueError("请选择本题当前题图。")
        path, file = FileStorageService(session, root=root).download(
            asset.file_id, actor_id=user.id
        )
        raw = path.read_bytes()
        return _image_html(raw, file.media_type, asset.caption or "本题原图")


def _image_html(raw: bytes, media_type: str | None, label: str) -> str:
    if media_type is None:
        if raw[:3] == b"\xff\xd8\xff":
            media_type = "image/jpeg"
        elif raw[:8] == b"\x89PNG\r\n\x1a\n":
            media_type = "image/png"
        elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            media_type = "image/webp"
        else:
            raise ValueError("原图格式无法识别，请核对已登记文件。")
    content = base64.b64encode(raw).decode("ascii")
    return f'<img alt="{escape(label)}" src="data:{escape(media_type)};base64,{content}" style="max-width:100%;height:auto;max-height:780px;object-fit:contain" />'


def check_images(question_id: str, payload: dict[str, Any], state: Mapping[str, Any]):
    command = ImageManualCheckRequest.model_validate(payload)
    with _scope(state) as (session, user):
        return ContentValidationService(
            session, root=get_settings().storage_root
        ).manual_image_check("question", UUID(question_id), command, actor_id=user.id)


async def understand_images(question_id: str, task: str, state: Mapping[str, Any]):
    with _scope(state) as (session, user):
        return await ImageUnderstandingService(
            session, root=get_settings().storage_root, settings=get_settings()
        ).understand("question", UUID(question_id), task=task, actor_id=user.id)


async def run_validation(
    question_id: str,
    state: Mapping[str, Any],
    teaching_chunk_ids: list[str] | None = None,
):
    with _scope(state) as (session, user):
        return await ContentValidationService(
            session, root=get_settings().storage_root
        ).validate_current(
            UUID(question_id),
            actor_id=user.id,
            teaching_chunk_ids=(
                [UUID(item) for item in teaching_chunk_ids]
                if teaching_chunk_ids is not None
                else None
            ),
        )


def teaching_chunks(question_id: str, state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Expose only real Ready teaching chunks in the selected question's course."""
    with _scope(state) as (session, user):
        question = QuestionService(session).get_question(
            question_id, teacher_id=user.id
        )
        knowledge = KnowledgeBaseService(session)
        chunks = []
        for document in knowledge.list_documents(
            course_id=question.course_id, teacher_id=user.id
        ):
            if document.status != DocumentStatus.READY:
                continue
            for chunk in knowledge.list_document_chunks(
                document.id, teacher_id=user.id
            ):
                chunks.append(
                    {
                        **chunk,
                        "source_file": document.original_filename,
                        "document_id": document.id,
                        "location": (chunk["metadata"] or {}).get("location"),
                    }
                )
        return chunks


def source_context(question_id: str, state: Mapping[str, Any]) -> dict[str, Any]:
    """Read current parent content and physical source page labels with teacher access."""
    with _scope(state) as (session, user):
        service = QuestionService(session)
        question = get_question(
            UUID(question_id), user, service, session, get_settings()
        )
        parents = []
        for origin in question.parent_sources:
            parent = service.get_question(
                origin["source_question_id"], teacher_id=user.id
            )
            parents.append(
                {
                    **origin,
                    "current_content": parent.content,
                    "current_status": parent.status,
                }
            )
        pages = []
        paper = question.paper_source
        if paper is not None:
            for page_id in paper["source_page_ids"]:
                page = session.get(SourcePage, UUID(str(page_id)))
                if page is None or str(page.paper_import_id) != str(
                    paper["paper_import_id"]
                ):
                    raise ValueError("来源原页已不可读，请核对实际导入记录。")
                pages.append(
                    {
                        "id": str(page.id),
                        "page_number": page.page_number,
                        "width": page.width,
                        "height": page.height,
                    }
                )
        return {"parents": parents, "pages": pages}


def source_page_html(question_id: str, page_id: str, state: Mapping[str, Any]) -> str:
    with _scope(state) as (session, user):
        question = get_question(
            UUID(question_id), user, QuestionService(session), session, get_settings()
        )
        paper = question.paper_source
        if paper is None or page_id not in {
            str(item) for item in paper["source_page_ids"]
        }:
            raise ValueError("请选择该题实际来源原页。")
        page = session.get(SourcePage, UUID(page_id))
        if page is None or str(page.paper_import_id) != str(paper["paper_import_id"]):
            raise ValueError("来源原页已不可读，请核对实际导入记录。")
        path, file = FileStorageService(
            session, root=get_settings().storage_root
        ).download("p_" + page.id.hex, actor_id=user.id)
        return _image_html(
            path.read_bytes(),
            file.media_type,
            f"原卷第 {page.page_number} 页，{page.width}×{page.height} 像素",
        )


def dispose_validation(
    question_id: str, report_id: str, payload: dict[str, Any], state: Mapping[str, Any]
):
    command = ManualDispositionRequest.model_validate(payload)
    with _scope(state) as (session, user):
        return ContentValidationService(
            session, root=get_settings().storage_root
        ).dispose_validation(
            UUID(question_id), UUID(report_id), command, actor_id=user.id
        )
