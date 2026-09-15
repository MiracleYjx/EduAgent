"""T048 Objective Grader 失败优先测试：客观题确定性规则评分。

任务编号：T048（M3 AI 阅卷；实现见 T049）。
必要性：客观题评分必须只由「学生答案 + 标准答案 + 题目分值」确定并可控重算
（data-model.md 「Objective 题的评分结果必须可由学生答案、标准答案和题目分值重算」），
且不得调用 LLM（FR-030、SC-004、plan.md 第 514 行）。评分器同时承担结构化输出铁律
（Constitution IV）与显式失败原则：越界、编码非法、标准答案缺失都必须报错而不是猜分。
覆盖内容：
1. 正确答案满分、错误答案 0 分、缺失答案 0 分（与 ``submission_service`` 既有缺失判定一致）；
2. 重复答案（同一题内多选键重复）按集合去重，不额外加分也不扣分；不涉及跨题重复
   （跨题重复仍由 ``submission_service`` 拒绝，本批不修改）；
3. 多选满对 / 漏选比例分 / 错选 0 分 / 空集合 0 分，含 ``ROUND_HALF_UP`` 两位小数舍入边界；
4. 归一化范围：NFKC（全角转半角）、首尾与内部空白折叠、大小写不敏感；
   不做同义词、数字格式（"3.0" vs "3"）与标点等价归一；
5. 显式失败：非法满分（≤0、NaN、±inf、非数值）、标准答案缺失、答案编码非法
   （dict、非字符串项、单选多值、无分隔符且非已知选项键的多选串、选项键越界）、
   主观题题型误入、未知或缺失题型；
6. 分数由规则直接产生且不做越界钳制；DTO 字段全量断言（含 question_type、confidence=1.0、
   validation_status=Validated、review_status=Not Required）；
7. SC-004：对同一组客观题答案重复评分 100 次结果完全一致；
8. 铁律：不调用 LLM / Embedding / Rerank（模块导入图检查 + 网络调用守卫双重证据）。
执行方法（先红后绿）：``python -m pytest tests/unit/grading/test_objective_grader.py -q``；
实现存在前预期失败原因为目标模块与符号缺失。
"""

from __future__ import annotations

import ast
import socket
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from backend.app.domain.enums import QuestionType
from backend.app.schemas.ai import GradingResult
from backend.app.services.grading import objective_grader as objective_grader_module
from backend.app.services.grading.objective_grader import (
    GRADING_INTERNAL_INVARIANT_VIOLATION,
    GRADING_INVALID_ANSWER_FORMAT,
    GRADING_INVALID_MAX_SCORE,
    GRADING_MISSING_REFERENCE_ANSWER,
    GRADING_MODE_MISMATCH,
    GradingInvariantError,
    GradingModeMismatchError,
    InvalidAnswerFormatError,
    InvalidMaxScoreError,
    MissingReferenceAnswerError,
    ObjectiveGrader,
    ObjectiveGradingError,
    is_blank_answer,
    parse_multiple_choice_keys,
)
from backend.app.services.grading.question_router import (
    QUESTION_TYPE_MISSING,
    QUESTION_TYPE_UNKNOWN,
    GradingRoutingError,
    UnknownQuestionTypeError,
)
from backend.app.services.submission_service import (
    _is_blank_answer as submission_service_is_blank_answer,
)

EXPECTED_RESULT_FIELDS = {
    "question_type",
    "score",
    "max_score",
    "reason",
    "correct_points",
    "missing_knowledge_points",
    "knowledge_points",
    "suggestions",
    "confidence",
    "validation_status",
    "review_status",
    "retrieved_context_ids",
    "answer_id",
    "submission_id",
}


@pytest.fixture(name="grader")
def grader_fixture() -> ObjectiveGrader:
    """被测评分器：无状态、不依赖数据库与模型。"""

    return ObjectiveGrader()


