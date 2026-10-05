"""Source and PyInstaller entry point for Windows single-host deployment."""

import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.deployment.windows_launcher import main

if __name__ == "__main__":
    # Frozen Python ignores PYTHONIOENCODING; keep redirected Chinese logs readable.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    code = main()
    if (
        code
        and getattr(sys, "frozen", False)
        and "--child" not in sys.argv
        and sys.stdin.isatty()
    ):
        try:
            input("启动失败，请按上面的步骤修正。按回车退出…")
        except EOFError:
            pass
    raise SystemExit(code)
