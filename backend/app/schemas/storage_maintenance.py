"""Private maintenance reports; these are not public file authorization grants."""
from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict

from backend.app.schemas.file_storage import FileErrorDetail


class MigrationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_ids: list[str]
    source_locator: str | None
    target_relative_path: str | None = None
    operation_id: UUID | None = None
    outcome: Literal["migrated", "already_migrated", "native", "missing", "history_unknown", "failed"]
    error: FileErrorDetail | None = None


class MigrationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    started_at: AwareDatetime
    completed_at: AwareDatetime
    items: list[MigrationItem]

    @property
    def counts(self) -> dict[str, int]:
        counts = dict.fromkeys(("migrated", "already_migrated", "native", "missing", "history_unknown", "failed"), 0)
        for item in self.items:
            counts[item.outcome] += len(item.file_ids)
        return counts

# BackupSet v1 is a disk manifest, never a new database table.
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated

from pydantic import AfterValidator, Field, model_validator

from backend.app.schemas.file_storage import MigrationStatus


def canonical_relative(value: str) -> str:
    if (not value or len(value) > 1024 or "\\" in value or PureWindowsPath(value).drive
            or PurePosixPath(value).is_absolute() or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise ValueError("noncanonical relative path")
    return value


RelativePath = Annotated[str, AfterValidator(canonical_relative)]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class BackupIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1)
    file_id: str | None = None
    relative_path: RelativePath | None = None
    stage: str = Field(min_length=1)
    message: str = Field(min_length=1)


class BackupFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    relative_path: RelativePath
    size_bytes: int = Field(ge=0, strict=True)
    sha256: Digest


class DatabaseDump(BackupFile):
    relative_path: Literal["database.dump"] = "database.dump"
    schema_revision: str | None


class BackupReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_id: str
    resource_type: Literal["document", "export"]
    resource_id: UUID
    owner: dict[str, str] = Field(min_length=1)
    relative_path: RelativePath | None
    availability: Literal["available", "missing", "history_unknown"]
    migration_status: MigrationStatus

    @model_validator(mode="after")
    def identity_matches(self):
        prefix = "d_" if self.resource_type == "document" else "e_"
        if self.file_id != prefix + self.resource_id.hex:
            raise ValueError("file identity does not match resource")
        if self.relative_path is not None and self.relative_path.split("/")[0] not in {"uploads", "papers", "assets", "exports"}:
            raise ValueError("reference outside managed buckets")
        return self


class BackupReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    relative_path: RelativePath
    operation_id: UUID
    stage: Literal["prepared", "written", "committed", "failed"]


class BackupManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manifest_version: Literal[1] = 1
    backup_set_id: UUID
    outcome: Literal["creating", "complete", "incomplete", "failed"]
    started_at: AwareDatetime
    window_started_at: AwareDatetime | None = None
    window_finished_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    database: DatabaseDump | None = None
    references: list[BackupReference]
    files: list[BackupFile]
    operation_receipts: list[BackupReceipt]
    issues: list[BackupIssue]

    @model_validator(mode="after")
    def validate_evidence(self):
        times = [time for time in (self.started_at, self.window_started_at, self.window_finished_at, self.completed_at) if time is not None]
        if times != sorted(times):
            raise ValueError("invalid actual stage times")
        paths = [entry.relative_path for entry in self.files]
        ids = [ref.file_id for ref in self.references]
        if len(paths) != len(set(paths)) or len(ids) != len(set(ids)):
            raise ValueError("duplicate physical paths or identities")
        for path in paths:
            if path.split("/")[0] not in {"uploads", "papers", "assets", "exports"}:
                raise ValueError("file outside managed buckets")
        if self.outcome in {"complete", "incomplete"} and (self.database is None or self.window_started_at is None
                or self.window_finished_at is None or self.completed_at is None):
            raise ValueError("finished backup requires actual dump and stage evidence")
        if self.outcome == "complete":
            if self.issues or any(ref.availability != "available" or ref.relative_path not in paths for ref in self.references):
                raise ValueError("complete requires all references covered and no issues")
            if any(receipt.relative_path not in paths or receipt.stage in {"prepared", "written"} for receipt in self.operation_receipts):
                raise ValueError("complete requires accounted and finished receipts")
        return self


class RestoreReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    restore_id: UUID
    backup_set_id: UUID
    started_at: AwareDatetime
    completed_at: AwareDatetime
    outcome: Literal["verified", "failed"]
    issues: list[BackupIssue]

    @model_validator(mode="after")
    def valid_outcome(self):
        if self.completed_at < self.started_at or (self.outcome == "verified" and self.issues):
            raise ValueError("invalid restore evidence")
        return self