def _grade(
    grader: ObjectiveGrader,
    *,
    question_type: QuestionType | str | None,
    reference_answer: str | None,
    student_answer: Any,
    max_score: Any = 5.0,
    option_keys: Any = None,
    knowledge_points: tuple[str, ...] = ("变量作用域",),
    answer_id: str | None = None,
    submission_id: str | None = None,
) -> GradingResult:
    """以关键字调用评分器，保持测试与生产调用形态一致。"""

    return grader.grade(
        question_type=question_type,
        reference_answer=reference_answer,
        student_answer=student_answer,
        max_score=max_score,
        option_keys=option_keys,
        knowledge_points=knowledge_points,
        answer_id=answer_id,
        submission_id=submission_id,
    )


@pytest.mark.parametrize(
    ("question_type", "reference_answer", "student_answer"),
    [
        (QuestionType.SINGLE_CHOICE, "B", "B"),
        (QuestionType.SINGLE_CHOICE, "Ｂ", "B"),
        (QuestionType.TRUE_FALSE, "True", "TRUE"),
        (QuestionType.FILL_BLANK, "Python 标准库", "  python   标准库 "),
    ],
)
def test_objective_exact_match_scores_full_marks(
    grader: ObjectiveGrader,
    question_type: QuestionType,
    reference_answer: str,
    student_answer: str,
) -> None:
    """归一化后与标准答案一致的客观题必须给满分。"""

    result = _grade(
        grader,
        question_type=question_type,
        reference_answer=reference_answer,
        student_answer=student_answer,
        max_score=5.0,
    )

    assert result.question_type is question_type
    assert result.score == 5.0
    assert result.max_score == 5.0
    assert result.correct_points == ["变量作用域"]
    assert result.missing_knowledge_points == []
    assert result.suggestions


def test_single_choice_wrong_answer_scores_zero(grader: ObjectiveGrader) -> None:
    """错误答案必须记 0 分并回填未命中的知识点。"""

    result = _grade(
        grader,
        question_type=QuestionType.SINGLE_CHOICE,
        reference_answer="B",
        student_answer="C",
    )

    assert result.score == 0.0
    assert result.missing_knowledge_points == ["变量作用域"]
    assert result.correct_points == []
    assert "不一致" in result.reason


@pytest.mark.parametrize("student_answer", [None, "", "   ", [], ["  "]])
def test_missing_answer_scores_zero(
    grader: ObjectiveGrader,
    student_answer: Any,
) -> None:
    """答案缺失必须记 0 分，并在理由中说明缺失。"""

    result = _grade(
        grader,
        question_type=QuestionType.SINGLE_CHOICE,
        reference_answer="B",
        student_answer=student_answer,
    )

    assert result.score == 0.0
    assert "缺失" in result.reason
    assert result.missing_knowledge_points == ["变量作用域"]


@pytest.mark.parametrize(
    "value",
    [None, "", "   ", [], ["  ", "\t"], {}, {"A": "  "}, "B", ["B"], {"A": "x"}],
)
def test_blank_answer_detection_matches_submission_service(value: Any) -> None:
    """缺失判定必须与既有 ``submission_service`` 语义一致，避免两套标准。"""

    assert is_blank_answer(value) is submission_service_is_blank_answer(value)


def test_multiple_choice_exact_match_scores_full_marks(
    grader: ObjectiveGrader,
) -> None:
    """多选顺序不同但集合相同，必须给满分。"""

    result = _grade(
        grader,
        question_type=QuestionType.MULTIPLE_CHOICE,
        reference_answer="A,C",
        student_answer="C , A",
        max_score=5.0,
    )

    assert result.score == 5.0
    assert result.missing_knowledge_points == []


