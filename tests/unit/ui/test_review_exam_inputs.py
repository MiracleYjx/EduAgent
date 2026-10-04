"""T178：本场评分事实、十进制编辑、真实轮次与授权内联原图（TCR33）。"""

from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import gradio as gr
import pytest
from PIL import Image

from backend.app.api.reviews import ReviewQueryError
from backend.app.schemas.question_assets import QuestionAssetView
from backend.app.services.file_storage_service import FileStorageError
from backend.app.ui import review_loaders, review_view
from tests.unit.grading.test_exam_scoring_consumption import fixed_input
from tests.unit.ui.test_review_view import _detail, _outcome


def case():
    value, teacher = fixed_input()
    detail = _detail().model_copy(
        update={
            "submission_id": value.submission_id,
            "answer_id": value.answer_id,
            "course_id": value.course_id,
            "exam_id": value.exam_id,
            "student_id": value.student_id,
            "question_id": value.question_id,
            "exam_question_id": value.exam_question_id,
            "scoring_input": value,
            "max_score": Decimal("10.00"),
            "score": Decimal("6.68"),
            "pending_review_round_id": uuid4(),
        }
    )
    state = {"access_token": "owned-session", "user_id": teacher, "roles": ["Teacher"]}
    return value, detail, state


def png():
    output = BytesIO()
    Image.new("RGB", (4, 3), "white").save(output, format="PNG")
    return output.getvalue()


@pytest.fixture(autouse=True)
def reset_loaders():
    review_view.configure_review_loaders()
    yield
    review_view.configure_review_loaders()


def test_details_display_fixed_basis_and_saved_context_without_rescaling():
    value, detail, _ = case()
    value = value.model_copy(update={"course_context": "Actual saved model context."})
    detail = detail.model_copy(
        update={"scoring_input": value, "scoring_rubric": "Changed bank rubric."}
    )
    fields = review_view._detail_fields(detail)
    assert fields[2] == "6.68"
    assert "10.00" in fields[0] and value.question_content in fields[0]
    assert value.source_rubric in fields[6] and "Changed bank rubric." not in fields[6]
    assert "3.34" in fields[6] and "3.33" in fields[6]
    assert "不得再次" in fields[6]
    assert "Actual saved model context." in fields[7]


def test_unmeasured_score_and_missing_fixed_input_remain_unknown():
    fields = review_view._detail_fields(_detail().model_copy(update={"score": None}))
    assert fields[2] == ""
    assert "未记录" in fields[6]
    assert "暂无" in review_view._detail_fields(None)[0]


def test_input_error_disables_teacher_actions_without_zero_score():
    _, detail, _ = case()
    detail = detail.model_copy(update={"scoring_input_error": "FILE_MISSING"})
    fields = review_view._detail_render(detail, "real error")
    assert "FILE_MISSING" in fields[0]
    assert fields[9]["interactive"] is False and fields[10]["interactive"] is False
    assert fields[2] == "6.68"


def test_ui_passes_actual_review_round_and_decimal_text():
    _, detail, state = case()
    calls = []

    async def decision(*args, **kwargs):
        calls.append(kwargs)
        return _outcome()

    from backend.app.api.reviews import ReviewQueuePageDTO
    from tests.unit.ui.test_review_view import _queue_item

    review_view.configure_review_loaders(
        decision=decision,
        detail=lambda *args, **kwargs: detail,
        queue=lambda *args, **kwargs: ReviewQueuePageDTO(
            items=[_queue_item()], total=1, limit=50, offset=0
        ),
    )
    asyncio.run(
        review_view.save_review_changes(
            detail.model_dump(mode="json"),
            "6.675000000000000000001",
            "真实修改理由",
            "",
            "",
            "Pending Review",
            state,
        )
    )
    assert calls[0]["score"] == "6.675000000000000000001"
    assert calls[0]["expected_review_round_id"] == str(detail.pending_review_round_id)


def test_image_event_uses_only_submission_answer_ids_and_clears_failure():
    _, detail, state = case()
    calls = []

    def images(*args, **kwargs):
        calls.append(kwargs)
        return '<img src="data:image/png;base64,actual" />'

    review_view.configure_review_loaders(images=images)
    selected = {
        "submission_id": detail.submission_id,
        "answer_id": detail.answer_id,
        "file_id": "attacker-selected",
    }
    html = review_view.load_selected_review_images(selected, state)
    assert "data:image/png" in html
    assert calls == [
        {"submission_id": detail.submission_id, "answer_id": detail.answer_id}
    ]
    assert "data:" not in review_view.load_selected_review_images(None, state)

    def missing(*args, **kwargs):
        raise ReviewQueryError("真实原图缺失", error_code="FILE_MISSING")

    review_view.configure_review_loaders(images=missing)
    html = review_view.load_selected_review_images(selected, state)
    assert "FILE_MISSING" in html and "<img" not in html


