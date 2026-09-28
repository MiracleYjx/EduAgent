"""T078 出题链路与教师审核集成测试。

测试使用真实 PostgreSQL 隔离 schema 和真实出题服务，只替换检索、Embedding、LLM
Provider。候选题必须经过校验和教师审核后才能进入题库考试。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.ai.agents.question_agent import (
    QUESTION_GENERATION_PROMPT_VERSION,
    QuestionAgent,
)
from backend.app.api.question_generation import (
    CandidateStoreNotReadyError,
    GenerationFailedError,
    QuestionGenerationService,
)
from backend.app.domain.enums import (
    DocumentStatus,
    QuestionStatus,
    UserRole,
)
from backend.app.models import (
    Course,
    Document,
    DocumentChunk,
    KnowledgeBase,
    Question,
    QuestionGenerationMetadata,
    QuestionRevisionComment,
    QuestionSourceChunk,
)
from backend.app.services.exam_service import ExamService, ExamValidationError
from backend.app.services.question_service import QuestionService
from backend.app.services.question_validator import QuestionValidator
from tests.postgres_helpers import isolated_postgres_engine
from tests.support.question_generation_doubles import (
    StubEmbeddingProvider,
    StubQuestionProvider,
    StubRetriever,
    make_candidate,
    make_chunk,
)
from tests.unit.services.test_submission_service import add_user
from tests.unit.settings_helpers import build_test_settings


@pytest.fixture
def engine() -> Iterator[Engine]:
    """提供真实 PostgreSQL 隔离 schema。"""

    with isolated_postgres_engine() as active:
        yield active


@pytest.fixture
def scenario(engine: Engine) -> dict[str, Any]:
    """创建教师、课程和可作为出题依据的真实课程资料。"""

    with Session(engine) as session:
        teacher = add_user(
            session,
            UserRole.TEACHER,
            username="t078-teacher",
            email="t078-teacher@example.com",
        )
        course = Course(name="T078 Python 基础", creator=teacher)
        session.add(course)
        session.flush()
        knowledge_base = KnowledgeBase(course_id=course.id, name="T078 课程资料")
        session.add(knowledge_base)
        session.flush()
        document = Document(
            course_id=course.id,
            knowledge_base_id=knowledge_base.id,
            uploaded_by=teacher.id,
            original_filename="变量与作用域.pdf",
            file_format="pdf",
            status=DocumentStatus.READY,
        )
        session.add(document)
        session.flush()
        chunk = DocumentChunk(
            document_id=document.id,
            course_id=course.id,
            knowledge_base_id=knowledge_base.id,
            chunk_index=0,
            content="变量用于保存数据，并可在后续语句中引用。",
            chunk_metadata={},
        )
        session.add(chunk)
        session.commit()
        return {
            "teacher_id": str(teacher.id),
            "course_id": str(course.id),
            "chunk_id": str(chunk.id),
            "document_id": str(document.id),
        }


def _service(
    engine: Engine,
    scenario: dict[str, Any],
    *,
    candidates: list[Any],
    provider_error: Exception | None = None,
) -> tuple[QuestionGenerationService, StubRetriever, StubQuestionProvider]:
    """按真实服务边界装配出题链路。"""

    course_chunk = make_chunk(
        scenario["chunk_id"],
        course_id=scenario["course_id"],
        document_id=scenario["document_id"],
    )
    provider = StubQuestionProvider(candidates=candidates, error=provider_error)
    retriever = StubRetriever([course_chunk])
    service = QuestionGenerationService(
        session_factory=lambda: Session(engine),
        agent=QuestionAgent(provider=provider),
        validator=QuestionValidator(),
        retriever=retriever,
        embedding_provider=StubEmbeddingProvider(),
        settings=build_test_settings(),
    )
    return service, retriever, provider


def _generate(
    service: QuestionGenerationService,
    scenario: dict[str, Any],
    count: int,
) -> Any:
    """提交教师条件并运行完整生成、校验、落库链路。"""

    return asyncio.run(
        service.generate_candidates(
            course_id=scenario["course_id"],
            actor_id=scenario["teacher_id"],
            request_id="request-t078",
            knowledge_points=["变量"],
            difficulty="中等",
            count=count,
        )
    )


def _questions(engine: Engine, course_id: str) -> list[Question]:
    """读取课程下的候选题事实。"""

    with Session(engine) as session:
        return list(
            session.scalars(
                select(Question)
                .where(Question.course_id == course_id)
                .order_by(Question.created_at, Question.id)
            )
        )


def test_generation_validation_and_teacher_review_gate(
    engine: Engine,
    scenario: dict[str, Any],
) -> None:
    """生成后先进入审核队列，教师批准或退回后才发生对应状态转换。"""

    candidates = [
        make_candidate(
            content="变量的作用是什么？",
            source_context_ids=[scenario["chunk_id"]],
            score=2,
        ),
        make_candidate(
            content="变量为什么可以被引用？",
            source_context_ids=[scenario["chunk_id"]],
            score=3,
        ),
    ]
    service, retriever, provider = _service(engine, scenario, candidates=candidates)
    response = _generate(service, scenario, count=2)

    assert response.generated_count == 2
    assert response.batch_validation.status is QuestionStatus.PENDING_REVIEW
    assert len(retriever.calls) == 1
    assert tuple(str(item) for item in retriever.calls[0]["filters"].course_ids) == (
        scenario["course_id"],
    )
    assert len(provider.calls) == 1
    assert scenario["course_id"] in provider.calls[0]["messages"][1]["content"]

    rows = _questions(engine, scenario["course_id"])
    assert len(rows) == 2
    assert {row.status for row in rows} == {QuestionStatus.PENDING_REVIEW}
    assert all(
        row.status not in {QuestionStatus.APPROVED, QuestionStatus.PUBLISHED}
        for row in rows
    )
    assert response.evidence and response.evidence[0].chunk_id == scenario["chunk_id"]

    approved = service.submit_review(
        actor_id=scenario["teacher_id"],
        candidate_id=str(rows[0].id),
        action="approve",
        expected_status=QuestionStatus.PENDING_REVIEW,
    )
    returned = service.submit_review(
        actor_id=scenario["teacher_id"],
        candidate_id=str(rows[1].id),
        action="request_revision",
        comment="请补充变量在后续语句中的引用说明。",
        expected_status=QuestionStatus.PENDING_REVIEW,
    )
    assert approved.status is QuestionStatus.APPROVED
    assert returned.status is QuestionStatus.NEEDS_REVISION
    final_rows = _questions(engine, scenario["course_id"])
    assert [row.status for row in final_rows] == [
        QuestionStatus.APPROVED,
        QuestionStatus.NEEDS_REVISION,
    ]


def test_unreviewed_ai_candidate_cannot_be_published_to_exam(
    engine: Engine,
    scenario: dict[str, Any],
) -> None:
    """AI 候选未经过教师审核时不能进入考试。"""

    candidate = make_candidate(source_context_ids=[scenario["chunk_id"]])
    service, _, _ = _service(engine, scenario, candidates=[candidate])
    _generate(service, scenario, count=1)
    question = _questions(engine, scenario["course_id"])[0]
    assert question.status is QuestionStatus.PENDING_REVIEW

    with Session(engine) as session:
        with pytest.raises(ExamValidationError, match="Approved"):
            ExamService(session).create_exam(
                course_id=scenario["course_id"],
                title="未审核候选不得组卷",
                question_ids=[str(question.id)],
                created_by=scenario["teacher_id"],
            )
        assert (
            session.scalars(select(Question)).one().status
            is QuestionStatus.PENDING_REVIEW
        )


def test_invalid_candidate_enters_needs_revision_without_partial_batch(
    engine: Engine,
    scenario: dict[str, Any],
) -> None:
    """同批候选校验结论分别落库，未通过者进入 Needs Revision 且不会自动批准。"""

    candidates = [
        make_candidate(source_context_ids=[scenario["chunk_id"]]),
        make_candidate(
            content="变量的作用域是什么？",
            source_context_ids=[scenario["chunk_id"]],
            scoring_rubric="2 分",
        ),
    ]
    service, _, _ = _service(engine, scenario, candidates=candidates)
    response = _generate(service, scenario, count=2)

    rows = _questions(engine, scenario["course_id"])
    assert response.batch_validation.status is QuestionStatus.NEEDS_REVISION
    assert len(rows) == 2
    assert [row.status for row in rows] == [
        QuestionStatus.PENDING_REVIEW,
        QuestionStatus.NEEDS_REVISION,
    ]
    assert all(row.status is not QuestionStatus.APPROVED for row in rows)


def test_generation_failure_rolls_back_the_whole_batch(
    engine: Engine,
    scenario: dict[str, Any],
) -> None:
    """Provider 失败时整批生成失败，数据库没有半批候选题。"""

    service, _, _ = _service(
        engine,
        scenario,
        candidates=[],
        provider_error=RuntimeError("模拟 Provider 失败"),
    )
    with pytest.raises(GenerationFailedError):
        _generate(service, scenario, count=2)
    assert _questions(engine, scenario["course_id"]) == []


def test_generation_persists_actual_source_snapshot_and_provider_metadata(
    engine: Engine,
    scenario: dict[str, Any],
) -> None:
    candidates = [
        make_candidate(
            content=f"生成题 {index}", source_context_ids=[scenario["chunk_id"]]
        )
        for index in range(2)
    ]
    service, _, provider = _service(engine, scenario, candidates=candidates)
    provider.model_name = "stub-model-for-this-call"
    provider.model_version = "stub-version-1"

    response = _generate(service, scenario, count=2)
    assert response.sources_persisted is True
    expected_content = make_chunk(scenario["chunk_id"]).content
    with Session(engine) as session:
        questions = list(session.scalars(select(Question).order_by(Question.content)))
        sources = list(session.scalars(select(QuestionSourceChunk)))
        metadata = list(session.scalars(select(QuestionGenerationMetadata)))
    assert len(questions) == len(sources) == len(metadata) == 2
    assert {source.question_id for source in sources} == {item.id for item in questions}
    assert {item.question_id for item in metadata} == {item.id for item in questions}
    for source in sources:
        assert source.chunk_id == UUID(scenario["chunk_id"])
        assert source.live_chunk_id == UUID(scenario["chunk_id"])
        assert source.document_id == UUID(scenario["document_id"])
        assert source.course_id == UUID(scenario["course_id"])
        assert source.source_order == 0
        assert source.content_snapshot == expected_content
        assert source.source_file == "变量与作用域.pdf"
        assert source.chunk_index == 0
        assert source.retrieval_rank == 1
        assert (source.score_kind, source.score_value) == ("semantic", 0.9)
    for item in metadata:
        assert item.request_id == uuid5(
            NAMESPACE_URL, "eduagent:question-generation:request-t078"
        )
        assert item.prompt_version == QUESTION_GENERATION_PROMPT_VERSION
        assert item.model == "stub-model-for-this-call"
        assert item.model_version == "stub-version-1"
        assert item.provider_name == "stub"
        assert item.retrieval_mode == "hybrid"
        assert item.generated_at.tzinfo is not None


def test_uuid_request_id_is_persisted_without_remapping(
    engine: Engine,
    scenario: dict[str, Any],
) -> None:
    service, _, _ = _service(
        engine,
        scenario,
        candidates=[make_candidate(source_context_ids=[scenario["chunk_id"]])],
    )
    request_id = uuid4()
    response = asyncio.run(
        service.generate_candidates(
            course_id=scenario["course_id"],
            actor_id=scenario["teacher_id"],
            request_id=str(request_id),
        )
    )
    assert response.request_id == str(request_id)
    with Session(engine) as session:
        assert (
            session.scalars(select(QuestionGenerationMetadata)).one().request_id
            == request_id
        )


def test_disappeared_live_source_rejects_the_whole_batch(
    engine: Engine,
    scenario: dict[str, Any],
) -> None:
    with Session(engine) as session:
        chunk = session.get(DocumentChunk, UUID(scenario["chunk_id"]))
        assert chunk is not None
        session.delete(chunk)
        session.commit()
    service, _, _ = _service(
        engine,
        scenario,
        candidates=[make_candidate(source_context_ids=[scenario["chunk_id"]])],
    )
    with pytest.raises(CandidateStoreNotReadyError):
        _generate(service, scenario, count=1)
    with Session(engine) as session:
        for model in (Question, QuestionSourceChunk, QuestionGenerationMetadata):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_cross_course_source_rejects_entire_generation_batch(
    engine: Engine,
    scenario: dict[str, Any],
) -> None:
    with Session(engine) as session:
        other_course = Course(name="另一课程", created_by=UUID(scenario["teacher_id"]))
        session.add(other_course)
        session.flush()
        knowledge_base = KnowledgeBase(course_id=other_course.id, name="其他资料")
        session.add(knowledge_base)
        session.flush()
        document = Document(
            course_id=other_course.id,
            knowledge_base_id=knowledge_base.id,
            uploaded_by=UUID(scenario["teacher_id"]),
            original_filename="other.pdf",
            file_format="pdf",
            status=DocumentStatus.READY,
        )
        session.add(document)
        session.flush()
        other_chunk = DocumentChunk(
            document_id=document.id,
            course_id=other_course.id,
            knowledge_base_id=knowledge_base.id,
            chunk_index=0,
            content="另一门课程的正文",
            chunk_metadata={},
        )
        session.add(other_chunk)
        session.commit()
        other_chunk_id = str(other_chunk.id)
        other_course_id = str(other_course.id)
        other_document_id = str(document.id)

    service, retriever, _ = _service(
        engine,
        scenario,
        candidates=[
            make_candidate(source_context_ids=[scenario["chunk_id"], other_chunk_id])
        ],
    )
    retriever.chunks.append(
        make_chunk(
            other_chunk_id,
            course_id=other_course_id,
            document_id=other_document_id,
            content="另一门课程的正文",
        )
    )
    with pytest.raises(GenerationFailedError) as error:
        _generate(service, scenario, count=1)
    assert error.value.error_code == "QUESTION_UNKNOWN_COURSE_EVIDENCE"
    with Session(engine) as session:
        for model in (Question, QuestionSourceChunk, QuestionGenerationMetadata):
            assert session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize(
    "failure_target", [QuestionSourceChunk, QuestionGenerationMetadata]
)
def test_source_or_metadata_failure_rolls_back_entire_generation_batch(
    engine: Engine,
    scenario: dict[str, Any],
    failure_target: type[QuestionSourceChunk | QuestionGenerationMetadata],
) -> None:
    service, _, _ = _service(
        engine,
        scenario,
        candidates=[
            make_candidate(
                content=f"事务题 {index}", source_context_ids=[scenario["chunk_id"]]
            )
            for index in range(2)
        ],
    )
    inserted = 0

    def fail_second_insert(_mapper: Any, _connection: Any, _target: Any) -> None:
        nonlocal inserted
        inserted += 1
        if inserted == 2:
            raise SQLAlchemyError("injected generation persistence failure")

    event.listen(failure_target, "before_insert", fail_second_insert)
    try:
        with pytest.raises(CandidateStoreNotReadyError):
            _generate(service, scenario, count=2)
    finally:
        event.remove(failure_target, "before_insert", fail_second_insert)
    assert inserted == 2
    with Session(engine) as session:
        for model in (Question, QuestionSourceChunk, QuestionGenerationMetadata):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_revision_comment_insert_failure_rolls_back_question_status(
    engine: Engine,
    scenario: dict[str, Any],
) -> None:
    service, _, _ = _service(
        engine,
        scenario,
        candidates=[make_candidate(source_context_ids=[scenario["chunk_id"]])],
    )
    candidate_id = _generate(service, scenario, count=1).candidates[0].candidate_id

    def fail_comment(_mapper: Any, _connection: Any, _target: Any) -> None:
        raise SQLAlchemyError("injected comment persistence failure")

    event.listen(QuestionRevisionComment, "before_insert", fail_comment)
    try:
        with pytest.raises(CandidateStoreNotReadyError):
            service.submit_review(
                actor_id=scenario["teacher_id"],
                candidate_id=candidate_id,
                action="request_revision",
                comment="请重新核对。",
                expected_status=QuestionStatus.PENDING_REVIEW,
            )
    finally:
        event.remove(QuestionRevisionComment, "before_insert", fail_comment)

    with Session(engine) as session:
        question = session.get(Question, UUID(candidate_id))
        assert question is not None
        assert question.status is QuestionStatus.PENDING_REVIEW
        assert (
            session.scalar(select(func.count()).select_from(QuestionRevisionComment))
            == 0
        )


def test_two_revision_rounds_append_comments_in_time_order(
    engine: Engine,
    scenario: dict[str, Any],
) -> None:
    service, _, _ = _service(
        engine,
        scenario,
        candidates=[make_candidate(source_context_ids=[scenario["chunk_id"]])],
    )
    candidate_id = _generate(service, scenario, count=1).candidates[0].candidate_id
    first = service.submit_review(
        actor_id=scenario["teacher_id"],
        candidate_id=candidate_id,
        action="request_revision",
        comment="第一轮意见",
    )
    assert first.comment_persisted is True
    with Session(engine) as session:
        QuestionService(session).update_question_status(
            candidate_id,
            QuestionStatus.PENDING_REVIEW,
            teacher_id=scenario["teacher_id"],
        )
    second = service.submit_review(
        actor_id=scenario["teacher_id"],
        candidate_id=candidate_id,
        action="request_revision",
        comment="第二轮意见",
    )
    assert second.comment_persisted is True
    with Session(engine) as session:
        comments = list(
            session.scalars(
                select(QuestionRevisionComment)
                .where(QuestionRevisionComment.question_id == UUID(candidate_id))
                .order_by(
                    QuestionRevisionComment.commented_at,
                    QuestionRevisionComment.id,
                )
            )
        )
        assert [item.comment for item in comments] == ["第一轮意见", "第二轮意见"]
        assert comments[0].commented_at <= comments[1].commented_at
        assert all(item.commented_at.tzinfo is not None for item in comments)


__all__ = ["test_generation_validation_and_teacher_review_gate"]
