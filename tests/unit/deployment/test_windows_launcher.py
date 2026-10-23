"""T221 启动器：Setup 优先、注册确认、受控重启与所有权边界。"""

from __future__ import annotations

import io
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import Mock

import pytest

from backend.app.deployment import windows_launcher as launcher
from backend.app.deployment.control_channel import ControlChannelError


class FakeChild:
    def __init__(self, codes: tuple[int, ...] = (0,)) -> None:
        self._codes = list(codes)
        self.pid = 4321
        self.stdin = io.StringIO()
        self.stdout = io.StringIO()
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            self.returncode = self._codes.pop(0) if self._codes else 0
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        if self.returncode is None:
            self.returncode = self._codes.pop(0) if self._codes else 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class FakeClient:
    instances: ClassVar[list[FakeClient]] = []

    def __init__(self, *, reader: object, writer: object, settings_base_url: str) -> None:
        self.settings_base_url = settings_base_url
        self.shutdown_calls = 0
        self.closed = False
        self.fail_registration = False
        FakeClient.instances.append(self)

    def wait_registration(self, *, timeout: float, settings_base_url: str) -> dict[str, Any]:
        if self.fail_registration:
            raise ControlChannelError("WORKER_BINDING_MISMATCH", "worker 身份不匹配。")
        return {"pid": 4321, "settings_base_url": settings_base_url}

    def open_settings(self, *, timeout: float) -> dict[str, Any]:
        return {"authorization_url": f"{self.settings_base_url}/#code=SYNTHETIC"}

    def reauthorize(self, *, timeout: float) -> dict[str, Any]:
        return {"authorization_url": f"{self.settings_base_url}/#code=SYNTHETIC2"}

    def request_restart(self, *, timeout: float) -> dict[str, Any]:
        return {"type": "restart_accepted"}

    def shutdown(self, *, timeout: float) -> None:
        self.shutdown_calls += 1

    def close(self) -> None:
        self.closed = True


def _prepare(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    *,
    codes: tuple[int, ...] = (0,),
    controls: list[str] | None = None,
) -> list[FakeChild]:
    profile = SimpleNamespace(
        storage_root=tmp_path / "storage",
        log_root=tmp_path / "logs",
        config_root=tmp_path / "config",
    )
    monkeypatch.setattr(launcher, "resolve_environment_profile", lambda: profile)
    monkeypatch.setattr(launcher, "prepare_runtime_paths", lambda _profile: None)
    monkeypatch.setattr(launcher, "check_storage", lambda *a, **k: None)
    monkeypatch.setattr(
        launcher, "environment_path", lambda *a, **k: tmp_path / "application.log"
    )
    monkeypatch.setattr(launcher, "port_available", lambda *a, **k: True)
    monkeypatch.setattr(launcher, "wait_setup_ready", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "ControlClient", FakeClient)
    FakeClient.instances.clear()
    children: list[FakeChild] = []

    def spawn(**_kwargs: Any) -> FakeChild:
        # `codes` describes the sequence across owned workers, not per worker.
        child = FakeChild(codes[len(children) :])
        children.append(child)
        return child

    monkeypatch.setattr(launcher, "_spawn_worker", spawn)
    script = list(controls or ["user_exit"])

    def run_control(
        _actions: launcher.ControlActions,
        child: FakeChild,
        *,
        window: bool,
    ) -> str:
        del window
        outcome = script.pop(0) if script else "user_exit"
        if outcome == "child_exited":
            child.returncode = codes[min(len(children) - 1, len(codes) - 1)]
        return outcome

    monkeypatch.setattr(launcher, "run_control", run_control)
    return children


def test_storage_probe_leaves_no_temporary_file(tmp_path: Any) -> None:
    launcher.check_storage(tmp_path / "data", frozen=False)
    assert (tmp_path / "data" / "logs").is_dir()
    assert not list((tmp_path / "data").glob(".write-check-*"))


def test_owned_shutdown_never_touches_another_process() -> None:
    owned = Mock()
    owned.poll.return_value = None
    other = Mock()
    launcher.stop_owned(owned)
    owned.terminate.assert_called_once()
    owned.wait.assert_called_once()
    other.terminate.assert_not_called()


def test_launcher_has_no_plaintext_or_preflight_startup_path() -> None:
    # T221：旧 .env 装载、Tk 编辑器、模型/DB/迁移预检不再属于启动链。
    for retired in (
        "configure",
        "check_models",
        "check_dependencies",
        "check_network",
        "migrate",
    ):
        assert not hasattr(launcher, retired)


