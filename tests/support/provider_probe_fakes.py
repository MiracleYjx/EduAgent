"""TCR-S2-PROVIDER-PROBE-009: observable inputs, never Registry/Probe policy.

The thin ports below are internal test seams, not a second public API. A future
implementation may rename these ports without weakening the behavioral tests.
Missing implementation fails at call time; genuine import errors remain visible.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import math
import os
import secrets
import socket
import subprocess
import sys
from collections.abc import Awaitable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import FrameType
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import BaseModel, SecretStr
from sqlalchemy.engine import Engine

from backend.app.ai.llm.base import BaseLLMProvider, LLMMessage
from backend.app.schemas.settings import (
    CredentialReference,
    DatabaseIdentity,
    EnvironmentName,
    TestResourceProfile,
)
from tests.support.credential_fakes import assert_no_secret, safe_surfaces

NOW = datetime(2026, 10, 16, tzinfo=UTC)
REGISTRY_MODULE = "backend.app.services.settings.provider_registry"
PROBE_MODULE = "backend.app.services.settings.probe_service"
_SOCKET_CONNECT = socket.socket.connect
_SOCKET_BIND = socket.socket.bind


def required_symbol(module_name: str, symbol: str) -> Any:
    if importlib.util.find_spec(module_name) is None:
        pytest.fail(f"T207_IMPLEMENTATION_REQUIRED: {module_name}.{symbol}", pytrace=False)
    module = importlib.import_module(module_name)
    if not hasattr(module, symbol):
        pytest.fail(f"T207_IMPLEMENTATION_REQUIRED: {module_name}.{symbol}", pytrace=False)
    return getattr(module, symbol)


def required_method(owner: Any, name: str) -> Any:
    method = getattr(owner, name, None)
    if not callable(method):
        pytest.fail(
            f"T207_IMPLEMENTATION_REQUIRED: {type(owner).__module__}.{type(owner).__name__}.{name}",
            pytrace=False,
        )
    return method


def view(value: Any) -> dict[str, Any]:
    """Serialization only: no classification or state decisions in this adapter."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")
    if isinstance(value, dict):
        return value
    raise TypeError("test port must return a mapping or Pydantic DTO")


def result_code(value: Any) -> str:
    data = view(value)
    for key in ("safe_error_code", "result_code", "code"):
        if key in data:
            return str(data[key])
    raise AssertionError("product result has no safe code")


def registry() -> Any:
    return required_symbol(REGISTRY_MODULE, "ProviderRegistry")()


def registry_entries(owner: Any, kind: str) -> Sequence[Any]:
    return required_method(owner, "entries")(kind=kind)


def validate_profile(owner: Any, profile: dict[str, Any]) -> Any:
    return required_method(owner, "validate_profile")(profile)


def resolve_binding(owner: Any, task: str, profiles: dict, bindings: dict) -> dict[str, Any]:
    return view(required_method(owner, "resolve_binding")(
        task_kind=task, profiles=profiles, bindings=bindings,
    ))


def make_profile(environment_id: UUID, **changes: Any) -> dict[str, Any]:
    reference = CredentialReference(
        credential_id=uuid4(), version=uuid4(), environment_id=environment_id,
        backend="dpapi", purpose="provider",
    )
    return {
        "profile_id": uuid4(), "profile_revision": uuid4(), "display_name": "T207 synthetic",
        "adapter_id": "openai_compatible", "model_id": "t207-model",
        "base_url": "https://provider.invalid/v1", "credential_ref": reference,
        "auth_mode": "api_key", "declared_capabilities": ("text", "json_object"),
        "parameters": {}, "enabled": True, **changes,
    }


def target_for(candidate_revision: UUID, profile: dict, capability: str) -> dict[str, Any]:
    reference = profile["credential_ref"]
    return {
        "candidate_revision": candidate_revision, "profile_id": profile["profile_id"],
        "profile_revision": profile["profile_revision"],
        "credential_version": reference.version if reference is not None else None,
        "model_id": profile["model_id"], "capability": capability,
    }


def successful_evidence(target: dict) -> dict[str, Any]:
    # Literal input facts. Whether these authorize anything is the product's job.
    return {
        **target, "probe_id": uuid4(), "status": "succeeded", "safe_error_code": "SUCCESS",
        "tested_at": NOW, "expires_at": NOW + timedelta(minutes=10),
        "external_requests": 1, "usage": None, "sample_id": "t207-synthetic",
    }


@dataclass
class FakeClock:
    now: datetime = NOW
    seconds: float = 0.0

    def __call__(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return self.seconds

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        self.seconds += seconds


@dataclass
class MemoryCredentials:
    values: dict[tuple[UUID, UUID], SecretStr] = field(default_factory=dict, repr=False)
    calls: list[tuple[UUID, UUID]] = field(default_factory=list)

    def get(self, reference: CredentialReference, version: UUID) -> SecretStr:
        self.calls.append((reference.credential_id, version))
        return self.values[(reference.credential_id, version)]


@dataclass
class HttpScenario:
    """A transport response, failure, or cancellable wait; no retry algorithm."""

    status: int = 200
    content: str = '{"ok": true}'
    json_body: dict | None = field(default=None, repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    error: Exception | None = field(default=None, repr=False)
    block: bool = False
    requests: list[httpx.Request] = field(default_factory=list, repr=False)
    started: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    release: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    finished: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    async def respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.started.set()
        try:
            if self.block:
                await self.release.wait()
            if self.error is not None:
                raise self.error
            body = self.json_body if self.json_body is not None else {
                "id": "t207-completion", "object": "chat.completion", "created": 1,
                "model": "t207-model", "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": self.content},
                     "finish_reason": "stop"},
                ],
            }
            return httpx.Response(self.status, json=body, headers=self.headers, request=request)
        finally:
            self.finished.set()


