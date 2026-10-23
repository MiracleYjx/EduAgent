"""Storage-root preparation is explicit, bounded and write-checked."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.services.settings.storage_preparation import (
    StoragePreparationError,
    prepare_storage_root,
)


def test_prepares_nested_root_and_keeps_bucket_parents_writable(tmp_path: Path) -> None:
    root = tmp_path / "custom" / "nested" / "storage"

    assert not root.exists()
    assert prepare_storage_root(root) == root
    assert root.is_dir()
    assert list(root.glob(".readiness-init-*")) == []

    # File writers create their bucket parents on demand; the prepared root must
    # support the same nested material/asset shape without pre-seeding data.
    assets = root / "materials" / "synthetic-material" / "assets"
    assets.mkdir(parents=True)
    assert assets.is_dir()


def test_existing_root_and_entries_are_preserved(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    marker = root / "existing-material.txt"
    marker.write_text("synthetic", encoding="utf-8")

    assert prepare_storage_root(root) == root
    assert marker.read_text(encoding="utf-8") == "synthetic"
    assert sorted(path.name for path in root.iterdir()) == [marker.name]


def test_file_at_root_is_rejected_without_leaking_os_detail(tmp_path: Path) -> None:
    target = tmp_path / "storage"
    target.write_text("not a directory", encoding="utf-8")

    with pytest.raises(StoragePreparationError) as error:
        prepare_storage_root(target)
    assert error.value.code == "STORAGE_ROOT_UNAVAILABLE"
    assert "not a directory" not in str(error.value)


def test_relative_root_is_rejected() -> None:
    with pytest.raises(StoragePreparationError) as error:
        prepare_storage_root(Path("relative") / "storage")
    assert error.value.code == "STORAGE_ROOT_UNVERIFIED"