@pytest.mark.parametrize(
    ("student_answer", "expected_score"),
    [("A", 1.0), ("A,B", 2.0), ("B", 1.0)],
)
def test_multiple_choice_missing_selection_scores_proportionally(
    grader: ObjectiveGrader,
    student_answer: str,
    expected_score: float,
) -> None:
    """多选漏选（无错选）按命中比例给分：满分 × 命中数 / 标准答案数。"""

    result = _grade(
        grader,
        question_type=QuestionType.MULTIPLE_CHOICE,
        reference_answer="A,B,C",
        student_answer=student_answer,
        max_score=3.0,
    )

    assert result.score == expected_score
    assert "漏选" in result.reason


def test_multiple_choice_ratio_uses_round_half_up(grader: ObjectiveGrader) -> None:
    """比例分保留两位小数并采用 ``ROUND_HALF_UP``（0.625 → 0.63）。"""

    result = _grade(
        grader,
        question_type=QuestionType.MULTIPLE_CHOICE,
        reference_answer="A,B,C,D",
        student_answer="A",
        max_score=2.5,
    )

    assert result.score == 0.63


@pytest.mark.parametrize(
    ("reference_answer", "student_answer", "option_keys"),
    [
        ("A,C", "A,B", ["A", "B", "C"]),
        ("A,C", "A,C,D", ["A", "B", "C", "D"]),
        ("A", "B", ["A", "B"]),
    ],
)
def test_multiple_choice_wrong_selection_scores_zero(
    grader: ObjectiveGrader,
    reference_answer: str,
    student_answer: str,
    option_keys: list[str],
) -> None:
    """存在错选时不得分，也不倒扣。"""

    result = _grade(
        grader,
        question_type=QuestionType.MULTIPLE_CHOICE,
        reference_answer=reference_answer,
        student_answer=student_answer,
        max_score=4.0,
        option_keys=option_keys,
    )

    assert result.score == 0.0
    assert "错选" in result.reason
    assert result.score >= 0.0


@pytest.mark.parametrize("student_answer", ["", "  ", None, []])
def test_multiple_choice_empty_answer_scores_zero(
    grader: ObjectiveGrader,
    student_answer: Any,
) -> None:
    """多选空集合必须记 0 分。"""

    result = _grade(
        grader,
        question_type=QuestionType.MULTIPLE_CHOICE,
        reference_answer="A,C",
        student_answer=student_answer,
        max_score=4.0,
    )

    assert result.score == 0.0
    assert result.missing_knowledge_points


@pytest.mark.parametrize("student_answer", ["A,A,B,B", ["A", "A", "B"]])
def test_multiple_choice_duplicate_keys_are_deduplicated(
    grader: ObjectiveGrader,
    student_answer: Any,
) -> None:
    """同一题内重复的选项键按集合去重：不额外加分，也不因重复扣分。"""

    result = _grade(
        grader,
        question_type=QuestionType.MULTIPLE_CHOICE,
        reference_answer="A,B",
        student_answer=student_answer,
        max_score=5.0,
        option_keys=["A", "B", "C"],
    )

    assert result.score == 5.0
    assert result.score <= result.max_score


def test_multiple_choice_unknown_question_type_propagates_router_error(
    grader: ObjectiveGrader,
) -> None:
    """未知题型必须沿用 Router 的错误码，不得被评分器改写。"""

    with pytest.raises(UnknownQuestionTypeError) as excinfo:
        _grade(
            grader,
            question_type="MATCHING",
            reference_answer="A",
            student_answer="A",
        )

    assert excinfo.value.error_code == QUESTION_TYPE_UNKNOWN
    assert GradingModeMismatchError not in type(excinfo.value).__mro__


@pytest.mark.parametrize("student_answer", ["AB", "AC", "ABC"])
def test_multiple_choice_undelimited_text_is_rejected(
    grader: ObjectiveGrader,
    student_answer: str,
) -> None:
    """无分隔符且无法匹配已知选项键的多选串必须显式报错，不自行拆字符。"""

    with pytest.raises(InvalidAnswerFormatError) as excinfo:
        _grade(
            grader,
            question_type=QuestionType.MULTIPLE_CHOICE,
            reference_answer="A,B",
            student_answer=student_answer,
            max_score=5.0,
        )

    assert excinfo.value.error_code == GRADING_INVALID_ANSWER_FORMAT


