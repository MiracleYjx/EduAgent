"""T183 PostgreSQL + actual Gradio resource reads, TCR §38."""

import asyncio
import base64
from types import SimpleNamespace

import gradio as gr
import pytest
from gradio.state_holder import SessionState
from sqlalchemy.orm import Session

from backend.app.api import results as results_api
from backend.app.core.config import get_settings
from backend.app.domain.enums import UserRole
from backend.app.services.auth_service import create_access_token
from backend.app.ui import results_loaders, results_view
from tests.contract.test_results_analysis_api import save_result, set_failure
from tests.integration import test_learning_feedback as source
from tests.unit.services.test_student_fixed_exam import add_image

session = source.session
feedback_scenario = source.feedback_scenario


def make_ui(s, session, monkeypatch):
    factory = lambda: Session(session.get_bind())
    monkeypatch.setattr(results_loaders, "get_session_factory", lambda: factory)
    monkeypatch.setattr(results_api, "get_session_factory", lambda: factory)
    results_loaders.configure_production_results_loaders()
    with gr.Blocks(analytics_enabled=False) as app:
        user = gr.State(
            {
                "access_token": "synthetic-student-session",
                "user_id": str(s["students"][0].id),
                "roles": ["Student"],
            }
        )
        view = results_view.create_results_view(user)
    state = SessionState(app)
    state[user._id] = user.value
    env = (app, view, user, state)
    process(env, "refresh_student_exams", [None, None])
    return env


def process(env, name, inputs, event=None):
    app, _view, _user, state = env
    fn = next(f for f in app.fns.values() if getattr(f.fn, "__name__", "") == name)
    response = asyncio.run(app.process_api(fn, inputs, state=state, event_data=event))
    return {c._id: v for c, v in zip(fn.outputs, response["data"], strict=True)}


def select(env, name, inputs, index):
    return process(
        env,
        name,
        inputs,
        gr.EventData(None, {"index": (index, 0), "selected": True, "value": None}),
    )


def test_current_final_page_and_material_details(
    session, feedback_scenario, monkeypatch
):
    s = feedback_scenario
    chunk = source.material(session, s)
    env = make_ui(s, session, monkeypatch)
    _, view, _, state = env
    try:
        values = process(env, "refresh_student_learning", [str(s["exam"].id), None])
        assert values[view.feedback_table._id]["data"][1][5] == "1.00"
        assert (
            "最终" in values[view.learning_message._id]
            and "基于当前" in values[view.weak_points._id]
        )
        resources = state[view.recommendation_records._id]
        index = next(
            i for i, r in enumerate(resources) if r["resource_id"] == str(chunk.id)
        )
        detail = select(env, "select_student_resource", [None, None], index)[
            view.resource_detail._id
        ]
        assert (
            chunk.content in detail
            and str(chunk.id) in detail
            and "<script>" not in detail
        )
    finally:
        results_view.configure_results_loaders()


def test_practice_image_and_original_options_order(
    session, feedback_scenario, monkeypatch
):
    s = feedback_scenario
    q, asset, path, data = source.practice(session, s, image=True)
    env = make_ui(s, session, monkeypatch)
    _, view, _, state = env
    try:
        process(env, "refresh_student_learning", [str(s["exam"].id), None])
        index = next(
            i
            for i, r in enumerate(state[view.recommendation_records._id])
            if r["resource_id"] == str(q.id)
        )
        html = select(env, "select_student_resource", [None, None], index)[
            view.resource_detail._id
        ]
        assert base64.b64encode(data).decode() in html and html.index(
            "D："
        ) < html.index("A：") < html.index("B：")
        assert "参考答案" not in html and str(path) not in html
        asset.student_visible = False
        session.commit()
        html = select(env, "select_student_resource", [None, None], index)[
            view.resource_detail._id
        ]
        assert (
            "GRADING_PERMISSION_DENIED" in html
            and base64.b64encode(data).decode() not in html
        )
    finally:
        results_view.configure_results_loaders()