def test_launcher_starts_setup_and_opens_settings_without_printing_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    children = _prepare(monkeypatch, tmp_path)
    opened: list[str] = []
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: opened.append(url) or True)

    assert launcher.main(["--port", "19492"]) == 0
    assert len(children) == 1
    assert opened and opened[0].endswith("#code=SYNTHETIC")
    # 一次性码只进浏览器 fragment，不写控制台。
    assert "#code=" not in capsys.readouterr().out
    assert FakeClient.instances[0].shutdown_calls == 1
    assert children[0].returncode == 0
    assert not children[0].terminated


def test_launcher_ignores_legacy_config_argument(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    _prepare(monkeypatch, tmp_path)
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: True)
    legacy = tmp_path / "config.env"
    legacy.write_text("DATABASE_URL=postgresql+psycopg://user:secret@127.0.0.1/db\n", encoding="utf-8")

    assert launcher.main(["--port", "19492", "--config", str(legacy)]) == 0
    out = capsys.readouterr().out
    assert "已忽略 --config" in out
    assert "secret" not in out


def test_launcher_restarts_only_its_own_worker_after_restart_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    children = _prepare(
        monkeypatch, tmp_path, codes=(75, 0), controls=["child_exited", "user_exit"]
    )
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: True)
    assert launcher.main(["--port", "19492", "--no-browser"]) == 0
    assert len(children) == 2
    assert children[0].returncode == 75
    # The restart exit is observed, not forced: no terminate/kill on that worker.
    assert not children[0].terminated


def test_launcher_refuses_occupied_port_without_spawning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    children = _prepare(monkeypatch, tmp_path)
    monkeypatch.setattr(launcher, "port_available", lambda *a, **k: False)
    assert launcher.main(["--port", "19492"]) == 1
    assert children == []
    assert "已被占用" in capsys.readouterr().err


def test_registration_failure_stops_the_owned_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    children = _prepare(monkeypatch, tmp_path)
    original = FakeClient.wait_registration

    def fail(self: FakeClient, *, timeout: float, settings_base_url: str) -> dict[str, Any]:
        del self, timeout, settings_base_url
        raise ControlChannelError("WORKER_BINDING_MISMATCH", "身份不一致。")

    monkeypatch.setattr(FakeClient, "wait_registration", fail)
    try:
        assert launcher.main(["--port", "19492"]) == 1
    finally:
        monkeypatch.setattr(FakeClient, "wait_registration", original)
    assert children[0].terminated
    assert "控制通道" in capsys.readouterr().err


def test_launcher_rejects_non_loopback_bind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _prepare(monkeypatch, tmp_path)
    monkeypatch.setenv("EDUAGENT_BIND_HOST", "0.0.0.0")
    try:
        assert launcher.main(["--port", "19492"]) == 1
    finally:
        monkeypatch.delenv("EDUAGENT_BIND_HOST", raising=False)


