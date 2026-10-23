"""Windows 本机启动器：Setup 优先、自有 worker 与匿名控制管道（T221）。

启动顺序固定为：

1. 只准备环境路径与持久目录（不加载 `.env`、不探测模型/数据库、不执行迁移）；
2. 用 `subprocess` 启动自有 worker，stdout 作为控制帧、stdin 作为命令、stderr 写
   `application.log`，Setup HTTP 立即对外可用；
3. 等待 worker 的注册确认，再校验 `/setup-ready`，确认后才打开带一次性码 fragment
   的设置页；
4. 保留最小控制窗口（打开设置/重新授权/退出）；退出只停止自有 worker。

模型、数据库、迁移与恢复由 T218–T220 的 Bootstrap/Readiness/设置 API 负责，Launcher
不再重复执行；旧 `.env` 只能由设置页显式导入。Tk 明文配置编辑器不再是默认路径，
`--configure` 只打开设置页。控制通道与所有权语义见 `control_channel`。
"""

from __future__ import annotations

import argparse
import functools
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import TextIO
from urllib.request import ProxyHandler, build_opener

from backend.app.core.bootstrap import listen_address
from backend.app.core.environment import (
    environment_path,
    prepare_runtime_paths,
    resolve_environment_profile,
)
from backend.app.core.settings_security import build_process_setup_security
from backend.app.deployment.control_channel import (
    RESTART_EXIT_CODE,
    ControlChannelError,
    ControlClient,
    ControlServer,
    RestartCoordinator,
)
from backend.app.deployment.local_management import (
    LOOPBACK_HOSTS,
    settings_base_url,
)

_LOCAL_HTTP = build_opener(ProxyHandler({}))

#: 连续自动重启上限；超过后保留失败现场并停止，不做无限重启。
MAX_CONSECUTIVE_RESTARTS = 3


class LaunchError(RuntimeError):
    def __init__(self, step: str, detail: str = "", action: str = "") -> None:
        self.step = step
        super().__init__(f"[{step}] {detail}；下一步：{action}")


def resource_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[3]))


def check_storage(root: Path, *, frozen: bool, log_root: Path | None = None) -> None:
    if frozen and (
        root.is_relative_to(resource_root())
        or root.is_relative_to(Path(sys.executable).parent)
    ):
        raise LaunchError(
            "持久目录", "STORAGE_ROOT 位于安装资源目录", "设置独立用户数据目录"
        )
    try:
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".write-check-", dir=root) as stream:
            stream.write(b"EduAgent")
            stream.flush()
        (log_root if log_root is not None else root / "logs").mkdir(
            parents=True, exist_ok=True
        )
    except OSError as exc:
        raise LaunchError(
            "持久目录", type(exc).__name__, "检查 STORAGE_ROOT 的写入权限与可用磁盘"
        ) from exc


def setup_ready(base_url: str, *, timeout: float = 2.0) -> bool:
    """Setup Ready 探测；业务未装配时仍应 200。"""

    try:
        with _LOCAL_HTTP.open(f"{base_url}/setup-ready", timeout=timeout) as response:
            return response.status == 200
    except OSError:
        return False


