"""Objective Grader：不调用 LLM 的客观题确定性规则评分。

契约依据：FR-030、SC-004、spec.md 第 57、87、89 行，plan.md 第 514 行，
``.specify/data-model.md`` 的「Objective 题的评分结果必须可由学生答案、标准答案和题目
分值重算」，以及 ``.specify/contracts/agent-workflow.md`` 的 ``Rule Grade`` 节点。

铁律：本模块只做规则计算，**不调用 LLM、不调用 Embedding、不调用 Rerank、不访问数据库
或网络**。客观题分数必须可由「学生答案 + 标准答案 + 题目分值」重算，且同一输入重复评分
结果完全一致（SC-004）。

评分合同（公开、可核对）：

输入契约
    1. ``question_type``：题库题型标识；必须落在 Objective 四类（SINGLE_CHOICE、
       MULTIPLE_CHOICE、TRUE_FALSE、FILL_BLANK），否则显式失败。
    2. ``reference_answer``：``Question.reference_answer`` 为文本列，因此标准答案必须是
       非空字符串；缺失或空白时显式失败，不默认 0 分掩盖题目数据问题。
    3. ``student_answer``：对应 ``Answer.content``，本批支持 ``str``、``list[str]`` 与
       ``None``。**本批显式不支持 dict[str, str]**（缺少键语义约定），遇到时显式报错；
       本批不修改 ``Answer.content`` 模型、``submission_service`` 与 UI 写入路径。
    4. ``max_score``：必须为有限正数（``bool`` 不接受）；非有限数（NaN、±inf）、超大值
       （整数或 ``Decimal`` 超出浮点范围）、``Decimal`` signaling NaN 或非数值都统一转为
       ``GRADING_INVALID_MAX_SCORE``，不泄漏底层 ``ValueError`` / ``OverflowError``。
    5. ``option_keys``（可选）：由调用方显式提供的选项键集合（例如 ``Question.options``
       为字典时取其键）。本批不定义「列表选项 → 字母键」的派生规则，派生属于调用方责任；
       未提供时评分器只做集合比较，不校验键是否合法。
    6. ``knowledge_points``、``answer_id``、``submission_id`` 透传到 ``GradingResult``。

归一化范围
    - 文本（单选、判断、填空）：``NFKC`` 兼容归一（全角转半角）、首尾去空白、内部连续空白
      折叠为单个空格，比较时 ``casefold``（大小写不敏感）。
    - 选项键（多选）：``NFKC`` 后删除全部空白再 ``casefold``。
    - 明确不做：同义词或别名映射（如 对/True）、数字格式等价（"3.0" 与 "3" 视为不同答案）、
      标点等价替换、拼写纠错。

题型规则
    - ``SINGLE_CHOICE`` / ``TRUE_FALSE``：标量整体文本等值；完全一致记满分，否则 0 分。
    - ``FILL_BLANK``：标准答案为单一字符串，本批定义为整体文本等值；学生答案可为 ``str``
      或长度 1 的 ``list[str]``。本批不定义多空位置编码，长度大于 1 的列表显式报错。
    - ``MULTIPLE_CHOICE``：选项键集合比较，规则见下。

多选编码（不自行拆分）
    - ``str``（键序列）：仅按显式分隔符 ``, ， 、 ; ； / |`` 与空白切分；**不做逐字符
      拆分**。切分后只剩一项且归一化长度大于 1 时，仅当该串本身就是已知选项键（出现在
      ``option_keys`` 中）才视为单键，否则显式报错。
    - ``list[str]``：**每一项就是一个完整选项键，不再二次切分**；项内空白原样保留（不做
      trim），仅做 ``NFKC`` 与大小写归一，以支持 ``"A 选项"`` 这类含空格的选项 ID；
      空白项不构成选项键，直接忽略。
    - 已知局限：``str`` 形式以空白作为分隔符，因此含空格的选项 ID 无法用字符串形式的
      标准答案表达；本批不新增该编码约定，留待后续任务定义参考键的显式表达方式。
    - 提供 ``option_keys`` 时，标准答案键与学生答案键都必须落在该集合内；越界键显式报错，
      以此区分「编码非法」与「合法但选错的错选」。``option_keys`` 的每项按 ``NFKC`` →
      去掉首尾空白 → ``casefold`` 归一（保留项内空白），与 ``list[str]`` 答案项键形态一致；
      ``str`` 形式因以空白为分隔符，仍会删除全部空白。
    - 同一题内重复出现的选项键按集合去重（重复既不额外加分也不扣分）；本模块不处理跨题
      重复，跨题重复仍由 ``submission_service`` 的既有规则拒绝。

多选给分与舍入
    - 集合相等：直接取满分（不经过比例计算）。
    - 真子集（漏选且无错选）：``Decimal(str(max_score)) × 命中数 ÷ 标准答案键数``，按
      ``ROUND_HALF_UP`` 保留 2 位小数（对齐 ``Numeric(8, 2)``）。
    - 存在错选（学生键不在标准答案集合内）：0 分且不倒扣。
    - 空集合（未作答）：0 分。
    - 分数由规则直接产生并落在 ``[0, max_score]``；若该区间被破坏，抛出
      ``GRADING_INTERNAL_INVARIANT_VIOLATION`` 显式失败，**不做静默钳制**（DTO 自身也会
      拒绝越界分数）。

输出
    只返回经过 Pydantic 校验的 :class:`backend.app.schemas.ai.GradingResult`：``score``、
    ``max_score``、中文 ``reason``、``correct_points``、``missing_knowledge_points``、
    ``knowledge_points``、``suggestions``、``confidence=1.0``
    （客观题走确定性规则，按 data-model.md 的 GradingResult 状态图直接进入接受路径）、
    ``validation_status="Validated"``、``review_status="Not Required"``、
    ``retrieved_context_ids=[]``，并透传 ``answer_id`` 与 ``submission_id``。
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, ClassVar, Final

from backend.app.domain.enums import (
    GradingMode,
    QuestionType,
    ReviewStatus,
    ValidationStatus,
)
from backend.app.schemas.ai import GradingResult
from backend.app.services.grading.question_router import (
    QuestionRouter,
    normalize_question_type,
)

#: 主观题被送进客观题评分器；必须显式失败，不得改写题型。
GRADING_MODE_MISMATCH: Final[str] = "GRADING_MODE_MISMATCH"
#: 标准答案缺失；必须显式失败，不得默认 0 分。
GRADING_MISSING_REFERENCE_ANSWER: Final[str] = "GRADING_MISSING_REFERENCE_ANSWER"
#: 满分缺失或非法（非有限、非正数、非数值）。
GRADING_INVALID_MAX_SCORE: Final[str] = "GRADING_INVALID_MAX_SCORE"
#: 答案编码不受支持（dict 形态、非字符串项、单选多值、多选键越界或无法解析）。
GRADING_INVALID_ANSWER_FORMAT: Final[str] = "GRADING_INVALID_ANSWER_FORMAT"
#: 内部不变量被破坏（例如比例分越界）；此时必须显式失败而不是钳制分数。
GRADING_INTERNAL_INVARIANT_VIOLATION: Final[str] = (
    "GRADING_INTERNAL_INVARIANT_VIOLATION"
)

#: 评分失败码对应的可读提示。
GRADING_ERROR_MESSAGES: Final[Mapping[str, str]] = MappingProxyType(
    {
        GRADING_MODE_MISMATCH: "该题不属于客观题评分路径，已停止规则评分。",
        GRADING_MISSING_REFERENCE_ANSWER: "题目缺少标准答案，无法进行确定性评分。",
        GRADING_INVALID_MAX_SCORE: "题目满分必须为有限正数，无法进行确定性评分。",
        GRADING_INVALID_ANSWER_FORMAT: "答案编码不受支持，已停止评分，不做猜测。",
        GRADING_INTERNAL_INVARIANT_VIOLATION: "评分不变量被破坏，已停止评分。",
    }
)

#: 分数的最小量化单位，对齐题目分值的 ``Numeric(8, 2)``。
SCORE_QUANTUM: Final[Decimal] = Decimal("0.01")

#: 使用标量整体文本等值规则的题型。
SCALAR_OBJECTIVE_TYPES: Final[frozenset[QuestionType]] = frozenset(
    {
        QuestionType.SINGLE_CHOICE,
        QuestionType.TRUE_FALSE,
        QuestionType.FILL_BLANK,
    }
)

#: 多选答案的分隔符：仅显式分隔符参与切分，不做逐字符拆分。
CHOICE_KEY_SEPARATORS: Final[re.Pattern[str]] = re.compile(r"[,，、;；/|\s]+")


class ObjectiveGradingError(RuntimeError):
    """客观题评分失败基类；默认按不可重试的输入或数据问题处理。"""

    error_code: ClassVar[str] = GRADING_INVALID_ANSWER_FORMAT
    retryable: ClassVar[bool] = False

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.error_code}：{detail}")
        self.detail = detail


class GradingModeMismatchError(ObjectiveGradingError):
    """题目属于 Subjective 评分路径，不能被客观题规则评分器处理。"""

    error_code: ClassVar[str] = GRADING_MODE_MISMATCH


class MissingReferenceAnswerError(ObjectiveGradingError):
    """题目缺少可用的标准答案；停止评分而不是猜测分数。"""

    error_code: ClassVar[str] = GRADING_MISSING_REFERENCE_ANSWER


class InvalidMaxScoreError(ObjectiveGradingError):
    """题目满分非法；无法计算合法分数。"""

    error_code: ClassVar[str] = GRADING_INVALID_MAX_SCORE


class InvalidAnswerFormatError(ObjectiveGradingError):
    """答案编码不在本批评分合同支持范围内。"""

    error_code: ClassVar[str] = GRADING_INVALID_ANSWER_FORMAT


class GradingInvariantError(ObjectiveGradingError):
    """评分内部不变量被破坏；拒绝静默钳制，交由上游显式处理。"""

    error_code: ClassVar[str] = GRADING_INTERNAL_INVARIANT_VIOLATION


def is_blank_answer(value: Any) -> bool:
    """判断答案是否缺失。

    语义与 :func:`backend.app.services.submission_service._is_blank_answer` 保持一致：
    ``None``、空白字符串、空列表或仅含空白项的列表、空字典或没有非空键值对的字典都视为
    缺失。字典形态对客观题评分属于不支持的编码，本函数只用于缺失判定与一致性回归。
    """

    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, Mapping):
        return not value or not any(
            str(key).strip() and str(item).strip() for key, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return not value or not any(
            isinstance(item, str) and item.strip() for item in value
        )
    return False


def normalize_text(value: str) -> str:
    """文本归一：``NFKC`` → 首尾去空白 → 内部连续空白折叠 → ``casefold``。"""

    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.split()).casefold()


def normalize_choice_key(value: str) -> str:
    """字符串形式选项键归一：``NFKC`` → 删除全部空白 → ``casefold``。

    仅用于 ``str`` 形式答案的切分结果，因为该形式以空白作为分隔符。
    """

    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()


def normalize_option_key(value: str) -> str:
    """选项键标识归一：``NFKC`` → 去掉首尾空白 → ``casefold``，保留项内空白。

    与 ``list[str]`` 答案项的键形态保持一致，使 ``"A 选项"`` 这类含空格的选项 ID 可用。
    """

    return unicodedata.normalize("NFKC", value).strip().casefold()


def _display_key(key: str) -> str:
    """选项键面向学生与教师的展示形式。"""

    return key.upper()


def _resolve_max_score(value: Any) -> float:
    """校验并转换题目满分；只接受有限正数。

    非数值、非有限数（NaN、±inf、``Decimal`` 的 signaling NaN）、超出浮点可表示范围的
    超大值（整数或 ``Decimal``）以及非正数都统一转为 :class:`InvalidMaxScoreError`：
    不向调用方泄漏 ``ValueError`` / ``OverflowError`` / ``InvalidOperation``，
    也不把原始异常链带入业务错误。
    """

    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise InvalidMaxScoreError(
            f"题目满分必须是有限正数，收到类型 {type(value).__name__}。"
        )
    number: float | None = None
    failed = False
    try:
        number = float(value)
    except (ValueError, OverflowError, InvalidOperation):
        failed = True
    if failed or number is None:
        raise InvalidMaxScoreError(
            f"题目满分必须是有限正数，收到 {value!r}；该数值无法转换为可比较的有限浮点数。"
        )
    if not math.isfinite(number) or number <= 0:
        raise InvalidMaxScoreError(f"题目满分必须是有限正数，收到 {value!r}。")
    return number


def _require_reference_answer(value: Any) -> str:
    """校验标准答案存在且为文本；缺失时显式失败。"""

    if value is None:
        raise MissingReferenceAnswerError("题目未提供标准答案。")
    if not isinstance(value, str):
        raise InvalidAnswerFormatError(
            f"标准答案必须是字符串，收到类型 {type(value).__name__}。"
        )
    if not value.strip():
        raise MissingReferenceAnswerError("标准答案为空或仅含空白字符。")
    return value


def _ensure_supported_answer(value: Any) -> None:
    """校验学生答案的编码形态属于本批合同。"""

    if value is None or isinstance(value, str):
        return
    if isinstance(value, Mapping):
        raise InvalidAnswerFormatError(
            "本批不支持字典形态的客观题答案，请按字符串或字符串列表提交。"
        )
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for item in value:
            if not isinstance(item, str):
                raise InvalidAnswerFormatError(
                    "答案列表必须只包含字符串，"
                    f"收到类型 {type(item).__name__}。"
                )
        return
    raise InvalidAnswerFormatError(
        f"不支持的答案类型：{type(value).__name__}。"
    )


def _scalar_answer_text(value: Any, *, label: str) -> str:
    """把标量答案（``str`` 或长度 1 的 ``list[str]``）归一为整体文本。"""

    if isinstance(value, str):
        return normalize_text(value)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        if len(value) != 1:
            raise InvalidAnswerFormatError(
                f"{label}只允许单个取值，收到 {len(value)} 项。"
            )
        return normalize_text(value[0])
    raise InvalidAnswerFormatError(f"不支持的{label}类型：{type(value).__name__}。")


def _known_option_keys(option_keys: Any) -> frozenset[str] | None:
    """归一调用方传入的选项键集合；未提供时返回 ``None``。"""

    if option_keys is None:
        return None
    if isinstance(option_keys, str) or not isinstance(option_keys, Sequence):
        raise InvalidAnswerFormatError("选项键集合必须是字符串序列。")
    keys: list[str] = []
    for item in option_keys:
        if not isinstance(item, str):
            raise InvalidAnswerFormatError(
                f"选项键必须是字符串，收到类型 {type(item).__name__}。"
            )
        key = normalize_option_key(item)
        if not key:
            raise InvalidAnswerFormatError("选项键不能为空或仅含空白字符。")
        keys.append(key)
    if not keys:
        raise InvalidAnswerFormatError("选项键集合不能为空，否则无法校验多选答案。")
    return frozenset(keys)


def _split_choice_items(
    text: str,
    *,
    known_keys: frozenset[str] | None,
    label: str,
) -> list[str]:
    """按显式分隔符切分选项键；无分隔符长串仅在等于已知键时成立。"""

    normalized = unicodedata.normalize("NFKC", text).strip()
    if not normalized:
        return []
    parts = [part for part in CHOICE_KEY_SEPARATORS.split(normalized) if part]
    keys = [normalize_choice_key(part) for part in parts]
    keys = [key for key in keys if key]
    if len(keys) == 1 and len(keys[0]) > 1 and (
        known_keys is None or keys[0] not in known_keys
    ):
        raise InvalidAnswerFormatError(
            f"{label}「{text}」缺少分隔符且不是已知选项键，评分器不自行拆分。"
        )
    return keys


def _verbatim_choice_key(item: str) -> str | None:
    """把列表项作为完整选项键：仅 ``NFKC`` 与大小写归一，不切分、不 trim。

    项内空白原样保留，以支持 ``"A 选项"`` 这类含空格的选项 ID；空白项不构成选项键，
    返回 ``None`` 交由调用方忽略。
    """

    if not item.strip():
        return None
    return normalize_option_key(item)


def parse_multiple_choice_keys(
    value: Any,
    *,
    option_keys: Sequence[str] | None = None,
    label: str = "答案",
) -> frozenset[str]:
    """把多选答案解析为选项键集合。

    ``value`` 为 ``str`` 时按显式分隔符切分；为 ``list[str]`` 时每一项即一个完整键，
    不再二次切分、也不做 trim。重复键按集合去重；提供 ``option_keys`` 时，任何越界键都会
    显式报错，以区分编码非法与合法但选错的答案。
    """

    known_keys = _known_option_keys(option_keys)
    if isinstance(value, str):
        keys = _split_choice_items(value, known_keys=known_keys, label=label)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        keys = []
        for item in value:
            if not isinstance(item, str):
                raise InvalidAnswerFormatError(
                    f"{label}列表必须只包含字符串，"
                    f"收到类型 {type(item).__name__}。"
                )
            # 契约：list[str] 的每一项就是一个完整选项键，不做二次切分、也不做 trim，
            # 避免把 "A,B" 或 "A 选项" 误解为多个键。
            key = _verbatim_choice_key(item)
            if key is not None:
                keys.append(key)
    else:
        raise InvalidAnswerFormatError(
            f"不支持的{label}类型：{type(value).__name__}。"
        )
    resolved = frozenset(keys)
    if known_keys is not None:
        outside = sorted(_display_key(key) for key in resolved - known_keys)
        if outside:
            raise InvalidAnswerFormatError(
                f"{label}包含不在选项键集合内的键：{'、'.join(outside)}。"
            )
    return resolved


def _proportional_score(max_score: float, hits: int, total: int) -> float:
    """按命中比例计算分数：``ROUND_HALF_UP`` 两位小数，越界即显式失败。"""

    if total <= 0:
        raise GradingInvariantError("标准答案选项数为 0，无法计算比例分。")
    exact = Decimal(str(max_score)) * Decimal(hits) / Decimal(total)
    try:
        quantized = exact.quantize(SCORE_QUANTUM, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:  # pragma: no cover - 防御性分支
        raise GradingInvariantError(f"比例分量化失败：{exc}。") from exc
    if quantized < 0 or quantized > Decimal(str(max_score)):
        raise GradingInvariantError(
            f"比例分 {quantized} 越出 [0, {max_score}] 区间，拒绝钳制。"
        )
    return float(quantized)


def _format_score(value: float) -> str:
    """分数展示：去掉无意义的小数零。"""

    return f"{value:g}"


class ObjectiveGrader:
    """不调用 LLM 的客观题确定性规则评分器。

    :param router: 题型分流器；默认使用 :class:`QuestionRouter`，题型归属以其为唯一事实来源。
    """

    def __init__(self, router: QuestionRouter | None = None) -> None:
        self._router = router if router is not None else QuestionRouter()

    def grade(
        self,
        *,
        question_type: QuestionType | str | None,
        reference_answer: str | None,
        student_answer: str | Sequence[str] | None,
        max_score: float,
        option_keys: Sequence[str] | None = None,
        knowledge_points: Sequence[str] = (),
        answer_id: str | None = None,
        submission_id: str | None = None,
    ) -> GradingResult:
        """按客观题规则给出确定性的 :class:`GradingResult`。"""

        normalized_type = normalize_question_type(question_type)
        mode = self._router.route_type(normalized_type)
        if mode is not GradingMode.OBJECTIVE:
            raise GradingModeMismatchError(
                f"题型 {normalized_type} 属于 Subjective 路径，"
                "不得使用客观题规则评分器。"
            )

        resolved_max_score = _resolve_max_score(max_score)
        reference_text = _require_reference_answer(reference_answer)
        _ensure_supported_answer(student_answer)
        knowledge = tuple(knowledge_points or ())

        if normalized_type is QuestionType.MULTIPLE_CHOICE:
            return self._grade_multiple_choice(
                student_answer=student_answer,
                reference_answer=reference_text,
                max_score=resolved_max_score,
                option_keys=option_keys,
                knowledge_points=knowledge,
                answer_id=answer_id,
                submission_id=submission_id,
            )
        if normalized_type in SCALAR_OBJECTIVE_TYPES:
            return self._grade_scalar(
                question_type=normalized_type,
                student_answer=student_answer,
                reference_answer=reference_text,
                max_score=resolved_max_score,
                knowledge_points=knowledge,
                answer_id=answer_id,
                submission_id=submission_id,
            )
        raise GradingModeMismatchError(
            f"题型 {normalized_type} 不在客观题规则评分范围内。"
        )

    def _grade_scalar(
        self,
        *,
        question_type: QuestionType,
        student_answer: Any,
        reference_answer: str,
        max_score: float,
        knowledge_points: tuple[str, ...],
        answer_id: str | None,
        submission_id: str | None,
    ) -> GradingResult:
        """单选、判断与填空：整体文本等值。"""

        expected = normalize_text(reference_answer)
        if is_blank_answer(student_answer):
            return self._build_result(
                question_type=question_type,
                score=0.0,
                max_score=max_score,
                reason="学生答案缺失，按客观题规则记 0 分。",
                correct_points=[],
                missing_points=knowledge_points,
                knowledge_points=knowledge_points,
                suggestions=["本题未作答，请先补全答案再提交，避免无谓失分。"],
                answer_id=answer_id,
                submission_id=submission_id,
            )

        given = _scalar_answer_text(student_answer, label="学生答案")
        if given == expected:
            return self._build_result(
                question_type=question_type,
                score=max_score,
                max_score=max_score,
                reason=(
                    f"学生答案与标准答案一致，按题目分值给满分 "
                    f"{_format_score(max_score)} 分。"
                ),
                correct_points=list(knowledge_points),
                missing_points=[],
                knowledge_points=knowledge_points,
                suggestions=["保持当前的准确作答，可继续挑战更高难度题目。"],
                answer_id=answer_id,
                submission_id=submission_id,
            )
        return self._build_result(
            question_type=question_type,
            score=0.0,
            max_score=max_score,
            reason="学生答案与标准答案不一致，按客观题规则记 0 分。",
            correct_points=[],
            missing_points=knowledge_points,
            knowledge_points=knowledge_points,
            suggestions=["对照标准答案复核本题，确认是知识点遗漏还是审题问题。"],
            answer_id=answer_id,
            submission_id=submission_id,
        )

    def _grade_multiple_choice(
        self,
        *,
        student_answer: Any,
        reference_answer: str,
        max_score: float,
        option_keys: Sequence[str] | None,
        knowledge_points: tuple[str, ...],
        answer_id: str | None,
        submission_id: str | None,
    ) -> GradingResult:
        """多选：选项键集合比较，漏选按比例给分，错选不得分。"""

        reference_keys = parse_multiple_choice_keys(
            reference_answer,
            option_keys=option_keys,
            label="标准答案",
        )
        if not reference_keys:
            raise MissingReferenceAnswerError("多选标准答案解析为空，无法评分。")

        if is_blank_answer(student_answer):
            return self._build_result(
                question_type=QuestionType.MULTIPLE_CHOICE,
                score=0.0,
                max_score=max_score,
                reason="学生答案缺失，按客观题规则记 0 分。",
                correct_points=[],
                missing_points=[
                    _display_key(key) for key in sorted(reference_keys)
                ],
                knowledge_points=knowledge_points,
                suggestions=["本题未作答，请先补全答案再提交，避免无谓失分。"],
                answer_id=answer_id,
                submission_id=submission_id,
            )

        student_keys = parse_multiple_choice_keys(
            student_answer,
            option_keys=option_keys,
            label="学生答案",
        )
        hits = student_keys & reference_keys
        missing = reference_keys - student_keys
        extra = student_keys - reference_keys

        if student_keys == reference_keys:
            return self._build_result(
                question_type=QuestionType.MULTIPLE_CHOICE,
                score=max_score,
                max_score=max_score,
                reason=(
                    f"多选答案与标准答案完全一致，按题目分值给满分 "
                    f"{_format_score(max_score)} 分。"
                ),
                correct_points=list(knowledge_points),
                missing_points=[],
                knowledge_points=knowledge_points,
                suggestions=["保持当前的准确作答，可继续挑战更高难度题目。"],
                answer_id=answer_id,
                submission_id=submission_id,
            )

        if extra:
            extra_display = "、".join(sorted(_display_key(key) for key in extra))
            return self._build_result(
                question_type=QuestionType.MULTIPLE_CHOICE,
                score=0.0,
                max_score=max_score,
                reason=(
                    f"多选题存在错选（{extra_display}），按客观题规则记 0 分，不倒扣。"
                ),
                correct_points=[],
                missing_points=[
                    _display_key(key) for key in sorted(missing)
                ],
                knowledge_points=knowledge_points,
                suggestions=[
                    "多选题存在错选，错选不计分，请重新核对每个选项与知识点的对应关系。"
                ],
                answer_id=answer_id,
                submission_id=submission_id,
            )

        score = _proportional_score(max_score, len(hits), len(reference_keys))
        missing_display = "、".join(sorted(_display_key(key) for key in missing))
        return self._build_result(
            question_type=QuestionType.MULTIPLE_CHOICE,
            score=score,
            max_score=max_score,
            reason=(
                f"多选题漏选 {missing_display}，按命中 {len(hits)}/{len(reference_keys)} "
                f"比例给分 {_format_score(score)} 分（满分 "
                f"{_format_score(max_score)} 分）。"
            ),
            correct_points=[_display_key(key) for key in sorted(hits)],
            missing_points=[_display_key(key) for key in sorted(missing)],
            knowledge_points=knowledge_points,
            suggestions=[
                f"多选题漏选会按比例扣分，请补齐未选中的选项：{missing_display}。"
            ],
            answer_id=answer_id,
            submission_id=submission_id,
        )

    def _build_result(
        self,
        *,
        question_type: QuestionType,
        score: float,
        max_score: float,
        reason: str,
        correct_points: Sequence[str],
        missing_points: Sequence[str],
        knowledge_points: tuple[str, ...],
        suggestions: list[str],
        answer_id: str | None,
        submission_id: str | None,
    ) -> GradingResult:
        """构造经过校验的 DTO；分数越界时显式失败而不是钳制。"""

        if not 0.0 <= score <= max_score:
            raise GradingInvariantError(
                f"得分 {score} 越出 [0, {max_score}] 区间，拒绝钳制。"
            )
        return GradingResult(
            question_type=question_type,
            score=score,
            max_score=max_score,
            reason=reason,
            correct_points=list(correct_points),
            missing_knowledge_points=list(missing_points),
            knowledge_points=list(knowledge_points),
            suggestions=suggestions,
            confidence=1.0,
            validation_status=ValidationStatus.VALIDATED.value,
            review_status=ReviewStatus.NOT_REQUIRED.value,
            retrieved_context_ids=[],
            answer_id=answer_id,
            submission_id=submission_id,
        )


__all__ = [
    "CHOICE_KEY_SEPARATORS",
    "GRADING_ERROR_MESSAGES",
    "GRADING_INTERNAL_INVARIANT_VIOLATION",
    "GRADING_INVALID_ANSWER_FORMAT",
    "GRADING_INVALID_MAX_SCORE",
    "GRADING_MISSING_REFERENCE_ANSWER",
    "GRADING_MODE_MISMATCH",
    "SCALAR_OBJECTIVE_TYPES",
    "SCORE_QUANTUM",
    "GradingInvariantError",
    "GradingModeMismatchError",
    "InvalidAnswerFormatError",
    "InvalidMaxScoreError",
    "MissingReferenceAnswerError",
    "ObjectiveGrader",
    "ObjectiveGradingError",
    "is_blank_answer",
    "normalize_choice_key",
    "normalize_option_key",
    "normalize_text",
    "parse_multiple_choice_keys",
]
