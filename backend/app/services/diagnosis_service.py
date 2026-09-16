"""T055 基于最终 ExamResult 的学生诊断报告。

契约依据：FR-038（评分完成后生成诊断）、SC-007（低置信度结果必须完成复核后才能进入
诊断）、plan.md §5.2（诊断只聚合已接受结果）、data-model.md 的 DiagnosisReport 定义。

准入规则（不做“拒绝或排除”的二选一）：

- 诊断只消费**最终** :class:`ExamResultDTO`；整卷存在未完成、未通过校验、待复核、
  重新评分或失败题目时，返回 ``status="Not Ready"``，且**不调用任何 LLM**。
- 允许合法空集合：全对答卷不强迫生成薄弱知识点或错误原因。

字段来源边界：

- 平台计算：``mastery_by_knowledge_point``、``weak_knowledge_points``、``error_reasons``、
  ``status``、``generated_at``。掌握度 = 该知识点已确认得分合计 / 满分合计（分子分母按
  题目累加、两位小数）；**不使用**评分 ``confidence`` 代替掌握度；题目未声明知识点时记入
  ``insufficient_evidence_answer_ids``，不填 0 冒充实测。
- LLM 生成：仅 ``learning_suggestions``，必须经 :meth:`BaseLLMProvider.generate_structured`
  与 Pydantic 校验；Provider 未就绪或结构化失败时返回 ``status="Failed"`` 并保留脱敏来源
  错误码，不返回伪造的成功诊断。

本文件中薄弱知识点阈值与掌握度公式属**新增业务约定**（文档未规定具体公式），默认 0.60，
可通过构造参数覆盖。

失效合同：教师最终结果改变后，``ExamResultDTO.aggregated_at`` 随之变化，旧报告通过
:meth:`DiagnosisService.is_current` 判定为过期，必须基于新 ExamResult 重新生成；本阶段只
定义消费与失效合同，不实现 T062/T074 的人工复核写入。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import ClassVar, Final

from pydantic import BaseModel, ConfigDict, Field

from backend.app.ai.llm.base import BaseLLMProvider, LLMMessage, LLMMessages
from backend.app.ai.llm.factory import create_llm_provider
from backend.app.core.retry_policy import ProviderExecutionError
from backend.app.schemas.ai import NonEmptyText
from backend.app.schemas.grading import (
    DiagnosisReportDTO,
    DiagnosisStatus,
    ExamResultDTO,
    MasteryByKnowledgePointDTO,
    QuestionResultDTO,
    WeakKnowledgePointDTO,
)

#: 诊断失败的通用错误码。
DIAGNOSIS_FAILED: Final[str] = "DIAGNOSIS_FAILED"
#: 整卷尚未形成最终成绩，诊断未就绪。
DIAGNOSIS_NOT_READY: Final[str] = "DIAGNOSIS_NOT_READY"
#: 学习建议生成失败（结构化输出或 Provider 调用失败）。
DIAGNOSIS_SUGGESTION_FAILED: Final[str] = "DIAGNOSIS_SUGGESTION_FAILED"
#: 建议 Provider 未就绪。
DIAGNOSIS_PROVIDER_NOT_READY: Final[str] = "DIAGNOSIS_PROVIDER_NOT_READY"
#: 薄弱知识点阈值配置非法。
DIAGNOSIS_INVALID_THRESHOLD: Final[str] = "DIAGNOSIS_INVALID_THRESHOLD"

#: 掌握度输出精度。
MASTERY_QUANTUM: Final[Decimal] = Decimal("0.01")
#: 薄弱知识点默认阈值（新增业务约定）。
DEFAULT_WEAK_MASTERY_THRESHOLD: Final[Decimal] = Decimal("0.60")

#: 建议生成的系统前缀，固定输出结构约束，避免远端模型返回自由文本。
SUGGESTION_SYSTEM_PROMPT: Final[str] = (
    "你是课程学习诊断助手。只能依据给定的平台计算结果，为学生生成中文学习建议。"
    "不得修改分数、掌握度或知识点，不得推断未提供的信息，不得输出学生答案原文。"
    "必须只输出 JSON 对象，字段为 suggestions（字符串数组，允许为空数组），"
    "不要输出任何额外文本。"
)


class DiagnosisError(RuntimeError):
    """诊断失败基类；可按来源保留重试语义。"""

    error_code: ClassVar[str] = DIAGNOSIS_FAILED
    #: 默认可重试语义；子类可覆盖，实例可用参数覆盖。
    retryable: bool = False

    def __init__(
        self,
        detail: str,
        *,
        retryable: bool | None = None,
        source_code: str | None = None,
        attempt_count: int | None = None,
    ) -> None:
        super().__init__(f"{self.error_code}：{detail}")
        self.detail = detail
        if retryable is not None:
            self.retryable = retryable
        self.source_code = source_code
        self.attempt_count = attempt_count


class InvalidThresholdError(DiagnosisError):
    """薄弱知识点阈值非法。"""

    error_code: ClassVar[str] = DIAGNOSIS_INVALID_THRESHOLD


class ProviderNotReadyError(DiagnosisError):
    """建议 Provider 未就绪。"""

    error_code: ClassVar[str] = DIAGNOSIS_PROVIDER_NOT_READY


class SuggestionGenerationError(DiagnosisError):
    """学习建议生成失败。"""

    error_code: ClassVar[str] = DIAGNOSIS_SUGGESTION_FAILED


class LearningSuggestions(BaseModel):
    """LLM 生成的学习建议结构化输出；允许空数组。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    suggestions: list[NonEmptyText] = Field(
        default_factory=list,
        description="面向学生的学习建议，允许为空数组。",
    )


