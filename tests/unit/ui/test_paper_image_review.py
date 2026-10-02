"""T164 displayed-version teacher commands and Gradio reset. TCR §17."""

from uuid import UUID, uuid4

import gradio as gr
import pytest

from backend.app.schemas.image_assessment import (
    ImageAssessmentView,
    ImageInput,
    ImageInputRefs,
)
from backend.app.ui import paper_correction_view as corrections
from backend.app.ui import paper_image_review_view as images
from backend.app.ui import paper_import_loaders as loaders
from tests.contract.test_question_correction_api import files_api as _files_api
from tests.contract.test_question_correction_api import pending
from tests.unit.ui.test_paper_import_view import connect, state

files_api = _files_api


def _snapshot():
    return ImageAssessmentView(
        owner_kind="extracted_question",
        owner_id=uuid4(),
        assessment=None,
        context_revision=8,
        run_no=3,
        check_no=2,
        input_refs=ImageInputRefs(
            text_fields=["content"],
            images=[
                ImageInput(
                    asset_id=uuid4(),
                    file_id=f"ea_{i}",
                    image_index=i,
                    asset_type="figure",
                    source_page_id=None,
                    region=None,
                )
                for i in [1, 2]
            ],
        ),
        current_run=None,
        current_check=None,
        imported_review=None,
        status="pending",
        open_issues=[],
        confirmed_conditions=[],
        requires_manual_review=True,
        evidence_readable=False,
    ).model_dump(mode="json")


def test_form_binds_displayed_group_and_revision_without_fabricating_ids():
    snapshot = _snapshot()
    value = images.manual_check_payload(
        snapshot,
        "unresolved",
        [
            ["2", "no_conditions_needed", "第二图无额外条件"],
            ["1", "conditions_confirmed", "第一图有明确长度"],
        ],
        [["1", "线段长 5", "", ""]],
        [["", "整组关系仍需核对"]],
        [],
        "实际教师核对意见",
    )
    assert (
        value["expected_context_revision"],
        value["expected_run_no"],
        value["expected_check_no"],
    ) == (8, 3, 2)
    assert (
        value["image_findings"][0]["asset_id"]
        == snapshot["input_refs"]["images"][1]["asset_id"]
    )
    assert value["confirmed_conditions"][0]["evidence_region"] is None
    assert value["confirmed_conditions"][0]["source_condition_id"] is None
    assert value["issues"] == [{"asset_ids": None, "message": "整组关系仍需核对"}]
    assert not {"teacher_id", "checked_at", "condition_id", "runs"} & value.keys()


def test_form_rejects_foreign_image_missing_reason_and_invented_coordinates():
    snapshot = _snapshot()
    base = (snapshot, "unresolved")
    with pytest.raises(ValueError):
        images.manual_check_payload(
            *base, [["3", "unresolved", "原因"]], [], [], [], "说明"
        )
    with pytest.raises(ValueError):
        images.manual_check_payload(
            *base, [["1", "no_conditions_needed", " "]], [], [], [], "说明"
        )
    with pytest.raises(ValueError):
        images.manual_check_payload(
            *base,
            [["1", "conditions_confirmed", "已看原图"]],
            [["1", "真实条件", "1,2,3", ""]],
            [],
            [],
            "说明",
        )


def _editor():
    with gr.Blocks() as demo:
        editor = corrections.create_paper_correction_view(
            gr.Dropdown(label="测试导入"), gr.State({})
        )
    components = {
        component.label: component
        for component in demo.blocks.values()
        if getattr(component, "label", None)
    }
    return demo, editor, components


def _callback(demo, name):
    matches = [
        function.fn
        for function in demo.fns.values()
        if function.fn and function.fn.__name__ == name
    ]
    assert len(matches) == 1
    return matches[0]


def _value(update):
    return (
        update.get("value")
        if isinstance(update, dict) and "__type__" in update
        else update
    )


def _prepared(monkeypatch, files_api):
    connect(monkeypatch, files_api)
    paper = pending(files_api, 1)
    identity, qid, pid = (
        paper["id"],
        paper["questions"][0]["id"],
        paper["pages"][0]["id"],
    )
    current = state(files_api)
    loaders.add_asset(
        qid,
        {
            "file_id": paper["pages"][0]["file_id"],
            "source_page_id": pid,
            "asset_type": "figure",
            "region": {"bbox": [0, 0, 100, 100]},
        },
        current,
    )
    return identity, qid, current


