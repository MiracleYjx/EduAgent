"""Question Validator：候选题目校验与审核状态转换（T068）。

契约依据：FR-024~FR-028、`.specify/plan.md` §4.1 AI 出题完整链路（步骤 6 Question Validator）
与 §4.2 题库审核状态规则、`.specify/data-model.md` 的候选题状态图，以及 B03 记录的作答编码事实
（``backend/app/ui/student_exam_view.py`` 列表选项提交选项全文、判断题默认提交 ``True``/``False``，
``backend/app/services/grading/objective_grader.py`` 对标量答案做整体文本等值比较）。

职责边界：

- **纯业务校验**：本模块不访问数据库、不调用 LLM、不修改 M1 的 ``Question`` 模型与
  ``QuestionStatus`` 枚举，也不复制评分算法；题型相关的答案编码复用 M3 既有的
  ``normalize_text`` 与 ``parse_multiple_choice_keys`` 口径。
- **只判候选**：原始 DTO 始终保留 ``Candidate Generation``；校验结果通过
  :attr:`CandidateValidationResult.status` **单独表达**审核状态，且只可能是
  ``Pending Review`` 或 ``Needs Revision``，**绝不允许** ``Approved``/``Published``。
- **禁止自动发布**：:func:`plan_transition` 先做权限判定，再做同状态幂等判定，因此 AGENT 与
  VALIDATOR 路径对 ``Approved``/``Published``（含 ``Approved→Approved``）一律抛出
  ``QUESTION_AUTOMATIC_PUBLISH_BLOCKED``；只有 TEACHER 路径可以批准，且不能跳过
  ``Needs Revision`` 直接批准。
- **失败原因保留**：每个问题都带原因码、脱敏中文说明与字段名，供教师退回修订使用。
- **落库可用性**：分值按 M1 ``question_service`` 的口径检查（有限正数、最多两位小数、
  不超过 999999.99），避免候选通过校验后在 T075 落库失败。
- **落库接线属 T075**：候选审核状态写入 ``QuestionService`` 与出题 API/UI 由 T075 负责；
  ``ValidationActor.TEACHER`` 只表示流程角色，不代表已完成身份与课程授权校验。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType
from typing import ClassVar, Final

from pydantic import BaseModel, ConfigDict, Field

from backend.app.domain.enums import QuestionStatus, QuestionType
from backend.app.schemas.ai import QuestionCandidate
from backend.app.services.grading.objective_grader import (
    InvalidAnswerFormatError,
    normalize_text,
    parse_multiple_choice_keys,
)

#: 参考答案与学生实际可提交的取值不一致（编码不兼容）。
QUESTION_ANSWER_ENCODING_INCOMPATIBLE: Final[str] = "QUESTION_ANSWER_ENCODING_INCOMPATIBLE"
#: 候选缺少课程依据（未引用任何片段）。
QUESTION_MISSING_COURSE_EVIDENCE: Final[str] = "QUESTION_MISSING_COURSE_EVIDENCE"
#: 候选引用了本次检索未写入生成上下文的片段。
QUESTION_UNKNOWN_COURSE_EVIDENCE: Final[str] = "QUESTION_UNKNOWN_COURSE_EVIDENCE"
#: 评分标准不可用（缺少可分配的分值或要点）。
QUESTION_RUBRIC_UNUSABLE: Final[str] = "QUESTION_RUBRIC_UNUSABLE"
#: 分值无法落库（非有限正数、超过两位小数或超过上限）。
QUESTION_SCORE_NOT_STORABLE: Final[str] = "QUESTION_SCORE_NOT_STORABLE"
#: 候选题难度与教师要求不一致（整批条件）。
QUESTION_DIFFICULTY_MISMATCH: Final[str] = "QUESTION_DIFFICULTY_MISMATCH"
#: 候选题未覆盖教师要求的全部知识点（整批条件）。
QUESTION_KNOWLEDGE_POINT_UNCOVERED: Final[str] = "QUESTION_KNOWLEDGE_POINT_UNCOVERED"
#: 待校验内容不是候选生成结果。
QUESTION_NOT_CANDIDATE: Final[str] = "QUESTION_NOT_CANDIDATE"
#: 状态转换不在允许的审核状态机内。
QUESTION_STATUS_TRANSITION_BLOCKED: Final[str] = "QUESTION_STATUS_TRANSITION_BLOCKED"
#: Agent 或校验器试图自动发布候选题。
QUESTION_AUTOMATIC_PUBLISH_BLOCKED: Final[str] = "QUESTION_AUTOMATIC_PUBLISH_BLOCKED"

#: 候选题必须处于的生成状态（复用 M1 ``QuestionStatus``）。
CANDIDATE_GENERATION_STATUS: Final[str] = QuestionStatus.CANDIDATE_GENERATION.value

#: 允许的审核状态转换（与 M1 人工建题状态表并存，覆盖候选生成路径）。
CANDIDATE_STATUS_TRANSITIONS: Final[Mapping[QuestionStatus, frozenset[QuestionStatus]]] = (
    MappingProxyType(
        {
            QuestionStatus.DRAFT: frozenset({QuestionStatus.PENDING_REVIEW}),
            QuestionStatus.CANDIDATE_GENERATION: frozenset(
                {QuestionStatus.PENDING_REVIEW, QuestionStatus.NEEDS_REVISION}
            ),
            QuestionStatus.PENDING_REVIEW: frozenset(
                {QuestionStatus.APPROVED, QuestionStatus.NEEDS_REVISION}
            ),
            # 修订内容必须重新校验后才能回到待审核。
            QuestionStatus.NEEDS_REVISION: frozenset({QuestionStatus.PENDING_REVIEW}),
            QuestionStatus.APPROVED: frozenset(),
            QuestionStatus.PUBLISHED: frozenset(),
        }
    )
)

#: 只有教师可以到达的状态；Agent 与校验器一律不得写入。
TEACHER_ONLY_STATUSES: Final[frozenset[QuestionStatus]] = frozenset(
    {QuestionStatus.APPROVED, QuestionStatus.PUBLISHED}
)

#: 校验结果允许表达的审核状态；永不包含可发布状态。
VALIDATOR_RESULT_STATUSES: Final[frozenset[QuestionStatus]] = frozenset(
    {QuestionStatus.PENDING_REVIEW, QuestionStatus.NEEDS_REVISION}
)

#: 分值量化单位与上限；与 M1 ``question_service`` 的落库约定一致。
SCORE_QUANTUM: Final[Decimal] = Decimal("0.01")
MAX_CANDIDATE_SCORE: Final[Decimal] = Decimal("999999.99")

#: 无需额外答案编码约束的题型（学生通过文本控件作答）。
_TEXT_ANSWER_TYPES: Final[frozenset[QuestionType]] = frozenset(
    {QuestionType.FILL_BLANK, QuestionType.SHORT_ANSWER, QuestionType.ESSAY}
)


class ValidationActor(StrEnum):
    """触发状态转换的流程角色；不代表已完成身份与课程授权校验（T075 负责）。"""

    AGENT = "agent"
    VALIDATOR = "validator"
    TEACHER = "teacher"


class QuestionValidatorError(RuntimeError):
    """校验与状态转换失败基类；默认按不可重试的业务错误处理。"""

    error_code: ClassVar[str] = QUESTION_STATUS_TRANSITION_BLOCKED

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.error_code}：{detail}")
        self.detail = detail


class StatusTransitionBlockedError(QuestionValidatorError):
    """状态转换不在允许的审核状态机内。"""

    error_code: ClassVar[str] = QUESTION_STATUS_TRANSITION_BLOCKED


class AutomaticPublishingBlockedError(QuestionValidatorError):
    """Agent 或校验器试图把候选题推进到可发布状态。"""

    error_code: ClassVar[str] = QUESTION_AUTOMATIC_PUBLISH_BLOCKED


class QuestionValidationIssue(BaseModel):
    """单条校验问题；保留可展示的原因码、脱敏说明与字段名。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    code: str = Field(min_length=1, description="原因码。")
    message: str = Field(min_length=1, description="脱敏中文说明。")
    field: str | None = Field(default=None, description="相关字段名；无对应字段时为 None。")


