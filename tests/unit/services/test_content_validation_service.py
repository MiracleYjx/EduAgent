"""T163 durable reports, real teacher evidence and invalidation. TCR §17."""

from copy import deepcopy
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from backend.app.domain.enums import QuestionStatus
from backend.app.models import (
    DocumentChunk,
    Question,
    QuestionRevisionComment,
    QuestionValidationResult,
)
from backend.app.schemas.content_validation import (
    ManualDispositionRequest,
    ValidationCheck,
    ValidationEvidence,
    ValidationInputRefs,
    ValidationOutput,
)
from backend.app.schemas.image_assessment import (
    ImageManualCheckRequest,
    ImageUnderstandingResult,
    TechnicalError,
)
from backend.app.schemas.question_assets import AssetLinkRequest
from backend.app.services.content_validation_service import (
    ContentValidationError,
    ContentValidationService,
)
from backend.app.services.question_correction_service import QuestionCorrectionService
from tests.unit.services.test_file_storage_service import (
    owned_document as _owned_document,
)
from tests.unit.services.test_question_asset_service import staged_case

owned_document = _owned_document


def semantic_case(owned):
    session, doc, teacher, root = owned
    doc.status = "Ready"
    question = Question(
        course_id=doc.course_id,
        created_by=teacher.id,
        type="SHORT_ANSWER",
        content="Newton condition",
        reference_answer="constant force",
        scoring_rubric="condition: 1 point",
        score=Decimal("1.00"),
        status=QuestionStatus.PENDING_REVIEW,
    )
    session.add(question)
    chunk = DocumentChunk(
        document_id=doc.id,
        course_id=doc.course_id,
        knowledge_base_id=doc.knowledge_base_id,
        chunk_index=0,
        content="Teaching evidence",
        chunk_metadata={"location": "page 1"},
    )
    session.add(chunk)
    session.commit()
    evidence = ValidationEvidence(
        evidence_id=uuid4(),
        kind="chunk",
        source_id=chunk.id,
        source_data={
            "chunk_id": str(chunk.id),
            "document_id": str(doc.id),
            "course_id": str(doc.course_id),
            "source_file": doc.original_filename,
            "location": "page 1",
            "content_snapshot": chunk.content,
        },
    )
    refs = ValidationInputRefs(
        fields=[
            "type",
            "content",
            "options",
            "reference_answer",
            "scoring_rubric",
            "analysis",
            "score",
        ],
        evidence=[evidence],
    )
    return ContentValidationService(session, root=root), question, refs


def result(refs, verdict="pass"):
    return ValidationOutput(
        checks=[
            ValidationCheck(
                kind=kind,
                verdict=verdict,
                reason="actual fixture check",
                evidence_refs=[refs.evidence[0].evidence_id],
            )
            for kind in (
                "answer_correctness",
                "condition_sufficiency",
                "option_ambiguity",
                "rubric_clarity",
            )
        ],
        issues=[],
    )


def start(service, question, refs, teacher):
    return service.start_validation(
        question.id,
        actor_id=teacher.id,
        input_refs=refs,
        executor_name="fixture_semantic_executor",
    )


def image_case(owned):
    session, _doc, teacher, root = owned
    assets, page, extracted = staged_case(owned)
    asset = assets.create_staged(
        extracted.id,
        AssetLinkRequest(
            file_id="p_" + page.id.hex,
            source_page_id=page.id,
            asset_type="diagram",
            region={"bbox": [10, 10, 40, 30]},
        ),
        actor_id=teacher.id,
    )
    return ContentValidationService(session, root=root), extracted, asset


def manual(view, asset, **changes):
    data = {
        "expected_context_revision": view.context_revision,
        "expected_run_no": view.run_no,
        "expected_check_no": view.check_no,
        "status": "confirmed",
        "confirmed_conditions": [
            {
                "asset_id": asset.id,
                "text": "Force points right",
                "evidence_region": {"bbox": [10, 10, 25, 20]},
            }
        ],
        "image_findings": [
            {
                "asset_id": asset.id,
                "finding": "conditions_confirmed",
                "reason": "Viewed the actual original crop",
            }
        ],
        "explanation": "Teacher checked the original crop",
    }
    data.update(changes)
    return ImageManualCheckRequest.model_validate(data)