def test_multiple_choice_accepts_undelimited_text_matching_known_key(
    grader: ObjectiveGrader,
) -> None:
    """选项键本身是多字符时（如 A1），无分隔符文本是合法单键。"""

    result = _grade(
        grader,
        question_type=QuestionType.MULTIPLE_CHOICE,
        reference_answer="A1",
        student_answer="A1",
        max_score=2.0,
        option_keys=["A1", "B1"],
    )

    assert result.score == 2.0


def test_multiple_choice_sequence_items_must_be_known_keys(
    grader: ObjectiveGrader,
) -> None:
    """列表形态逐项作为键：越界键必须报错，不能被当作“靠猜测拆出的键”。"""

    with pytest.raises(InvalidAnswerFormatError) as excinfo:
        _grade(
            grader,
            question_type=QuestionType.MULTIPLE_CHOICE,
            reference_answer="A1",
            student_answer=["A", "1"],
            max_score=2.0,
            option_keys=["A1", "B1"],
        )

    assert excinfo.value.error_code == GRADING_INVALID_ANSWER_FORMAT


@pytest.mark.parametrize(
    ("reference_answer", "student_answer"),
    [("A", "A,C"), ("A,D", "A"), ("A", "c")],
)
def test_multiple_choice_keys_outside_option_keys_are_rejected(
    grader: ObjectiveGrader,
    reference_answer: str,
    student_answer: str,
) -> None:
    """提供选项键集合时，标准答案与学生答案的键都必须落在该集合内。"""

    with pytest.raises(InvalidAnswerFormatError):
        _grade(
            grader,
            question_type=QuestionType.MULTIPLE_CHOICE,
            reference_answer=reference_answer,
            student_answer=student_answer,
            max_score=2.0,
            option_keys=["A", "B"],
        )


def test_multiple_choice_empty_option_keys_are_rejected(
    grader: ObjectiveGrader,
) -> None:
    """显式提供空选项键集合时无法校验答案，必须报错。"""

    with pytest.raises(InvalidAnswerFormatError):
        _grade(
            grader,
            question_type=QuestionType.MULTIPLE_CHOICE,
            reference_answer="A",
            student_answer="A",
            max_score=2.0,
            option_keys=[],
        )


@pytest.mark.parametrize("reference_answer", [None, "", "   "])
def test_missing_reference_answer_raises(
    grader: ObjectiveGrader,
    reference_answer: str | None,
) -> None:
    """标准答案缺失时必须显式失败，不得默认为 0 分掩盖题目数据问题。"""

    with pytest.raises(MissingReferenceAnswerError) as excinfo:
        _grade(
            grader,
            question_type=QuestionType.SINGLE_CHOICE,
            reference_answer=reference_answer,
            student_answer="B",
        )

    assert excinfo.value.error_code == GRADING_MISSING_REFERENCE_ANSWER
    assert excinfo.value.retryable is False


@pytest.mark.parametrize(
    "max_score",
    [0, 0.0, -1.0, float("nan"), float("inf"), float("-inf"), "5", None, True],
)
def test_invalid_max_score_raises(
    grader: ObjectiveGrader,
    max_score: Any,
) -> None:
    """满分必须为有限正数；非有限、非数值与非正数都必须显式失败。"""

    with pytest.raises(InvalidMaxScoreError) as excinfo:
        _grade(
            grader,
            question_type=QuestionType.SINGLE_CHOICE,
            reference_answer="B",
            student_answer="B",
            max_score=max_score,
        )

    assert excinfo.value.error_code == GRADING_INVALID_MAX_SCORE


