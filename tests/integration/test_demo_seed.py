"""T087：真实 PostgreSQL 隔离 schema，生产服务写入，只有 Embedding 使用替身。"""

import asyncio
from decimal import Decimal
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
from backend.app.services.content_validation_service import ContentValidationService
from backend.app.services.course_service import CourseService
from backend.app.services.exam_service import ExamService
from backend.app.services.knowledge_base_service import KnowledgeBaseService
from backend.app.services.question_service import QuestionService
from backend.app.services.submission_service import SubmissionService
from scripts.demo_seed import DemoSeedError, seed_demo
from scripts.run_retrieval_benchmark import StubHashEmbeddingProvider
from tests.postgres_helpers import isolated_postgres_engine
from tests.support.semantic_validation_doubles import StubSemanticProvider
from tests.unit.services.test_exam_service import prepare_synthetic_exam_scoring
from tests.unit.settings_helpers import build_test_settings


class DemoEmbedding(StubHashEmbeddingProvider):
    calls = 0
    fail = False

    async def embed_documents(self, documents):
        self.calls += 1
        if self.fail:
            raise EmbeddingProviderError("controlled test failure")
        return await super().embed_documents(documents)


def _approve_demo_questions(session, question_ids):
    """A real teacher selects ingested evidence; only the Provider is controlled."""
    teacher = session.scalar(select(User).where(User.username == "dev_teacher"))
    chunks = list(session.scalars(select(DocumentChunk).order_by(DocumentChunk.chunk_index)))
    assert teacher is not None and chunks
    provider = StubSemanticProvider()
    questions = QuestionService(session)
    for question_id in question_ids:
        question = session.get(Question, UUID(question_id))
        assert question is not None
        if not question.scoring_rubric:
            questions.update_question(
                question.id,
                scoring_rubric="选择 A 得 5 分，其他答案得 0 分。",
                teacher_id=teacher.id,
            )
        report = asyncio.run(ContentValidationService(session).run_validation(
            question.id,
            actor_id=teacher.id,
            teaching_chunk_ids=[chunk.id for chunk in chunks],
            provider=provider,
        ))
        assert report.outcome == "passed" and report.can_review
        assert all(item.kind == "chunk" for item in report.input_refs.evidence)
        questions.update_question_status(
            question.id, QuestionStatus.APPROVED, teacher_id=teacher.id,
        )
    assert len(provider.calls) == len(question_ids)


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
            pending = seed_demo(session, settings=settings, embedding_provider=provider)
            assert pending["status"] == "awaiting_teacher_review"
            assert pending["awaiting_question_ids"] == pending["question_ids"]
            assert pending["exam_id"] is None and pending["exam_status"] is None
            assert {item["status"] for item in pending["question_states"]} == {
                QuestionStatus.PENDING_REVIEW.value,
            }
            assert session.scalar(select(func.count()).select_from(Exam)) == 0
            assert set(session.scalars(select(Question.status))) == {
                QuestionStatus.PENDING_REVIEW,
            }
            chunk_ids = set(session.scalars(select(DocumentChunk.id)))
            password_hashes = list(session.scalars(select(User.password_hash).order_by(User.id)))
        with Session(engine) as session:
            still_pending = seed_demo(session, settings=settings, embedding_provider=provider)
            assert pending == still_pending
            assert session.scalar(select(func.count()).select_from(Question)) == 2
            assert session.scalar(select(func.count()).select_from(Exam)) == 0
            _approve_demo_questions(session, pending["question_ids"])
            scoring_pending = seed_demo(session, settings=settings, embedding_provider=provider)
            assert scoring_pending["status"] == "awaiting_exam_scoring"
            assert scoring_pending["exam_status"] == ExamStatus.DRAFT.value
            assert not scoring_pending["awaiting_question_ids"]
            assert scoring_pending["publication_checks"]
            assert seed_demo(session, settings=settings, embedding_provider=provider) == scoring_pending
            teacher = session.scalar(select(User).where(User.username == "dev_teacher"))
            prepare_synthetic_exam_scoring(session, scoring_pending["exam_id"], teacher.id)
            assert session.get(Exam, UUID(scoring_pending["exam_id"])).status == ExamStatus.DRAFT
            first = seed_demo(session, settings=settings, embedding_provider=provider)
            assert first["status"] == "ready" and not first["awaiting_question_ids"]
            assert first["exam_status"] == ExamStatus.PUBLISHED.value
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
    assert provider.calls == 1  # 再次构建不重复摄取或访问远端。
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
            assert result["status"] == "awaiting_teacher_review"
            assert session.scalar(select(func.count()).select_from(Exam)) == 0
            assert session.scalar(select(func.count()).select_from(Document)) == 1
            assert session.scalar(select(Document.status)) is DocumentStatus.READY
    assert provider.calls == 2



def test_seed_preserves_teacher_revision_and_does_not_create_exam():
    settings = build_test_settings(DEV_MODE=True)
    with isolated_postgres_engine() as engine, Session(engine) as session:
        pending = seed_demo(session, settings=settings, embedding_provider=DemoEmbedding())
        teacher = session.scalar(select(User).where(User.username == "dev_teacher"))
        question_id = pending["question_ids"][0]
        questions = QuestionService(session)
        questions.update_question_status(
            question_id,
            QuestionStatus.NEEDS_REVISION,
            teacher_id=teacher.id,
            revision_comment="Controlled teacher asks for explicit scoring criteria.",
        )
        questions.update_question(question_id, score=6, teacher_id=teacher.id)
        result = seed_demo(session, settings=settings, embedding_provider=DemoEmbedding())
        assert result["status"] == "awaiting_teacher_review"
        assert result["question_ids"] == pending["question_ids"]
        assert question_id in result["awaiting_question_ids"]
        row = session.get(Question, UUID(question_id))
        assert row.status is QuestionStatus.NEEDS_REVISION and row.score == Decimal(6)
        assert session.scalar(select(func.count()).select_from(Question)) == 2
        assert session.scalar(select(func.count()).select_from(Exam)) == 0


def test_seed_does_not_reopen_existing_closed_exam():
    settings = build_test_settings(DEV_MODE=True)
    with isolated_postgres_engine() as engine, Session(engine) as session:
        pending = seed_demo(session, settings=settings, embedding_provider=DemoEmbedding())
        _approve_demo_questions(session, pending["question_ids"])
        scoring_pending = seed_demo(session, settings=settings, embedding_provider=DemoEmbedding())
        assert scoring_pending["status"] == "awaiting_exam_scoring"
        assert scoring_pending["exam_status"] == ExamStatus.DRAFT.value
        teacher = session.scalar(select(User).where(User.username == "dev_teacher"))
        prepare_synthetic_exam_scoring(session, scoring_pending["exam_id"], teacher.id)
        ready = seed_demo(session, settings=settings, embedding_provider=DemoEmbedding())
        assert ready["status"] == "ready" and ready["exam_status"] == ExamStatus.PUBLISHED.value
        ExamService(session).update_exam_status(
            ready["exam_id"], ExamStatus.CLOSED, teacher_id=teacher.id,
        )
        with pytest.raises(DemoSeedError, match="DEMO_EXAM_NOT_OPEN"):
            seed_demo(session, settings=settings, embedding_provider=DemoEmbedding())
        assert session.get(Exam, UUID(ready["exam_id"])).status is ExamStatus.CLOSED
        assert session.scalar(select(func.count()).select_from(Exam)) == 1
