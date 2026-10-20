"""Personal API DTOs; generated TS shares these exact schemas."""

from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)

from backend.app.schemas.content_blocks import (
    ContentBlock,
    RevisionContent,
    SourceLocation,
)
from backend.app.schemas.content_validation import SemanticMachineOutput
from backend.app.schemas.learning_charts import ChartPoint


class LearningOperationView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    purpose: str
    preview_digest: str
    runtime_snapshot_id: UUID
    state: str
    request_budget: int
    actual_requests: int
    steps: list[dict[str, Any]]
    estimate_state: str = "unknown"
    estimated_cost: str | None = None
    retry_of_id: UUID | None = None


class LearningPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    purpose: str = Field(min_length=1, max_length=80)
    idempotency_key: str = Field(min_length=1, max_length=160)
    scope: dict[str, Any]


class LearningConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_digest: str = Field(min_length=64, max_length=64)


class LearningApiError(BaseModel):
    code: str
    message: str
    stage: Literal["learning_operation", "personal_learning"] = "learning_operation"
    retryable: Literal[False] = False


class SubjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=4000)


class SubjectPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=4000)

    @field_validator("name")
    @classmethod
    def name_is_not_null(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("subject name cannot be null")
        return value

    @model_validator(mode="after")
    def require_change(self) -> "SubjectPatch":
        if not self.model_fields_set:
            raise ValueError("at least one field required")
        return self


class SubjectView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    description: str | None = None


class SubjectList(BaseModel):
    subjects: list[SubjectView]


class MaterialPurpose(StrEnum):
    """上传用途；两种用途不得混用（FR-005）。

    学习资料建立教学知识库；试卷只保留原文件与抽取事实，不默认进入教学检索。
    """

    STUDY_MATERIAL = "study_material"
    EXAM_PAPER = "exam_paper"


class MaterialUpload(BaseModel):
    """上传用途与可选描述；文件内容本身不经 JSON 传输。

    文件走专用 ``multipart/form-data`` 路由（受控流式读取），缺失的可选元数据
    不得猜测填充。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    purpose: MaterialPurpose
    #: 可选描述（FR-006.08）：识别不到就不写，不阻塞导入。
    year: int | None = Field(default=None, ge=1900, le=2100)
    publisher: str | None = Field(default=None, min_length=1, max_length=160)
    isbn: str | None = Field(default=None, min_length=10, max_length=20)
    exam_name: str | None = Field(default=None, min_length=1, max_length=160)
    #: 用户显式要求从第几页继续处理；不做内置自动续处理。
    start_page: int | None = Field(default=None, ge=1)

    @property
    def builds_teaching_index(self) -> bool:
        """只有学习资料进入教学知识库；试卷不默认成为教材 RAG。"""

        return self.purpose is MaterialPurpose.STUDY_MATERIAL

    def optional_metadata(self) -> dict[str, Any]:
        """只返回用户实际提供的描述事实，不补造默认值。"""

        values = {
            "year": self.year,
            "publisher": self.publisher,
            "isbn": self.isbn,
            "exam_name": self.exam_name,
        }
        return {key: value for key, value in values.items() if value is not None}


MaterialState = Literal["uploaded", "parsed", "indexed", "failed"]


class MaterialAssetView(BaseModel):
    """受管媒体的真实事实；不含公开 URL 或可猜测文件名。"""

    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    media_type: str
    anchor: str
    page: int | None = None
    stored: bool = False


class MaterialBlockView(BaseModel):
    """有序内容块：与 ``content_blocks.ContentBlock`` 同名的判别值。"""

    type: Literal["text", "image", "formula", "table", "original"]
    anchor: str
    page: int | None = None
    text: str | None = None
    rows: list[list[str]] = Field(default_factory=list)
    mathml: str | None = None
    expression: str | None = None
    sha256: str | None = None
    media_type: str | None = None
    placement: str | None = None
    reason: str | None = None


class MaterialView(BaseModel):
    """资料的真实解析/索引状态；不把半成品表达为已完成。"""

    id: UUID
    subject_id: UUID
    purpose: MaterialPurpose
    original_filename: str
    file_format: str
    state: MaterialState
    parse_state: Literal["parsed", "failed", "partial"]
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    optional_metadata: dict[str, Any] = Field(default_factory=dict)
    chunk_count: int = 0
    issues: list[str] = Field(default_factory=list)
    requires_ocr: bool = False
    blocks: list[MaterialBlockView] = Field(default_factory=list)
    assets: list[MaterialAssetView] = Field(default_factory=list)
    series_id: UUID | None = None
    version_id: UUID | None = None
    version_label: str | None = None
    default_version_id: UUID | None = None


class MaterialList(BaseModel):
    materials: list[MaterialView]


class KnowledgeChapterInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: UUID | None = None
    title: str = Field(min_length=1, max_length=160)
    parent_id: UUID | None = None
    knowledge_point_ids: list[UUID] = Field(default_factory=list)
    document_id: UUID | None = None
    anchors: list[str] = Field(default_factory=list)


class KnowledgePointInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: UUID | None = None
    name: str = Field(min_length=1, max_length=160)


class KnowledgeConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    knowledge_points: list[KnowledgePointInput] = Field(default_factory=list)
    chapters: list[KnowledgeChapterInput] = Field(default_factory=list)


class KnowledgeChapterView(BaseModel):
    id: UUID
    title: str
    parent_id: UUID | None = None
    confirmation_state: Literal["suggested", "confirmed"]
    knowledge_point_ids: list[UUID] = Field(default_factory=list)
    document_id: UUID | None = None
    anchors: list[str] = Field(default_factory=list)


class KnowledgePointView(BaseModel):
    id: UUID
    name: str
    confirmation_state: str


class KnowledgeView(BaseModel):
    subject_id: UUID
    confirmed: bool
    chapters: list[KnowledgeChapterView]
    knowledge_points: list[KnowledgePointView]


class MaterialVersionInput(BaseModel):
    """用户显式把一个资料加入版本系列；系统不自动推断同属一本教材。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    document_id: UUID
    series_id: UUID | None = None
    series_name: str | None = Field(default=None, min_length=1, max_length=160)
    label: str = Field(min_length=1, max_length=160)


class MaterialVersionView(BaseModel):
    id: UUID
    document_id: UUID
    label: str
    is_default: bool


class MaterialSeriesView(BaseModel):
    id: UUID
    subject_id: UUID
    name: str
    default_version_id: UUID | None = None
    versions: list[MaterialVersionView]


class MaterialSeriesList(BaseModel):
    series: list[MaterialSeriesView]


class DefaultVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version_id: UUID


class PersonalPaperCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: UUID


class PersonalPaperExtractionPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=160)

    retry_of_id: UUID | None = None
    acknowledge_unknown: StrictBool = False

