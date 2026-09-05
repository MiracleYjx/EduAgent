"""FastAPI 应用工厂与共享 HTTP 入口点。"""

from __future__ import annotations

import gradio as gr
from fastapi import FastAPI
from pydantic import BaseModel

from backend.app.core.config import AppSettings, get_settings


class HealthResponse(BaseModel):
    """容器健康检查端点返回的稳定响应结构."""

    status: str = "ok"
    service: str = "backend"


def create_gradio_app() -> gr.Blocks:
    """构建由后端挂载的 Gradio 界面占位。"""

    return gr.Blocks(title="EduAgent")


def create_app(
    *,
    settings: AppSettings | None = None,
    gradio_app: gr.Blocks | None = None,
) -> FastAPI:
    """创建并配置 FastAPI 应用。"""

    runtime_settings = settings or get_settings()
    app = FastAPI(title="EduAgent", version="0.1.0")
    app.state.settings = runtime_settings

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
