"""T078 出题链路与教师审核集成测试。

测试使用真实 PostgreSQL 隔离 schema 和真实出题服务，只替换检索、Embedding、LLM
Provider。候选题必须经过校验和教师审核后才能进入题库考试。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from backend.app.ai.agents.question_agent import QuestionAgent
from backend.app.api.question_generation import (
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
)
from backend.app.services.exam_service import ExamService, ExamValidationError
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
    assert all(row.status not in {QuestionStatus.APPROVED, QuestionStatus.PUBLISHED} for row in rows)
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
        assert session.scalars(select(Question)).one().status is QuestionStatus.PENDING_REVIEW


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


__all__ = ["test_generation_validation_and_teacher_review_gate"]
