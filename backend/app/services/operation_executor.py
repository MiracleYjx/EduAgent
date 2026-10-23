"""Kernel-held execution witnesses survive PID reuse and container namespaces.

An executor holds the witness through its final database write. Recovery must
lock the very same file object; a copied/restored or missing file is no proof.
The database execution UUID independently fences every late write.
"""
from __future__ import annotations

import json
import os
import platform
import sys
from pathlib import Path
from typing import Any, BinaryIO, Self
from uuid import UUID


class ExecutorError(ValueError):
    pass


def _process_identity() -> dict[str, Any]:
    identity: dict[str, Any] = {"pid": os.getpid(), "platform": os.name, "host": platform.node()}
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        times = [wintypes.FILETIME() for _ in range(4)]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
        if not kernel.GetProcessTimes(kernel.GetCurrentProcess(), *(ctypes.byref(t) for t in times)):
            raise ExecutorError("EXECUTOR_PROOF_UNAVAILABLE")
        identity["process_start"] = str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
    else:
        try:
            identity["boot_id"] = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            identity["pid_namespace"] = os.readlink("/proc/self/ns/pid")
            identity["process_start"] = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19]
        except OSError as error:
            raise ExecutorError("EXECUTOR_PROOF_UNAVAILABLE") from error
    return identity


class ExecutorGuard:
    def __init__(self, path: Path, stream: BinaryIO, identity: dict[str, Any]):
        self.path, self.stream, self.identity = path, stream, identity

    @staticmethod
    def _lock(stream: BinaryIO) -> None:
        try:
            stream.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ExecutorError("EXECUTOR_STILL_ACTIVE") from error

    @classmethod
    def create(cls, root: Path, execution_id: UUID) -> ExecutorGuard:
        directory = root / "operation-executors"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (execution_id.hex + ".lock")
        stream = path.open("x+b")
        try:
            stream.write(b"1")
            stream.flush()
            cls._lock(stream)
            stat = os.fstat(stream.fileno())
            identity = {**_process_identity(), "execution_id": str(execution_id),
                "file_device": stat.st_dev, "file_inode": stat.st_ino}
            stream.seek(1)
            stream.write(json.dumps(identity, sort_keys=True).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
            return cls(path, stream, identity)
        except BaseException:
            stream.close()
            raise

    @classmethod
    def stopped(cls, root: Path, identity: dict[str, Any]) -> ExecutorGuard:
        try:
            execution_id = UUID(identity["execution_id"])
            path = root / "operation-executors" / (execution_id.hex + ".lock")
            if path.is_symlink() or path.resolve().parent != (root / "operation-executors").resolve():
                raise ValueError("invalid witness")
            stream = path.open("r+b")
            stat = os.fstat(stream.fileno())
            if (stat.st_dev, stat.st_ino) != (identity["file_device"], identity["file_inode"]):
                stream.close()
                raise ValueError("witness was replaced")
        except (OSError, KeyError, TypeError, ValueError) as error:
            raise ExecutorError("EXECUTOR_PROOF_UNAVAILABLE") from error
        try:
            cls._lock(stream)
            stream.seek(1)
            recorded = json.loads(stream.read())
            if recorded != identity:
                raise ExecutorError("EXECUTOR_PROOF_UNAVAILABLE")
            return cls(path, stream, identity)
        except BaseException:
            stream.close()
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_) -> None:
        self.stream.close()