def test_setup_ready_probes_only_the_setup_endpoint() -> None:
    observed: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            observed.append(self.path)
            self.send_response(200 if self.path == "/setup-ready" else 503)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert launcher.setup_ready(f"http://127.0.0.1:{server.server_port}")
        assert observed == ["/setup-ready"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_wait_setup_ready_reports_child_exit() -> None:
    child = FakeChild()
    child.returncode = 2
    with pytest.raises(launcher.LaunchError, match="应用启动"):
        launcher.wait_setup_ready(child, "http://127.0.0.1:1", 1)


def test_port_available_detects_bound_socket() -> None:
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", 0))
        probe.listen(1)
        port = probe.getsockname()[1]
        assert launcher.port_available("127.0.0.1", port) is False
    finally:
        probe.close()


def test_console_command_failure_never_prints_the_authorization_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    actions = launcher.ControlActions(
        FakeClient(reader=None, writer=None, settings_base_url="http://127.0.0.1:19492"),
        base_url="http://127.0.0.1:19492",
        timeout=1,
        opener=lambda _url: False,
    )
    assert launcher._handle_command("o", actions) is None
    out = capsys.readouterr().out
    assert "http://127.0.0.1:19492/" in out
    assert "#code=" not in out


def test_console_quit_maps_to_user_exit() -> None:
    actions = launcher.ControlActions(
        FakeClient(reader=None, writer=None, settings_base_url="http://127.0.0.1:19492"),
        base_url="http://127.0.0.1:19492",
        timeout=1,
        opener=lambda _url: True,
    )
    assert launcher._handle_command("q", actions) == "user_exit"


def test_non_loopback_settings_base_url_is_rejected() -> None:
    from backend.app.deployment.local_management import (
        LocalManagementError,
        settings_base_url,
    )

    with pytest.raises(LocalManagementError) as error:
        settings_base_url("0.0.0.0", 8000)
    assert error.value.code == "SETTINGS_BIND_NOT_LOOPBACK"
    assert settings_base_url("::1", 8000) == "http://[::1]:8000"
    assert settings_base_url("localhost", 8000) == "http://localhost:8000"


def test_frozen_worker_uses_bundled_ui_and_windows_identity(monkeypatch, tmp_path):
    import argparse

    captured = {}
    child = FakeChild()
    def spawn(command, **kwargs):
        captured.update(command=command, **kwargs)
        return child
    monkeypatch.setattr(launcher.sys, "frozen", True, raising=False)
    monkeypatch.setattr(launcher.sys, "_MEIPASS", str(tmp_path / "_internal"), raising=False)
    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    monkeypatch.setenv("EDUAGENT_SETTINGS_UI_ROOT", "untrusted-relative")
    assert launcher._spawn_worker(args=argparse.Namespace(port=19492), host="127.0.0.1", log=io.StringIO()) is child
    assert captured["env"]["EDUAGENT_SETTINGS_UI_ROOT"] == str(tmp_path / "_internal" / "settings-ui")
    assert captured["env"]["EDUAGENT_RUNTIME_KIND"] == "windows"
    assert captured["command"][1:3] == ["--child", "serve"]
    assert "--config" not in captured["command"]


def test_graceful_shutdown_timeout_still_stops_only_owned_worker(monkeypatch, tmp_path):
    children = _prepare(monkeypatch, tmp_path)
    original_wait = FakeChild.wait
    def wait(self, timeout=None):
        if timeout == 5 and not self.terminated:
            raise launcher.subprocess.TimeoutExpired("owned-worker", timeout)
        return original_wait(self, timeout)
    monkeypatch.setattr(FakeChild, "wait", wait)
    assert launcher.main(["--no-browser", "--no-window"]) == 0
    assert children[0].terminated


def test_worker_installs_existing_tls_loader_before_runtime_assembly(monkeypatch):
    from backend.app.deployment import windows_tls
    from backend.app.services.settings import management

    steps = []

    class AssemblyReached(Exception):
        pass

    def assemble(*args, **kwargs):
        assert steps == ["tls"]
        raise AssemblyReached

    monkeypatch.setattr(windows_tls, "install_windows_tls_loader", lambda: steps.append("tls"))
    monkeypatch.setattr(management, "build_settings_runtime", assemble)
    monkeypatch.setattr(launcher, "resolve_environment_profile", lambda: object())
    monkeypatch.setattr(launcher.sys, "stdout", io.StringIO())
    with pytest.raises(AssemblyReached):
        launcher.serve(19492, bind_host="127.0.0.1")


@pytest.mark.parametrize("frozen,platform,expected", [
    (True, "win32", ["numpy", "tls"]),
    (False, "win32", ["tls"]),
    (True, "linux", ["tls"]),
])
def test_frozen_windows_worker_initializes_numpy_on_main_thread_before_assembly(
    monkeypatch, frozen, platform, expected,
):
    import builtins

    from backend.app.deployment import windows_tls
    from backend.app.services.settings import management

    steps = []
    original_import = builtins.__import__

    class AssemblyReached(Exception):
        pass

    def tracked_import(name, *args, **kwargs):
        if name == "numpy":
            assert threading.current_thread() is threading.main_thread()
            steps.append("numpy")
            return object()
        return original_import(name, *args, **kwargs)

    def assemble(*args, **kwargs):
        assert steps == expected
        raise AssemblyReached

    monkeypatch.setattr(launcher.sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(launcher.sys, "platform", platform)
    monkeypatch.setattr(builtins, "__import__", tracked_import)
    monkeypatch.setattr(windows_tls, "install_windows_tls_loader", lambda: steps.append("tls"))
    monkeypatch.setattr(management, "build_settings_runtime", assemble)
    monkeypatch.setattr(launcher, "resolve_environment_profile", lambda: object())
    monkeypatch.setattr(launcher.sys, "stdout", io.StringIO())
    with pytest.raises(AssemblyReached):
        launcher.serve(19492, bind_host="127.0.0.1")
