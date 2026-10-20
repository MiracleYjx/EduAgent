"""Environment-bound, immutable Windows credential revisions, outside business DB.

Only ciphertext is written for secrets. put creates a candidate; it never changes
configuration, active/pending pointers or an older version. T210 supplies the real
configuration/snapshot reference index; absence of that index forbids deletion.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import sys
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Protocol, Self
from uuid import UUID, uuid4

from pydantic import SecretStr

from backend.app.schemas.settings import (
    CredentialReference,
    CredentialState,
    CredentialStatus,
)
from backend.app.services.settings.windows_credentials import (
    WindowsDataProtector,
    WindowsFileSecurity,
)

_PURPOSES = {"provider", "database", "redis", "jwt"}
_MAX_RECORD = 16 * 1024 * 1024
_TERMINAL_STATES = {"invalid", "revoked"}


class CredentialProtector(Protocol):
    def protect(self, plaintext: bytes) -> bytes: ...
    def unprotect(self, ciphertext: bytes) -> bytes: ...


class CredentialFileSecurity(Protocol):
    def secure(self, path: Path, *, directory: bool) -> None: ...
    def verify(self, path: Path, *, directory: bool) -> None: ...


class CredentialReferenceIndex(Protocol):
    def references(self, reference: CredentialReference, version: UUID) -> Sequence[str]: ...


class CredentialStoreError(RuntimeError):
    """Safe service boundary: raw OS/protector/index errors never escape."""

    def __init__(self, code: str, *, stage: str):
        super().__init__(code)
        self.code = code
        self.stage = stage
        self.status_code = 409 if code in {"IN_USE", "CREDENTIAL_WRITER_LOCKED"} else (
            404 if code == "CREDENTIAL_ABSENT" else 422
        )


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


class CredentialStore:
    def __init__(
        self, root: Path, environment_id: UUID, *, protector: CredentialProtector | None = None,
        reference_index: CredentialReferenceIndex | None = None,
        file_security: CredentialFileSecurity | None = None,
        mutex: Any = None,
    ) -> None:
        if not isinstance(root, Path) or not root.is_absolute() or root != Path(os.path.abspath(root)):
            raise CredentialStoreError("ABSOLUTE_CREDENTIAL_ROOT_REQUIRED", stage="root")
        if not isinstance(environment_id, UUID) or not environment_id.int:
            raise CredentialStoreError("INVALID_ENVIRONMENT_ID", stage="root")
        self.root = root
        self.environment_id = environment_id
        self.credentials = root / "credentials"
        # T210 publication and deletion share one outermost lock supplied by the
        # configuration service; the default keeps this store standalone.
        self._mutex = mutex if mutex is not None else threading.RLock()
        self._lock_file: BinaryIO | None = None
        self._closed = False
        self._index = reference_index
        try:
            self._protector = protector if protector is not None else WindowsDataProtector()
            self._security = file_security if file_security is not None else WindowsFileSecurity()
            self._guard_path(self.root)
            self.root.mkdir(parents=True, exist_ok=True)
            directory = self._guard_path(self.credentials)
            if directory.exists():
                self._verify(directory, directory=True)
            else:
                directory.mkdir()
                self._secure(directory, directory=True)
            self._acquire_writer()
            self._environment_binding(create=True)
        except CredentialStoreError:
            self.close()
            raise
        except (OSError, ValueError, RuntimeError, TypeError):
            self.close()
            raise CredentialStoreError("CREDENTIAL_BACKEND_UNAVAILABLE", stage="open") from None

    def __enter__(self) -> Self:
        self._require_open()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def _guard_path(self, path: Path) -> Path:
        try:
            if not path.is_relative_to(self.root) or path.resolve() != path:
                raise CredentialStoreError("CREDENTIAL_PATH_UNSAFE", stage="path")
            # Reject symlinks/junctions at any existing component, including root
            # ancestors. Hardlinks must not allow ACL/write/delete to affect an
            # independently named file outside this vault.
            for component in (path, *path.parents):
                try:
                    info = component.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise CredentialStoreError("CREDENTIAL_PATH_UNSAFE", stage="path")
                if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                    raise CredentialStoreError("CREDENTIAL_PATH_UNSAFE", stage="path")
        except CredentialStoreError:
            raise
        except (OSError, ValueError, RuntimeError, TypeError):
            raise CredentialStoreError("CREDENTIAL_PATH_UNSAFE", stage="path") from None
        return path

    def _secure(self, path: Path, *, directory: bool) -> None:
        try:
            self._security.secure(self._guard_path(path), directory=directory)
        except (OSError, ValueError, RuntimeError, TypeError):
            raise CredentialStoreError("CREDENTIAL_ACL_UNAVAILABLE", stage="acl_set") from None

    def _verify(self, path: Path, *, directory: bool) -> None:
        try:
            self._security.verify(self._guard_path(path), directory=directory)
        except (OSError, ValueError, RuntimeError, TypeError):
            raise CredentialStoreError("CREDENTIAL_ACL_UNAVAILABLE", stage="acl_verify") from None

    def _acquire_writer(self) -> None:
        path = self._guard_path(self.credentials / "writer.lock")
        existed = path.exists()
        if existed:
            self._verify(path, directory=False)
        handle = path.open("a+b")
        try:
            if not existed:
                self._secure(path, directory=False)
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except CredentialStoreError:
            handle.close()
            raise
        except (OSError, ValueError, RuntimeError, TypeError):
            handle.close()
            raise CredentialStoreError("CREDENTIAL_WRITER_LOCKED", stage="writer_lock") from None
        self._lock_file = handle

    def _state_path(self, reference: CredentialReference, version: UUID) -> Path:
        return self._guard_path(self.credentials / f"state.{reference.credential_id}.{version}.json")

    def _state(self, reference: CredentialReference, version: UUID) -> CredentialState | None:
        """Read the private invalid/revoked marker; a damaged marker stays unavailable."""
        path = self._state_path(reference, version)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_bytes())
            if (not isinstance(data, dict) or set(data) != {
                "schema_version", "credential_id", "version", "purpose", "environment_id", "state", "marked_at", "integrity",
            } or type(data["schema_version"]) is not int or data["schema_version"] != 1):
                raise ValueError
            integrity = data.pop("integrity")
            if (not isinstance(integrity, str)
                    or not hmac.compare_digest(integrity, hashlib.sha256(_json_bytes(data)).hexdigest())):
                raise ValueError
            if (data["credential_id"] != str(reference.credential_id) or data["version"] != str(version)
                    or data["purpose"] != reference.purpose or data["environment_id"] != str(self.environment_id)
                    or data["state"] not in _TERMINAL_STATES or not isinstance(data["marked_at"], str)):
                raise ValueError
            return data["state"]
        except (OSError, ValueError, TypeError, KeyError):
            raise CredentialStoreError("CREDENTIAL_UNAVAILABLE", stage="state") from None

    def _mark(self, reference: CredentialReference, version: UUID, state: str) -> None:
        """Record an explicit terminal state beside the untouched ciphertext."""
        with self._mutex:
            path = self._reference(reference, version)
            self._ready()
            if not self._guard_path(path).is_file():
                raise CredentialStoreError("CREDENTIAL_ABSENT", stage="state")
            current = self._state(reference, version)
            if current == "revoked" and state != "revoked":
                raise CredentialStoreError("CREDENTIAL_STATE_CONFLICT", stage="state")
            target = self._state_path(reference, version)
            payload = {
                "schema_version": 1, "credential_id": str(reference.credential_id), "version": str(version),
                "purpose": reference.purpose, "environment_id": str(self.environment_id), "state": state,
                "marked_at": datetime.now(UTC).isoformat(),
            }
            payload["integrity"] = hashlib.sha256(_json_bytes(payload)).hexdigest()
            temporary = self._guard_path(target.with_name(f".{target.name}.{uuid4().hex}.tmp"))
            try:
                with temporary.open("xb") as handle:
                    self._secure(temporary, directory=False)
                    handle.write(_json_bytes(payload))
                    handle.flush()
                    os.fsync(handle.fileno())
                self._guard_path(temporary)
                self._guard_path(target)
                os.replace(temporary, target)
            except CredentialStoreError:
                raise
            except (OSError, ValueError, RuntimeError, TypeError):
                raise CredentialStoreError("CREDENTIAL_WRITE_FAILED", stage="state") from None

    def mark_invalid(self, reference: CredentialReference, version: UUID) -> None:
        self._mark(reference, version, "invalid")

    def revoke(self, reference: CredentialReference, version: UUID) -> None:
        self._mark(reference, version, "revoked")

    def close(self) -> None:
        with self._mutex:
            handle, self._lock_file = self._lock_file, None
            self._closed = True
            if handle is not None:
                try:
                    handle.seek(0)
                    if sys.platform == "win32":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except (OSError, ValueError):
                    raise CredentialStoreError("CREDENTIAL_RELEASE_FAILED", stage="close") from None
                finally:
                    handle.close()

    def _require_open(self) -> None:
        if self._closed or self._lock_file is None:
            raise CredentialStoreError("CREDENTIAL_STORE_CLOSED", stage="access")

    def _atomic_write(self, destination: Path, content: bytes) -> None:
        self._guard_path(destination)
        if destination.exists():
            raise CredentialStoreError("IMMUTABLE_CREDENTIAL_EXISTS", stage="write")
        temporary = self._guard_path(destination.with_name(f".{destination.name}.{uuid4().hex}.tmp"))
        try:
            with temporary.open("xb") as handle:
                # The DACL is protected before even ciphertext is written.
                self._secure(temporary, directory=False)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            self._guard_path(destination)
            self._guard_path(temporary)
            if destination.exists():
                raise CredentialStoreError("IMMUTABLE_CREDENTIAL_EXISTS", stage="write")
            # Same-directory rename is the only commit point. Failed candidates
            # stay private as evidence; no automatic GC and no old-version write.
            os.replace(temporary, destination)
        except CredentialStoreError:
            raise
        except (OSError, ValueError, RuntimeError, TypeError):
            raise CredentialStoreError("CREDENTIAL_WRITE_FAILED", stage="write") from None

    def _environment_binding(self, *, create: bool = False) -> None:
        path = self._guard_path(self.credentials / "environment.json")
        if not path.exists():
            if not create or any(item.name != "writer.lock" for item in self.credentials.iterdir()):
                raise CredentialStoreError("CREDENTIAL_ENVIRONMENT_MISSING", stage="environment")
            self._atomic_write(path, _json_bytes({"schema_version": 1, "environment_id": str(self.environment_id)}))
        try:
            self._verify(path, directory=False)
            value = json.loads(path.read_bytes())
            if not isinstance(value, dict) or set(value) != {"schema_version", "environment_id"} or type(value["schema_version"]) is not int or value["schema_version"] != 1:
                raise CredentialStoreError("CREDENTIAL_ENVIRONMENT_CORRUPT", stage="environment")
            if value["environment_id"] != str(self.environment_id):
                raise CredentialStoreError("ENVIRONMENT_MISMATCH", stage="environment")
        except CredentialStoreError:
            raise
        except (OSError, ValueError, RuntimeError, TypeError):
            raise CredentialStoreError("CREDENTIAL_ENVIRONMENT_CORRUPT", stage="environment") from None

    def _ready(self) -> None:
        self._require_open()
        self._verify(self.credentials, directory=True)
        # Reads must never repair/create a missing environment binding.
        if not self._guard_path(self.credentials / "environment.json").is_file():
            raise CredentialStoreError("CREDENTIAL_ENVIRONMENT_MISSING", stage="environment")
        self._environment_binding()

    def _reference(self, reference: CredentialReference, version: UUID) -> Path:
        if not isinstance(reference, CredentialReference):
            raise CredentialStoreError("INVALID_CREDENTIAL_REFERENCE", stage="reference")
        if reference.environment_id != self.environment_id:
            raise CredentialStoreError("ENVIRONMENT_MISMATCH", stage="reference")
        if (reference.backend != "dpapi" or not isinstance(reference.purpose, str)
                or reference.purpose not in _PURPOSES or reference.external_secret_id is not None
                or not isinstance(reference.credential_id, UUID) or not reference.credential_id.int
                or not isinstance(reference.version, UUID) or not reference.version.int
                or not isinstance(version, UUID) or version != reference.version):
            raise CredentialStoreError("INVALID_CREDENTIAL_REFERENCE", stage="reference")
        return self.credentials / f"{reference.credential_id}.{reference.purpose}.{version}.dpapi"

    def put(self, *, purpose: str, secret_value: SecretStr, credential_id: UUID | None = None) -> CredentialReference:
        with self._mutex:
            self._ready()
            if (not isinstance(purpose, str) or purpose not in _PURPOSES or not isinstance(secret_value, SecretStr)
                    or not secret_value.get_secret_value().strip()
                    or (credential_id is not None and (not isinstance(credential_id, UUID) or not credential_id.int))):
                raise CredentialStoreError("INVALID_SECRET_WRITE", stage="input")
            reference = CredentialReference(credential_id=credential_id or uuid4(), version=uuid4(),
                                             environment_id=self.environment_id, backend="dpapi", purpose=purpose)
            payload = {"schema_version": 1, "reference": reference.model_dump(mode="json"),
                       "created_at": datetime.now(UTC).isoformat(), "secret_value": secret_value.get_secret_value()}
            try:
                plaintext = _json_bytes({"payload": payload, "integrity": hashlib.sha256(_json_bytes(payload)).hexdigest()})
                if len(plaintext) > _MAX_RECORD:
                    raise CredentialStoreError("INVALID_SECRET_WRITE", stage="input")
                ciphertext = self._protector.protect(plaintext)
                if not isinstance(ciphertext, bytes) or not 0 < len(ciphertext) <= _MAX_RECORD:
                    raise CredentialStoreError("CREDENTIAL_PROTECTION_FAILED", stage="protect")
            except CredentialStoreError:
                raise
            except (OSError, ValueError, RuntimeError, TypeError):
                raise CredentialStoreError("CREDENTIAL_PROTECTION_FAILED", stage="protect") from None
            finally:
                # No secret cache; this does not claim to zero immutable Python objects.
                payload.clear()
            self._atomic_write(self._reference(reference, reference.version), ciphertext)
            return reference

    def _read_secret(self, reference: CredentialReference, path: Path) -> SecretStr:
        self._ready()
        if self._state(reference, reference.version) is not None:
            raise CredentialStoreError("CREDENTIAL_UNAVAILABLE", stage="state")
        path = self._guard_path(path)
        try:
            info = path.stat()
        except FileNotFoundError:
            raise CredentialStoreError("CREDENTIAL_ABSENT", stage="read") from None
        except (OSError, ValueError):
            raise CredentialStoreError("CREDENTIAL_UNAVAILABLE", stage="read") from None
        self._verify(path, directory=False)
        try:
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= _MAX_RECORD:
                raise CredentialStoreError("CREDENTIAL_REENTRY_REQUIRED", stage="record")
            ciphertext = path.read_bytes()
        except CredentialStoreError:
            raise
        except (OSError, ValueError):
            raise CredentialStoreError("CREDENTIAL_UNAVAILABLE", stage="read") from None
        try:
            plaintext = self._protector.unprotect(ciphertext)
            record = json.loads(plaintext)
            if not isinstance(record, dict) or set(record) != {"payload", "integrity"}:
                raise ValueError
            payload = record["payload"]
            if (not isinstance(payload, dict) or set(payload) != {"schema_version", "reference", "created_at", "secret_value"}
                    or type(payload["schema_version"]) is not int or payload["schema_version"] != 1
                    or not isinstance(record["integrity"], str)
                    or not hmac.compare_digest(record["integrity"], hashlib.sha256(_json_bytes(payload)).hexdigest())
                    or CredentialReference.model_validate(payload["reference"]) != reference
                    or not isinstance(payload["secret_value"], str) or not payload["secret_value"].strip()):
                raise ValueError
            return SecretStr(payload["secret_value"])
        except (OSError, ValueError, RuntimeError, TypeError, KeyError):
            # Corruption, missing user profile and cross-user DPAPI failures all
            # require re-entry. Never read .env or rewrite/delete the old blob.
            raise CredentialStoreError("CREDENTIAL_REENTRY_REQUIRED", stage="unprotect") from None

    def get(self, reference: CredentialReference, version: UUID) -> SecretStr:
        with self._mutex:
            return self._read_secret(reference, self._reference(reference, version))

    def status(self, reference: CredentialReference, version: UUID) -> CredentialStatus:
        with self._mutex:
            path = self._reference(reference, version)  # invalid/cross-env references are rejected, not projected
            try:
                marked: CredentialState | None = self._state(reference, version)
            except CredentialStoreError:
                marked = "unavailable"
            if marked is not None:
                return CredentialStatus(credential_ref=reference.credential_id, version=reference.version, status=marked)
            state: CredentialState = "available"
            try:
                self._read_secret(reference, path)
            except CredentialStoreError as error:
                if error.code == "CREDENTIAL_ABSENT":
                    state = "absent"
                elif error.code == "CREDENTIAL_REENTRY_REQUIRED":
                    state = "reentry_required"
                else:
                    state = "unavailable"
            return CredentialStatus(credential_ref=reference.credential_id, version=reference.version, status=state)

    def delete(self, reference: CredentialReference, version: UUID) -> None:
        with self._mutex:
            path = self._reference(reference, version)
            self._ready()
            if self._index is None:
                raise CredentialStoreError("REFERENCE_INDEX_UNAVAILABLE", stage="references")
            try:
                facts = self._index.references(reference, version)
                if not isinstance(facts, (tuple, list)) or any(not isinstance(fact, str) for fact in facts):
                    raise ValueError
                if any(fact != "terminal_history" for fact in facts):
                    raise CredentialStoreError("IN_USE", stage="references")
            except CredentialStoreError:
                raise
            except (OSError, ValueError, RuntimeError, TypeError):
                raise CredentialStoreError("REFERENCE_INDEX_UNAVAILABLE", stage="references") from None
            path = self._guard_path(path)
            if not path.is_file():
                raise CredentialStoreError("CREDENTIAL_ABSENT", stage="delete")
            self._verify(path, directory=False)
            try:
                path.unlink()  # only successful physical deletion changes availability
            except (OSError, ValueError):
                raise CredentialStoreError("CREDENTIAL_DELETE_FAILED", stage="delete") from None
            marker = self._state_path(reference, version)
            if marker.exists():
                try:
                    marker.unlink()
                except OSError:
                    pass
