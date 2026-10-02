"""T161 teacher UI producer callbacks, fresh data and session cleanup; TCR §16."""

import json
from uuid import uuid4

import gradio as gr
import pytest

from backend.app.ui import chapter_scope_loaders as loaders
from backend.app.ui.chapter_scope_view import create_chapter_scope_view


def build_view():
    with gr.Blocks() as app:
        course = gr.Textbox()
        kb = gr.Textbox()
        state = gr.State({})
        view = create_chapter_scope_view(course, kb, state)
    return app, view


def callback(app, name):
    return next(
        block.fn for block in app.fns.values() if block.fn and block.fn.__name__ == name
    )


def test_unknown_vs_confirmed_empty_tags_and_independent_location(monkeypatch):
    app, _view = build_view()
    captured = []
    row = {
        "metadata": {"knowledge_points": []},
        "chapter_id": str(uuid4()),
        "section_order": None,
    }
    monkeypatch.setattr(
        loaders, "confirm", lambda *args: captured.append(args[4]) or row
    )
    monkeypatch.setattr(loaders, "chunks", lambda *args: [row])
    write = (
        callback(app, "write_scope")
        if any(
            block.fn and block.fn.__name__ == "write_scope"
            for block in app.fns.values()
        )
        else None
    )
    # Button closures use one writer with independently submitted dimensions.
    actions = [
        block.fn
        for block in app.fns.values()
        if block.fn and block.fn.__name__ == "action"
    ]
    args = ("course", "kb", "doc", "chunk", row["chapter_id"], None, "[]", {})
    actions[0](*args)
    actions[2](*args)
    actions[3](*args)
    assert captured == [
        {"chapter_id": row["chapter_id"], "section_order": None},
        {"knowledge_points": []},
        {"knowledge_points": None},
    ]
    assert write is None


def test_chunk_editor_displays_full_evidence_and_reset_clears_dynamic_choices():
    app, view = build_view()
    identity = str(uuid4())
    content = "完整正文" * 600
    result = callback(app, "chunk_fields")(
        identity,
        [
            {
                "id": identity,
                "content": content,
                "chapter_id": None,
                "section_order": None,
                "metadata": {"knowledge_points": None, "heading_path": ["章", "节"]},
            }
        ],
        [],
    )
    assert result[0] == content
    assert json.loads(result[1])["heading_path"] == ["章", "节"]
    assert result[4] == "null"
    reset = view.reset()
    dropdowns = [component for component in reset if isinstance(component, gr.Dropdown)]
    assert len(dropdowns) == 5
    assert all(
        reset[component]["choices"] == [] and reset[component]["value"] is None
        for component in dropdowns
    )
    assert all(
        reset[component]["value"] == []
        for component in reset
        if isinstance(component, gr.State)
    )


def test_reopening_chunk_reads_fresh_records_and_bad_split_is_visible(monkeypatch):
    app, _view = build_view()
    versions = [
        [{"id": "old", "chunk_index": 0}],
        [{"id": "new", "chunk_index": 0}],
    ]
    monkeypatch.setattr(loaders, "chunks", lambda *args: versions.pop(0))
    read = callback(app, "load_chunks")
    assert read("course", "kb", "doc", {})[1][0]["id"] == "old"
    assert read("course", "kb", "doc", {})[1][0]["id"] == "new"
    with pytest.raises(gr.Error):
        callback(app, "run_split")("course", "kb", "doc", "{}", {})


def test_title_only_choice_is_explicit_and_reset_for_other_chapter(monkeypatch):
    app, view = build_view()
    captured = []

    def save(*args, **kwargs):
        captured.append(kwargs["preserve_section_meaning"])
        return {"title": "corrected"}

    monkeypatch.setattr(loaders, "save_chapter", save)
    callback(app, "save_directory")("course", "chapter", "corrected", "[]", {}, True)
    assert captured == [True]
    fields = callback(app, "chapter_fields")(
        "chapter", [{"id": "chapter", "title": "corrected", "sections": []}]
    )
    assert fields == ("corrected", "[]", False)
    assert all(
        value["value"] is False
        for component, value in view.reset().items()
        if isinstance(component, gr.Checkbox)
    )


def test_document_change_clears_source_and_cut_plan_and_resplit_clears_old_chunks(
    monkeypatch,
):
    from types import SimpleNamespace

    from backend.app.domain.enums import DocumentStatus
    from backend.app.services.knowledge_base_service import DocumentIngestionResult

    app, _view = build_view()
    monkeypatch.setattr(
        loaders, "chunks", lambda *args: [{"id": "B", "chunk_index": 0}]
    )
    assert callback(app, "load_chunks")("course", "kb", "B", {})[-2:] == ("", "[]")
    monkeypatch.setattr(
        loaders,
        "resplit",
        lambda *args: DocumentIngestionResult(
            document_id="B",
            status=DocumentStatus.FAILED,
            error_code="EMBEDDING_NOT_READY",
            error_message="provider unavailable",
        ),
    )
    monkeypatch.setattr(
        loaders,
        "list_context",
        lambda *args: (
            [],
            [
                SimpleNamespace(
                    id="B", original_filename="B.md", status=DocumentStatus.FAILED
                )
            ],
        ),
    )
    updates = callback(app, "run_split")(
        "course", "kb", "B", '[{"section_index":1,"cut_points":[2]}]', {}
    )
    labels = {
        getattr(component, "label", None): value for component, value in updates.items()
    }
    assert labels["核对片段（全部片段）"]["value"] is None
    assert labels["清洗原稿段落（全文与 section_index）"]["value"] == ""
    assert any(
        "重切分失败：EMBEDDING_NOT_READY" in value
        for value in updates.values()
        if isinstance(value, str)
    )
    assert labels["核对教学资料"]["choices"] == [("B.md · Failed", "B")]
