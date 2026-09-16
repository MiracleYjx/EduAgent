"""T057 生产结果与诊断加载器：把 ``ResultsQueryService`` 接入学生/教师结果视图。

S01 约束：

- 学生刷新入口接收 ``exam_id``，先用**当前学生授权范围内的答卷**定位 ``submission_id``，
  再调用详情查询；``exam_id`` 与 ``submission_id`` 不混用、不互相替代。
- 每次刷新自建查询服务与数据库会话，模块级不保存用户身份、不共享 Session。
- 加载器只读：诊断来自已持久化报告，刷新不会触发生成与 LLM 调用；任何失败由视图转为
  明确空态，不伪装成成绩或诊断结论。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from backend.app.api.results import (
    ResultsQueryService,
    build_production_results_query_service,
)
from backend.app.schemas.grading import (
    DiagnosisReportDTO,
    StudentResultSummaryDTO,
    SubmissionResultDTO,
)
from backend.app.ui.results_view import configure_results_loaders


def state_user_id(state: Mapping[str, Any] | None) -> str:
    """从会话状态读取当前用户标识；缺失时返回空串。"""

    if not isinstance(state, Mapping):
        return ""
    return str(state.get("user_id") or "").strip()


def resolve_student_submission_id(
    service: ResultsQueryService,
    student_id: str,
    exam_id: str | None,
) -> str | None:
    """S01：在学生授权范围内按 ``exam_id`` 定位答卷标识。"""

    target = str(exam_id or "").strip()
    for summary in service.list_student_results(student_id):
        assert isinstance(summary, StudentResultSummaryDTO)
        if not target or str(summary.exam_id) == target:
            return str(summary.submission_id)
    return None


def load_student_result(
    exam_id: str | None = None,
    state: Mapping[str, Any] | None = None,
) -> SubmissionResultDTO | None:
    """学生逐题结果加载器；无法定位授权答卷时返回 ``None``（视图显示空态）。"""

    student_id = state_user_id(state)
    if not student_id:
        return None
    service = build_production_results_query_service()
    submission_id = resolve_student_submission_id(service, student_id, exam_id)
    if submission_id is None:
        return None
    return service.get_student_result(student_id, submission_id)


def _is_final_result(result: Any) -> bool:
    """读取整卷是否已形成最终成绩；对象与映射两种读模型都支持。"""

    if isinstance(result, Mapping):
        return bool(result.get("is_final", False))
    return bool(getattr(result, "is_final", False))


def load_student_diagnosis(
    exam_id: str | None = None,
    state: Mapping[str, Any] | None = None,
) -> tuple[DiagnosisReportDTO, bool] | None:
    """学生诊断加载器；一并返回整卷是否已形成最终成绩，供视图标注待复核。"""

    student_id = state_user_id(state)
    if not student_id:
        return None
    service = build_production_results_query_service()
    submission_id = resolve_student_submission_id(service, student_id, exam_id)
    if submission_id is None:
        return None
    result = service.get_student_result(student_id, submission_id)
    report = service.get_student_diagnosis(student_id, submission_id)
    return report, _is_final_result(result)


def load_teacher_results(
    course_id: str | None = None,
    exam_id: str | None = None,
    state: Mapping[str, Any] | None = None,
) -> list[StudentResultSummaryDTO] | None:
    """教师结果加载器；未选择考试时返回 ``None``，由视图显示明确空态。"""

    teacher_id = state_user_id(state)
    target_exam = str(exam_id or "").strip()
    if not teacher_id or not target_exam:
        return None
    service = build_production_results_query_service()
    return service.list_exam_results(teacher_id, target_exam)


def configure_production_results_loaders() -> None:
    """把生产加载器注入结果视图；应用装配期间调用一次。"""

    configure_results_loaders(
        student_loader=load_student_result,
        student_diagnosis_loader=load_student_diagnosis,
        teacher_loader=load_teacher_results,
    )


__all__ = [
    "configure_production_results_loaders",
    "load_student_diagnosis",
    "load_student_result",
    "load_teacher_results",
    "resolve_student_submission_id",
    "state_user_id",
]
