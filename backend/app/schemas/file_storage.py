"""Validated private file metadata and public resource projection."""
from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

MigrationStatus = Literal["not_required", "not_migrated", "migrated", "missing", "history_unknown", "failed"]


class FileErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1)
    stage: str = Field(min_length=1)


class MigrationAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID
    source_locator: str = Field(min_length=1, max_length=1024)
    target_relative_path: str = Field(min_length=1, max_length=1024)
    started_at: AwareDatetime
    verified_at: AwareDatetime | None = None
    committed_at: AwareDatetime | None = None
    error: FileErrorDetail | None = None

    @model_validator(mode="after")
    def validate_times(self):
        if self.verified_at is not None and self.verified_at < self.started_at:
            raise ValueError("verification precedes operation")
        if self.committed_at is not None and (
            self.verified_at is None or self.committed_at < self.verified_at
        ):
            raise ValueError("commit requires completed verification")
        return self


class MigrationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: MigrationStatus
    latest_attempt: MigrationAttempt | None = None

    @model_validator(mode="after")
    def validate_outcome(self):
        if self.status == "not_required" and self.latest_attempt is not None:
            raise ValueError("native files do not have migration attempts")
        if self.status == "migrated" and (
            self.latest_attempt is None
            or self.latest_attempt.committed_at is None
            or self.latest_attempt.error is not None
        ):
            raise ValueError("migration requires verified committed evidence")
        if self.status == "failed" and (
            self.latest_attempt is None or self.latest_attempt.error is None
        ):
            raise ValueError("failed migration requires actual error")
        return self


class FileMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    media_type: str | None = Field(default=None, min_length=1, max_length=255)
    size_bytes: int | None = Field(default=None, ge=0, strict=True)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    migration: MigrationRecord


class ManagedFileView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    file_id: str
    resource_type: Literal["document", "source_page", "staged_asset", "question_asset", "export"]
    resource_id: UUID
    course_id: UUID
    original_filename: str | None
    media_type: str | None
    size_bytes: int | None
    availability: Literal["available", "missing", "history_unknown"]
    migration_status: MigrationStatus


class OperationReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID
    resource_type: Literal["document", "source_page", "staged_asset", "question_asset", "export"]
    resource_id: UUID
    owner: dict[str, str]
    actor_id: UUID
    candidate_locator: str
    stage: Literal["prepared", "written", "committed", "failed"]
    started_at: AwareDatetime
    updated_at: AwareDatetime
    error: FileErrorDetail | None = None
