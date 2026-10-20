"""最小 Bootstrap 应用与业务装配边界（T218）。

引导段依赖环境/数据根/端口、设置授权与静态资源；不装配旧业务或 Gradio，不要求 JWT
或 AI 配置。个人科目面按当前空间独立核验数据库和文件根；未装配的旧业务 API 仍以
`503 BUSINESS_NOT_READY` 拒绝，设置授权与个人数据访问各守其边界。

业务装配协调（配置激活、前置检查、恢复钩子）归 T220：本模块只在“业务配置显式齐备”
时复用既有完整工厂，保持已装配应用的业务路由、JWT/RBAC、Workflow 与 Gradio 不变。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from fastapi import FastAPI, Request
from pydantic import ValidationError
from starlette.responses import JSONResponse, Response

from backend.app.core.environment import (
    EnvironmentSelectionError,
    prepare_runtime_paths,
    resolve_environment_profile,
)
from backend.app.core.settings_security import (
    SETTINGS_PATH_PREFIX,
    SettingsSecurityError,
    SetupAuthorizationMiddleware,
    SetupSecurityService,
    SetupSourcePolicy,
    build_process_setup_security,
    error_response,
    parse_authority,
)
from backend.app.schemas.settings import (
    BootstrapOptions,
    BootstrapSettings,
    PublicHealthResponse,
    SetupReadyResponse,
)

if TYPE_CHECKING:
    from backend.app.core.config import AppSettings

#: 业务未装配时的稳定错误码（契约：旧业务入口返回 503，不用 SPA HTML 冒充成功）。
BUSINESS_NOT_READY = "BUSINESS_NOT_READY"

BIND_HOST_ENV = "EDUAGENT_BIND_HOST"
BIND_PORT_ENV = "EDUAGENT_BIND_PORT"
SETTINGS_UI_ROOT_ENV = "EDUAGENT_SETTINGS_UI_ROOT"
DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_BIND_PORT = 8000

STATIC_AVAILABLE = "available"
STATIC_NOT_BUNDLED = "not_bundled"

_BUSINESS_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
#: Only these client-side routes may fall back to the settings UI entry file.
_SETTINGS_SPA_SEGMENTS = frozenset({"setup", "settings", "diagnostics", "business", "subjects"})


def listen_address(environ: Mapping[str, str] | None = None) -> tuple[str, int]:
    """解析本实例监听地址；与 `SetupSourcePolicy` 使用同一来源，避免两处漂移。"""

    source = os.environ if environ is None else environ
    host = (source.get(BIND_HOST_ENV) or DEFAULT_BIND_HOST).strip()
    if not host:
        raise ValueError("EDUAGENT_BIND_HOST_INVALID")
    raw_port = (source.get(BIND_PORT_ENV) or "").strip()
    port = DEFAULT_BIND_PORT
    if raw_port:
        try:
            port = int(raw_port)
        except ValueError:
            raise ValueError("EDUAGENT_BIND_PORT_INVALID") from None
    if not 1 <= port <= 65535:
        raise ValueError("EDUAGENT_BIND_PORT_INVALID")
    return host, port


def configured_bind(environ: Mapping[str, str] | None = None) -> BootstrapOptions | None:
    """仅当绑定被显式配置时返回它；未配置时不建立静态 Host 允许集合。"""

    source = os.environ if environ is None else environ
    if not (source.get(BIND_HOST_ENV) or "").strip() and not (source.get(BIND_PORT_ENV) or "").strip():
        return None
    try:
        host, port = listen_address(source)
    except ValueError:
        return None
    # `BootstrapOptions.bind_host` 是受限枚举；非本机监听名没有可精确匹配的客户端 Host。
    bind_host: Literal["127.0.0.1", "::1", "0.0.0.0"]
    if host == "127.0.0.1":
        bind_host = "127.0.0.1"
    elif host == "::1":
        bind_host = "::1"
    elif host == "0.0.0.0":
        bind_host = "0.0.0.0"
    else:
        return None
    return BootstrapOptions(bind_host=bind_host, port=port)


def settings_authorities(bind: BootstrapOptions | None) -> frozenset[str]:
    """把实际 loopback 绑定转换为精确的 Host 允许集合。"""

    if bind is None:
        return frozenset()
    port = bind.port
    if bind.bind_host == "127.0.0.1":
        return frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})
    if bind.bind_host == "::1":
        return frozenset({f"[::1]:{port}", f"localhost:{port}"})
    # 0.0.0.0：容器/局域网发布后的客户端 Host 由外部映射决定，无法精确匹配；
    # 继续依赖 Setup 令牌 + 同源 Origin，不因 Docker NAT 或转发头放行。
    return frozenset()


def settings_ui_root(environ: Mapping[str, str] | None = None) -> Path | None:
    """设置 UI bundle 目录；未显式配置时不臆造路径，也不伪造页面。"""

    source = os.environ if environ is None else environ
    raw = (source.get(SETTINGS_UI_ROOT_ENV) or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute() or path != path.resolve():
        return None
    return path


def static_bundle_state(root: Path | None) -> str:
    if root is not None and (root / "index.html").is_file():
        return STATIC_AVAILABLE
    return STATIC_NOT_BUNDLED


@dataclass(frozen=True, slots=True)
class BootstrapContext:
    """最小引导事实：设置身份、精确绑定与静态 bundle；失败只记录安全码。"""

    settings: BootstrapSettings | None
    bind: BootstrapOptions | None
    static_root: Path | None
    failure: str | None = None

    @property
    def static_resources(self) -> str:
        return static_bundle_state(self.static_root)


def resolve_bootstrap_context(
    *, environ: Mapping[str, str] | None = None
) -> BootstrapContext:
    """只读取环境身份/路径/端口；不建目录、不连服务、不读业务秘密。"""

    source = os.environ if environ is None else environ
    bind = configured_bind(source)
    ui_root = settings_ui_root(source)
    try:
        profile = resolve_environment_profile(process_env=source)
    except (EnvironmentSelectionError, ValueError, OSError):
        return BootstrapContext(
            settings=None,
            bind=bind,
            static_root=ui_root,
            failure="ENVIRONMENT_PROFILE_UNAVAILABLE",
        )
    settings = BootstrapSettings(
        environment=profile.environment,
        environment_id=profile.environment_id,
        runtime_kind=profile.runtime_kind,
        config_root=profile.config_root,
        static_root=ui_root,
        options=bind or BootstrapOptions(),
    )
    return BootstrapContext(settings=settings, bind=bind, static_root=ui_root)


def build_bootstrap_security(context: BootstrapContext) -> SetupSecurityService:
    """设置授权仍独立于业务 JWT/DB；绑定已知时同时拒绝非本机 Host。"""

    settings = context.settings
    if settings is None:
        return build_process_setup_security()
    authorities = settings_authorities(context.bind)
    # Docker NAT does not reveal the host's published port. Its deployment must
    # advertise that exact port; an absent/invalid mapping keeps personal routes closed.
    if not authorities and settings.runtime_kind.value == "docker":
        public_port = os.environ.get("EDUAGENT_PUBLIC_PORT", "")
        if public_port.isascii() and public_port.isdigit() and 1 <= int(public_port) <= 65535:
            authorities = frozenset({str(parse_authority(f"127.0.0.1:{int(public_port)}")),
                                     str(parse_authority(f"localhost:{int(public_port)}"))})
    return SetupSecurityService(
        environment_id=settings.environment_id,
        environment=settings.environment,
        runtime_kind=settings.runtime_kind,
        policy=SetupSourcePolicy(
            environment=settings.environment,
            runtime_kind=settings.runtime_kind,
            allowed_hosts=authorities,
        ),
    )


async def _settings_security_handler(_request: Request, exc: Exception) -> Response:
    """统一设置授权失败响应；不泄露内部异常原文。"""

    assert isinstance(exc, SettingsSecurityError)
    return error_response(exc)


def install_settings_surface(
    app: FastAPI,
    *,
    security: SetupSecurityService | None,
    configuration_available: bool,
    static_resources: str,
    failure: str | None = None,
) -> None:
    """挂载设置授权面与 `/setup-ready`；引导与完整应用共用同一实现。"""

    if security is not None:
        app.state.setup_security = security
        app.add_middleware(SetupAuthorizationMiddleware)
        # 延迟导入：最小引导模块导入期不得出现 backend.app.api.* 业务/接口模块。
        from backend.app.api.settings import router as settings_router

        app.include_router(settings_router)
        # T220：管理面的领域错误统一收敛为安全错误体；不改业务异常语义。
        from backend.app.api.settings import install_settings_api

        install_settings_api(app)
        app.add_exception_handler(SettingsSecurityError, _settings_security_handler)

    @app.get("/setup-ready", tags=["system"], response_model=SetupReadyResponse,
             responses={503: {"model": SetupReadyResponse}})
    def setup_ready() -> JSONResponse:
        """设置面可用性；不等同于业务就绪或 AI 可用。"""

        details = {
            "service": "backend",
            "setup": {
                "configuration": "available" if configuration_available else "invalid",
                "settings_authorization": (
                    "available" if security is not None else "unavailable"
                ),
                "static_resources": static_resources,
            },
        }
        if not configuration_available or security is None:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "not_ready",
                    **details,
                    "failed_check": "config" if not configuration_available else "assembly",
                    "detail": failure
                    or "设置面尚未就绪，请检查运行环境、数据目录与端口配置。",
                },
            )
        return JSONResponse(status_code=200, content={"status": "ok", **details})


def _not_ready_response(request: Request) -> JSONResponse:
    if request.url.path == SETTINGS_PATH_PREFIX or request.url.path.startswith(
        SETTINGS_PATH_PREFIX + "/"
    ):
        # 设置端点未注册时保持真实 404，不用“业务未就绪”冒充设置语义。
        return JSONResponse(
            status_code=404,
            content={"code": "NOT_FOUND", "message": "设置端点尚未提供。"},
        )
    return JSONResponse(
        status_code=503,
        content={
            "code": BUSINESS_NOT_READY,
            "message": "业务入口尚未装配，请先完成设置。",
            "stage": "business_assembly",
            "retryable": True,
            "correlation_id": uuid4().hex,
        },
    )


async def _api_root_not_ready(request: Request) -> JSONResponse:
    return _not_ready_response(request)


async def _api_path_not_ready(request: Request, rest: str) -> JSONResponse:
    del rest
    return _not_ready_response(request)


async def _settings_unavailable(_request: Request) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "code": "SETTINGS_SECURITY_UNAVAILABLE",
            "message": "设置授权服务不可用，请检查运行环境与数据目录。",
            "stage": "settings_authorization",
            "retryable": True,
            "correlation_id": uuid4().hex,
        },
    )


async def _settings_path_unavailable(_request: Request, rest: str) -> JSONResponse:
    del rest
    return await _settings_unavailable(_request)


def register_settings_unavailable(app: FastAPI) -> None:
    """无引导身份时失败关闭：设置命名空间明确 503，而不是冒充业务未就绪。"""

    app.add_api_route(
        SETTINGS_PATH_PREFIX,
        _settings_unavailable,
        methods=list(_BUSINESS_METHODS),
        include_in_schema=False,
    )
    app.add_api_route(
        SETTINGS_PATH_PREFIX + "/{rest:path}",
        _settings_path_unavailable,
        methods=list(_BUSINESS_METHODS),
        include_in_schema=False,
    )


def register_business_not_ready(app: FastAPI) -> None:
    """未装配业务时的入口保护：真实 HTTP 503，先于任何业务鉴权。"""

    app.add_api_route(
        "/api", _api_root_not_ready, methods=list(_BUSINESS_METHODS), include_in_schema=False
    )
    app.add_api_route(
        "/api/{rest:path}",
        _api_path_not_ready,
        methods=list(_BUSINESS_METHODS),
        include_in_schema=False,
    )


def _is_settings_spa_path(path: str) -> bool:
    """只允许已登记的个人/设置路由回退，避免 HTML 吞掉 API 或未知资源。"""

    normalized = path.replace("\\", "/").strip("/")
    segment = normalized.split("/", 1)[0]
    return segment in _SETTINGS_SPA_SEGMENTS


def mount_static_bundle(app: FastAPI, root: Path | None) -> str:
    """bundle 存在才挂载（SPA 入口），否则不占位、不伪造页面。"""

    state = static_bundle_state(root)
    if state == STATIC_AVAILABLE and root is not None:
        from starlette.exceptions import HTTPException
        from starlette.staticfiles import StaticFiles

        class SettingsUiStaticFiles(StaticFiles):
            """为已确认的个人和设置路由提供客户端入口。"""

            async def get_response(self, path: str, scope: Any) -> Response:
                try:
                    return await super().get_response(path, scope)
                except HTTPException as exc:
                    method = scope.get("method", "GET")
                    if (
                        exc.status_code != 404
                        or method not in {"GET", "HEAD"}
                        or not _is_settings_spa_path(path)
                    ):
                        raise
                    return await super().get_response("index.html", scope)

        app.mount(
            "/",
            SettingsUiStaticFiles(directory=str(root), html=True),
            name="settings-ui",
        )
    return state


@asynccontextmanager
async def _bootstrap_lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        yield
    finally:
        service = getattr(app.state, "subject_service", None)
        if service is not None:
            await service.wait_for_local_work()
        operations = getattr(app.state, "learning_operations", None)
        if operations is not None:
            await operations.aclose()
        if service is not None:
            service.close()


def create_bootstrap_app(
    *,
    context: BootstrapContext | None = None,
    settings_runtime: Any = None,
    business_failure: tuple[str, str] | None = None,
) -> FastAPI:
    """最小根应用：存活、设置面、静态挂载与业务未就绪保护。"""

    resolved = context if context is not None else resolve_bootstrap_context()
    app = FastAPI(title="EduAgent Settings", version="0.1.0", lifespan=_bootstrap_lifespan)
    app.state.bootstrap = resolved
    app.state.business_assembly = "not_assembled"
    app.state.recovery = "not_started"
    app.state.settings_runtime = settings_runtime
    app.state.business_candidate = None
    #: 前置检查失败时的安全分类与说明；只含既有 failed_check 码，不含异常原文。
    app.state.business_failure = business_failure

    @app.get("/health", tags=["system"], response_model=PublicHealthResponse)
    def health() -> dict[str, str]:
        """存活检查：与依赖、业务装配和 AI 状态无关。"""

        return {"status": "ok", "service": "backend"}

    @app.get("/ready", tags=["system"])
    def ready() -> JSONResponse:
        """业务就绪：同一分层门禁；未装配业务时保持 503，AI 状态不在此判断。"""

        # 延迟导入，保持引导模块导入期轻量。
        from backend.app.services.settings import readiness

        failure = app.state.business_failure
        if failure is not None:
            failed_check, detail = failure
            return JSONResponse(
                status_code=503,
                content={
                    "status": "not_ready",
                    "service": "backend",
                    "failed_check": failed_check,
                    "detail": detail,
                },
            )
        inputs = readiness.inputs_for_app(app, business_settings=None)
        failure = readiness.gate_ready(readiness.evaluate_readiness(inputs))
        failed_check, detail = (
            failure
            if failure is not None
            else ("business", "业务入口未达到就绪状态。")
        )
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "service": "backend",
                "failed_check": failed_check,
                "detail": detail,
            },
        )

    security = (
        build_bootstrap_security(resolved) if resolved.settings is not None else None
    )
    install_settings_surface(
        app,
        security=security,
        configuration_available=resolved.settings is not None,
        static_resources=resolved.static_resources,
        failure="最小引导配置不可读取，请按运行方式检查环境、数据根与端口。",
    )
    if security is None:
        register_settings_unavailable(app)
    from backend.app.api.subjects import install_subjects

    install_subjects(app)
    register_business_not_ready(app)
    mount_static_bundle(app, resolved.static_root)
    return app


def load_business_settings() -> AppSettings | None:
    """仅用当前进程环境判断业务配置是否显式齐备；不隐式读取仓库/当前目录 `.env`。"""

    from backend.app.core.config import AppSettings

    try:
        # pydantic-settings 的 `_env_file` 是运行期初始化参数，其 mypy 插件签名未包含它。
        return AppSettings(_env_file=None)  # type: ignore[call-arg]
    except (ValidationError, ValueError):
        return None


def create_application(
    *,
    settings: AppSettings | None = None,
    context: BootstrapContext | None = None,
    settings_runtime: Any = None,
    probes: Any = None,
) -> FastAPI:
    """按「前置检查 → 装配 → 恢复」选择引导/完整应用。

    ``settings`` 显式传入时视为调用方已经完成启动前置（测试/嵌入使用），
    沿用原完整工厂；真实进程入口（``backend.main``）不传，先读取已激活修订、
    做短超时前置检查，未通过则保持 Setup，不提前装配业务或宣告 ready。
    """

    resolved = context if context is not None else resolve_bootstrap_context()
    if settings is not None:
        from backend.app.core.app import create_app

        return create_app(
            settings=settings,
            bootstrap_context=resolved,
            settings_runtime=settings_runtime,
        )
    if resolved.settings is None:
        return create_bootstrap_app(
            context=resolved,
            settings_runtime=settings_runtime,
            business_failure=("config", "最小引导配置不可读取，请检查运行环境与数据目录。"),
        )
    from backend.app.services.settings.management import (
        SettingsManagementError,
        SettingsRuntime,
        build_settings_runtime,
    )

    runtime = settings_runtime if isinstance(settings_runtime, SettingsRuntime) else None
    if runtime is None:
        try:
            profile = resolve_environment_profile()
        except (EnvironmentSelectionError, ValueError, OSError):
            return create_bootstrap_app(
                context=resolved,
                business_failure=(
                    "config",
                    "运行环境或数据目录不可用，设置面保持最小引导。",
                ),
            )
        runtime = build_settings_runtime(profile)
    # 只在新的、尚未对外提供路由的应用工厂接纳 pending；普通 GET/诊断
    # 与当前已挂载应用不会走这里，也不重新执行已接纳模型的 Probe。
    business, startup_pending, reason = runtime.pending_startup_settings()
    if reason is not None:
        return create_bootstrap_app(
            context=resolved, settings_runtime=runtime,
            business_failure=("assembly", f"待重启配置未通过本地接纳（{reason}），原修订与候选保持不变。"),
        )
    if startup_pending is None:
        business, reason = runtime.effective_business_settings()
    if business is None:
        if startup_pending is not None:
            runtime.accept_pending_startup(startup_pending)
        return create_bootstrap_app(
            context=resolved,
            settings_runtime=runtime,
            business_failure=(
                "assembly",
                f"业务配置尚未可用（{reason or 'CONFIGURATION_UNAVAILABLE'}），业务入口保持未装配。",
            ),
        )
    failure = runtime.prerequisites_failure(business, probes=probes)
    if failure is not None:
        application = create_bootstrap_app(
            context=resolved,
            settings_runtime=runtime,
            business_failure=failure,
        )
        application.state.business_candidate = business
        return application
    # 真实进程启动：沿用既有路径准备（Gradio/模型缓存）后再导入业务工厂。
    prepare_runtime_paths(resolve_environment_profile())
    # 已激活配置成为业务消费者的设置来源；不写进程环境、不读仓库 `.env`。
    from backend.app.core.config import install_runtime_settings

    install_runtime_settings(business)
    from backend.app.core.app import create_app

    try:
        application = create_app(
            settings=business, bootstrap_context=resolved, settings_runtime=runtime,
        )
        if startup_pending is not None:
            facts = runtime.deployment_facts()
            runtime.accept_pending_startup(startup_pending)
            facts.update(configuration_revision=str(startup_pending), pending_revision=None)
            for name, value in facts.items():
                setattr(application.state, name, value)
        return application
    except Exception as error:
        if startup_pending is None:
            raise
        # 工厂尚未交给 HTTP 服务器；构造或最终本地门禁失败不能提交 pending。
        from backend.app.core.config import reset_settings_cache

        reset_settings_cache()
        code = error.code if isinstance(error, SettingsManagementError) else "ASSEMBLY_FAILED"
        return create_bootstrap_app(
            context=resolved, settings_runtime=runtime,
            business_failure=("assembly", f"待重启配置未完成装配（{code}），请检查诊断后显式重试。"),
        )


__all__ = [
    "BIND_HOST_ENV",
    "BIND_PORT_ENV",
    "BUSINESS_NOT_READY",
    "DEFAULT_BIND_HOST",
    "DEFAULT_BIND_PORT",
    "SETTINGS_UI_ROOT_ENV",
    "STATIC_AVAILABLE",
    "STATIC_NOT_BUNDLED",
    "BootstrapContext",
    "build_bootstrap_security",
    "configured_bind",
    "create_application",
    "create_bootstrap_app",
    "install_settings_surface",
    "listen_address",
    "load_business_settings",
    "mount_static_bundle",
    "register_business_not_ready",
    "register_settings_unavailable",
    "resolve_bootstrap_context",
    "settings_authorities",
    "settings_ui_root",
    "static_bundle_state",
]
