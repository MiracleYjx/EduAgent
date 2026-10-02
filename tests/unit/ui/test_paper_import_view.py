"""T159 callback and Gradio wiring tests, backed by the import contract fixtures. TCR §13."""

from types import SimpleNamespace

import gradio as gr
import pytest

from backend.app.ui import paper_correction_view as corrections
from backend.app.ui import paper_import_view as imports
from backend.app.ui.layout_view import is_authorized_navigation
from tests.contract.test_question_correction_api import files_api as _files_api
from tests.contract.test_question_correction_api import pending

files_api = _files_api


def state(files_api):
    _client, _session, _files, _doc, users, headers = files_api
    return {
        "access_token": headers[0]["Authorization"][7:],
        "roles": ["Teacher"],
        "user_id": str(users[0].id),
    }


def connect(monkeypatch, files_api):
    from sqlalchemy.orm import sessionmaker

    from backend.app.ui import paper_import_loaders as loaders

    client, session, *_ = files_api
    monkeypatch.setattr(
        loaders,
        "get_session_factory",
        lambda: sessionmaker(bind=session.get_bind(), expire_on_commit=False),
    )
    monkeypatch.setattr(loaders, "get_settings", lambda: client.app.state.settings)


def test_real_loader_save_reopen_and_inline_authorized_original(monkeypatch, files_api):
    connect(monkeypatch, files_api)
    paper = pending(files_api, 1)
    from backend.app.ui import paper_import_loaders as loaders

    current = state(files_api)
    loaded = loaders.load(paper["id"], current)
    qid = loaded.questions[0].id
    saved = loaders.patch(
        paper["id"],
        str(qid),
        {"analysis": "重新打开仍可见", "score": "5", "assets": []},
        current,
    )
    assert saved.analysis == "重新打开仍可见"
    assert loaders.load(paper["id"], current).questions[0].analysis == "重新打开仍可见"
    html = loaders.page_image(paper["id"], str(loaded.pages[0].id), current)
    assert (
        "data:image/png;base64," in html and "/file=" not in html and "D:" not in html
    )
    foreign = current | {"user_id": str(files_api[4][1].id)}
    from backend.app.domain.permissions import PermissionDeniedError

    with pytest.raises(PermissionDeniedError):
        loaders.load(paper["id"], foreign)


def test_boundary_options_form_preserves_unknown_and_explicit_empty():
    paper = SimpleNamespace(
        pages=[
            SimpleNamespace(page_number=1, id="one"),
            SimpleNamespace(page_number=2, id="two"),
        ]
    )
    assert corrections.options_from_rows(
        [["B", "选项二"], ["A", "选项一"]], "标签选项"
    ) == {"B": "选项二", "A": "选项一"}
    assert corrections.options_from_rows(
        [["", "文本二"], ["", "文本一"]], "文本选项"
    ) == ["文本二", "文本一"]
    assert corrections.regions_from_rows([], paper, unknown=True) is None
    assert corrections.regions_from_rows([], paper, unknown=False) == []
    assert corrections.regions_from_rows(
        [[2, 0, 0, 100, 80]], paper, unknown=False
    ) == [{"source_page_id": "two", "bbox": [0, 0, 100, 80]}]
    with pytest.raises(ValueError):
        corrections.regions_from_rows([[99, 0, 0, 100, 80]], paper, unknown=False)
    with pytest.raises(ValueError):
        corrections.options_from_rows([["A", "first"], ["A", "second"]], "标签选项")


def test_view_mounts_source_correction_visibility_and_explicit_batch():
    assert is_authorized_navigation("teacher.paper_import", ["Teacher"])
    assert not is_authorized_navigation("teacher.paper_import", ["Student"])
    with gr.Blocks() as demo:
        view = imports.create_paper_import_view(gr.State({}))
    assert view.panel.visible is False
    labels = {block.label for block in demo.blocks.values() if hasattr(block, "label")}
    assert {
        "原试卷",
        "试卷导入记录",
        "本题来源页（可跨页）",
        "学生可见（已核对不含答案）",
        "确认入库的题目",
    } <= labels
    buttons = {
        block.value for block in demo.blocks.values() if isinstance(block, gr.Button)
    }
    assert {"保存校正", "拒绝此题", "确认所选题入库", "刷新进度 / 重新读取"} <= buttons
    callbacks = [f for f in demo.fns.values() if f.fn is not None]
    assert callbacks and all(
        any(isinstance(x, gr.State) for x in f.inputs) for f in callbacks
    )