def _resolve_threshold(value: Decimal | str | float | None) -> Decimal:
    """解析并校验薄弱知识点阈值，非法时显式失败。"""

    if value is None:
        return DEFAULT_WEAK_MASTERY_THRESHOLD
    try:
        resolved = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise InvalidThresholdError("薄弱知识点阈值无法解析为十进制数值。") from exc
    if not resolved.is_finite() or not Decimal(0) <= resolved <= Decimal(1):
        raise InvalidThresholdError("薄弱知识点阈值必须位于 [0, 1] 区间。")
    return resolved


def _to_score(value: Decimal) -> Decimal:
    """在输出边界量化到两位小数。"""

    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _to_ratio(awarded: Decimal, maximum: Decimal) -> Decimal:
    """计算掌握度比例并量化到两位小数。"""

    return (awarded / maximum).quantize(MASTERY_QUANTUM, rounding=ROUND_HALF_UP)


def _error_reason(item: QuestionResultDTO, effective: Decimal) -> str:
    """由平台从缺失要点派生错误原因，不依赖 LLM。"""

    missing = "、".join(item.missing_knowledge_points)
    if missing:
        return (
            f"{item.question_id}：缺少要点 {missing}"
            f"（得分 {effective}/{item.max_score}）。"
        )
    return (
        f"{item.question_id}：未得满分，请复核评分标准与作答完整性"
        f"（得分 {effective}/{item.max_score}）。"
    )


