"""Component harness for retained shared code, never a production entry.

Only existing library/component assertions use these explicitly selected routers.
Production route retirement is verified separately against the real factory.
No Gradio, virtual login, automatic recovery or real Provider is installed here.
"""

from importlib import import_module

from backend.app.core.app import create_app
from backend.app.core.config import AppSettings
from backend.app.core.security import AuthenticationMiddleware


def component_app(*, settings: AppSettings, routers: tuple[str, ...]):
    app = create_app(settings=settings)
    app.add_middleware(AuthenticationMiddleware)
    for name in routers:
        app.include_router(import_module("backend.app.api." + name).router)
    return app
