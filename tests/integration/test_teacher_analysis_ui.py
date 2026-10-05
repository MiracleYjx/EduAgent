"""T182 actual Gradio postprocess and production loaders, TCR §37."""

import gradio as gr

from tests.integration import test_teacher_results_ui as old

teacher_ui_env = old.teacher_ui_env


def test_statistics_and_selected_answer_use_real_services(teacher_ui_env):
    env = teacher_ui_env
    state = old._panel_state(env)
    fn, response = old._process(
        env, "refresh_teacher_panel", [env.ids.course_id, env.ids.exam_id, None], state
    )
    values = {c._id: v for c, v in zip(fn.outputs, response["data"], strict=True)}
    assert "最终答卷：1" in values[env.view.analysis_overview._id]
    assert values[env.view.score_distribution._id]["data"] == [["8.00", "1"]]
    assert values[env.view.question_statistics._id]["data"][0][4] == "1"
    assert values[env.view.knowledge_statistics._id]["data"][0][0] == "变量"
    fn, response = old._select_student(env, state, env.ids.final_submission_id)
    values = {c._id: v for c, v in zip(fn.outputs, response["data"], strict=True)}
    assert values[env.view.answer_details._id]["data"][0][1] == "变量用于保存数据。"
    assert values[env.view.answer_details._id]["data"][0][3:5] == ["8.00", "10.00"]


def test_attention_selection_reuses_real_pending_review_context(teacher_ui_env):
    env = teacher_ui_env
    state = old._panel_state(env)
    attention = state[env.view.attention_records._id]
    index = next(
        i
        for i, r in enumerate(attention)
        if r["submission_id"] == env.ids.pending_submission_id
    )
    event = gr.EventData(None, {"index": (index, 0), "selected": True, "value": None})
    fn, response = old._process(
        env,
        "select_teacher_attention",
        [None, None, env.ids.exam_id, None],
        state,
        event_data=event,
    )
    context = state[env.view.review_context._id]
    assert (
        context["workflow_id"] == "workflow-p23-pending"
        and context["answer_id"] == env.ids.pending_answer_id
    )
    values = {c._id: v for c, v in zip(fn.outputs, response["data"], strict=True)}
    assert values[env.view.review_button._id]["interactive"] is True
    assert values[env.view.answer_details._id]["data"][0][1] == "变量是一个名称。"


def test_refresh_clears_previous_selection_and_new_data(teacher_ui_env):
    env = teacher_ui_env
    state = old._panel_state(env)
    old._select_student(env, state, env.ids.pending_submission_id)
    fn, response = old._process(
        env, "refresh_teacher_panel", [env.ids.course_id, env.ids.exam_id, None], state
    )
    values = {c._id: v for c, v in zip(fn.outputs, response["data"], strict=True)}
    assert state[env.view.review_context._id] is None
    assert values[env.view.answer_details._id]["data"] == []
    assert values[env.view.review_button._id]["interactive"] is False
