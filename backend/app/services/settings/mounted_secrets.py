"""Read-only, server-allowlisted Docker secrets with immutable version identity.

The constructor is a trusted bootstrap registration boundary, never an HTTP
binding request. Only new IDs are enrolled; unavailable initial versions stay
unproven. Private integrity metadata is not ordinary configuration or a backup.
get/status never enroll, repair metadata, normalize values or fall back to env.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Self
from uuid import UUID, uuid4

from pydantic import SecretStr

from backend.app.schemas.settings import CredentialReference, CredentialStatus
from backend.app.services.settings.credential_store import CredentialFileSecurity
from backend.app.services.settings.windows_credentials import WindowsFileSecurity

_PURPOSES = {"provider", "database", "redis", "jwt"}
_SECRET_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z", re.ASCII)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_MAX_SECRET = 1024 * 1024
_MAX_INDEX = 4 * 1024 * 1024
_IO_ERRORS = (OSError, ValueError, RuntimeError, TypeError)


class MountedSecretError(RuntimeError):
    """Stable, safe errors: no OS text, input path, secret or integrity value."""

    def __init__(self, code: str, *, stage: str):
        message = ("凭据由外部 Secret 管理，请由宿主操作者挂载新名称和版本。" if code == "EXTERNALLY_MANAGED"
                   else "挂载凭据不可用，请检查许可引用、原版本挂载及私有凭据元数据。")
        super().__init__(f"{code}：{message}")
        self.code = code
        self.stage = stage
        self.status_code = (405 if code == "EXTERNALLY_MANAGED" else 409 if code in {
            "SECRET_BINDING_IMMUTABLE", "CREDENTIAL_WRITER_LOCKED", "ENVIRONMENT_MISMATCH",
        } else 403 if code == "SECRET_REFERENCE_NOT_ALLOWED" else 503 if code in {
            "CREDENTIAL_UNAVAILABLE", "MOUNTED_METADATA_UNAVAILABLE",
        } else 422)


class _PosixFileSecurity:
    """Only the current uid may access integrity metadata; never applied to mounts."""

    def secure(self, path: Path, *, directory: bool) -> None:
        os.chmod(path, 0o700 if directory else 0o600)
        self.verify(path, directory=directory)

    def verify(self, path: Path, *, directory: bool) -> None:
        info = path.lstat()
        get_uid = getattr(os, "geteuid", None)
        if (not callable(get_uid) or info.st_uid != get_uid() or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
                or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
            raise MountedSecretError("MOUNTED_METADATA_UNAVAILABLE", stage="permissions")


@dataclass(frozen=True, repr=False)
class _Binding:
    reference: CredentialReference
    path: Path
    integrity: str | None = None


class MountedSecretStore:
    def __init__(
        self, root: Path, environment_id: UUID, *,
        mounts: Mapping[str, tuple[CredentialReference, Path]],
        file_security: CredentialFileSecurity | None = None,
    ) -> None:
        self._absolute(root)
        if root.parent == root or not isinstance(environment_id, UUID) or not environment_id.int:
            raise MountedSecretError("INVALID_MOUNTED_ROOT", stage="root")
        self.root = root
        self.environment_id = environment_id
        self.credentials = root / "credentials"
        self.metadata_root = self.credentials / "mounted"
        self._index_path = self.metadata_root / "index.json"
        self._environment_path = self.credentials / "mounted-environment.json"
        self._mutex = threading.RLock()
        self._lock_file: BinaryIO | None = None
        self._closed = False
        # Validate every authorization fact before opening/creating any files.
        self._mounts = self._validate_mounts(mounts)
        try:
            self._security = file_security if file_security is not None else (
                WindowsFileSecurity() if sys.platform == "win32" else _PosixFileSecurity())
            self._guard_path(self.root)
            self.root.mkdir(parents=True, exist_ok=True)
            self._private_directory(self.credentials)
            # Only an empty, trusted bootstrap vault may create initial facts.
            # A lost mounted directory in an existing vault is never fresh.
            fresh = not any(self.credentials.iterdir())
            self._acquire_writer()
            if fresh:
                self._write_json(self._environment_path, {
                    "schema_version": 1, "environment_id": str(self.environment_id),
                }, immutable=True)
                self._private_directory(self.metadata_root)
            else:
                self._environment_binding()
                self._guard_path(self.metadata_root)
                if not self.metadata_root.is_dir():
                    raise MountedSecretError("MOUNTED_METADATA_UNAVAILABLE", stage="metadata")
                self._security.verify(self.metadata_root, directory=True)
            self._register(fresh=fresh)
        except MountedSecretError:
            self.close()
            raise
        except _IO_ERRORS:
            self.close()
            raise MountedSecretError("MOUNTED_METADATA_UNAVAILABLE", stage="open") from None

    def __enter__(self) -> Self:
        self._require_open()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    @staticmethod
    def _absolute(path: Path) -> None:
        if not isinstance(path, Path) or not path.is_absolute() or path != Path(os.path.abspath(path)):
            raise MountedSecretError("MOUNTED_PATH_UNSAFE", stage="path")

    @staticmethod
    def _valid_id(secret_id: object) -> bool:
        return isinstance(secret_id, str) and _SECRET_ID.fullmatch(secret_id) is not None

    def _valid_reference(self, reference: object) -> bool:
        return (isinstance(reference, CredentialReference) and reference.backend == "mounted_secret"
                and isinstance(reference.environment_id, UUID) and reference.environment_id == self.environment_id
                and isinstance(reference.credential_id, UUID) and bool(reference.credential_id.int)
                and isinstance(reference.version, UUID) and bool(reference.version.int)
                and isinstance(reference.purpose, str) and reference.purpose in _PURPOSES
                and self._valid_id(reference.external_secret_id))

    def _validate_mounts(self, mounts: Mapping[str, tuple[CredentialReference, Path]]) -> dict[str, _Binding]:
        if not isinstance(mounts, Mapping):
            raise MountedSecretError("INVALID_MOUNTED_MAPPING", stage="mapping")
        result: dict[str, _Binding] = {}
        paths: set[Path] = set()
        identities: set[tuple[UUID, UUID]] = set()
        for secret_id, item in mounts.items():
            if (not self._valid_id(secret_id) or not isinstance(item, tuple) or len(item) != 2
                    or not self._valid_reference(item[0]) or item[0].external_secret_id != secret_id):
                raise MountedSecretError("INVALID_MOUNTED_MAPPING", stage="mapping")
            reference, path = item
            self._absolute(path)
            identity = (reference.credential_id, reference.version)
            if path in paths or identity in identities:
                raise MountedSecretError("SECRET_BINDING_IMMUTABLE", stage="mapping")
            paths.add(path)
            identities.add(identity)
            # Copy the trusted facts, not the caller's mutable mapping.
            result[secret_id] = _Binding(reference.model_copy(), path)
        return result

    def _guard_path(self, path: Path) -> None:
        try:
            self._absolute(path)
            if path.resolve() != path:
                raise MountedSecretError("MOUNTED_PATH_UNSAFE", stage="path")
            for component in (path, *path.parents):
                try:
                    info = component.lstat()
                except FileNotFoundError:
                    continue
                if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                        or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
                    raise MountedSecretError("MOUNTED_PATH_UNSAFE", stage="path")
        except MountedSecretError:
            raise
        except _IO_ERRORS:
            raise MountedSecretError("MOUNTED_PATH_UNSAFE", stage="path") from None

    def _private_directory(self, path: Path) -> None:
        self._guard_path(path)
        if path.exists():
            self._security.verify(path, directory=True)
        else:
            path.mkdir(mode=0o700)
            self._security.secure(path, directory=True)

    def _acquire_writer(self) -> None:
        path = self.credentials / "writer.lock"
        self._guard_path(path)
        existed = path.exists()
        if existed:
            self._security.verify(path, directory=False)
        handle = path.open("a+b")
        try:
            if not existed:
                self._security.secure(path, directory=False)
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except _IO_ERRORS:
            handle.close()
            raise MountedSecretError("CREDENTIAL_WRITER_LOCKED", stage="writer_lock") from None
        self._lock_file = handle

    def close(self) -> None:
        with self._mutex:
            handle, self._lock_file = self._lock_file, None
            self._closed = True
            if handle is not None:
                try:
                    # Closing the owning descriptor also releases its OS lock.
                    handle.close()
                except OSError:
                    raise MountedSecretError("MOUNTED_METADATA_UNAVAILABLE", stage="close") from None

    def _require_open(self) -> None:
        if self._closed or self._lock_file is None:
            raise MountedSecretError("CREDENTIAL_STORE_CLOSED", stage="access")

    @staticmethod
    def _stamp(info: os.stat_result) -> tuple[int, ...]:
        # Windows path/handle APIs can give ctime different meanings. Compare
        # ctime only before/after through the same API, never across interfaces.
        return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns)

    @staticmethod
    def _open_readonly(path: Path, _flags: int) -> int:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
        if os.name != "posix":
            return os.open(path, flags)
        # Anchor every directory component: an external rotation must not make
        # open follow a raced-in symlink. NONBLOCK also prevents FIFO hangs.
        required_flags = {name: getattr(os, name, 0) for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")}
        if not all(required_flags.values()):
            raise MountedSecretError("MOUNTED_PATH_UNSAFE", stage="path")
        directory_flags = os.O_RDONLY | required_flags["O_DIRECTORY"] | required_flags["O_NOFOLLOW"] | required_flags["O_CLOEXEC"]
        descriptor = os.open(path.anchor, directory_flags)
        try:
            for component in path.parts[1:-1]:
                child = os.open(component, directory_flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            return os.open(path.name, flags | required_flags["O_NOFOLLOW"], dir_fd=descriptor)
        finally:
            os.close(descriptor)

    def _read_file(self, path: Path, *, limit: int, private: bool = False) -> bytes:
        self._guard_path(path)
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 < before.st_size <= limit:
            raise MountedSecretError("CREDENTIAL_UNAVAILABLE", stage="read")
        if private:
            self._security.verify(path, directory=False)
        # The full-path open audit event precedes the anchored POSIX opener.
        with open(path, "rb", opener=lambda _, flags: self._open_readonly(path, flags)) as handle:
            opened = os.fstat(handle.fileno())
            self._guard_path(path)
            if self._stamp(opened) != self._stamp(before):
                raise MountedSecretError("CREDENTIAL_UNAVAILABLE", stage="read")
            value = handle.read(limit + 1)
            after = os.fstat(handle.fileno())
        self._guard_path(path)
        final = path.lstat()
        if (not 0 < len(value) <= limit or len(value) != after.st_size
                or opened.st_ctime_ns != after.st_ctime_ns or before.st_ctime_ns != final.st_ctime_ns
                or self._stamp(opened) != self._stamp(after) or self._stamp(after) != self._stamp(final)):
            raise MountedSecretError("CREDENTIAL_UNAVAILABLE", stage="read")
        return value

    def _source_value(self, path: Path) -> tuple[bytes, str]:
        value = self._read_file(path, limit=_MAX_SECRET)
        text = value.decode("utf-8")
        if not text.strip() or "\x00" in text:
            raise MountedSecretError("CREDENTIAL_UNAVAILABLE", stage="read")
        return value, text

    @staticmethod
    def _json_bytes(value: object) -> bytes:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")

    def _environment_binding(self) -> None:
        try:
            self._security.verify(self.credentials, directory=True)
            data = json.loads(self._read_file(self._environment_path, limit=_MAX_INDEX, private=True))
            if (not isinstance(data, dict) or set(data) != {"schema_version", "environment_id"}
                    or type(data["schema_version"]) is not int or data["schema_version"] != 1):
                raise ValueError
            if data["environment_id"] != str(self.environment_id):
                raise MountedSecretError("ENVIRONMENT_MISMATCH", stage="environment")
        except MountedSecretError:
            raise
        except _IO_ERRORS:
            raise MountedSecretError("MOUNTED_METADATA_UNAVAILABLE", stage="environment") from None

    def _load_index(self) -> dict[str, _Binding]:
        self._require_open()
        try:
            self._environment_binding()
            self._guard_path(self.metadata_root)
            self._security.verify(self.credentials, directory=True)
            self._security.verify(self.metadata_root, directory=True)
            data = json.loads(self._read_file(self._index_path, limit=_MAX_INDEX, private=True))
            if (not isinstance(data, dict) or set(data) != {"schema_version", "environment_id", "records", "integrity"}
                    or type(data["schema_version"]) is not int or data["schema_version"] != 1
                    or not isinstance(data["records"], dict) or not isinstance(data["integrity"], str)
                    or _DIGEST.fullmatch(data["integrity"]) is None):
                raise ValueError
            integrity = data.pop("integrity")
            if not hmac.compare_digest(integrity, hashlib.sha256(self._json_bytes(data)).hexdigest()):
                raise ValueError
            if data["environment_id"] != str(self.environment_id):
                raise MountedSecretError("ENVIRONMENT_MISMATCH", stage="metadata")
            records: dict[str, _Binding] = {}
            paths: set[Path] = set()
            identities: set[tuple[UUID, UUID]] = set()
            for secret_id, record in data["records"].items():
                if (not self._valid_id(secret_id) or not isinstance(record, dict)
                        or set(record) != {"reference", "path", "integrity"} or not isinstance(record["path"], str)):
                    raise ValueError
                reference = CredentialReference.model_validate(record["reference"])
                if not self._valid_reference(reference) or reference.external_secret_id != secret_id:
                    raise ValueError
                path = Path(record["path"])
                self._absolute(path)
                integrity = record["integrity"]
                if integrity is not None and (not isinstance(integrity, str) or _DIGEST.fullmatch(integrity) is None):
                    raise ValueError
                identity = (reference.credential_id, reference.version)
                if path in paths or identity in identities:
                    raise ValueError
                paths.add(path)
                identities.add(identity)
                records[secret_id] = _Binding(reference, path, integrity)
            return records
        except MountedSecretError:
            raise
        except _IO_ERRORS:
            raise MountedSecretError("MOUNTED_METADATA_UNAVAILABLE", stage="metadata") from None

    def _write_json(self, destination: Path, payload: object, *, immutable: bool = False) -> None:
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        try:
            self._guard_path(destination)
            if immutable and destination.exists():
                raise MountedSecretError("SECRET_BINDING_IMMUTABLE", stage="metadata_write")
            value = self._json_bytes(payload)
            if len(value) > _MAX_INDEX:
                raise ValueError
            self._guard_path(temporary)
            with temporary.open("xb") as handle:
                self._security.secure(temporary, directory=False)
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            self._guard_path(temporary)
            self._guard_path(destination)
            if immutable and destination.exists():
                raise MountedSecretError("SECRET_BINDING_IMMUTABLE", stage="metadata_write")
            os.replace(temporary, destination)
        except _IO_ERRORS:
            # Only private metadata candidates may remain; never copy plaintext.
            raise MountedSecretError("MOUNTED_METADATA_UNAVAILABLE", stage="metadata_write") from None

    def _write_index(self, records: dict[str, _Binding]) -> None:
        payload: dict[str, object] = {"schema_version": 1, "environment_id": str(self.environment_id), "records": {
            key: {"reference": binding.reference.model_dump(mode="json"), "path": str(binding.path), "integrity": binding.integrity}
            for key, binding in records.items()}}
        payload["integrity"] = hashlib.sha256(self._json_bytes(payload)).hexdigest()
        self._write_json(self._index_path, payload)

    def _register(self, *, fresh: bool) -> None:
        records = {} if fresh else self._load_index()
        existing_paths = {binding.path for binding in records.values()}
        identities = {(binding.reference.credential_id, binding.reference.version) for binding in records.values()}
        additions = {}
        for secret_id, binding in self._mounts.items():
            previous = records.get(secret_id)
            if previous is not None:
                if previous.reference != binding.reference or previous.path != binding.path:
                    raise MountedSecretError("SECRET_BINDING_IMMUTABLE", stage="registration")
            elif binding.path in existing_paths or (binding.reference.credential_id, binding.reference.version) in identities:
                raise MountedSecretError("SECRET_BINDING_IMMUTABLE", stage="registration")
            else:
                additions[secret_id] = binding
        for secret_id, binding in additions.items():
            integrity = None
            try:
                value, _ = self._source_value(binding.path)
                integrity = hashlib.sha256(value).hexdigest()
            except _IO_ERRORS:
                # A never-proven version cannot later acquire a different value
                # under its old identity. Supply a new external ID/version.
                pass
            records[secret_id] = _Binding(binding.reference, binding.path, integrity)
        if fresh or additions:
            self._write_index(records)

    def _allowed(self, reference: CredentialReference, version: UUID) -> _Binding:
        if (not self._valid_reference(reference) or not isinstance(reference.external_secret_id, str)
                or not isinstance(version, UUID) or version != reference.version):
            raise MountedSecretError("INVALID_CREDENTIAL_REFERENCE", stage="reference")
        binding = self._mounts.get(reference.external_secret_id)
        if binding is None or binding.reference != reference:
            raise MountedSecretError("SECRET_REFERENCE_NOT_ALLOWED", stage="reference")
        return binding

    def _get(self, reference: CredentialReference, binding: _Binding) -> SecretStr:
        try:
            records = self._load_index()
            if not isinstance(reference.external_secret_id, str):
                raise TypeError
            record = records.get(reference.external_secret_id)
            if (record is None or record.reference != reference or record.path != binding.path or record.integrity is None):
                raise ValueError
            value, text = self._source_value(binding.path)
            if not hmac.compare_digest(record.integrity, hashlib.sha256(value).hexdigest()):
                raise ValueError
            return SecretStr(text)
        except _IO_ERRORS:
            raise MountedSecretError("CREDENTIAL_UNAVAILABLE", stage="resolve") from None

    def get(self, reference: CredentialReference, version: UUID) -> SecretStr:
        with self._mutex:
            self._require_open()
            return self._get(reference, self._allowed(reference, version))

    def status(self, reference: CredentialReference, version: UUID) -> CredentialStatus:
        with self._mutex:
            binding = self._allowed(reference, version)
            try:
                self._get(reference, binding)
            except MountedSecretError:
                return CredentialStatus(credential_ref=reference.credential_id, version=reference.version, status="unavailable")
            return CredentialStatus(credential_ref=reference.credential_id, version=reference.version, status="available")

    def put(self, *, purpose: str, secret_value: SecretStr, credential_id: UUID | None = None) -> CredentialReference:
        raise MountedSecretError("EXTERNALLY_MANAGED", stage="write")

    def delete(self, reference: CredentialReference, version: UUID) -> None:
        raise MountedSecretError("EXTERNALLY_MANAGED", stage="delete")
