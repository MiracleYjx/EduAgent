"""T221 控制通道单元：帧边界、注册确认、授权码发行与受控动作。"""

from __future__ import annotations

import io
import json
import queue
import threading
from collections.abc import Iterator
from uuid import uuid4

import pytest

from backend.app.core.settings_security import (
    EnvironmentName,
    RuntimeKind,
    SettingsSecurityError,
    SetupSecurityService,
    SetupSourcePolicy,
)
from backend.app.deployment.control_channel import (
    FRAME_PREFIX,
    MAX_FRAME_BYTES,
    ControlChannelError,
    ControlClient,
    ControlServer,
    RestartCoordinator,
    decode_frame,
    encode_frame,
)

BASE_URL = "http://127.0.0.1:18121"


def _security(port: int = 18121) -> SetupSecurityService:
    policy = SetupSourcePolicy(
        environment=EnvironmentName.DEVELOPMENT,
        runtime_kind=RuntimeKind.PYTHON,
        allowed_hosts=frozenset({f"127.0.0.1:{port}", f"localhost:{port}"}),
    )
    return SetupSecurityService(
        environment_id=uuid4(),
        environment=EnvironmentName.DEVELOPMENT,
        runtime_kind=RuntimeKind.PYTHON,
        policy=policy,
    )


def _server(writer: io.StringIO, reader: io.StringIO | None = None) -> ControlServer:
    return ControlServer(
        _security(),
        settings_base_url=BASE_URL,
        reader=reader or io.StringIO(""),
        writer=writer,
    )


def _frames(text: str) -> list[dict[str, object]]:
    return [json.loads(line[len(FRAME_PREFIX) :]) for line in text.splitlines()]


def test_encode_rejects_missing_type_and_oversized_frame() -> None:
    with pytest.raises(ControlChannelError) as missing:
        encode_frame({"code": "x"})
    assert missing.value.code == "CONTROL_FRAME_INVALID"

    with pytest.raises(ControlChannelError) as oversized:
        encode_frame({"type": "open_settings", "padding": "x" * MAX_FRAME_BYTES})
    assert oversized.value.code == "CONTROL_FRAME_TOO_LARGE"


def test_decode_ignores_noise_and_rejects_malformed_control_lines() -> None:
    assert decode_frame("uvicorn started\n") is None
    with pytest.raises(ControlChannelError) as broken:
        decode_frame(FRAME_PREFIX + "{not-json}\n")
    assert broken.value.code == "CONTROL_FRAME_INVALID"
    with pytest.raises(ControlChannelError) as too_long:
        decode_frame(FRAME_PREFIX + "x" * MAX_FRAME_BYTES + "\n")
    assert too_long.value.code == "CONTROL_FRAME_TOO_LARGE"
    decoded = decode_frame(encode_frame({"type": "shutdown"}))
    assert decoded == {"type": "shutdown"}


def test_registration_carries_identity_but_never_a_code() -> None:
    writer = io.StringIO()
    server = _server(writer)
    server.write(server.registration())
    frame = _frames(writer.getvalue())[0]
    assert frame["type"] == "worker_registered"
    assert frame["settings_base_url"] == BASE_URL
    assert frame["pid"] > 0
    assert "code" not in frame
    assert "authorization_url" not in frame


def test_open_settings_issues_code_and_new_code_invalidates_previous() -> None:
    writer = io.StringIO()
    security = _security()
    server = ControlServer(
        security, settings_base_url=BASE_URL, reader=io.StringIO(""), writer=writer
    )
    grant = server.handle(
        {"type": "open_settings", "settings_base_url": BASE_URL}
    )[0]
    assert grant["type"] == "one_time_code_grant"
    assert str(grant["authorization_url"]).startswith(BASE_URL + "/#code=")

    second = server.handle({"type": "reauthorize", "settings_base_url": BASE_URL})[0]
    assert second["code"] != grant["code"]
    with pytest.raises(SettingsSecurityError) as used:
        security.exchange(str(grant["code"]))
    assert used.value.code == "CODE_EXPIRED"
    # 新码仍可兑换，Bearer 语义不在本通道内变化。
    session = security.exchange(str(second["code"]))
    assert session.instance_id == security.instance_id


def test_base_url_mismatch_is_rejected_without_issuing_a_code() -> None:
    writer = io.StringIO()
    server = _server(writer)
    response, action = server.handle(
        {"type": "open_settings", "settings_base_url": "http://127.0.0.1:1"}
    )
    assert action is None
    assert response["type"] == "control_error"
    assert response["code"] == "AUTHORIZATION_BASE_URL_MISMATCH"
    assert writer.getvalue() == ""


def test_unknown_message_and_restart_without_channel_are_safe_errors() -> None:
    writer = io.StringIO()
    server = _server(writer)
    unknown, _ = server.handle({"type": "run_shell"})
    assert unknown["code"] == "CONTROL_MESSAGE_UNSUPPORTED"
    restart, action = server.handle({"type": "restart"})
    assert restart["code"] == "RESTART_UNAVAILABLE"
    assert action is None


