"""T178 teacher assembly, association edits and authorized preview (TCR 33)."""

from contextlib import nullcontext
from decimal import Decimal
from unittest.mock import Mock
from uuid import UUID

import gradio as gr
import pytest

import backend.app.ui.exam_view as view
from backend.app.schemas.exam_assembly import AssemblyResponse
from backend.app.services.exam_assembly_service import AssemblyError
from backend.app.services.file_storage_service import FileStorageError

TEACHER = {
    "access_token": "synthetic",
    "roles": ["Teacher"],
    "user_id": "00000000-0000-4000-8000-000000000001",
}
COURSE = "00000000-0000-4000-8000-000000000002"
EXAM = "00000000-0000-4000-8000-000000000003"
QUESTION = "00000000-0000-4000-8000-000000000004"
REPLACEMENT = "00000000-0000-4000-8000-000000000005"
ASSET = "00000000-0000-4000-8000-000000000006"


def test_request_keeps_exact_teacher_counts_coverage_and_decimal_override():
    payload = view._assembly_request(
        COURSE, ["1", "0", "2"], [["条件", "2"]], {QUESTION: "3.33"}, "10.00"
    )
    assert payload.question_count == 3
    assert [(item.question_type, item.count) for item in payload.type_distribution] == [
        ("SINGLE_CHOICE", 1),
        ("SHORT_ANSWER", 2),
    ]
    assert payload.knowledge_coverage[0].knowledge_point == "条件"
    assert payload.knowledge_coverage[0].min_questions == 2
    assert payload.score_overrides[0].score == Decimal("3.33")
    assert payload.total_score == Decimal("10.00")


@pytest.mark.parametrize("count", ["1.5", "-1", "NaN", "abc"])
def test_request_rejects_noninteger_or_negative_counts(count):
    with pytest.raises(ValueError):
        view._assembly_request(COURSE, [count, "0", "1"], [], {}, "10.00")


@pytest.mark.parametrize(
    ("change", "default", "score", "expected"),
    [
        (False, False, "99", {}),
        (True, True, "99", {"score": None}),
        (True, False, "3.33", {"score": "3.33"}),
    ],
)
def test_patch_distinguishes_omitted_explicit_and_default_score(
    change, default, score, expected
):
    payload = view._exam_question_patch("2", REPLACEMENT, change, default, score)
    raw = payload.model_dump(mode="json", exclude_unset=True)
    assert raw == {"order_index": 2, "replacement_question_id": REPLACEMENT, **expected}


def test_failure_reports_confirmed_unsaved_unknown_intent_without_claiming_success():
    error = AssemblyError(
        "EXAM_ASSEMBLY_UNSATISFIABLE",
        "候选不足",
        details={
            "intent_saved": None,
            "intent_save_error": {"code": "DISCONNECT"},
            "gaps": [
                {
                    "kind": "type",
                    "question_type": "SHORT_ANSWER",
                    "required": 2,
                    "available": 1,
                    "missing": 1,
                }
            ],
        },
    )
    text = view._assembly_failure(error)
    assert "保存结果未知" in text and "DISCONNECT" in text
    assert "简答" in text and "2" in text and "1" in text
    assert "已保存" not in text
    assert "尚未保存" in view._assembly_failure(
        AssemblyError("X", "未保存", details={"intent_saved": False})
    )
    assert "已保存" in view._assembly_failure(
        AssemblyError("X", "不足", details={"intent_saved": True})
    )


def preview():
    return AssemblyResponse.model_validate(
        {
            "exam_id": EXAM,
            "current_status": "Draft",
            "total_score": "3.33",
            "assembly_constraints": None,
            "conditions": [],
            "publication_checks": [],
            "exam_questions": [
                {
                    "id": REPLACEMENT,
                    "exam_id": EXAM,
                    "question_id": QUESTION,
                    "order_index": 1,
                    "score": "3.33",
                    "effective_score": "3.33",
                    "base_score": "10.00",
                    "question_type": "SHORT_ANSWER",
                    "content": "题干<script>alert(1)</script>",
                    "options": {"B": "乙", "A": "甲"},
                    "reference_answer": "答案",
                    "scoring_rubric": "实际标准",
                    "analysis": "解析",
                    "knowledge_points": ["条件"],
                    "assets": [
                        {
                            "id": ASSET,
                            "question_id": QUESTION,
                            "file_id": "a_" + ASSET.replace("-", ""),
                            "asset_type": "figure",
                            "width": 1,
                            "height": 1,
                            "caption": "<图>",
                            "source_page_id": None,
                            "region": None,
                            "order_index": 1,
                            "student_visible": False,
                        }
                    ],
                }
            ],
        }
    )


def test_preview_reads_only_authorized_bytes_and_escapes_text(tmp_path):
    path = tmp_path / "private.png"
    path.write_bytes(b"actual-owned-image")
    files = Mock()
    files.download.return_value = (path, Mock(media_type="image/png"))
    html = view._teacher_preview_html(preview(), files, UUID(TEACHER["user_id"]))
    files.download.assert_called_once_with(
        "a_" + ASSET.replace("-", ""), actor_id=UUID(TEACHER["user_id"])
    )
    assert "data:image/png;base64,YWN0dWFsLW93bmVkLWltYWdl" in html
    assert "<script>" not in html and "&lt;图&gt;" in html
    assert "实际标准" in html and "答案" in html and "3.33" in html
    assert html.index("乙") < html.index("甲")
    assert str(path) not in html and TEACHER["access_token"] not in html


def test_preview_missing_asset_does_not_emit_partial_success():
    files = Mock()
    files.download.side_effect = FileStorageError("FILE_MISSING", "真实原图缺失")
    with pytest.raises(FileStorageError) as found:
        view._teacher_preview_html(preview(), files, UUID(TEACHER["user_id"]))
    assert found.value.code == "FILE_MISSING"


@pytest.fixture(scope="module")
def app():
    with gr.Blocks() as result:
        view.create_exam_view(gr.State(TEACHER))
    return result


def node(app, name):
    return next(fn for fn in app.fns.values() if getattr(fn.fn, "__name__", "") == name)


def component(app, suffix):
    return next(
        block
        for block in app.blocks.values()
        if block.elem_id == "edu-exam-assembly-" + suffix
    )


def test_new_write_controls_exist_and_failure_clears_previous_confirmations(
    app, monkeypatch
):
    for suffix in [
        "assemble",
        "patch",
        "preview",
        "default-score",
        "override-question",
    ]:
        component(app, suffix)
    factory = Mock()
    factory.return_value.assemble_exam.side_effect = AssemblyError(
        "EXAM_ASSEMBLY_UNSATISFIABLE", "候选不足", details={"intent_saved": True}
    )
    monkeypatch.setattr(view, "ExamService", factory)
    monkeypatch.setattr(view, "get_session_factory", lambda: lambda: nullcontext(None))
    result = node(app, "assemble").fn(
        EXAM, COURSE, "1", "0", "0", [], "10.00", {}, TEACHER
    )
    publish = node(app, "publish")
    assert result[publish.inputs[2]] is None
    load_scoring = node(app, "prepare_scoring")
    assert result[load_scoring.inputs[-2]] is None
    assert "已保存" in result[component(app, "status")]
