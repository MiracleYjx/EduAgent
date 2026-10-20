"""Launcher 与自有 worker 之间的匿名控制管道（T221）。

设计边界：

- 通道只在父 Launcher 与它自己用 ``Popen`` 启动的 worker 之间建立，没有监听端口、
  没有命名管道、也没有新的后台服务；Launcher 退出即关闭管道，worker 读到 EOF 后
  有序退出，避免遗留孤儿进程。
- worker 的 stdout 只承载带固定前缀的控制帧，stderr 继续写 ``application.log``；
  worker 启动后立刻把 ``sys.stdout`` 指回 stderr，第三方库的打印不会污染协议。
- 一次码只出现在授权帧里，帧不解码到日志、不写 argv、不进查询串。错误帧只含稳定码。
- 帧有长度上限与类型白名单；未知类型、畸形 JSON、超长帧都收敛为安全错误，不执行。
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any, TextIO
from uuid import UUID

from backend.app.core.settings_security import SetupSecurityService
from backend.app.deployment.local_management import (
    LocalManagementAuthority,
    LocalManagementError,
)

#: 控制帧前缀；worker stdout 里的其他行一律忽略（不解析、不回显）。
FRAME_PREFIX = "EDUAGENT-CONTROL/1 "
MAX_FRAME_BYTES = 64 * 1024
#: worker 因“受控重启”退出时使用的退出码；Launcher 只据此重启自己的 worker。
RESTART_EXIT_CODE = 75

AUTHORIZATION_MESSAGES = frozenset({"open_settings", "reauthorize"})
CLIENT_MESSAGES = AUTHORIZATION_MESSAGES | {"shutdown", "restart"}
SERVER_MESSAGES = frozenset(
    {
        "worker_registered",
        "one_time_code_grant",
        "shutdown_accepted",
        "restart_accepted",
        "control_error",
    }
)


class ControlChannelError(RuntimeError):
    """控制通道的安全错误；消息只含稳定码，不含授权码或令牌。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def encode_frame(payload: Mapping[str, object]) -> str:
    """把一帧编码为单行；超长帧在写入前拒绝。"""

    kind = payload.get("type")
    if not isinstance(kind, str) or not kind:
        raise ControlChannelError("CONTROL_FRAME_INVALID", "控制帧缺少消息类型。")
    body = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"))
    frame = f"{FRAME_PREFIX}{body}\n"
    if len(frame.encode("utf-8")) > MAX_FRAME_BYTES:
        raise ControlChannelError("CONTROL_FRAME_TOO_LARGE", "控制帧超过长度上限。")
    return frame


def decode_frame(line: str) -> dict[str, Any] | None:
    """解码一行；非控制行返回 ``None``，畸形/超长控制帧抛安全错误。"""

    text = line.rstrip("\r\n")
    if not text.startswith(FRAME_PREFIX):
        return None
    if len(text.encode("utf-8")) > MAX_FRAME_BYTES:
        raise ControlChannelError("CONTROL_FRAME_TOO_LARGE", "控制帧超过长度上限。")
    body = text[len(FRAME_PREFIX) :]
    try:
        payload = json.loads(body)
    except ValueError:
        raise ControlChannelError("CONTROL_FRAME_INVALID", "控制帧不是合法 JSON。") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
        raise ControlChannelError("CONTROL_FRAME_INVALID", "控制帧缺少消息类型。")
    return payload


def _error(code: str, message: str) -> dict[str, object]:
    return {"type": "control_error", "code": code, "message": message}


