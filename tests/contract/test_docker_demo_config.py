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

from backend.app.ai.embedding.base import EmbeddingProviderNotReadyError
from backend.app.ai.embedding.factory import create_embedding_provider
from backend.app.core.config import AppSettings

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
        result = subprocess.run(
            [
                docker,
                "compose",
                "--project-name",
                "eduagent-p41-config-test",
                "--file",
                str(PROJECT_ROOT / "docker-compose.yml"),
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


def _settings(backend: dict[str, Any]) -> AppSettings:
    environment = backend["environment"]
    values = {
        field: environment[field.upper()]
        for field in AppSettings.model_fields
        if field.upper() in environment
    }
    return AppSettings(_env_file=None, **values)


def test_demo_cloud_configuration_is_separate_from_host_bge(
    compose_config: Callable[[dict[str, str]], dict[str, Any]],
) -> None:
    config = compose_config(
        {
            "DEMO_EMBEDDING_MODEL": "test-cloud-model-1024",
            "DEMO_EMBEDDING_BASE_URL": "http://127.0.0.1:9/v1",
            "DEMO_EMBEDDING_API_KEY": "test-only-demo-key",
        }
    )
    backend = config["services"]["backend"]
    settings = _settings(backend)
    assert settings.embedding_provider == "openai_compatible"
    assert settings.embedding_model == "test-cloud-model-1024"
    assert str(settings.embedding_base_url) == "http://127.0.0.1:9/v1"
    assert settings.embedding_api_key.get_secret_value() == "test-only-demo-key"
    assert str(settings.database_url) == "postgresql+psycopg://eduagent@postgres:5432/eduagent"
    assert str(settings.redis_url) == "redis://redis:6379/0"
    assert "args" not in backend["build"]  # 密钥只在运行环境，不传入镜像构建。
    assert not backend.get("volumes")  # Demo 不依赖主机本地模型缓存。
    assert create_embedding_provider(settings).is_ready()


@pytest.mark.parametrize("missing", ["DEMO_EMBEDDING_MODEL", "DEMO_EMBEDDING_API_KEY"])
def test_missing_demo_configuration_fails_explicitly_without_host_fallback(
    compose_config: Callable[[dict[str, str]], dict[str, Any]], missing: str
) -> None:
    overrides = {
        "DEMO_EMBEDDING_MODEL": "test-cloud-model-1024",
        "DEMO_EMBEDDING_BASE_URL": "http://127.0.0.1:9/v1",
        "DEMO_EMBEDDING_API_KEY": "test-only-demo-key",
        missing: "",
    }
    config = compose_config(overrides)
    # 缺 Demo 配置仍能装配基础设施，主机开发只启动 postgres/redis 不受影响。
    assert {"postgres", "redis"} <= config["services"].keys()
    settings = _settings(config["services"]["backend"])
    with pytest.raises(EmbeddingProviderNotReadyError) as caught:
        create_embedding_provider(settings)
    assert caught.value.error_code == "EMBEDDING_PROVIDER_NOT_READY"
    assert missing.removeprefix("DEMO_") in caught.value.detail
    assert "host-only-key" not in str(caught.value)
    assert "test-only-demo-key" not in str(caught.value)