def test_registered_panel_loads_actual_group_and_saves_real_check(
    monkeypatch, files_api
):
    identity, qid, current = _prepared(monkeypatch, files_api)
    demo, editor, components = _editor()
    loaded = editor.reload(identity, qid, current)
    preview = next(
        block
        for block in editor.outputs
        if isinstance(block, gr.HTML) and "图 1" in str(loaded.get(block, ""))
    )
    assert "data:image/png;base64," in loaded[preview]
    snapshot_component = next(
        block for block in editor.outputs if isinstance(block, gr.State)
    )
    snapshot = loaded[snapshot_component]
    assert snapshot["owner_id"] == qid and snapshot["current_check"] is None
    assert _value(loaded[components["逐图核对结论与理由"]]) == [["1", "仍未确定", ""]]
    assert _value(loaded[components["教师确认的题图条件"]]) == []
    saved = _callback(demo, "save_image_check")(
        identity,
        qid,
        snapshot,
        "confirmed",
        [["1", "conditions_confirmed", "逐一对照原页确认"]],
        [["1", "原图中的关系已核对", "", ""]],
        [],
        [],
        "真实教师核对",
        current,
    )
    view = loaders.image_assessment(qid, current)
    assert (
        view.status == "confirmed"
        and str(view.current_check.teacher_id) == current["user_id"]
    )
    assert view.current_check.confirmed_conditions[0].evidence_region is None
    assert any("已保存本次" in str(value) for value in saved.values())
    reloaded = editor.reload(identity, qid, current)
    assert "真实教师核对" in str(reloaded[components["题图调用与核对历史"]])
    assert _value(reloaded[components["逐图核对结论与理由"]]) == [["1", "仍未确定", ""]]


def test_stale_displayed_form_is_rejected_without_overwriting_history(
    monkeypatch, files_api
):
    identity, qid, current = _prepared(monkeypatch, files_api)
    demo, editor, _components = _editor()
    loaded = editor.reload(identity, qid, current)
    snapshot = loaded[
        next(block for block in editor.outputs if isinstance(block, gr.State))
    ]
    loaders.patch(identity, qid, {"content": "输入已改变，旧表单不得静默复用"}, current)
    with pytest.raises(gr.Error):
        _callback(demo, "save_image_check")(
            identity,
            qid,
            snapshot,
            "confirmed",
            [["1", "no_conditions_needed", "已阅读"]],
            [],
            [],
            [],
            "旧表单说明",
            current,
        )
    view = loaders.image_assessment(qid, current)
    assert view.current_check is None and view.check_no == 0


def test_panel_reset_clears_private_images_history_and_expected_counters(
    monkeypatch, files_api
):
    identity, qid, current = _prepared(monkeypatch, files_api)
    _demo, editor, components = _editor()
    loaded = editor.reload(identity, qid, current)
    assert any("data:image/png" in str(value) for value in loaded.values())
    cleared = editor.reload(None, None, {})
    assert all("data:image" not in str(value) for value in cleared.values())
    snapshot_component = next(
        block for block in editor.outputs if isinstance(block, gr.State)
    )
    assert cleared[snapshot_component] is None
    assert _value(cleared[components["题图调用与核对历史"]]) is None
    assert _value(cleared[components["逐图核对结论与理由"]]) == []
    assert _value(cleared[components["本次真实教师核对说明"]]) == ""


def test_image_loader_rechecks_real_jwt_identity(monkeypatch, files_api):
    from backend.app.domain.permissions import PermissionDeniedError

    _identity, qid, current = _prepared(monkeypatch, files_api)
    assert loaders.image_assessment(qid, current).owner_id == UUID(qid)
    with pytest.raises(PermissionDeniedError):
        loaders.image_assessment(qid, current | {"user_id": str(files_api[4][1].id)})


def test_registered_machine_failure_remains_visible_and_does_not_create_teacher_check(
    monkeypatch, files_api
):
    import asyncio

    identity, qid, current = _prepared(monkeypatch, files_api)
    demo, _editor_view, components = _editor()
    result = asyncio.run(
        _callback(demo, "understand_image_group")(
            identity,
            qid,
            "读取整组真实题图",
            current,
        )
    )
    view = loaders.image_assessment(qid, current)
    assert view.current_run.outcome == "technical_error"
    assert view.current_run.error.code == "VISION_PROVIDER_NOT_READY"
    assert view.current_check is None and view.status == "pending"
    assert "VISION_PROVIDER_NOT_READY" in str(result[components["题图调用与核对历史"]])
    assert _value(result[components["教师确认的题图条件"]]) == []
