"""T174 teacher Rubric preparation/confirmation UI boundaries; see TCR §29."""

from contextlib import nullcontext
from copy import deepcopy
from unittest.mock import Mock

import gradio as gr
import pytest

import backend.app.ui.exam_view as view_module

TEACHER = {
    "access_token": "synthetic-token",
    "user_id": "00000000-0000-4000-8000-000000000001",
    "roles": ["Teacher"],
}
EXAM = "00000000-0000-4000-8000-000000000010"
QUESTION = "00000000-0000-4000-8000-000000000020"
PREPARATION = "00000000-0000-4000-8000-000000000030"


@pytest.fixture(scope="module")
def app():
    with gr.Blocks() as result:
        view_module.create_exam_view(gr.State(TEACHER))
    return result


def component(app, name):
    return next(
        block
        for block in app.blocks.values()
        if block.elem_id == "edu-exam-scoring-" + name
    )


def callback_node(app, name):
    return next(fn for fn in app.fns.values() if getattr(fn.fn, "__name__", "") == name)


def call(app, name, *args):
    return callback_node(app, name).fn(*args)


def raw_view(*, prepared=False, score="10.00", editable=True):
    result = {
        "exam_id": EXAM,
        "exam_question_id": "00000000-0000-4000-8000-000000000040",
        "question_id": QUESTION,
        "question_type": "SHORT_ANSWER",
        "source_rubric": "根据原文的三项条件逐项评分，不能从其中的数字猜权重。",
        "question_validation_revision": 7,
        "explicit_score": score,
        "effective_score": score,
        "question_score": "3.00",
        "base_score": None,
        "basis": None,
        "editable": editable,
    }
    if prepared:
        default = "3.33" if score == "10.00" else "0.67"
        result["base_score"] = "3.00"
        result["basis"] = {
            "preparation_id": PREPARATION,
            "kind": "subjective",
            "rounding_mode": "ROUND_HALF_UP",
            "additive": True,
            "rounding_delta": "0.01" if score == "10.00" else "-0.01",
            "confirmation": None,
            "points": [
                {
                    "key": f"p{i}",
                    "label": f"实际条件 {i}",
                    "base_points": "1.00",
                    "default_points": default,
                    "confirmed_points": default,
                }
                for i in range(1, 4)
            ],
        }
    return result


def service_fixture(monkeypatch, *, before=None, after=None):
    from backend.app.schemas.exam_scoring import ScoringBasisView

    service = Mock()
    service.get_scoring_basis.return_value = ScoringBasisView.model_validate(
        before or raw_view()
    )
    service.prepare_scoring_basis.return_value = ScoringBasisView.model_validate(
        after or raw_view(prepared=True)
    )
    service.confirm_scoring_basis.return_value = ScoringBasisView.model_validate(
        after or raw_view(prepared=True)
    )
    monkeypatch.setattr(
        view_module, "get_session_factory", lambda: lambda: nullcontext(None)
    )
    monkeypatch.setattr(
        view_module, "ExamScoringService", lambda session: service, raising=False
    )
    return service


def assert_publication_invalidated(app, result):
    publish = callback_node(app, "publish")
    assert result[publish.inputs[2]] is None
    button = next(
        block
        for block in app.blocks.values()
        if isinstance(block, gr.Button) and block.value == "发布考试"
    )
    assert result[button]["interactive"] is False


def test_panel_uses_text_amounts_readonly_source_and_explicit_teacher_actions(app):
    assert component(app, "source").interactive is False
    assert component(app, "points").datatype == ["str", "str", "str"]
    assert component(app, "final").datatype == ["str", "str"]
    assert component(app, "defaults").interactive is False
    assert component(app, "prepare").value == "准备本场标准"
    assert component(app, "confirm").value == "确认本场标准"


def test_loading_uses_saved_basis_and_retains_unknown_prepared_base(app, monkeypatch):
    service = service_fixture(monkeypatch)
    result = call(app, "load_scoring", EXAM, QUESTION, TEACHER)
    service.get_scoring_basis.assert_called_once()
    assert result[component(app, "source")] == raw_view()["source_rubric"]
    assert "尚未准备" in result[component(app, "context")]
    assert (
        "3.00" in result[component(app, "context")]
        and "10.00" in result[component(app, "context")]
    )
    assert result[component(app, "defaults")] == []
    assert result[component(app, "confirm")]["interactive"] is False


@pytest.mark.parametrize(
    ("score", "delta", "default"),
    [("10.00", "+0.01", "3.33"), ("2.00", "-0.01", "0.67")],
)
def test_prepare_shows_real_signed_delta_without_distributing_points(
    app, monkeypatch, score, delta, default
):
    before = raw_view(score=score)
    service = service_fixture(
        monkeypatch, before=before, after=raw_view(prepared=True, score=score)
    )
    rows = [[f"p{i}", f"实际条件 {i}", "1.00"] for i in range(1, 4)]
    result = call(
        app, "prepare_scoring", EXAM, QUESTION, "可加总数值要点", rows, before, TEACHER
    )
    payload = service.prepare_scoring_basis.call_args.args[2].model_dump(mode="json")
    assert payload["expected_effective_score"] == score
    assert payload["expected_base_score"] is None
    assert payload["expected_basis"] is None
    assert payload["points"] == [
        {"key": f"p{i}", "label": f"实际条件 {i}", "base_points": "1.00"}
        for i in range(1, 4)
    ]
    assert delta in result[component(app, "delta")]
    assert result[component(app, "final")]["value"] == [
        [f"p{i}", default] for i in range(1, 4)
    ]
    assert result[component(app, "status")] != "已确认"
    assert_publication_invalidated(app, result)


