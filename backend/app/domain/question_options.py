"""Compare only question options with their JSON object order intact."""

from typing import Any


def options_equal(left: Any, right: Any) -> bool:
    """Object key order is part of an option sequence, including nested values."""
    if isinstance(left, dict) and isinstance(right, dict):
        return list(left) == list(right) and all(
            options_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            options_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    return bool(left == right)
