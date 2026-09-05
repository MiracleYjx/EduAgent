"""AI 出题与阅卷使用的结构化数据传输对象。"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


class QuestionType(str, Enum):
    """题库支持的题型标识。"""

    SINGLE_CHOICE = "SINGLE_CHOICE"
    MULTIPLE_CHOICE = "MULTIPLE_CHOICE"
    TRUE_FALSE = "TRUE_FALSE"
    FILL_BLANK = "FILL_BLANK"
    SHORT_ANSWER = "SHORT_ANSWER"
    ESSAY = "ESSAY"


NonEmptyText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]
Score = Annotated[float, Field(ge=0)]
PositiveScore = Annotated[float, Field(gt=0)]
ConfidenceScore = Annotated[float, Field(ge=0, le=1)]
KnowledgePoints = Annotated[list[NonEmptyText], Field(min_length=1)]


class QuestionCandidate(BaseModel):
    """Question Agent 生成的待审核候选题目。"""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
    )

    question_type: QuestionType = Field(
        validation_alias=AliasChoices("question_type", "type"),
        description="题型标识。",
    )
    content: NonEmptyText = Field(description="题目内容。")
    options: list[NonEmptyText] | None = Field(
        default=None,
        description="选择题选项；非选择题可以不提供。",
    )
    reference_answer: NonEmptyText = Field(description="参考答案。")
    scoring_rubric: NonEmptyText = Field(description="评分标准。")
    difficulty: NonEmptyText = Field(description="题目难度。")
    knowledge_points: KnowledgePoints = Field(description="题目覆盖的知识点。")
    score: PositiveScore = Field(description="题目满分，必须大于零。")
    status: Literal["Candidate Generation"] = Field(
        default="Candidate Generation",
        validation_alias=AliasChoices("status", "generation_status"),
        description="候选题目的生成状态，不得自动发布。",
    )
    source_context_ids: list[NonEmptyText] = Field(
        default_factory=list,
        validation_alias=AliasChoices("source_context_ids", "retrieved_context_ids"),
        description="支撑候选题目的课程知识片段标识。",
    )

    @field_validator("options")
    @classmethod
    def _validate_options(
        cls,
        value: list[NonEmptyText] | None,
    ) -> list[NonEmptyText] | None:
        """拒绝重复选项，避免结构化题目产生歧义。"""

        if value is not None and len(set(value)) != len(value):
            raise ValueError("选项不得重复。")
        return value

    @model_validator(mode="after")
    def _validate_choice_options(self) -> QuestionCandidate:
        """选择题必须包含足够的选项，其他题型不强制提供选项。"""

        if self.question_type in {
            QuestionType.SINGLE_CHOICE,
            QuestionType.MULTIPLE_CHOICE,
        } and (self.options is None or len(self.options) < 2):
            raise ValueError("单选题或多选题至少需要两个选项。")
        return self

    @property
    def generation_status(self) -> str:
        """返回兼容的候选生成状态名称。"""

        return self.status


class GradingResult(BaseModel):
    """单道题经过结构化校验的评分结果。"""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
    )

    question_type: QuestionType = Field(
        validation_alias=AliasChoices("question_type", "type"),
        description="被评分题目的题型。",
    )
    score: Score = Field(description="本题得分，不能小于零。")
    max_score: PositiveScore = Field(description="本题满分，必须大于零。")
    reason: NonEmptyText = Field(description="评分理由。")
    correct_points: list[NonEmptyText] = Field(
        description="学生答案中命中的正确知识点或要点。",
    )
    missing_knowledge_points: list[NonEmptyText] = Field(
        description="学生答案中缺失的知识点或要点。",
    )
    knowledge_points: list[NonEmptyText] = Field(
        default_factory=list,
        description="本题涉及的知识点汇总。",
    )
    suggestions: list[NonEmptyText] = Field(
        description="面向学生的学习建议。",
    )
    confidence: ConfidenceScore = Field(description="评分置信度，范围为 0 到 1。")
    validation_status: NonEmptyText = Field(
        default="Validated",
        description="结构化校验状态。",
    )
    review_status: NonEmptyText = Field(
        default="Not Required",
        description="人工复核状态。",
    )
    retrieved_context_ids: list[NonEmptyText] = Field(
        default_factory=list,
        description="评分所使用的课程知识片段标识。",
    )
    answer_id: NonEmptyText | None = Field(
        default=None,
        description="关联的答案标识。",
    )
    submission_id: NonEmptyText | None = Field(
        default=None,
        description="关联的答卷标识。",
    )

    @model_validator(mode="after")
    def _validate_score_range(self) -> GradingResult:
        """确保单题得分不会超过题目满分。"""

        if self.score > self.max_score:
            raise ValueError("本题得分不能超过题目满分。")
        return self


# 保留显式 DTO 别名，便于后续服务按领域名称导入。
QuestionCandidateDTO = QuestionCandidate
GradingResultDTO = GradingResult


__all__ = [
    "ConfidenceScore",
    "GradingResult",
    "GradingResultDTO",
    "KnowledgePoints",
    "QuestionCandidate",
    "QuestionCandidateDTO",
    "QuestionType",
]