class CandidateValidationResult(BaseModel):
    """单个候选题的校验结果；``status`` 是审核状态，不是 DTO 上的状态字段。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: QuestionStatus = Field(description="建议的审核状态。")
    issues: tuple[QuestionValidationIssue, ...] = Field(
        default=(), description="失败原因；为空表示校验通过。"
    )

    @property
    def is_valid(self) -> bool:
        """返回是否通过校验（唯一通过状态是 ``Pending Review``）。"""

        return self.status is QuestionStatus.PENDING_REVIEW and not self.issues


class CandidateBatchValidation(BaseModel):
    """一次生成的整批校验结果：逐题结果 + 整批条件问题。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    results: tuple[CandidateValidationResult, ...] = Field(
        default=(), description="逐题校验结果。"
    )
    issues: tuple[QuestionValidationIssue, ...] = Field(
        default=(), description="整批条件问题（难度匹配、知识点覆盖）。"
    )

    @property
    def is_valid(self) -> bool:
        """返回整批是否可以进入 ``Pending Review``。"""

        return not self.issues and all(result.is_valid for result in self.results)

    @property
    def status(self) -> QuestionStatus:
        """返回整批建议的审核状态。"""

        return (
            QuestionStatus.PENDING_REVIEW if self.is_valid else QuestionStatus.NEEDS_REVISION
        )


