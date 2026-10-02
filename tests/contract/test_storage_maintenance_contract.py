"""T151 private schemas, CLI and real file write boundary. TCR §9."""
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.app.services.file_storage_service import (
    FileStorageError,
    FileStorageService,
)
from tests.unit.services.test_file_storage_service import (
    owned_document as _owned_document,
)

owned_document = _owned_document
ROOT = Path(__file__).resolve().parents[2]


def test_cli_help_does_not_require_token_or_dump():
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    environment.pop("EDUAGENT_MAINTENANCE_TOKEN", None)
    for script in ("migrate_storage_paths.py", "backup_restore.py"):
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / script), "--help"], env=environment, capture_output=True, text=True, encoding="utf-8", check=False)
        assert result.returncode == 0
        assert "--token-env" in result.stdout


def manifest(outcome="creating"):
    return {"manifest_version": 1, "backup_set_id": str(uuid4()), "outcome": outcome,
            "started_at": datetime.now(UTC), "window_started_at": None, "window_finished_at": None,
            "completed_at": None, "database": None, "references": [], "files": [],
            "operation_receipts": [], "issues": []}


def test_complete_requires_real_database_times_and_coverage():
    from backend.app.schemas.storage_maintenance import BackupManifest
    assert BackupManifest.model_validate(manifest()).outcome == "creating"
    with pytest.raises(ValidationError):
        BackupManifest.model_validate(manifest("complete"))
    value = manifest("complete")
    now = datetime.now(UTC)
    value.update(window_started_at=now, window_finished_at=now, completed_at=now,
                 database={"relative_path": "database.dump", "size_bytes": 1, "sha256": "a" * 64, "schema_revision": None})
    value["references"] = [{"file_id": "d_" + uuid4().hex, "resource_type": "document",
                            "resource_id": str(uuid4()), "owner": {"course_id": str(uuid4())},
                            "relative_path": "uploads/missing.txt", "availability": "available", "migration_status": "migrated"}]
    with pytest.raises(ValidationError):
        BackupManifest.model_validate(value)


@pytest.mark.parametrize("path", ["../outside", "/absolute", "C:/outside", "uploads/../secret", "uploads\\secret"])
def test_manifest_rejects_noncanonical_paths(path):
    from backend.app.schemas.storage_maintenance import BackupManifest
    value = manifest()
    value["files"] = [{"relative_path": path, "size_bytes": 1, "sha256": "a" * 64}]
    with pytest.raises(ValidationError):
        BackupManifest.model_validate(value)


def test_maintenance_marker_blocks_bytes_link_export_and_cleanup(owned_document):
    session, doc, teacher, root = owned_document
    root.mkdir()
    (root / ".maintenance.json").write_text('{"operation_id":"existing-window"}', encoding="utf-8")
    service = FileStorageService(session, root=root)
    with pytest.raises(FileStorageError) as err:
        service.store_document(doc, b"new data", actor_id=teacher.id)
    assert err.value.code == "STORAGE_MAINTENANCE"
    with pytest.raises(FileStorageError):
        service.create_export(filename="result.csv", content=b"x", actor_id=teacher.id, audience="teacher_only", course_id=doc.course_id)
    with pytest.raises(FileStorageError):
        service.delete_unreferenced_bytes("uploads/no-such-file.txt")
    assert list(root.iterdir()) == [root / ".maintenance.json"]