class ObservedAsyncClient(httpx.AsyncClient):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1
        await super().aclose()


@dataclass
class HttpClientFactory:
    scenario: HttpScenario
    clients: list[ObservedAsyncClient] = field(default_factory=list, repr=False)
    options: list[dict] = field(default_factory=list, repr=False)

    def __call__(self, **kwargs: Any) -> ObservedAsyncClient:
        # Reject a product attempt to bypass the supplied mock transport.
        assert "transport" not in kwargs
        self.options.append(dict(kwargs))
        client = ObservedAsyncClient(transport=httpx.MockTransport(self.scenario.respond), **kwargs)
        self.clients.append(client)
        return client


@dataclass
class ControlledDeadline:
    """Observe the requested timeout; optionally cancel one real awaitable.

    This substitutes only the waiting primitive, not Probe timeout decisions.
    It deliberately avoids spending 30 real seconds or claiming a performance run.
    """

    clock: FakeClock
    expire_calls: set[int] = field(default_factory=set)
    started: asyncio.Event | None = field(default=None, repr=False)
    timeouts: list[float] = field(default_factory=list)

    async def wait_for(self, awaitable: Awaitable[Any], timeout: float) -> Any:
        assert math.isfinite(timeout) and timeout > 0
        self.timeouts.append(timeout)
        if len(self.timeouts) in self.expire_calls:
            task = asyncio.ensure_future(awaitable)
            try:
                if self.started is not None:
                    # This scenario is an in-flight timeout, not a preflight race.
                    await asyncio.wait_for(self.started.wait(), 1)
                else:
                    await asyncio.sleep(0)
                self.clock.advance(timeout)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            raise TimeoutError("synthetic deadline")
        return await awaitable


@dataclass
class LocalResource:
    samples: list[Any] = field(default_factory=list, repr=False)
    close_calls: int = 0

    async def run(self, sample: Any, **_kwargs: Any) -> dict:
        self.samples.append(sample)
        return {"text": "T207 synthetic OCR"}

    async def aclose(self) -> None:
        self.close_calls += 1


@dataclass
class LocalLoader:
    calls: list[dict] = field(default_factory=list, repr=False)
    resource: LocalResource = field(default_factory=LocalResource)
    error: Exception | None = field(default=None, repr=False)

    def __call__(self, **kwargs: Any) -> LocalResource:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.resource


class MinimalAnswer(BaseModel):
    ok: bool


class RecordingLLM(BaseLLMProvider):
    """Implements only the original required contract, no capability policy."""

    provider_name = "t207-stub"

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.close_calls = 0

    async def generate_structured(
        self, messages: Sequence[LLMMessage], schema: type[BaseModel], model: str | None = None,
    ) -> BaseModel:
        self.calls.append({"messages": list(messages), "schema": schema, "model": model})
        return schema.model_validate({"ok": True})

    async def aclose(self) -> None:
        self.close_calls += 1


@dataclass
class ProbeSandbox:
    environment_id: UUID = field(default_factory=uuid4)
    candidate_revision: UUID = field(default_factory=uuid4)
    markers: dict[str, str] = field(default_factory=dict, repr=False)
    credentials: MemoryCredentials = field(default_factory=MemoryCredentials, repr=False)
    scenario: HttpScenario = field(default_factory=HttpScenario, repr=False)
    clock: FakeClock = field(default_factory=FakeClock)
    local: LocalLoader = field(default_factory=LocalLoader, repr=False)
    http: HttpClientFactory = field(init=False, repr=False)
    deadline: ControlledDeadline = field(init=False, repr=False)
    profile: dict = field(init=False, repr=False)
    calls: dict[str, int] = field(default_factory=lambda: {"network": 0, "database": 0, "process": 0})

    def __post_init__(self) -> None:
        self.http = HttpClientFactory(self.scenario)
        self.deadline = ControlledDeadline(self.clock, started=self.scenario.started)
        self.profile = make_profile(self.environment_id)
        reference = self.profile["credential_ref"]
        self.credentials.values[(reference.credential_id, reference.version)] = SecretStr(self.markers["key"])


def make_probe(sandbox: ProbeSandbox, *, environment_id: UUID | None = None) -> Any:
    cls = required_symbol(PROBE_MODULE, "ProbeService")
    return cls(
        environment_id=environment_id or sandbox.environment_id, registry=registry(),
        credential_store=sandbox.credentials, http_client_factory=sandbox.http,
        clock=sandbox.clock, wait_for=sandbox.deadline.wait_for,
        local_resource_loader=sandbox.local,
    )


