"""T087 一键脚本合同：真实 PowerShell 解析/执行，只替换 Docker 命令边界。"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_demo.ps1"


@pytest.fixture
def powershell():
    command = shutil.which("pwsh") or shutil.which("powershell")
    if not command:
        pytest.skip("T087 脚本验证需要 PowerShell。")
    return command


def test_run_demo_script_exists_and_parses(powershell):
    assert SCRIPT.is_file()
    result = subprocess.run([
        powershell, "-NoProfile", "-NonInteractive", "-Command",
        ("$tokens=$null; $errors=$null; "
         "[System.Management.Automation.Language.Parser]::ParseFile("
         f"'{SCRIPT.as_posix()}', [ref]$tokens, [ref]$errors) | Out-Null; "
         "if ($errors.Count) { $errors; exit 1 }"),
    ], capture_output=True, text=True, encoding="utf-8", timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def run_demo(powershell, tmp_path):
    config_path = tmp_path / "compose.json"
    calls_path = tmp_path / "calls.jsonl"
    env_path = tmp_path / "demo.env"
    env_path.write_text("# test env only\n", encoding="utf-8")
    driver = tmp_path / "driver.ps1"
    driver.write_text(
        "function global:docker {\n"
        "  Add-Content -LiteralPath $env:T087_CALLS -Value "
        "(ConvertTo-Json -InputObject @($args) -Compress)\n"
        "  $global:LASTEXITCODE = 0\n"
        "  if ($args -contains 'config') { Get-Content -LiteralPath $env:T087_CONFIG -Raw }\n"
        "  if ($env:T087_FAIL -and $args -contains $env:T087_FAIL) { $global:LASTEXITCODE = 23 }\n"
        "}\n"
        f"& '{SCRIPT.as_posix()}' -EnvFile $env:T087_ENV -WaitTimeoutSeconds 30\n"
        "if ($LASTEXITCODE) { exit $LASTEXITCODE }\n"
        "if ($env:EDUAGENT_ENV_FILE -ne 'original-test.env') { exit 99 }\n",
        encoding="utf-8",
    )

    def execute(*, missing=None, fail=""):
        environment = {
            "JWT_SECRET_KEY": "sensitive-test-secret-" * 3,
            "DEV_MODE": "true", "LLM_PROVIDER": "deepseek",
            "DEEPSEEK_MODEL": "deepseek-chat", "DEEPSEEK_BASE_URL": "https://example.invalid",
            "DEEPSEEK_API_KEY": "sensitive-test-api-key", "RERANK_PROVIDER": "llm",
            "CONFIDENCE_THRESHOLD": "0.80", "EMBEDDING_MODEL": "test-model-1024",
            "EMBEDDING_API_KEY": "sensitive-test-embedding-key",
        }
        if missing:
            environment.pop(missing)
        config_path.write_text(json.dumps({"services": {"backend": {
            "environment": environment, "ports": [{"target": 8000, "published": "18000"}],
        }}}), encoding="utf-8")
        result = subprocess.run([
            powershell, "-NoProfile", "-NonInteractive", "-File", str(driver),
        ], cwd=tmp_path, env={**os.environ, "T087_CONFIG": str(config_path),
                             "T087_CALLS": str(calls_path), "T087_ENV": str(env_path),
                             "T087_FAIL": fail, "EDUAGENT_ENV_FILE": "original-test.env"},
            capture_output=True, text=True, encoding="utf-8", timeout=30, check=False)
        calls = [json.loads(line) for line in calls_path.read_text(encoding="utf-8-sig").splitlines()]
        assert "sensitive-test" not in result.stdout + result.stderr
        return result, calls

    return execute


def test_run_demo_waits_then_migrates_then_seeds_before_reporting_success(run_demo):
    result, calls = run_demo()
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(calls) == 4
    assert calls[0][-3:] == ["config", "--format", "json"]
    assert calls[1][-6:] == ["up", "-d", "--build", "--wait", "--wait-timeout", "30"]
    assert calls[2][-6:] == ["exec", "-T", "backend", "alembic", "upgrade", "head"]
    assert calls[3][-6:] == ["exec", "-T", "backend", "python", "-m", "scripts.demo_seed"]
    assert "http://127.0.0.1:18000/gradio" in result.stdout
    assert "dev_teacher / dev_student / dev_admin" in result.stdout
    assert "Demo UI running" in result.stdout and "Demo ready" not in result.stdout
    assert "awaiting_teacher_review" in result.stdout
    assert "awaiting_exam_scoring" in result.stdout


@pytest.mark.parametrize("missing", ["DEV_MODE", "EMBEDDING_API_KEY", "JWT_SECRET_KEY"])
def test_run_demo_missing_config_stops_before_startup(run_demo, missing):
    result, calls = run_demo(missing=missing)
    assert result.returncode == 1
    assert len(calls) == 1
    assert "DEMO_" in result.stdout and "Demo ready" not in result.stdout


@pytest.mark.parametrize(("stage", "call_count"), [("up", 2), ("alembic", 3), ("scripts.demo_seed", 4)])
def test_run_demo_command_failure_stops_later_steps(run_demo, stage, call_count):
    result, calls = run_demo(fail=stage)
    assert result.returncode == 1
    assert len(calls) == call_count
    assert "DEMO_STEP_FAILED" in result.stdout and "Demo ready" not in result.stdout
