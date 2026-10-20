"""Explicit anonymous-pipe handoff for a caller-owned browser controller.

The receiver consumes one UTF-8 JSON object per line. It must open the URL in
its own browser in memory; Chromium itself does not implement this protocol.
Only an inherited pipe handle is passed on argv, never an authorization code.
"""
from __future__ import annotations

import json
import os
from typing import TextIO


class BrowserPipe:
    def __init__(self, handle: int):
        if os.name != "nt":
            raise ValueError("BROWSER_PIPE_REQUIRES_WINDOWS")
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetFileType.argtypes = [wintypes.HANDLE]
        kernel.GetFileType.restype = wintypes.DWORD
        if handle <= 0 or kernel.GetFileType(handle) != 3:  # FILE_TYPE_PIPE
            raise ValueError("BROWSER_HANDLE_IS_NOT_PIPE")
        os.set_handle_inheritable(handle, False)
        fd = msvcrt.open_osfhandle(handle, os.O_WRONLY)
        self.stream: TextIO = os.fdopen(fd, "w", encoding="utf-8", newline="\n")

    def __call__(self, url: str) -> bool:
        frame = json.dumps({"type": "open_settings", "authorization_url": url})
        if len(frame.encode("utf-8")) > 8192:
            raise ValueError("BROWSER_FRAME_TOO_LARGE")
        self.stream.write(frame + "\n")
        self.stream.flush()
        return True

    def close(self) -> None:
        self.stream.close()
