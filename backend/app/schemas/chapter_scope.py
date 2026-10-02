"""Teacher-owned chapter and chunk scope write inputs."""

from __future__ import annotations

from itertools import pairwise
from typing import Annotated, Any, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

PositiveOrder = Annotated[StrictInt, Field(gt=0)]


def normalize_knowledge_points(value: Any) -> list[str]:
    """Trim and stably deduplicate exact labels without rewriting their meaning."""
    if not isinstance(value, (list, tuple)):
        raise ValueError("Knowledge points must be a list of strings.")  # noqa: TRY004 -- Pydantic reports ValueError as validation failure.
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("Knowledge points must contain nonblank strings.")
        label = item.strip()
        if label not in normalized:
            normalized.append(label)
    return normalized


def _title(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Title must be a nonblank string.")
    return value.strip()


class ChapterSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_order: PositiveOrder
    title: str

    @field_validator("title", mode="before")
    @classmethod
    def normalize_title(cls, value: Any) -> str:
        return _title(value)


class ChapterWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(max_length=160)
    sections: list[ChapterSection] = Field(default_factory=list)

    @field_validator("title", mode="before")
    @classmethod
    def normalize_title(cls, value: Any) -> str:
        return _title(value)

    @model_validator(mode="after")
    def continuous_directory(self) -> Self:
        if [s.section_order for s in self.sections] != list(
            range(1, len(self.sections) + 1)
        ):
            raise ValueError("Section orders must be unique and continuous from 1.")
        return self


class ChunkScopeUpdate(BaseModel):
    """model_fields_set distinguishes untouched dimensions from explicit clearing."""

    model_config = ConfigDict(extra="forbid")
    chapter_id: UUID | None = None
    section_order: PositiveOrder | None = None
    knowledge_points: list[str] | None = None

    @field_validator("knowledge_points", mode="before")
    @classmethod
    def normalize_labels(cls, value: Any) -> list[str] | None:
        return None if value is None else normalize_knowledge_points(value)


class SourceSplit(BaseModel):
    """Internal cuts in one previewed cleaned section; endpoints are implicit."""

    model_config = ConfigDict(extra="forbid")
    section_index: PositiveOrder
    cut_points: list[StrictInt]

    @field_validator("cut_points")
    @classmethod
    def increasing_internal_cuts(cls, value: list[int]) -> list[int]:
        if any(point <= 0 for point in value) or any(
            a >= b for a, b in pairwise(value)
        ):
            raise ValueError("Cut points must be positive and strictly increasing.")
        return value
