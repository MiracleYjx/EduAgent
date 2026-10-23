"""TCR-T093: explicit, single-use Docker controlled restart channel."""

from __future__ import annotations

import threading

from backend.app.deployment.container_control import (
    container_restart_channel,
    container_restart_enabled,
)


def test_container_restart_requires_explicit_deployment_opt_in() -> None:
    assert container_restart_enabled({}) is False
    assert container_restart_enabled({"EDUAGENT_CONTAINER_RESTART": "0"}) is False
    assert container_restart_enabled({"EDUAGENT_CONTAINER_RESTART": "false"}) is False
    assert container_restart_enabled({"EDUAGENT_CONTAINER_RESTART": "1"}) is True
    assert container_restart_enabled({"EDUAGENT_CONTAINER_RESTART": " TRUE "}) is True


def test_container_restart_channel_is_single_use_and_defers_termination() -> None:
    terminated = threading.Event()
    calls: list[int] = []

    def terminate() -> None:
        calls.append(1)
        terminated.set()

    request = container_restart_channel(terminate=terminate, delay=0)

    assert request() == "accepted"
    assert request() == "accepted"
    assert terminated.wait(timeout=2)
    assert calls == [1]
