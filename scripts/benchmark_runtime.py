"""Small lifecycle and error projections shared by Benchmark entry points."""
from __future__ import annotations

import re


async def close_owned(component: object) -> None:
    """Close an owned asynchronous client when its concrete contract supports it."""
    close = getattr(component, "aclose", None)
    if callable(close):
        await close()


def error_code(error: BaseException) -> str:
    """Keep declared error classification, never arbitrary exception/SDK text."""
    code = getattr(error, "error_code", None) or getattr(error, "code", None)
    if isinstance(code, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{2,95}", code):
        return code
    return type(error).__name__


class BenchmarkValidationError(ValueError):
    """Locally authored public validation message, with no provider/user payload."""


def safe_error_detail(error: BaseException) -> str:
    return str(error) if isinstance(error, BenchmarkValidationError) else error_code(error)