def _normalize_status(value: QuestionStatus | str) -> QuestionStatus:
    """把审核状态名称或取值规范化为 :class:`QuestionStatus`；未知取值显式失败。"""

    if isinstance(value, QuestionStatus):
        return value
    if isinstance(value, str):
        candidate = value.strip()
        for supported in QuestionStatus:
            if candidate.lower() in {supported.value.lower(), supported.name.lower()}:
                return supported
    raise StatusTransitionBlockedError("审核状态无效，已拒绝该状态转换。")


def _normalize_actor(value: ValidationActor | str) -> ValidationActor:
    """把流程角色名称或取值规范化为 :class:`ValidationActor`；未知取值显式失败。"""

    if isinstance(value, ValidationActor):
        return value
    if isinstance(value, str):
        candidate = value.strip()
        for supported in ValidationActor:
            if candidate.lower() in {supported.value.lower(), supported.name.lower()}:
                return supported
    raise StatusTransitionBlockedError("流程角色无效，已拒绝该状态转换。")


def plan_transition(
    current: QuestionStatus | str,
    target: QuestionStatus | str,
    *,
    actor: ValidationActor | str = ValidationActor.VALIDATOR,
) -> QuestionStatus:
    """按审核状态机规划一次状态转换。

    判定顺序（权限先于幂等）：目标属于 ``Approved``/``Published`` 时，非 TEACHER 角色一律抛出
    :class:`AutomaticPublishingBlockedError`，包括 ``Approved→Approved`` 这类同状态调用；
    其余同状态调用视为幂等并直接返回；最后才检查状态机是否允许该转换。
    """

    resolved_current = _normalize_status(current)
    resolved_target = _normalize_status(target)
    resolved_actor = _normalize_actor(actor)

    if resolved_target in TEACHER_ONLY_STATUSES and resolved_actor is not ValidationActor.TEACHER:
        raise AutomaticPublishingBlockedError(
            "只有教师可以批准或发布题目，Agent 与校验器不得自动发布。"
        )
    if resolved_current is resolved_target:
        return resolved_target
    if resolved_target not in CANDIDATE_STATUS_TRANSITIONS[resolved_current]:
        raise StatusTransitionBlockedError(
            f"题目状态不能从“{resolved_current.value}”变更为“{resolved_target.value}”。"
        )
    return resolved_target


