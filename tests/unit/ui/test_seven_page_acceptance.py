"""T186 cancellation re-reads current authorization and never submits (TCR §41)."""

import gradio as gr

from backend.app.ui import review_view as view
from tests.unit.ui import test_review_view as existing

reset_loaders = existing.reset_loaders


def test_registered_cancel_review_reloads_actual_score_without_decision():
    loaders = existing._StubLoaders()
    existing._install(loaders)
    with gr.Blocks() as app:
        panel = view.create_review_view(gr.State(existing._TEACHER_STATE))
    node = next(
        f
        for f in app.fns.values()
        if getattr(f.fn, "__name__", "") == "cancel_review_edit"
    )
    result = node.fn(
        {
            "submission_id": "submission-1",
            "answer_id": "answer-1",
            "score": "999",
            "reason": "unsaved",
        },
        existing._TEACHER_STATE,
    )
    assert result[2] == "6.00" and result[3] == "说明了变量的作用。"
    assert [name for name, _ in loaders.calls] == ["detail"]
    assert "已取消" in result[-1]
    assert panel.score in node.outputs and panel.reason in node.outputs


def test_cancel_review_rejects_student_and_discards_stale_values():
    loaders = existing._StubLoaders()
    existing._install(loaders)
    result = view.cancel_review_edit(
        {"submission_id": "submission-1", "answer_id": "answer-1", "score": "999"},
        existing._STUDENT_STATE,
    )
    assert result[1:4] == ("", "", "")
    assert not loaders.calls and "无权访问阅卷复核" in result[-1]


def test_registered_table_event_authenticates_the_state_after_injection(monkeypatch):
    from backend.app.ui import gradio_app as app_module

    app = app_module.create_gradio_app()
    callback = next(
        f.fn
        for f in app.fns.values()
        if getattr(f.fn, "__name__", "") == "select_course_row"
    )
    observed = []
    monkeypatch.setattr(
        app_module,
        "_authenticated_state",
        lambda state: observed.append(state) or state,
    )
    state = {"roles": ["Student"]}
    result = callback(
        gr.SelectData(None, {"index": (0, 0), "value": "course"}), [], state
    )
    assert observed == [state]
    assert len(result) == 12


def test_approval_refreshes_current_review_facts(monkeypatch):
    from backend.app.domain.enums import QuestionStatus
    from backend.app.ui import question_generation_view as generation
    from tests.unit.ui import test_question_generation_view as fixtures

    monkeypatch.setattr(
        generation,
        "submit_candidate_review_action",
        lambda *args: (
            "saved",
            [],
            [],
            "old",
            gr.update(interactive=False),
            gr.update(interactive=False),
        ),
    )
    monkeypatch.setattr(
        generation,
        "_candidate_with_detail",
        lambda *args: (
            fixtures._candidate(status=QuestionStatus.APPROVED),
            fixtures._detail().model_copy(update={"status": QuestionStatus.APPROVED}),
        ),
    )
    with gr.Blocks() as app:
        generation.create_question_generation_view(gr.State(fixtures._TEACHER_STATE))
    callback = next(
        f.fn
        for f in app.fns.values()
        if getattr(f.fn, "__name__", "") == "approve_candidate_ui"
    )
    result = callback(
        {"candidate_id": "candidate-1"}, "", "course-1", "", fixtures._TEACHER_STATE
    )
    assert any(
        "当前题目状态：审核通过" in value
        for value in result.values()
        if isinstance(value, str)
    )
    assert "saved" in result.values()
