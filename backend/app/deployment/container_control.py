"""Docker exec authorization through a private, same-uid Unix socket.

No HTTP issuing route or service log contains a grant. The CLI returns the
one-time fragment URL only to the operator's explicit exec invocation.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import stat
import struct
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI

from backend.app.core.environment import resolve_environment_profile
from backend.app.core.settings_security import SettingsSecurityError
from backend.app.deployment.local_management import (
    CONTROL_MESSAGES,
    LocalManagementAuthority,
    LocalManagementError,
    authorization_url,
    settings_base_url,
)
from backend.app.schemas.settings import RuntimeKind

_LIMIT = 8192


def dispatch(authority: LocalManagementAuthority, message: object) -> dict[str, object]:
    if (not isinstance(message, dict)
            or set(message) != {"type", "settings_base_url"}
            or not isinstance(message.get("type"), str)
            or message.get("type") not in CONTROL_MESSAGES
            or not isinstance(message.get("settings_base_url"), str)):
        raise LocalManagementError("CONTROL_MESSAGE_INVALID", "固定管理消息无效。")
    base = message["settings_base_url"]
    try:
        parts = urlsplit(base)
        expected = settings_base_url(parts.hostname or "", parts.port or 80)
        if base != expected:
            raise ValueError
    except (ValueError, TypeError):
        raise LocalManagementError("AUTHORIZATION_BASE_URL_INVALID", "需要精确回环地址。") from None
    return authority.handle_control_message(message)


def _current_uid() -> int:
    getter = getattr(os, "geteuid", None)
    if not callable(getter):
        raise TypeError("CONTAINER_CONTROL_PLATFORM_UNSUPPORTED")
    return int(getter())


def control_path() -> Path:
    return resolve_environment_profile().temp_root / "local-management" / "setup.sock"


def _read_message(peer: socket.socket) -> Any:
    data = bytearray()
    while b"\n" not in data and len(data) <= _LIMIT:
        part = peer.recv(min(1024, _LIMIT + 1 - len(data)))
        if not part:
            break
        data.extend(part)
    line, separator, tail = data.partition(b"\n")
    if not separator or tail.strip() or len(data) > _LIMIT:
        raise ValueError("CONTROL_FRAME_INVALID")
    return json.loads(line)


def _send(peer: socket.socket, payload: object) -> None:
    peer.sendall(json.dumps(payload, separators=(",", ":")).encode() + b"\n")


class ContainerControl:
    def __init__(self, authority: LocalManagementAuthority, path: Path):
        self.authority, self.path = authority, path
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._identity: tuple[int, int] | None = None

    def start(self) -> None:
        if os.name != "posix" or not hasattr(socket, "SO_PEERCRED"):
            raise RuntimeError("CONTAINER_CONTROL_PLATFORM_UNSUPPORTED")
        directory = self.path.parent
        if not directory.is_absolute() or directory.resolve() != directory:
            raise RuntimeError("CONTAINER_CONTROL_PATH_INVALID")
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = directory.lstat()
        if info.st_uid != _current_uid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise RuntimeError("CONTAINER_CONTROL_DIRECTORY_UNSAFE")
        if self.path.exists():
            existing = self.path.lstat()
            if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != _current_uid():
                raise RuntimeError("CONTAINER_CONTROL_PATH_OCCUPIED")
            with socket.socket(getattr(socket, "AF_UNIX", -1)) as probe:
                try:
                    probe.connect(str(self.path))
                except ConnectionRefusedError:
                    # Only a stale owned socket in our private directory can be removed.
                    if self.path.lstat() != existing:
                        raise RuntimeError("CONTAINER_CONTROL_PATH_CHANGED") from None
                    self.path.unlink()
                else:
                    raise RuntimeError("CONTAINER_CONTROL_ALREADY_RUNNING")
        listener = socket.socket(getattr(socket, "AF_UNIX", -1), socket.SOCK_STREAM)
        try:
            listener.bind(str(self.path))
            os.chmod(self.path, 0o600)
            info = self.path.lstat()
            self._identity = (info.st_dev, info.st_ino)
            listener.listen(4)
            listener.settimeout(.2)
            self._listener = listener
            self._thread = threading.Thread(target=self._serve, name="container-control", daemon=True)
            self._thread.start()
        except OSError:
            listener.close()
            self.close()
            raise

    def _serve(self) -> None:
        listener = self._listener
        assert listener is not None
        while not self._stop.is_set():
            try:
                peer, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                raise
            with peer:
                peer.settimeout(1)
                try:
                    _pid, uid, _gid = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, getattr(socket, "SO_PEERCRED", -1), 12))
                    if uid != _current_uid():
                        raise LocalManagementError("CONTROL_PEER_DENIED", "仅容器运行用户可管理。")
                    reply = dispatch(self.authority, _read_message(peer))
                except (LocalManagementError, SettingsSecurityError) as error:
                    reply = {"error": error.code}
                except (OSError, ValueError, TypeError):
                    reply = {"error": "CONTROL_MESSAGE_INVALID"}
                try:
                    _send(peer, reply)
                except OSError:
                    pass  # The caller disconnected; no grant is logged or persisted.

    def close(self) -> None:
        self._stop.set()
        if self._listener is not None:
            self._listener.close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            if self._thread.is_alive():
                raise RuntimeError("CONTAINER_CONTROL_NOT_STOPPED")
        if self._identity is not None and self.path.exists():
            info = self.path.lstat()
            if (info.st_dev, info.st_ino) == self._identity and stat.S_ISSOCK(info.st_mode):
                self.path.unlink()


def install_container_control(app: FastAPI) -> None:
    context = getattr(app.state, "bootstrap", None)
    settings = getattr(context, "settings", None)
    if settings is None or settings.runtime_kind != RuntimeKind.DOCKER:
        return
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        async with original(application):
            control = ContainerControl(LocalManagementAuthority(application.state.setup_security), control_path())
            try:
                control.start()
                yield
            finally:
                control.close()

    app.router.lifespan_context = lifespan


def request_authorization(base: str, *, reauthorize: bool = False) -> str:
    path = control_path()
    info = path.lstat()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != _current_uid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise RuntimeError("CONTAINER_CONTROL_SOCKET_UNSAFE")
    with socket.socket(getattr(socket, "AF_UNIX", -1)) as peer:
        peer.settimeout(3)
        peer.connect(str(path))
        _send(peer, {"type": "reauthorize" if reauthorize else "open_settings", "settings_base_url": base})
        result = _read_message(peer)
    if not isinstance(result, dict) or not isinstance(result.get("code"), str):
        raise TypeError("CONTAINER_AUTHORIZATION_FAILED")
    expected = authorization_url(base, result["code"])
    if result.get("authorization_url") != expected:
        raise RuntimeError("CONTAINER_AUTHORIZATION_INVALID")
    return expected


def main() -> None:
    parser = argparse.ArgumentParser(description="只向受限 docker exec 操作者返回一次性设置 URL")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True, help="实际宿主回环发布端口")
    parser.add_argument("--reauthorize", action="store_true")
    args = parser.parse_args()
    try:
        print(request_authorization(settings_base_url(args.host, args.port), reauthorize=args.reauthorize))
    except (OSError, ValueError, TypeError, RuntimeError, LocalManagementError):
        raise SystemExit("CONTAINER_AUTHORIZATION_UNAVAILABLE") from None


if __name__ == "__main__":
    main()
