"""T057 学生诊断区域的只读展示映射。

本模块只做**展示映射**：把结果与诊断读模型转成 Markdown 与表格数据，不访问数据库、
不重算成绩与掌握度、也不触发诊断生成。状态语义与 plan §5.2 / FR-038 一致：

- ``Ready``：展示薄弱知识点、错误原因与学习建议、知识点掌握度；
- ``Not Ready``：明确空态（成绩最终确认后才会生成），不用 0 或空结论冒充；
- ``Stale``：明确说明报告已过期，不再代表当前最终诊断；
- ``Failed``：展示失败错误码并说明成绩不受影响；
- 成绩未最终确认（待复核）：只展示已确认部分并说明最终成绩尚未形成。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from backend.app.ui.layout_view import empty_state

#: 知识点掌握度表头。
MASTERY_HEADERS: tuple[str, ...] = (
    "知识点",
    "题目数",
    "完全正确",
    "已确认得分",
    "满分",
    "掌握度",
)
#: 掌握度表格列类型：数值列交给 Gradio 排序展示。
MASTERY_TABLE_DATATYPES: tuple[Literal["str", "number"], ...] = (
    "str",
    "number",
    "number",
    "number",
    "number",
    "str",
)

#: 诊断尚未生成时的空态说明。
DIAGNOSIS_NOT_READY_MESSAGE = "诊断报告尚未生成：成绩最终确认后才会生成诊断。"
#: 诊断已过期时的说明。
DIAGNOSIS_STALE_MESSAGE = "诊断报告已过期：该报告生成后成绩已更新，请以最新成绩为准。"
#: 待复核时不得把未确认结果当作最终结论。
PENDING_FINAL_MESSAGE = "成绩待人工复核：待复核题目不计入最终成绩，诊断仅覆盖已确认部分。"


def _value(item: Mapping[str, Any] | Any, name: str, default: Any = "") -> Any:
    """从服务 DTO 或映射中读取字段，不改变服务返回值。"""

    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _display(value: Any, fallback: str = "未提供") -> str:
    """把字段值转换为展示文本；``None`` 与空串使用明确占位，数值 0 必须保留。"""

    if value is None:
        return fallback
    text = str(getattr(value, "value", value)).strip()
    return text or fallback


def diagnosis_status(report: Mapping[str, Any] | Any | None) -> str:
    """读取诊断状态码；不按分数或其它字段推导状态。"""

    return _display(_value(report, "status", ""), fallback="")


def mastery_rows(report: Mapping[str, Any] | Any | None) -> list[list[str]]:
    """把知识点掌握度映射为表格行；数值直接取服务端返回值。"""

    if report is None:
        return []
    rows: list[list[str]] = [
        [
            _display(_value(item, "knowledge_point")),
            _display(_value(item, "answered_count")),
            _display(_value(item, "correct_count")),
            _display(_value(item, "awarded_score")),
            _display(_value(item, "max_score")),
            _display(_value(item, "mastery")),
        ]
        for item in _value(report, "mastery_by_knowledge_point", []) or []
    ]
    return rows


def weak_knowledge_points_text(report: Mapping[str, Any] | Any | None) -> str:
    """薄弱知识点区域：逐条给出知识点、掌握度与判定原因。"""

    if report is None:
        return empty_state(DIAGNOSIS_NOT_READY_MESSAGE)
    entries = _value(report, "weak_knowledge_points", []) or []
    lines = [
        "- {name}：掌握度 {mastery}。{reason}".format(
            name=_display(_value(item, "knowledge_point")),
            mastery=_display(_value(item, "mastery")),
            reason=_display(_value(item, "reason"), fallback=""),
        )
        for item in entries
    ]
    if not lines:
        return empty_state("暂无可展示的薄弱知识点。")
    return "\n".join(lines)


def _bullets(values: Sequence[Any] | None, fallback: str) -> str:
    """把字符串集合渲染为 Markdown 列表；空集合给出明确空态。"""

    entries = [str(item).strip() for item in values or [] if str(item).strip()]
    if not entries:
        return empty_state(fallback)
    return "\n".join(f"- {entry}" for entry in entries)


def diagnosis_text(
    report: Mapping[str, Any] | Any | None,
    *,
    is_final: bool = False,
) -> str:
    """错误原因与学习建议区域：状态、原因与建议分开展示。

    :param is_final: 整卷是否已形成最终成绩；未最终确认时明确说明诊断只覆盖已确认部分。
    """

    if report is None:
        return empty_state(DIAGNOSIS_NOT_READY_MESSAGE)
    status = diagnosis_status(report)
    if status == "Not Ready":
        return empty_state(DIAGNOSIS_NOT_READY_MESSAGE)
    if status == "Stale":
        return (
            f"{empty_state(DIAGNOSIS_STALE_MESSAGE)}\n\n"
            f"### 错误原因\n\n{_bullets(_value(report, 'error_reasons', []), '暂无错误原因。')}"
        )
    if status == "Failed":
        code = _display(_value(report, "error_code"), fallback="未知错误")
        return empty_state(f"诊断生成失败（{code}），已提交成绩不受影响。")
    prefix = "" if is_final else f"{PENDING_FINAL_MESSAGE}\n\n"
    reasons = _bullets(_value(report, "error_reasons", []), "暂无错误原因。")
    suggestions = _bullets(_value(report, "learning_suggestions", []), "暂无学习建议。")
    return f"{prefix}### 错误原因\n\n{reasons}\n\n### 学习建议\n\n{suggestions}"


def diagnosis_sections(
    report: Mapping[str, Any] | Any | None,
    *,
    is_final: bool = False,
) -> tuple[str, str, list[list[str]]]:
    """返回 (薄弱知识点, 错误原因与建议, 掌握度表格)。

    只有 ``Ready`` 报告才展示薄弱知识点与掌握度；未生成、过期与失败时两个区域都给出明确
    说明，不用旧数据或空数据冒充结论。
    """

    if report is None:
        message = empty_state(DIAGNOSIS_NOT_READY_MESSAGE)
        return message, message, []
    detail = diagnosis_text(report, is_final=is_final)
    status = diagnosis_status(report)
    if status == "Ready":
        return weak_knowledge_points_text(report), detail, mastery_rows(report)
    if status == "Stale":
        return empty_state(DIAGNOSIS_STALE_MESSAGE), detail, []
    return detail, detail, []


__all__ = [
    "DIAGNOSIS_NOT_READY_MESSAGE",
    "DIAGNOSIS_STALE_MESSAGE",
    "MASTERY_HEADERS",
    "MASTERY_TABLE_DATATYPES",
    "PENDING_FINAL_MESSAGE",
    "diagnosis_sections",
    "diagnosis_status",
    "diagnosis_text",
    "mastery_rows",
    "weak_knowledge_points_text",
]