@pytest.mark.parametrize(
    "question_type", [QuestionType.SHORT_ANSWER, QuestionType.ESSAY, "ESSAY"]
)
def test_subjective_question_type_is_rejected(
    grader: ObjectiveGrader,
    question_type: QuestionType | str,
) -> None:
    """主观题不得进入规则评分器，必须报错交给 Subjective 路径。"""

    with pytest.raises(GradingModeMismatchError) as excinfo:
        _grade(
            grader,
            question_type=question_type,
            reference_answer="B",
            student_answer="B",
            max_score=5.0,
        )

    assert excinfo.value.error_code == GRADING_MODE_MISMATCH
    assert isinstance(excinfo.value, ObjectiveGradingError)


def test_missing_question_type_raises(grader: ObjectiveGrader) -> None:
    """题型缺失必须由 Router 显式失败，且不会被改写成评分器错误。"""

    with pytest.raises(GradingRoutingError) as excinfo:
        _grade(
            grader,
            question_type=None,
            reference_answer="B",
            student_answer="B",
        )

    assert excinfo.value.error_code == QUESTION_TYPE_MISSING
    assert not isinstance(excinfo.value, ObjectiveGradingError)


@pytest.mark.parametrize(
    "student_answer",
    [{"A": "True"}, {"1": "answer"}, {"A": "True", "B": "False"}],
)
def test_objective_answers_in_mapping_form_are_rejected(
    grader: ObjectiveGrader,
    student_answer: dict[str, str],
) -> None:
    """字典形态答案缺少数值语义约定，本批显式不支持并报错。"""

    with pytest.raises(InvalidAnswerFormatError) as excinfo:
        _grade(
            grader,
            question_type=QuestionType.MULTIPLE_CHOICE,
            reference_answer="A",
            student_answer=student_answer,
            max_score=2.0,
            option_keys=["A", "B"],
        )

    assert excinfo.value.error_code == GRADING_INVALID_ANSWER_FORMAT


@pytest.mark.parametrize(
    ("question_type", "student_answer"),
    [
        (QuestionType.SINGLE_CHOICE, ["A", "B"]),
        (QuestionType.TRUE_FALSE, ["True", "False"]),
        (QuestionType.FILL_BLANK, ["第一个空", "第二个空"]),
        (QuestionType.SINGLE_CHOICE, ["A", 1]),
        (QuestionType.FILL_BLANK, ["答案", None]),
    ],
)
def test_unsupported_answer_shapes_are_rejected(
    grader: ObjectiveGrader,
    question_type: QuestionType,
    student_answer: list[Any],
) -> None:
    """单选不允许多值，填空本批只支持整体文本（或多空位置的单一整体答案）。"""

    with pytest.raises(InvalidAnswerFormatError):
        _grade(
            grader,
            question_type=question_type,
            reference_answer="A",
            student_answer=student_answer,
            max_score=2.0,
        )


@pytest.mark.parametrize(
    ("max_score", "student_answer", "expected_score"),
    [(0.05, "A,B", 0.03), (0.01, "A,B", 0.01), (1.0, "A", 0.33)],
)
def test_score_is_produced_within_range_without_clamping(
    grader: ObjectiveGrader,
    max_score: float,
    student_answer: str,
    expected_score: float,
) -> None:
    """分数由规则直接产生：舍入后不会超过满分，也不需要静默钳制。"""

    result = _grade(
        grader,
        question_type=QuestionType.MULTIPLE_CHOICE,
        reference_answer="A,B,C",
        student_answer=student_answer,
        max_score=max_score,
    )

    assert result.score == expected_score
    assert 0.0 <= result.score <= result.max_score


def test_internal_invariant_error_is_defined_for_unexpected_scores() -> None:
    """不变量被破坏时必须显式失败，而不是把分数静默压回合法范围。"""

    error = GradingInvariantError("比例分超出满分。")

    assert error.error_code == GRADING_INTERNAL_INVARIANT_VIOLATION
    assert error.retryable is False
    assert issubclass(GradingInvariantError, ObjectiveGradingError)


