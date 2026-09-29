"""T087：真实 PostgreSQL 隔离 schema，生产服务写入，只有 Embedding 使用替身。"""

from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.ai.embedding.base import EmbeddingProviderError
from backend.app.domain.enums import DocumentStatus, ExamStatus, QuestionStatus
from backend.app.models import (
    Course,
    Document,
    DocumentChunk,
    Exam,
    KnowledgeBase,
    Question,
    User,
)
from backend.app.services.course_service import CourseService
from backend.app.services.exam_service import ExamService
from backend.app.services.knowledge_base_service import KnowledgeBaseService
from backend.app.services.question_service import QuestionService
from backend.app.services.submission_service import SubmissionService
from scripts.demo_seed import DemoSeedError, seed_demo
from scripts.run_retrieval_benchmark import StubHashEmbeddingProvider
from tests.postgres_helpers import isolated_postgres_engine
from tests.unit.settings_helpers import build_test_settings


class DemoEmbedding(StubHashEmbeddingProvider):
    calls = 0
    fail = False

    async def embed_documents(self, documents):
        self.calls += 1
        if self.fail:
            raise EmbeddingProviderError("controlled test failure")
        return await super().embed_documents(documents)


def test_seed_twice_reuses_records_and_writes_through_real_services(monkeypatch):
    calls = []
    for service, method in (
        (CourseService, "create_course"),
        (KnowledgeBaseService, "create_knowledge_base"),
        (KnowledgeBaseService, "ingest_document"),
        (QuestionService, "create_question"),
        (ExamService, "create_exam"),
    ):
        original = getattr(service, method)

        def observe(self, *args, _original=original, _method=method, **kwargs):
            calls.append(_method)
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(service, method, observe)

    provider = DemoEmbedding()
    settings = build_test_settings(DEV_MODE=True)
    with isolated_postgres_engine() as engine:
        with Session(engine) as session:
            first = seed_demo(session, settings=settings, embedding_provider=provider)
            chunk_ids = set(session.scalars(select(DocumentChunk.id)))
            password_hashes = list(session.scalars(select(User.password_hash).order_by(User.id)))
        with Session(engine) as session:
            second = seed_demo(session, settings=settings, embedding_provider=provider)
            assert first == second
            for model, count in ((User, 3), (Course, 1), (KnowledgeBase, 1), (Document, 1),
                                 (Question, 2), (Exam, 1)):
                assert session.scalar(select(func.count()).select_from(model)) == count
            assert chunk_ids == set(session.scalars(select(DocumentChunk.id)))
            assert password_hashes == list(session.scalars(
                select(User.password_hash).order_by(User.id),
            ))
            document = session.get(Document, UUID(first["document_id"]))
            assert document.status is DocumentStatus.READY
            chunks = session.scalars(select(DocumentChunk)).all()
            assert chunks and all(row.embedding is not None and row.search_vector for row in chunks)
            assert set(session.scalars(select(func.vector_dims(DocumentChunk.embedding)))) == {1024}
            assert set(session.scalars(select(Question.status))) == {QuestionStatus.APPROVED}
            exam = session.get(Exam, UUID(first["exam_id"]))
            assert exam.status is ExamStatus.PUBLISHED
            assert {str(item.id) for item in exam.questions} == set(first["question_ids"])
            student = session.scalar(select(User).where(User.username == "dev_student"))
            assert first["exam_id"] in {
                item.id for item in SubmissionService(session).list_available_exams(student.id)
            }
    assert provider.calls == 1  # 再次灌入不重复摄取或调用远端。
    assert calls == ["create_course", "create_knowledge_base", "ingest_document",
                     "create_question", "create_question", "create_exam"]


def test_seed_refuses_disabled_dev_mode_without_creating_data():
    with isolated_postgres_engine() as engine, Session(engine) as session:
        with pytest.raises(DemoSeedError, match="DEMO_DEV_MODE_REQUIRED"):
            seed_demo(session, settings=build_test_settings(DEV_MODE=False))
        assert session.scalar(select(func.count()).select_from(User)) == 0
        assert session.scalar(select(func.count()).select_from(Course)) == 0


def test_seed_failed_ingestion_is_visible_and_retry_reuses_document():
    provider = DemoEmbedding()
    provider.fail = True
    settings = build_test_settings(DEV_MODE=True)
    with isolated_postgres_engine() as engine:
        with Session(engine) as session:
            with pytest.raises(DemoSeedError, match="DEMO_INGESTION_FAILED"):
                seed_demo(session, settings=settings, embedding_provider=provider)
            document = session.scalar(select(Document))
            document_id = document.id
            assert document.status is DocumentStatus.FAILED and document.retryable
            assert session.scalar(select(func.count()).select_from(DocumentChunk)) == 0
            assert session.scalar(select(func.count()).select_from(Question)) == 0
            assert session.scalar(select(func.count()).select_from(Exam)) == 0
        provider.fail = False
        with Session(engine) as session:
            result = seed_demo(session, settings=settings, embedding_provider=provider)
            assert result["document_id"] == str(document_id)
            assert session.scalar(select(func.count()).select_from(Document)) == 1
            assert session.scalar(select(Document.status)) is DocumentStatus.READY
    assert provider.calls == 2