# T159 behavior proofs use registered nested callbacks and the real services. TCR §15.


def _callback(app, name):
    matches = [
        block_fn.fn
        for block_fn in app.fns.values()
        if block_fn.fn is not None and getattr(block_fn.fn, "__name__", None) == name
    ]
    assert len(matches) == 1, name
    return matches[0]


def _editor():
    with gr.Blocks() as app:
        editor = corrections.create_paper_correction_view(
            gr.Dropdown(label="测试导入"), gr.State({})
        )
    components = {
        component.label: component
        for component in app.blocks.values()
        if getattr(component, "label", None)
    }
    return app, editor, components


def _value(update):
    return update["value"] if isinstance(update, dict) else update


def _save_form(
    app, editor, components, identity, question_id, current_state, **changes
):
    loaded = editor.reload(identity, question_id, current_state)
    labels = [
        "原题号",
        "导入内题序",
        "题型",
        "题干",
        "选项形式",
        "选项",
        "分值",
        "知识点（每行一个）",
        "参考答案（未知留空）",
        "评分标准（未知留空）",
        "解析（未知留空）",
        "校正说明 / 拒绝理由",
        "本题来源页（可跨页）",
        "边界未知，仅保留真实来源页",
        "题目区域",
        "已核对，本题不关联题图",
    ]
    values = {label: _value(loaded[components[label]]) for label in labels}
    assert changes.keys() <= values.keys()
    values.update(changes)
    return _callback(app, "save_fields")(
        identity, question_id, *(values[label] for label in labels), current_state
    )


def test_registered_save_keeps_legacy_false_then_preserves_explicit_key_order(
    monkeypatch, files_api
):
    from uuid import UUID

    from backend.app.models import ExtractedQuestion
    from backend.app.ui import paper_import_loaders as loaders

    connect(monkeypatch, files_api)
    paper = pending(files_api, 1)
    identity, question_id = paper["id"], paper["questions"][0]["id"]
    current = state(files_api)
    options = {"C": "7", "A": "5", "D": "8", "B": "6"}
    loaders.patch(identity, question_id, {"options": options}, current)
    session = files_api[1]
    session.expire_all()
    record = session.get(ExtractedQuestion, UUID(question_id))
    record.order_preserved = False
    session.commit()
    app, editor, components = _editor()
    loaded = editor.reload(identity, question_id, current)
    status = loaded[
        next(
            c
            for c in editor.outputs
            if c not in components.values()
            and isinstance(c, gr.Markdown)
            and "状态：" in str(loaded.get(c, ""))
        )
    ]
    assert "原始选项顺序无法证明" in status
    rows = _value(loaded[components["选项"]])
    assert [row[0] for row in rows] == list(options)
    _save_form(
        app,
        editor,
        components,
        identity,
        question_id,
        current,
        **{"解析（未知留空）": "仅修改解析"},
    )
    unchanged = loaders.load(identity, current).questions[0]
    assert unchanged.order_preserved is False
    assert unchanged.knowledge_points is None and unchanged.assets is None
    reordered = [rows[2], rows[0], rows[3], rows[1]]
    saved = _save_form(
        app,
        editor,
        components,
        identity,
        question_id,
        current,
        选项=reordered,
    )
    reopened = loaders.load(identity, current).questions[0]
    assert reopened.order_preserved is True
    assert list(reopened.options.items()) == [(row[0], row[1]) for row in reordered]
    assert _value(saved[components["选项"]]) == reordered
    reopened_view = editor.reload(identity, question_id, current)
    assert _value(reopened_view[components["选项"]]) == reordered


@pytest.mark.parametrize("row", [["unexpected-label", None], ["unexpected-label", ""]])
def test_text_options_reject_absent_content_instead_of_fabricating_text(row):
    with pytest.raises(ValueError):
        corrections.options_from_rows([row], "文本选项")


