"""FastAPI 应用工厂与共享 HTTP 入口点。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import gradio as gr
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend.app.ai.errors import AIConfigurationError
from backend.app.api.admin import router as admin_router
from backend.app.api.auth import router as auth_router
from backend.app.api.content_validation import router as content_validation_router
from backend.app.api.courses import router as courses_router
from backend.app.api.exams import router as exams_router
from backend.app.api.file_storage import router as file_storage_router
from backend.app.api.grading import (
    recover_interrupted_grading_tasks,
)
from backend.app.api.grading import (
    router as grading_router,
)
from backend.app.api.image_understanding import router as image_understanding_router
from backend.app.api.knowledge_bases import router as knowledge_bases_router
from backend.app.api.paper_import import recover_interrupted_paper_imports
from backend.app.api.paper_import import router as paper_import_router
from backend.app.api.question_assets import router as question_assets_router
from backend.app.api.question_generation import router as question_generation_router
from backend.app.api.questions import router as questions_router
from backend.app.api.results import router as results_router
from backend.app.api.reviews import router as reviews_router
from backend.app.api.submissions import exam_submission_router
from backend.app.api.submissions import router as submissions_router
from backend.app.api.traces import router as traces_router
from backend.app.api.workflow import recover_diagnosis_pending_runs
from backend.app.api.workflow import router as workflow_router
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
from backend.app.core.security import AuthenticationMiddleware
from backend.app.core.settings_security import SetupSecurityService
from backend.app.services.settings import readiness
from backend.app.ui.gradio_app import create_gradio_app as build_gradio_app


class HealthResponse(BaseModel):
    """容器健康检查端点返回的稳定响应结构."""

    status: str = "ok"
    service: str = "backend"


def create_gradio_app() -> gr.Blocks:
    """构建由后端挂载的 Gradio 应用外壳。"""

    return build_gradio_app()


@asynccontextmanager
async def _application_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期：启动阶段收敛遗留阅卷任务，只执行一次。

    结果存储未迁移或不可连接时跳过，不阻断启动；本阶段把遗留的进行中 M3 任务标为中断失败，
    并把“整卷结果已提交、诊断未生成”的遗留 M4 运行改判为可恢复的诊断待生成态（P1.4），
    不实现自动恢复队列。健康轮询/诊断/单次请求都不会再次调用这些钩子。
    """

    runtime = getattr(app.state, "settings_runtime", None)
    try:
        recover_interrupted_grading_tasks()
        recover_diagnosis_pending_runs()
        recover_interrupted_paper_imports()
    except Exception:
        # 保留原启动失败语义；同时留下可诊断的恢复阶段事实。
        app.state.recovery = "failed"
        raise
    # 原恢复钩子启动阶段完成；只证明本进程完成该阶段，不代表历史 Workflow 全部成功。
    app.state.recovery = "completed"
    try:
        yield
    finally:
        subjects = getattr(app.state, "subject_service", None)
        if subjects is not None:
            subjects.close()
        personal = getattr(app.state, "subject_service", None)
        if personal is not None:
            await personal.wait_for_local_work()
        operations = getattr(app.state, "learning_operations", None)
        if operations is not None:
            await operations.aclose()
        if runtime is not None and callable(getattr(runtime, "aclose", None)):
            await runtime.aclose()


def create_app(
    *,
    settings: AppSettings | None = None,
    gradio_app: gr.Blocks | None = None,
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
    app.add_middleware(AuthenticationMiddleware)
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
    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(courses_router)
    app.include_router(knowledge_bases_router)
    app.include_router(file_storage_router)
    app.include_router(question_generation_router)
    app.include_router(questions_router)
    app.include_router(question_assets_router)
    app.include_router(content_validation_router)
    app.include_router(image_understanding_router)
    app.include_router(paper_import_router)
    app.include_router(exams_router)
    app.include_router(grading_router)
    app.include_router(results_router)
    app.include_router(reviews_router)
    app.include_router(submissions_router)
    app.include_router(exam_submission_router)
    app.include_router(workflow_router)
    app.include_router(traces_router)

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

    gr.mount_gradio_app(
        app,
        gradio_app or create_gradio_app(),
        path="/gradio",
    )
    mount_static_bundle(app, context.static_root)
    return app


__all__ = ["HealthResponse", "create_app", "create_gradio_app"]