class DiagnosisService:
    """基于最终 ExamResult 生成学生诊断报告。

    :param provider: 建议 Provider；``None`` 表示调用时按既有工厂解析，未就绪时显式失败。
    :param weak_mastery_threshold: 薄弱知识点阈值（新增业务约定，默认 0.60）。
    """

    def __init__(
        self,
        *,
        provider: BaseLLMProvider | None = None,
        weak_mastery_threshold: Decimal | str | float | None = None,
    ) -> None:
        self._provider = provider
        self._threshold = _resolve_threshold(weak_mastery_threshold)

    @property
    def weak_mastery_threshold(self) -> Decimal:
        """返回生效的薄弱知识点阈值。"""

        return self._threshold

    def is_current(
        self,
        report: DiagnosisReportDTO,
        exam_result: ExamResultDTO,
    ) -> bool:
        """判断报告是否仍对应当前最终 ExamResult。

        教师改分或重新评分后 ``aggregated_at`` 变化，旧报告必须重新生成。
        """

        return (
            report.status is DiagnosisStatus.READY
            and report.submission_id == exam_result.submission_id
            and report.source_exam_result_updated_at == exam_result.aggregated_at
        )

    async def generate(self, exam_result: ExamResultDTO) -> DiagnosisReportDTO:
        """生成诊断报告；未就绪时不调用 LLM，失败时不伪造成功。"""

        if not exam_result.is_final:
            return self._not_ready_report(exam_result)

        entries, weak, error_reasons, insufficient = self._compute_platform_fields(
            exam_result
        )
        result_id = f"exam-result:{exam_result.submission_id}"
        try:
            suggestions = await self._generate_suggestions(
                exam_result,
                entries,
                weak,
                error_reasons,
            )
        except DiagnosisError as error:
            return DiagnosisReportDTO(
                exam_result_id=result_id,
                submission_id=exam_result.submission_id,
                student_id=exam_result.student_id,
                mastery_by_knowledge_point=entries,
                weak_knowledge_points=weak,
                error_reasons=error_reasons,
                insufficient_evidence_answer_ids=insufficient,
                source_exam_result_updated_at=exam_result.aggregated_at,
                status=DiagnosisStatus.FAILED,
                error_code=error.error_code,
                retryable=error.retryable,
                source_code=error.source_code,
                attempt_count=error.attempt_count,
                generated_at=datetime.now(UTC),
            )
        return DiagnosisReportDTO(
            exam_result_id=result_id,
            submission_id=exam_result.submission_id,
            student_id=exam_result.student_id,
            mastery_by_knowledge_point=entries,
            weak_knowledge_points=weak,
            error_reasons=error_reasons,
            insufficient_evidence_answer_ids=insufficient,
            source_exam_result_updated_at=exam_result.aggregated_at,
            status=DiagnosisStatus.READY,
            learning_suggestions=suggestions,
            generated_at=datetime.now(UTC),
        )

    def _not_ready_report(self, exam_result: ExamResultDTO) -> DiagnosisReportDTO:
        """构建未就绪报告：不推断结论、不调用 LLM。"""

        return DiagnosisReportDTO(
            exam_result_id=None,
            submission_id=exam_result.submission_id,
            student_id=exam_result.student_id,
            status=DiagnosisStatus.NOT_READY,
            error_code=DIAGNOSIS_NOT_READY,
            retryable=False,
            source_exam_result_updated_at=exam_result.aggregated_at,
        )

    def _compute_platform_fields(
        self,
        exam_result: ExamResultDTO,
    ) -> tuple[
        list[MasteryByKnowledgePointDTO],
        list[WeakKnowledgePointDTO],
        list[str],
        list[str],
    ]:
        """按知识点聚合已确认得分，派生掌握度、薄弱点与错误原因。"""

        awarded: dict[str, Decimal] = {}
        maximum: dict[str, Decimal] = {}
        answered: dict[str, int] = {}
        correct: dict[str, int] = {}
        order: list[str] = []
        error_reasons: list[str] = []
        insufficient: list[str] = []

        for item in exam_result.items:
            if not item.counted:
                continue
            effective = item.effective_score or Decimal(0)
            full = effective >= item.max_score
            if not full:
                error_reasons.append(_error_reason(item, effective))
            if not item.knowledge_points:
                insufficient.append(item.answer_id)
                continue
            for point in item.knowledge_points:
                if point not in awarded:
                    awarded[point] = Decimal(0)
                    maximum[point] = Decimal(0)
                    answered[point] = 0
                    correct[point] = 0
                    order.append(point)
                awarded[point] += effective
                maximum[point] += item.max_score
                answered[point] += 1
                if full:
                    correct[point] += 1

        entries: list[MasteryByKnowledgePointDTO] = []
        weak: list[WeakKnowledgePointDTO] = []
        for point in order:
            mastery = _to_ratio(awarded[point], maximum[point])
            awarded_score = _to_score(awarded[point])
            max_score = _to_score(maximum[point])
            entries.append(
                MasteryByKnowledgePointDTO(
                    knowledge_point=point,
                    answered_count=answered[point],
                    correct_count=correct[point],
                    awarded_score=awarded_score,
                    max_score=max_score,
                    mastery=mastery,
                )
            )
            if mastery < self._threshold:
                weak.append(
                    WeakKnowledgePointDTO(
                        knowledge_point=point,
                        reason=(
                            f"掌握度 {mastery} 低于阈值 {self._threshold}"
                            f"（{awarded_score}/{max_score}）。"
                        ),
                        error_count=answered[point] - correct[point],
                        awarded_score=awarded_score,
                        max_score=max_score,
                        mastery=mastery,
                    )
                )
        return entries, weak, error_reasons, insufficient

    async def _generate_suggestions(
        self,
        exam_result: ExamResultDTO,
        entries: list[MasteryByKnowledgePointDTO],
        weak: list[WeakKnowledgePointDTO],
        error_reasons: list[str],
    ) -> list[str]:
        """通过结构化输出生成学习建议，失败时保留脱敏来源语义。"""

        messages = _build_suggestion_messages(exam_result, entries, weak, error_reasons)
        provider = self._provider
        if provider is None:
            try:
                provider = create_llm_provider()
            except Exception:  # noqa: BLE001 - 未就绪不得静默降级
                raise ProviderNotReadyError(
                    "诊断建议 Provider 未就绪，请检查 LLM_PROVIDER 与模型配置。"
                ) from None
        try:
            payload = await provider.generate_structured(messages, LearningSuggestions)
        except ProviderExecutionError as exc:
            info = exc.info
            raise SuggestionGenerationError(
                f"诊断建议生成失败（来源码 {info.code}，尝试 {info.attempt_count} 次）。",
                retryable=bool(info.retryable),
                source_code=str(info.code),
                attempt_count=int(info.attempt_count),
            ) from None
        except DiagnosisError:
            raise
        except Exception:  # noqa: BLE001 - 统一收敛为脱敏失败
            raise SuggestionGenerationError("诊断建议生成失败。") from None
        if not isinstance(payload, LearningSuggestions):
            raise SuggestionGenerationError("诊断建议 Provider 未返回约定的结构化结果。")
        return list(payload.suggestions)