def _issue(code: str, message: str, field: str | None = None) -> QuestionValidationIssue:
    """构造一条校验问题。"""

    return QuestionValidationIssue(code=code, message=message, field=field)


def _submittable_values(candidate: QuestionCandidate) -> tuple[set[str], bool]:
    """返回学生实际可提交的归一化取值集合，以及取值域是否由选项定义。

    T010 的 ``QuestionCandidate.options`` 只允许列表形式，因此候选题的可提交取值就是选项全文；
    字典形式的选项键属于 M1 人工建题（其参考答案由教师直接控制），不在本模块输入范围内。
    无选项时取值域未定义，由调用方按题型补充（判断题使用 ``True``/``False``）。
    """

    options = candidate.options
    if (
        isinstance(options, Sequence)
        and not isinstance(options, (str, bytes))
        and len(options) > 0
    ):
        return {normalize_text(str(item)) for item in options}, True
    return set(), False


def _answer_encoding_issue(candidate: QuestionCandidate) -> QuestionValidationIssue | None:
    """检查参考答案是否对应学生实际可提交的取值（B03 编码兼容性）。"""

    question_type = candidate.question_type
    if question_type in {QuestionType.SINGLE_CHOICE, QuestionType.TRUE_FALSE}:
        values, defined = _submittable_values(candidate)
        if not defined and question_type is QuestionType.TRUE_FALSE:
            values = {normalize_text("True"), normalize_text("False")}
            defined = True
        if not defined:
            return _issue(
                QUESTION_ANSWER_ENCODING_INCOMPATIBLE,
                "选择题缺少选项，无法确定学生可提交的答案取值。",
                "options",
            )
        if normalize_text(candidate.reference_answer) not in values:
            return _issue(
                QUESTION_ANSWER_ENCODING_INCOMPATIBLE,
                "参考答案与学生实际可提交的取值不一致（列表选项需填写选项全文，"
                "无选项判断题使用 True/False）。",
                "reference_answer",
            )
        return None
    if question_type is QuestionType.MULTIPLE_CHOICE:
        options = candidate.options
        option_keys: tuple[str, ...] | None = None
        if isinstance(options, Sequence) and not isinstance(options, (str, bytes)):
            option_keys = tuple(str(item) for item in options)
        try:
            keys = parse_multiple_choice_keys(
                candidate.reference_answer,
                option_keys=option_keys,
                label="候选参考答案",
            )
        except InvalidAnswerFormatError:
            return _issue(
                QUESTION_ANSWER_ENCODING_INCOMPATIBLE,
                "多选题参考答案必须使用与选项一致的选项键编码。",
                "reference_answer",
            )
        if not keys:
            return _issue(
                QUESTION_ANSWER_ENCODING_INCOMPATIBLE,
                "多选题参考答案为空，无法按选项键集合评分。",
                "reference_answer",
            )
        return None
    if question_type in _TEXT_ANSWER_TYPES:
        # 填空题、简答题与论述题由文本控件作答，评分链路按文本等值或主观题处理，无需额外编码约束。
        return None
    return None


def _rubric_issue(candidate: QuestionCandidate) -> QuestionValidationIssue | None:
    """检查评分标准是否可用：必须写明可分配的分值数字。

    可用性下限采用“至少包含一个数字”的客观规则：只有明确的分值（如“2 分”）才能让教师和
    评分链路复用该标准；没有分值的描述无法直接使用，应退回修订。
    """

    rubric = candidate.scoring_rubric.strip()
    if not rubric or not any(character.isdigit() for character in rubric):
        return _issue(
            QUESTION_RUBRIC_UNUSABLE,
            "评分标准必须写明可分配的分值数字（如“每个要点 2 分”），当前内容不可用。",
            "scoring_rubric",
        )
    return None