def test_reports_release_start_transaction_and_new_running_blocks_old_pass(
    owned_document,
):
    session, _doc, teacher, _root = owned_document
    service, question, refs = semantic_case(owned_document)
    first = start(service, question, refs, teacher)
    assert first.outcome == "running" and first.checks is None
    done = service.finish_validation(first.id, actor_id=teacher.id, output=result(refs))
    assert done.outcome == "passed" and done.can_review
    second = start(service, question, refs, teacher)
    old = service.get_validation(question.id, first.id, actor_id=teacher.id)
    assert second.run_no == 2 and old.stale and not old.can_review
    assert len(service.list_validations(question.id, actor_id=teacher.id)) == 2
    assert session.scalar(select(QuestionRevisionComment)) is None


def test_late_failure_and_technical_error_never_change_current_content(owned_document):
    session, _doc, teacher, _root = owned_document
    service, question, refs = semantic_case(owned_document)
    first = start(service, question, refs, teacher)
    question.validation_revision += 1
    session.commit()
    late = service.finish_validation(
        first.id, actor_id=teacher.id, output=result(refs, "fail")
    )
    assert late.stale and question.status == QuestionStatus.PENDING_REVIEW
    second = start(service, question, refs, teacher)
    error = TechnicalError(
        code="PROVIDER_TIMEOUT",
        message="actual fixture timeout",
        stage="call",
        retryable=True,
    )
    failed = service.finish_validation(second.id, actor_id=teacher.id, error=error)
    assert (
        failed.outcome == "technical_error"
        and failed.checks is None
        and failed.issues is None
    )
    assert question.status == QuestionStatus.PENDING_REVIEW
    assert failed.provenance.model is None


def test_current_failed_report_and_retreat_are_atomic_no_fake_comment(
    owned_document, monkeypatch
):
    session, _doc, teacher, _root = owned_document
    service, question, refs = semantic_case(owned_document)
    report = start(service, question, refs, teacher)
    actual = session.commit

    def fail():
        raise SQLAlchemyError("fixture failed commit")

    monkeypatch.setattr(session, "commit", fail)
    with pytest.raises(ContentValidationError) as error:
        service.finish_validation(
            report.id, actor_id=teacher.id, output=result(refs, "fail")
        )
    assert error.value.code == "CONTENT_VALIDATION_PERSISTENCE_FAILED"
    monkeypatch.setattr(session, "commit", actual)
    session.expire_all()
    assert session.get(QuestionValidationResult, report.id).outcome == "running"
    assert question.status == QuestionStatus.PENDING_REVIEW
    done = service.finish_validation(
        report.id, actor_id=teacher.id, output=result(refs, "fail")
    )
    assert done.outcome == "failed" and question.status == QuestionStatus.NEEDS_REVISION
    assert session.scalar(select(QuestionRevisionComment)) is None


def test_disposition_appends_real_teacher_keeps_machine_results(owned_document):
    session, _doc, teacher, _root = owned_document
    service, question, refs = semantic_case(owned_document)
    first = start(service, question, refs, teacher)
    service.finish_validation(first.id, actor_id=teacher.id, output=result(refs))
    original = deepcopy(session.get(QuestionValidationResult, first.id).checks)
    payload = ManualDispositionRequest(
        check_kind="answer_correctness",
        action="provide_evidence",
        reason="Teacher actual explanation",
        evidence_refs=[refs.evidence[0].evidence_id],
    )
    view = service.dispose_validation(
        question.id, first.id, payload, actor_id=teacher.id
    )
    assert not view.can_review and view.outcome == "passed"
    event = view.manual_dispositions[0]
    assert (
        event.handled_by == teacher.id
        and event.handled_at.utcoffset().total_seconds() == 0
    )
    assert session.get(QuestionValidationResult, first.id).checks == original
    view = service.dispose_validation(
        question.id,
        first.id,
        ManualDispositionRequest(
            check_kind="answer_correctness",
            action="request_revision",
            reason="Actual required correction",
        ),
        actor_id=teacher.id,
    )
    assert question.status == QuestionStatus.NEEDS_REVISION
    comment = session.get(
        QuestionRevisionComment, view.manual_dispositions[-1].revision_comment_id
    )
    assert (
        comment.commented_by == teacher.id
        and comment.comment == "Actual required correction"
    )