def test_result_exposes_required_dto_fields(grader: ObjectiveGrader) -> None:
    """结果必须是完整 GradingResult DTO，状态与置信度符合客观题直接接受路径。"""

    result = _grade(
        grader,
        question_type=QuestionType.SINGLE_CHOICE,
        reference_answer="B",
        student_answer="B",
        max_score=5.0,
        answer_id="answer-1",
        submission_id="submission-1",
    )

    assert isinstance(result, GradingResult)
    assert EXPECTED_RESULT_FIELDS <= set(result.model_dump())
    assert result.confidence == 1.0
    assert result.validation_status == "Validated"
    assert result.review_status == "Not Required"
    assert result.retrieved_context_ids == []
    assert result.answer_id == "answer-1"
    assert result.submission_id == "submission-1"
    assert result.knowledge_points == ["变量作用域"]


def test_repeated_grading_is_stable_for_one_hundred_runs(
    grader: ObjectiveGrader,
) -> None:
    """SC-004：同一组客观题答案重复评分 100 次，结果必须完全一致。"""

    cases = (
        (QuestionType.SINGLE_CHOICE, "B", "B", 5.0),
        (QuestionType.SINGLE_CHOICE, "B", "C", 5.0),
        (QuestionType.MULTIPLE_CHOICE, "A,B,C", "A,B", 3.0),
        (QuestionType.MULTIPLE_CHOICE, "A,C", "A,B", 4.0),
        (QuestionType.TRUE_FALSE, "True", None, 2.0),
    )
    snapshots = set()
    for _ in range(100):
        for question_type, reference, student, max_score in cases:
            result = _grade(
                grader,
                question_type=question_type,
                reference_answer=reference,
                student_answer=student,
                max_score=max_score,
            )
            snapshots.add(
                (
                    str(result.question_type),
                    result.score,
                    result.max_score,
                    result.reason,
                    tuple(result.correct_points),
                    tuple(result.missing_knowledge_points),
                    result.confidence,
                )
            )

    assert len(snapshots) == len(cases)