def test_regrading_clears_resources_and_rejects_old_selection(
    session, feedback_scenario, monkeypatch
):
    s = feedback_scenario
    source.material(session, s)
    env = make_ui(s, session, monkeypatch)
    _, view, _, state = env
    try:
        process(env, "refresh_student_learning", [str(s["exam"].id), None])
        assert state[view.recommendation_records._id]
        save_result(session, s, 0, ["2", "0"], pending=True)
        html = select(env, "select_student_resource", [None, None], 0)[
            view.resource_detail._id
        ]
        assert "GRADING_PERMISSION_DENIED" in html
        values = process(env, "refresh_student_learning", [str(s["exam"].id), None])
        assert (
            state[view.recommendation_records._id] == []
            and values[view.resource_detail._id] == ""
        )
        assert "待人工复核" in values[view.learning_message._id]
        assert values[view.mastery_table._id]["data"] == []
    finally:
        results_view.configure_results_loaders()


@pytest.mark.parametrize(
    ("index", "expected", "code"),
    [
        (1, "unfinished", None),
        (2, "pending_review", None),
        (3, "failed", "ProviderTimeout"),
        (4, "insufficient_evidence", "EXAM_SCORING_BASIS_MISSING"),
    ],
)
def test_current_processing_facts_are_shown_without_inferred_weakness(
    session, feedback_scenario, monkeypatch, index, expected, code
):
    s = feedback_scenario
    if expected == "pending_review":
        save_result(session, s, index, ["1", "1"], pending=True)
    if code:
        set_failure(session, s["submissions"][index], code, m4=True)
    dto = s["service"].get_student_learning_feedback(
        str(s["students"][index].id), str(s["submissions"][index].id)
    )
    assert dto.processing_status == expected and dto.processing_error_code == code
    assert (
        not dto.is_final
        and dto.weak_knowledge_points == []
        and dto.recommendations == []
    )
    env = make_ui(s, session, monkeypatch)
    _, view, user, state = env
    state[user._id] = {**user.value, "user_id": str(s["students"][index].id)}
    try:
        values = process(env, "refresh_student_learning", [str(s["exam"].id), None])
        assert dto.processing_reason in values[view.learning_message._id]
        if code:
            assert code in values[view.learning_message._id]
        assert values[view.mastery_table._id]["data"] == []
    finally:
        results_view.configure_results_loaders()


def test_final_result_ignores_old_failure_and_question_image_failure_keeps_grade(
    session, feedback_scenario, monkeypatch
):
    s = feedback_scenario
    info = SimpleNamespace(
        objective_question_id=s["questions"][0].id, teacher_id=s["teacher"].id
    )
    _asset, path, data = add_image((session, info, s["root"]))
    set_failure(session, s["submissions"][0], "ProviderTimeout")
    dto = s["service"].get_student_learning_feedback(
        str(s["students"][0].id), str(s["submissions"][0].id)
    )
    assert dto.processing_status == "final" and dto.processing_error_code is None
    env = make_ui(s, session, monkeypatch)
    _, view, _, _ = env
    try:
        process(env, "refresh_student_learning", [str(s["exam"].id), None])
        html = select(env, "select_student_question", [None, None], 0)[
            view.question_detail._id
        ]
        assert (
            base64.b64encode(data).decode() in html
            and "本人答案：B" in html
            and "2.00/2.00" in html
        )
        path.unlink()
        html = select(env, "select_student_question", [None, None], 0)[
            view.question_detail._id
        ]
        assert base64.b64encode(data).decode() not in html and "2.00/2.00" in html
        assert "FILE_MISSING" in html and str(path) not in html
    finally:
        results_view.configure_results_loaders()


