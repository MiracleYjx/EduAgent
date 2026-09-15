"""Question Router：按题目自身题型分流 Objective 与 Subjective 评分路径。

契约依据：spec.md 第 87 行、FR-029、FR-030，plan.md 第 513、514 与 634-635 行，
``.specify/contracts/agent-workflow.md`` 的 ``Classify Question`` 节点。

设计约束：

- 分流只读取题目自身的题型标识（``Question.type``），**不读取任何模型输出**，也不接受
  调用方以模型结论覆盖题型；模型输出不得反向改变评分路径。
- Objective 与 Subjective 的题型清单来自 :mod:`backend.app.domain.enums`，本模块不重复
  维护映射，避免出现两套题型归属。
- 未知题型与题型缺失都显式失败：错误码分别为 ``GRADING_UNKNOWN_QUESTION_TYPE`` 与
  ``GRADING_MISSING_QUESTION_TYPE``，出错时绝不默认归入任一模式，也不返回部分结果。
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from types import MappingProxyType
from typing import ClassVar, Final

from backend.app.domain.enums import (
    OBJECTIVE_QUESTION_TYPES,
    SUBJECTIVE_QUESTION_TYPES,
    GradingMode,
    QuestionType,
)

#: 题型标识不在支持的题型清单内；必须停止评分并标记异常。
QUESTION_TYPE_UNKNOWN: Final[str] = "GRADING_UNKNOWN_QUESTION_TYPE"
#: 题目缺少题型标识；属于上游数据异常，同样必须停止评分。
QUESTION_TYPE_MISSING: Final[str] = "GRADING_MISSING_QUESTION_TYPE"

#: 分流失败码对应的可读提示。
GRADING_ROUTING_MESSAGES: Final[Mapping[str, str]] = MappingProxyType(
    {
        QUESTION_TYPE_UNKNOWN: "题目题型不在支持的清单内，已停止评分，不得默认归类。",
        QUESTION_TYPE_MISSING: "题目缺少题型标识，已停止评分，不得默认归类。",
    }
)


class GradingRoutingError(RuntimeError):
    """题型分流失败基类；默认按不可重试的数据问题处理。"""

    error_code: ClassVar[str] = QUESTION_TYPE_UNKNOWN
    retryable: ClassVar[bool] = False

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.error_code}：{detail}")
        self.detail = detail


class UnknownQuestionTypeError(GradingRoutingError):
    """题型标识无法识别；不得把主观题当作客观题或反之。"""

    error_code: ClassVar[str] = QUESTION_TYPE_UNKNOWN


class MissingQuestionTypeError(GradingRoutingError):
    """题目没有可用的题型标识；不得用默认题型兜底。"""

    error_code: ClassVar[str] = QUESTION_TYPE_MISSING


def _build_mode_map() -> Mapping[QuestionType, GradingMode]:
    """由领域题型清单构造题型到评分模式的映射，并校验没有遗漏题型。"""

    mode_map: dict[QuestionType, GradingMode] = {}
    for question_type in sorted(OBJECTIVE_QUESTION_TYPES):
        mode_map[question_type] = GradingMode.OBJECTIVE
    for question_type in sorted(SUBJECTIVE_QUESTION_TYPES):
        mode_map[question_type] = GradingMode.SUBJECTIVE
    uncovered = set(QuestionType) - set(mode_map)
    if uncovered:
        raise GradingRoutingError(
            "存在未定义评分模式的题型：" + "、".join(sorted(uncovered)) + "。"
        )
    return MappingProxyType(mode_map)


#: 题型到评分模式的唯一映射，由领域题型清单派生。
MODE_BY_QUESTION_TYPE: Final[Mapping[QuestionType, GradingMode]] = _build_mode_map()


def normalize_question_type(value: QuestionType | str | None) -> QuestionType:
    """把外部题型标识归一为 :class:`QuestionType`。

    归一化范围限定为：``NFKC`` 兼容归一（全角转半角）、首尾去空白、大小写不敏感匹配。
    不做同义词、别名或近义题型猜测；无法识别时抛出 :class:`UnknownQuestionTypeError`。
    """

    if isinstance(value, QuestionType):
        return value
    if value is None:
        raise MissingQuestionTypeError("题目缺少题型标识，无法确定评分路径。")
    if not isinstance(value, str):
        raise UnknownQuestionTypeError(
            f"题型标识类型不受支持：{type(value).__name__}。"
        )
    candidate = unicodedata.normalize("NFKC", value).strip()
    if not candidate:
        raise UnknownQuestionTypeError("题型标识为空字符串，无法确定评分路径。")
    try:
        return QuestionType(candidate)
    except ValueError:
        pass
    folded = candidate.casefold()
    for member in QuestionType:
        if member.value.casefold() == folded:
            return member
    raise UnknownQuestionTypeError(
        f"未知题型：{candidate}，不默认归入任何评分模式。"
    )


class QuestionRouter:
    """无状态题型分流器。

    :meth:`route` 面向 ORM 题目对象或测试替身，只读取 ``type`` 属性；
    :meth:`route_type` 面向已经取出的题型标识。两者共用同一映射与同一套失败语义。
    """

    def route(self, question: object) -> GradingMode:
        """按题目自身题型返回评分模式，忽略题目上的其它字段与模型输出。"""

        return self.route_type(getattr(question, "type", None))

    def route_type(self, question_type: QuestionType | str | None) -> GradingMode:
        """按题型标识返回评分模式；未知题型或题型缺失时显式失败。"""

        normalized = normalize_question_type(question_type)
        mode = MODE_BY_QUESTION_TYPE.get(normalized)
        if mode is None:
            raise UnknownQuestionTypeError(
                f"未知题型：{normalized}，不默认归入任何评分模式。"
            )
        return mode


__all__ = [
    "GRADING_ROUTING_MESSAGES",
    "MODE_BY_QUESTION_TYPE",
    "QUESTION_TYPE_MISSING",
    "QUESTION_TYPE_UNKNOWN",
    "GradingRoutingError",
    "MissingQuestionTypeError",
    "QuestionRouter",
    "UnknownQuestionTypeError",
    "normalize_question_type",
]