def test_semantic_evidence_must_be_real_and_ended_reports_immutable(owned_document):
    _session, _doc, teacher, _root = owned_document
    service, question, refs = semantic_case(owned_document)
    forged = refs.model_copy(deep=True)
    forged.evidence[0].source_data["content_snapshot"] = "Invented teacher source"
    with pytest.raises(ContentValidationError) as error:
        start(service, question, forged, teacher)
    assert error.value.code == "CONTENT_SOURCE_INVALID"
    report = start(service, question, refs, teacher)
    service.finish_validation(report.id, actor_id=teacher.id, output=result(refs))
    with pytest.raises(ContentValidationError):
        service.finish_validation(
            report.id, actor_id=teacher.id, output=result(refs, "fail")
        )


def test_manual_without_vision_is_real_durable_and_stale_commands_rejected(
    owned_document,
):
    session, _doc, teacher, _root = owned_document
    service, extracted, asset = image_case(owned_document)
    view = service.get_image_assessment(
        "extracted_question", extracted.id, actor_id=teacher.id
    )
    payload = manual(view, asset)
    checked = service.manual_image_check(
        "extracted_question", extracted.id, payload, actor_id=teacher.id
    )
    assert (
        checked.status == "confirmed" and checked.run_no == 0 and checked.check_no == 1
    )
    assert (
        checked.current_check.teacher_id == teacher.id and checked.assessment.runs == []
    )
    session.expire_all()
    assert (
        service.get_image_assessment(
            "extracted_question", extracted.id, actor_id=teacher.id
        ).status
        == "confirmed"
    )
    with pytest.raises(ContentValidationError) as error:
        service.manual_image_check(
            "extracted_question", extracted.id, payload, actor_id=teacher.id
        )
    assert error.value.code == "IMAGE_ASSESSMENT_STALE"


def test_running_conflict_technical_error_and_independent_teacher_takeover(
    owned_document,
):
    _session, _doc, teacher, _root = owned_document
    service, extracted, asset = image_case(owned_document)
    run = service.start_image_run(
        "extracted_question", extracted.id, task="read condition", actor_id=teacher.id
    )
    with pytest.raises(ContentValidationError) as error:
        service.start_image_run(
            "extracted_question",
            extracted.id,
            task="another unknown call",
            actor_id=teacher.id,
        )
    assert error.value.code == "VISION_STATE_CONFLICT"
    prepared = service.prepare_image_run(
        "extracted_question", extracted.id, run.id, actor_id=teacher.id
    )
    assert prepared.images[0].width == 30 and prepared.images[0].height == 20
    assert "data" not in repr(prepared.images[0])
    pending = service.get_image_assessment(
        "extracted_question", extracted.id, actor_id=teacher.id
    )
    with pytest.raises(ContentValidationError):
        service.manual_image_check(
            "extracted_question",
            extracted.id,
            manual(pending, asset),
            actor_id=teacher.id,
        )
    failed = service.finish_image_run(
        "extracted_question",
        extracted.id,
        run.id,
        actor_id=teacher.id,
        error=TechnicalError(
            code="VISION_NOT_SUPPORTED",
            message="No configured capability",
            stage="preflight",
            retryable=False,
        ),
    )
    done = service.manual_image_check(
        "extracted_question", extracted.id, manual(failed, asset), actor_id=teacher.id
    )
    assert done.status == "confirmed" and done.current_run.outcome == "technical_error"
    assert done.current_run.provenance.model is None


def test_context_changed_keeps_global_machine_round_for_independent_check(
    owned_document,
):
    session, _doc, teacher, _root = owned_document
    service, extracted, asset = image_case(owned_document)
    run = service.start_image_run(
        "extracted_question", extracted.id, task="read", actor_id=teacher.id
    )
    from backend.app.schemas.image_assessment import advance_image_context

    extracted.image_assessment = advance_image_context(extracted.image_assessment)
    extracted.content = "Changed actual input"
    session.commit()
    late = service.finish_image_run(
        "extracted_question",
        extracted.id,
        run.id,
        actor_id=teacher.id,
        error=TechnicalError(
            code="CANCELLED",
            message="Actual late completion",
            stage="call",
            retryable=False,
        ),
    )
    assert late.current_run is None and late.run_no == 1
    checked = service.manual_image_check(
        "extracted_question", extracted.id, manual(late, asset), actor_id=teacher.id
    )
    assert checked.current_check.run_no == 1 and checked.current_check.run_id is None