def _score_issue(candidate: QuestionCandidate) -> QuestionValidationIssue | None:
    """检查分值是否可落库：有限正数、最多两位小数、不超过 M1 上限。"""

    raw = candidate.score
    if isinstance(raw, bool):
        return _issue(QUESTION_SCORE_NOT_STORABLE, "候选题分值必须是有限正数。", "score")
    try:
        score = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return _issue(QUESTION_SCORE_NOT_STORABLE, "候选题分值必须是有限正数。", "score")
    if not score.is_finite() or score <= 0:
        return _issue(QUESTION_SCORE_NOT_STORABLE, "候选题分值必须是有限正数。", "score")
    try:
        quantized = score.quantize(SCORE_QUANTUM)
    except InvalidOperation:
        return _issue(QUESTION_SCORE_NOT_STORABLE, "候选题分值无法量化到两位小数。", "score")
    if quantized != score:
        return _issue(
            QUESTION_SCORE_NOT_STORABLE,
            "候选题分值最多保留两位小数。",
            "score",
        )
    if quantized > MAX_CANDIDATE_SCORE:
        return _issue(
            QUESTION_SCORE_NOT_STORABLE,
            f"候选题分值不能超过 {MAX_CANDIDATE_SCORE}。",
            "score",
        )
    return None


def _evidence_issues(
    candidate: QuestionCandidate,
    whitelist: frozenset[str],
) -> list[QuestionValidationIssue]:
    """检查课程依据：必须有引用，且引用必须属于本次写入生成 Prompt 的片段。"""

    cited = [str(chunk_id) for chunk_id in candidate.source_context_ids if str(chunk_id).strip()]
    if not cited:
        return [
            _issue(
                QUESTION_MISSING_COURSE_EVIDENCE,
                "候选题未引用任何课程片段，缺少课程依据，需要教师修订或重新生成。",
                "source_context_ids",
            )
        ]
    unknown = [chunk_id for chunk_id in cited if chunk_id not in whitelist]
    if unknown:
        return [
            _issue(
                QUESTION_UNKNOWN_COURSE_EVIDENCE,
                "候选题引用了本次检索未写入生成上下文的片段，来源不可追溯。",
                "source_context_ids",
            )
        ]
    return []


