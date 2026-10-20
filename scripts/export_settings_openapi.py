"""Export settings OpenAPI without lifespan, dotenv, business services or secrets."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from uuid import UUID


def main() -> None:
    # Drop inherited provider/connection settings by name before importing the app.
    for name in tuple(os.environ):
        key = name.upper()
        if key.startswith(("EDUAGENT_", "DEEPSEEK_", "EMBEDDING_", "LLM_", "RERANK_",
                           "OCR_", "VISION_", "JWT_", "DATABASE_", "REDIS_", "OPENAI_",
                           "LANGSMITH_", "LANGCHAIN_")) or key.endswith("_API_KEY") or key in {
            "STORAGE_ROOT", "DEV_MODE",
        }:
            del os.environ[name]
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from backend.app.core.bootstrap import BootstrapContext, create_bootstrap_app
    from backend.app.schemas.settings import (
        BootstrapSettings,
        EnvironmentName,
        RuntimeKind,
    )

    context = BootstrapContext(settings=BootstrapSettings(
        environment=EnvironmentName.TEST,
        environment_id=UUID("01234567-89ab-4cde-8fab-0123456789ab"),
        runtime_kind=RuntimeKind.PYTHON, config_root=root / ".cache" / "openapi-only",
    ), bind=None, static_root=None)
    from backend.app.api.learning_operations import install_learning_operations

    app = create_bootstrap_app(context=context)
    install_learning_operations(app)
    print(json.dumps(app.openapi(), ensure_ascii=False))


if __name__ == "__main__":
    main()