def test_repreparing_sends_complete_loaded_basis_to_protect_new_confirmation(
    app, monkeypatch
):
    before = raw_view(prepared=True)
    service = service_fixture(monkeypatch, before=before)
    rows = [[f"p{i}", f"实际条件 {i}", "1.00"] for i in range(1, 4)]
    call(
        app, "prepare_scoring", EXAM, QUESTION, "可加总数值要点", rows, before, TEACHER
    )
    payload = service.prepare_scoring_basis.call_args.args[2].model_dump(mode="json")
    assert payload["expected_base_score"] == "3.00"
    assert payload["expected_basis"] == before["basis"]


def test_qualitative_preparation_does_not_create_zero_points(app, monkeypatch):
    after = raw_view(prepared=True)
    after["basis"].update(additive=False, points=[], rounding_delta=None)
    service = service_fixture(monkeypatch, after=after)
    result = call(
        app, "prepare_scoring", EXAM, QUESTION, "定性文字标准", [], raw_view(), TEACHER
    )
    payload = service.prepare_scoring_basis.call_args.args[2].model_dump(mode="json")
    assert payload["points"] == [] and payload["additive"] is False
    assert result[component(app, "final")]["value"] == []
    assert "不适用" in result[component(app, "delta")]


def test_confirmation_sends_loaded_identity_exact_values_and_reason(app, monkeypatch):
    before = raw_view(prepared=True)
    after = deepcopy(before)
    after["basis"]["points"][0]["confirmed_points"] = "3.34"
    after["basis"]["confirmation"] = {
        "teacher_id": TEACHER["user_id"],
        "confirmed_at": "2026-10-04T00:00:00Z",
        "reason": "开发夹具中教师明确给首项增加0.01分",
    }
    service = service_fixture(monkeypatch, before=before, after=after)
    result = call(
        app,
        "confirm_scoring",
        EXAM,
        QUESTION,
        [["p1", "3.34"], ["p2", "3.33"], ["p3", "3.33"]],
        after["basis"]["confirmation"]["reason"],
        before,
        TEACHER,
    )
    payload = service.confirm_scoring_basis.call_args.args[2].model_dump(mode="json")
    assert payload["preparation_id"] == PREPARATION
    assert payload["expected_basis"] == before["basis"]
    assert payload["expected_question_validation_revision"] == 7
    assert payload["expected_base_score"] == "3.00"
    assert payload["confirmed_points"][0] == {"key": "p1", "points": "3.34"}
    assert "teacher_id" not in payload and "confirmed_at" not in payload
    assert "已确认" in result[component(app, "status")]
    assert_publication_invalidated(app, result)


def test_reloading_confirmed_or_protected_basis_is_readonly_and_factual(
    app, monkeypatch
):
    before = raw_view(prepared=True, editable=False)
    before["basis"]["confirmation"] = {
        "teacher_id": TEACHER["user_id"],
        "confirmed_at": "2026-10-04T00:00:00Z",
        "reason": "真实读取的合成确认记录",
    }
    service_fixture(monkeypatch, before=before)
    result = call(app, "load_scoring", EXAM, QUESTION, TEACHER)
    assert result[component(app, "prepare")]["interactive"] is False
    assert result[component(app, "confirm")]["interactive"] is False
    assert "真实读取的合成确认记录" in result[component(app, "status")]
    assert "2026-10-04" in result[component(app, "status")]


def test_stale_confirmation_error_is_visible_without_success_or_local_mutation(
    app, monkeypatch
):
    service = service_fixture(monkeypatch)
    service.confirm_scoring_basis.side_effect = ValueError(
        "本场评分依据已改变，请重新加载。"
    )
    before = raw_view(prepared=True)
    saved = deepcopy(before)
    result = call(
        app,
        "confirm_scoring",
        EXAM,
        QUESTION,
        [["p1", "3.34"], ["p2", "3.33"], ["p3", "3.33"]],
        "明确核对",
        before,
        TEACHER,
    )
    assert before == saved
    assert "已改变" in result[component(app, "status")]
    assert "已确认" not in result[component(app, "status")]
    assert_publication_invalidated(app, result)


def test_other_role_cannot_load_or_prepare_scoring_basis(app, monkeypatch):
    service = service_fixture(monkeypatch)
    denied = {**TEACHER, "roles": ["Student"]}
    result = call(app, "load_scoring", EXAM, QUESTION, denied)
    assert "无权" in result[component(app, "status")]
    service.get_scoring_basis.assert_not_called()
    result = call(
        app, "prepare_scoring", EXAM, QUESTION, "定性文字标准", [], raw_view(), denied
    )
    assert "无权" in result[component(app, "status")]
    service.prepare_scoring_basis.assert_not_called()


def test_switching_question_cannot_confirm_previous_preparation(app, monkeypatch):
    service = service_fixture(monkeypatch)
    result = call(
        app,
        "confirm_scoring",
        EXAM,
        "00000000-0000-4000-8000-000000000021",
        [],
        "核对",
        raw_view(prepared=True),
        TEACHER,
    )
    service.confirm_scoring_basis.assert_not_called()
    assert "重新加载" in result[component(app, "status")]
    assert result[component(app, "confirm")]["interactive"] is False