def test_control_server_runs_action_after_writing_the_response() -> None:
    writer = io.StringIO()
    observed: list[str] = []

    def on_restart() -> None:
        observed.append("restart")

    server = ControlServer(
        _security(),
        settings_base_url=BASE_URL,
        reader=io.StringIO(encode_frame({"type": "restart"})),
        writer=writer,
        on_restart=on_restart,
    )
    server.serve_forever()
    frames = _frames(writer.getvalue())
    assert frames[0]["type"] == "restart_accepted"
    assert observed == ["restart"]
    assert "code" not in writer.getvalue()


def test_control_channel_eof_marks_the_worker_as_unsupervised() -> None:
    writer = io.StringIO()
    lost: list[str] = []
    server = ControlServer(
        _security(),
        settings_base_url=BASE_URL,
        reader=io.StringIO(""),
        writer=writer,
        on_channel_lost=lambda: lost.append("lost"),
    )
    server.serve_forever()
    assert lost == ["lost"]


class _QueueReader:
    """按行投递的控制管道替身；`None` 表示对端关闭。"""

    def __init__(self) -> None:
        self._items: queue.Queue[str | None] = queue.Queue()

    def push(self, line: str) -> None:
        self._items.put(line)

    def close(self) -> None:
        self._items.put(None)

    def __iter__(self) -> Iterator[str]:
        while True:
            item = self._items.get(timeout=5)
            if item is None:
                return
            yield item


@pytest.fixture
def _client():
    owned = []

    def create(reader: _QueueReader) -> ControlClient:
        client = ControlClient(reader=reader, writer=io.StringIO(), settings_base_url=BASE_URL)
        owned.append((client, reader))
        return client

    yield create
    for client, reader in owned:
        reader.close()
        client.close()
        client._thread.join(timeout=5)
        assert not client._thread.is_alive()


def test_client_requires_registration_before_any_command(_client) -> None:
    reader = _QueueReader()
    client = _client(reader)
    frame = {
        "type": "worker_registered",
        "pid": 4242,
        "environment_id": str(uuid4()),
        "instance_id": str(uuid4()),
        "settings_base_url": BASE_URL,
    }
    reader.push(encode_frame(frame))
    registration = client.wait_registration(timeout=5, settings_base_url=BASE_URL)
    assert registration["pid"] == 4242


def test_client_rejects_registration_from_another_bind(_client) -> None:
    reader = _QueueReader()
    client = _client(reader)
    reader.push(
        encode_frame(
            {
                "type": "worker_registered",
                "pid": 4242,
                "environment_id": str(uuid4()),
                "instance_id": str(uuid4()),
                "settings_base_url": "http://127.0.0.1:19999",
            }
        )
    )
    with pytest.raises(ControlChannelError) as mismatch:
        client.wait_registration(timeout=5, settings_base_url=BASE_URL)
    assert mismatch.value.code == "WORKER_BINDING_MISMATCH"


def test_client_times_out_when_channel_closes_without_registration(_client) -> None:
    reader = _QueueReader()
    client = _client(reader)
    threading.Timer(0.05, reader.close).start()
    with pytest.raises(ControlChannelError) as closed:
        client.wait_registration(timeout=5, settings_base_url=BASE_URL)
    assert closed.value.code == "CONTROL_CHANNEL_CLOSED"


def test_client_surfaces_control_errors_and_missing_authorization_url(_client) -> None:
    reader = _QueueReader()
    client = _client(reader)
    reader.push(encode_frame({"type": "worker_registered", "pid": 1}))
    # 注册帧本身非法：直接暴露稳定码，不执行任何动作。
    with pytest.raises(ControlChannelError) as invalid:
        client.wait_registration(timeout=5, settings_base_url=BASE_URL)
    assert invalid.value.code == "WORKER_BINDING_MISMATCH"

    reader2 = _QueueReader()
    client2 = _client(reader2)
    reader2.push(
        encode_frame(
            {
                "type": "worker_registered",
                "pid": 1,
                "environment_id": str(uuid4()),
                "instance_id": str(uuid4()),
                "settings_base_url": BASE_URL,
            }
        )
    )
    client2.wait_registration(timeout=5, settings_base_url=BASE_URL)
    reader2.push(encode_frame({"type": "one_time_code_grant", "code": "synthetic"}))
    with pytest.raises(ControlChannelError) as missing:
        client2.open_settings(timeout=5)
    assert missing.value.code == "AUTHORIZATION_URL_MISSING"


def test_restart_coordinator_binds_only_to_a_live_channel() -> None:
    coordinator = RestartCoordinator()
    assert coordinator.request() == "unavailable"
    assert not coordinator.requested

    calls: list[str] = []
    coordinator.bind(lambda: calls.append("exit"))
    assert coordinator.request() == "accepted"
    assert coordinator.requested
    assert calls == ["exit"]

    broken = RestartCoordinator()

    def explode() -> None:
        raise RuntimeError("no channel")

    broken.bind(explode)
    assert broken.request() == "unavailable"
    assert not broken.requested
