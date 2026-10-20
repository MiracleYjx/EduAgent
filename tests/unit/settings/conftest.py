"""Synthetic-only test boundary for this Feature's settings unit tests."""

import os
import socket
import sys

import pytest
from sqlalchemy.engine import Engine

#: Capture the real implementation before the per-test monkeypatch.
_REAL_SOCKET_CONNECT = socket.socket.connect


@pytest.fixture(autouse=True)
def forbid_real_services(monkeypatch):
    # Clear only settings source names, never read or copy inherited secret values.
    for name in tuple(os.environ):
        if name.upper() in {"DEV_MODE", "JWT_ALGORITHM", "JWT_EXPIRE_MINUTES", "STORAGE_ROOT",
                            "EDUAGENT_ENVIRONMENT"} or name.upper().startswith("EDUAGENT_"):
            monkeypatch.delenv(name, raising=False)

    def blocked(self, address, *args, **kwargs):
        # Windows Proactor/Selector loops create an internal loopback
        # socketpair in ``_fallback_socketpair``. Allowing only that exact
        # call keeps real HTTP/DB connects blocked while letting async tests
        # start an event loop; no test data or service is reached.
        caller = sys._getframe(1).f_code.co_name
        if caller == "_fallback_socketpair":
            return _REAL_SOCKET_CONNECT(self, address, *args, **kwargs)
        raise AssertionError("real network or database access is forbidden in this unit suite")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(Engine, "connect", blocked)

