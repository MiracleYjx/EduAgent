"""T166 semantic execution and approval eligibility; TCR section 20."""

from copy import deepcopy
from uuid import uuid4

import pytest

from backend.app.domain.enums import QuestionStatus
from backend.app.schemas.image_assessment import TechnicalError
from backend.app.services.content_validation_service import ContentValidationError
from backend.app.services.question_service import (
    QuestionService,
    QuestionValidationError,
)
from tests.unit.services.test_content_validation_service import (
    result,
    semantic_case,
    start,
)
from tests.unit.services.test_file_storage_service import (
    owned_document as _owned_document,
)

owned_document = _owned_document


def test_no_report_cannot_be_approved_by_direct_service(owned_document):
    session, _doc, teacher, _root = owned_document
    _service, question, _refs = semantic_case(owned_document)
    with pytest.raises(QuestionValidationError):
        QuestionService(session).update_question_status(
            question.id, QuestionStatus.APPROVED, teacher_id=teacher.id
        )
    session.rollback()
    assert question.status is QuestionStatus.PENDING_REVIEW
    assert question.frozen_at is None


def test_current_passed_report_allows_real_freeze_and_revision_invalidates(
    owned_document,
):
    session, _doc, teacher, _root = owned_document
    service, question, refs = semantic_case(owned_document)
    report = start(service, question, refs, teacher)
    service.finish_validation(report.id, actor_id=teacher.id, output=result(refs))
    approved = QuestionService(session).update_question_status(
        question.id, QuestionStatus.APPROVED, teacher_id=teacher.id
    )
    assert approved.frozen_at is not None
    historical = QuestionService(session).update_question_status(
        question.id, QuestionStatus.APPROVED, teacher_id=teacher.id
    )
    assert historical.frozen_at == approved.frozen_at
    QuestionService(session).update_question_status(
        question.id,
        QuestionStatus.NEEDS_REVISION,
        teacher_id=teacher.id,
        revision_comment="Actual teacher revision reason",
    )
    assert question.frozen_at is None
    QuestionService(session).update_question_status(
        question.id, QuestionStatus.PENDING_REVIEW, teacher_id=teacher.id
    )
    with pytest.raises(QuestionValidationError):
        QuestionService(session).update_question_status(
            question.id, QuestionStatus.APPROVED, teacher_id=teacher.id
        )


def test_new_running_or_technical_error_cannot_fallback_to_passed(owned_document):
    session, _doc, teacher, _root = owned_document
    service, question, refs = semantic_case(owned_document)
    first = start(service, question, refs, teacher)
    service.finish_validation(first.id, actor_id=teacher.id, output=result(refs))
    current = start(service, question, refs, teacher)
    with pytest.raises(QuestionValidationError):
        QuestionService(session).update_question_status(
            question.id, QuestionStatus.APPROVED, teacher_id=teacher.id
        )
    service.finish_validation(
        current.id,
        actor_id=teacher.id,
        error=TechnicalError(
            code="ProviderTimeout",
            message="fixture timeout",
            stage="call",
            retryable=True,
        ),
    )
    with pytest.raises(QuestionValidationError):
        QuestionService(session).update_question_status(
            question.id, QuestionStatus.APPROVED, teacher_id=teacher.id
        )
    assert question.status is QuestionStatus.PENDING_REVIEW


def test_prepare_uses_frozen_fields_and_releases_lock(owned_document):
    session, _doc, teacher, _root = owned_document
    service, question, refs = semantic_case(owned_document)
    prepared = service.prepare_validation(
        question.id,
        actor_id=teacher.id,
        input_refs=refs,
        executor_name="fixture_semantic",
    )
    assert not session.in_transaction()
    assert prepared.input.input_revision == prepared.report.input_revision
    assert prepared.input.fields.content == question.content
    assert prepared.input.fields.score == question.score
    assert prepared.input.evidence == refs.evidence
    assert "difficulty" not in prepared.input.fields.model_dump()
    assert "knowledge_points" not in prepared.input.fields.model_dump()
    original = deepcopy(prepared.input.fields.model_dump())
    QuestionService(session).update_question(
        question.id, content="changed condition", teacher_id=teacher.id
    )
    assert prepared.input.fields.model_dump() == original
    late = service.finish_validation(
        prepared.report.id, actor_id=teacher.id, output=result(refs, "fail")
    )
    assert late.stale and question.status is QuestionStatus.PENDING_REVIEW


