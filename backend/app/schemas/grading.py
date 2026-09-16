"""阅卷结果汇总与诊断使用的数据传输对象。

本模块承载 M3 阅卷链路的读模型 DTO：

- :class:`ExpectedAnswer` 与 :class:`SubmissionContext`：服务端权威的预期题目/答案集合
  与题序，供汇总阶段判断完整性，不由收到的结果条数推断。
- :class:`QuestionResultDTO` 与 :class:`ExamResultDTO`：单题与整卷的汇总结果。
- :class:`ConfidenceDecisionDTO`：本次评分实际执行的置信度决策快照。
- :class:`ExamResultStatus`：**读模型**整卷结果状态，刻意与 ``SubmissionStatus``、
  ``GradingStatus``、``ReviewStatus`` 分离，避免“任务执行完成”与“成绩已经最终确认”
  混用同一字段。

单题评分 DTO 复用 :mod:`backend.app.schemas.ai` 的 :class:`GradingResult`，本模块不复制
第二套同名评分 DTO；持久化实体属 T060～T064，本阶段只定义数据形状。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.app.domain.enums import QuestionType
from backend.app.schemas.ai import ConfidenceScore, NonEmptyText

#: 单题得分（不小于零）。
DecimalScore = Annotated[Decimal, Field(ge=0)]
#: 单题满分（必须大于零）。
PositiveDecimalScore = Annotated[Decimal, Field(gt=0)]


class ExamResultStatus(StrEnum):
    """整卷结果状态（读模型，非持久化实体字段）。

    - ``Pending``：存在尚未完成评分的题目（缺结果、未通过校验或等待重新评分）。
    - ``Pending Review``：全部题目已评分，但存在低置信度题目等待教师复核。
    - ``Final``：全部预期题目均为最终接受结果，可以给出最终总分。
    - ``Failed``：存在结构化校验失败或评分失败的题目，不得进入最终成绩。
    """

    PENDING = "Pending"
    PENDING_REVIEW = "Pending Review"
    FINAL = "Final"
    FAILED = "Failed"


class ExpectedAnswer(BaseModel):
    """服务端权威的单题预期集合条目（题序 + 答案/题目关联 + 满分 + 知识点）。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    order: int = Field(ge=1, description="考试中的题序，从 1 开始。")
    answer_id: NonEmptyText = Field(description="答卷中的答案标识。")
    question_id: NonEmptyText = Field(description="题目标识。")
    question_type: QuestionType = Field(description="题目自身声明的题型。")
    max_score: PositiveDecimalScore = Field(description="题目满分，来自题目定义。")
    knowledge_points: list[NonEmptyText] = Field(
        default_factory=list,
        description="题目声明的知识点；未声明时为空列表。",
    )


class SubmissionContext(BaseModel):
    """一次汇总所需的答卷上下文。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    submission_id: NonEmptyText = Field(description="答卷标识。")
    exam_id: NonEmptyText = Field(description="考试标识。")
    student_id: NonEmptyText = Field(description="学生标识。")
    expected_answers: list[ExpectedAnswer] = Field(
        description="预期题目/答案集合与题序，必须来自服务端考试定义。",
    )


class ConfidenceDecisionDTO(BaseModel):
    """本次评分实际执行的置信度决策快照。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    confidence: ConfidenceScore = Field(description="评分置信度。")
    threshold: ConfidenceScore = Field(description="本次决策使用的阈值。")
    requires_review: bool = Field(description="是否需要人工复核。")
    review_status: NonEmptyText = Field(description="决策对应的复核状态。")
    grading_status: NonEmptyText = Field(description="决策对应的评分状态。")
    reason: NonEmptyText = Field(description="决策原因。")


