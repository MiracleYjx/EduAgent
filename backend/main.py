"""Process entry point for the EduAgent backend."""

from __future__ import annotations

import uvicorn

from backend.app.core.app import create_app

app = create_app()


def main() -> None:
    """Run the backend when invoked as a Python module."""

    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=8000,
    )


if __name__ == "__main__":
    main()