def test_changed_owner_or_role_cannot_read_prior_selected_content(
    session, feedback_scenario, monkeypatch
):
    s = feedback_scenario
    chunk = source.material(session, s)
    env = make_ui(s, session, monkeypatch)
    _, view, user, state = env
    try:
        process(env, "refresh_student_learning", [str(s["exam"].id), None])
        state[user._id] = {**user.value, "user_id": str(s["students"][1].id)}
        html = select(env, "select_student_resource", [None, None], 0)[
            view.resource_detail._id
        ]
        assert "GRADING_PERMISSION_DENIED" in html and chunk.content not in html
        state[user._id] = {**user.value, "roles": ["Teacher"]}
        html = select(env, "select_student_resource", [None, None], 0)[
            view.resource_detail._id
        ]
        assert "无权访问" in html and chunk.content not in html
    finally:
        results_view.configure_results_loaders()


def test_homepage_opens_all_feedback_and_logout_resets_new_details(
    session, feedback_scenario, monkeypatch
):
    from backend.app.ui import gradio_app

    s = feedback_scenario
    source.material(session, s)
    factory = lambda: Session(session.get_bind())
    for module in (results_loaders, results_api, gradio_app):
        monkeypatch.setattr(module, "get_session_factory", lambda: factory)
    token = create_access_token(
        s["students"][0].id,
        secret_key=get_settings().JWT_SECRET_KEY,
        roles=[UserRole.STUDENT],
    )
    current = gradio_app._authenticated_state({"access_token": token})
    app = gradio_app.create_gradio_app()
    fn = next(
        f
        for f in app.fns.values()
        if getattr(f.fn, "__name__", "") == "open_student_result_from_dashboard"
    )
    sub = s["submissions"][0]
    records = [
        {
            "id": str(sub.id),
            "submission_id": str(sub.id),
            "exam_id": str(s["exam"].id),
            "exam_title": s["exam"].title,
            "result_status": "Final",
        }
    ]
    try:
        updates = fn.fn(records, str(sub.id), current, {})
        table = next(
            b
            for b in app.blocks.values()
            if getattr(b, "label", "") == "本人答案、最终失分与评分解释（选中查看原图）"
        )
        assert updates[table][1][5] == "1.00"
        assert any(
            isinstance(component, gr.State)
            and isinstance(v, list)
            and v
            and isinstance(v[0], dict)
            and v[0].get("kind") == "material"
            for component, v in updates.items()
        )
        reset = next(
            f.fn
            for f in app.fns.values()
            if getattr(f.fn, "__name__", "") == "clear_workspace"
        )()
        for identity in ["edu-student-question-detail", "edu-student-resource-detail"]:
            component = next(b for b in app.blocks.values() if b.elem_id == identity)
            assert reset[component]["value"] == ""
        assert reset[table]["value"]["data"] == []
        resource_state = next(
            c
            for c, v in updates.items()
            if isinstance(c, gr.State)
            and isinstance(v, list)
            and v
            and isinstance(v[0], dict)
            and v[0].get("kind") == "material"
        )
        assert reset[resource_state]["value"] == []
    finally:
        results_view.configure_results_loaders()


def test_no_owned_submission_clears_old_stage_placeholder(
    session, feedback_scenario, monkeypatch
):
    s = feedback_scenario
    env = make_ui(s, session, monkeypatch)
    _, view, user, state = env
    state[user._id] = {**user.value, "user_id": str(s["students"][6].id)}
    try:
        choices = process(env, "refresh_student_exams", [None, None])[view.exam._id]
        assert choices["choices"] == [] and choices["value"] is None
        values = process(env, "refresh_student_learning", [None, None])
        assert "暂无本人学习反馈" in values[view.learning_message._id]
        assert values[view.message._id] == ""
        assert values[view.feedback_table._id]["data"] == []
        assert (
            state[view.learning_records._id] is None
            and state[view.recommendation_records._id] == []
        )
    finally:
        results_view.configure_results_loaders()
