"""T046 Question Router 失败优先测试：题型分流与未知题型显式失败。

任务编号：T046（M3 AI 阅卷；实现见 T047）。
必要性：答案解析只能由题目自身题型（``Question.type``）分流。若路由缺失、默认到某一类，
或让模型输出参与分流，主观题会被当作客观题做确定性评分（反之亦然），直接违反
spec.md 第 87 行、FR-029/FR-030、plan.md 第 513 与 634-635 行的约束，并使
objective_grader “不调用 LLM” 的铁律失去前提。
覆盖内容：
1. Objective 四类题型（SINGLE_CHOICE/MULTIPLE_CHOICE/TRUE_FALSE/FILL_BLANK）分流为
   ``GradingMode.OBJECTIVE``；
2. Subjective 两类题型（SHORT_ANSWER/ESSAY）分流为 ``GradingMode.SUBJECTIVE``；
3. 未知题型与题型缺失分别抛出显式异常，错误码可区分，且绝不落入任何一类；
4. 题目替身携带模型侧字段（``grading_mode``/``model_output``）或干扰字段时路由结果不变；
5. ORM 风格替身、``QuestionType`` 枚举与等价字符串三种入参形态，以及 ``route`` 误用时报错。
执行方法（先红后绿）：``python -m pytest tests/unit/grading/test_question_router.py -q``；
实现存在前预期失败原因为目标模块与符号缺失，不使用 mock 绕过被测路由逻辑。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from backend.app.domain.enums import (
    OBJECTIVE_QUESTION_TYPES,
    SUBJECTIVE_QUESTION_TYPES,
    GradingMode,
    QuestionType,
)
from backend.app.services.grading.question_router import (
    QUESTION_TYPE_MISSING,
    QUESTION_TYPE_UNKNOWN,
    GradingRoutingError,
    MissingQuestionTypeError,
    QuestionRouter,
    UnknownQuestionTypeError,
    normalize_question_type,
)

OBJECTIVE_CASES = (
    QuestionType.SINGLE_CHOICE,
    QuestionType.MULTIPLE_CHOICE,
    QuestionType.TRUE_FALSE,
    QuestionType.FILL_BLANK,
)
SUBJECTIVE_CASES = (QuestionType.SHORT_ANSWER, QuestionType.ESSAY)
UNKNOWN_VALUES = ("MATCHING", "SINGLE", "", "   ")


def _question(question_type: QuestionType | str | None) -> SimpleNamespace:
    """构造只带题型标识的题目替身，避免单元测试依赖数据库模型。"""

    return SimpleNamespace(type=question_type)


@pytest.fixture(name="router")
def router_fixture() -> QuestionRouter:
    """被测路由：无状态，可重复使用。"""

    return QuestionRouter()


@pytest.mark.parametrize("question_type", OBJECTIVE_CASES)
def test_objective_question_types_route_to_objective(
    router: QuestionRouter,
    question_type: QuestionType,
) -> None:
    """Objective 题型必须分流到确定性规则评分。"""

    assert router.route(_question(question_type)) is GradingMode.OBJECTIVE
    assert router.route_type(question_type) is GradingMode.OBJECTIVE


@pytest.mark.parametrize("question_type", SUBJECTIVE_CASES)
def test_subjective_question_types_route_to_subjective(
    router: QuestionRouter,
    question_type: QuestionType,
) -> None:
    """Subjective 题型必须分流到 RAG + LLM 评分。"""

    assert router.route(_question(question_type)) is GradingMode.SUBJECTIVE
    assert router.route_type(question_type) is GradingMode.SUBJECTIVE


def test_every_supported_question_type_is_classified(router: QuestionRouter) -> None:
    """全部题型都必须被显式分流，不允许存在无人负责的题型。"""

    assert OBJECTIVE_QUESTION_TYPES | SUBJECTIVE_QUESTION_TYPES == set(QuestionType)
    for question_type in QuestionType:
        assert router.route_type(question_type) in (
            GradingMode.OBJECTIVE,
            GradingMode.SUBJECTIVE,
        )


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("SINGLE_CHOICE", QuestionType.SINGLE_CHOICE),
        ("multiple_choice", QuestionType.MULTIPLE_CHOICE),
        ("  Essay  ", QuestionType.ESSAY),
        ("TRUE_FALSE", QuestionType.TRUE_FALSE),
        ("fill_blank", QuestionType.FILL_BLANK),
        ("ＳＨＯＲＴ＿ＡＮＳＷＥＲ", QuestionType.SHORT_ANSWER),
    ],
)
def test_route_type_accepts_equivalent_text(
    router: QuestionRouter,
    raw_value: str,
    expected: QuestionType,
) -> None:
    """题型标识来自数据库或接口，大小写、空白与全角差异必须被归一化。"""

    assert normalize_question_type(raw_value) is expected
    assert router.route_type(raw_value) is router.route_type(expected)


@pytest.mark.parametrize("raw_value", UNKNOWN_VALUES)
def test_unknown_question_type_raises_explicit_error(
    router: QuestionRouter,
    raw_value: str,
) -> None:
    """未知题型必须显式报错，错误码与不可重试标记对外可见。"""

    with pytest.raises(UnknownQuestionTypeError) as excinfo:
        router.route_type(raw_value)
    assert excinfo.value.error_code == QUESTION_TYPE_UNKNOWN
    assert excinfo.value.retryable is False
    assert GradingRoutingError in type(excinfo.value).__mro__


def test_unknown_question_type_never_defaults_to_a_mode(router: QuestionRouter) -> None:
    """未知题型不得默认到 Objective 或 Subjective。"""

    for raw_value in UNKNOWN_VALUES:
        with pytest.raises(GradingRoutingError):
            router.route(_question(raw_value))
        with pytest.raises(GradingRoutingError):
            router.route_type(raw_value)


@pytest.mark.parametrize(
    "question",
    [SimpleNamespace(type=None), SimpleNamespace()],
)
def test_missing_question_type_raises_explicit_error(
    router: QuestionRouter,
    question: SimpleNamespace,
) -> None:
    """题型缺失属于数据异常，必须与未知题型区分并显式失败。"""

    with pytest.raises(MissingQuestionTypeError) as excinfo:
        router.route(question)
    assert excinfo.value.error_code == QUESTION_TYPE_MISSING
    assert excinfo.value.retryable is False


def test_route_rejects_raw_question_type_value(router: QuestionRouter) -> None:
    """route() 只接受题目对象；直接传题型值属于误用，必须显式失败。"""

    misused: Any = "SINGLE_CHOICE"
    with pytest.raises(MissingQuestionTypeError):
        router.route(misused)


def test_model_output_does_not_change_routing(router: QuestionRouter) -> None:
    """题型路由只读取 Question.type，模型输出不得反向改变分流结果。"""

    subjective = SimpleNamespace(
        type=QuestionType.ESSAY,
        grading_mode="Objective",
        model_output={"question_type": "SINGLE_CHOICE"},
    )
    objective = SimpleNamespace(
        type=QuestionType.SINGLE_CHOICE,
        grading_mode="Subjective",
        model_output={"question_type": "ESSAY", "answer": "B"},
    )

    assert router.route(subjective) is GradingMode.SUBJECTIVE
    assert router.route(objective) is GradingMode.OBJECTIVE


def test_router_ignores_unrelated_question_fields(router: QuestionRouter) -> None:
    """题目内容、参考答案与分值不参与分流。"""

    stub = SimpleNamespace(
        type=QuestionType.FILL_BLANK,
        content="请填写标准库名称：____",
        reference_answer="SINGLE_CHOICE",
        scoring_rubric="ESSAY",
        score=5.0,
    )

    assert router.route(stub) is GradingMode.OBJECTIVE


def test_router_is_reusable(router: QuestionRouter) -> None:
    """路由无状态：重复调用结果一致，供多次阅卷复用。"""

    results = {
        router.route(_question(QuestionType.TRUE_FALSE))
        for _ in range(100)
    }

    assert results == {GradingMode.OBJECTIVE}
