"""Subjects belong to the selected environment database, never a browser/user."""

import asyncio
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.core.environment import EnvironmentSelectionError, environment_path
from backend.app.core.maintenance import ensure_storage_writable
from backend.app.models.subject import Subject
from backend.app.schemas.personal_learning import (
    SubjectCreate,
    SubjectList,
    SubjectPatch,
    SubjectView,
)
from backend.app.schemas.settings import EnvironmentProfile


class SubjectError(RuntimeError):
    def __init__(self, code: str, status: int = 503):
        self.code = code
        self.status = status
        super().__init__(code)


class SubjectService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        storage_root: Path,
        owned_engine: Engine | None = None,
    ):
        self.sessions = sessions
        self.storage_root = storage_root
        self.owned_engine = owned_engine
        self.profile: EnvironmentProfile | None = None
        self.local_ocr_tasks: set[asyncio.Task] = set()
        #: 生产装配点；测试可直接注入显式 SubjectivePort 或快照服务。
        self.config: Any = None
        self.runtime_snapshots: Any = None
        self.subjective_port: Any = None
        self.grading_operations_required = False

    def _check_space(self) -> None:
        if self.profile is not None:
            try:
                if (
                    environment_path(self.profile, "storage") != self.storage_root
                    or not self.storage_root.is_dir()
                ):
                    raise SubjectError("LEARNING_STORAGE_UNAVAILABLE")
            except (EnvironmentSelectionError, ValueError):
                raise SubjectError("LEARNING_STORAGE_UNAVAILABLE") from None

    def list(self) -> SubjectList:
        self._check_space()
        with self.sessions() as session:
            return SubjectList(
                subjects=[
                    SubjectView.model_validate(row)
                    for row in session.scalars(
                        select(Subject).order_by(Subject.created_at, Subject.id)
                    )
                ]
            )

    def get(self, subject_id: UUID) -> SubjectView:
        self._check_space()
        with self.sessions() as session:
            row = session.get(Subject, subject_id)
            if row is None:
                raise SubjectError("SUBJECT_NOT_FOUND", 404)
            return SubjectView.model_validate(row)

    def create(self, body: SubjectCreate) -> SubjectView:
        self._check_space()
        ensure_storage_writable(self.storage_root)
        with self.sessions.begin() as session:
            row = Subject(**body.model_dump())
            session.add(row)
            session.flush()
            return SubjectView.model_validate(row)

    def patch(self, subject_id: UUID, body: SubjectPatch) -> SubjectView:
        self._check_space()
        ensure_storage_writable(self.storage_root)
        with self.sessions.begin() as session:
            row = session.get(Subject, subject_id, with_for_update=True)
            if row is None:
                raise SubjectError("SUBJECT_NOT_FOUND", 404)
            for key, value in body.model_dump(exclude_unset=True).items():
                setattr(row, key, value)
            session.flush()
            return SubjectView.model_validate(row)

    async def wait_for_local_work(self) -> None:
        if self.local_ocr_tasks:
            await asyncio.gather(*tuple(self.local_ocr_tasks), return_exceptions=True)

    def close(self) -> None:
        if self.owned_engine is not None:
            self.owned_engine.dispose()
        if self.runtime_snapshots is not None:
            self.runtime_snapshots.close()
