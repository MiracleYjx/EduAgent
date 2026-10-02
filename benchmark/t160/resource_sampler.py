"""Actual Windows working set and container-process RSS observations for T160.

This module makes no model calls and never treats missing observations as zero.
Container RSS sums may count shared pages more than once. PostgreSQL scope is all
postgres processes in the shared container, not only the isolated test database.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import threading
import time
from ctypes import wintypes
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path


def _utc():
    return datetime.now(UTC).isoformat()


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def verified_worker_chain(worker_pid: int, launched_pid: int) -> list[int]:
    """Prove a handshaken worker belongs to the controller's exact launched subtree."""
    if os.name != "nt":
        raise RuntimeError("Windows process-tree verification required")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    kernel.Process32NextW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise RuntimeError("Cannot inspect launched Windows process subtree")
    parents = {}
    try:
        row = PROCESSENTRY32W()
        row.dwSize = ctypes.sizeof(row)
        valid = kernel.Process32FirstW(handle, ctypes.byref(row))
        while valid:
            parents[int(row.th32ProcessID)] = int(row.th32ParentProcessID)
            valid = kernel.Process32NextW(handle, ctypes.byref(row))
    finally:
        kernel.CloseHandle(handle)
    chain = [int(worker_pid)]
    for _ in range(32):
        if chain[-1] == int(launched_pid) and chain[-1] in parents:
            return chain
        parent = parents.get(chain[-1])
        if parent is None or parent <= 0 or parent in chain:
            break
        chain.append(parent)
    raise RuntimeError("Actual worker is not in the exact launched process subtree")


def windows_working_set(pid: int):
    if os.name != "nt":
        return None, {"code": "WINDOWS_REQUIRED"}
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    psapi.GetProcessMemoryInfo.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX),
        wintypes.DWORD,
    )
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x0400 | 0x0010, False, int(pid))
    if not handle:
        return None, {
            "code": "PROCESS_OPEN_FAILED",
            "winerror": ctypes.get_last_error(),
        }
    try:
        counters = PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return None, {
                "code": "PROCESS_MEMORY_FAILED",
                "winerror": ctypes.get_last_error(),
            }
        return int(counters.WorkingSetSize), None
    finally:
        kernel.CloseHandle(handle)