def test_illegal_answer_encoding_never_starts_semantic_call(owned_document):
    session, _doc, teacher, _root = owned_document
    service, question, refs = semantic_case(owned_document)
    question.type = "SINGLE_CHOICE"
    question.options = {"C": "three", "A": "one"}
    question.reference_answer = "Z"
    session.commit()
    with pytest.raises(ContentValidationError):
        service.prepare_validation(
            question.id,
            actor_id=teacher.id,
            input_refs=refs,
            executor_name="fixture_semantic",
        )


def test_history_approved_read_and_idempotence_do_not_forge_report(owned_document):
    session, _doc, teacher, _root = owned_document
    _service, question, _refs = semantic_case(owned_document)
    question.status = QuestionStatus.APPROVED
    question.frozen_at = None
    session.commit()
    assert (
        QuestionService(session).get_question(question.id, teacher_id=teacher.id).status
        is QuestionStatus.APPROVED
    )
    assert (
        QuestionService(session)
        .update_question_status(
            question.id, QuestionStatus.APPROVED, teacher_id=teacher.id
        )
        .frozen_at
        is None
    )


class SemanticProvider:
    model_name = "fixture-semantic-model"
    model_version = "fixture-v1"

    def __init__(self, behavior="pass"):
        self.behavior = behavior
        self.calls = []
        self.closed = 0

    async def generate_structured(self, messages, schema, model=None):
        import asyncio
        import json

        self.calls.append(json.loads(messages[1]["content"]))
        if self.behavior == "timeout":
            raise TimeoutError("credential should never leak")
        if self.behavior == "cancel":
            raise asyncio.CancelledError()
        evidence_id = self.calls[-1]["evidence"][0]["evidence_id"]
        if self.behavior == "unknown_evidence":
            evidence_id = str(uuid4())
        data = {
            "checks": [
                {
                    "kind": kind,
                    "verdict": "fail" if self.behavior == "fail" else "pass",
                    "reason": (
                        "not applicable: no options"
                        if kind == "option_ambiguity"
                        else "Actual fixture conclusion"
                    ),
                    "evidence_refs": [evidence_id],
                }
                for kind in (
                    "answer_correctness",
                    "condition_sufficiency",
                    "option_ambiguity",
                    "rubric_clarity",
                )
            ],
            "issues": (
                [
                    {
                        "code": "WRONG_ANSWER",
                        "field": "reference_answer",
                        "severity": "error",
                        "message": "Actual fixture semantic problem",
                        "evidence_refs": [evidence_id],
                    }
                ]
                if self.behavior == "fail"
                else []
            ),
        }
        if self.behavior == "forged_identity":
            data["can_review"] = True
            from pydantic import BaseModel, ConfigDict

            class UnvalidatedResult(BaseModel):
                model_config = ConfigDict(extra="allow")

            return UnvalidatedResult.model_validate(data)
        return schema.model_validate(data)

    async def aclose(self):
        self.closed += 1


