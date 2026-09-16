"""M0 冒烟入口的隔离检查。

TCR（环境隔离）：全量测试与独立脚本都能启动 Compose 并执行迁移，必须证明隔离配置
缺失或指向开发端口时，在任何 Docker 调用前拒绝执行。测试用函数替身拦截 Docker，
不启动容器、不访问开发数据库，覆盖 pytest 跳过提示、固定配置及脚本失败诊断边界。
"""

from __future__ import annotations

import os
import subprocess

import pytest

from tests.integration import test_m0_smoke as smoke


def _reject_docker() -> None:
    """隔离检查失败后不允许继续访问 Docker。"""

    pytest.fail("隔离检查之前不得访问 Docker。")


@pytest.mark.parametrize("missing", list(smoke.ISOLATION_ENV))
def test_missing_isolation_skips_before_docker(
    monkeypatch: pytest.MonkeyPatch, missing: str,
) -> None:
    """任一必填配置缺失都明确跳过，不依赖宿主机是否安装 Docker。"""

    for name, value in smoke.ISOLATION_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(missing)
    monkeypatch.setattr(smoke, "_docker_daemon_available", _reject_docker)
    with pytest.raises(pytest.skip.Exception, match="缺少隔离配置"):
        smoke.test_m0_smoke_script()


@pytest.mark.parametrize("name,value", [
    ("COMPOSE_PROJECT_NAME", "eduagent"),
    ("POSTGRES_PORT", "5432"),
    ("REDIS_PORT", "6379"),
    ("BACKEND_PORT", "8000"),
])
def test_development_project_or_port_is_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str,
) -> None:
    """开发项目或任一开发端口不能绕过固定隔离入口。"""

    for key, expected in smoke.ISOLATION_ENV.items():
        monkeypatch.setenv(key, expected)
    monkeypatch.setenv(name, value)
    monkeypatch.setattr(smoke, "_docker_daemon_available", _reject_docker)
    with pytest.raises(pytest.fail.Exception, match="隔离配置不符合固定值"):
        smoke.test_m0_smoke_script()


def test_fixed_isolation_configuration_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """四个指定值均已显式设置时，入口允许继续运行。"""

    for name, value in smoke.ISOLATION_ENV.items():
        monkeypatch.setenv(name, value)
    smoke._require_isolated_environment()


@pytest.mark.parametrize("configuration", ["missing", "development"])
def test_powershell_rejects_unsafe_environment_without_docker(configuration: str) -> None:
    """独立脚本在隔离未成立时非零退出，诊断分支也不能读取默认项目。"""

    powershell = smoke._find_powershell()
    if powershell is None:
        pytest.skip("未安装 PowerShell，无法验证独立冒烟脚本入口。")
    environment = os.environ.copy()
    for name in smoke.ISOLATION_ENV:
        environment.pop(name, None)
    if configuration == "development":
        environment.update(smoke.ISOLATION_ENV, COMPOSE_PROJECT_NAME="eduagent")
    script_path = str(smoke.SMOKE_SCRIPT).replace("'", "''")
    command = (
        "function docker { Write-Output '隔离前调用了Docker'; $global:LASTEXITCODE = 0 }\n"
        f"& '{script_path}'"
    )
    result = subprocess.run(
        [powershell, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=smoke.PROJECT_ROOT, env=environment, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=15, check=False,
    )
    assert result.returncode == 1
    assert "M0 冒烟验证失败" in result.stdout
    assert "隔离前调用了Docker" not in result.stdout + result.stderr
