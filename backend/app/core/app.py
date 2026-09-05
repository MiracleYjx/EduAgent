"""FastAPI application factory and shared HTTP entry points."""

from __future__ import annotations

import gradio as gr
from fastapi import FastAPI
from pydantic import BaseModel

from backend.app.core.config import AppSettings, get_settings


class HealthResponse(BaseModel):
    """Stable response returned by the container healthcheck endpoint."""

    status: str = "ok"
    service: str = "backend"


def create_gradio_app() -> gr.Blocks:
    """Build the placeholder Gradio surface mounted by the backend."""

    return gr.Blocks(title="EduAgent")


def create_app(
    *,
    settings: AppSettings | None = None,
    gradio_app: gr.Blocks | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application."""

    runtime_settings = settings or get_settings()
    app = FastAPI(title="EduAgent", version="0.1.0")
    app.state.settings = runtime_settings

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    async def health() -> HealthResponse:
        """Return a dependency-neutral liveness response for Compose."""

        return HealthResponse()

    gr.mount_gradio_app(
        app,
        gradio_app or create_gradio_app(),
        path="/gradio",
    )
    return app


__all__ = ["HealthResponse", "create_app", "create_gradio_app"]