class QuestionValidator:
    """校验候选题并给出审核状态建议；不写数据库、不调用 LLM。

    :param actor: 触发校验的流程角色；默认 ``VALIDATOR``。Agent 与校验器路径永远不能产生
        可发布状态，批准动作必须由 TEACHER 走 :func:`plan_transition` 完成。
    """

    def __init__(self, *, actor: ValidationActor | str = ValidationActor.VALIDATOR) -> None:
        self._actor = _normalize_actor(actor)

    @property
    def actor(self) -> ValidationActor:
        """返回当前校验角色。"""

        return self._actor

    def validate_candidate(
        self,
        candidate: QuestionCandidate,
        *,
        retrieved_context_ids: Sequence[str],
    ) -> CandidateValidationResult:
        """校验单个候选题，返回 ``Pending Review`` 或 ``Needs Revision``。"""

        if candidate.status != CANDIDATE_GENERATION_STATUS:
            return CandidateValidationResult(
                status=QuestionStatus.NEEDS_REVISION,
                issues=(
                    _issue(
                        QUESTION_NOT_CANDIDATE,
                        "待校验内容不是候选生成结果，不能作为候选题进入教师审核。",
                        "status",
                    ),
                ),
            )

        whitelist = frozenset(
            str(chunk_id) for chunk_id in retrieved_context_ids if str(chunk_id).strip()
        )
        issues: list[QuestionValidationIssue] = []
        issues.extend(_evidence_issues(candidate, whitelist))
        for check in (_answer_encoding_issue, _rubric_issue, _score_issue):
            issue = check(candidate)
            if issue is not None:
                issues.append(issue)

        status = (
            QuestionStatus.PENDING_REVIEW if not issues else QuestionStatus.NEEDS_REVISION
        )
        return CandidateValidationResult(status=status, issues=tuple(issues))

    def validate_candidates(
        self,
        candidates: Sequence[QuestionCandidate],
        *,
        retrieved_context_ids: Sequence[str],
        generation_request: object | None = None,
    ) -> CandidateBatchValidation:
        """校验整批候选，并检查整批条件（难度匹配、知识点覆盖）。

        ``generation_request`` 为 T065 的 ``QuestionGenerationRequest``；为 ``None`` 时只做逐题
        校验。整批条件问题记录在 ``issues`` 中，任一逐题或整批问题都会让整批进入
        ``Needs Revision``，逐题结果仍保持可见，便于教师定位需要修订的题目。
        """

        results = tuple(
            self.validate_candidate(
                candidate,
                retrieved_context_ids=retrieved_context_ids,
            )
            for candidate in candidates
        )
        batch_issues = self._batch_issues(candidates, generation_request)
        return CandidateBatchValidation(results=results, issues=batch_issues)

    @staticmethod
    def _batch_issues(
        candidates: Sequence[QuestionCandidate],
        generation_request: object | None,
    ) -> tuple[QuestionValidationIssue, ...]:
        """检查整批条件：非空难度逐题匹配，请求知识点必须被整批候选覆盖。"""

        if generation_request is None:
            return ()
        issues: list[QuestionValidationIssue] = []
        difficulty = getattr(generation_request, "difficulty", None)
        requested_difficulty = difficulty.strip() if isinstance(difficulty, str) else ""
        if requested_difficulty and any(
            normalize_text(candidate.difficulty) != normalize_text(requested_difficulty)
            for candidate in candidates
        ):
            issues.append(
                _issue(
                    QUESTION_DIFFICULTY_MISMATCH,
                    "候选题目难度与教师要求的难度不一致。",
                    "difficulty",
                )
            )
        requested_points = [
            point.strip()
            for point in (getattr(generation_request, "knowledge_points", None) or [])
            if isinstance(point, str) and point.strip()
        ]
        if requested_points:
            covered = {
                normalize_text(point)
                for candidate in candidates
                for point in candidate.knowledge_points
            }
            if any(normalize_text(point) not in covered for point in requested_points):
                issues.append(
                    _issue(
                        QUESTION_KNOWLEDGE_POINT_UNCOVERED,
                        "候选题目未覆盖教师要求的全部知识点。",
                        "knowledge_points",
                    )
                )
        return tuple(issues)


__all__ = [
    "CANDIDATE_GENERATION_STATUS",
    "CANDIDATE_STATUS_TRANSITIONS",
    "MAX_CANDIDATE_SCORE",
    "QUESTION_ANSWER_ENCODING_INCOMPATIBLE",
    "QUESTION_AUTOMATIC_PUBLISH_BLOCKED",
    "QUESTION_DIFFICULTY_MISMATCH",
    "QUESTION_KNOWLEDGE_POINT_UNCOVERED",
    "QUESTION_MISSING_COURSE_EVIDENCE",
    "QUESTION_NOT_CANDIDATE",
    "QUESTION_RUBRIC_UNUSABLE",
    "QUESTION_SCORE_NOT_STORABLE",
    "QUESTION_STATUS_TRANSITION_BLOCKED",
    "QUESTION_UNKNOWN_COURSE_EVIDENCE",
    "SCORE_QUANTUM",
    "TEACHER_ONLY_STATUSES",
    "VALIDATOR_RESULT_STATUSES",
    "AutomaticPublishingBlockedError",
    "CandidateBatchValidation",
    "CandidateValidationResult",
    "QuestionValidationIssue",
    "QuestionValidator",
    "QuestionValidatorError",
    "StatusTransitionBlockedError",
    "ValidationActor",
    "plan_transition",
]
