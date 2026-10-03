"""T169 supplemental PostgreSQL behavior acceptance; TCR section 24.

Providers below are controlled protocol fixtures. These checks prove business
approval, revision and graph boundaries, not semantic model quality.
"""

from __future__ import annotations

import asyncio
from typing import Literal

import pytest
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.ai.llm.base import LLMMessages
from backend.app.domain.enums import QuestionStatus, QuestionType
from backend.app.models import (
    Question,
    QuestionRevisionComment,
    QuestionValidationResult,
)
from backend.app.schemas.content_validation import CHECK_KINDS, CheckKind
from backend.app.services.content_validation_service import (
    ContentValidationError,
    ContentValidationService,
)
from backend.app.services.question_service import (
    QuestionService,
    QuestionValidationError,
)
from tests.integration.test_content_validation_persistence import pg_case as _pg_case
from tests.integration.test_question_adaptation import database as _adaptation_database
from tests.support.semantic_validation_doubles import StubSemanticProvider
from tests.unit.services.test_question_adaptation_service import (
    test_parent_graph_rejects_self_cross_course_and_cycle as exercise_parent_graph,
)

pg_case = _pg_case
database = _adaptation_database


class SingleCheckProvider(StubSemanticProvider):
    """Keep three applicable checks passed and vary only the selected fourth."""

    def __init__(
        self,
        kind: CheckKind,
        verdict: Literal["fail", "needs_review", "insufficient_evidence"],
    ) -> None:
        super().__init__()
        self.kind = kind
        self.check_verdict = verdict

    async def generate_structured(
        self, messages: LLMMessages, schema: type[BaseModel], model: str | None = None
    ) -> BaseModel:
        output = await super().generate_structured(messages, schema, model)
        data = output.model_dump(mode="json")
        for check in data["checks"]:
            if check["kind"] == self.kind:
                check["verdict"] = self.check_verdict
                check["reason"] = "Controlled single-check blocking conclusion."
        return schema.model_validate(data)


def test_postgres_parent_graph_rejects_self_cross_course_and_cycle(database):
    """Reuse the existing graph exercise with its genuine PostgreSQL fixture."""
    exercise_parent_graph(database)


@pytest.mark.parametrize("kind", sorted(CHECK_KINDS))
@pytest.mark.parametrize("verdict", ["fail", "needs_review", "insufficient_evidence"])
def test_each_individual_check_blocks_approval_on_postgres(pg_case, kind, verdict):
    engine, question_id, teacher_id, refs, root = pg_case
    provider = SingleCheckProvider(kind, verdict)
    with Session(engine, expire_on_commit=False, autoflush=False) as session:
        QuestionService(session).update_question(
            question_id,
            question_type=QuestionType.SINGLE_CHOICE,
            content="Which answer matches the actual teaching fixture?",
            options={"B": "Another condition", "A": "Actual teaching evidence"},
            reference_answer="A",
            scoring_rubric="Only A receives 1 point; every other answer receives 0.",
            teacher_id=teacher_id,
        )
        service = ContentValidationService(session, root=root)
        report = asyncio.run(
            service.run_validation(
                question_id, actor_id=teacher_id, input_refs=refs, provider=provider
            )
        )
        assert len(provider.calls) == 1
        assert report.outcome == "failed" and not report.can_review
        assert report.is_current and not report.stale
        assert {check.kind: check.verdict for check in report.checks} == {
            name: verdict if name == kind else "pass" for name in CHECK_KINDS
        }
        question = session.get(Question, question_id)
        with pytest.raises(ContentValidationError) as failure:
            service.require_can_approve(question, teacher_id)
        assert failure.value.code == "CONTENT_VALIDATION_NOT_READY"
        session.rollback()
        with pytest.raises(QuestionValidationError):
            QuestionService(session).update_question_status(
                question_id, QuestionStatus.APPROVED, teacher_id=teacher_id
            )
        session.rollback()
    with Session(engine) as reader:
        question = reader.get(Question, question_id)
        row = reader.get(QuestionValidationResult, report.id)
        assert question.status is QuestionStatus.NEEDS_REVISION
        assert question.frozen_at is None
        assert row.outcome == "failed" and row.input_revision == question.validation_revision
        assert {check["kind"]: check["verdict"] for check in row.checks} == {
            name: verdict if name == kind else "pass" for name in CHECK_KINDS
        }
        assert reader.scalar(select(QuestionRevisionComment)) is None


def test_return_to_original_content_requires_new_report_on_postgres(pg_case):
    engine, question_id, teacher_id, refs, root = pg_case
    first_provider = StubSemanticProvider()
    with Session(engine, expire_on_commit=False, autoflush=False) as session:
        service = ContentValidationService(session, root=root)
        question = session.get(Question, question_id)
        original_content, original_answer = question.content, question.reference_answer
        first = asyncio.run(
            service.run_validation(
                question_id,
                actor_id=teacher_id,
                input_refs=refs,
                provider=first_provider,
            )
        )
        assert first.can_review and first.input_revision == 0 and first.run_no == 1
        QuestionService(session).update_question(
            question_id,
            content="Changed real fixture condition",
            reference_answer="Changed fixture answer",
            teacher_id=teacher_id,
        )
        assert session.get(Question, question_id).validation_revision == 1
        QuestionService(session).update_question(
            question_id,
            content=original_content,
            reference_answer=original_answer,
            teacher_id=teacher_id,
        )
        assert session.get(Question, question_id).validation_revision == 2
    with Session(engine, expire_on_commit=False, autoflush=False) as reader:
        service = ContentValidationService(reader, root=root)
        question = reader.get(Question, question_id)
        assert question.content == original_content
        assert question.reference_answer == original_answer
        old = service.get_validation(question_id, first.id, actor_id=teacher_id)
        assert old.outcome == "passed" and old.stale and not old.can_review
        assert old.input_revision == 0 and not old.is_current
        with pytest.raises(QuestionValidationError):
            QuestionService(reader).update_question_status(
                question_id, QuestionStatus.APPROVED, teacher_id=teacher_id
            )
        reader.rollback()
        assert reader.get(Question, question_id).frozen_at is None
        current_provider = StubSemanticProvider()
        current = asyncio.run(
            service.run_validation(
                question_id,
                actor_id=teacher_id,
                input_refs=refs,
                provider=current_provider,
            )
        )
        assert current.can_review and current.input_revision == 2 and current.run_no == 2
        assert current.id != first.id
        assert current_provider.calls[0]["fields"] == first_provider.calls[0]["fields"]
        approved = QuestionService(reader).update_question_status(
            question_id, QuestionStatus.APPROVED, teacher_id=teacher_id
        )
        assert approved.frozen_at is not None
    with Session(engine) as final_reader:
        question = final_reader.get(Question, question_id)
        old_row = final_reader.get(QuestionValidationResult, first.id)
        new_row = final_reader.get(QuestionValidationResult, current.id)
        assert question.status is QuestionStatus.APPROVED and question.validation_revision == 2
        assert old_row.outcome == "passed" and old_row.input_revision == 0
        assert new_row.outcome == "passed" and new_row.input_revision == 2
        assert final_reader.scalar(select(QuestionRevisionComment)) is None
