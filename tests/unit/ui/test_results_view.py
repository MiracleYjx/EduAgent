"""T057 结果视图回调测试：真实加载接线、待复核标注与空态。

TCR（2026-09-16，T057 / B06）：原实现只有空态占位，无法证明“UI 消费受权限保护的结果读模型”
与“待复核必须显式标注、不得伪装成最终成绩”。本测试通过 ``configure_results_loaders`` 注入
替身加载器，验证回调真实消费读模型、待复核标注、失败与未接通时保留空态，且视图不自行计算
总分或平均分。

测试只断言视图对外行为，不复制服务内部实现。

TCR（B05）：原 ``student_result_rows`` 把整卷摘要统计写进四列表格，未渲染服务返回的逐题
``items``。新增真实 Pydantic 读模型到视图行的断言，覆盖题序、状态、得分、理由和错题标记。
教师结果表同时覆盖仅有 ``student_id`` 时的展示回退。

TCR（B05 补充）：经真实 Gradio 刷新事件和组件序列化验证逐题四列与摘要接线，覆盖最终、
待复核和尚无成绩三种读模型，防止空态被误报为待复核；不启动服务器或访问外部服务。

TCR（教师字段）：用生产 StudentResultSummaryDTO 验证只有 student_id 的记录，并验证
姓名、标识、兼容 student 字段的优先级，防止再次显示“未提供”或误用旧字段。
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from decimal import Decimal
from typing import Any

import gradio as gr
import pytest
from gradio.state_holder import SessionState

from backend.app.domain.enums import GradingStatus, QuestionType
from backend.app.schemas.grading import (
    ExamResultStatus,
    QuestionResultDTO,
    StudentResultSummaryDTO,
    SubmissionResultDTO,
)
from backend.app.ui.results_diagnosis import (
    DIAGNOSIS_NOT_READY_MESSAGE,
    DIAGNOSIS_STALE_MESSAGE,
    PENDING_FINAL_MESSAGE,
)
from backend.app.ui.results_view import (
    PENDING_REVIEW_MESSAGE,
    TEACHER_UNAVAILABLE_MESSAGE,
    UNAVAILABLE_MESSAGE,
    configure_results_loaders,
    create_results_view,
    refresh_student_diagnosis,
    refresh_student_panel,
    refresh_student_results,
    refresh_teacher_results,
    result_status_text,
    student_result_rows,
    teacher_result_rows,
)


@pytest.fixture(autouse=True)
def reset_loaders() -> Generator[None, None, None]:
    """每个用例前后清空视线接线，避免用例互相污染。"""

    configure_results_loaders()
    yield
    configure_results_loaders()


def _pending_review_payload() -> dict[str, Any]:
    """构造待复核结果读模型。"""

    return {
        "submission_id": "submission-1",
        "exam_id": "exam-1",
        "exam_title": "期中测验",
        "student_id": "student-1",
        "result_status": "Pending Review",
        "is_final": False,
        "total_score": None,
        "confirmed_subtotal": "4.00",
        "confirmed_subtotal_label": "已确认部分小计（不含待复核）。",
        "pending_review_count": 1,
        "graded_answer_count": 0,
        "items": [{
            "order": 1,
            "answer_id": "answer-pending",
            "grading_status": "Pending Review",
            "reason": "答案待人工复核",
        }],
        "mistake_answer_ids": [],
    }


def _final_payload() -> dict[str, Any]:
    """构造最终成绩读模型。"""

    return {
        "submission_id": "submission-1",
        "exam_id": "exam-1",
        "exam_title": "期中测验",
        "student_id": "student-1",
        "result_status": "Final",
        "is_final": True,
        "total_score": "88.50",
        "confirmed_subtotal": "88.50",
        "pending_review_count": 0,
        "items": [{
            "order": 1,
            "answer_id": "answer-1",
            "grading_status": "Final",
            "effective_score": "88.50",
            "reason": "答案已确认",
        }],
        "mistake_answer_ids": ["answer-2"],
    }


def test_student_results_show_empty_state_without_loader() -> None:
    """未接线时保留空态文案，不展示任何成绩。"""

    rows, availability = refresh_student_results("exam-1", None)

    assert rows == []
    assert UNAVAILABLE_MESSAGE in availability


def test_student_pending_review_is_marked_and_total_stays_empty() -> None:
    """待复核结果显式标注，最终总分为空。"""

    configure_results_loaders(
        student_loader=lambda exam_id, state: _pending_review_payload()
    )

    rows, availability = refresh_student_results("exam-1", None)
    rendered = "\n".join(" | ".join(cell for cell in row) for row in rows)

    assert "88.50" not in rendered
    assert "待复核" in rendered or "待人工复核" in rendered
    assert "Pending Review" in availability or "待人工复核" in availability


def test_student_final_result_maps_total_score() -> None:
    """最终成绩映射总分与状态，不由视图重新计算。"""

    configure_results_loaders(student_loader=lambda exam_id, state: _final_payload())

    rows, availability = refresh_student_results("exam-1", None)
    rendered = "\n".join(" | ".join(cell for cell in row) for row in rows)

    assert "88.50" in rendered
    assert "最终" in rendered
    assert result_status_text("Final")
    assert availability == ""


def test_student_loader_failure_keeps_empty_state() -> None:
    """加载失败不得伪装成成绩，保留空态。"""

    def _boom(exam_id: str | None, state: Any) -> Any:
        raise RuntimeError("查询失败")

    configure_results_loaders(student_loader=_boom)

    rows, availability = refresh_student_results("exam-1", None)

    assert rows == []
    assert UNAVAILABLE_MESSAGE in availability


def test_teacher_results_use_loader_records_and_keep_average_from_service() -> None:
    """教师视图消费服务返回的记录，不自行计算平均分。"""

    records = [
        {
            "student_id": "student-1",
            "total_score": "88.50",
            "result_status": "Final",
            "pending_review_count": 0,
        },
        {
            "student_id": "student-2",
            "total_score": None,
            "result_status": "Pending Review",
            "pending_review_count": 2,
        },
    ]
    configure_results_loaders(teacher_loader=lambda course_id, exam_id, state: records)

    rows, availability = refresh_teacher_results("course-1", "exam-1", None)

    assert len(rows) == 2
    assert rows[0] == teacher_result_rows(records)[0]
    assert availability == ""


def test_teacher_results_keep_empty_state_without_loader() -> None:
    """教师视图未接线时保留空态。"""

    rows, availability = refresh_teacher_results("course-1", "exam-1", None)

    assert rows == []
    assert TEACHER_UNAVAILABLE_MESSAGE in availability


def test_student_result_rows_do_not_derive_status_from_score() -> None:
    """状态展示只来自服务字段，缺状态时不得由分数推导。"""

    rows = student_result_rows({"total_score": "60.00", "is_final": False})

    rendered = "\n".join(" | ".join(cell for cell in row) for row in rows)

    assert "60.00" not in rendered


@pytest.mark.parametrize("result_state", ["final", "pending", "not_ready"])
def test_student_panel_renders_dto_through_gradio_components(result_state: str) -> None:
    """真实读模型经过刷新回调与组件序列化，逐题、错题与摘要各归其位。"""

    items = [
        QuestionResultDTO(
            order=1,
            answer_id="answer-1",
            question_id="question-1",
            question_type=QuestionType.SINGLE_CHOICE,
            max_score=Decimal(2),
            score=Decimal(2),
            effective_score=Decimal(2),
            counted=True,
            grading_status=GradingStatus.FINAL,
            review_status="Not Required",
            validation_status="Validated",
            reason="答案正确",
        ),
        QuestionResultDTO(
            order=2,
            answer_id="answer-2",
            question_id="question-2",
            question_type=QuestionType.SHORT_ANSWER,
            max_score=Decimal(3),
            score=Decimal(1),
            effective_score=Decimal(1),
            counted=True,
            grading_status=GradingStatus.FINAL,
            review_status="Not Required",
            validation_status="Validated",
            reason="遗漏一个要点",
        ),
    ]
    payload = SubmissionResultDTO(
        submission_id="submission-1",
        exam_id="exam-1",
        exam_title="期中测验",
        student_id="student-1",
        result_status=ExamResultStatus.FINAL,
        is_final=True,
        total_score=Decimal(3),
        confirmed_subtotal=Decimal(3),
        total_max_score=Decimal(5),
        expected_answer_count=2,
        graded_answer_count=2,
        pending_review_count=0,
        items=items,
        mistake_answer_ids=["answer-2"],
    )
    if result_state == "pending":
        payload = SubmissionResultDTO.model_validate({
            **payload.model_dump(),
            "result_status": ExamResultStatus.PENDING_REVIEW,
            "is_final": False,
            "total_score": None,
            "expected_answer_count": 3,
            "pending_review_count": 1,
        })
    elif result_state == "not_ready":
        payload = SubmissionResultDTO(
            submission_id="submission-1", exam_id="exam-1", exam_title="期中测验",
            student_id="student-1", not_ready_reason="阅卷结果尚未生成。",
        )
    requests = []

    def load_result(exam_id, state):
        requests.append((exam_id, state))
        return payload

    configure_results_loaders(student_loader=load_result)
    with gr.Blocks(analytics_enabled=False) as app:
        state_component = gr.State(_STUDENT_STATE)
        view = create_results_view(state_component)
    callback = next(fn for fn in app.fns.values() if fn.fn is refresh_student_panel)
    callback.inputs[0].choices = [("期中测验", "exam-1")]
    state = SessionState(app)
    state[state_component._id] = _STUDENT_STATE
    response = asyncio.run(app.process_api(callback, ["exam-1", None], state=state))
    rendered = dict(zip((component._id for component in callback.outputs), response["data"], strict=True))
    table = rendered[view.results_table._id]
    rows = table["data"]

    assert requests == [("exam-1", _STUDENT_STATE)]
    assert table["headers"] == ["题号", "状态", "得分", "反馈"]
    if result_state == "not_ready":
        assert rows == []
        assert rendered[view.total_score._id] == "暂无最终成绩"
        assert rendered[view.graded_count._id] == rendered[view.pending_count._id] == "暂无"
        assert "阅卷结果尚未生成" in rendered[view.message._id]
        return
    assert rows == [
        ["第 1 题", result_status_text(GradingStatus.FINAL), "2", "答案正确"],
        ["⚠ 第 2 题", result_status_text(GradingStatus.FINAL), "1", "遗漏一个要点"],
    ]
    assert all(len(row) == 4 for row in rows)
    assert all("最终总分" not in row for row in rows)
    assert rendered[view.total_score._id] == (
        "3" if result_state == "final" else "待复核，暂无最终总分"
    )
    assert rendered[view.graded_count._id] == "2"
    assert rendered[view.pending_count._id] == ("0" if result_state == "final" else "1")


def test_teacher_result_rows_fallback_to_student_id() -> None:
    """教师 DTO 只有学生标识时，表格显示可读的学生回退文本。"""

    rows = teacher_result_rows([
        StudentResultSummaryDTO(
            submission_id="submission-1",
            exam_id="exam-1",
            exam_title="期中测验",
            student_id="student-1",
            result_status=ExamResultStatus.FINAL,
            is_final=True,
            total_score=Decimal("88.50"),
            pending_review_count=0,
        )
    ])

    assert rows[0][0] == "学生 #student-1"


@pytest.mark.parametrize("fields,expected", [
    ({"student_name": "张同学", "student_id": "student-1", "student": "旧称呼"}, "张同学"),
    ({"student_name": "", "student_id": "student-1", "student": "旧称呼"}, "学生 #student-1"),
    ({"student_name": None, "student_id": None, "student": "旧称呼"}, "旧称呼"),
])
def test_teacher_student_label_uses_contract_priority(fields: dict[str, Any], expected: str) -> None:
    """展示优先使用姓名，其次真实学生标识，最后兼容 student 字段。"""

    assert teacher_result_rows([fields])[0][0] == expected


# --------------------------------------------------------------------------- #
# T057：诊断、薄弱知识点与掌握度区域的动态输出绑定
# --------------------------------------------------------------------------- #


#: 学生会话状态：通过视图的学生守卫（含访问令牌与学生角色）。
_STUDENT_STATE: dict[str, Any] = {
    "access_token": "token",
    "user_id": "student-1",
    "roles": ["Student"],
}

def _ready_report(**overrides: Any) -> dict[str, Any]:
    """构造 Ready 诊断读模型（含平台字段与建议）。"""

    payload: dict[str, Any] = {
        "status": "Ready",
        "mastery_by_knowledge_point": [
            {
                "knowledge_point": "变量",
                "answered_count": 2,
                "correct_count": 1,
                "awarded_score": "10.00",
                "max_score": "20.00",
                "mastery": "0.50",
            }
        ],
        "weak_knowledge_points": [
            {
                "knowledge_point": "变量",
                "reason": "掌握度 0.50 低于阈值 0.60。",
                "error_count": 1,
                "awarded_score": "10.00",
                "max_score": "20.00",
                "mastery": "0.50",
            }
        ],
        "error_reasons": ["question-2：缺少要点 引用数据（得分 5.00/10.00）。"],
        "learning_suggestions": ["复习变量的引用方式。"],
        "generated_at": "2026-09-16T12:00:00+00:00",
        "source_exam_result_updated_at": "2026-09-16T12:00:00+00:00",
    }
    payload.update(overrides)
    return payload


def test_student_diagnosis_sections_use_loader_payload() -> None:
    """薄弱知识点、错误原因与掌握度区域直接由诊断读模型驱动。"""

    configure_results_loaders(
        student_diagnosis_loader=lambda exam_id, state: (_ready_report(), True)
    )

    weak_points, diagnosis, mastery = refresh_student_diagnosis(
        "exam-1", _STUDENT_STATE
    )

    assert "变量" in weak_points
    assert "0.50" in weak_points
    assert "复习变量的引用方式。" in diagnosis
    assert mastery == [["变量", "2", "1", "10.00", "20.00", "0.50"]]


def test_student_diagnosis_marks_unconfirmed_result() -> None:
    """成绩未最终确认时明确说明诊断只覆盖已确认部分。"""

    configure_results_loaders(
        student_diagnosis_loader=lambda exam_id, state: (_ready_report(), False)
    )

    _, diagnosis, _ = refresh_student_diagnosis("exam-1", _STUDENT_STATE)

    assert PENDING_FINAL_MESSAGE in diagnosis


@pytest.mark.parametrize(
    "status,expected",
    [("Not Ready", DIAGNOSIS_NOT_READY_MESSAGE), ("Stale", DIAGNOSIS_STALE_MESSAGE)],
)
def test_student_diagnosis_reports_empty_and_stale_states(
    status: str, expected: str
) -> None:
    """未生成与过期的诊断给出明确说明，不展示掌握度表格。"""

    configure_results_loaders(
        student_diagnosis_loader=lambda exam_id, state, status=status: (
            _ready_report(status=status),
            True,
        )
    )

    weak_points, diagnosis, mastery = refresh_student_diagnosis(
        "exam-1", _STUDENT_STATE
    )

    assert expected in diagnosis
    assert expected in weak_points
    assert mastery == []


def test_student_diagnosis_reports_failure_code_without_mastery() -> None:
    """诊断失败时展示错误码，并不用掌握度数据冒充结论。"""

    configure_results_loaders(
        student_diagnosis_loader=lambda exam_id, state: (
            _ready_report(status="Failed", error_code="DIAGNOSIS_PROVIDER_NOT_READY"),
            True,
        )
    )

    _, diagnosis, mastery = refresh_student_diagnosis(
        "exam-1", _STUDENT_STATE
    )

    assert "DIAGNOSIS_PROVIDER_NOT_READY" in diagnosis
    assert mastery == []


def test_student_diagnosis_loader_failure_keeps_empty_state() -> None:
    """诊断加载失败时保留明确空态，不展示旧结论。"""

    def _boom(exam_id: str | None, state: Any) -> Any:
        raise RuntimeError("boom")

    configure_results_loaders(student_diagnosis_loader=_boom)

    _, diagnosis, mastery = refresh_student_diagnosis(
        "exam-1", _STUDENT_STATE
    )

    assert UNAVAILABLE_MESSAGE in diagnosis
    assert mastery == []


def test_student_diagnosis_requires_student_role() -> None:
    """非学生身份不得读取诊断，未接线时同样保持空态。"""

    configure_results_loaders(
        student_diagnosis_loader=lambda exam_id, state: (_ready_report(), True)
    )

    _, diagnosis, _ = refresh_student_diagnosis(
        "exam-1", {"access_token": "token", "roles": ["Teacher"]}
    )
    assert UNAVAILABLE_MESSAGE in diagnosis

    configure_results_loaders()
    _, diagnosis, _ = refresh_student_diagnosis(
        "exam-1", _STUDENT_STATE
    )
    assert UNAVAILABLE_MESSAGE in diagnosis


def test_student_panel_returns_rows_and_diagnosis_sections() -> None:
    """面板刷新一次返回逐题结果、诊断区域与提示。"""

    configure_results_loaders(
        student_loader=lambda exam_id, state: _pending_review_payload(),
        student_diagnosis_loader=lambda exam_id, state: (_ready_report(), False),
    )

    rows, total, graded, pending, weak_points, diagnosis, mastery, message = refresh_student_panel(
        "exam-1", _STUDENT_STATE
    )

    assert rows
    assert total == "待复核，暂无最终总分"
    assert graded == "0"
    assert pending == "1"
    assert weak_points and mastery
    assert PENDING_REVIEW_MESSAGE in message
    assert PENDING_FINAL_MESSAGE in diagnosis