@pytest.mark.parametrize(
    "behavior,outcome",
    [
        ("pass", "passed"),
        ("fail", "failed"),
        ("timeout", "technical_error"),
        ("unknown_evidence", "technical_error"),
        ("forged_identity", "technical_error"),
    ],
)
def test_actual_one_shot_provider_results_are_persisted_without_fake_teacher(
    owned_document, behavior, outcome
):
    import asyncio

    from sqlalchemy import select

    from backend.app.models import QuestionRevisionComment

    session, _doc, teacher, _root = owned_document
    service, question, refs = semantic_case(owned_document)
    provider = SemanticProvider(behavior)
    view = asyncio.run(
        service.run_validation(
            question.id, actor_id=teacher.id, input_refs=refs, provider=provider
        )
    )
    assert view.outcome == outcome
    assert len(provider.calls) == 1 and provider.closed == 0
    assert view.provenance.model == "fixture-semantic-model"
    assert (
        view.provenance.provider_name is None
    )  # Duck injection cannot invent a provider identity.
    assert view.provenance.prompt_version == "question-semantics-v1"
    assert session.scalar(select(QuestionRevisionComment)) is None
    assert question.status is (
        QuestionStatus.NEEDS_REVISION
        if outcome == "failed"
        else QuestionStatus.PENDING_REVIEW
    )
    if outcome == "technical_error":
        assert view.checks is None and view.issues is None
        assert "credential" not in view.error.message
    if outcome == "failed":
        assert view.issues[0].issue_id is not None


def test_cancel_records_real_error_and_reraises(owned_document):
    import asyncio

    from sqlalchemy import select

    from backend.app.models import QuestionValidationResult

    session, _doc, teacher, _root = owned_document
    service, question, refs = semantic_case(owned_document)
    provider = SemanticProvider("cancel")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            service.run_validation(
                question.id, actor_id=teacher.id, input_refs=refs, provider=provider
            )
        )
    row = session.scalar(
        select(QuestionValidationResult).where(
            QuestionValidationResult.question_id == question.id
        )
    )
    assert row.outcome == "technical_error" and row.error["code"] == "CONTENT_CANCELLED"
    assert question.status is QuestionStatus.PENDING_REVIEW


def test_owned_provider_factory_is_closed_and_configuration_failure_is_recorded(
    owned_document,
):
    import asyncio

    _session, _doc, teacher, _root = owned_document
    service, question, refs = semantic_case(owned_document)
    provider = SemanticProvider()
    done = asyncio.run(
        service.run_validation(
            question.id,
            actor_id=teacher.id,
            input_refs=refs,
            provider_factory=lambda: provider,
        )
    )
    assert done.outcome == "passed" and provider.closed == 1

    def unavailable():
        raise ValueError("private configuration credential")

    failed = asyncio.run(
        service.run_validation(
            question.id,
            actor_id=teacher.id,
            input_refs=refs,
            provider_factory=unavailable,
        )
    )
    assert failed.outcome == "technical_error"
    assert failed.error.stage == "configuration" and failed.provenance.model is None
    assert (
        failed.error.cause == "ValueError" and "credential" not in failed.error.message
    )
    assert not failed.can_review


def test_projection_preserves_ordered_object_without_classification_fields(
    owned_document,
):
    import asyncio

    session, _doc, teacher, _root = owned_document
    service, question, refs = semantic_case(owned_document)
    question.type = "SINGLE_CHOICE"
    question.options = {"C": "three", "A": "one", "B": "two"}
    question.reference_answer = "C"
    question.difficulty = "classification only"
    question.knowledge_points = ["not retrieval evidence"]
    session.commit()
    provider = SemanticProvider()
    view = asyncio.run(
        service.run_validation(
            question.id, actor_id=teacher.id, input_refs=refs, provider=provider
        )
    )
    assert view.outcome == "passed"
    assert list(provider.calls[0]["fields"]["options"]) == ["C", "A", "B"]
    assert "difficulty" not in provider.calls[0]["fields"]
    assert "knowledge_points" not in provider.calls[0]["fields"]