def wait_setup_ready(
    child: subprocess.Popen[str], base_url: str, timeout: float
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise LaunchError(
                "应用启动",
                f"所属进程已退出，退出码 {child.returncode}",
                "查看当前环境日志目录中的 application.log",
            )
        if setup_ready(base_url):
            return
        time.sleep(0.2)
    raise LaunchError(
        "应用就绪",
        "Setup 服务未在限定时间内可用",
        "查看 application.log 与运行环境/数据目录配置",
    )


def port_available(host: str, port: int) -> bool:
    """仅探测端口是否可绑定；不连接、不枚举、不终止任何既有进程。"""

    with socket.socket() as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def stop_owned(child: subprocess.Popen[str]) -> None:
    """只停止本启动器创建的子进程；不按端口或进程名查找其他进程。"""

    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


class ControlActions:
    """控制窗口/控制台共用的动作，便于不打开 GUI 的聚焦测试。"""

    def __init__(
        self,
        client: ControlClient,
        *,
        base_url: str,
        timeout: float,
        opener: Callable[[str], bool] | None = None,
    ) -> None:
        self.client = client
        self.base_url = base_url
        self.timeout = timeout
        self._opener = opener or webbrowser.open
        self.last_url: str | None = None
        self.console_commands: queue.Queue[str] | None = None
    def _open(self, grant: dict[str, object]) -> str:
        url = grant.get("authorization_url")
        if not isinstance(url, str):
            raise ControlChannelError(
                "AUTHORIZATION_URL_MISSING", "授权帧缺少设置页地址。"
            )
        self.last_url = url
        try:
            opened = bool(self._opener(url))
        except Exception:  # noqa: BLE001 - 浏览器失败只降级为提示，不外泄授权码
            opened = False
        if not opened:
            # 不打印带 fragment 的地址：那会把一次性码写进控制台/日志。
            print(
                f"浏览器未打开，请在控制窗口重新授权后手动访问 {self.base_url}/",
                flush=True,
            )
        return url

    def open_settings(self) -> str:
        return self._open(self.client.open_settings(timeout=self.timeout))

    def reauthorize(self) -> str:
        return self._open(self.client.reauthorize(timeout=self.timeout))

    def request_restart(self) -> bool:
        self.client.request_restart(timeout=self.timeout)
        return True


def _stdin_commands(target: queue.Queue[str]) -> None:
    try:
        for line in sys.stdin:
            target.put(line.strip().lower())
    except (OSError, ValueError):
        target.put("q")
    else:
        # EOF 也是控制台退出信号；唤醒控制循环以复用 user_exit 收尾。
        target.put("")


def _handle_command(command: str, actions: ControlActions) -> str | None:
    if command in {"o", "open", "打开设置"}:
        try:
            actions.open_settings()
        except ControlChannelError as exc:
            print(f"打开设置失败：{exc.code}；请稍后重试或退出后重新启动。", flush=True)
        return None
    if command in {"r", "reauthorize", "重新授权"}:
        try:
            actions.reauthorize()
        except ControlChannelError as exc:
            print(f"重新授权失败：{exc.code}；请稍后重试。", flush=True)
        return None
    if command in {"q", "quit", "exit", "退出", ""}:
        return "user_exit"
    print("可用命令：o=打开设置，r=重新授权，q=退出。", flush=True)
    return None


def run_console_control(
    actions: ControlActions, child: subprocess.Popen[str]
) -> str:
    commands = actions.console_commands
    if commands is None:
        commands = queue.Queue()
        actions.console_commands = commands
        threading.Thread(
            target=_stdin_commands, args=(commands,), name="eduagent-console", daemon=True
        ).start()
    print(
        "控制台模式：o=打开设置，r=重新授权，q=退出。Ctrl+C 也会停止所属进程。",
        flush=True,
    )
    while True:
        try:
            command = commands.get(timeout=0.2)
        except queue.Empty:
            if child.poll() is not None:
                return "child_exited"
            continue
        result = _handle_command(command, actions)
        if result is not None:
            return result


def run_control_window(actions: ControlActions, child: subprocess.Popen[str]) -> str:
    """最小 Tk 控制窗口；关闭窗口等同退出并停止自有 worker。"""

    import tkinter as tk

    result = {"reason": "user_exit"}
    commands: queue.Queue[str] = queue.Queue()
    root = tk.Tk()

    def poll() -> None:
        if child.poll() is not None:
            result["reason"] = "child_exited"
            root.destroy()
            return
        try:
            command = commands.get_nowait()
        except queue.Empty:
            root.after(200, poll)
            return
        if _handle_command(command, actions) == "user_exit":
            root.destroy()
            return
        root.after(200, poll)

    root.title("EduAgent")
    root.geometry("360x150")
    tk.Label(root, text="EduAgent 本机控制").pack(pady=6)
    buttons = tk.Frame(root)
    buttons.pack(pady=4)
    for label, command in (
        ("打开设置", "o"),
        ("重新授权", "r"),
        ("退出", "q"),
    ):
        tk.Button(
            buttons,
            text=label,
            width=10,
            command=functools.partial(commands.put, command),
        ).pack(side=tk.LEFT, padx=4)
    tk.Label(root, text=f"设置地址：{actions.base_url}/").pack(pady=6)
    root.after(200, poll)
    try:
        root.mainloop()
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass
    return result["reason"]


def run_control(
    actions: ControlActions,
    child: subprocess.Popen[str],
    *,
    window: bool,
) -> str:
    if window:
        return run_control_window(actions, child)
    return run_console_control(actions, child)


def _register_when_started(
    server: object, control: ControlServer, timeout: float
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if bool(getattr(server, "started", False)):
            control.write(control.registration())
            return
        time.sleep(0.05)


def serve(
    port: int, *, bind_host: str, await_preflight: bool = False
) -> int:
    """worker 入口：先生成应用（Setup 立即可用），再挂控制通道并运行 HTTP。"""

    del await_preflight  # T221 起不再有 SERVE 握手；参数保留以兼容旧命令行。
    if getattr(sys, "frozen", False) and sys.platform == "win32":
        # NumPy's first native import stalls in a frozen OCR worker. Initialize
        # the bundled numeric runtime on the main thread before serving work;
        # OCR providers and external model weights still load on explicit use.
        import numpy  # noqa: F401

    from backend.app.deployment.windows_tls import install_windows_tls_loader

    # Preserve the existing Windows CA loader before business HTTP clients import.
    install_windows_tls_loader()
    import uvicorn

    from backend.app.core.bootstrap import create_application
    from backend.app.services.settings.management import build_settings_runtime

    control_out = sys.stdout
    # 控制帧只走专用 stdout；第三方库/uvicorn 的打印全部转到 stderr（日志）。
    if control_out is None:
        raise LaunchError("控制通道", "缺少可用的标准输出", "以 EXE 或脚本方式启动")
    sys.stdout = sys.stderr

    profile = resolve_environment_profile()
    coordinator = RestartCoordinator()
    runtime = build_settings_runtime(profile, restart_channel=coordinator.request)
    application = create_application(settings_runtime=runtime)
    security = getattr(application.state, "setup_security", None)
    if security is None:
        security = build_process_setup_security()
        application.state.setup_security = security
    base_url = settings_base_url(bind_host, port)

    config = uvicorn.Config(
        application, host=bind_host, port=port, workers=1, access_log=False
    )
    server = uvicorn.Server(config)
    coordinator.bind(lambda: setattr(server, "should_exit", True))

    def _lose_channel() -> None:
        # Launcher 消失时有序退出；不继续对外提供无人监管的设置面。
        server.should_exit = True

    control = ControlServer(
        security,
        settings_base_url=base_url,
        reader=sys.stdin,
        writer=control_out,
        on_restart=lambda: setattr(server, "should_exit", True),
        on_shutdown=lambda: setattr(server, "should_exit", True),
        on_channel_lost=_lose_channel,
    )
    control.start()
    threading.Thread(
        target=_register_when_started,
        args=(server, control, 30.0),
        name="eduagent-register",
        daemon=True,
    ).start()
    server.run()
    return RESTART_EXIT_CODE if coordinator.requested else 0


def _spawn_worker(
    *,
    args: argparse.Namespace,
    host: str,
    log: TextIO,
) -> subprocess.Popen[str]:
    command = [sys.executable]
    if not getattr(sys, "frozen", False):
        command.append(str(resource_root() / "scripts/launch_windows.py"))
    command += [
        "--child",
        "serve",
        "--port",
        str(args.port),
        "--bind-host",
        host,
    ]
    env = os.environ.copy()
    env["GRADIO_ANALYTICS_ENABLED"] = "False"
    env["PYTHONIOENCODING"] = "utf-8"
    env["EDUAGENT_BIND_HOST"] = host
    env["EDUAGENT_BIND_PORT"] = str(args.port)
    if getattr(sys, "frozen", False):
        env["EDUAGENT_RUNTIME_KIND"] = "windows"
        env["EDUAGENT_SETTINGS_UI_ROOT"] = str(resource_root() / "settings-ui")
    return subprocess.Popen(
        command,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=log,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )


def _parse_bind(port: int) -> str:
    host, _ = listen_address()
    if host not in LOOPBACK_HOSTS:
        raise LaunchError(
            "监听地址",
            f"启动器只允许回环监听，当前为 {host}",
            "移除 EDUAGENT_BIND_HOST 或设为 127.0.0.1",
        )
    return host


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="EduAgent Windows 单机启动器；退出只停止所属应用进程"
    )
    parser.add_argument(
        "--config", type=Path, help="已退役：旧 .env 请在设置页显式导入"
    )
    parser.add_argument(
        "--configure",
        action="store_true",
        help="启动设置服务并打开设置页（不再编辑明文配置文件）",
    )
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--startup-timeout", type=float, default=60)
    parser.add_argument("--restart-timeout", type=float, default=30)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--browser-pipe-handle", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--no-window", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--child", choices=["serve"], help=argparse.SUPPRESS)
    parser.add_argument("--bind-host", default="127.0.0.1", help=argparse.SUPPRESS)
    parser.add_argument(
        "--await-preflight", action="store_true", help=argparse.SUPPRESS
    )
    args = parser.parse_args(argv)
    if args.child:
        return serve(
            args.port, bind_host=args.bind_host, await_preflight=args.await_preflight
        )

    child: subprocess.Popen[str] | None = None
    client: ControlClient | None = None
    log_path: Path | None = None
    browser_pipe = None
    try:
        if args.browser_pipe_handle is not None:
            from backend.app.deployment.browser_pipe import BrowserPipe

            browser_pipe = BrowserPipe(args.browser_pipe_handle)
        if not 1 <= args.port <= 65535 or args.startup_timeout <= 0:
            raise LaunchError(
                "启动参数", "端口或超时无效", "端口应为1–65535，超时应大于0"
            )
        if args.config is not None:
            print(
                "已忽略 --config：启动器不再加载明文 .env；"
                "请在设置页“旧配置导入”中显式接纳。",
                flush=True,
            )
        host = _parse_bind(args.port)
        base_url = settings_base_url(host, args.port)
        profile = resolve_environment_profile()
        prepare_runtime_paths(profile)
        check_storage(
            profile.storage_root,
            frozen=bool(getattr(sys, "frozen", False)),
            log_root=profile.log_root,
        )
        if not port_available(host, args.port):
            raise LaunchError(
                "服务端口",
                "已被占用",
                "如已有 EduAgent 在运行，请回到原 Launcher 控制窗口；"
                "启动器不会停止无关进程",
            )
        log_path = environment_path(profile, "logs", "application.log")
        open_on_start = (not args.no_browser) or args.configure
        restarts = 0
        console_commands: queue.Queue[str] | None = None
        with log_path.open("a", encoding="utf-8") as log:
            while True:
                child = _spawn_worker(args=args, host=host, log=log)
                client = ControlClient(
                    reader=child.stdout,  # type: ignore[arg-type]
                    writer=child.stdin,  # type: ignore[arg-type]
                    settings_base_url=base_url,
                )
                client.wait_registration(
                    timeout=args.startup_timeout, settings_base_url=base_url
                )
                wait_setup_ready(child, base_url, args.startup_timeout)
                actions = ControlActions(
                    client, base_url=base_url, timeout=args.restart_timeout,
                    opener=browser_pipe,
                )
                print(
                    f"设置服务已就绪：{base_url}/；所属 PID={child.pid}",
                    flush=True,
                )
                if open_on_start:
                    actions.open_settings()
                    open_on_start = False
                actions.console_commands = console_commands
                reason = run_control(
                    actions, child, window=not args.no_window
                )
                console_commands = actions.console_commands
                if reason == "user_exit":
                    try:
                        client.shutdown(timeout=5)
                    except ControlChannelError:
                        pass  # 退出路径不以通道错误掩盖真实停止原因
                    else:
                        try:
                            child.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            pass  # 超时后才终止自有进程；正常退出保留 worker 的真实返回码。
                    stop_owned(child)
                else:
                    client.close()
                code = child.wait()
                if code == RESTART_EXIT_CODE and restarts < MAX_CONSECUTIVE_RESTARTS:
                    restarts += 1
                    print("正在按请求重启应用…", flush=True)
                    client = None
                    continue
                return code or 0
    except KeyboardInterrupt:
        print("正在停止所属应用进程…", flush=True)
        return 0
    except (LaunchError, ControlChannelError) as exc:
        message = (
            f"启动失败：{exc}"
            if isinstance(exc, LaunchError)
            else f"启动失败：[控制通道] {exc.code}"
        )
        print(message, file=sys.stderr, flush=True)
        if log_path is not None:
            try:
                with log_path.open("a", encoding="utf-8") as log:
                    log.write(message + "\n")
            except OSError:
                pass  # Keep the original failure if its log cannot be written.
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI boundary must redact credentials
        if isinstance(exc, OSError) and exc.filename:
            print(f"失败资源：{exc.filename}", file=sys.stderr, flush=True)
        print(
            f"启动失败：[启动器] {type(exc).__name__}；检查运行环境、持久目录与应用日志。",
            file=sys.stderr,
            flush=True,
        )
        if args.child:
            traceback.print_exc(file=sys.stderr)
        return 1
    finally:
        if browser_pipe is not None:
            browser_pipe.close()
        if client is not None:
            client.close()
        if child is not None:
            for stream in (child.stdin, child.stdout):
                if stream is not None and not stream.closed:
                    try:
                        stream.close()
                    except OSError:
                        pass
            stop_owned(child)


if __name__ == "__main__":
    raise SystemExit(main())
