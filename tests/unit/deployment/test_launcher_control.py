"""T221：控制动作、控制台命令与设置运行时重启钩子。"""

from __future__ import annotations

import io
import sys
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from backend.app.deployment import windows_launcher as launcher
from backend.app.services.settings.management import SettingsRuntime

BASE_URL = "http://127.0.0.1:19492"


class _Client:
    def __init__(self) -> None:
        self.shutdown_calls = 0
        self.restart_calls = 0

    def open_settings(self, *, timeout: float) -> dict[str, Any]:
        del timeout
        return {"authorization_url": f"{BASE_URL}/#code=FIRST"}

    def reauthorize(self, *, timeout: float) -> dict[str, Any]:
        del timeout
        return {"authorization_url": f"{BASE_URL}/#code=SECOND"}

    def request_restart(self, *, timeout: float) -> dict[str, Any]:
        del timeout
        self.restart_calls += 1
        return {"type": "restart_accepted"}

    def shutdown(self, *, timeout: float) -> None:
        del timeout
        self.shutdown_calls += 1


class _Child:
    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode


def test_control_actions_open_and_reauthorize_hand_the_url_to_the_browser() -> None:
    opened: list[str] = []
    actions = launcher.ControlActions(
        _Client(),
        base_url=BASE_URL,
        timeout=1,
        opener=lambda url: opened.append(url) or True,
    )
    assert actions.open_settings().endswith("#code=FIRST")
    assert actions.reauthorize().endswith("#code=SECOND")
    assert opened == [f"{BASE_URL}/#code=FIRST", f"{BASE_URL}/#code=SECOND"]


def test_control_actions_request_restart_through_the_channel() -> None:
    client = _Client()
    actions = launcher.ControlActions(client, base_url=BASE_URL, timeout=1)
    assert actions.request_restart() is True
    assert client.restart_calls == 1


def test_console_control_reads_commands_and_exits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    opened: list[str] = []
    actions = launcher.ControlActions(
        _Client(),
        base_url=BASE_URL,
        timeout=1,
        opener=lambda url: opened.append(url) or True,
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("o\nr\nq\n"))
    assert launcher.run_console_control(actions, _Child()) == "user_exit"  # type: ignore[arg-type]
    assert opened == [f"{BASE_URL}/#code=FIRST", f"{BASE_URL}/#code=SECOND"]
    assert "o=打开设置" in capsys.readouterr().out


def test_console_control_exits_when_the_worker_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actions = launcher.ControlActions(_Client(), base_url=BASE_URL, timeout=1)
    monkeypatch.setattr(launcher, "_stdin_commands", lambda _queue: None)
    assert launcher.run_console_control(actions, _Child(returncode=0)) == "child_exited"  # type: ignore[arg-type]


def test_console_control_treats_closed_stdin_as_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actions = launcher.ControlActions(_Client(), base_url=BASE_URL, timeout=1)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert launcher.run_console_control(actions, _Child()) == "user_exit"  # type: ignore[arg-type]


class _Manifest:
    def __init__(self) -> None:
        self.active_revision = uuid4()
        self.pending_restart_revision = None


class _Config:
    def __init__(self, root: Any, manifest: _Manifest) -> None:
        self.root = root
        self._manifest = manifest

    def read_manifest(self) -> _Manifest:
        return self._manifest


def _runtime(tmp_path: Any, **kwargs: Any) -> SettingsRuntime:
    manifest = _Manifest()
    profile = SimpleNamespace(environment="development", environment_id=uuid4())
    return SettingsRuntime(
        profile=profile, config=_Config(tmp_path, manifest), **kwargs
    )


def test_runtime_without_channel_keeps_manual_restart(tmp_path: Any) -> None:
    runtime = _runtime(tmp_path)
    manifest = runtime.config.read_manifest()  # type: ignore[union-attr]
    status, reason, pending = runtime.restart(
        expected_revision=manifest.active_revision
    )
    assert status == "manual_restart_required"
    assert "Launcher" in reason
    assert pending is None


def test_runtime_with_channel_reports_restart_requested(tmp_path: Any) -> None:
    calls: list[str] = []
    runtime = _runtime(tmp_path, restart_channel=lambda: calls.append("x") or "accepted")
    manifest = runtime.config.read_manifest()  # type: ignore[union-attr]
    status, reason, _pending = runtime.restart(
        expected_revision=manifest.active_revision
    )
    assert status == "restart_requested"
    assert "受控重启" in reason
    assert calls == ["x"]


def test_runtime_channel_failure_falls_back_to_manual(tmp_path: Any) -> None:
    runtime = _runtime(tmp_path, restart_channel=lambda: "unavailable")
    manifest = runtime.config.read_manifest()  # type: ignore[union-attr]
    status, _reason, _pending = runtime.restart(
        expected_revision=manifest.active_revision
    )
    assert status == "manual_restart_required"

def test_console_reader_is_reused_across_worker_restart(monkeypatch):
    import queue
    first = launcher.ControlActions(_Client(), base_url=BASE_URL, timeout=1)
    second = launcher.ControlActions(_Client(), base_url=BASE_URL, timeout=1)
    commands = queue.Queue()
    first.console_commands = commands
    second.console_commands = commands
    def forbidden(*args, **kwargs):
        pytest.fail("must not start a second stdin reader for the same Launcher")
    monkeypatch.setattr(launcher.threading, "Thread", forbidden)
    assert launcher.run_console_control(first, _Child(returncode=75)) == "child_exited"
    commands.put("q")
    assert launcher.run_console_control(second, _Child()) == "user_exit"