def _second_source_page(files_api, paper):
    from uuid import UUID

    from backend.app.domain.enums import PaperImportStatus
    from backend.app.models import PaperImport
    from backend.app.services.question_asset_service import QuestionAssetService

    client, session, files, _document, users, _headers = files_api
    record = session.get(PaperImport, UUID(paper["id"]))
    path, _ = files.download(paper["pages"][0]["file_id"], actor_id=users[0].id)
    record.status = PaperImportStatus.PARSING
    record.page_count = 2
    session.commit()
    page = QuestionAssetService(
        session, root=client.app.state.settings.storage_root
    ).create_source_page(
        record.id, page_number=2, content=path.read_bytes(), actor_id=users[0].id
    )
    record.status = PaperImportStatus.PENDING_REVIEW
    session.commit()
    return str(page.id)


def test_registered_cross_page_regions_and_asset_controls_persist(
    monkeypatch, files_api
):
    from backend.app.ui import paper_import_loaders as loaders

    connect(monkeypatch, files_api)
    paper = pending(files_api, 1)
    second_page = _second_source_page(files_api, paper)
    identity, question_id = paper["id"], paper["questions"][0]["id"]
    first_page = paper["pages"][0]["id"]
    current = state(files_api)
    app, editor, components = _editor()
    _save_form(
        app,
        editor,
        components,
        identity,
        question_id,
        current,
        **{
            "分值": "3.00",
            "本题来源页（可跨页）": [first_page, second_page],
            "边界未知，仅保留真实来源页": False,
            "题目区域": [[2, 0, 0, 200, 120], [1, 0, 0, 100, 100]],
        },
    )
    reopened = loaders.load(identity, current).questions[0]
    assert [str(page) for page in reopened.source_page_ids] == [first_page, second_page]
    assert [str(region.source_page_id) for region in reopened.source_regions] == [
        first_page,
        second_page,
    ]
    show_page = _callback(app, "show_page")(identity, second_page, current)
    assert "data:image/png;base64," in show_page[0]
    assert "像素" in show_page[1]
    _callback(app, "add_image")(
        identity,
        question_id,
        first_page,
        "0,0,100,100",
        "figure",
        "第一图",
        False,
        current,
    )
    _callback(app, "add_image")(
        identity,
        question_id,
        second_page,
        "0,0,200,120",
        "diagram",
        "第二图",
        False,
        current,
    )
    assets = loaders.load(identity, current).questions[0].assets
    first_asset, second_asset = [str(asset.id) for asset in assets]
    shown = _callback(app, "show_asset")(identity, question_id, first_asset, current)
    assert "data:image/png;base64," in shown[0] and shown[-1] is False
    _callback(app, "update_image")(
        identity, question_id, first_asset, "table", 2, "改为表格", True, current
    )
    reordered = loaders.load(identity, current).questions[0].assets
    assert [str(asset.id) for asset in reordered] == [second_asset, first_asset]
    assert reordered[1].asset_type == "table" and reordered[1].student_visible is True
    _callback(app, "remove_image")(identity, question_id, first_asset, current)
    remaining = loaders.load(identity, current).questions[0].assets
    assert [str(asset.id) for asset in remaining] == [second_asset]
    assert remaining[0].student_visible is False