class ControlServer:
    """worker 侧：登记本进程并处理 Launcher 的受控请求。"""

    def __init__(
        self,
        security: SetupSecurityService,
        *,
        settings_base_url: str,
        reader: TextIO,
        writer: TextIO,
        on_restart: Callable[[], None] | None = None,
        on_shutdown: Callable[[], None] | None = None,
        on_channel_lost: Callable[[], None] | None = None,
    ) -> None:
        self.authority = LocalManagementAuthority(security)
        self.settings_base_url = settings_base_url
        self._reader = reader
        self._writer = writer
        self._on_restart = on_restart
        self._on_shutdown = on_shutdown
        self._on_channel_lost = on_channel_lost
        self._stop = threading.Event()
        self._write_lock = threading.Lock()

    # -- 帧输出 -----------------------------------------------------------

    def registration(self) -> dict[str, object]:
        """登记确认：只含非敏感身份，不含授权码。"""

        return {
            "type": "worker_registered",
            "pid": _process_id(),
            "environment_id": str(self.authority.security.environment_id),
            "instance_id": str(self.authority.security.instance_id),
            "settings_base_url": self.settings_base_url,
        }

    def write(self, payload: Mapping[str, object]) -> None:
        try:
            frame = encode_frame(payload)
        except ControlChannelError:
            frame = encode_frame(_error("CONTROL_FRAME_INVALID", "控制帧无法编码。"))
        with self._write_lock:
            try:
                self._writer.write(frame)
                self._writer.flush()
            except (OSError, ValueError):
                # Launcher 已断开：按“通道丢失”处理，不把授权码写入任何日志。
                self._stop.set()
                if self._on_channel_lost is not None:
                    self._on_channel_lost()

    # -- 帧处理 -----------------------------------------------------------

    def _require_base_url(self, payload: Mapping[str, Any]) -> None:
        supplied = payload.get("settings_base_url")
        if supplied is not None and supplied != self.settings_base_url:
            raise ControlChannelError(
                "AUTHORIZATION_BASE_URL_MISMATCH",
                "设置页地址与本进程实际监听地址不一致。",
            )

    def handle(
        self, payload: Mapping[str, Any]
    ) -> tuple[dict[str, object], Callable[[], None] | None]:
        """返回（响应帧，响应后的动作）；动作在响应写出后才执行。"""

        kind = payload.get("type")
        if not isinstance(kind, str) or kind not in CLIENT_MESSAGES:
            return _error("CONTROL_MESSAGE_UNSUPPORTED", "不支持的控制消息类型。"), None
        if kind in AUTHORIZATION_MESSAGES:
            try:
                self._require_base_url(payload)
                grant = self.authority.handle_control_message(payload)
            except ControlChannelError as exc:
                return _error(exc.code, exc.message), None
            except LocalManagementError as exc:
                return _error(exc.code, exc.message), None
            return grant, None
        if kind == "shutdown":
            return {"type": "shutdown_accepted"}, self._request_shutdown
        if self._on_restart is None:
            return _error("RESTART_UNAVAILABLE", "本进程未连接受控重启通道。"), None
        return {"type": "restart_accepted"}, self._request_restart

    def _request_shutdown(self) -> None:
        self._stop.set()
        if self._on_shutdown is not None:
            self._on_shutdown()

    def _request_restart(self) -> None:
        self._stop.set()
        if self._on_restart is not None:
            self._on_restart()

    def serve_forever(self) -> None:
        """读命令直到停止、EOF 或通道丢断；EOF 视为 Launcher 已退出。"""

        for line in self._reader:
            if self._stop.is_set():
                return
            try:
                payload = decode_frame(line)
            except ControlChannelError as exc:
                self.write(_error(exc.code, exc.message))
                continue
            if payload is None:
                continue
            response, action = self.handle(payload)
            self.write(response)
            if action is not None:
                action()
                return
        if not self._stop.is_set():
            self._stop.set()
            if self._on_channel_lost is not None:
                self._on_channel_lost()

    def start(self) -> threading.Thread:
        thread = threading.Thread(
            target=self.serve_forever, name="eduagent-control", daemon=True
        )
        thread.start()
        return thread


