"""Load the same default CA PEM efficiently in Windows launcher processes.

OpenSSL's file-based CA loading is expensive on this Windows runtime. Keep a
fresh SSLContext and all stdlib verification defaults; change only the input
transport of the explicitly selected certifi file. Custom trust inputs are
passed to the original factory unchanged.
"""

from __future__ import annotations

import os
import ssl
from functools import wraps
from pathlib import Path

import certifi

_installed = False


def install_windows_tls_loader() -> None:
    global _installed
    if os.name != "nt" or _installed:
        return
    original = ssl.create_default_context
    default_ca = Path(certifi.where()).resolve()

    @wraps(original)
    def create_context(
        purpose=ssl.Purpose.SERVER_AUTH, *, cafile=None, capath=None, cadata=None
    ):
        if (
            cafile is not None
            and capath is None
            and cadata is None
            and Path(os.fsdecode(cafile)).resolve() == default_ca
        ):
            # Read the current installed file each time. Never cache mutable
            # contexts or silently fall back to a different trust source.
            return original(purpose, cadata=default_ca.read_text(encoding="ascii"))
        return original(purpose, cafile=cafile, capath=capath, cadata=cadata)

    ssl.create_default_context = create_context
    _installed = True
