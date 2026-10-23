"""Prepare the selected storage root only after explicit initialization.

Read-only setup probes and runtime path preparation intentionally do not create
an empty replacement storage root. File writers create their own bucket parents
(for example ``materials/<id>/assets``) on demand; this helper creates and
verifies only the environment-owned root itself.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4


class StoragePreparationError(RuntimeError):
    """A safe, stable failure while preparing the initialized storage root."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def prepare_storage_root(root: Path) -> Path:
    """Create and write-check the selected storage root without following links."""

    path = Path(root)
    try:
        if not path.is_absolute():
            raise StoragePreparationError("STORAGE_ROOT_UNVERIFIED")
        path.mkdir(parents=True, exist_ok=True)
        # Reject a junction/symlink or a non-canonical spelling before any probe
        # is written. The selected environment path must stay exactly as declared.
        if path.resolve(strict=True) != path or not path.is_dir():
            raise StoragePreparationError("STORAGE_ROOT_UNVERIFIED")
        probe = path / f".readiness-init-{uuid4().hex}.probe"
        try:
            with probe.open("xb") as stream:
                stream.write(b"ok")
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass
    except StoragePreparationError:
        raise
    except (OSError, ValueError):
        raise StoragePreparationError("STORAGE_ROOT_UNAVAILABLE") from None
    return path


__all__ = ["StoragePreparationError", "prepare_storage_root"]
