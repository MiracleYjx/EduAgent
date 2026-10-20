"""Persist only HTTP timing and typed model/usage metadata, never SDK bodies."""
from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx


def _model(value: object) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", value):
        return None
    if value.lower().startswith("sk-") or any(word in value.lower() for word in ("secret", "password", "api_key", "bearer")):
        return None
    return value


class HTTPObservation:
    def __init__(self, directory: Path):
        self.directory = directory
        self.attempts: list[dict[str, Any]] = []

    def _save(self, attempt: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / f"http-{attempt['attempt_index']:03d}.json").write_text(
            json.dumps(attempt, ensure_ascii=False, indent=2), encoding="utf-8")

    async def request(self, request: httpx.Request) -> None:
        if request.method != "POST" or not request.url.path.endswith("/chat/completions"):
            raise ValueError("UNEXPECTED_BENCHMARK_ROUTE")
        body = json.loads(request.content)
        attempt = {
            "attempt_index": len(self.attempts) + 1,
            "started_at": datetime.now(UTC).isoformat(), "start_perf": time.perf_counter(),
            "route": "/chat/completions", "status": "request_started",
            "request": {"model": _model(body.get("model"))},
        }
        request.extensions["t160_attempt"] = attempt["attempt_index"]
        self.attempts.append(attempt)
        self._save(attempt)

    async def response(self, response: httpx.Response) -> None:
        attempt = self.attempts[response.request.extensions["t160_attempt"] - 1]
        await response.aread()
        attempt.update(completed_at=datetime.now(UTC).isoformat(),
            elapsed_ms=(time.perf_counter() - attempt["start_perf"]) * 1000,
            status="response_received", http_status=response.status_code)
        try:
            body = response.json()
            if response.status_code == 200:
                raw_usage = body.get("usage")
                raw_usage = raw_usage if isinstance(raw_usage, dict) else {}
                attempt.update(actual_response_model=_model(body.get("model")), usage={
                    key: value if type(value) is int and value >= 0 else None
                    for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                    for value in (raw_usage.get(key),)
                })
            else:
                attempt["error"] = {"code": f"HTTP_{response.status_code}"}
        except (ValueError, AttributeError):
            attempt["response_parse_status"] = "not_json"
        self._save(attempt)
