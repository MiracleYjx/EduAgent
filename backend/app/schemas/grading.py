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
#: 掌握度比例（0 到 1）。
MasteryRatio = Annotated[Decimal, Field(ge=0, le=1)]


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
    correct_points: list[NonEmptyText] = Field(
        default_factory=list,
        description="学生答案命中的正确要点；保持原顺序且不去重。",
    )
    suggestions: list[NonEmptyText] = Field(
        default_factory=list,
        description="面向学生的学习建议；保持原顺序且不去重。",
    )
    retrieved_context_ids: list[NonEmptyText] = Field(
        default_factory=list,
        description="评分使用的课程知识片段标识；保持原顺序与重复关系。",
    )
    confidence: ConfidenceScore | None = Field(
        default=None,
        description="原评分置信度；缺评分结果时为 None，不用决策字段替代。",
    )
    submission_id: NonEmptyText | None = Field(
        default=None, description="关联答卷标识，使单题结果自描述归属。"
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


class DiagnosisStatus(StrEnum):
    """诊断报告状态。

    - ``Ready``：基于最终 ExamResult 生成，可展示。
    - ``Not Ready``：整卷尚未形成最终成绩（含待复核、未完成、重新评分与失败题目）。
    - ``Failed``：平台计算完成但建议生成失败，必须展示失败原因，不得冒充成功。
    - ``Stale``：ExamResult 已更新，旧报告不再代表当前最终诊断。
    """

    READY = "Ready"
    NOT_READY = "Not Ready"
    FAILED = "Failed"
    STALE = "Stale"


class MasteryByKnowledgePointDTO(BaseModel):
    """按知识点的掌握度（平台计算字段）。

    掌握度 = 该知识点已确认得分合计 / 该知识点满分合计；分子分母均按题目累加，
    不使用评分 ``confidence``，且属于本阶段新增业务约定。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    knowledge_point: NonEmptyText = Field(description="知识点名称。")
    answered_count: int = Field(ge=1, description="该知识点覆盖的题目数量。")
    correct_count: int = Field(ge=0, description="该知识点完全正确的题目数量。")
    awarded_score: DecimalScore = Field(description="该知识点已确认得分合计。")
    max_score: PositiveDecimalScore = Field(description="该知识点满分合计。")
    mastery: MasteryRatio = Field(description="掌握度，两位小数。")


class WeakKnowledgePointDTO(BaseModel):
    """低于阈值（新增业务约定，默认 0.60）的薄弱知识点。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    knowledge_point: NonEmptyText = Field(description="知识点名称。")
    reason: NonEmptyText = Field(description="判定原因，含掌握度与阈值。")
    error_count: int = Field(ge=0, description="未得满分的题目数量。")
    awarded_score: DecimalScore = Field(description="已确认得分合计。")
    max_score: PositiveDecimalScore = Field(description="满分合计。")
    mastery: MasteryRatio = Field(description="掌握度，两位小数。")


class DiagnosisReportDTO(BaseModel):
    """学生学习诊断报告：平台计算字段与 LLM 建议字段分离。

    平台字段：``mastery_by_knowledge_point``、``weak_knowledge_points``、
    ``error_reasons``、``status``、``generated_at``。
    LLM 字段：``learning_suggestions``（必须经结构化输出与 Pydantic 校验，允许空集合）。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    exam_result_id: NonEmptyText | None = Field(
        default=None,
        description="关联的整卷结果标识；T061 持久化前为应用层标识。",
    )
    submission_id: NonEmptyText = Field(description="答卷标识。")
    student_id: NonEmptyText = Field(description="学生标识。")
    status: DiagnosisStatus = Field(description="诊断状态。")
    mastery_by_knowledge_point: list[MasteryByKnowledgePointDTO] = Field(
        default_factory=list, description="按知识点的掌握度。"
    )
    weak_knowledge_points: list[WeakKnowledgePointDTO] = Field(
        default_factory=list, description="薄弱知识点。"
    )
    error_reasons: list[NonEmptyText] = Field(
        default_factory=list, description="平台计算的错误原因。"
    )
    learning_suggestions: list[NonEmptyText] = Field(
        default_factory=list, description="LLM 生成的学习建议。"
    )
    insufficient_evidence_answer_ids: list[NonEmptyText] = Field(
        default_factory=list,
        description="未声明知识点、无法归因为掌握度的答案标识。",
    )
    generated_at: datetime | None = Field(
        default=None, description="报告生成时间；未就绪时为 None。"
    )
    source_exam_result_updated_at: datetime | None = Field(
        default=None,
        description="生成时所消费的 ExamResult 汇总时间，用于失效判断。",
    )
    error_code: str | None = Field(default=None, description="失败错误码。")
    retryable: bool | None = Field(
        default=None, description="失败是否可重试；未知时为 None。"
    )
    source_code: str | None = Field(
        default=None, description="来源 Provider 错误码（已脱敏）。"
    )
    attempt_count: int | None = Field(
        default=None, ge=0, description="来源 Provider 尝试次数。"
    )

    @model_validator(mode="after")
    def _validate_status_consistency(self) -> DiagnosisReportDTO:
        """状态与错误、时间字段必须自洽，避免把失败伪装成就绪。"""

        if self.status is DiagnosisStatus.READY:
            if self.error_code is not None:
                raise ValueError("就绪诊断不得携带 error_code。")
            if self.generated_at is None:
                raise ValueError("就绪诊断必须给出 generated_at。")
        if self.status is DiagnosisStatus.FAILED and self.error_code is None:
            raise ValueError("失败诊断必须给出 error_code。")
        return self


class GradingTaskStatus(StrEnum):
    """阅卷任务运行时状态。

    与整卷结果状态分离：``Completed`` 只表示任务执行完成，不代表成绩已最终确认。
    本阶段不是 ``WorkflowRun``（T064），因此不具备跨重启恢复能力。
    """

    QUEUED = "Queued"
    RUNNING = "Running"
    COMPLETED = "Completed"
    FAILED = "Failed"


class GradingTaskStatusDTO(BaseModel):
    """阅卷任务状态读模型。

    ``durable`` 必须如实反映持久性：T060/T064 前任务句柄只存在于当前进程的存储实现中，
    调用方不得把它当作可跨重启查询的事实源。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    task_id: NonEmptyText = Field(description="任务标识。")
    submission_id: NonEmptyText = Field(description="关联答卷标识。")
    status: GradingTaskStatus = Field(description="任务运行时状态。")
    durable: bool = Field(
        default=False, description="任务状态是否已持久化；T060/T064 前为 False。"
    )
    reused: bool = Field(
        default=False, description="是否复用了同一答卷已有的进行中任务。"
    )
    created_at: datetime = Field(description="任务创建时间。")
    started_at: datetime | None = Field(default=None, description="开始执行时间。")
    finished_at: datetime | None = Field(default=None, description="结束时间。")
    expected_answer_count: int | None = Field(
        default=None, ge=0, description="预期题目数量。"
    )
    graded_answer_count: int | None = Field(
        default=None, ge=0, description="已评分题目数量。"
    )
    pending_review_answer_count: int | None = Field(
        default=None, ge=0, description="待人工复核题目数量。"
    )
    exam_result_status: ExamResultStatus | None = Field(
        default=None, description="整卷结果状态；未产出时为 None。"
    )
    is_final: bool | None = Field(
        default=None, description="整卷是否已形成最终成绩；未产出时为 None。"
    )
    error_code: str | None = Field(default=None, description="失败错误码。")
    error_message: str | None = Field(default=None, description="脱敏失败说明。")
    retryable: bool | None = Field(
        default=None, description="失败是否可重试；未失败时为 None。"
    )

    @model_validator(mode="after")
    def _validate_status_consistency(self) -> GradingTaskStatusDTO:
        """失败必须有错误码，完成不得携带错误码。"""

        if self.status is GradingTaskStatus.FAILED and self.error_code is None:
            raise ValueError("失败任务必须给出 error_code。")
        if self.status is GradingTaskStatus.COMPLETED and self.error_code is not None:
            raise ValueError("完成任务不得携带 error_code。")
        return self


class StudentResultSummaryDTO(BaseModel):
    """成绩列表条目；教师面额外填充 ``student_id``。

    未生成最终成绩时 ``total_score`` 为 ``None``，并以 ``not_ready_reason`` 明确空态原因，
    不得用 0 分或 0 替代。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    submission_id: NonEmptyText = Field(description="答卷标识。")
    exam_id: NonEmptyText = Field(description="考试标识。")
    exam_title: NonEmptyText = Field(description="考试名称。")
    student_id: NonEmptyText | None = Field(
        default=None, description="学生标识；教师面查询时填充。"
    )
    submitted_at: datetime | None = Field(default=None, description="提交时间。")
    result_status: ExamResultStatus | None = Field(
        default=None, description="整卷结果状态；尚无结果时为 None。"
    )
    is_final: bool | None = Field(
        default=None, description="是否已形成最终成绩；尚无结果时为 None。"
    )
    total_score: DecimalScore | None = Field(
        default=None, description="最终总分；未最终确认时为 None。"
    )
    confirmed_subtotal: DecimalScore | None = Field(
        default=None, description="已确认部分小计。"
    )
    pending_review_count: int | None = Field(
        default=None, ge=0, description="待人工复核题目数量。"
    )
    not_ready_reason: str | None = Field(
        default=None, description="结果未就绪的明确原因。"
    )


class SubmissionResultDTO(BaseModel):
    """单份答卷的结果读模型；学生面与教师面共用，差别在授权与条目过滤。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    submission_id: NonEmptyText = Field(description="答卷标识。")
    exam_id: NonEmptyText = Field(description="考试标识。")
    exam_title: NonEmptyText = Field(description="考试名称。")
    student_id: NonEmptyText = Field(description="学生标识。")
    result_status: ExamResultStatus | None = Field(
        default=None, description="整卷结果状态。"
    )
    is_final: bool | None = Field(default=None, description="是否已形成最终成绩。")
    total_score: DecimalScore | None = Field(
        default=None, description="最终总分；未最终确认时为 None。"
    )
    confirmed_subtotal: DecimalScore | None = Field(
        default=None, description="已确认部分小计，不得当作最终总分。"
    )
    confirmed_subtotal_label: str | None = Field(
        default=None, description="已确认部分小计的展示文案。"
    )
    total_max_score: PositiveDecimalScore | None = Field(
        default=None, description="整卷满分。"
    )
    expected_answer_count: int | None = Field(default=None, ge=0)
    graded_answer_count: int | None = Field(default=None, ge=0)
    pending_review_count: int | None = Field(default=None, ge=0)
    items: list[QuestionResultDTO] = Field(
        default_factory=list, description="允许展示的逐题结果。"
    )
    mistake_answer_ids: list[NonEmptyText] = Field(
        default_factory=list, description="已确认未得满分的题目；不含待复核题目。"
    )
    diagnosis_status: DiagnosisStatus | None = Field(
        default=None, description="诊断状态；未生成时为 None。"
    )
    diagnosis: DiagnosisReportDTO | None = Field(
        default=None, description="诊断报告；未就绪时为 None。"
    )
    not_ready_reason: str | None = Field(
        default=None, description="结果未就绪的明确原因。"
    )


class TeacherExamResultSummaryDTO(BaseModel):
    """教师考试结果摘要；平均分只统计最终成绩。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    exam_id: NonEmptyText = Field(description="考试标识。")
    submitted_count: int = Field(default=0, ge=0, description="已提交答卷数量。")
    final_count: int = Field(default=0, ge=0, description="已形成最终成绩的数量。")
    pending_review_count: int = Field(default=0, ge=0, description="待复核题目数量合计。")
    average_of_final_scores: DecimalScore | None = Field(
        default=None, description="仅基于最终成绩的平均分；无最终成绩时为 None。"
    )
    not_ready: bool = Field(
        default=False, description="是否存在尚无结果的答卷或依赖未接通。"
    )
    not_ready_reason: str | None = Field(
        default=None, description="未就绪的明确原因，不得用 0 分或 0 人代替。"
    )


__all__ = [
    "ConfidenceDecisionDTO",
    "DecimalScore",
    "DiagnosisReportDTO",
    "DiagnosisStatus",
    "ExamResultDTO",
    "ExamResultStatus",
    "ExpectedAnswer",
    "GradingTaskStatus",
    "GradingTaskStatusDTO",
    "MasteryByKnowledgePointDTO",
    "MasteryRatio",
    "PositiveDecimalScore",
    "QuestionResultDTO",
    "StudentResultSummaryDTO",
    "SubmissionContext",
    "SubmissionResultDTO",
    "TeacherExamResultSummaryDTO",
    "WeakKnowledgePointDTO",
]
