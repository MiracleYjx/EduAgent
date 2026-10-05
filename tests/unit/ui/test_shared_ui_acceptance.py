"""T185 status semantics and actual correction callbacks, TCR §40."""

import gradio as gr

from backend.app.domain.enums import ExtractedQuestionStatus, QuestionStatus
from backend.app.ui.layout_view import UiStatus, status_banner
from tests.unit.ui import test_paper_import_view as paper

files_api = paper.files_api


def test_six_business_states_remain_distinct_and_escape_actual_error():
    cases = [
        (ExtractedQuestionStatus.PENDING_CORRECTION, "extracted_question", "待校正"),
        (UiStatus.PENDING_COMPLETION, "ui", "待补全"),
        (QuestionStatus.PENDING_REVIEW, "question", "待教师审核"),
        (QuestionStatus.APPROVED, "question", "审核通过"),
        (UiStatus.PENDING_REVIEW, "ui", "待人工复核"),
        (UiStatus.FAILED, "ui", "处理失败"),
    ]
    banners = [
        status_banner(value, entity=entity, detail="<script>原始错误</script>")
        for value, entity, _ in cases
    ]
    for banner, (_, _, label) in zip(banners, cases, strict=True):
        assert (
            label in banner
            and "<script>" not in banner
            and "&lt;script&gt;原始错误" in banner
        )
    assert len(set(banners)) == 6 and 'role="alert"' in banners[-1]


def test_correction_explicit_edit_cancel_and_save_use_persisted_values(
    monkeypatch, files_api
):
    paper.connect(monkeypatch, files_api)
    record = paper.pending(files_api, 1)
    identity, qid = record["id"], record["questions"][0]["id"]
    current = paper.state(files_api)
    app, view, components = paper._editor()
    by_id = {getattr(c, "elem_id", None): c for c in app.blocks.values()}
    editor, detail = by_id["edu-paper-editor"], by_id["edu-paper-detail"]
    loaded = view.reload(identity, qid, current)
    assert loaded[editor]["visible"] is False and loaded[detail]["visible"] is True
    opened = paper._callback(app, "begin_edit")(identity, qid, current)
    assert opened[editor]["visible"] is True and opened[detail]["visible"] is False
    from backend.app.ui import paper_import_loaders as loaders

    before = loaders.load(identity, current).questions[0].model_dump(mode="json")
    cancelled = paper._callback(app, "cancel_edit")(identity, qid, current)
    assert (
        cancelled[editor]["visible"] is False and cancelled[detail]["visible"] is True
    )
    assert paper._value(cancelled[components["题干"]]) == before["content"]
    assert (
        loaders.load(identity, current).questions[0].model_dump(mode="json") == before
    )
    saved = paper._save_form(
        app,
        view,
        components,
        identity,
        qid,
        current,
        **{"解析（未知留空）": "T185受控校正保存"},
    )
    assert saved[editor]["visible"] is False and saved[detail]["visible"] is True
    assert "T185受控校正保存" in paper._value(saved[detail])
    assert loaders.load(identity, current).questions[0].analysis == "T185受控校正保存"
    assert any(
        isinstance(c, gr.HTML) and "data:image/png" in str(paper._value(v))
        for c, v in saved.items()
    )
