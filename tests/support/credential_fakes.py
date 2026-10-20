"""TCR-S2-CREDENTIALS-006: test ports, not a credential-store implementation.

Only constructors below adapt dependency injection. Lifecycle decisions must be
made by production put/get/delete/status; these doubles never make them.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import os
import secrets
import socket
import subprocess
import sys
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy.engine import Engine

from backend.app.schemas.settings import CredentialReference
from backend.app.services.settings.config_service import ConfigService

PURPOSES = ("provider", "database", "redis", "jwt")


def required_symbol(module_name: str, symbol: str):
    """Keep missing implementation distinct from ImportError/collection failure."""
    if importlib.util.find_spec(module_name) is None:
        pytest.fail(f"T204_IMPLEMENTATION_REQUIRED: {module_name}.{symbol}", pytrace=False)
    module = importlib.import_module(module_name)
    if not hasattr(module, symbol):
        pytest.fail(f"T204_IMPLEMENTATION_REQUIRED: {module_name}.{symbol}", pytrace=False)
    return getattr(module, symbol)


def make_reference(environment_id: UUID, *, purpose="jwt", backend="dpapi", secret_id=None):
    return CredentialReference(credential_id=uuid4(), version=uuid4(), environment_id=environment_id,
                               backend=backend, purpose=purpose, external_secret_id=secret_id)


@dataclass
class FakeProtector:
    """Opaque in-memory substitution; no crypto, disk, or reference policy."""

    failure_text: str = "synthetic-protector-failure"
    protect_error: bool = False
    unprotect_error: bool = False
    protect_calls: int = 0
    unprotect_calls: int = 0
    blobs: dict[bytes, bytes] = field(default_factory=dict, repr=False)

    def protect(self, plaintext: bytes) -> bytes:
        self.protect_calls += 1
        if self.protect_error:
            raise OSError(self.failure_text)
        blob = b"FAKE-DPAPI-OPAQUE:" + uuid4().hex.encode("ascii")
        self.blobs[blob] = bytes(plaintext)
        return blob

    def unprotect(self, ciphertext: bytes) -> bytes:
        self.unprotect_calls += 1
        if self.unprotect_error or ciphertext not in self.blobs:
            raise OSError(self.failure_text)
        return self.blobs[ciphertext]


@dataclass
class FakeReferenceIndex:
    """Return only caller-owned reference facts; never decide deletion policy."""

    uses: tuple[str, ...] = ()
    failure_text: str | None = None
    calls: list[tuple[UUID, UUID]] = field(default_factory=list)

    def references(self, reference: CredentialReference, version: UUID) -> tuple[str, ...]:
        self.calls.append((reference.credential_id, version))
        if self.failure_text is not None:
            raise OSError(self.failure_text)
        return self.uses


@dataclass
class FakeFileSecurity:
    """Record ACL boundaries only; no filesystem or credential policy."""

    failure_text: str | None = None
    calls: list[tuple[str, Path, bool]] = field(default_factory=list)

    def secure(self, path: Path, *, directory: bool) -> None:
        self.calls.append(("secure", path, directory))
        if self.failure_text is not None:
            raise OSError(self.failure_text)

    def verify(self, path: Path, *, directory: bool) -> None:
        self.calls.append(("verify", path, directory))
        if self.failure_text is not None:
            raise OSError(self.failure_text)


@contextmanager
def windows_store(root, environment_id, protector, reference_index):
    cls = required_symbol("backend.app.services.settings.credential_store", "CredentialStore")
    store = cls(root=root, environment_id=environment_id, protector=protector,
                reference_index=reference_index, file_security=FakeFileSecurity())
    try:
        yield store
    finally:
        close = getattr(store, "close", None)
        if close is not None:
            close()


@contextmanager
def mounted_store(root, environment_id, mounts):
    # Mapping is trusted server configuration: secret_id -> (immutable ref, path).
    cls = required_symbol("backend.app.services.settings.mounted_secrets", "MountedSecretStore")
    store = cls(root=root, environment_id=environment_id, mounts=mounts)
    try:
        yield store
    finally:
        close = getattr(store, "close", None)
        if close is not None:
            close()


def safe_surfaces(value):
    if hasattr(value, "model_dump_json"):
        serialized = value.model_dump_json()
    else:
        serialized = json.dumps(value, default=str, ensure_ascii=False)
    return {"serialized": serialized, "repr": repr(value), "str": str(value)}


def error_surfaces(error):
    return {"error": str(error), "repr": repr(error), "trace": "".join(traceback.format_exception(error)),
            "attributes": json.dumps(vars(error), default=str, ensure_ascii=False), "args": repr(error.args)}


def assert_no_secret(markers, surfaces):
    """Never include offending bytes or marker values in pytest diagnostics."""
    markers = tuple(markers.values()) if isinstance(markers, dict) else tuple(markers)
    for label, content in surfaces.items():
        raw = content if isinstance(content, bytes) else str(content).encode("utf-8")
        if any(marker.encode("utf-8") in raw for marker in markers):
            pytest.fail(f"SECRET_DISCLOSURE in {label}", pytrace=False)


def assert_secret_equal(actual, expected):
    if isinstance(actual, SecretStr):
        actual = actual.get_secret_value()
    if isinstance(actual, (bytes, bytearray)):
        actual = bytes(actual).decode("utf-8")
    if actual != expected:
        pytest.fail("controlled credential value did not match the synthetic input", pytrace=False)


def state_of(status):
    return status["status"] if isinstance(status, dict) else status.status


def files_under(root):
    # Windows denies reading the byte range held by ConfigService's empty lock.
    # stat proves a zero-byte file has no content; never ignore a nonempty lock.
    return {path.relative_to(root).as_posix(): (
                b"" if path.name == "writer.lock" and path.stat().st_size == 0 else path.read_bytes()
            ) for path in sorted(root.rglob("*")) if path.is_file()} if root.exists() else {}


@contextmanager
def observe_reads(target, *, failure_text=None):
    """Observe actual Python open attempts independent of an I/O wrapper choice."""
    target = target.resolve()
    attempts = []
    active = True

    def observer(event, args):
        if active and event == "open" and not isinstance(args[0], int):
            path, _mode, flags = args
            if Path(os.fsdecode(path)).resolve() == target and not flags & (os.O_WRONLY | os.O_RDWR):
                attempts.append("read")
                if failure_text is not None:
                    raise PermissionError(failure_text)

    sys.addaudithook(observer)
    try:
        yield attempts
    finally:
        active = False  # Python audit hooks cannot be removed; inactive is a no-op.


def protected_files(root, protector):
    return {name: content for name, content in files_under(root).items()
            if any(blob in content for blob in protector.blobs)}


def assert_same_files(before, after):
    if before != after:
        pytest.fail("test-owned files changed unexpectedly", pytrace=False)


def baseline_values(root, environment_id, reference=None):
    return {"business": {
        "database_locator": {"host": "database.invalid", "database": "synthetic_test", "user": "synthetic_runner"},
        "redis_locator": {"host": "redis.invalid"}, "storage_root": str(root / "storage"),
        "jwt_secret_ref": (reference or make_reference(environment_id)).model_dump(mode="json"),
    }}


def activate_reference(service: ConfigService, reference):
    draft = service.create_draft(baseline_values(service.root, service.environment_id, reference),
                                 expected_revision=None, process_env={})
    service.validate(draft.revision_id, expected_revision=None, process_env={})
    return service.activate(draft.revision_id, expected_revision=None, process_env={})


def stage_pending_reference(service: ConfigService, active):
    changed = active.values.model_dump(mode="json")
    changed["business"]["jwt_expire_minutes"] += 1
    draft = service.create_draft(changed, expected_revision=active.revision_id, process_env={})
    service.validate(draft.revision_id, expected_revision=active.revision_id, process_env={})
    pending = service.activate(draft.revision_id, expected_revision=active.revision_id, process_env={})
    assert pending.status == "pending_restart"
    return pending


@pytest.fixture
def secret_bank():
    return {purpose: "t204_synthetic_" + secrets.token_hex(24) for purpose in PURPOSES}


@pytest.fixture
def credential_sandbox(tmp_path, monkeypatch, caplog, capsys, request, secret_bank):
    """Local fallback for direct pytest; the session runner also guards imports."""
    for name in tuple(os.environ):
        upper = name.upper()
        if upper.startswith(("EDUAGENT_", "LANGSMITH_", "LANGCHAIN_")) or upper.endswith("_API_KEY") or upper in {
            "DATABASE_URL", "REDIS_URL", "JWT_SECRET_KEY", "DEV_MODE", "STORAGE_ROOT", "PYTEST_ADDOPTS",
        }:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EDUAGENT_ENVIRONMENT", "test")
    monkeypatch.setenv("EDUAGENT_DATA_ROOT", str(tmp_path / "runtime"))
    for name in ("HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "TEMP", "TMP", "TMPDIR"):
        monkeypatch.setenv(name, str(tmp_path))
    monkeypatch.chdir(tmp_path)
    calls = {"network": 0, "database": 0, "process": 0}

    def blocked(kind):
        def reject(*_args, **_kwargs):
            calls[kind] += 1
            pytest.fail("T204 forbids real " + kind + " access", pytrace=False)
        return reject

    for name in ("connect", "connect_ex", "sendto", "bind"):
        monkeypatch.setattr(socket.socket, name, blocked("network"))
    monkeypatch.setattr(socket, "getaddrinfo", blocked("network"))
    monkeypatch.setattr(Engine, "connect", blocked("database"))
    monkeypatch.setattr(subprocess, "Popen", blocked("process"))
    caplog.set_level(logging.DEBUG)
    yield
    captured = capsys.readouterr()
    surfaces = {"stdout": captured.out, "stderr": captured.err, "logs": caplog.text}
    for directory in ("config", "credentials", "logs", "results", "cache"):
        for name, content in files_under(tmp_path / directory).items():
            surfaces[directory + "/" + name] = content
    assert_no_secret(secret_bank, surfaces)
    request.node.user_properties.append(("t204_forbidden_calls", dict(calls)))
    request.node.user_properties.append(("t204_secret_matches", 0))
    assert calls == {"network": 0, "database": 0, "process": 0}
