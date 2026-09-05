"""EduAgent 后端进程入口点。"""

from __future__ import annotations

import uvicorn

from backend.app.core.app import create_app

app = create_app()


def main() -> None:
    """当作为 Python 模块调用时运行后端。"""

    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=8000,
    )


if __name__ == "__main__":
    main()