class PersonalPaperAssetView(BaseModel):
    id: UUID
    sha256: str
    media_type: str
    source: SourceLocation


class PersonalPaperQuestionView(RevisionContent):
    id: UUID
    revision: int
    status: str
    question_id: UUID | None = None
    source_locations: list[SourceLocation]
    question_number: str | None = None
    reference_answer: str | None = None
    scoring_rubric: str | None = None
    answer_origin: Literal["paper", "user_correction", "ai_completion"] | None = None
    score: Decimal | None = None
    difficulty: str | None = None
    knowledge_point_ids: list[UUID] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    confirmed_asset_ids: list[UUID] = Field(default_factory=list)


class PersonalPaperView(BaseModel):
    id: UUID
    subject_id: UUID
    document_id: UUID
    status: str
    extraction_operation_id: UUID | None = None
    questions: list[PersonalPaperQuestionView]
    assets: list[PersonalPaperAssetView]
    error_code: str | None = None


class PersonalPaperEdit(RevisionContent):
    """Explicit full corrected content; answer provenance is set by the service."""
    expected_revision: int = Field(ge=1, strict=True)
    source_locations: list[SourceLocation] = Field(min_length=1)
    question_number: str | None = None
    reference_answer: str | None = None
    scoring_rubric: str | None = None
    score: Decimal | None = Field(default=None, gt=0, max_digits=8, decimal_places=2)
    difficulty: str | None = Field(default=None, min_length=1, max_length=32)
    knowledge_point_ids: list[UUID] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    confirmed_asset_ids: list[UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def actual_assets(self):
        if not set(self.confirmed_asset_ids) <= self.asset_ids():
            raise ValueError("Image confirmation must name actual content assets")
        if len(set(self.knowledge_point_ids)) != len(self.knowledge_point_ids):
            raise ValueError("Knowledge point IDs must be unique")
        return self


class PersonalQuestionView(RevisionContent):
    id: UUID
    subject_id: UUID
    revision_id: UUID
    revision: int
    canonical_id: UUID
    reference_answer: str | None = None
    scoring_rubric: str | None = None
    answer_origin: str | None = None
    source_kind: Literal["uploaded", "ai"]
    admission_state: str
    default_score: Decimal
    difficulty: str | None = None
    knowledge_point_ids: list[UUID]
    source_locations: list[SourceLocation]
    asset_sources: dict[str, SourceLocation] = Field(default_factory=dict)
    issues: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)


class PersonalQuestionList(BaseModel):
    questions: list[PersonalQuestionView]


class PersonalQuality(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer_basis: str = Field(min_length=1)
    assessment: SemanticMachineOutput
    chart_answer_points: dict[str, list[ChartPoint]] = Field(default_factory=dict)

    @field_validator("answer_basis")
    @classmethod
    def nonblank_basis(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("An actual answer derivation is required")
        return value


class PersonalAnswerResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference_answer: str = Field(min_length=1)
    scoring_rubric: str | None = None
    explanation_blocks: list[ContentBlock] = Field(default_factory=list)
    quality: PersonalQuality


class PersonalAnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1, strict=True)
    mode: Literal["complete", "review"]
    idempotency_key: str = Field(min_length=1, max_length=160)


class PersonalQuestionEdit(PersonalPaperEdit):
    source_locations: list[SourceLocation] = Field(default_factory=list)
