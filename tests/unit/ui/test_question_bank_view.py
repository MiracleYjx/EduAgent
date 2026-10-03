"""题库列表、详情、编辑状态与原有审核边界。TCR 见 docs/test-change-record-ui-question-bank.md。"""

from collections.abc import Callable
from contextlib import nullcontext
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import Mock
from uuid import UUID

import gradio as gr
import pytest
from sqlalchemy.exc import SQLAlchemyError

import backend.app.ui.question_view as question_module
from backend.app.api.questions import QuestionDetailDTO
from backend.app.domain.enums import QuestionStatus, QuestionType
from backend.app.schemas.image_assessment import ImageAssessmentView
from backend.app.services.question_service import QuestionSummary

TEACHER = {"access_token": "test-token", "user_id": "teacher-id", "roles": ["Teacher"]}
COURSES = [("计算机基础", "course-id")]


@pytest.fixture(scope="module")
def app() -> gr.Blocks:
    with gr.Blocks() as result:
        question_module.create_question_view(gr.State(TEACHER))
    return result


@pytest.fixture
def question() -> QuestionSummary:
    return QuestionSummary(
        id="question-id",
        course_id="course-id",
        type=QuestionType.SHORT_ANSWER,
        content="说明数据库事务的一致性。",
        reference_answer="事务保持数据库的一致状态。",
        scoring_rubric="解释概念 5 分，举例 5 分。",
        knowledge_points=["事务"],
        score=Decimal(10),
        status=QuestionStatus.PENDING_REVIEW,
        created_by="teacher-id",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def callback(app: gr.Blocks, name: str) -> Callable[..., Any]:
    matches = [
        fn.fn for fn in app.fns.values() if getattr(fn.fn, "__name__", "") == name
    ]
    unique = list(dict.fromkeys(matches))
    assert len(unique) == 1
    return unique[0]


def component(app: gr.Blocks, identifier: str) -> Any:
    return next(block for block in app.blocks.values() if block.elem_id == identifier)


def field(app: gr.Blocks, label: str) -> Any:
    return next(
        block
        for block in app.blocks.values()
        if (
            block.value
            if isinstance(block, gr.Button)
            else getattr(block, "label", None)
        )
        == label
    )


def service_fixture(monkeypatch: pytest.MonkeyPatch, question: QuestionSummary) -> Mock:
    service = Mock()
    service.get_question.return_value = question
    monkeypatch.setattr(
        question_module, "get_session_factory", lambda: lambda: nullcontext(None)
    )
    monkeypatch.setattr(question_module, "QuestionService", lambda session: service)
    monkeypatch.setattr(
        question_module, "teacher_course_choices", lambda state: COURSES
    )

    def load_detail(identity, state):
        value = service.get_question(identity, teacher_id=state["user_id"])
        return QuestionDetailDTO(
            **value.model_dump(),
            sources_persisted=False,
            source_status="history_unknown",
            sources=[],
            revision_comments=[],
            image_assessment=ImageAssessmentView(
                owner_kind="question",
                owner_id=UUID(int=1),
                assessment=None,
                context_revision=0,
                run_no=0,
                check_no=0,
                input_refs=None,
                current_run=None,
                current_check=None,
                imported_review=None,
                status="not_required",
                confirmed_conditions=[],
                requires_manual_review=False,
                evidence_readable=True,
            ),
        )

    monkeypatch.setattr(question_module.question_review_loaders, "detail", load_detail)
    return service


def test_selection_opens_readonly_details_then_explicit_editor(
    app: gr.Blocks,
    question: QuestionSummary,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = service_fixture(monkeypatch, question)
    event = gr.SelectData(
        None, {"index": [0, 0], "value": question.content, "selected": True}
    )
    result = callback(app, "select_row")([question.id], TEACHER, event)
    assert result[component(app, "edu-question-list")]["visible"] is False
    assert result[component(app, "edu-question-detail")]["visible"] is True
    assert result[component(app, "edu-question-editor")]["visible"] is False
    assert question.content in result[component(app, "edu-question-preview")]["value"]
    assert result[field(app, "审核通过")]["interactive"] is False

    result = callback(app, "edit_selected")(question.model_dump(mode="json"), TEACHER)
    assert result[component(app, "edu-question-editor")]["visible"] is True
    assert result[component(app, "edu-question-preview")]["visible"] is False
    assert result[field(app, "完整题干")]["interactive"] is True
    assert result[field(app, "审核通过")]["interactive"] is False
    assert result[field(app, "提交审核")]["interactive"] is False
    service.get_question.assert_called_with(question.id, teacher_id=TEACHER["user_id"])


def test_approved_question_cannot_enter_editor(
    app: gr.Blocks,
    question: QuestionSummary,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approved = question.model_copy(update={"status": QuestionStatus.APPROVED})
    service_fixture(monkeypatch, approved)
    result = callback(app, "edit_selected")(approved.model_dump(mode="json"), TEACHER)
    assert result[component(app, "edu-question-editor")]["visible"] is False
    assert result[component(app, "edu-question-edit")]["interactive"] is False
    assert result[field(app, "完整题干")]["interactive"] is False
    assert result[component(app, "edu-question-save")]["interactive"] is False


@pytest.mark.parametrize("state", [{}, {"access_token": "test", "roles": ["Student"]}])
def test_editor_rejects_unauthorized_reads(
    app: gr.Blocks,
    question: QuestionSummary,
    monkeypatch: pytest.MonkeyPatch,
    state: dict[str, Any],
) -> None:
    service = service_fixture(monkeypatch, question)
    result = callback(app, "edit_selected")(question.model_dump(mode="json"), state)
    service.get_question.assert_not_called()
    assert 'role="alert"' in result[component(app, "edu-question-message")]
    assert component(app, "edu-question-editor") not in result


def test_save_returns_persisted_details_and_keeps_failures_in_editor(
    app: gr.Blocks,
    question: QuestionSummary,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = service_fixture(monkeypatch, question)
    saved = question.model_copy(update={"reference_answer": "保存后的答案"})
    service.update_question.return_value = saved
    service.get_question.return_value = saved
    monkeypatch.setattr(question_module, "load_question_choices", lambda *args: [saved])
    args = [
        question.model_dump(mode="json"),
        question.course_id,
        question.type.value,
        question.content,
        [],
        None,
        [],
        None,
        "保存后的答案",
        question.scoring_rubric,
        "补全解析",
        "中等",
        "事务",
        10,
        TEACHER,
    ]
    result = callback(app, "save")(*args)
    assert result[component(app, "edu-question-editor")]["visible"] is False
    assert "保存后的答案" in result[component(app, "edu-question-preview")]["value"]
    service.update_question.assert_called_once()

    service.update_question.side_effect = SQLAlchemyError("secret database detail")
    result = callback(app, "save")(*args)
    assert set(result) == {component(app, "edu-question-message")}
    assert "无法连接数据库" in next(iter(result.values()))
    assert "secret" not in next(iter(result.values()))


def test_return_and_failed_refresh_clear_old_selection_and_statistics(
    app: gr.Blocks,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = callback(app, "close_detail")()
    assert result[component(app, "edu-question-list")]["visible"] is True
    assert result[component(app, "edu-question-detail")]["visible"] is False
    assert result[component(app, "edu-question-preview")]["value"] == ""
    monkeypatch.setattr(
        question_module, "teacher_course_choices", Mock(side_effect=SQLAlchemyError())
    )
    result = callback(app, "refresh")(None, "", "", "", TEACHER)
    assert "统计暂不可用" in result[component(app, "edu-question-summary")]
    assert "无法连接数据库" in result[component(app, "edu-question-message")]


def test_preview_preserves_full_text_and_escapes_complex_options(
    question: QuestionSummary,
) -> None:
    source = question.model_copy(
        update={
            "content": "长题目" * 100 + "<script>alert(1)</script>",
            "options": {"A": {"image": "<img src=x onerror=alert(1)>"}},
            "reference_answer": "<b>答案</b>",
        }
    )
    html = question_module.question_preview_html(source, COURSES)
    assert "长题目" * 100 in html
    assert "&lt;script&gt;" in html and "<script>" not in html
    assert "&lt;img" in html and "<img" not in html
    assert "&lt;b&gt;答案&lt;/b&gt;" in html
    assert "计算机基础" in html and "course-id" not in html