def test_default_evidence_uses_actual_persisted_source_snapshot(owned_document):
    import asyncio

    from backend.app.models import QuestionSourceChunk

    session, doc, teacher, _root = owned_document
    service, question, _refs = semantic_case(owned_document)
    source = QuestionSourceChunk(
        question_id=question.id,
        chunk_id=uuid4(),
        live_chunk_id=None,
        document_id=doc.id,
        course_id=doc.course_id,
        source_order=1,
        content_snapshot="Actual retained generation evidence",
        source_file="original.md",
        chunk_index=0,
    )
    session.add(source)
    session.commit()
    provider = SemanticProvider()
    view = asyncio.run(
        service.run_validation(question.id, actor_id=teacher.id, provider=provider)
    )
    assert view.outcome == "passed"
    assert view.input_refs.evidence[0].source_id == source.id
    assert (
        provider.calls[0]["evidence"][0]["source_data"]["content_snapshot"]
        == source.content_snapshot
    )


@pytest.mark.parametrize("exam_status", ["Published", "Closed", "Archived", "Draft"])
def test_published_or_submission_protected_question_cannot_retreat(
    owned_document, exam_status
):
    from backend.app.models import Exam, ExamQuestion, Submission
    from backend.app.services.question_service import QuestionPublishedImmutableError

    session, _doc, teacher, _root = owned_document
    _service, question, _refs = semantic_case(owned_document)
    question.status = QuestionStatus.APPROVED
    exam = Exam(
        course_id=question.course_id,
        created_by=teacher.id,
        title="Actual fixture exam",
        status=exam_status,
        exam_question_links=[ExamQuestion(question=question, order_index=1)],
    )
    session.add(exam)
    session.flush()
    if exam_status == "Draft":
        session.add(Submission(exam_id=exam.id, student_id=teacher.id))
    session.commit()
    with pytest.raises(QuestionPublishedImmutableError):
        QuestionService(session).update_question_status(
            question.id,
            QuestionStatus.NEEDS_REVISION,
            teacher_id=teacher.id,
            revision_comment="Actual teacher requested revision",
        )
    session.rollback()
    assert question.status is QuestionStatus.APPROVED


def test_manual_retreat_requires_real_comment_and_persists_atomically(owned_document):
    from sqlalchemy import select

    from backend.app.models import QuestionRevisionComment

    session, _doc, teacher, _root = owned_document
    _service, question, _refs = semantic_case(owned_document)
    with pytest.raises(QuestionValidationError):
        QuestionService(session).update_question_status(
            question.id, QuestionStatus.NEEDS_REVISION, teacher_id=teacher.id
        )
    session.rollback()
    changed = QuestionService(session).update_question_status(
        question.id,
        QuestionStatus.NEEDS_REVISION,
        teacher_id=teacher.id,
        revision_comment="Actual teacher revision rationale",
    )
    assert changed.status is QuestionStatus.NEEDS_REVISION
    comment = session.scalar(
        select(QuestionRevisionComment).where(
            QuestionRevisionComment.question_id == question.id
        )
    )
    assert (
        comment.commented_by == teacher.id
        and comment.comment == "Actual teacher revision rationale"
    )


def test_provider_policy_error_preserves_actual_code_and_retryability(owned_document):
    import asyncio

    from backend.app.core.retry_policy import ProviderErrorInfo, ProviderExecutionError

    _session, _doc, teacher, _root = owned_document
    service, question, refs = semantic_case(owned_document)

    class ExhaustedProvider(SemanticProvider):
        async def generate_structured(self, messages, schema, model=None):
            raise ProviderExecutionError(
                ProviderErrorInfo(
                    code="ProviderRateLimited",
                    message="Actual safe provider rate limit",
                    retryable=True,
                    attempt_count=3,
                )
            )

    view = asyncio.run(
        service.run_validation(
            question.id,
            actor_id=teacher.id,
            input_refs=refs,
            provider=ExhaustedProvider(),
        )
    )
    assert view.outcome == "technical_error"
    assert view.error.code == "ProviderRateLimited" and view.error.retryable
    assert view.error.message == "Actual safe provider rate limit"
