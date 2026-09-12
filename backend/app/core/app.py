"""FastAPI 应用工厂与共享 HTTP 入口点。"""

from __future__ import annotations

import gradio as gr
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend.app.api.admin import router as admin_router
from backend.app.api.auth import router as auth_router
from backend.app.api.courses import router as courses_router
from backend.app.api.exams import router as exams_router
from backend.app.api.knowledge_bases import router as knowledge_bases_router
from backend.app.api.questions import router as questions_router
from backend.app.api.submissions import exam_submission_router
from backend.app.api.submissions import router as submissions_router
from backend.app.core.config import AppSettings, ConfigurationError, get_settings
from backend.app.core.database import (
    DatabaseNotReadyError,
    check_postgres_ready,
    create_database_engine,
)
from backend.app.core.redis import (
    RedisNotReadyError,
    check_redis_ready,
    create_redis_client,
)
from backend.app.core.security import AuthenticationMiddleware
from backend.app.ui.gradio_app import create_gradio_app as build_gradio_app


class HealthResponse(BaseModel):
    """容器健康检查端点返回的稳定响应结构."""

    status: str = "ok"
    service: str = "backend"


def create_gradio_app() -> gr.Blocks:
    """构建由后端挂载的 Gradio 应用外壳。"""

    return build_gradio_app()


def create_app(
    *,
    settings: AppSettings | None = None,
    gradio_app: gr.Blocks | None = None,
) -> FastAPI:
    """创建并配置 FastAPI 应用。"""

    runtime_settings = settings or get_settings()
    app = FastAPI(title="EduAgent", version="0.1.0")
    app.state.settings = runtime_settings
    app.add_middleware(AuthenticationMiddleware)
    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(courses_router)
    app.include_router(knowledge_bases_router)
    app.include_router(questions_router)
    app.include_router(exams_router)
    app.include_router(submissions_router)
    app.include_router(exam_submission_router)

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
        """依次检查配置、PostgreSQL 和 Redis，返回不含凭据的失败原因。"""

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

        engine = create_database_engine(checked_settings, connect_timeout=2)
        try:
            check_postgres_ready(engine)
        except DatabaseNotReadyError:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "not_ready",
                    "service": "backend",
                    "failed_check": "postgres",
                    "detail": "PostgreSQL 未就绪，请检查数据库服务和连接配置。",
                },
            )
        finally:
            engine.dispose()

        client = create_redis_client(
            checked_settings, socket_connect_timeout=1, socket_timeout=1
        )
        try:
            check_redis_ready(client)
        except RedisNotReadyError:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "not_ready",
                    "service": "backend",
                    "failed_check": "redis",
                    "detail": "Redis 未就绪，请检查 Redis 服务和连接配置。",
                },
            )
        finally:
            client.close()
        return HealthResponse()

    gr.mount_gradio_app(
        app,
        gradio_app or create_gradio_app(),
        path="/gradio",
    )
    return app


__all__ = ["HealthResponse", "create_app", "create_gradio_app"]
