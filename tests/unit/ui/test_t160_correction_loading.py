"""T160 request-local correction reads and current authorization. TCR §19."""

from uuid import UUID

import gradio as gr
import pytest

from backend.app.services.file_storage_service import FileStorageError
from backend.app.services.paper_import_service import PaperImportService
from backend.app.ui import paper_import_loaders as loaders
from tests.contract.test_question_correction_api import files_api as _files_api
from tests.contract.test_question_correction_api import pending
from tests.unit.ui.test_paper_import_view import (
    _callback,
    _editor,
    _second_source_page,
    _value,
    connect,
    state,
)

files_api = _files_api


def count_full_reads(monkeypatch):
    calls = []
    original = PaperImportService.get

    def counted(service, identity, *, actor_id):
        calls.append(identity)
        return original(service, identity, actor_id=actor_id)

    monkeypatch.setattr(PaperImportService, "get", counted)
    return calls


def test_question_reload_reads_one_current_paper_without_cross_request_cache(
    monkeypatch, files_api
):
    connect(monkeypatch, files_api)
    paper = pending(files_api, 2)
    identity, qid = paper["id"], paper["questions"][1]["id"]
    current = state(files_api)
    _app, editor, components = _editor()
    calls = count_full_reads(monkeypatch)

    first = editor.reload(identity, qid, current)
    assert _value(first[components["原题号"]]) == "2"
    assert len(calls) == 1

    loaders.patch(identity, qid, {"analysis": "新的持久解析"}, current)
    calls.clear()
    reopened = editor.reload(identity, qid, current)
    assert _value(reopened[components["解析（未知留空）"]]) == "新的持久解析"
    assert len(calls) == 1


def test_page_switch_reads_selected_page_without_loading_whole_import(
    monkeypatch, files_api
):
    connect(monkeypatch, files_api)
    paper = pending(files_api, 2)
    second = _second_source_page(files_api, paper)
    app, _editor_view, _components = _editor()
    calls = count_full_reads(monkeypatch)

    image, facts = _callback(app, "show_page")(paper["id"], second, state(files_api))
    assert "原卷第 2 页" in image and "data:image/png;base64," in image
    assert "像素" in facts
    assert calls == []


def test_selected_page_keeps_import_membership_and_current_teacher_authorization(
    monkeypatch, files_api
):
    connect(monkeypatch, files_api)
    paper = pending(files_api, 1)
    other = pending(files_api, 1)
    current = state(files_api)
    with pytest.raises(ValueError, match="当前导入"):
        loaders.page_image(paper["id"], other["pages"][0]["id"], current)
    from backend.app.domain.permissions import PermissionDeniedError

    with pytest.raises(PermissionDeniedError):
        loaders.page_image(
            paper["id"], paper["pages"][0]["id"],
            current | {"user_id": str(files_api[4][1].id)},
        )
    other_user = files_api[4][1]
    other_token = files_api[5][1]["Authorization"][7:]
    with pytest.raises((PermissionDeniedError, FileStorageError)):
        loaders.page_image(
            paper["id"], paper["pages"][0]["id"],
            {"user_id": str(other_user.id), "access_token": other_token},
        )


def test_page_file_disappearance_is_reported_after_an_earlier_success(
    monkeypatch, files_api
):
    connect(monkeypatch, files_api)
    paper = pending(files_api, 1)
    current = state(files_api)
    identity, pid = paper["id"], paper["pages"][0]["id"]
    assert "data:image/png;base64," in loaders.page_image(identity, pid, current)
    path, _ = files_api[2].download("p_" + UUID(pid).hex, actor_id=files_api[4][0].id)
    path.unlink()
    with pytest.raises(FileStorageError) as error:
        loaders.page_image(identity, pid, current)
    assert error.value.code == "FILE_MISSING"
    app, _view, _components = _editor()
    with pytest.raises(gr.Error):
        _callback(app, "show_page")(identity, pid, current)


def test_navigation_subscribes_once_to_value_changes_for_mouse_and_keyboard():
    """Gradio single-select emits input again on blur; change covers keyboard too."""
    app, _view, components = _editor()
    for label in ("待校正题目", "查看原页"):
        identity = components[label]._id
        targets = [
            event_name
            for function in app.fns.values()
            for component_id, event_name in function.targets
            if component_id == identity
        ]
        assert targets == ["change"]
