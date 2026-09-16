"""T057 生产结果/诊断加载器测试：S01 标识转换与只读约束。

TCR（2026-09-16，T057 / S01）：学生刷新入口接收 ``exam_id``，必须先在**当前学生授权范围**
内定位 ``submission_id`` 再查询详情；``exam_id`` 与 ``submission_id`` 不得混用，加载器也不得
保存用户身份或共享会话。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from backend.app.schemas.grading import ExamResultStatus, StudentResultSummaryDTO
from backend.app.ui import results_loaders
from backend.app.ui.results_diagnosis import PENDING_FINAL_MESSAGE
from backend.app.ui.results_view import (
    configure_results_loaders,
    refresh_student_diagnosis,
)

#: 学生会话状态：通过视图的学生守卫（含访问令牌与学生角色）。
_STUDENT_STATE: dict[str, Any] = {
    "access_token": "token",
    "user_id": "student-1",
    "roles": ["Student"],
}

class StubResultsService:
    """结果读模型替身：只记录调用并返回固定对象。"""

    def __init__(
        self,
        *,
        summaries: tuple[StudentResultSummaryDTO, ...] = (),
        result: Any = None,
        report: Any = None,
    ) -> None:
        self.summaries = list(summaries)
        self.result = result
        self.report = report
        self.calls: list[tuple[str, ...]] = []

    def list_student_results(self, student_id: str) -> list[StudentResultSummaryDTO]:
        self.calls.append(("list", student_id))
        return list(self.summaries)

    def get_student_result(self, student_id: str, submission_id: str) -> Any:
        self.calls.append(("result", student_id, submission_id))
        return self.result

    def get_student_diagnosis(self, student_id: str, submission_id: str) -> Any:
        self.calls.append(("diagnosis", student_id, submission_id))
        return self.report


def _summary(submission_id: str, exam_id: str) -> StudentResultSummaryDTO:
    """构造成绩列表条目。"""

    return StudentResultSummaryDTO(
        submission_id=submission_id,
        exam_id=exam_id,
        exam_title="Python 阶段测验",
        result_status=ExamResultStatus.FINAL,
        is_final=True,
        total_score=Decimal("10.00"),
        submitted_at=datetime(2026, 9, 16, 12, 0, tzinfo=UTC),
    )


@pytest.fixture(autouse=True)
def reset_loaders() -> Any:
    """每个用例前后恢复空态接线。"""

    configure_results_loaders()
    yield
    configure_results_loaders()


def test_resolve_submission_uses_authorised_scope() -> None:
    """按 exam_id 在学生授权答卷中定位 submission_id；未指定时取最新一条。"""

    service = StubResultsService(
        summaries=(_summary("submission-a", "exam-a"), _summary("submission-b", "exam-b"))
    )

    assert (
        results_loaders.resolve_student_submission_id(service, "student-1", "exam-b")
        == "submission-b"
    )
    assert (
        results_loaders.resolve_student_submission_id(service, "student-1", None)
        == "submission-a"
    )


def test_resolve_submission_returns_none_for_unknown_exam() -> None:
    """授权范围内没有该考试的答卷时不猜测、不返回答卷标识。"""

    service = StubResultsService(summaries=(_summary("submission-a", "exam-a"),))

    assert results_loaders.resolve_student_submission_id(service, "student-1", "exam-x") is None


def test_loaders_require_authenticated_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """未登录时直接返回空态，不构建查询服务、不访问数据库。"""

    def _explode() -> Any:
        raise AssertionError("未登录不得构建结果查询服务。")

    monkeypatch.setattr(
        results_loaders, "build_production_results_query_service", _explode
    )

    assert results_loaders.load_student_result("exam-1", {}) is None
    assert results_loaders.load_student_diagnosis("exam-1", {}) is None


def test_student_loaders_use_matched_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """加载器用定位到的 submission_id 查询详情与诊断。"""

    service = StubResultsService(
        summaries=(_summary("submission-a", "exam-a"), _summary("submission-b", "exam-b")),
        result={"submission_id": "submission-b", "is_final": True},
        report={"status": "Ready", "mastery_by_knowledge_point": []},
    )
    monkeypatch.setattr(
        results_loaders, "build_production_results_query_service", lambda: service
    )

    payload = results_loaders.load_student_result("exam-b", _STUDENT_STATE)
    report, is_final = results_loaders.load_student_diagnosis(  # type: ignore[misc]
        "exam-b", _STUDENT_STATE
    )

    assert payload == {"submission_id": "submission-b", "is_final": True}
    assert report["status"] == "Ready"
    assert is_final is True
    assert ("result", "student-1", "submission-b") in service.calls
    assert ("diagnosis", "student-1", "submission-b") in service.calls


def test_production_configuration_drives_view_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """按应用装配注入后，视图输出随真实读模型变化。"""

    service = StubResultsService(
        summaries=(_summary("submission-a", "exam-a"),),
        result={"submission_id": "submission-a", "is_final": False, "items": []},
        report={
            "status": "Ready",
            "mastery_by_knowledge_point": [
                {
                    "knowledge_point": "变量",
                    "answered_count": 1,
                    "correct_count": 0,
                    "awarded_score": "5.00",
                    "max_score": "10.00",
                    "mastery": "0.50",
                }
            ],
            "weak_knowledge_points": [],
            "error_reasons": ["question-1：未得满分。"],
            "learning_suggestions": ["复习变量作用域。"],
        },
    )
    monkeypatch.setattr(
        results_loaders, "build_production_results_query_service", lambda: service
    )

    results_loaders.configure_production_results_loaders()
    _, diagnosis, mastery = refresh_student_diagnosis("exam-a", _STUDENT_STATE)

    assert "复习变量作用域。" in diagnosis
    assert PENDING_FINAL_MESSAGE in diagnosis
    assert mastery == [["变量", "1", "0", "5.00", "10.00", "0.50"]]
