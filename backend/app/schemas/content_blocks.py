"""Personal revision content; plain text is a projection, never a second stem."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.app.schemas.learning_charts import LearningChart


class ContentModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceLocation(ContentModel):
    document_id: UUID
    page: int | None = Field(default=None, gt=0, strict=True)
    anchor: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def located(self):
        if self.page is None and self.anchor is None:
            raise ValueError("An actual page or object anchor is required")
        return self


class MathNode(ContentModel):
    """Finite native MathML tree, with no markup, URLs, attributes or scripts."""

    kind: Literal["row", "identifier", "number", "operator", "text", "sup", "sub", "fraction", "sqrt"]
    text: str | None = None
    children: list["MathNode"] = Field(default_factory=list)

    @model_validator(mode="after")
    def shape(self):
        if self.kind in {"identifier", "number", "operator", "text"}:
            if not self.text or self.children:
                raise ValueError("Math leaves require text and no children")
        elif self.text is not None or not self.children:
            raise ValueError("Math containers require children and no text")
        elif self.kind in {"sup", "sub", "fraction"} and len(self.children) != 2:
            raise ValueError("Binary math node requires two operands")
        elif self.kind == "sqrt" and len(self.children) != 1:
            raise ValueError("Square root requires one operand")
        return self

    def projection(self) -> str:
        if self.text is not None:
            return self.text
        parts = [child.projection() for child in self.children]
        if self.kind in {"sup", "sub", "fraction"}:
            operator = {"sup": "^", "sub": "_", "fraction": "/"}[self.kind]
            return f"({parts[0]}){operator}({parts[1]})"
        return "sqrt(" + parts[0] + ")" if self.kind == "sqrt" else "".join(parts)


class TextBlock(ContentModel):
    type: Literal["text"]
    text: str = Field(min_length=1)


class ImageBlock(ContentModel):
    type: Literal["image"]
    asset_id: UUID
    alt: str = "题目图片"


class FormulaBlock(ContentModel):
    type: Literal["formula"]
    expression: MathNode


class TableBlock(ContentModel):
    type: Literal["table"]
    headers: list[str] = Field(min_length=1)
    rows: list[list[str]] = Field(min_length=1)

    @model_validator(mode="after")
    def rectangular(self):
        if any(len(row) != len(self.headers) for row in self.rows):
            raise ValueError("Table rows must match the columns")
        return self


class OriginalBlock(ContentModel):
    """Unreliable formula/table/object retains its managed original and location."""

    type: Literal["original"]
    asset_id: UUID
    reason: str = Field(min_length=1)
    source: SourceLocation


class ChartBlock(ContentModel):
    type: Literal["chart"]
    chart: LearningChart


ContentBlock = Annotated[
    TextBlock | ImageBlock | FormulaBlock | TableBlock | OriginalBlock | ChartBlock,
    Field(discriminator="type"),
]
PersonalQuestionType = Literal["single_choice", "multiple_choice", "short_answer"]


class ContentOption(ContentModel):
    id: str = Field(min_length=1)
    blocks: list[ContentBlock] = Field(min_length=1)


class RevisionContent(ContentModel):
    question_type: PersonalQuestionType
    stem_blocks: list[ContentBlock] = Field(min_length=1)
    options: list[ContentOption] = Field(default_factory=list)
    explanation_blocks: list[ContentBlock] = Field(default_factory=list)

    @model_validator(mode="after")
    def option_ids(self):
        if len({option.id for option in self.options}) != len(self.options):
            raise ValueError("Option IDs must be unique within a revision")
        if self.question_type == "short_answer":
            if self.options:
                raise ValueError("Text answers do not have selectable options")
        elif len(self.options) < 2:
            raise ValueError("Choice questions require multiple options")
        return self

    def blocks(self, *, include_explanation: bool = True) -> list[ContentBlock]:
        return self.stem_blocks + [b for o in self.options for b in o.blocks] + (
            self.explanation_blocks if include_explanation else []
        )

    def asset_ids(self, *, include_explanation: bool = True) -> set[UUID]:
        return {b.asset_id for b in self.blocks(include_explanation=include_explanation)
                if isinstance(b, (ImageBlock, OriginalBlock))}

    @property
    def needs_confirmation(self) -> bool:
        # Charts are conservatively pending until the later admission workflow
        # supplies and verifies answer basis. This shared reader cannot invent it.
        return any(isinstance(b, (OriginalBlock, ChartBlock)) for b in self.blocks())

    def text_projection(self, *, include_explanation: bool = False) -> str:
        def project(block: ContentBlock) -> str:
            if isinstance(block, TextBlock):
                return block.text
            if isinstance(block, FormulaBlock):
                return block.expression.projection()
            if isinstance(block, TableBlock):
                return "\n".join("\t".join(row) for row in [block.headers, *block.rows])
            if isinstance(block, OriginalBlock):
                return f"[待确认原内容:{block.asset_id}]"
            if isinstance(block, ChartBlock):
                chart = block.chart
                data = "; ".join(f"({p.x},{p.y})" for p in chart.points)
                return f"{chart.title}\n{chart.x_axis.label}({chart.x_axis.unit}),{chart.y_axis.label}({chart.y_axis.unit}): {data}\n依据:{chart.basis}"
            return f"[图片:{block.asset_id}]"
        return "\n".join(project(b) for b in self.blocks(include_explanation=include_explanation))


class QuestionContentView(ContentModel):
    question_id: UUID
    revision_id: UUID
    question_type: PersonalQuestionType
    phase: Literal["answering", "review"]
    stem_blocks: list[ContentBlock]
    options: list[ContentOption]
    explanation_blocks: list[ContentBlock] | None = None
    automatic_grading_ready: bool
    issues: list[str] = Field(default_factory=list)
    asset_sources: dict[str, SourceLocation] = Field(default_factory=dict)