def container_rss(container: str, process_name: str):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", container):
        return None, None, {"code": "INVALID_CONTAINER_NAME"}
    # Names and commands are fixed by the experiment, never user shell text.
    program = (
        "awk '/^Name:/ {n=$2} /^VmRSS:/ {if(n==\""
        + process_name
        + "\") print FILENAME,$2}' /proc/[0-9]*/status"
    )
    try:
        result = subprocess.run(
            ["docker", "exec", container, "sh", "-c", program],
            capture_output=True,
            check=False,
            text=True,
            timeout=4,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode != 0:
            return (
                None,
                None,
                {"code": "CONTAINER_PROBE_FAILED", "exit_code": result.returncode},
            )
        rows = []
        for line in result.stdout.splitlines():
            match = re.fullmatch(r"/proc/(\d+)/status\s+(\d+)", line.strip())
            if match is None:
                return None, None, {"code": "CONTAINER_PROBE_INVALID_OUTPUT"}
            rows.append({"pid": int(match[1]), "rss_bytes": int(match[2]) * 1024})
        if not rows:
            return None, None, {"code": "CONTAINER_PROCESS_NOT_FOUND"}
        return sum(row["rss_bytes"] for row in rows), rows, None
    except subprocess.TimeoutExpired:
        return None, None, {"code": "CONTAINER_PROBE_TIMEOUT"}
    except OSError as exc:
        return (
            None,
            None,
            {"code": "CONTAINER_PROBE_UNAVAILABLE", "type": type(exc).__name__},
        )


class ResourceSampler:
    def __init__(
        self,
        *,
        pid: int,
        pg_container: str,
        redis_container: str,
        output_path: Path,
        interval_seconds: float = 1.0,
    ):
        if pid <= 0 or interval_seconds <= 0:
            raise ValueError("pid and interval_seconds must be positive")
        self.pid = pid
        self.pg_container = pg_container
        self.redis_container = redis_container
        self.output_path = Path(output_path)
        self.interval_seconds = float(interval_seconds)
        self._stop = threading.Event()
        self._guard = threading.Lock()
        self._rows = []
        self._thread = None
        self._stream = None

    def start(self):
        if self._thread is not None:
            raise RuntimeError("sampler already started")
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.output_path.open("x", encoding="utf-8")
        self.sample_now()
        self._thread = threading.Thread(
            target=self._loop, name="t160-resource-observer", daemon=True
        )
        self._thread.start()
        return self

    def _loop(self):
        next_tick = time.perf_counter() + self.interval_seconds
        while not self._stop.wait(max(0.0, next_tick - time.perf_counter())):
            self.sample_now()
            next_tick += self.interval_seconds
            if next_tick < time.perf_counter():
                next_tick = time.perf_counter() + self.interval_seconds

    def sample_now(self):
        with self._guard:
            if self._stream is None:
                raise RuntimeError("sampler is not running")
            start = time.perf_counter()
            app, app_error = windows_working_set(self.pid)
            pg, pg_rows, pg_error = container_rss(self.pg_container, "postgres")
            redis, redis_rows, redis_error = container_rss(
                self.redis_container, "redis-server"
            )
            row = {
                "sampled_at": _utc(),
                "perf_counter": start,
                "sample_completed_perf": time.perf_counter(),
                "pid": self.pid,
                "app_working_set_bytes": app,
                "postgres_container_process_rss_bytes": pg,
                "redis_container_process_rss_bytes": redis,
                "postgres_processes": pg_rows,
                "redis_processes": redis_rows,
                "errors": {
                    k: v
                    for k, v in (
                        ("app", app_error),
                        ("postgres", pg_error),
                        ("redis", redis_error),
                    )
                    if v is not None
                },
            }
            row["simultaneous_observed_sum_bytes"] = (
                app + pg + redis
                if all(v is not None for v in (app, pg, redis))
                else None
            )
            self._rows.append(row)
            self._stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._stream.flush()
            return row

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=12)
            if self._thread.is_alive():
                raise RuntimeError(
                    "sampler did not stop after bounded container probes"
                )
        with self._guard:
            if self._stream is not None:
                self._stream.close()
                self._stream = None

    def summary(
        self, *, start_perf: float | None = None, end_perf: float | None = None
    ):
        with self._guard:
            rows = [
                r
                for r in self._rows
                if (start_perf is None or r["perf_counter"] >= start_perf)
                and (end_perf is None or r["sample_completed_perf"] <= end_perf)
            ]
        keys = (
            "app_working_set_bytes",
            "postgres_container_process_rss_bytes",
            "redis_container_process_rss_bytes",
            "simultaneous_observed_sum_bytes",
        )
        peaks = {
            key: max((r[key] for r in rows if r[key] is not None), default=None)
            for key in keys
        }
        observed = [r["perf_counter"] for r in rows]
        gaps = [b - a for a, b in pairwise(observed)]
        complete = bool(rows) and all(
            not r["errors"] and all(r[key] is not None for key in keys) for r in rows
        )
        if start_perf is not None:
            complete = (
                complete
                and rows[0]["perf_counter"] - start_perf <= self.interval_seconds * 2.5
            )
        if end_perf is not None:
            complete = (
                complete
                and end_perf - rows[-1]["sample_completed_perf"]
                <= self.interval_seconds * 2.5
            )
        maximum_gap = max(gaps, default=0.0) if rows else None
        if maximum_gap is not None and maximum_gap > self.interval_seconds * 2.5:
            complete = False
        return {
            "sample_count": len(rows),
            "requested_interval_seconds": self.interval_seconds,
            "max_observed_gap_seconds": maximum_gap,
            "complete_at_requested_sampling_resolution": complete,
            "peaks": peaks,
            "missing_sample_count": sum(bool(r["errors"]) for r in rows),
            "first_sample_at": rows[0]["sampled_at"] if rows else None,
            "last_sample_at": rows[-1]["sampled_at"] if rows else None,
            "scope": {
                "app": "specified Windows application process working set; excludes experiment controller",
                "postgres": "sum of all postgres process RSS in shared "
                + self.pg_container
                + "; not isolated database attribution; shared pages can be counted repeatedly",
                "redis": "sum of all redis-server process RSS in dedicated "
                + self.redis_container,
                "sum": "observations within one sample, read sequentially; sampled peaks, not a continuous physical-memory high-water mark",
            },
        }
