"""Benchmark 结果目录夹具：只在已选环境自有的 results 边界内写入。

TCR（T081，最低必要变更）：`resolve_results_directory` 对受控环境（显式 profile 或
`EDUAGENT_ENVIRONMENT` / `EDUAGENT_DATA_ROOT`）要求结果目录位于该环境自己的
`results` 根内，`RESULTS_OUTSIDE_ENVIRONMENT` 是既有产品边界，不放宽也不加 skip。
旧夹具直接返回 `tmp_path / "results"`，因此在 Test 环境断言前即被边界拒绝；这里改为
在受控环境派生唯一子目录并在用例结束后删除，未选环境的 synthetic 调用仍用 pytest
临时目录（与原语义一致）。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from uuid import uuid4

from backend.app.core.environment import environment_path, resolve_environment_profile

_CONTROLLED_KEYS = (
    "EDUAGENT_ENVIRONMENT",
    "EDUAGENT_DATA_ROOT",
    "EDUAGENT_TEST_RESOURCE_PROFILE",
)


def controlled_environment_selected() -> bool:
    """Mirror the product's own boundary predicate before writing results."""

    return any(os.environ.get(key) for key in _CONTROLLED_KEYS)


def benchmark_results_directory(tmp_path: Path, *, prefix: str) -> Path:
    """Return a per-test results directory that satisfies the environment boundary."""

    if not controlled_environment_selected():
        return tmp_path / "results"
    profile = resolve_environment_profile()
    target = environment_path(profile, "results") / f"{prefix}-{uuid4().hex}"
    target.mkdir(parents=True, exist_ok=False)
    return target


def discard_results_directory(path: Path, tmp_path: Path) -> None:
    """Remove only environment-owned result dirs; pytest keeps owning its tmp_path."""

    if path.is_relative_to(tmp_path):
        return
    shutil.rmtree(path, ignore_errors=True)
