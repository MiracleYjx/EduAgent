"""T057 结果视图回调测试：真实加载接线、待复核标注与空态。

TCR（2026-09-16，T057 / B06）：原实现只有空态占位，无法证明“UI 消费受权限保护的结果读模型”
与“待复核必须显式标注、不得伪装成最终成绩”。本测试通过 ``configure_results_loaders`` 注入
替身加载器，验证回调真实消费读模型、待复核标注、失败与未接通时保留空态，且视图不自行计算
总分或平均分。

测试只断言视图对外行为，不复制服务内部实现。
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import pytest

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
        "items": [],
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
        "items": [{"answer_id": "answer-1"}],
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

    rows, weak_points, diagnosis, mastery, message = refresh_student_panel(
        "exam-1", _STUDENT_STATE
    )

    assert rows
    assert weak_points and mastery
    assert PENDING_REVIEW_MESSAGE in message
    assert PENDING_FINAL_MESSAGE in diagnosis
