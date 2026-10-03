"""T167 real report presentation and authority boundary; TCR v2 section 20."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from backend.app.domain.enums import QuestionStatus
from backend.app.schemas.content_validation import ValidationReportView
from backend.app.ui.question_review_view import (
    approval_enabled,
    question_details_html,
    semantic_history_html,
    semantic_report_html,
)

ID = UUID("00000000-0000-0000-0000-000000000001")


def report(**changes):
    value = {
        "id": ID,
        "question_id": ID,
        "input_revision": 2,
        "run_no": 3,
        "outcome": "passed",
        "input_refs": {"fields": ["content", "reference_answer"]},
        "checks": [
            {"kind": kind, "verdict": "pass", "reason": "Real checked text"}
            for kind in (
                "answer_correctness",
                "condition_sufficiency",
                "option_ambiguity",
                "rubric_clarity",
            )
        ],
        "issues": [],
        "error": None,
        "executor_kind": "agent",
        "executor_name": "semantic_validator",
        "requested_by": None,
        "agent_run_id": None,
        "provenance": {
            "provider_name": "actual-provider",
            "model": "actual-response-model",
            "prompt_version": "semantic-v2",
        },
        "manual_dispositions": [],
        "created_at": datetime(2026, 10, 3, tzinfo=UTC),
        "completed_at": datetime(2026, 10, 3, tzinfo=UTC),
        "is_current": True,
        "stale": False,
        "can_review": True,
        "requires_manual_review": False,
    }
    value.update(changes)
    return ValidationReportView.model_validate(value)


def test_displays_each_real_check_and_actual_execution_source():
    html = semantic_report_html(report())
    assert html.count("Real checked text") == 4
    assert "actual-provider" in html
    assert "actual-response-model" in html
    assert "semantic-v2" in html
    assert "3" in html and "2" in html


def test_technical_error_preserves_null_checks_and_actual_error():
    html = semantic_report_html(
        report(
            outcome="technical_error",
            checks=None,
            issues=None,
            can_review=False,
            error={
                "code": "PROVIDER_TIMEOUT",
                "message": "actual timeout",
                "stage": "provider_call",
                "retryable": True,
            },
        )
    )
    assert "PROVIDER_TIMEOUT" in html and "actual timeout" in html
    assert "provider_call" in html
    assert "Real checked text" not in html
    assert "未产生合法分项结果" in html


def test_history_labels_old_pass_as_stale_and_does_not_offer_approval():
    html = semantic_report_html(report(is_current=False, stale=True, can_review=False))
    assert "过期" in html
    assert "当前可批准" not in html
    assert "当前不可批准" in html


def test_unchecked_history_is_unknown_and_does_not_invent_pass():
    html = semantic_history_html([])
    assert "核验未知" in html
    assert "通过" not in html


def test_report_text_is_escaped_without_hiding_actual_issue():
    html = semantic_report_html(
        report(
            issues=[
                {
                    "issue_id": ID,
                    "code": "CONDITION_MISSING",
                    "field": "content",
                    "severity": "error",
                    "message": "<script>alert(1)</script>",
                }
            ]
        )
    )
    assert "CONDITION_MISSING" in html
    assert "&lt;script&gt;" in html
    assert "<script>" not in html


@pytest.mark.parametrize("can_review", [None, False, True])
def test_approval_uses_explicit_service_gate(can_review):
    detail = SimpleNamespace(status=QuestionStatus.PENDING_REVIEW)
    if can_review is not None:
        detail.can_review = can_review
    assert approval_enabled(detail) is (can_review is True)


def test_pending_status_alone_is_not_an_approval_gate():
    assert not approval_enabled({"status": "Pending Review"})
    assert approval_enabled({"status": "Pending Review", "can_review": True})


def test_detail_displays_actual_parent_paper_and_preserved_teaching_snapshot():
    html = question_details_html(
        SimpleNamespace(
            source_type="adapted",
            analysis="Actual analysis <b>",
            source_status="persisted",
            sources=[
                {
                    "source_file": "book.pdf",
                    "chunk_index": 4,
                    "content_snapshot": "Actual old text",
                    "source_deleted": True,
                    "location_snapshot": None,
                }
            ],
            parent_sources=[
                {"source_question_id": "parent-question", "adaptation_type": "rewrite"}
            ],
            paper_source={
                "paper_import_id": "import-id",
                "source_page_ids": ["real-page-id"],
                "question_number": "Q4",
            },
            current_validation=report(),
            image_assessment=None,
        )
    )
    for actual in (
        "book.pdf",
        "Actual old text",
        "parent-question",
        "import-id",
        "real-page-id",
        "Q4",
    ):
        assert actual in html
    assert "Actual analysis &lt;b&gt;" in html
    assert "历史位置未知" in html
    assert "来源已删除" in html


def test_detail_unknown_history_does_not_reconstruct_location_or_teaching_source():
    html = question_details_html(
        SimpleNamespace(source_status="history_unknown", analysis=None)
    )
    assert "历史教学来源未知" in html
    assert "尚未提供解析" in html
    assert "历史核验未知" in html


from unittest.mock import Mock

import gradio as gr

from backend.app.schemas.image_assessment import ImageAssessmentView
from backend.app.services.content_validation_service import ContentValidationError
from backend.app.ui import question_review_view as review_module


def image_view():
    return ImageAssessmentView.model_validate(
        {
            "owner_kind": "question",
            "owner_id": ID,
            "assessment": None,
            "context_revision": 2,
            "run_no": 1,
            "check_no": 0,
            "input_refs": {
                "text_fields": ["content"],
                "images": [
                    {
                        "asset_id": ID,
                        "file_id": "question_asset:" + str(ID),
                        "image_index": 1,
                        "asset_type": "figure",
                        "source_page_id": None,
                        "region": None,
                        "width": 16,
                        "height": 16,
                        "mime_type": "image/png",
                    }
                ],
            },
            "current_run": None,
            "current_check": None,
            "imported_review": None,
            "status": "pending",
            "confirmed_conditions": [],
            "requires_manual_review": True,
            "evidence_readable": True,
        }
    )


@pytest.fixture
def review_panel():
    with gr.Blocks() as app:
        selected = gr.State(str(ID))
        state = gr.State(
            {"access_token": "token", "user_id": str(ID), "roles": ["Teacher"]}
        )
        approve = gr.Button("Approve")
        panel = review_module.create_question_review_panel(
            selected, state, approval_button=approve
        )
    return app, panel, approve


def registered(app, name):
    return next(
        fn.fn for fn in app.fns.values() if getattr(fn.fn, "__name__", "") == name
    )


def check_args(*, editing=False):
    return [
        str(ID),
        image_view().model_dump(mode="json"),
        "confirmed",
        [["1", "无需额外条件", "Decorative source image"]],
        [],
        [],
        [],
        "Actual teacher explanation",
        {"user_id": str(ID)},
        editing,
    ]


def test_manual_check_uses_displayed_revision_and_refreshes_approval_gate(
    review_panel, monkeypatch
):
    app, _panel, approve = review_panel
    check = Mock()
    monkeypatch.setattr(review_module.loaders, "check_images", check)
    monkeypatch.setattr(
        review_module.loaders,
        "detail",
        lambda *args: SimpleNamespace(
            status=QuestionStatus.PENDING_REVIEW,
            can_review=False,
            image_assessment=image_view(),
            sources=[],
            current_validation=None,
        ),
    )
    monkeypatch.setattr(
        review_module.loaders,
        "image_html",
        lambda *args: '<img alt="actual authorized bytes" />',
    )
    result = registered(app, "save_check")(*check_args())
    assert check.call_args.args[1]["expected_context_revision"] == 2
    assert check.call_args.args[1]["expected_run_no"] == 1
    assert check.call_args.args[1]["expected_check_no"] == 0
    assert result[approve]["interactive"] is False
    assert any("保存真实题图核对" in str(value) for value in result.values())


def test_stale_manual_check_preserves_teacher_input_and_does_not_reload_before_write(
    review_panel, monkeypatch
):
    app, _panel, _ = review_panel
    check = Mock(
        side_effect=ContentValidationError(
            "CONTENT_VALIDATION_STALE", "Actual stale revision"
        )
    )
    detail = Mock()
    monkeypatch.setattr(review_module.loaders, "check_images", check)
    monkeypatch.setattr(review_module.loaders, "detail", detail)
    result = registered(app, "save_check")(*check_args())
    detail.assert_not_called()
    assert len(result) == 1
    assert "Actual stale revision" in next(iter(result.values()))


def test_image_commands_reject_unsaved_editor_without_issuing_service_call(
    review_panel, monkeypatch
):
    app, _, _ = review_panel
    check = Mock()
    monkeypatch.setattr(review_module.loaders, "check_images", check)
    result = registered(app, "save_check")(*check_args(editing=True))
    check.assert_not_called()
    assert "先保存题目文本" in next(iter(result.values()))


def test_current_report_gate_enables_approval_but_editing_still_blocks_it(review_panel):
    _, panel, approve = review_panel
    detail = SimpleNamespace(
        status=QuestionStatus.PENDING_REVIEW,
        can_review=True,
        current_validation=report(),
        image_assessment=None,
    )
    assert panel.render(detail)[approve]["interactive"] is True
    assert panel.render(detail, editing=True)[approve]["interactive"] is False


def test_teacher_disposition_uses_report_identity_and_real_evidence_then_refreshes_gate(
    review_panel, monkeypatch
):
    app, _panel, approve = review_panel
    proof = {
        "evidence_id": ID,
        "kind": "chunk",
        "source_id": ID,
        "source_data": {
            "chunk_id": str(ID),
            "document_id": str(ID),
            "course_id": str(ID),
            "source_file": "book.pdf",
            "location": None,
            "content_snapshot": "Real teaching text",
        },
    }
    current = report(input_refs={"fields": ["content"], "evidence": [proof]})
    dispose = Mock()
    monkeypatch.setattr(review_module.loaders, "dispose_validation", dispose)
    monkeypatch.setattr(
        review_module.loaders,
        "detail",
        lambda *args: SimpleNamespace(
            status=QuestionStatus.PENDING_REVIEW,
            can_review=False,
            current_validation=current.model_copy(update={"can_review": False}),
            image_assessment=None,
        ),
    )
    result = registered(app, "save_teacher_disposition")(
        str(ID),
        current.model_dump(mode="json"),
        "check:answer_correctness",
        "provide_evidence",
        [str(ID)],
        "Real teacher reason",
        {"user_id": str(ID)},
        False,
    )
    assert dispose.call_args.args[1] == str(ID)
    sent = dispose.call_args.args[2]
    assert sent["check_kind"] == "answer_correctness"
    assert sent["evidence_refs"] == [str(ID)]
    assert sent["reason"] == "Real teacher reason"
    assert result[approve]["interactive"] is False


def test_failed_disposition_preserves_form_and_actual_error(review_panel, monkeypatch):
    app, _, _ = review_panel
    dispose = Mock(
        side_effect=ContentValidationError(
            "CONTENT_VALIDATION_STALE", "Actual report changed"
        )
    )
    detail = Mock()
    monkeypatch.setattr(review_module.loaders, "dispose_validation", dispose)
    monkeypatch.setattr(review_module.loaders, "detail", detail)
    result = registered(app, "save_teacher_disposition")(
        str(ID),
        report().model_dump(mode="json"),
        "check:answer_correctness",
        "request_revision",
        [],
        "Real reason",
        {},
        False,
    )
    detail.assert_not_called()
    assert len(result) == 1
    assert "Actual report changed" in next(iter(result.values()))


def test_teaching_picker_displays_real_file_position_and_content(
    review_panel, monkeypatch
):
    app, panel, _ = review_panel
    chunks = [
        {
            "id": str(ID),
            "source_file": "teacher-book.pdf",
            "chunk_index": 7,
            "content": "Actual teaching text <b>",
            "location": {"page": 4},
            "chapter_id": None,
            "section_order": None,
        }
    ]
    monkeypatch.setattr(review_module.loaders, "teaching_chunks", lambda *args: chunks)
    loaded = registered(app, "load_teaching_basis")(str(ID), {})
    assert any(
        "teacher-book.pdf" in str(value) and "Actual teaching text" in str(value)
        for value in loaded.values()
    )
    chosen = registered(app, "select_teaching_basis")([str(ID)], chunks)
    assert chosen[panel.teaching_chunk_ids] == [str(ID)]
    assert any(
        "Actual teaching text &lt;b&gt;" in str(value) for value in chosen.values()
    )


def test_explicit_empty_basis_is_not_changed_into_reuse(review_panel):
    app, panel, _ = review_panel
    result = registered(app, "select_teaching_basis")([], [])
    assert result[panel.teaching_chunk_ids] == []
    assert panel.clear()[panel.teaching_chunk_ids] is None


def test_validation_loader_passes_real_selection_and_keeps_none_distinct(monkeypatch):
    import asyncio
    from contextlib import contextmanager
    from unittest.mock import AsyncMock

    from backend.app.ui import question_review_loaders as review_loaders

    @contextmanager
    def scoped(state):
        yield Mock(), SimpleNamespace(id=ID)

    runner = AsyncMock()
    monkeypatch.setattr(review_loaders, "_scope", scoped)
    monkeypatch.setattr(
        review_loaders,
        "ContentValidationService",
        lambda *args, **kwargs: SimpleNamespace(validate_current=runner),
    )
    asyncio.run(review_loaders.run_validation(str(ID), {}, [str(ID)]))
    assert runner.call_args.kwargs["teaching_chunk_ids"] == [ID]
    asyncio.run(review_loaders.run_validation(str(ID), {}))
    assert runner.call_args.kwargs["teaching_chunk_ids"] is None
    asyncio.run(review_loaders.run_validation(str(ID), {}, []))
    assert runner.call_args.kwargs["teaching_chunk_ids"] == []


def test_original_page_preview_uses_actual_authorized_source_page(
    review_panel, monkeypatch
):
    app, _, _ = review_panel
    preview = Mock(return_value='<img alt="actual original page" />')
    monkeypatch.setattr(review_module.loaders, "source_page_html", preview)
    result = registered(app, "show_source_page")(str(ID), str(ID), {})
    preview.assert_called_once_with(str(ID), str(ID), {})
    assert any("actual original page" in str(value) for value in result.values())


def test_teaching_loader_only_exposes_ready_material_in_question_course(monkeypatch):
    from contextlib import contextmanager

    from backend.app.domain.enums import DocumentStatus
    from backend.app.ui import question_review_loaders as review_loaders

    @contextmanager
    def scoped(state):
        yield Mock(), SimpleNamespace(id=ID)

    questions = SimpleNamespace(
        get_question=Mock(return_value=SimpleNamespace(course_id=str(ID)))
    )
    knowledge = SimpleNamespace(
        list_documents=Mock(
            return_value=[
                SimpleNamespace(
                    id="ready-doc",
                    original_filename="ready-book.pdf",
                    status=DocumentStatus.READY,
                ),
                SimpleNamespace(
                    id="pending-doc",
                    original_filename="pending-book.pdf",
                    status=DocumentStatus.PARSING,
                ),
            ]
        ),
        list_document_chunks=Mock(
            return_value=[
                {
                    "id": str(ID),
                    "chunk_index": 5,
                    "content": "Real book text",
                    "metadata": {},
                }
            ]
        ),
    )
    monkeypatch.setattr(review_loaders, "_scope", scoped)
    monkeypatch.setattr(review_loaders, "QuestionService", lambda session: questions)
    monkeypatch.setattr(
        review_loaders, "KnowledgeBaseService", lambda session: knowledge
    )
    actual = review_loaders.teaching_chunks(str(ID), {})
    knowledge.list_documents.assert_called_once_with(course_id=str(ID), teacher_id=ID)
    knowledge.list_document_chunks.assert_called_once_with("ready-doc", teacher_id=ID)
    assert actual[0]["source_file"] == "ready-book.pdf"
    assert actual[0]["location"] is None
    assert actual[0]["content"] == "Real book text"


def test_source_page_loader_rejects_unrelated_page_before_file_read(monkeypatch):
    from contextlib import contextmanager

    from backend.app.ui import question_review_loaders as review_loaders

    @contextmanager
    def scoped(state):
        yield Mock(), SimpleNamespace(id=ID)

    files = Mock()
    monkeypatch.setattr(review_loaders, "_scope", scoped)
    monkeypatch.setattr(
        review_loaders,
        "get_question",
        lambda *args: SimpleNamespace(
            paper_source={"source_page_ids": [str(ID)], "paper_import_id": str(ID)}
        ),
    )
    monkeypatch.setattr(review_loaders, "FileStorageService", files)
    with pytest.raises(ValueError, match="实际来源原页"):
        review_loaders.source_page_html(
            str(ID), "00000000-0000-0000-0000-000000000002", {}
        )
    files.assert_not_called()
