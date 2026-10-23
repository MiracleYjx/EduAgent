"""FastAPI 应用工厂与共享 HTTP 入口点。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend.app.ai.errors import AIConfigurationError
from backend.app.core.bootstrap import (
    BootstrapContext,
    build_bootstrap_security,
    install_settings_surface,
    mount_static_bundle,
    resolve_bootstrap_context,
)
from backend.app.core.config import (
    EMBEDDING_DIMENSION_DEFAULT,
    AppSettings,
    ConfigurationError,
    get_settings,
)
from backend.app.core.settings_security import SetupSecurityService
from backend.app.services.settings import readiness


class HealthResponse(BaseModel):
    """容器健康检查端点返回的稳定响应结构."""

    status: str = "ok"
    service: str = "backend"


@asynccontextmanager
async def _application_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """个人运行保留持久状态；启动不恢复旧考试任务或自动重发模型请求。"""

    runtime = getattr(app.state, "settings_runtime", None)
    app.state.recovery = "completed"
    first_error: BaseException | None = None
    try:
        yield
    except BaseException as error:  # noqa: BLE001 - preserve the application error while finishing all owners below.
        first_error = error
    finally:
        subjects = getattr(app.state, "subject_service", None)
        try:
            if subjects is not None:
                await subjects.wait_for_local_work()
        except BaseException as error:  # noqa: BLE001 - finish all owners before re-raising the original error.
            if first_error is None:
                first_error = error
        operations = getattr(app.state, "learning_operations", None)
        try:
            if operations is not None:
                await operations.aclose()
        except BaseException as error:  # noqa: BLE001 - finish all owners before re-raising the original error.
            if first_error is None:
                first_error = error
        try:
            if subjects is not None:
                subjects.close()
        except BaseException as error:  # noqa: BLE001 - finish all owners before re-raising the original error.
            if first_error is None:
                first_error = error
        try:
            if runtime is not None and callable(getattr(runtime, "aclose", None)):
                await runtime.aclose()
        except BaseException as error:  # noqa: BLE001 - finish all owners before re-raising the original error.
            if first_error is None:
                first_error = error
    if first_error is not None:
        raise first_error


def create_app(
    *,
    settings: AppSettings | None = None,
    setup_security: SetupSecurityService | None = None,
    bootstrap_context: BootstrapContext | None = None,
    settings_runtime: Any = None,
) -> FastAPI:
    """创建并配置完整业务应用（引导边界与未就绪保护见 `core.bootstrap`）。"""

    runtime_settings = settings or get_settings()
    if runtime_settings.embedding_dimension != EMBEDDING_DIMENSION_DEFAULT:
        raise ConfigurationError(
            f"EMBEDDING_DIMENSION 当前为 {runtime_settings.embedding_dimension}，"
            "与迁移 0004 的 vector(1024) 定义不一致。",
            fields=("EMBEDDING_DIMENSION",),
        )
    app = FastAPI(
        title="EduAgent",
        version="0.1.0",
        lifespan=_application_lifespan,
    )
    app.state.settings = runtime_settings
    context = (
        bootstrap_context
        if bootstrap_context is not None
        else resolve_bootstrap_context()
    )
    app.state.bootstrap = context
    app.state.business_assembly = "assembled"
    app.state.recovery = "not_started"
    app.state.settings_runtime = settings_runtime
    app.state.business_candidate = None
    app.state.business_failure = None
    # 装配事实只来自设置运行时；没有运行时（嵌入/测试）时保持未知，不伪造。
    facts = (
        settings_runtime.deployment_facts()
        if settings_runtime is not None
        and callable(getattr(settings_runtime, "deployment_facts", None))
        else {}
    )
    app.state.configuration_revision = facts.get("configuration_revision")
    app.state.pending_revision = facts.get("pending_revision")
    app.state.migration_head = facts.get("migration_head")
    app.state.credential_backend_available = facts.get("credential_backend_available")
    # T217/T218：设置授权面在业务路由之前安装，引导模式与完整模式共用同一实现。
    install_settings_surface(
        app,
        security=(
            setup_security
            if setup_security is not None
            else build_bootstrap_security(context)
        ),
        configuration_available=context.settings is not None,
        static_resources=context.static_resources,
        failure="最小引导配置不可读取。",
    )
    from backend.app.api.learning_operations import install_learning_operations

    install_learning_operations(app)
    from backend.app.api.subjects import install_subjects

    install_subjects(app)

    @app.exception_handler(AIConfigurationError)
    async def ai_configuration_handler(
        _request: Request, exc: AIConfigurationError
    ) -> JSONResponse:
        """AI 未配置是显式门禁，不是 500；消息由固定构造器生成。"""

        return JSONResponse(
            status_code=503,
            content={
                "code": exc.code,
                "message": str(exc),
                "stage": "ai_configuration",
                "retryable": False,
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        """把请求校验错误转换成统一的中文响应。"""

        details: list[dict[str, object]] = []
        for error in exc.errors():
            location = [str(item) for item in error.get("loc", ())]
            field_name = location[-1] if location else "请求参数"
            raw_message = str(error.get("msg", "输入无效。"))
            if field_name == "roles":
                message = "角色不能为空，且至少需要一个角色。"
            elif raw_message == "Field required":
                message = f"{field_name}不能为空。"
            elif "should be" in raw_message or "valid" in raw_message:
                message = f"{field_name}格式无效。"
            else:
                message = f"{field_name}输入无效。"
            details.append(
                {
                    "loc": location,
                    "msg": message,
                    "type": str(error.get("type", "value_error")),
                }
            )
        return JSONResponse(status_code=422, content={"detail": details})

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    async def health() -> HealthResponse:
        """返回一个与依赖无关的容器存活检查响应。"""

        return HealthResponse()

    @app.get("/ready", response_model=HealthResponse, tags=["system"])
    def ready() -> HealthResponse | JSONResponse:
        """分层就绪：配置/依赖/装配/恢复全部通过才 200；AI 状态不在此判断。"""

        try:
            checked_settings = settings or get_settings()
        except ConfigurationError:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "not_ready",
                    "service": "backend",
                    "failed_check": "config",
                    "detail": "运行配置无效，请检查必填配置项。",
                },
            )

        inputs = readiness.inputs_for_app(app, business_settings=checked_settings)
        failure = readiness.gate_ready(readiness.evaluate_readiness(inputs))
        if failure is None:
            return HealthResponse()
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

    mount_static_bundle(app, context.static_root)
    return app


__all__ = ["HealthResponse", "create_app"]
