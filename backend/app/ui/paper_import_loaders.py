"""Authenticated UI loaders; sessions and durable records belong to services."""

from __future__ import annotations

import base64
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from html import escape
from pathlib import Path
from typing import Any
from uuid import UUID

from backend.app.core.config import get_settings
from backend.app.core.database import get_session_factory
from backend.app.domain.enums import UserRole
from backend.app.domain.permissions import PermissionDeniedError
from backend.app.schemas.paper_import import CommitRequest, CorrectionPayload
from backend.app.schemas.question_assets import AssetLinkRequest
from backend.app.services.auth_service import AuthService
from backend.app.services.course_service import CourseService
from backend.app.services.paper_import_service import PaperImportRunner
from backend.app.services.question_correction_service import QuestionCorrectionService


@contextmanager
def _scope(
    state: Mapping[str, Any],
) -> Iterator[tuple[QuestionCorrectionService, UUID]]:
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
                QuestionCorrectionService(session, root=settings.storage_root),
                user.id,
            )
        except Exception:
            session.rollback()
            raise


def courses(state):
    with _scope(state) as (service, actor):
        return [
            (course.name, course.id)
            for course in CourseService(service.session).list_courses(teacher_id=actor)
        ]


def list_imports(course_id: str, state):
    with _scope(state) as (service, actor):
        return service.list(UUID(course_id), actor_id=actor)


def load(identity: str, state):
    with _scope(state) as (service, actor):
        return service.get(UUID(identity), actor_id=actor)


def upload(course_id: str, filename: str, content: bytes, state):
    with _scope(state) as (service, actor):
        return service.upload(
            UUID(course_id), filename=filename, content=content, actor_id=actor
        )


def run(identity: str, state) -> None:
    with _scope(state) as (service, actor):
        service.get_record(UUID(identity), actor)
    PaperImportRunner(get_session_factory(), settings=get_settings()).run(
        UUID(identity)
    )


def patch(identity: str, question_id: str, payload: dict[str, Any], state):
    with _scope(state) as (service, actor):
        return service.patch(
            UUID(identity),
            UUID(question_id),
            CorrectionPayload.model_validate(payload),
            actor_id=actor,
        )


def commit(identity: str, ids: list[str], state):
    request = CommitRequest(question_ids=[UUID(identity) for identity in ids])
    with _scope(state) as (service, actor):
        return service.commit(UUID(identity), request.question_ids, actor_id=actor)


def add_asset(question_id: str, payload: dict[str, Any], state):
    with _scope(state) as (service, actor):
        return service.assets.create_staged(
            UUID(question_id), AssetLinkRequest.model_validate(payload), actor_id=actor
        )


def remove_asset(question_id: str, asset_id: str, state) -> None:
    with _scope(state) as (service, actor):
        service.assets.remove_staged(UUID(question_id), UUID(asset_id), actor_id=actor)


def _image_html(path: Path, label: str) -> str:
    content = path.read_bytes()
    encoded = base64.b64encode(content).decode("ascii")
    mime = "image/jpeg" if content[:3] == b"\xff\xd8\xff" else "image/png"
    return f'<img alt="{escape(label)}" src="data:{mime};base64,{encoded}" style="max-width:100%;height:auto;max-height:780px;object-fit:contain" />'


def page_image(identity: str, page_id: str, state) -> str:
    with _scope(state) as (service, actor):
        paper = service.get(UUID(identity), actor_id=actor)
        page = next((p for p in paper.pages if str(p.id) == page_id), None)
        if page is None:
            raise ValueError("请选择当前导入的原页。")
        path, _ = service.files.download(page.file_id, actor_id=actor)
        return _image_html(
            path, f"原卷第 {page.page_number} 页，{page.width}×{page.height} 像素"
        )


def asset_image(question_id: str, asset_id: str, state) -> str:
    with _scope(state) as (service, actor):
        assets = service.assets.list_staged(UUID(question_id), actor_id=actor)
        asset = next((a for a in assets if str(a.id) == asset_id), None)
        if asset is None:
            raise ValueError("请选择本题的题图。")
        path, _ = service.files.download(asset.file_id, actor_id=actor)
        return _image_html(path, asset.caption or "本题原图")
