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


def windows_process_parents() -> dict[int, int]:
    """Read the current Windows process identities and parent relationships."""
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
    return parents


def verified_worker_chain(worker_pid: int, launched_pid: int) -> list[int]:
    """Prove a handshaken worker belongs to the controller's exact launched subtree."""
    parents = windows_process_parents()
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


def windows_application_working_set(root_pid: int):
    """Observe the verified worker and its actual descendants; exclude its parents."""
    try:
        parents = windows_process_parents()
    except RuntimeError as exc:
        return (
            None,
            [],
            {"code": "APPLICATION_TREE_UNAVAILABLE", "type": type(exc).__name__},
        )
    if root_pid not in parents:
        return None, [], {"code": "APPLICATION_PROCESS_NOT_FOUND"}
    identities = {root_pid}
    while True:
        children = {
            pid for pid, parent in parents.items() if parent in identities
        } - identities
        if not children:
            break
        identities.update(children)
    rows = []
    for pid in [root_pid, *sorted(identities - {root_pid})]:
        value, error = windows_working_set(pid)
        rows.append(
            {
                "pid": pid,
                "parent_pid": parents[pid],
                "working_set_bytes": value,
                "error": error,
            }
        )
    if any(row["working_set_bytes"] is None for row in rows):
        return None, rows, {"code": "APPLICATION_PROCESS_SAMPLE_MISSING"}
    return sum(row["working_set_bytes"] for row in rows), rows, None


def evaluate_v2_memory_budget(resources):
    """Compare complete measured windows with plan v2 soft upper budgets."""
    limits = {
        "app_working_set_bytes": 4_000_000_000,
        "postgres_container_process_rss_bytes": 1_500_000_000,
        "redis_container_process_rss_bytes": 512_000_000,
        "simultaneous_observed_sum_bytes": 6_000_000_000,
    }
    peaks = resources.get("peaks", {})
    complete = resources.get(
        "complete_at_requested_sampling_resolution", False
    ) and all(peaks.get(key) is not None for key in limits)
    passes = {
        key: peaks[key] <= value if complete else None for key, value in limits.items()
    }
    return {
        "protocol": "plan-v2-memory-soft-upper-budgets",
        "unit_policy": "GB=1000000000 bytes; MB=1000000 bytes; display MiB=1048576 bytes",
        "limits_bytes": limits,
        "limits_mib": {key: value / 1_048_576 for key, value in limits.items()},
        "observed_peaks_bytes": {key: peaks.get(key) for key in limits},
        "observed_peaks_mib": {
            key: peaks[key] / 1_048_576 if peaks.get(key) is not None else None
            for key in limits
        },
        "observed_over_budget": {
            key: peaks[key] > value if peaks.get(key) is not None else None
            for key, value in limits.items()
        },
        "resource_complete": bool(complete),
        "component_pass": passes,
        "budget_pass": all(passes.values()) if complete else None,
        "scope": "Observed application subtree working set plus PostgreSQL/Redis process RSS. Shared pages may be counted repeatedly; not unique physical memory or EXE acceptance.",
    }


def container_rss(container: str, process_name: str):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", container):
        return None, None, {"code": "INVALID_CONTAINER_NAME"}
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,15}", process_name):
        return None, None, {"code": "INVALID_PROCESS_NAME"}
    # First select actual target identities. A disappearing health-check shell
    # must not make awk's broad /proc glob fail. A selected target disappearing,
    # lacking RSS, or failing to read still fails the entire observation.
    program = (
        "for process in /proc/[0-9]*; do "
        'IFS= read -r name < "$process/comm" 2>/dev/null || continue; '
        f'[ "$name" = "{process_name}" ] || continue; '
        "awk '/^VmRSS:/ {print FILENAME,$2; found=1} END {if (!found) exit 1}' "
        '"$process/status" || exit 1; done'
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
            app, app_rows, app_error = windows_application_working_set(self.pid)
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
                "application_processes": app_rows,
                "application_root_pid": self.pid,
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
        coverage_span = observed[-1] - observed[0] if observed else None
        complete = (
            len(rows) >= 2
            and coverage_span >= self.interval_seconds
            and all(
                not r["errors"] and all(r[key] is not None for key in keys)
                for r in rows
            )
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
            "observed_sampling_span_seconds": coverage_span,
            "window_duration_seconds": (
                end_perf - start_perf
                if start_perf is not None and end_perf is not None
                else None
            ),
            "full_sampling_cycles_observed": (
                int(coverage_span / self.interval_seconds)
                if coverage_span is not None
                else 0
            ),
            "requested_interval_seconds": self.interval_seconds,
            "max_observed_gap_seconds": maximum_gap,
            "complete_at_requested_sampling_resolution": complete,
            "peaks": peaks,
            "missing_sample_count": sum(bool(r["errors"]) for r in rows),
            "first_sample_at": rows[0]["sampled_at"] if rows else None,
            "last_sample_at": rows[-1]["sampled_at"] if rows else None,
            "scope": {
                "app": "verified Windows worker and actual descendant process working sets; excludes experiment controller and parent redirector",
                "postgres": "sum of all postgres process RSS in shared "
                + self.pg_container
                + "; not isolated database attribution; shared pages can be counted repeatedly",
                "redis": "sum of all redis-server process RSS in dedicated "
                + self.redis_container,
                "sum": "observations within one sample, read sequentially; sampled peaks, not a continuous physical-memory high-water mark",
            },
        }
