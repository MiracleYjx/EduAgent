"""TCR-S3-BOOTSTRAP-HEALTH-018（T216）：引导层导入与边界单元契约。

固定 T218 的启动边界：最小引导模块不得以数据库/密钥/JWT 为导入前置，不得隐式
拉起业务 API，也不得为设置入口挂载完整 Gradio UI（E1 只需要静态外壳）。

`tests/unit/settings/conftest.py` 已在本目录阻断真实 socket 与 Engine.connect，
因此任何“引导期连数据库/建网络”的实现都会在这里暴露为失败，而不是被容忍。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

T218 = "T218_BOOTSTRAP_REQUIRED"

#: 业务强校验项：最小引导不应要求其中任何一项存在。
BUSINESS_ONLY_ENV = (
    "DATABASE_URL",
    "REDIS_URL",
    "JWT_SECRET_KEY",
    "DEEPSEEK_API_KEY",
    "EMBEDDING_API_KEY",
    "LLM_PROVIDER",
    "EMBEDDING_PROVIDER",
)


def _minimal_environment(monkeypatch: pytest.MonkeyPatch, data_root: Path) -> None:
    for name in BUSINESS_ONLY_ENV:
        monkeypatch.delenv(name, raising=False)
    # 最小引导仍需要环境身份与数据根，但不得要求业务数据库/密钥/JWT。
    monkeypatch.setenv("EDUAGENT_ENVIRONMENT", "development")
    monkeypatch.setenv("EDUAGENT_DATA_ROOT", str(data_root))
    monkeypatch.setenv("GRADIO_ANALYTICS_ENABLED", "False")


def _import_bootstrap(monkeypatch: pytest.MonkeyPatch, data_root: Path):
    _minimal_environment(monkeypatch, data_root)
    try:
        return importlib.import_module("backend.app.core.bootstrap")
    except ImportError as error:
        pytest.fail(
            f"{T218}：backend.app.core.bootstrap 尚未提供（{error}）", pytrace=False
        )


def test_bootstrap_module_imports_without_business_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _import_bootstrap(monkeypatch, tmp_path)

    assert module is not None


def test_bootstrap_import_does_not_load_business_api_modules(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in [name for name in sys.modules if name.startswith("backend.app.api.")]:
        monkeypatch.delitem(sys.modules, name)

    _import_bootstrap(monkeypatch, tmp_path)

    loaded = {
        name
        for name in sys.modules
        if name.startswith("backend.app.api.") and name.count(".") == 3
    }
    assert loaded == set()


def test_bootstrap_module_does_not_require_full_gradio_ui(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delitem(sys.modules, "backend.app.ui.gradio_app", raising=False)

    _import_bootstrap(monkeypatch, tmp_path)

    assert "backend.app.ui.gradio_app" not in sys.modules