def _build_suggestion_messages(
    exam_result: ExamResultDTO,
    entries: list[MasteryByKnowledgePointDTO],
    weak: list[WeakKnowledgePointDTO],
    error_reasons: list[str],
) -> LLMMessages:
    """组装建议请求：只传平台计算结果，不传学生答案原文。"""

    lines = [
        f"答卷：{exam_result.submission_id}",
        f"考试：{exam_result.exam_id}",
        f"最终总分：{exam_result.final_total_score}/{exam_result.total_max_score}",
        "知识点掌握度：",
    ]
    if entries:
        lines.extend(
            f"- {entry.knowledge_point}：{entry.awarded_score}/{entry.max_score}"
            f"（掌握度 {entry.mastery}，{entry.correct_count}/{entry.answered_count} 题满分）"
            for entry in entries
        )
    else:
        lines.append("- 无可用知识点归因（题目未声明知识点）。")
    lines.append("薄弱知识点：")
    if weak:
        lines.extend(f"- {entry.knowledge_point}：{entry.reason}" for entry in weak)
    else:
        lines.append("- 无。")
    lines.append("错误原因：")
    if error_reasons:
        lines.extend(f"- {reason}" for reason in error_reasons)
    else:
        lines.append("- 无。")
    messages: list[LLMMessage] = [
        {"role": "system", "content": SUGGESTION_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]
    return messages


__all__ = [
    "DEFAULT_WEAK_MASTERY_THRESHOLD",
    "DIAGNOSIS_FAILED",
    "DIAGNOSIS_INVALID_THRESHOLD",
    "DIAGNOSIS_NOT_READY",
    "DIAGNOSIS_PROVIDER_NOT_READY",
    "DIAGNOSIS_SUGGESTION_FAILED",
    "MASTERY_QUANTUM",
    "SUGGESTION_SYSTEM_PROMPT",
    "DiagnosisError",
    "DiagnosisService",
    "InvalidThresholdError",
    "LearningSuggestions",
    "ProviderNotReadyError",
    "SuggestionGenerationError",
]