def test_registered_rejection_and_explicit_batch_confirmation_are_durable(
    monkeypatch, files_api
):
    from sqlalchemy import func, select

    from backend.app.models import Question
    from backend.app.ui import paper_import_loaders as loaders

    connect(monkeypatch, files_api)
    paper = pending(files_api, 3)
    identity = paper["id"]
    first, second, rejected = [question["id"] for question in paper["questions"]]
    current = state(files_api)
    app, editor, components = _editor()
    with pytest.raises(gr.Error):
        _callback(app, "reject_question")(identity, rejected, "", current)
    _callback(app, "reject_question")(identity, rejected, "误识别，明确拒绝", current)
    for question_id in (first, second):
        _save_form(
            app,
            editor,
            components,
            identity,
            question_id,
            current,
            **{"分值": "5", "已核对，本题不关联题图": True},
        )
    with pytest.raises(gr.Error):
        _callback(app, "commit_batch")(identity, [], current)
    result = _callback(app, "commit_batch")(identity, [first], current)
    assert "1 题待补全" in result[editor.message]
    loaded = loaders.load(identity, current)
    assert loaded.status.value == "Pending Review"
    by_id = {str(question.id): question for question in loaded.questions}
    assert by_id[first].status.value == "Corrected"
    assert by_id[second].status.value == "Pending Correction"
    assert by_id[rejected].status.value == "Rejected"
    formal_id = by_id[first].question_id
    _callback(app, "commit_batch")(identity, [first], current)
    assert loaders.load(identity, current).questions[0].question_id == formal_id
    session = files_api[1]
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Question)) == 1
    _callback(app, "commit_batch")(identity, [first, second], current)
    assert loaders.load(identity, current).status.value == "Ready"
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Question)) == 2


@pytest.fixture(scope="module")
def full_paper_app():
    from backend.app.ui.gradio_app import create_gradio_app

    return create_gradio_app()


def test_full_shell_registered_upload_remains_generator_and_executes_real_pipeline(
    full_paper_app, monkeypatch, files_api
):
    import inspect
    from uuid import UUID

    from sqlalchemy.orm import sessionmaker

    from backend.app.ai.paper_extraction import PaperExtractor
    from backend.app.services.paper_import_service import PaperImportRunner
    from backend.app.ui import gradio_app
    from backend.app.ui import paper_import_loaders as loaders
    from tests.contract.test_paper_import_api import SAMPLES
    from tests.unit.ingestion.test_paper_extraction import Provider, question

    connect(monkeypatch, files_api)
    monkeypatch.setattr(gradio_app, "_authenticated_state", lambda current: current)
    callback = _callback(full_paper_app, "upload_action")
    registered = next(
        block_fn for block_fn in full_paper_app.fns.values() if block_fn.fn is callback
    )
    assert registered.concurrency_id == "eduagent-ui"
    assert registered.concurrency_limit == 1
    assert inspect.isgeneratorfunction(callback)
    client, session, _files, document, _users, _headers = files_api
    runner = PaperImportRunner(
        sessionmaker(bind=session.get_bind(), expire_on_commit=False),
        settings=client.app.state.settings,
        extractor=PaperExtractor(Provider([{"questions": [question([1])]}])),
    )
    monkeypatch.setattr(
        loaders, "run", lambda identity, _state: runner.run(UUID(identity))
    )
    current = state(files_api)
    stream = callback(str(document.course_id), str(SAMPLES / "paper_text.pdf"), current)
    receipt = next(stream)
    identity = receipt[0]["value"]
    assert loaders.load(identity, current).status.value == "Uploaded"
    final = next(stream)
    assert final[0]["value"] == identity
    assert "提取结束" in final[1]
    assert loaders.load(identity, current).status.value == "Pending Review"
    with pytest.raises(StopIteration):
        next(stream)


def test_full_shell_logout_clears_paper_files_choices_and_source_pixels(full_paper_app):
    callback = _callback(full_paper_app, "clear_workspace")
    result = callback()
    components = {
        component.label: component
        for component in full_paper_app.blocks.values()
        if getattr(component, "label", None)
    }
    dynamic_labels = [
        "导入课程",
        "试卷导入记录",
        "待校正题目",
        "查看原页",
        "本题来源页（可跨页）",
        "本题题图",
        "题图来源页",
        "确认入库的题目",
    ]
    for label in dynamic_labels:
        update = result[components[label]]
        assert update.get("choices") == [], label
        assert update["value"] in (None, []), label
    source = components["原试卷"]
    assert isinstance(source, gr.File)
    assert result[source]["value"] is None
    event = next(
        block_fn for block_fn in full_paper_app.fns.values() if block_fn.fn is callback
    )
    assert source in event.outputs
    previews = [
        block
        for block in full_paper_app.blocks.values()
        if isinstance(block, gr.HTML) and block in result and block.parent is not None
    ]
    assert previews
    assert all("data:image" not in str(result[preview]) for preview in previews)
