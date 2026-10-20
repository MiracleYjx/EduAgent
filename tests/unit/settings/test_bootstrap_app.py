"""TCR-S3-BOOTSTRAP-SPLIT-020（T218）：最小引导单元契约。

只验证引导段本身：监听绑定→精确 Host 允许集合、环境不可用时的安全降级、静态 bundle
判定，以及“引导装配不导入业务路由/Gradio”。

本目录 `conftest.py` 已阻断真实 socket 与 Engine.connect（Windows 事件循环自管道也会被
拦），因此 **HTTP 请求一律放在 `tests/contract/test_bootstrap_api.py`**；这里不启动
lifespan、不建目录、不连服务。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from uuid import UUID

import pytest

from backend.app.core.bootstrap import (
    BootstrapContext,
    configured_bind,
    create_application,
    create_bootstrap_app,
    listen_address,
    resolve_bootstrap_context,
    settings_authorities,
    settings_ui_root,
    static_bundle_state,
)
from backend.app.core.config import AppSettings, install_runtime_settings
from backend.app.core.environment import (
    EnvironmentSelectionError,
    resolve_environment_profile,
)
from backend.app.core.settings_security import SetupSecurityService
from backend.app.schemas.settings import BootstrapOptions, EnvironmentName, RuntimeKind

BUSINESS_ENV = {
    "DATABASE_URL",
    "REDIS_URL",
    "JWT_SECRET_KEY",
    "DEEPSEEK_API_KEY",
    "EMBEDDING_API_KEY",
    "LLM_PROVIDER",
    "EMBEDDING_PROVIDER",
}


def _minimal_environ(root: Path, **overrides: str) -> dict[str, str]:
    """模拟“只有环境与数据根”的最小启动环境（不复制宿主秘密）。"""

    values = {
        "EDUAGENT_ENVIRONMENT": "development",
        "EDUAGENT_DATA_ROOT": str(root / "data"),
        "GRADIO_ANALYTICS_ENABLED": "False",
    }
    values.update(overrides)
    return values


@pytest.fixture(autouse=True)
def _no_ambient_bootstrap_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in BUSINESS_ENV:
        monkeypatch.delenv(name, raising=False)
    for name in ("EDUAGENT_BIND_HOST", "EDUAGENT_BIND_PORT", "EDUAGENT_SETTINGS_UI_ROOT"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    return tmp_path.resolve()


# ---------------------------------------------------------------------------
# 监听绑定与来源策略
# ---------------------------------------------------------------------------


def test_default_listen_address_is_loopback() -> None:
    assert listen_address({}) == ("127.0.0.1", 8000)
    assert listen_address(
        {"EDUAGENT_BIND_HOST": "0.0.0.0", "EDUAGENT_BIND_PORT": "8123"}
    ) == ("0.0.0.0", 8123)


@pytest.mark.parametrize(
    "environ",
    [
        {"EDUAGENT_BIND_PORT": "0"},
        {"EDUAGENT_BIND_PORT": "65536"},
        {"EDUAGENT_BIND_PORT": "abc"},
        {"EDUAGENT_BIND_HOST": "   "},
    ],
)
def test_invalid_bind_is_rejected_loudly(environ: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        listen_address(environ)


def test_bind_authorities_are_exact_and_loopback_only() -> None:
    assert settings_authorities(
        BootstrapOptions(bind_host="127.0.0.1", port=8123)
    ) == frozenset({"127.0.0.1:8123", "localhost:8123"})
    assert settings_authorities(
        BootstrapOptions(bind_host="::1", port=8123)
    ) == frozenset({"[::1]:8123", "localhost:8123"})
    # 0.0.0.0：发布后的客户端 Host 不可知，不建立“任意 localhost/任意端口”放行。
    assert settings_authorities(BootstrapOptions(bind_host="0.0.0.0", port=8123)) == frozenset()
    assert settings_authorities(None) == frozenset()


def test_bind_must_be_explicit_and_unsupported_names_never_widen_policy() -> None:
    assert configured_bind({}) is None
    assert configured_bind({"EDUAGENT_BIND_PORT": "8001"}) == BootstrapOptions(
        bind_host="127.0.0.1", port=8001
    )
    assert configured_bind({"EDUAGENT_BIND_PORT": "not-a-port"}) is None
    # 非受支持的监听名（如局域网 IP）不会退化成“任意 Host 放行”。
    assert configured_bind({"EDUAGENT_BIND_HOST": "192.168.1.10"}) is None


def test_static_bundle_state_requires_real_index(root: Path) -> None:
    assert static_bundle_state(None) == "not_bundled"
    assert static_bundle_state(root) == "not_bundled"
    (root / "index.html").write_text("<html></html>", encoding="utf-8")
    assert static_bundle_state(root) == "available"
    assert settings_ui_root({"EDUAGENT_SETTINGS_UI_ROOT": "relative/ui"}) is None
    assert settings_ui_root({}) is None
    assert settings_ui_root({"EDUAGENT_SETTINGS_UI_ROOT": str(root)}) == root


# ---------------------------------------------------------------------------
# 引导上下文与装配边界
# ---------------------------------------------------------------------------


def test_context_carries_identity_without_business_configuration(root: Path) -> None:
    context = resolve_bootstrap_context(environ=_minimal_environ(root))

    assert context.settings is not None
    assert context.settings.environment is EnvironmentName.DEVELOPMENT
    assert isinstance(context.settings.environment_id, UUID)
    assert context.settings.config_root.is_absolute()
    assert context.bind is None
    assert context.static_resources == "not_bundled"
    assert context.failure is None


def test_context_reports_degraded_state_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch, root: Path
) -> None:
    def unavailable(**_kwargs: object) -> object:
        raise EnvironmentSelectionError("ENVIRONMENT_PROFILE_UNAVAILABLE")

    monkeypatch.setattr("backend.app.core.bootstrap.resolve_environment_profile", unavailable)

    context = resolve_bootstrap_context(environ=_minimal_environ(root))

    assert context.settings is None
    assert context.failure == "ENVIRONMENT_PROFILE_UNAVAILABLE"


def test_bootstrap_app_does_not_import_business_routes_or_gradio(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watched = (
        "backend.app.ui.gradio_app",
        "backend.app.api.courses",
        "backend.app.api.workflow",
        "backend.app.core.app",
    )
    for name in watched:
        monkeypatch.delitem(sys.modules, name, raising=False)

    create_bootstrap_app(context=resolve_bootstrap_context(environ=_minimal_environ(root)))

    for name in watched:
        assert name not in sys.modules, f"{name} 不应在最小引导装配时被导入"


def test_bootstrap_security_uses_context_identity(root: Path) -> None:
    context = resolve_bootstrap_context(environ=_minimal_environ(root))
    application = create_bootstrap_app(context=context)
    security = application.state.setup_security

    assert context.settings is not None
    assert isinstance(security, SetupSecurityService)
    assert security.environment_id == context.settings.environment_id
    assert security.instance_id is not None
    assert security.runtime_kind is RuntimeKind.PYTHON


def test_degraded_context_still_builds_a_safe_minimal_app() -> None:
    context = BootstrapContext(
        settings=None,
        bind=None,
        static_root=None,
        failure="ENVIRONMENT_PROFILE_UNAVAILABLE",
    )

    application = create_bootstrap_app(context=context)

    assert application.state.business_assembly == "not_assembled"
    assert not hasattr(application.state, "setup_security")


def test_assembly_prepares_runtime_paths_before_business_factory_import(
    monkeypatch: pytest.MonkeyPatch, root: Path
) -> None:
    """T218/T202：完整装配必须先准备运行路径，再导入业务工厂。"""

    monkeypatch.setattr(tempfile, "tempdir", None)
    environ = _minimal_environ(root)
    environ.update(
        {
            "DATABASE_URL": "postgresql+psycopg://synthetic:synthetic@127.0.0.1:1/synthetic",
            "REDIS_URL": "redis://127.0.0.1:1/0",
            "JWT_SECRET_KEY": "synthetic-jwt-secret-that-is-not-a-production-secret",
            "LLM_PROVIDER": "none",
            "EMBEDDING_PROVIDER": "none",
            "RERANK_PROVIDER": "none",
            "CONFIDENCE_THRESHOLD": "0.8",
        }
    )
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    # 宿主环境的小写 AI 字段不得影响显式 none 的合成业务配置。
    for name in (
        "DEEPSEEK_BASE_URL",
        "DEEPSEEK_MODEL",
        "EMBEDDING_MODEL",
        "EMBEDDING_BASE_URL",
        "VISION_MODEL",
        "RERANK_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)

    business = AppSettings(_env_file=None)
    selected = resolve_environment_profile()

    class Runtime:
        def pending_startup_settings(self):
            return None, None, None

        def effective_business_settings(self):
            return business, None

        def prerequisites_failure(self, *_args: object, **_kwargs: object) -> None:
            return None

    monkeypatch.setattr(
        "backend.app.services.settings.management.build_settings_runtime",
        lambda _profile: Runtime(),
    )
    observed: list[str] = []
    app_module = ModuleType("backend.app.core.app")

    def create_app(**_kwargs: object) -> object:
        assert Path(tempfile.gettempdir()) == selected.temp_root
        assert Path(os.environ["HF_HOME"]).is_relative_to(selected.cache_root)
        assert Path(os.environ["TORCH_HOME"]).is_relative_to(selected.cache_root)
        assert selected.log_root.is_dir()
        observed.append("factory")
        return object()

    app_module.create_app = create_app  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "backend.app.core.app", app_module)
    try:
        application = create_application()
    finally:
        install_runtime_settings(None)

    assert application is not None and observed == ["factory"]
    assert not selected.storage_root.exists()


def test_bootstrap_mode_does_not_prepare_business_runtime_paths(
    monkeypatch: pytest.MonkeyPatch, root: Path
) -> None:
    """引导段不得产生业务运行目录副作用，也不得调用完整工厂。"""

    prepared: list[str] = []

    def record(*_args: object, **_kwargs: object) -> None:
        prepared.append("prepared")

    monkeypatch.setattr("backend.app.core.bootstrap.prepare_runtime_paths", record)
    monkeypatch.setattr("backend.app.core.bootstrap.load_business_settings", lambda: None)

    application = create_application(
        context=resolve_bootstrap_context(environ=_minimal_environ(root))
    )

    assert prepared == []
    assert application.state.business_assembly == "not_assembled"
