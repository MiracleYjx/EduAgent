"""Explicit teaching scope; course authorization is supplied by the business caller."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from backend.app.schemas.chapter_scope import normalize_knowledge_points


class SectionRange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    chapter_id: UUID
    start_order: Annotated[StrictInt, Field(gt=0)]
    end_order: Annotated[StrictInt, Field(gt=0)]

    @model_validator(mode="after")
    def ordered_range(self) -> SectionRange:
        if self.start_order > self.end_order:
            raise ValueError("小节范围起点不能大于终点。")
        return self


class RetrievalScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_ids: tuple[UUID, ...] = ()
    chapter_ids: tuple[UUID, ...] = ()
    section_range: SectionRange | None = None
    knowledge_points: tuple[str, ...] = ()

    @field_validator("knowledge_points", mode="before")
    @classmethod
    def normalize_labels(cls, value: Any) -> tuple[str, ...]:
        return tuple(normalize_knowledge_points(value))

    @property
    def is_empty(self) -> bool:
        return not (
            self.document_ids
            or self.chapter_ids
            or self.section_range
            or self.knowledge_points
        )


__all__ = ["RetrievalScope", "SectionRange"]
