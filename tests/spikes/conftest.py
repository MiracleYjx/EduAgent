"""T001 safety boundary for local-only preparation; never a product implementation."""
from __future__ import annotations
import ipaddress
import os
from pathlib import Path
import socket
import sys
import pytest

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(os.environ.get('PL002_OUTPUT_ROOT', '')).resolve()
if OUT != (ROOT / '.cache/personal-learning-spike-20261019').resolve():
    raise RuntimeError('PL002_EXPLICIT_OWNED_OUTPUT_REQUIRED')
if os.environ.get('PYTHON_DOTENV_DISABLED') != '1':
    raise RuntimeError('PL002_DOTENV_MUST_BE_DISABLED')


def _no_real_secrets(event, args):
    if event != 'open' or not args or not isinstance(args[0], (str, bytes, os.PathLike)):
        return
    path = Path(os.fsdecode(args[0])).resolve()
    if path.name == '.env' or path == ROOT / 'storage' or (ROOT / 'storage') in path.parents:
        raise RuntimeError('PL002_REAL_CONFIG_OR_BUSINESS_READ_FORBIDDEN')


sys.addaudithook(_no_real_secrets)
from backend.app.core.config import AppSettings
AppSettings.model_config['env_file'] = None


@pytest.fixture(autouse=True)
def local_only_network(monkeypatch):
    """MockTransport requires no sockets; only loopback OS event-loop setup is allowed."""
    original = socket.socket.connect
    original_ex = socket.socket.connect_ex
    def allow(address):
        if isinstance(address, tuple):
            try:
                if not ipaddress.ip_address(address[0]).is_loopback:
                    raise RuntimeError('PL002_EXTERNAL_NETWORK_FORBIDDEN')
            except ValueError:
                raise RuntimeError('PL002_DNS_NETWORK_FORBIDDEN') from None
    def checked(self, address):
        allow(address)
        return original(self, address)
    def checked_ex(self, address):
        allow(address)
        return original_ex(self, address)
    monkeypatch.setattr(socket.socket, 'connect', checked)
    monkeypatch.setattr(socket.socket, 'connect_ex', checked_ex)
    assert AppSettings.model_config['env_file'] is None


def pytest_report_header():
    return 'PL002: explicit Test root; dotenv disabled; external sockets forbidden; Fake providers only'
