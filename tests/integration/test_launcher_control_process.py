"""T221 真实进程：匿名控制管道的注册、授权码与受控重启退出码。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from backend.app.deployment.control_channel import (
    FRAME_PREFIX,
    RESTART_EXIT_CODE,
    ControlClient,
    decode_frame,
    encode_frame,
)

ROOT = Path(__file__).resolve().parents[2]
STUB = ROOT / "tests" / "support" / "launcher_worker_stub.py"


def _spawn(port: int) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, str(STUB), str(port)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )


def _client(proc: subprocess.Popen[str], base_url: str) -> ControlClient:
    assert proc.stdout is not None and proc.stdin is not None
    return ControlClient(
        reader=proc.stdout, writer=proc.stdin, settings_base_url=base_url
    )


def _cleanup(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=10)
    for stream in (proc.stdin, proc.stdout):
        if stream is not None and not stream.closed:
            stream.close()


def test_real_pipe_registers_issues_codes_and_restarts_itself() -> None:
    port = 19491
    base_url = f"http://127.0.0.1:{port}"
    proc = _spawn(port)
    client = _client(proc, base_url)
    try:
        registration = client.wait_registration(
            timeout=30, settings_base_url=base_url
        )
        # venv python.exe may be a redirector; the worker reports the real
        # interpreter PID rather than the redirector Popen PID.
        assert isinstance(registration["pid"], int)
        assert registration["pid"] > 0
        assert registration["settings_base_url"] == base_url

        first = client.open_settings(timeout=15)
        second = client.reauthorize(timeout=15)
        assert str(first["authorization_url"]).startswith(base_url + "/#code=")
        assert first["code"] != second["code"]

        client.request_restart(timeout=15)
        assert proc.wait(timeout=20) == RESTART_EXIT_CODE
    finally:
        client.close()
        _cleanup(proc)


def test_real_pipe_eof_stops_the_worker_without_orphan() -> None:
    port = 19492
    base_url = f"http://127.0.0.1:{port}"
    proc = _spawn(port)
    client = _client(proc, base_url)
    try:
        client.wait_registration(timeout=30, settings_base_url=base_url)
        # 只关闭 Launcher 侧写端，模拟控制通道丢失。
        assert proc.stdin is not None
        proc.stdin.close()
        assert proc.wait(timeout=20) == 3
    finally:
        client.close()
        _cleanup(proc)


def test_worker_stdout_only_carries_control_frames() -> None:
    port = 19493
    base_url = f"http://127.0.0.1:{port}"
    proc = _spawn(port)
    try:
        assert proc.stdout is not None and proc.stdin is not None
        noise = proc.stdout.readline()
        assert not noise.startswith(FRAME_PREFIX)
        registration = decode_frame(proc.stdout.readline())
        assert registration is not None
        assert registration["type"] == "worker_registered"

        proc.stdin.write(
            encode_frame({"type": "open_settings", "settings_base_url": base_url})
        )
        proc.stdin.flush()
        grant = decode_frame(proc.stdout.readline())
        assert grant is not None and grant["type"] == "one_time_code_grant"
        assert "#code=" in str(grant["authorization_url"])

        proc.stdin.write(encode_frame({"type": "shutdown"}))
        proc.stdin.flush()
        ack = decode_frame(proc.stdout.readline())
        assert ack == {"type": "shutdown_accepted"}
        assert proc.wait(timeout=20) == 0
    finally:
        _cleanup(proc)
