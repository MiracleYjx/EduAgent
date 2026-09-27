"""P4B.3.1 TCR：真实 PostgreSQL 约束与删除行为，快照不依赖当前 Chunk。"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.domain.enums import QuestionType
from backend.app.models import (
    Course,
    Document,
    DocumentChunk,
    KnowledgeBase,
    Question,
    QuestionGenerationMetadata,
    QuestionRevisionComment,
    QuestionSourceChunk,
    User,
)
from tests.postgres_helpers import isolated_postgres_engine


def _seed(session: Session) -> tuple[UUID, UUID, UUID, UUID, UUID, UUID, UUID]:
    teacher = User(
        username=f"source-{uuid4().hex}",
        email=f"{uuid4().hex}@example.com",
        password_hash="hashed-password",
    )
    course = Course(name="来源持久化课程", creator=teacher)
    question = Question(
        course=course,
        creator=teacher,
        type=QuestionType.SHORT_ANSWER,
        content="什么是变量？",
        score=Decimal("10.00"),
    )
    knowledge_base = KnowledgeBase(name="课程资料", course=course)
    session.add_all([question, knowledge_base])
    session.flush()
    document = Document(
        course=course,
        knowledge_base=knowledge_base,
        uploader=teacher,
        original_filename="lesson.md",
        file_format="markdown",
    )
    session.add(document)
    session.flush()
    chunk = DocumentChunk(
        document_id=document.id,
        course_id=course.id,
        knowledge_base_id=knowledge_base.id,
        chunk_index=0,
        content="变量保存数据",
        chunk_metadata={},
    )
    session.add(chunk)
    session.flush()
    source = QuestionSourceChunk(
        question_id=question.id,
        chunk_id=chunk.id,
        live_chunk_id=chunk.id,
        document_id=document.id,
        course_id=course.id,
        source_order=0,
        content_snapshot="变量保存数据",
        source_file="lesson.md",
        chunk_index=0,
        retrieval_rank=1,
        score_kind="semantic",
        score_value=0.75,
    )
    metadata = QuestionGenerationMetadata(
        question_id=question.id,
        request_id=uuid4(),
        prompt_version="question-v1",
        model="test-model",
        model_version=None,
        provider_name="stub",
        retrieval_mode="hybrid",
    )
    comment = QuestionRevisionComment(
        question_id=question.id,
        comment="请补充示例",
        commented_by=teacher.id,
    )
    session.add_all([source, metadata, comment])
    session.commit()
    return (
        teacher.id,
        course.id,
        question.id,
        document.id,
        chunk.id,
        source.id,
        comment.id,
    )


def test_new_tables_columns_constraints_and_indexes() -> None:
    with isolated_postgres_engine() as engine, engine.connect() as connection:
        inspector = inspect(connection)
        assert {
            "question_source_chunks",
            "question_generation_metadata",
            "question_revision_comments",
        } <= set(inspector.get_table_names())
        source_columns = {
            item["name"]: item
            for item in inspector.get_columns("question_source_chunks")
        }
        assert {
            "id",
            "question_id",
            "chunk_id",
            "live_chunk_id",
            "document_id",
            "course_id",
            "source_order",
            "content_snapshot",
            "source_file",
            "chunk_index",
            "retrieval_rank",
            "score_kind",
            "score_value",
        } == set(source_columns)
        assert source_columns["live_chunk_id"]["nullable"] is True
        assert source_columns["content_snapshot"]["nullable"] is False
        uniques = {
            tuple(item["column_names"])
            for item in inspector.get_unique_constraints("question_source_chunks")
        }
        assert {("question_id", "chunk_id"), ("question_id", "source_order")} <= uniques
        assert any(
            item["column_names"] == ["course_id", "question_id"]
            for item in inspector.get_indexes("question_source_chunks")
        )
        assert any(
            item["column_names"] == ["question_id", "commented_at"]
            for item in inspector.get_indexes("question_revision_comments")
        )
        assert inspector.get_pk_constraint("question_generation_metadata")[
            "constrained_columns"
        ] == ["question_id"]
        for table, name in (
            ("question_generation_metadata", "generated_at"),
            ("question_revision_comments", "commented_at"),
        ):
            column = next(
                item for item in inspector.get_columns(table) if item["name"] == name
            )
            assert column["type"].timezone is True


@pytest.mark.parametrize(
    "violation",
    [
        "duplicate_chunk",
        "duplicate_order",
        "bad_question",
        "bad_live_chunk",
        "duplicate_metadata",
        "empty_comment",
        "blank_comment",
        "long_comment",
        "bad_commenter",
    ],
)
def test_database_rejects_invalid_sources_and_comments(violation: str) -> None:
    with (
        isolated_postgres_engine() as engine,
        Session(engine, expire_on_commit=False) as session,
    ):
        teacher_id, course_id, question_id, document_id, chunk_id, _, _ = _seed(session)
        source = QuestionSourceChunk(
            question_id=question_id,
            chunk_id=uuid4(),
            live_chunk_id=None,
            document_id=document_id,
            course_id=course_id,
            source_order=1,
            content_snapshot="另一个片段",
            source_file="lesson.md",
            chunk_index=1,
        )
        comment = QuestionRevisionComment(
            question_id=question_id,
            comment="再次修改",
            commented_by=teacher_id,
        )
        if violation == "duplicate_chunk":
            source.chunk_id = chunk_id
        elif violation == "duplicate_order":
            source.source_order = 0
        elif violation == "bad_question":
            source.question_id = uuid4()
        elif violation == "bad_live_chunk":
            source.live_chunk_id = uuid4()
        elif violation == "duplicate_metadata":
            row = QuestionGenerationMetadata(
                question_id=question_id,
                request_id=uuid4(),
                prompt_version="v2",
                model="test",
                provider_name="stub",
                retrieval_mode="keyword_only",
            )
        elif violation == "empty_comment":
            comment.comment = ""
        elif violation == "blank_comment":
            comment.comment = "   "
        elif violation == "long_comment":
            comment.comment = "字" * 2001
        elif violation == "bad_commenter":
            comment.commented_by = uuid4()
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(
                row
                if violation == "duplicate_metadata"
                else comment
                if violation.endswith("comment") or violation == "bad_commenter"
                else source
            )
            session.flush()


def test_chunk_and_document_deletion_preserve_snapshot_then_question_cascades() -> None:
    with isolated_postgres_engine() as engine:
        with Session(engine, expire_on_commit=False) as session:
            _, course_id, question_id, document_id, chunk_id, source_id, comment_id = (
                _seed(session)
            )
            chunk = session.get(DocumentChunk, chunk_id)
            assert chunk is not None
            session.delete(chunk)
            session.commit()
        with Session(engine) as session:
            source = session.get(QuestionSourceChunk, source_id)
            assert source is not None
            assert source.live_chunk_id is None
            assert source.chunk_id == chunk_id
            assert source.document_id == document_id
            assert source.course_id == course_id
            assert source.content_snapshot == "变量保存数据"
            assert source.source_file == "lesson.md"
            document = session.get(Document, document_id)
            assert document is not None
            session.delete(document)
            session.commit()
        with Session(engine) as session:
            assert session.get(QuestionSourceChunk, source_id) is not None
            question = session.get(Question, question_id)
            assert question is not None
            session.delete(question)
            session.commit()
        with Session(engine) as session:
            assert (
                session.scalar(select(func.count()).select_from(QuestionSourceChunk))
                == 0
            )
            assert session.get(QuestionGenerationMetadata, question_id) is None
            assert session.get(QuestionRevisionComment, comment_id) is None


def test_timestamps_are_utc_aware_and_comment_boundary_is_accepted() -> None:
    with (
        isolated_postgres_engine() as engine,
        Session(engine, expire_on_commit=False) as session,
    ):
        _, _, question_id, _, _, _, comment_id = _seed(session)
        metadata = session.get(QuestionGenerationMetadata, question_id)
        comment = session.get(QuestionRevisionComment, comment_id)
        assert metadata is not None and comment is not None
        assert metadata.model_version is None
        for timestamp in (metadata.generated_at, comment.commented_at):
            assert timestamp.tzinfo is not None
            assert abs((datetime.now(UTC) - timestamp).total_seconds()) < 60
        comment.comment = "字" * 2000
        session.commit()
        session.refresh(comment)
        assert len(comment.comment) == 2000
