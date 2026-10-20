"""P4.1：通过真实 Compose 装配验证 Demo 配置，不调用云端或下载模型。

测试变更依据：docs/test-change-record-p4.md。临时 env 只有测试值，绝不读取用户 .env。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def compose_config(tmp_path: Path) -> Callable[[dict[str, str]], dict[str, Any]]:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("P4.1 配置装配测试需要 Docker Compose CLI（无需启动引擎）。")
    version = subprocess.run(
        [docker, "compose", "version"], capture_output=True, timeout=15, check=False
    )
    if version.returncode:
        pytest.skip("P4.1 配置装配测试需要 Docker Compose CLI。")

    def assemble(overrides: dict[str, str]) -> dict[str, Any]:
        values = dict(dotenv_values(PROJECT_ROOT / ".env.example", interpolate=False))
        values.update(
            DATABASE_URL="postgresql+psycopg://dev@localhost:5432/dev",
            REDIS_URL="redis://localhost:6379/0",
            DEEPSEEK_API_KEY="test-only-llm-key",
            JWT_SECRET_KEY="test-only-jwt-secret-not-for-production",
            EMBEDDING_API_KEY="host-only-key-must-not-leak-to-demo-provider",
            EMBEDDING_BASE_URL="http://127.0.0.1:9/host",
        )
        values.update(overrides)
        env_file = tmp_path / "demo.env"
        env_file.write_text(
            "\n".join(f"{key}={value or ''}" for key, value in values.items()),
            encoding="utf-8",
        )
        # 不让调用者 shell 的配置覆盖本测试，也不读取其他 Compose 文件或用户 env。
        process_env = {
            key: value
            for key, value in os.environ.items()
            if key.upper() not in values
            and not key.upper().startswith(("COMPOSE_", "POSTGRES_"))
            and key.upper() not in {"REDIS_PORT", "BACKEND_PORT"}
        }
        process_env["EDUAGENT_ENV_FILE"] = str(env_file)
        files = ["--file", str(PROJECT_ROOT / "docker-compose.yml")]
        if "EDUAGENT_SECRETS_DIRECTORY" in overrides:
            files += ["--file", str(PROJECT_ROOT / "docker-compose.secrets.yml")]
        result = subprocess.run(
            [
                docker,
                "compose",
                "--project-name",
                "eduagent-p41-config-test",
                *files,
                "--env-file",
                str(env_file),
                "config",
                "--format",
                "json",
            ],
            cwd=PROJECT_ROOT,
            env=process_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    return assemble


def test_docker_bootstrap_does_not_copy_host_or_legacy_demo_keys(compose_config):
    config = compose_config({"DEMO_EMBEDDING_API_KEY": "test-only-demo-key"})
    backend = config["services"]["backend"]
    assert set(config["services"]) == {"backend", "postgres", "redis"}
    assert "depends_on" not in backend and "env_file" not in backend
    assert "args" not in backend["build"]
    assert not {"DEEPSEEK_API_KEY", "JWT_SECRET_KEY", "EMBEDDING_API_KEY"} & backend["environment"].keys()
    assert "host-only-key" not in json.dumps(backend) and "test-only-demo-key" not in json.dumps(backend)
    assert backend["environment"]["STORAGE_ROOT"] == "/app/storage/storage"
    assert backend["environment"]["EDUAGENT_DATA_ROOT"] == "/app/storage"
    assert backend["environment"]["DATABASE_URL"] == "postgresql+psycopg://eduagent@postgres:5432/eduagent"
    assert backend["environment"]["REDIS_URL"] == "redis://redis:6379/0"
    assert "http://127.0.0.1:8000/ready" in " ".join(backend["healthcheck"]["test"])
    assert [(v["type"], v["source"], v["target"]) for v in backend["volumes"]] == [
        ("volume", "storage_data", "/app/storage")
    ]
    for service in config["services"].values():
        assert all(port["host_ip"] == "127.0.0.1" for port in service["ports"])


@pytest.mark.parametrize("missing", ["DEMO_EMBEDDING_MODEL", "DEMO_EMBEDDING_API_KEY"])
def test_missing_model_configuration_still_assembles_bootstrap(compose_config, missing):
    config = compose_config({missing: ""})
    backend = config["services"]["backend"]
    assert backend["environment"]["EDUAGENT_SECRET_BACKEND"] == "mounted_secret"
    assert not backend["environment"]["EDUAGENT_SECRET_MOUNTS"]
    assert "depends_on" not in backend
    assert "host-only-key" not in json.dumps(backend)


def test_secret_overlay_requires_explicit_readonly_directory(compose_config, tmp_path):
    source = tmp_path / "owned-secrets"
    source.mkdir()
    catalog = {"model-v1": {"path": "/run/secrets/model-v1", "purpose": "provider"}}
    config = compose_config({
        "EDUAGENT_SECRETS_DIRECTORY": str(source),
        "EDUAGENT_SECRET_MOUNTS": json.dumps(catalog),
    })
    backend = config["services"]["backend"]
    assert json.loads(backend["environment"]["EDUAGENT_SECRET_MOUNTS"]) == catalog
    mount = next(m for m in backend["volumes"] if m["target"] == "/run/secrets")
    assert mount["type"] == "bind" and mount["read_only"] is True
    assert Path(mount["source"]) == source
    assert not mount.get("bind", {}).get("create_host_path", False)
    assert set(config["services"]) == {"backend", "postgres", "redis"}
