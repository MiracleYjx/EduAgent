"""G05 持久证据 Schema；本批不执行 Vision 或人工语义核对。"""
from __future__ import annotations

from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from backend.app.schemas.paper_import import PixelRegion


class EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ImageInput(EvidenceModel):
    asset_id: UUID
    file_id: str = Field(min_length=1)
    image_index: int = Field(ge=1, le=5, strict=True)
    asset_type: Literal["figure", "table", "diagram"]
    source_page_id: UUID | None
    region: PixelRegion | None
    width: int | None = Field(default=None, gt=0, strict=True)
    height: int | None = Field(default=None, gt=0, strict=True)
    mime_type: str | None = None


class ImageInputRefs(EvidenceModel):
    text_fields: list[str]
    images: list[ImageInput] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def image_order(self) -> Self:
        if [image.image_index for image in self.images] != list(range(1, len(self.images) + 1)):
            raise ValueError("图片序号须连续对应输入顺序。")
        if len({image.asset_id for image in self.images}) != len(self.images):
            raise ValueError("图片输入身份重复。")
        return self


class ImageIssue(EvidenceModel):
    issue_id: UUID
    asset_ids: list[UUID] | None
    message: str = Field(min_length=1)


class Observation(EvidenceModel):
    image_index: int = Field(ge=1, le=5, strict=True)
    kind: str = Field(min_length=1)
    description: str = Field(min_length=1)
    bbox: tuple[float, float, float, float] | None = None


class MachineCondition(EvidenceModel):
    condition_id: UUID
    asset_id: UUID
    image_index: int = Field(ge=1, le=5, strict=True)
    text: str = Field(min_length=1)
    evidence_region: PixelRegion | None


class ImageUnderstandingResult(EvidenceModel):
    observations: list[Observation]
    conditions: list[MachineCondition]
    unresolved_issues: list[ImageIssue]
    requires_manual_review: bool


class TechnicalError(EvidenceModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    stage: str = Field(min_length=1)
    retryable: bool
    cause: str | None = None


class Provenance(EvidenceModel):
    provider_name: str | None
    model: str | None
    model_version: str | None
    prompt_version: str | None


class ImageUnderstandingRun(EvidenceModel):
    id: UUID
    run_no: int = Field(ge=1, strict=True)
    context_revision: int = Field(ge=0, strict=True)
    task: str = Field(min_length=1)
    input_refs: ImageInputRefs
    outcome: Literal["running", "completed", "technical_error"]
    result: ImageUnderstandingResult | None
    error: TechnicalError | None
    executor_kind: Literal["service", "agent"]
    executor_name: str = Field(min_length=1)
    requested_by: UUID | None
    agent_run_id: UUID | None
    provenance: Provenance
    started_at: AwareDatetime
    completed_at: AwareDatetime | None

    @model_validator(mode="after")
    def truthful_outcome(self) -> Self:
        if self.outcome == "running":
            if any(value is not None for value in (self.result, self.error, self.completed_at)):
                raise ValueError("未结束调用不能保存成功/失败结束事实。")
        elif self.completed_at is None or self.completed_at < self.started_at:
            raise ValueError("结束时间须真实且不早于开始。")
        elif self.outcome == "completed":
            if self.result is None or self.error is not None or any(image.width is None or image.height is None or image.mime_type is None for image in self.input_refs.images):
                raise ValueError("成功调用必须有合法结果和真实图像事实。")
        elif self.error is None or self.result is not None:
            raise ValueError("技术失败必须保留真实错误，不保存成功结果。")
        return self


class ConfirmedCondition(EvidenceModel):
    condition_id: UUID
    asset_id: UUID
    text: str = Field(min_length=1)
    evidence_region: PixelRegion | None
    source_condition_id: UUID | None


class ImageFinding(EvidenceModel):
    asset_id: UUID
    finding: Literal["conditions_confirmed", "no_conditions_needed", "unresolved"]
    reason: str = Field(min_length=1)


class IssueResolution(EvidenceModel):
    issue_id: UUID
    resolution: Literal["resolved", "unresolved"]
    reason: str = Field(min_length=1)


class ImageManualCheck(EvidenceModel):
    id: UUID
    check_no: int = Field(ge=1, strict=True)
    context_revision: int = Field(ge=0, strict=True)
    run_no: int = Field(ge=0, strict=True)
    run_id: UUID | None
    input_refs: ImageInputRefs
    status: Literal["confirmed", "unresolved"]
    confirmed_conditions: list[ConfirmedCondition]
    image_findings: list[ImageFinding]
    issues: list[ImageIssue]
    issue_resolutions: list[IssueResolution]
    teacher_id: UUID
    checked_at: AwareDatetime
    explanation: str = Field(min_length=1)

    @model_validator(mode="after")
    def whole_group_review(self) -> Self:
        ids = {image.asset_id for image in self.input_refs.images}
        if {finding.asset_id for finding in self.image_findings} != ids or len(self.image_findings) != len(ids):
            raise ValueError("人工核对必须逐一对应整组当前图像。")
        if any(image.width is None or image.height is None or image.mime_type is None for image in self.input_refs.images):
            raise ValueError("人工核对须有实际可读图像尺寸/格式。")
        if any(condition.asset_id not in ids for condition in self.confirmed_conditions):
            raise ValueError("条件引用不属于核对输入。")
        for finding in self.image_findings:
            if finding.finding == "conditions_confirmed" and not any(c.asset_id == finding.asset_id for c in self.confirmed_conditions):
                raise ValueError("确认必要条件须保存实际条件。")
        if self.status == "confirmed" and (self.issues or any(f.finding == "unresolved" for f in self.image_findings) or any(r.resolution != "resolved" for r in self.issue_resolutions)):
            raise ValueError("未解决问题不能宣称已确认。")
        return self


class ImportedReviewSource(EvidenceModel):
    owner_kind: Literal["extracted_question"] = "extracted_question"
    owner_id: UUID
    check_id: UUID
    binding_id: None = None


class ImportedImageReview(EvidenceModel):
    id: UUID
    context_revision: int = Field(ge=0, strict=True)
    input_refs: ImageInputRefs
    source_ref: ImportedReviewSource
    bound_by: UUID
    bound_at: AwareDatetime


class ImageAssessment(EvidenceModel):
    context_revision: int = Field(default=0, ge=0, strict=True)
    runs: list[ImageUnderstandingRun] = Field(default_factory=list)
    manual_checks: list[ImageManualCheck] = Field(default_factory=list)
    imported_review: ImportedImageReview | None = None

    @model_validator(mode="after")
    def unique_events(self) -> Self:
        for events, number in ((self.runs, "run_no"), (self.manual_checks, "check_no")):
            numbers = [getattr(event, number) for event in events]
            if numbers != sorted(set(numbers)) or len({event.id for event in events}) != len(events):
                raise ValueError("调用/核对轮次或身份重复，或非单调顺序。")
            if any(event.context_revision > self.context_revision for event in events):
                raise ValueError("事件引用未来修订。")
        return self


def advance_image_context(value: dict | None) -> dict:
    if value is None:
        return ImageAssessment().model_dump(mode="json")
    assessment = ImageAssessment.model_validate(value)
    assessment.context_revision += 1
    return assessment.model_dump(mode="json")