def test_module_does_not_import_llm_embedding_or_retrieval() -> None:
    """铁律证据一：模块导入图不含 LLM、Embedding、Rerank 与数据库依赖。"""

    source_path = Path(objective_grader_module.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    forbidden_prefixes = (
        "backend.app.ai.llm",
        "backend.app.ai.embedding",
        "backend.app.ai.retrieval",
        "sqlalchemy",
        "openai",
    )
    assert not [
        name for name in imported if name.startswith(forbidden_prefixes)
    ]


def test_grading_does_not_open_network_connections(
    grader: ObjectiveGrader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """铁律证据二：评分全过程不得发起任何网络连接（LLM/Embedding/Rerank 通道）。"""

    def _deny(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("客观题评分不得发起网络调用。")

    monkeypatch.setattr(socket.socket, "connect", _deny)
    monkeypatch.setattr(socket.socket, "connect_ex", _deny)
    monkeypatch.setattr(socket, "create_connection", _deny)

    for question_type, reference, student in (
        (QuestionType.SINGLE_CHOICE, "B", "B"),
        (QuestionType.MULTIPLE_CHOICE, "A,C", "A"),
        (QuestionType.FILL_BLANK, "Python 标准库", "python 标准库"),
    ):
        result = _grade(
            grader,
            question_type=question_type,
            reference_answer=reference,
            student_answer=student,
            max_score=5.0,
        )
        assert 0.0 <= result.score <= result.max_score


# --- B01 回归：list[str] 每项就是一个完整选项键，不再二次切分 ---


def test_b01_list_items_are_not_split_again() -> None:
    """契约：``["A,B"]`` 是单个键，不得被二次切分为 ``{"A", "B"}``。"""

    keys = parse_multiple_choice_keys(
        ["A,B"], option_keys=["A,B", "C"], label="学生答案"
    )

    assert keys == frozenset({"a,b"})


def test_b01_list_items_keep_inner_whitespace() -> None:
    """选项 ID 可能含空格（如 ``"A 选项"``），列表项原样保留、不做 trim。"""

    keys = parse_multiple_choice_keys(
        ["A 选项"], option_keys=["A 选项", "B 选项"], label="学生答案"
    )

    assert keys == frozenset({"a 选项"})


def test_b01_two_list_items_are_two_keys() -> None:
    """``["A", "B"]`` 逐项作为键，正常识别为两个键。"""

    keys = parse_multiple_choice_keys(
        ["A", "B"], option_keys=["A", "B", "C"], label="学生答案"
    )

    assert keys == frozenset({"a", "b"})


def test_b01_compound_key_is_not_silently_split(grader: ObjectiveGrader) -> None:
    """选项键集合不含 ``"A,B"`` 时，``["A,B"]`` 不得被静默理解为 ``{A, B}``。"""

    with pytest.raises(InvalidAnswerFormatError) as excinfo:
        _grade(
            grader,
            question_type=QuestionType.MULTIPLE_CHOICE,
            reference_answer="A,B",
            student_answer=["A,B"],
            max_score=5.0,
            option_keys=["A", "B"],
        )

    assert excinfo.value.error_code == GRADING_INVALID_ANSWER_FORMAT


def test_b01_list_form_still_matches_reference_text(grader: ObjectiveGrader) -> None:
    """``["A", "B"]`` 与标准答案字符串 ``"A,B"`` 等价，仍给满分。"""

    result = _grade(
        grader,
        question_type=QuestionType.MULTIPLE_CHOICE,
        reference_answer="A,B",
        student_answer=["A", "B"],
        max_score=5.0,
        option_keys=["A", "B", "C"],
    )

    assert result.score == 5.0


# --- B02 回归：非法 Decimal / 超大整数满分统一为业务异常 ---


@pytest.mark.parametrize(
    "max_score",
    [
        Decimal("sNaN"),
        Decimal("NaN"),
        Decimal("1E+400"),
        Decimal("-1E+400"),
        10**400,
        -(10**400),
    ],
)
def test_b02_invalid_decimal_max_score_is_a_business_error(
    grader: ObjectiveGrader,
    max_score: Any,
) -> None:
    """非法 Decimal/超大整数满分必须统一为业务异常，不泄漏底层异常类型。"""

    with pytest.raises(InvalidMaxScoreError) as excinfo:
        _grade(
            grader,
            question_type=QuestionType.SINGLE_CHOICE,
            reference_answer="B",
            student_answer="B",
            max_score=max_score,
        )

    assert excinfo.value.error_code == GRADING_INVALID_MAX_SCORE
    assert not isinstance(excinfo.value, (ValueError, OverflowError))
    # 不把底层异常链带给调用方，便于统一按业务错误码处理。
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


def test_grader_accepts_question_like_objects_end_to_end(
    grader: ObjectiveGrader,
) -> None:
    """题目与答案替身可直接取字段评分，不依赖数据库会话。"""

    question = SimpleNamespace(
        type=QuestionType.MULTIPLE_CHOICE,
        reference_answer="A,B",
        score=2.0,
        knowledge_points=["列表推导式"],
    )
    answer = SimpleNamespace(content=["B", "A"], id="answer-9")

    result = _grade(
        grader,
        question_type=question.type,
        reference_answer=question.reference_answer,
        student_answer=answer.content,
        max_score=float(question.score),
        knowledge_points=tuple(question.knowledge_points),
        answer_id=answer.id,
    )

    assert result.score == 2.0
    assert result.knowledge_points == ["列表推导式"]
    assert result.answer_id == "answer-9"
