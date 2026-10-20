"""M0 准入的纯 Fake 单元测试；不执行 Docker、服务或原脚本尾部。

只由 PowerShell 7 Parser 提取原脚本所需的顶层赋值和两个 Assert 定义。
所有 profile、Compose 展开结果、环境文件及子进程输出均为 tmp_path 合成物；
子进程不继承父进程凭据、Docker 配置或项目 .env。缺少 PowerShell 7 时失败，
不以 skip 或 Windows PowerShell 5 替代本准入验证。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SMOKE_SCRIPT = PROJECT_ROOT / "scripts" / "smoke_m0.ps1"
ASSERT_NAMES = ["Assert-IsolatedEnvironment", "Assert-IsolatedComposeConfiguration"]
CONFIG_COMMAND = "compose config --format json"
ENVIRONMENT_ID = "4e927bee-650a-4510-b25c-8bcb2380dc8d"
INSTANCE_ID = "7500000000000000042"
OUTPUT_LIMIT = 8192

# 不 dot-source 原脚本：只运行经过顶层 AST 白名单选取的原定义。
# ParseInput 的文件名使用合成仓库中的 harness，令原函数的 PSScriptRoot
# 指向合成 scripts/，保留原 Compose 来源校验，而非改写该校验。
POWERSHELL_HARNESS = r"""
param(
    [Parameter(Mandatory = $true)][string]$SourceScript,
    [Parameter(Mandatory = $true)][string]$CaseFile
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
if ($PSVersionTable.PSVersion.Major -lt 7) {
    throw 'This Fake admission harness requires PowerShell 7 or later.'
}
$script:fixture = Get-Content -LiteralPath $CaseFile -Raw -Encoding UTF8 | ConvertFrom-Json
$script:dockerCalls = [System.Collections.Generic.List[string]]::new()
$script:sideEffects = [System.Collections.Generic.List[string]]::new()
$script:reparseHits = 0

function Invoke-DockerCommand {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $command = $Arguments -join ' '
    $script:dockerCalls.Add($command)
    if ($Arguments.Count -ne 4 -or
        $Arguments[0] -cne 'compose' -or $Arguments[1] -cne 'config' -or
        $Arguments[2] -cne '--format' -or $Arguments[3] -cne 'json') {
        $script:sideEffects.Add("forbidden-docker:$command")
        throw "Fake Docker refuses every command except compose config --format json."
    }
    return [PSCustomObject]@{
        ExitCode = 0
        Output = [System.IO.File]::ReadAllText($script:fixture.compose_json)
    }
}

# 防止今后原 Assert 意外绕过 Invoke-DockerCommand，仍不触达真实 Docker。
function docker {
    $script:sideEffects.Add('native-docker')
    throw 'Native Docker is forbidden in this unit test.'
}
function docker.exe {
    $script:sideEffects.Add('native-docker.exe')
    throw 'Native Docker is forbidden in this unit test.'
}

# 仅替换文件系统属性输入；不复写原来源路径/祖先遍历/拒绝判定。
function Get-Item {
    param([string]$LiteralPath, [switch]$Force)
    if (-not [string]::IsNullOrWhiteSpace($script:fixture.reparse_path) -and
        [System.IO.Path]::GetFullPath($LiteralPath) -ceq
        [System.IO.Path]::GetFullPath($script:fixture.reparse_path)) {
        $script:reparseHits += 1
        return [PSCustomObject]@{
            Attributes = [System.IO.FileAttributes]::ReparsePoint
        }
    }
    Microsoft.PowerShell.Management\Get-Item -LiteralPath $LiteralPath -Force:$Force
}

$tokens = $null
$parseErrors = $null
$sourceAst = [System.Management.Automation.Language.Parser]::ParseFile(
    $SourceScript, [ref]$tokens, [ref]$parseErrors
)
if (@($parseErrors).Count -ne 0) {
    throw 'The original M0 script has PowerShell parse errors.'
}
$requiredVariables = @('containerProfileSource', 'isolationEnvironment')
$requiredFunctions = @('Assert-IsolatedEnvironment', 'Assert-IsolatedComposeConfiguration')
$selected = @(
    foreach ($statement in $sourceAst.EndBlock.Statements) {
        if ($statement -is [System.Management.Automation.Language.AssignmentStatementAst] -and
            $statement.Left -is [System.Management.Automation.Language.VariableExpressionAst] -and
            $statement.Left.VariablePath.UserPath -cin $requiredVariables) {
            $statement
        }
        elseif ($statement -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                $statement.Name -cin $requiredFunctions) {
            $statement
        }
    }
)
$variableNames = @($selected | Where-Object {
    $_ -is [System.Management.Automation.Language.AssignmentStatementAst]
} | ForEach-Object { $_.Left.VariablePath.UserPath } | Sort-Object)
$functionNames = @($selected | Where-Object {
    $_ -is [System.Management.Automation.Language.FunctionDefinitionAst]
} | ForEach-Object { $_.Name } | Sort-Object)
if (($variableNames -join ',') -cne 'containerProfileSource,isolationEnvironment' -or
    ($functionNames -join ',') -cne 'Assert-IsolatedComposeConfiguration,Assert-IsolatedEnvironment') {
    throw 'The original top-level admission definitions changed; update this harness explicitly.'
}
$definitionText = ($selected | ForEach-Object { $_.Extent.Text }) -join "`n"
$definitionsAst = [System.Management.Automation.Language.Parser]::ParseInput(
    $definitionText, $PSCommandPath, [ref]$tokens, [ref]$parseErrors
)
if (@($parseErrors).Count -ne 0) {
    throw 'The extracted original admission definitions could not be parsed.'
}
. ($definitionsAst.GetScriptBlock())

$entered = [System.Collections.Generic.List[string]]::new()
$passed = [System.Collections.Generic.List[string]]::new()
$errorText = $null
try {
    $entered.Add('Assert-IsolatedEnvironment')
    Assert-IsolatedEnvironment
    $passed.Add('Assert-IsolatedEnvironment')
    $entered.Add('Assert-IsolatedComposeConfiguration')
    Assert-IsolatedComposeConfiguration
    $passed.Add('Assert-IsolatedComposeConfiguration')
}
catch {
    $errorText = $_.Exception.Message
}
$report = [ordered]@{
    accepted = ($passed.Count -eq 2)
    entered = $entered
    passed = $passed
    error = $errorText
    docker_calls = $script:dockerCalls
    side_effects = $script:sideEffects
    fake_reparse_hits = $script:reparseHits
    container_source = $script:containerProfileSource
    original_functions = $functionNames
    original_variables = $variableNames
    powershell_major = $PSVersionTable.PSVersion.Major
}
[Console]::WriteLine(($report | ConvertTo-Json -Depth 8 -Compress))
"""


@pytest.fixture(scope="session")
def powershell7() -> str:
    """沿用 T202 显式路径优先规则，但兜底仅允许 pwsh，不允许 Windows 5。"""

    configured = os.environ.get("T202_POWERSHELL")
    if configured and Path(configured).is_file():
        return str(Path(configured).resolve())
    executable = shutil.which("pwsh")
    if executable is None:
        pytest.fail(
            "M0 Fake 准入需要 PowerShell 7；请显式设置 T202_POWERSHELL 或提供 pwsh。"
        )
    return str(Path(executable).resolve())


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _snapshot(root: Path) -> dict[str, bytes | None]:
    """记录合成仓库的目录和文件，连新增/删除/改写来源路径也必须被发现。"""

    return {
        path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
        for path in sorted(root.rglob("*"))
    }


def _bounded_output(path: Path) -> str:
    with path.open("rb") as stream:
        raw = stream.read(OUTPUT_LIMIT + 1)
    assert len(raw) <= OUTPUT_LIMIT, (
        f"Fake PowerShell 输出超过 {OUTPUT_LIMIT} 字节：{path.name}"
    )
    return raw.decode("utf-8-sig", errors="replace")


def _child_environment(powershell: str, tmp_path: Path) -> dict[str, str]:
    """从白名单构造环境，绝不复制 os.environ 后再尝试猜测秘密字段。"""

    environment = {
        name: os.environ[name]
        for name in ("SystemRoot", "WINDIR", "SYSTEMDRIVE")
        if name in os.environ
    }
    runtime = tmp_path / "powershell-runtime"
    for directory in ("home", "temp", "config", "cache", "data", "docker"):
        (runtime / directory).mkdir(parents=True, exist_ok=True)
    executable_root = Path(powershell).parent
    environment.update(
        {
            "PATH": str(executable_root),
            "PSModulePath": str(executable_root / "Modules"),
            "HOME": str(runtime / "home"),
            "USERPROFILE": str(runtime / "home"),
            "APPDATA": str(runtime / "config"),
            "LOCALAPPDATA": str(runtime / "data"),
            "XDG_CONFIG_HOME": str(runtime / "config"),
            "XDG_CACHE_HOME": str(runtime / "cache"),
            "XDG_DATA_HOME": str(runtime / "data"),
            "TEMP": str(runtime / "temp"),
            "TMP": str(runtime / "temp"),
            "TMPDIR": str(runtime / "temp"),
            "PSModuleAnalysisCachePath": str(runtime / "cache" / "module-analysis"),
            "POWERSHELL_TELEMETRY_OPTOUT": "1",
            "POWERSHELL_UPDATECHECK": "Off",
            "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
            "DOCKER_CONFIG": str(runtime / "docker"),
            "DOCKER_HOST": "tcp://127.0.0.1:1",
        }
    )
    return environment


def _run_admission(
    tmp_path: Path,
    powershell: str,
    change: tuple[tuple[str | int, ...], Any] | None = None,
) -> dict[str, Any]:
    project = (tmp_path / "fake-repository").resolve()
    root = project / "resource-root"
    host_path = root / "config" / "host-resource-profile.json"
    container_path = root / "config" / "container-resource-profile.json"
    other_profile = project / "other-container-profile.json"
    env_file = project / "fixture-only.env"
    compose_file = project / "docker-compose.test.yml"
    harness = project / "scripts" / "profile-admission.ps1"
    compose_json = project / "expanded-compose.json"
    case_file = project / "fake-inputs.json"

    database = {
        "host": "127.0.0.1",
        "port": 15432,
        "database": "eduagent_test",
        "user": "eduagent_test_runner",
        "schema_name": "public",
        "instance_id": INSTANCE_ID,
    }
    maintenance = {**database, "database": "postgres", "user": "eduagent_test_admin"}
    host_profile = {
        "environment": "test",
        "environment_id": ENVIRONMENT_ID,
        "root": str(root),
        "database_identity": database,
        "maintenance_identity": maintenance,
        "redis_instance_id": "fake-redis-instance-01",
    }
    container_profile = {
        **host_profile,
        "root": "/app",
        "database_identity": {**database, "host": "postgres", "port": 5432},
        "maintenance_identity": {**maintenance, "host": "postgres", "port": 5432},
    }
    services: dict[str, Any] = {
        service: {
            "ports": [
                {
                    "host_ip": "127.0.0.1",
                    "published": str(published),
                    "target": target,
                }
            ]
        }
        for service, published, target in (
            ("postgres", 15432, 5432),
            ("redis", 16379, 6379),
            ("backend", 18000, 8000),
        )
    }
    for service, source, target in (
        ("postgres", "postgres_data", "/var/lib/postgresql/data"),
        ("redis", "redis_data", "/data"),
    ):
        services[service]["volumes"] = [
            {
                "type": "volume",
                "source": source,
                "target": target,
                "read_only": False,
                "volume": {},
            }
        ]
    services["postgres"]["environment"] = {
        "POSTGRES_DB": "postgres",
        "POSTGRES_USER": "eduagent_test_admin",
        "POSTGRES_PASSWORD": "fake-admin-password-not-used",
        "POSTGRES_HOST_AUTH_METHOD": "scram-sha-256",
    }
    services["backend"].update(
        {
            "environment": {
                "EDUAGENT_ENVIRONMENT": "test",
                "EDUAGENT_RUNTIME_KIND": "docker",
                "EDUAGENT_DATA_ROOT": "/app",
                "EDUAGENT_TEST_RESOURCE_PROFILE": "/run/eduagent/test-resource-profile.json",
                "STORAGE_ROOT": "/app/storage",
                "DATABASE_URL": (
                    "postgresql+psycopg://eduagent_test_runner:synthetic-only"
                    "@postgres:5432/eduagent_test"
                ),
                "REDIS_URL": "redis://redis:6379/0",
            },
            "volumes": [
                {
                    "type": "volume",
                    "source": "storage_data",
                    "target": "/app/storage",
                    "read_only": False,
                },
                {
                    "type": "bind",
                    "source": str(container_path),
                    "target": "/run/eduagent/test-resource-profile.json",
                    "read_only": True,
                    "bind": {"create_host_path": False},
                },
            ],
        }
    )
    data: dict[str, Any] = {
        "host": host_profile,
        "container": container_profile,
        "compose": {
            "name": "eduagent-test",
            "services": services,
            "volumes": {
                name: {"name": f"eduagent-test_{name}"}
                for name in ("postgres_data", "redis_data", "storage_data")
            },
        },
        "env": {
            "COMPOSE_PROJECT_NAME": "eduagent-test",
            "POSTGRES_PORT": "15432",
            "REDIS_PORT": "16379",
            "BACKEND_PORT": "18000",
            "EDUAGENT_ENVIRONMENT": "test",
            "POSTGRES_DB": "eduagent_test",
            "POSTGRES_USER": "eduagent_test_runner",
            "POSTGRES_PASSWORD": "fake-unit-password-not-used",
            "POSTGRES_HOST_AUTH_METHOD": "scram-sha-256",
            "EDUAGENT_TEST_ADMIN_PASSWORD": "fake-admin-password-not-used",
            "EDUAGENT_TEST_RUNNER_PASSWORD": "synthetic-only",
            "EDUAGENT_TEST_RESOURCE_PROFILE": str(host_path),
            "EDUAGENT_TEST_CONTAINER_PROFILE": str(container_path),
            "EDUAGENT_ENV_FILE": str(env_file),
            "COMPOSE_FILE": str(compose_file),
            "COMPOSE_DISABLE_ENV_FILE": "1",
        },
        "write_host": True,
        "write_container": True,
        "reparse_path": None,
    }
    if change is not None:
        key_path, value = change
        parent = data
        for key in key_path[:-1]:
            parent = parent[key]
        parent[key_path[-1]] = value(root) if callable(value) else value

    root.mkdir(parents=True)
    harness.parent.mkdir(parents=True)
    if data["write_host"]:
        _write_json(host_path, data["host"])
    if data["write_container"]:
        _write_json(container_path, data["container"])
    _write_json(other_profile, data["container"])
    _write_json(compose_json, data["compose"])
    _write_json(
        case_file,
        {"compose_json": str(compose_json), "reparse_path": data["reparse_path"]},
    )
    env_file.write_text(
        "EDUAGENT_ENVIRONMENT=test\nFAKE_ONLY=never-connect\n", encoding="utf-8"
    )
    compose_file.write_text(
        "# Fake only: no Compose process reads this file.\n", encoding="utf-8"
    )
    harness.write_text(POWERSHELL_HARNESS, encoding="utf-8")
    environment = _child_environment(powershell, tmp_path)
    environment.update(data["env"])
    before = _snapshot(project)
    stdout_path, stderr_path = tmp_path / "child.stdout.log", tmp_path / "child.stderr.log"
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        completed = subprocess.run(
            [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(harness),
                "-SourceScript",
                str(SMOKE_SCRIPT),
                "-CaseFile",
                str(case_file),
            ],
            cwd=project,
            env=environment,
            stdout=stdout,
            stderr=stderr,
            timeout=30,
            check=False,
        )
    output, errors = _bounded_output(stdout_path), _bounded_output(stderr_path)
    assert completed.returncode == 0, (
        f"Fake PowerShell harness 失败，exit={completed.returncode}\n{output}\n{errors}"
    )
    assert not errors.strip(), f"Fake PowerShell harness 有意外 stderr：\n{errors}"
    assert _snapshot(project) == before, "准入检查改写、创建或删除了合成资源文件"
    report = json.loads(output)
    assert report["powershell_major"] >= 7
    assert report["original_functions"] == sorted(ASSERT_NAMES)
    assert report["original_variables"] == ["containerProfileSource", "isolationEnvironment"]
    assert report["side_effects"] == [], "准入发生了 config 读取以外的 Docker 调用"
    assert report["fake_reparse_hits"] == int(data["reparse_path"] is not None)
    return report


def test_valid_profiles_execute_both_original_assertions(
    tmp_path: Path, powershell7: str
) -> None:
    report = _run_admission(tmp_path, powershell7)
    assert report["accepted"] is True
    assert report["entered"] == ASSERT_NAMES
    assert report["passed"] == ASSERT_NAMES
    assert report["docker_calls"] == [CONFIG_COMMAND]
    assert report["error"] is None
    assert report["container_source"] == str(
        (
            tmp_path
            / "fake-repository"
            / "resource-root"
            / "config"
            / "container-resource-profile.json"
        ).resolve()
    )


@pytest.mark.parametrize(
    ("key_path", "value", "error_fragment"),
    [
        pytest.param(
            ("write_host",),
            False,
            "缺少可读取的绝对 Test 资源 profile",
            id="missing-host",
        ),
        pytest.param(
            ("write_container",),
            False,
            "必须已存在且可读取",
            id="missing-container",
        ),
        pytest.param(
            ("env", "COMPOSE_PROJECT_NAME"),
            "eduagent",
            "COMPOSE_PROJECT_NAME",
            id="wrong-isolation",
        ),
        pytest.param(
            ("host", "environment"), "dev", "身份不符", id="wrong-host-environment"
        ),
        pytest.param(
            ("container", "environment"),
            "dev",
            "环境、资源根或资源身份",
            id="wrong-container-environment",
        ),
        pytest.param(
            ("host", "environment_id"),
            "00000000-0000-0000-0000-000000000000",
            "环境、资源根或资源身份",
            id="empty-environment-id",
        ),
        pytest.param(
            ("container", "environment_id"),
            "71497112-7459-4d0a-b9b9-4a41b80518f4",
            "环境、资源根或资源身份",
            id="different-environment-id",
        ),
        pytest.param(
            ("container", "database_identity", "instance_id"),
            "7500000000000000099",
            "不得改写主机数据库资源事实",
            id="different-db-instance",
        ),
        pytest.param(
            ("container", "redis_instance_id"),
            "other-fake-redis",
            "环境、资源根或资源身份",
            id="different-redis-instance",
        ),
        pytest.param(
            ("container", "database_identity", "host"),
            "127.0.0.1",
            "数据库身份字段或容器地址",
            id="wrong-db-host",
        ),
        pytest.param(
            ("container", "database_identity", "port"),
            15432,
            "数据库身份字段或容器地址",
            id="wrong-db-port",
        ),
        pytest.param(
            ("container", "database_identity", "database"),
            "eduagent_dev",
            "环境、资源根或资源身份",
            id="wrong-database",
        ),
        pytest.param(
            ("container", "database_identity", "user"),
            "eduagent_dev_runner",
            "环境、资源根或资源身份",
            id="wrong-db-user",
        ),
        pytest.param(
            ("container", "database_identity", "schema_name"),
            "private",
            "不得改写主机数据库资源事实",
            id="wrong-schema",
        ),
        pytest.param(
            ("container", "root"),
            lambda root: str(root) if os.name == "nt" else r"C:\fake\host-root",
            "环境、资源根或资源身份",
            id="windows-root-as-container",
        ),
        pytest.param(
            ("container", "credentials"),
            {"password": "fake-never-a-real-secret"},
            "环境、资源根或资源身份",
            id="extra-secret-field",
        ),
        pytest.param(
            ("container", "database_identity", "password"),
            "fake-never-a-real-secret",
            "数据库身份字段或容器地址",
            id="nested-secret-field",
        ),
        pytest.param(
            ("env", "EDUAGENT_TEST_CONTAINER_PROFILE"),
            lambda root: str(root.parent / "other-container-profile.json"),
            "来源必须为本次主机 Test 资源根内",
            id="misplaced-source",
        ),
        pytest.param(
            ("reparse_path",),
            lambda root: str(root / "config"),
            "不得经过重解析路径",
            id="fake-reparse-ancestor",
        ),
    ],
)
def test_host_admission_rejects_before_any_docker_call(
    tmp_path: Path,
    powershell7: str,
    key_path: tuple[str | int, ...],
    value: Any,
    error_fragment: str,
) -> None:
    report = _run_admission(tmp_path, powershell7, (key_path, value))
    assert report["accepted"] is False
    assert report["entered"] == ASSERT_NAMES[:1]
    assert report["passed"] == []
    assert report["docker_calls"] == []
    assert report["container_source"] is None
    assert error_fragment in report["error"]


@pytest.mark.parametrize(
    ("key_path", "value", "error_fragment"),
    [
        pytest.param(
            ("compose", "services", "backend", "volumes", 1, "read_only"),
            False,
            "挂载必须保留原可写 storage",
            id="writable-profile-bind",
        ),
        pytest.param(
            ("compose", "services", "backend", "volumes", 1, "source"),
            lambda root: str(root.parent / "other-container-profile.json"),
            "挂载必须保留原可写 storage",
            id="wrong-bind-source",
        ),
        pytest.param(
            (
                "compose",
                "services",
                "backend",
                "volumes",
                1,
                "bind",
                "create_host_path",
            ),
            True,
            "挂载必须保留原可写 storage",
            id="automatic-host-path-creation",
        ),
        pytest.param(
            ("compose", "services", "backend", "volumes"),
            lambda root: [
                {
                    "type": "volume",
                    "source": "storage_data",
                    "target": "/app/storage",
                    "read_only": False,
                },
                {
                    "type": "bind",
                    "source": str(root / "config" / "container-resource-profile.json"),
                    "target": "/run/eduagent/test-resource-profile.json",
                    "read_only": True,
                    "bind": {"create_host_path": False},
                },
                {"type": "volume", "source": "unknown_data", "target": "/app/unknown"},
            ],
            "Backend 仅允许原 storage 卷和单一容器 Test profile bind",
            id="extra-unknown-mount",
        ),
        pytest.param(
            ("compose", "services", "backend", "volumes", 0, "read_only"),
            True,
            "挂载必须保留原可写 storage",
            id="readonly-canonical-storage",
        ),
        pytest.param(
            ("compose", "volumes", "postgres_data", "name"),
            "eduagent_postgres_data",
            "postgres_data 不属于 Test 项目",
            id="wrong-postgres-volume",
        ),
        pytest.param(
            ("compose", "volumes", "redis_data", "name"),
            "eduagent_redis_data",
            "redis_data 不属于 Test 项目",
            id="wrong-redis-volume",
        ),
        pytest.param(
            ("compose", "volumes", "storage_data", "name"),
            "eduagent_storage_data",
            "storage_data 不属于 Test 项目",
            id="wrong-storage-volume",
        ),
        pytest.param(
            ("compose", "services", "backend", "ports", 0, "host_ip"),
            "0.0.0.0",
            "发布端口不符合 Test 约定",
            id="non-loopback-port",
        ),
        pytest.param(
            ("compose", "services", "backend", "environment", "EDUAGENT_DATA_ROOT"),
            lambda root: str(root),
            "Backend 必须显式使用容器 Test profile",
            id="host-root-in-backend-environment",
        ),
        # 补充审查发现的缺口：保留必须拒绝的断言，待父会话修复原 smoke。
        # 新分支尚无既定错误文案，只断言原 Compose Assert 报错，绝不 xfail。
        pytest.param(
            ("compose", "services", "postgres", "volumes", 0, "source"),
            "redis_data",
            None,
            id="postgres-wrong-mount-source",
        ),
        pytest.param(
            ("compose", "services", "postgres", "volumes", 0, "target"),
            "/var/lib/postgresql/wrong-data",
            None,
            id="postgres-wrong-mount-target",
        ),
        pytest.param(
            ("compose", "services", "postgres", "volumes"),
            lambda root: [
                {
                    "type": "volume",
                    "source": "postgres_data",
                    "target": "/var/lib/postgresql/data",
                    "read_only": False,
                    "volume": {},
                },
                {
                    "type": "bind",
                    "source": str(root / "config"),
                    "target": "/unexpected",
                    "read_only": True,
                    "bind": {"create_host_path": False},
                },
            ],
            None,
            id="postgres-extra-bind",
        ),
        pytest.param(
            ("compose", "services", "redis", "volumes", 0, "source"),
            "postgres_data",
            None,
            id="redis-wrong-mount-source",
        ),
        pytest.param(
            ("compose", "services", "redis", "volumes", 0, "target"),
            "/wrong-data",
            None,
            id="redis-wrong-mount-target",
        ),
        pytest.param(
            ("compose", "volumes", "unknown_data"),
            {"name": "eduagent-test_unknown_data"},
            None,
            id="extra-top-level-volume",
        ),
        pytest.param(
            ("compose", "services", "backend", "environment", "DATABASE_URL"),
            "postgresql+psycopg://eduagent_test_runner:synthetic-only"
            "@127.0.0.1:5432/eduagent_test",
            None,
            id="backend-database-wrong-host",
        ),
        pytest.param(
            ("compose", "services", "backend", "environment", "DATABASE_URL"),
            "postgresql+psycopg://eduagent_test_runner:synthetic-only"
            "@postgres:5432/eduagent_dev",
            None,
            id="backend-database-wrong-database",
        ),
        pytest.param(
            ("compose", "services", "backend", "environment", "DATABASE_URL"),
            "postgresql+psycopg://eduagent_dev_runner:synthetic-only"
            "@postgres:5432/eduagent_test",
            None,
            id="backend-database-wrong-user",
        ),
        pytest.param(
            ("compose", "services", "backend", "environment", "REDIS_URL"),
            "redis://127.0.0.1:6379/0",
            None,
            id="backend-redis-wrong-host",
        ),
    ],
)
def test_compose_admission_rejects_after_config_read_only(
    tmp_path: Path,
    powershell7: str,
    key_path: tuple[str | int, ...],
    value: Any,
    error_fragment: str | None,
) -> None:
    report = _run_admission(tmp_path, powershell7, (key_path, value))
    assert report["accepted"] is False
    assert report["entered"] == ASSERT_NAMES
    assert report["passed"] == ASSERT_NAMES[:1]
    assert report["docker_calls"] == [CONFIG_COMMAND]
    assert report["error"], "Compose 准入拒绝必须保留原异常信息"
    if error_fragment is not None:
        assert error_fragment in report["error"]