def test_machine_and_prior_teacher_issues_require_explicit_resolution(owned_document):
    _session, _doc, teacher, _root = owned_document
    service, extracted, asset = image_case(owned_document)
    run = service.start_image_run(
        "extracted_question", extracted.id, task="read", actor_id=teacher.id
    )
    service.prepare_image_run(
        "extracted_question", extracted.id, run.id, actor_id=teacher.id
    )
    issue_id = uuid4()
    view = service.finish_image_run(
        "extracted_question",
        extracted.id,
        run.id,
        actor_id=teacher.id,
        result=ImageUnderstandingResult(
            observations=[],
            conditions=[],
            unresolved_issues=[
                {
                    "issue_id": issue_id,
                    "asset_ids": [asset.id],
                    "message": "Unreadable label",
                }
            ],
            requires_manual_review=True,
        ),
    )
    assert [issue.issue_id for issue in view.open_issues] == [issue_id]
    with pytest.raises(ContentValidationError) as error:
        service.manual_image_check(
            "extracted_question", extracted.id, manual(view, asset), actor_id=teacher.id
        )
    assert error.value.code == "CONTENT_SOURCE_INVALID"
    done = service.manual_image_check(
        "extracted_question",
        extracted.id,
        manual(
            view,
            asset,
            issue_resolutions=[
                {
                    "issue_id": issue_id,
                    "resolution": "resolved",
                    "reason": "Read original printed label",
                }
            ],
        ),
        actor_id=teacher.id,
    )
    assert done.status == "confirmed" and done.open_issues == []
    assert done.current_run.result.unresolved_issues[0].issue_id == issue_id


def test_whole_group_and_unknown_regions_are_not_accepted_as_success(owned_document):
    _session, _doc, teacher, _root = owned_document
    service, extracted, asset = image_case(owned_document)
    view = service.get_image_assessment(
        "extracted_question", extracted.id, actor_id=teacher.id
    )
    with pytest.raises(ContentValidationError) as error:
        service.manual_image_check(
            "extracted_question",
            extracted.id,
            manual(view, asset, image_findings=[]),
            actor_id=teacher.id,
        )
    assert error.value.http_status == 422


def test_binding_current_real_teacher_then_formal_native_check(owned_document):
    session, _doc, teacher, root = owned_document
    service, extracted, asset = image_case(owned_document)
    extracted.question_type = "SHORT_ANSWER"
    extracted.content = "Diagram question"
    extracted.reference_answer = "Force right"
    extracted.scoring_rubric = "Direction:1"
    extracted.score = Decimal("1.00")
    extracted.order_index = 1
    session.commit()
    view = service.get_image_assessment(
        "extracted_question", extracted.id, actor_id=teacher.id
    )
    service.manual_image_check(
        "extracted_question", extracted.id, manual(view, asset), actor_id=teacher.id
    )
    committed = QuestionCorrectionService(session, root=root).commit(
        extracted.paper_import_id, [extracted.id], actor_id=teacher.id
    )
    formal = service.get_image_assessment(
        "question", committed.questions[0].question_id, actor_id=teacher.id
    )
    assert formal.status == "confirmed" and formal.imported_review is not None
    assert formal.current_check.teacher_id == teacher.id and formal.check_no == 0
    native = service.manual_image_check(
        "question", formal.owner_id, manual(formal, asset), actor_id=teacher.id
    )
    assert native.status == "confirmed" and native.imported_review is None
    question = session.get(Question, formal.owner_id)
    assert (
        question.validation_revision == 1
        and question.image_assessment["imported_review"] is not None
    )


def test_missing_image_does_not_return_old_confirmed(owned_document):
    _session, _doc, teacher, _root = owned_document
    service, extracted, asset = image_case(owned_document)
    view = service.get_image_assessment(
        "extracted_question", extracted.id, actor_id=teacher.id
    )
    checked = service.manual_image_check(
        "extracted_question", extracted.id, manual(view, asset), actor_id=teacher.id
    )
    path, _ = service.files.download(asset.file_id, actor_id=teacher.id)
    path.unlink()
    missing = service.get_image_assessment(
        "extracted_question", extracted.id, actor_id=teacher.id
    )
    assert checked.status == "confirmed" and missing.status == "pending"
    assert missing.error.code == "FILE_MISSING" and not missing.evidence_readable


