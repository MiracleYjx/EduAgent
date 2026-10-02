"""T150 real PostgreSQL aliases, historical snapshots and common physical lock."""
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from backend.app.domain.enums import DocumentStatus, UserRole
from backend.app.models import Course, Document, KnowledgeBase, Role, User
from backend.app.services.file_storage_service import FileStorageService
from backend.app.services.storage_migration_service import StorageMigrationService
from tests.postgres_helpers import isolated_postgres_engine


def test_postgres_shared_migration_commits_and_restart_reads(tmp_path):
    with isolated_postgres_engine() as engine, Session(engine, expire_on_commit=False) as session:
        teacher = User(username="migration_teacher", email="mt@example.com", password_hash="unused")
        teacher.roles.append(Role(name=UserRole.TEACHER))
        admin = User(username="migration_admin", email="ma@example.com", password_hash="unused")
        admin.roles.append(Role(name=UserRole.ADMIN))
        session.add_all([teacher, admin])
        session.flush()
        course = Course(name="真实迁移", created_by=teacher.id)
        session.add(course)
        session.flush()
        kb = KnowledgeBase(name="资料", course_id=course.id)
        session.add(kb)
        session.flush()
        source = tmp_path / "real-original.txt"
        source.write_bytes(b"real shared original")
        docs = [Document(id=uuid4(), course_id=course.id, knowledge_base_id=kb.id, uploaded_by=teacher.id,
                         original_filename="same.txt", file_format="txt", storage_path=str(source),
                         status=DocumentStatus.READY) for _ in range(2)]
        session.add_all(docs)
        session.commit()
        ids = [d.id for d in docs]
        actor_id = admin.id
        root = tmp_path / "persistent"
        report = StorageMigrationService(session, root=root).run(actor_id=actor_id)
        assert report.counts["migrated"] == 2
        session.close()
        with Session(engine) as restarted:
            rows = list(restarted.scalars(select(Document).where(Document.id.in_(ids))))
            assert len(rows) == 2 and len({r.storage_path for r in rows}) == 1
            assert all(r.status == DocumentStatus.READY for r in rows)
            assert all(FileStorageService(restarted, root=root).read_document(r) == source.read_bytes() for r in rows)
            assert StorageMigrationService(restarted, root=root).run(actor_id=actor_id).counts["already_migrated"] == 2


def test_legacy_and_canonical_lock_are_same_physical_key(tmp_path):
    with isolated_postgres_engine() as engine, Session(engine) as migration, Session(engine) as writer:
        root = tmp_path / "persistent"
        first = FileStorageService(migration, root=root)
        second = FileStorageService(writer, root=root)
        original = root / "uploads/shared/original.txt"
        first.lock_path(original)
        writer.execute(text("SET LOCAL lock_timeout = '100ms'"))
        with pytest.raises(OperationalError) as err:
            second.lock_locator("uploads/shared/original.txt")
        assert err.value.orig.sqlstate == "55P03"
        writer.rollback()
        migration.rollback()
        second.lock_locator("uploads/shared/original.txt")
        writer.rollback()
        assert not Path(original).exists()
