"""TCR-T093: startup-only binding never replaces an existing restart channel."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from backend.app.services.settings.management import SettingsRuntime


def _runtime() -> SettingsRuntime:
    return SettingsRuntime(
        profile=SimpleNamespace(environment="test", environment_id=uuid4()),
        config=None,
    )


def test_bind_restart_channel_keeps_the_first_startup_channel() -> None:
    runtime = _runtime()
    def first() -> str:
        return "accepted"

    def second() -> str:
        return "accepted"

    runtime.bind_restart_channel(first)
    runtime.bind_restart_channel(second)

    assert runtime._restart_channel is first