async def run_probe(service: Any, sandbox: ProbeSandbox, **changes: Any) -> Any:
    arguments = {
        "candidate_revision": sandbox.candidate_revision, "profile": sandbox.profile,
        "capability": "json_object", "allow_billable": True, **changes,
    }
    return await required_method(service, "run")(**arguments)


def assess_evidence(service: Any, target: dict, probe: dict, accepted: dict | None = None) -> dict:
    return view(required_method(service, "assess_evidence")(
        target=target, probe=probe, activation_validation=accepted,
    ))


def classify_error(error: BaseException, *, correlation_id: str = "t207-correlation") -> Any:
    return required_symbol(PROBE_MODULE, "classify_probe_error")(
        error, correlation_id=correlation_id,
    )


def _internal_selfpipe() -> bool:
    frame: FrameType | None = sys._getframe(1)
    while frame is not None:
        filename = frame.f_code.co_filename.replace("\\", "/")
        if frame.f_code.co_name == "_make_self_pipe" and filename.endswith(
            ("/asyncio/proactor_events.py", "/asyncio/selector_events.py"),
        ):
            return True
        frame = frame.f_back
    return False


@pytest.fixture
def probe_sandbox(tmp_path, monkeypatch, caplog, capsys, request) -> Iterator[ProbeSandbox]:
    # Names only: do not read/copy inherited credential values or business data.
    for name in tuple(os.environ):
        upper = name.upper()
        if upper.startswith(("EDUAGENT_", "LANGCHAIN_", "LANGSMITH_")) or upper.endswith("_API_KEY") or upper in {
            "DATABASE_URL", "REDIS_URL", "JWT_SECRET_KEY", "DEV_MODE", "STORAGE_ROOT", "PYTEST_ADDOPTS",
        }:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EDUAGENT_ENVIRONMENT", "test")
    monkeypatch.setenv("EDUAGENT_DATA_ROOT", str(tmp_path / "runtime"))
    for name in ("HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "TEMP", "TMP", "TMPDIR"):
        monkeypatch.setenv(name, str(tmp_path))
    monkeypatch.chdir(tmp_path)
    caplog.set_level(logging.DEBUG)
    sandbox = ProbeSandbox(markers={
        name: "t207_synthetic_" + secrets.token_hex(24) for name in ("key", "body", "business")
    })

    # Non-secret synthetic receipt only; it grants no service/cleanup authority.
    resource_profile = TestResourceProfile(
        environment=EnvironmentName.TEST, environment_id=sandbox.environment_id, root=tmp_path / "runtime",
        database_identity=DatabaseIdentity(
            host="database.invalid", port=65432, database="eduagent_test",
            user="eduagent_test_runner", schema_name="public", instance_id="t207-synthetic-no-service",
        ),
    )
    receipt_path = tmp_path / "test-resource-profile.json"
    receipt_path.write_text(resource_profile.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("EDUAGENT_TEST_RESOURCE_PROFILE", str(receipt_path))

    def blocked(kind: str):
        def reject(*_args: Any, **_kwargs: Any) -> Any:
            sandbox.calls[kind] += 1
            pytest.fail("T207 forbids real " + kind + " access", pytrace=False)
        return reject

    def guarded_connect(sock: socket.socket, address: Any) -> Any:
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"} and _internal_selfpipe():
            return _SOCKET_CONNECT(sock, address)
        return blocked("network")()

    def guarded_bind(sock: socket.socket, address: Any) -> Any:
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"} and _internal_selfpipe():
            return _SOCKET_BIND(sock, address)
        return blocked("network")()

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "bind", guarded_bind)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked("network"))
    monkeypatch.setattr(socket, "getaddrinfo", blocked("network"))
    monkeypatch.setattr(socket.socket, "sendto", blocked("network"))
    monkeypatch.setattr(Engine, "connect", blocked("database"))
    monkeypatch.setattr(subprocess, "Popen", blocked("process"))
    try:
        yield sandbox
    finally:
        out, err = capsys.readouterr()
        assert_no_secret(sandbox.markers, {"logs": caplog.text, "stdout": out, "stderr": err})
        request.node.user_properties.append(("t207_sandbox", {
            **sandbox.calls, "external_transports": len(sandbox.scenario.requests),
            "clients": len(sandbox.http.clients),
            "clients_closed": sum(client.is_closed for client in sandbox.http.clients),
            "synthetic_marker_matches": 0,
        }))
        assert sandbox.calls == {"network": 0, "database": 0, "process": 0}


def assert_safe_result(sandbox: ProbeSandbox, result: Any) -> None:
    assert_no_secret(sandbox.markers, safe_surfaces(result))
    data = view(result)
    assert not {"raw_body", "response_body", "authorization", "secret_value", "api_key"} & data.keys()


def assert_one_closed_request(sandbox: ProbeSandbox) -> None:
    assert len(sandbox.scenario.requests) == 1
    assert len(sandbox.http.clients) == 1
    client = sandbox.http.clients[0]
    assert client.is_closed
    assert client.close_calls >= 1
