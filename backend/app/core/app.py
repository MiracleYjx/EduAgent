"""FastAPI 应用工厂与共享 HTTP 入口点。"""

from __future__ import annotations

import gradio as gr
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend.app.api.admin import router as admin_router
from backend.app.api.auth import router as auth_router
from backend.app.core.config import AppSettings, get_settings
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

    gr.mount_gradio_app(
        app,
        gradio_app or create_gradio_app(),
        path="/gradio",
    )
    return app


__all__ = ["HealthResponse", "create_app", "create_gradio_app"]
