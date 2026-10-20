"""One acceptance grant, three durable reservations; never a billing guarantee.

The caller must supply a verified tokenizer for the selected model. Missing
tokenization fails closed. A received HTTP response needs explicit semantic
confirmation before the next phase. No keys, prompts or raw responses persist.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

PHASES = ("text", "json_object", "grading")
GRANT = "eduagent-t237-20261019-deepseek-flash-3"
MODEL = "deepseek-flash"
MAX_INPUT = 8192
MAX_OUTPUT = 2048


class BudgetError(RuntimeError):
    """Safe error codes only; never wrap request/response text."""


class BudgetLedger:
    def __init__(self, path: Path):
        self.path = path.resolve()

    @staticmethod
    def initialize(path: Path) -> None:
        # Deliberate one-time operation, never performed by opening a ledger.
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb"):
            pass
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE grant_identity (id TEXT NOT NULL)")
            db.execute("INSERT INTO grant_identity VALUES (?)", (GRANT,))
            db.execute("CREATE TABLE attempts (phase TEXT PRIMARY KEY, input_tokens INTEGER, "
                       "state TEXT NOT NULL, metadata TEXT NOT NULL)")
        # A partial initialization is not deleted/recreated on error.

    def _connect(self) -> sqlite3.Connection:
        try:
            db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=5)
            db.execute("PRAGMA synchronous=FULL")
            if db.execute("SELECT id FROM grant_identity").fetchall() != [(GRANT,)]:
                db.close()
                raise BudgetError("GRANT_IDENTITY_MISMATCH")
            return db
        except sqlite3.Error:
            raise BudgetError("BUDGET_LEDGER_UNAVAILABLE") from None

    def reserve(self, phase: str, input_tokens: int) -> None:
        if type(input_tokens) is not int or not 0 <= input_tokens <= MAX_INPUT:
            raise BudgetError("INPUT_TOKEN_LIMIT")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT phase,state FROM attempts ORDER BY rowid").fetchall()
            if len(rows) >= 3 or phase != PHASES[len(rows)]:
                raise BudgetError("REQUEST_OR_PHASE_LIMIT")
            if any(p != PHASES[i] or s != "passed" for i, (p, s) in enumerate(rows)):
                raise BudgetError("PREVIOUS_REQUEST_NOT_CONFIRMED")
            db.execute("INSERT INTO attempts VALUES (?,?,?,?)",
                       (phase, input_tokens, "reserved", "{}"))
            db.commit()  # Durable before the network; crash/timeout consumes the slot.
        except sqlite3.Error:
            raise BudgetError("BUDGET_LEDGER_UNAVAILABLE") from None
        finally:
            db.close()

    def _update(self, phase: str, state: str, metadata: dict[str, Any]) -> None:
        db = self._connect()
        try:
            with db:
                result = db.execute("UPDATE attempts SET state=?,metadata=? WHERE phase=? AND state='reserved'",
                                    (state, json.dumps(metadata), phase))
                if result.rowcount != 1:
                    raise BudgetError("RESERVATION_STATE_CONFLICT")
        finally:
            db.close()

    def received(self, phase: str, metadata: dict[str, Any]) -> None:
        self._update(phase, "received", metadata)

    def failed(self, phase: str, code: str) -> None:
        # Codes originate here, never from an external exception body.
        self._update(phase, "failed", {"error": code})

    def confirm(self, phase: str) -> None:
        db = self._connect()
        try:
            with db:
                result = db.execute("UPDATE attempts SET state='passed' WHERE phase=? AND state='received'", (phase,))
                if result.rowcount != 1:
                    raise BudgetError("RESPONSE_NOT_CONFIRMABLE")
        finally:
            db.close()

    def rows(self) -> list[dict[str, Any]]:
        db = self._connect()
        try:
            return [{"phase": p, "input_tokens": n, "state": s, "metadata": json.loads(m)}
                    for p, n, s, m in db.execute("SELECT * FROM attempts ORDER BY rowid")]
        finally:
            db.close()


class BudgetTransport(httpx.AsyncBaseTransport):
    """The sole outward transport for this acceptance run; owns its inner pool."""

    def __init__(self, ledger: BudgetLedger, phase: str, inner: httpx.AsyncBaseTransport,
                 *, count_input: Callable[[dict[str, Any]], int] | None = None):
        self.ledger = ledger
        self.phase = phase
        self.inner = inner
        self.count_input = count_input
        self.closed = False

    def _preflight(self, request: httpx.Request) -> tuple[int, float]:
        if self.closed:
            raise BudgetError("TRANSPORT_CLOSED")
        if request.method != "POST" or str(request.url) not in {
            "https://api.deepseek.com/chat/completions",
            "https://api.deepseek.com/v1/chat/completions",
        }:
            raise BudgetError("ENDPOINT_NOT_AUTHORIZED")
        try:
            body = json.loads(request.content)
        except (ValueError, UnicodeError):
            raise BudgetError("INVALID_REQUEST") from None
        allowed = {"model", "messages", "max_tokens", "response_format", "temperature", "stream"}
        if not isinstance(body, dict) or set(body) - allowed:
            raise BudgetError("REQUEST_PARAMETERS_NOT_AUTHORIZED")
        if body.get("model") != MODEL or type(body.get("max_tokens")) is not int or body["max_tokens"] != MAX_OUTPUT:
            raise BudgetError("MODEL_OR_OUTPUT_LIMIT")
        if body.get("stream", False) is not False or body.get("response_format") != {"type": "json_object"}:
            raise BudgetError("RESPONSE_FORMAT_NOT_AUTHORIZED")
        temperature = body.get("temperature", 1)
        if type(temperature) not in (int, float) or not math.isfinite(temperature) or not 0 <= temperature <= 2:
            raise BudgetError("TEMPERATURE_INVALID")
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages or any(
            not isinstance(m, dict) or set(m) != {"role", "content"}
            or m["role"] not in {"system", "user", "assistant"} or not isinstance(m["content"], str)
            for m in messages
        ):
            raise BudgetError("TEXT_INPUT_REQUIRED")
        limits = request.extensions.get("timeout")
        if not isinstance(limits, dict) or set(limits) != {"connect", "read", "write", "pool"} or any(
            type(v) not in (int, float) or not math.isfinite(v) or not 0 < v <= 30
            for v in limits.values()
        ):
            raise BudgetError("TIMEOUT_LIMIT")
        if self.count_input is None:
            raise BudgetError("TOKEN_COUNTER_REQUIRED")
        tokens = self.count_input(body)
        if type(tokens) is not int or not 0 <= tokens <= MAX_INPUT:
            raise BudgetError("INPUT_TOKEN_LIMIT")
        return tokens, min(limits.values())

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        tokens, deadline = self._preflight(request)
        self.ledger.reserve(self.phase, tokens)
        start = time.monotonic()
        response: httpx.Response | None = None
        try:
            async with asyncio.timeout(deadline):
                response = await self.inner.handle_async_request(request)
                await response.aread()
        except BaseException:
            self.ledger.failed(self.phase, "NETWORK_TIMEOUT_CANCEL_OR_UNKNOWN")
            if response is not None:
                await response.aclose()
            raise
        metadata: dict[str, Any] = {"requested_model": MODEL, "http_status": response.status_code,
                                    "elapsed_seconds": time.monotonic() - start}
        if response.status_code != 200:
            self.ledger.failed(self.phase, f"HTTP_{response.status_code}")
            return response
        try:
            payload = response.json()
            usage = payload["usage"]
            model = payload.get("model")
            if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", model) or model.lower().startswith("sk-"):
                model = None
            counts = {k: usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
            if any(type(v) is not int or v < 0 for v in counts.values()):
                raise ValueError
            if counts["prompt_tokens"] > MAX_INPUT or counts["completion_tokens"] > MAX_OUTPUT:
                self.ledger.failed(self.phase, "PROVIDER_REPORTED_LIMIT_EXCEEDED")
                raise BudgetError("PROVIDER_REPORTED_LIMIT_EXCEEDED")
            metadata.update(returned_model=model, usage=counts)
        except (KeyError, TypeError, ValueError):
            self.ledger.failed(self.phase, "RESPONSE_METADATA_INVALID")
            raise BudgetError("RESPONSE_METADATA_INVALID") from None
        self.ledger.received(self.phase, metadata)
        return response

    async def aclose(self) -> None:
        if not self.closed:
            self.closed = True
            await self.inner.aclose()