class QuestionResultDTO(BaseModel):
    """单个预期题目的汇总结果（缺结果时如实标注，不补 0 分）。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    order: int = Field(ge=1, description="考试题序。")
    answer_id: NonEmptyText = Field(description="答案标识。")
    question_id: NonEmptyText = Field(description="题目标识。")
    question_type: QuestionType = Field(description="题目题型。")
    max_score: PositiveDecimalScore = Field(description="题目满分。")
    score: DecimalScore | None = Field(
        default=None,
        description="收到的单题正式得分；缺少结果时为 None。",
    )
    effective_score: DecimalScore | None = Field(
        default=None,
        description="计入最终总分时使用的分数；未计入时为 None。",
    )
    counted: bool = Field(description="是否已满足最终接受条件并计入总分。")
    missing: bool = Field(default=False, description="是否缺少评分结果。")
    requires_review: bool = Field(default=False, description="是否需要人工复核。")
    grading_status: NonEmptyText = Field(description="平台判定的单题评分状态。")
    review_status: NonEmptyText | None = Field(
        default=None, description="结果中的复核状态；缺结果时为 None。"
    )
    validation_status: NonEmptyText | None = Field(
        default=None, description="结果中的结构化校验状态；缺结果时为 None。"
    )
    reason: NonEmptyText | None = Field(default=None, description="评分理由。")
    knowledge_points: list[NonEmptyText] = Field(
        default_factory=list, description="题目知识点。"
    )
    missing_knowledge_points: list[NonEmptyText] = Field(
        default_factory=list, description="学生答案缺失的知识点。"
    )
    decision: ConfidenceDecisionDTO | None = Field(
        default=None, description="本次评分的置信度决策；客观题与人工结论为空。"
    )

    @model_validator(mode="after")
    def _validate_counted_consistency(self) -> QuestionResultDTO:
        """计入总分时必须给出实际使用的分数，未计入时不得给出。"""

        if self.counted and self.effective_score is None:
            raise ValueError("计入总分的题目必须给出 effective_score。")
        if not self.counted and self.effective_score is not None:
            raise ValueError("未计入总分的题目不得给出 effective_score。")
        if self.counted and self.missing:
            raise ValueError("缺少结果的题目不得计入总分。")
        return self


class ExamResultDTO(BaseModel):
    """整卷汇总结果：状态、最终分数与逐题明细。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    submission_id: NonEmptyText = Field(description="答卷标识。")
    exam_id: NonEmptyText = Field(description="考试标识。")
    student_id: NonEmptyText = Field(description="学生标识。")
    result_status: ExamResultStatus = Field(description="整卷结果状态。")
    is_final: bool = Field(description="是否已形成最终成绩。")
    final_total_score: DecimalScore | None = Field(
        default=None, description="最终总分；未形成最终成绩时为 None。"
    )
    confirmed_subtotal: DecimalScore = Field(
        description="已确认部分小计；不得当作最终总分使用。"
    )
    confirmed_subtotal_label: str = Field(
        description="已确认部分小计的中文说明，供界面直接展示。"
    )
    total_max_score: PositiveDecimalScore = Field(description="整卷满分。")
    expected_answer_count: int = Field(ge=1, description="预期题目数量。")
    graded_answer_count: int = Field(ge=0, description="已收到结果的题目数量。")
    counted_answer_count: int = Field(ge=0, description="已计入总分的题目数量。")
    pending_review_answer_count: int = Field(ge=0, description="待人工复核题目数量。")
    missing_answer_ids: list[NonEmptyText] = Field(
        default_factory=list, description="尚缺评分结果的答案标识。"
    )
    failed_answer_ids: list[NonEmptyText] = Field(
        default_factory=list, description="评分失败的答案标识。"
    )
    not_validated_answer_ids: list[NonEmptyText] = Field(
        default_factory=list, description="未通过结构化校验的答案标识。"
    )
    items: list[QuestionResultDTO] = Field(description="按考试题序排列的逐题明细。")
    aggregated_at: datetime = Field(description="本次汇总时间。")

    @model_validator(mode="after")
    def _validate_final_consistency(self) -> ExamResultDTO:
        """最终成绩与非最终状态必须与总分字段保持一致。"""

        if self.is_final and self.final_total_score is None:
            raise ValueError("最终成绩必须给出 final_total_score。")
        if not self.is_final and self.final_total_score is not None:
            raise ValueError("非最终成绩不得给出 final_total_score。")
        if self.is_final and self.result_status is not ExamResultStatus.FINAL:
            raise ValueError("is_final 为真时 result_status 必须是 Final。")
        if self.result_status is ExamResultStatus.FINAL and not self.is_final:
            raise ValueError("result_status 为 Final 时 is_final 必须为真。")
        return self


__all__ = [
    "ConfidenceDecisionDTO",
    "DecimalScore",
    "ExamResultDTO",
    "ExamResultStatus",
    "ExpectedAnswer",
    "PositiveDecimalScore",
    "QuestionResultDTO",
    "SubmissionContext",
]