def test_production_image_loader_rereads_authorized_detail_and_inlines_actual_bytes(
    monkeypatch, tmp_path
):
    value, detail, state = case()
    asset = QuestionAssetView.model_validate(
        {
            "id": uuid4(),
            "question_id": value.question_id,
            "file_id": "actual-authorized",
            "asset_type": "diagram",
            "width": 4,
            "height": 3,
            "caption": "<script>original</script>",
            "source_page_id": None,
            "region": None,
            "order_index": 1,
        }
    )
    detail = detail.model_copy(
        update={"scoring_input": value.model_copy(update={"assets": [asset]})}
    )
    raw = png()
    path = tmp_path / "private-original.png"
    path.write_bytes(raw)
    calls = []

    class Query:
        def get_answer_detail(self, *args):
            calls.append(("detail", args))
            return detail

    class Files:
        def __init__(self, session, *, root):
            pass

        def download(self, file_id, *, actor_id):
            calls.append(("file", file_id, str(actor_id)))
            return path, SimpleNamespace(media_type="image/png")

    @contextmanager
    def session():
        yield object()

    monkeypatch.setattr(review_loaders, "build_query_service", lambda: Query())
    monkeypatch.setattr(review_loaders, "get_session_factory", lambda: session)
    monkeypatch.setattr(review_loaders, "FileStorageService", Files, raising=False)
    html = review_loaders.load_images(
        state, submission_id=detail.submission_id, answer_id=detail.answer_id
    )
    assert calls == [
        ("detail", (state["user_id"], detail.submission_id, detail.answer_id)),
        ("file", "actual-authorized", state["user_id"]),
    ]
    assert base64.b64encode(raw).decode("ascii") in html
    assert "&lt;script&gt;original&lt;/script&gt;" in html and "<script>" not in html
    assert str(tmp_path) not in html and "/gradio_api/file=" not in html


def test_production_image_loader_preserves_real_file_error_and_student_guard(
    monkeypatch,
):
    value, detail, state = case()
    asset = QuestionAssetView.model_validate(
        {
            "id": uuid4(),
            "question_id": value.question_id,
            "file_id": "actual-authorized",
            "asset_type": "diagram",
            "width": 4,
            "height": 3,
            "caption": None,
            "source_page_id": None,
            "region": None,
            "order_index": 1,
        }
    )
    detail = detail.model_copy(
        update={"scoring_input": value.model_copy(update={"assets": [asset]})}
    )
    monkeypatch.setattr(
        review_loaders,
        "build_query_service",
        lambda: SimpleNamespace(get_answer_detail=lambda *args: detail),
    )

    class Files:
        def __init__(self, session, *, root):
            pass

        def download(self, *args, **kwargs):
            raise FileStorageError("FILE_MISSING", "真实原图丢失")

    @contextmanager
    def session():
        yield object()

    monkeypatch.setattr(review_loaders, "get_session_factory", lambda: session)
    monkeypatch.setattr(review_loaders, "FileStorageService", Files, raising=False)
    with pytest.raises(ReviewQueryError) as error:
        review_loaders.load_images(
            state, submission_id=detail.submission_id, answer_id=detail.answer_id
        )
    assert error.value.error_code == "FILE_MISSING"
    from backend.app.domain.permissions import PermissionDeniedError

    with pytest.raises(PermissionDeniedError):
        review_loaders.load_images(
            {**state, "roles": ["Student"]},
            submission_id=detail.submission_id,
            answer_id=detail.answer_id,
        )


def test_score_loader_keeps_original_precision_and_current_round(monkeypatch):
    _, detail, state = case()
    calls = []

    async def submit(**kwargs):
        calls.append(kwargs)
        return _outcome()

    monkeypatch.setattr(
        review_loaders, "build_decision_service", lambda: SimpleNamespace(submit=submit)
    )
    asyncio.run(
        review_loaders.submit_decision(
            state,
            submission_id=detail.submission_id,
            answer_id=detail.answer_id,
            action="modify",
            score="6.675000000000000000001",
            reason="real reason",
            expected_review_round_id=str(detail.pending_review_round_id),
        )
    )
    assert calls[0]["payload"].score == Decimal("6.675000000000000000001")
    assert (
        calls[0]["payload"].expected_review_round_id == detail.pending_review_round_id
    )


def test_invalid_text_score_is_explicit_existing_business_error(monkeypatch):
    _, detail, state = case()
    with pytest.raises(ReviewQueryError) as error:
        asyncio.run(
            review_loaders.submit_decision(
                state,
                submission_id=detail.submission_id,
                answer_id=detail.answer_id,
                action="modify",
                score="七分",
                reason="real reason",
            )
        )
    assert error.value.error_code == "REVIEW_DECISION_INVALID_SCORE"


def test_review_component_preserves_main_event_protocol_and_inlines_images():
    with gr.Blocks() as app:
        components = review_view.create_review_view()
    assert isinstance(components.score, gr.Textbox)
    assert isinstance(components.images, gr.HTML)
    callbacks = list(app.fns.values())
    for name, count in [
        ("select_review_item", 12),
        ("next_review_item", 12),
        ("confirm_review", 14),
        ("save_review_changes", 14),
    ]:
        callback = next(
            fn for fn in callbacks if getattr(fn.fn, "__name__", None) == name
        )
        assert len(callback.outputs) == count
    images = [
        fn
        for fn in callbacks
        if getattr(fn.fn, "__name__", None) == "load_selected_review_images"
    ]
    assert len(images) == 4 and all(fn.outputs == [components.images] for fn in images)