def test_missing_actual_required_inputs_never_register_machine_success(owned_document):
    session, _doc, teacher, _root = owned_document
    service, question, refs = semantic_case(owned_document)
    question.reference_answer = None
    session.commit()
    with pytest.raises(ContentValidationError) as error:
        start(service, question, refs, teacher)
    assert error.value.code == "CONTENT_INPUT_INCOMPLETE"
    assert session.scalar(select(QuestionValidationResult)) is None


@pytest.mark.parametrize("operation", ["start", "finish", "dispose"])
def test_production_autoflush_false_semantic_projection_keeps_pending_writes(
    owned_document, operation
):
    from backend.app.services.question_asset_service import QuestionAssetService
    from tests.unit.services.test_question_asset_service import png

    session, _doc, teacher, root = owned_document
    service, question, refs = semantic_case(owned_document)
    asset = QuestionAssetService(session, root=root).upload_question(
        question.id,
        content=png(),
        asset_type="figure",
        caption="actual fixture",
        actor_id=teacher.id,
    )
    view = service.get_image_assessment("question", question.id, actor_id=teacher.id)
    checked = service.manual_image_check(
        "question",
        question.id,
        manual(
            view,
            asset,
            confirmed_conditions=[
                {
                    "asset_id": asset.id,
                    "text": "actual condition",
                    "evidence_region": None,
                }
            ],
        ),
        actor_id=teacher.id,
    )
    image_evidence = ValidationEvidence(
        evidence_id=uuid4(),
        kind="question_asset",
        source_id=asset.id,
        source_data={
            "file_id": asset.file_id,
            "source_page_id": None,
            "region": None,
            "image_review_ref": {
                "owner_kind": "question",
                "owner_id": str(question.id),
                "check_id": str(checked.current_check.id),
                "binding_id": None,
            },
            "confirmed_conditions": [
                item.model_dump(mode="json") for item in checked.confirmed_conditions
            ],
        },
    )
    refs = refs.model_copy(update={"evidence": [*refs.evidence, image_evidence]})
    if operation != "start":
        report = start(service, question, refs, teacher)
        if operation == "dispose":
            service.finish_validation(
                report.id, actor_id=teacher.id, output=result(refs)
            )
    session.autoflush = False
    if operation == "start":
        returned = start(service, question, refs, teacher)
        assert returned.is_current and not returned.stale
        assert not session.in_transaction()
    elif operation == "finish":
        returned = service.finish_validation(
            report.id, actor_id=teacher.id, output=result(refs, "fail")
        )
        assert returned.outcome == "failed"
        assert not session.in_transaction()
        session.expire_all()
        assert (
            session.get(Question, question.id).status == QuestionStatus.NEEDS_REVISION
        )
    else:
        service.dispose_validation(
            question.id,
            report.id,
            ManualDispositionRequest(
                check_kind="answer_correctness",
                action="request_revision",
                reason="actual required correction",
            ),
            actor_id=teacher.id,
        )
        assert not session.in_transaction()
        session.expire_all()
        assert (
            session.get(Question, question.id).status == QuestionStatus.NEEDS_REVISION
        )


@pytest.mark.parametrize("operation", ["finish", "manual"])
def test_production_autoflush_false_image_projection_persists_actual_event(
    owned_document, operation
):
    session, _doc, teacher, _root = owned_document
    service, extracted, asset = image_case(owned_document)
    if operation == "finish":
        run = service.start_image_run(
            "extracted_question", extracted.id, task="read", actor_id=teacher.id
        )
    view = service.get_image_assessment(
        "extracted_question", extracted.id, actor_id=teacher.id
    )
    session.autoflush = False
    if operation == "finish":
        returned = service.finish_image_run(
            "extracted_question",
            extracted.id,
            run.id,
            actor_id=teacher.id,
            error=TechnicalError(
                code="VISION_PROVIDER_NOT_READY",
                message="actual unconfigured model",
                stage="preflight",
                retryable=False,
            ),
        )
        assert returned.current_run.outcome == "technical_error"
    else:
        returned = service.manual_image_check(
            "extracted_question", extracted.id, manual(view, asset), actor_id=teacher.id
        )
        assert returned.current_check is not None and returned.check_no == 1
    assert not session.in_transaction()
    session.expire_all()
    saved = service.get_image_assessment(
        "extracted_question", extracted.id, actor_id=teacher.id
    )
    if operation == "finish":
        assert saved.current_run.error.code == "VISION_PROVIDER_NOT_READY"
    else:
        assert (
            saved.current_check.teacher_id == teacher.id and saved.status == "confirmed"
        )
