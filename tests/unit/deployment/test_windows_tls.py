"""TCR 45: equivalent TLS trust and untouched explicit trust configurations."""

import ssl
from pathlib import Path
from unittest.mock import Mock

import certifi
import pytest

from backend.app.deployment import windows_tls


@pytest.fixture
def install(monkeypatch):
    monkeypatch.setattr(windows_tls, "_installed", False)
    monkeypatch.setattr(windows_tls.os, "name", "nt")
    original = ssl.create_default_context
    monkeypatch.setattr(ssl, "create_default_context", original)
    windows_tls.install_windows_tls_loader()
    return original


def test_complete_ca_trust_and_verification_settings_are_equivalent(install):
    original = install(cafile=certifi.where())
    optimized = ssl.create_default_context(cafile=certifi.where())
    assert set(original.get_ca_certs(binary_form=True)) == set(
        optimized.get_ca_certs(binary_form=True)
    )
    assert original.cert_store_stats() == optimized.cert_store_stats()
    for name in (
        "verify_mode",
        "check_hostname",
        "verify_flags",
        "options",
        "minimum_version",
        "maximum_version",
    ):
        assert getattr(original, name) == getattr(optimized, name)
    assert optimized.verify_mode == ssl.CERT_REQUIRED
    assert optimized.check_hostname is True
    assert optimized is not ssl.create_default_context(cafile=certifi.where())


def test_explicit_trust_inputs_are_forwarded_unchanged(monkeypatch, tmp_path):
    original = Mock()
    monkeypatch.setattr(ssl, "create_default_context", original)
    monkeypatch.setattr(windows_tls, "_installed", False)
    monkeypatch.setattr(windows_tls.os, "name", "nt")
    windows_tls.install_windows_tls_loader()
    custom = tmp_path / "custom.pem"
    ssl.create_default_context(cafile=custom)
    original.assert_called_with(
        ssl.Purpose.SERVER_AUTH, cafile=custom, capath=None, cadata=None
    )
    ssl.create_default_context(cafile=certifi.where(), capath=tmp_path)
    original.assert_called_with(
        ssl.Purpose.SERVER_AUTH, cafile=certifi.where(), capath=tmp_path, cadata=None
    )
    ssl.create_default_context(cadata="explicit trust")
    original.assert_called_with(
        ssl.Purpose.SERVER_AUTH, cafile=None, capath=None, cadata="explicit trust"
    )


def test_invalid_default_certificate_remains_failure(install, monkeypatch):
    monkeypatch.setattr(
        Path, "read_text", lambda *args, **kwargs: "invalid certificate"
    )
    with pytest.raises(ssl.SSLError):
        ssl.create_default_context(cafile=certifi.where())


def test_repeated_install_is_idempotent(install):
    wrapped = ssl.create_default_context
    windows_tls.install_windows_tls_loader()
    assert ssl.create_default_context is wrapped
