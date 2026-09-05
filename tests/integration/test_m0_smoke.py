"""M0 Docker Compose、数据库迁移和 Redis 连接冒烟测试。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SMOKE_SCRIPT = PROJECT_ROOT / "scripts" / "smoke_m0.ps1"


def _find_powershell() -> str | None:
    """查找 Windows PowerShell 或 PowerShell Core。"""

    return (
        shutil.which("pwsh")
        or shutil.which("powershell.exe")
        or shutil.which("powershell")
    )


def _docker_daemon_available() -> tuple[bool, str]:
    """检查 Docker CLI 和 Docker 引擎是否可用，不输出敏感配置。"""

    docker = shutil.which("docker.exe") or shutil.which("docker")
    if docker is None:
        return False, "未找到 Docker 命令。"

    version = subprocess.run(
        [docker, "compose", "version"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )
    if version.returncode != 0:
        return False, "Docker Compose 不可用。"

    info = subprocess.run(
        [docker, "info", "--format", "{{.ServerVersion}}"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )
    if info.returncode != 0:
        return False, "Docker 引擎未运行或当前用户无权访问。"
    return True, ""


def test_m0_smoke_script() -> None:
    """执行独立 M0 冒烟脚本并要求所有检查成功。"""

    if not SMOKE_SCRIPT.exists():
        pytest.fail(f"未找到 M0 冒烟脚本：{SMOKE_SCRIPT}")

    powershell = _find_powershell()
    if powershell is None:
        pytest.skip("未找到 PowerShell，跳过 M0 容器冒烟测试。")

    docker_available, reason = _docker_daemon_available()
    if not docker_available:
        pytest.skip(f"Docker 前置条件不可用，跳过 M0 容器冒烟测试：{reason}")

    result = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SMOKE_SCRIPT),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=240,
        check=False,
    )
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, f"M0 冒烟脚本执行失败：\n{output}"