class ControlClient:
    """Launcher 侧：只与自有 worker 交换控制帧。"""

    def __init__(
        self, *, reader: TextIO, writer: TextIO, settings_base_url: str
    ) -> None:
        self._reader = reader
        self._writer = writer
        self.settings_base_url = settings_base_url
        self._condition = threading.Condition()
        self._frames: deque[dict[str, Any]] = deque()
        self._closed = False
        self._failure: str | None = None
        self._registration: dict[str, Any] | None = None
        self._thread = threading.Thread(
            target=self._read_loop, name="eduagent-control-reader", daemon=True
        )
        self._thread.start()

    # -- 读取 -------------------------------------------------------------

    def _read_loop(self) -> None:
        try:
            for line in self._reader:
                try:
                    payload = decode_frame(line)
                except ControlChannelError as exc:
                    self._fail(exc.code)
                    return
                if payload is None:
                    continue
                self._push(payload)
        finally:
            self._fail("CONTROL_CHANNEL_CLOSED")

    def _push(self, payload: dict[str, Any]) -> None:
        with self._condition:
            self._frames.append(payload)
            self._condition.notify_all()

    def _fail(self, code: str) -> None:
        with self._condition:
            if self._failure is None:
                self._failure = code
            self._closed = True
            self._condition.notify_all()

    def _next(self, timeout: float) -> dict[str, Any]:
        with self._condition:
            if not self._frames and not self._closed:
                self._condition.wait(timeout)
            if self._frames:
                return self._frames.popleft()
            code = self._failure or "CONTROL_CHANNEL_CLOSED"
        raise ControlChannelError(code, "控制通道已关闭或超时。")

    # -- 命令 -------------------------------------------------------------

    def _send(self, payload: Mapping[str, object]) -> None:
        try:
            self._writer.write(encode_frame(payload))
            self._writer.flush()
        except (OSError, ValueError):
            raise ControlChannelError(
                "CONTROL_CHANNEL_CLOSED", "无法写入控制通道。"
            ) from None

    def wait_registration(
        self, *, timeout: float, settings_base_url: str
    ) -> dict[str, Any]:
        """等待注册确认；确认后才允许打开设置页。"""

        deadline = _deadline(timeout)
        while True:
            payload = self._next(_remaining(deadline))
            if payload.get("type") == "worker_registered":
                _validate_registration(payload, settings_base_url)
                with self._condition:
                    self._registration = payload
                return payload
            if payload.get("type") == "control_error":
                raise ControlChannelError(
                    str(payload.get("code") or "CONTROL_MESSAGE_REJECTED"),
                    "worker 拒绝了控制消息。",
                )

    def request(
        self, payload: Mapping[str, object], *, expected: str, timeout: float
    ) -> dict[str, Any]:
        self._send(payload)
        deadline = _deadline(timeout)
        while True:
            frame = self._next(_remaining(deadline))
            kind = frame.get("type")
            if kind == "control_error":
                raise ControlChannelError(
                    str(frame.get("code") or "CONTROL_MESSAGE_REJECTED"),
                    "worker 拒绝了控制消息。",
                )
            if kind == expected:
                return frame

    def open_settings(self, *, timeout: float) -> dict[str, Any]:
        grant = self.request(
            {"type": "open_settings", "settings_base_url": self.settings_base_url},
            expected="one_time_code_grant",
            timeout=timeout,
        )
        url = grant.get("authorization_url")
        if not isinstance(url, str) or "#code=" not in url:
            raise ControlChannelError(
                "AUTHORIZATION_URL_MISSING", "授权帧缺少设置页地址。"
            )
        return grant

    def reauthorize(self, *, timeout: float) -> dict[str, Any]:
        grant = self.request(
            {"type": "reauthorize", "settings_base_url": self.settings_base_url},
            expected="one_time_code_grant",
            timeout=timeout,
        )
        url = grant.get("authorization_url")
        if not isinstance(url, str) or "#code=" not in url:
            raise ControlChannelError(
                "AUTHORIZATION_URL_MISSING", "授权帧缺少设置页地址。"
            )
        return grant

    def request_restart(self, *, timeout: float) -> dict[str, Any]:
        return self.request(
            {"type": "restart"}, expected="restart_accepted", timeout=timeout
        )

    def shutdown(self, *, timeout: float) -> None:
        self.request({"type": "shutdown"}, expected="shutdown_accepted", timeout=timeout)

    def close(self) -> None:
        """只释放本端读取线程；管道由启动方关闭。"""

        with self._condition:
            self._closed = True
            self._condition.notify_all()


class RestartCoordinator:
    """设置运行时的重启钩子：只把“接受重启”转成 worker 优雅退出。

    未绑定 Launcher 控制通道时保持 ``unavailable``，设置 API 继续返回手工重启指引。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._callback: Callable[[], None] | None = None
        self._requested = False

    def bind(self, callback: Callable[[], None]) -> None:
        with self._lock:
            self._callback = callback

    @property
    def requested(self) -> bool:
        with self._lock:
            return self._requested

    def request(self) -> str:
        with self._lock:
            callback = self._callback
            if callback is None:
                return "unavailable"
            self._requested = True
        try:
            callback()
        except Exception:  # noqa: BLE001 - 安全降级为手工重启，不暴露内部异常
            with self._lock:
                self._requested = False
            return "unavailable"
        return "accepted"


def _process_id() -> int:
    return os.getpid()


def _deadline(timeout: float) -> float:
    return time.monotonic() + max(timeout, 0.0)


def _remaining(deadline: float) -> float:
    return max(deadline - time.monotonic(), 0.0)


def _validate_registration(payload: Mapping[str, Any], expected_base_url: str) -> None:
    base_url = payload.get("settings_base_url")
    if base_url != expected_base_url:
        raise ControlChannelError(
            "WORKER_BINDING_MISMATCH", "worker 报告的实际监听地址与启动参数不一致。"
        )
    pid = payload.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        raise ControlChannelError("WORKER_BINDING_MISMATCH", "worker 注册缺少有效进程号。")
    for field in ("environment_id", "instance_id"):
        try:
            UUID(str(payload.get(field)))
        except (TypeError, ValueError):
            raise ControlChannelError(
                "WORKER_BINDING_MISMATCH", "worker 注册身份不可解析。"
            ) from None


__all__ = [
    "AUTHORIZATION_MESSAGES",
    "CLIENT_MESSAGES",
    "FRAME_PREFIX",
    "MAX_FRAME_BYTES",
    "RESTART_EXIT_CODE",
    "SERVER_MESSAGES",
    "ControlChannelError",
    "ControlClient",
    "ControlServer",
    "RestartCoordinator",
    "decode_frame",
    "encode_frame",
]
