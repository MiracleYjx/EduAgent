"""Personal operation scope, durable send accounting and snapshot-owned clients.

Business producers supply a finite plan and an invocation callback. Only guarded
HTTP clients can be acquired from this service. No business workflow is added.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.core.retry_policy import RetryPolicy
from backend.app.models.operation_run import OperationRun
from backend.app.schemas.personal_learning import LearningOperationView
from backend.app.schemas.settings import CredentialReference
from backend.app.services.settings.runtime_snapshot import RuntimeSnapshotService

MiB = 1024 * 1024


class OperationError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class LearningLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    upload_bytes: int = Field(default=20 * MiB, gt=0, le=20 * MiB)
    docx_expanded_bytes: int = Field(default=20 * MiB, gt=0, le=20 * MiB)
    decoded_rgba_bytes: int = Field(default=10 * MiB, gt=0, le=10 * MiB)
    pdf_batch_pages: int = Field(default=10, gt=0, le=10)
    docx_image_occurrences: int = Field(default=8, gt=0, le=8)
    ocr_concurrency: int = Field(default=1, ge=1, le=1)
    model_concurrency: int = Field(default=1, ge=1, le=1)
    auto_paid_retries: int = Field(default=0, ge=0, le=0)
    local_wait_seconds: float = Field(default=60, gt=0, le=60)
    http_timeout_seconds: float = Field(default=30, gt=0, le=30)
    text_request_bytes: int = Field(default=65536, gt=0, le=65536)
    response_bytes: int = Field(default=65536, gt=0, le=65536)

    def check(self, kind: str, observed: int) -> None:
        limit = getattr(self, kind)
        if observed < 0 or observed > limit:
            raise OperationError("LIMIT_EXCEEDED")


class StepPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    step_id: str = Field(min_length=1, max_length=80)
    task_kind: str = Field(min_length=1, max_length=80)
    messages: list[dict[str, Any]]
    request_budget: int = Field(gt=0, strict=True)
    input_token_limit: int | None = Field(default=None, gt=0, strict=True)
    output_token_limit: int | None = Field(default=None, gt=0, strict=True)
    estimated_cost: str | None = None
    embedding_inputs: list[str] | None = None
    scope_summary: str | None = None


def encoded(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


@dataclass
class Invocation:
    operation_id: UUID
    step: StepPlan
    runtime: Any
    profile: dict[str, Any] | None = None
    clients: list[Any] = field(default_factory=list)
    failure: str | None = None
    expected_messages: Any = None
    embedding_cursor: int = 0
    response_facts: dict[str, Any] = field(default_factory=dict)


class GuardedTransport(httpx.AsyncBaseTransport):
    def __init__(self, service: LearningOperations, invocation: Invocation):
        self.service = service
        self.invocation = invocation

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        service = self.service
        call = self.invocation
        try:
            body = json.loads(request.content)
            model = call.runtime.requested_model(call.step.task_kind)
            if body.get("model") != model:
                raise OperationError("MODEL_CHANGED")
            service.limits.check(
                "text_request_bytes",
                len(encoded(body.get("messages", body.get("input", body)))),
            )
            if (
                call.step.output_token_limit is not None
                and body.get("max_tokens") != call.step.output_token_limit
            ):
                raise OperationError("MODEL_BUDGET_CHANGED")
            profile = call.profile or {}
            base = httpx.URL(profile["base_url"])
            if (
                request.url.scheme != base.scheme
                or request.url.host != base.host
                or request.url.port != base.port
                or not request.url.path.startswith(base.path.rstrip("/") + "/")
            ):
                raise OperationError("ENDPOINT_CHANGED")
            if "messages" in body and body["messages"] != call.expected_messages:
                raise OperationError("INPUT_SCOPE_CHANGED")
            if "input" in body:
                batch = (
                    body["input"]
                    if isinstance(body["input"], list)
                    else [body["input"]]
                )
                expected = call.step.embedding_inputs
                if (
                    expected is None
                    or batch
                    != expected[
                        call.embedding_cursor : call.embedding_cursor + len(batch)
                    ]
                ):
                    raise OperationError("INPUT_SCOPE_CHANGED")
                call.embedding_cursor += len(batch)
            if not isinstance(service.transport, httpx.MockTransport):
                service._check_paid(service.view(call.operation_id))
                if (
                    service.token_counter is None
                    or call.step.input_token_limit is None
                    or (count := service.token_counter(model, body)) < 0
                    or isinstance(count, bool)
                    or count > call.step.input_token_limit
                ):
                    raise OperationError("MODEL_BUDGET_REQUIRED")
            service._reserve(call.operation_id, call.step.step_id)
        except OperationError as error:
            call.failure = error.code
            raise
        inner = service.transport
        owned = inner is None
        if inner is None:
            inner = httpx.AsyncHTTPTransport(retries=0, trust_env=False)
        try:
            async with asyncio.timeout(service.limits.http_timeout_seconds):
                response = await inner.handle_async_request(request)
                content = bytearray()
                try:
                    async for chunk in response.aiter_bytes():
                        if len(content) + len(chunk) > service.limits.response_bytes:
                            raise OperationError("LIMIT_EXCEEDED")
                        content.extend(chunk)
                finally:
                    await response.aclose()
            try:
                received = json.loads(content)
                usage = received.get("usage") or {}
                call.response_facts = {
                    "returned_model": received.get("model")
                    if isinstance(received.get("model"), str)
                    else None,
                    "usage": {
                        k: v
                        for k, v in usage.items()
                        if k in {"prompt_tokens", "completion_tokens", "total_tokens"}
                        and isinstance(v, int)
                        and not isinstance(v, bool)
                        and v >= 0
                    },
                }
            except (ValueError, AttributeError):
                pass
            headers = {
                k: v
                for k, v in response.headers.items()
                if k.lower() not in {"content-length", "content-encoding"}
            }
            return httpx.Response(
                response.status_code,
                headers=headers,
                content=bytes(content),
                request=request,
            )
        except (
            TimeoutError,
            httpx.TimeoutException,
            httpx.NetworkError,
            asyncio.CancelledError,
        ):
            call.failure = "OUTCOME_UNKNOWN"
            raise
        except OperationError as error:
            call.failure = error.code
            raise
        finally:
            service._release_slot(call.operation_id)
            if owned:
                await inner.aclose()

    async def aclose(self) -> None:
        # The service owns the injected transport, never a single provider lease.
        pass


class LearningOperations:
    def __init__(
        self,
        *,
        sessions: sessionmaker[Session],
        config: Any,
        transport: httpx.AsyncBaseTransport | None = None,
        limits: LearningLimits | None = None,
        paid_authorization: Callable[[LearningOperationView], bool] | None = None,
        token_counter: Callable[[str, Any], int] | None = None,
    ):
        self.owned_engine: Engine | None = None
        self.sessions = sessions
        self.config = config
        self.transport = transport
        self.limits = limits or LearningLimits()
        self.paid_authorization = paid_authorization
        self.token_counter = token_counter
        self.planners: dict[str, Callable[[dict[str, Any]], list[StepPlan]]] = {}
        self._current: ContextVar[Invocation] = ContextVar("learning_invocation")
        self.snapshots = RuntimeSnapshotService(
            config, client_factory=self._client_factory
        )

    def _row(
        self, session: Session, operation_id: UUID, *, lock: bool = False
    ) -> OperationRun:
        query = select(OperationRun).where(
            OperationRun.id == operation_id,
            OperationRun.environment_id == self.config.environment_id,
        )
        if lock:
            query = query.with_for_update()
        row = session.scalar(query)
        if row is None:
            raise OperationError("OPERATION_NOT_FOUND")
        return row

    def view(self, operation_id: UUID) -> LearningOperationView:
        with self.sessions() as session:
            return LearningOperationView.model_validate(
                self._row(session, operation_id)
            )

    def prepare(
        self,
        *,
        purpose: str,
        idempotency_key: str,
        steps: list[StepPlan],
        retry_of_id: UUID | None = None,
    ) -> LearningOperationView:
        if (
            not steps
            or len({s.step_id for s in steps}) != len(steps)
            or not 0 < len(idempotency_key) <= 160
            or not 0 < len(purpose) <= 80
        ):
            raise OperationError("INVALID_OPERATION_PLAN")
        for step in steps:
            if step.embedding_inputs is not None:
                self.limits.check(
                    "text_request_bytes", len(encoded(step.embedding_inputs))
                )
            self.limits.check("text_request_bytes", len(encoded(step.messages)))
        plan_digest = digest(
            {"purpose": purpose, "steps": [s.model_dump(mode="json") for s in steps]}
        )
        with self.sessions() as session:
            existing = session.scalar(
                select(OperationRun).where(
                    OperationRun.environment_id == self.config.environment_id,
                    OperationRun.idempotency_key == idempotency_key,
                )
            )
            if existing:
                if existing.plan_digest != plan_digest:
                    raise OperationError("IDEMPOTENCY_CONFLICT")
                return LearningOperationView.model_validate(existing)
        identifier = uuid4()
        snapshot = self.snapshots.create(
            workflow_id=str(identifier),
            request_id=idempotency_key,
            task_kinds=tuple(dict.fromkeys(s.task_kind for s in steps)),
        )
        saved = [
            {
                **s.model_dump(mode="json"),
                "model": snapshot.requested_model[s.task_kind],
                "state": "pending",
                "actual_requests": 0,
                "result": None,
                "error_code": None,
            }
            for s in steps
        ]
        preview_digest = digest(
            {"purpose": purpose, "snapshot": str(snapshot.snapshot_id), "steps": saved}
        )
        with self.sessions() as session:
            row = OperationRun(
                id=identifier,
                environment_id=self.config.environment_id,
                idempotency_key=idempotency_key,
                purpose=purpose,
                plan_digest=plan_digest,
                preview_digest=preview_digest,
                runtime_snapshot_id=snapshot.snapshot_id,
                state="prepared",
                request_budget=sum(s.request_budget for s in steps),
                actual_requests=0,
                steps=saved,
                retry_of_id=retry_of_id,
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = session.scalar(
                    select(OperationRun).where(
                        OperationRun.environment_id == self.config.environment_id,
                        OperationRun.idempotency_key == idempotency_key,
                    )
                )
                if existing is None or existing.plan_digest != plan_digest:
                    raise OperationError("IDEMPOTENCY_CONFLICT") from None
                return LearningOperationView.model_validate(existing)
            return LearningOperationView.model_validate(row)

    def preview(
        self, *, purpose: str, scope: dict[str, Any], idempotency_key: str
    ) -> LearningOperationView:
        planner = self.planners.get(purpose)
        if planner is None:
            raise OperationError("OPERATION_NOT_AVAILABLE")
        return self.prepare(
            purpose=purpose, idempotency_key=idempotency_key, steps=planner(scope)
        )

    def confirm(
        self, operation_id: UUID, *, preview_digest: str
    ) -> LearningOperationView:
        with self.sessions.begin() as session:
            row = self._row(session, operation_id, lock=True)
            if row.preview_digest != preview_digest:
                raise OperationError("PREVIEW_CHANGED")
            if row.state == "prepared":
                row.state = "confirmed"
        return self.view(operation_id)

    def _reserve(self, operation_id: UUID, step_id: str) -> None:
        try:
            with self.sessions.begin() as session:
                row = self._row(session, operation_id, lock=True)
                values = copy.deepcopy(row.steps)
                step = next(s for s in values if s["step_id"] == step_id)
                if (
                    row.state != "running"
                    or row.actual_requests >= row.request_budget
                    or step["actual_requests"] >= step["request_budget"]
                ):
                    raise OperationError("REQUEST_BUDGET_EXCEEDED")
                row.active_slot = "model"
                row.actual_requests += 1
                step["actual_requests"] += 1
                step["state"] = "outcome_unknown"
                row.steps = values
        except IntegrityError:
            raise OperationError("MODEL_BUSY") from None

    def _release_slot(self, operation_id: UUID) -> None:
        with self.sessions.begin() as session:
            row = self._row(session, operation_id, lock=True)
            row.active_slot = None

    def _client_factory(self, task_kind: str, profile: Mapping[str, Any]) -> Any:
        call = self._current.get()
        if task_kind != call.step.task_kind:
            raise OperationError("UNPLANNED_TASK")
        call.profile = dict(profile)
        if profile["adapter_id"] == "deepseek":
            from backend.app.ai.llm.deepseek import DeepSeekProvider

            call.expected_messages = DeepSeekProvider._prepare_messages(
                call.step.messages
            )
        else:
            from backend.app.ai.llm.openai_compatible import OpenAICompatibleProvider

            call.expected_messages = OpenAICompatibleProvider._prepare_messages(
                call.step.messages
            )
        http = httpx.AsyncClient(
            transport=GuardedTransport(self, call),
            timeout=self.limits.http_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )
        call.clients.append(http)
        key = "unused-no-auth"
        if profile.get("auth_mode") == "api_key":
            ref = CredentialReference.model_validate(profile["credential_ref"])
            key = self.config.credential_store.get(ref, ref.version).get_secret_value()
        client = AsyncOpenAI(
            api_key=key,
            base_url=profile["base_url"],
            http_client=http,
            timeout=self.limits.http_timeout_seconds,
            max_retries=0,
            admin_api_key="",
            organization="",
            project="",
            webhook_secret="",
        )
        client._custom_headers = {}
        call.clients.append(client)
        selected = copy.deepcopy(dict(profile))
        if call.step.output_token_limit is not None:
            selected["parameters"] = {
                **selected.get("parameters", {}),
                "max_tokens": call.step.output_token_limit,
            }
        options = {"client": client, "timeout": self.limits.http_timeout_seconds}
        if task_kind == "embedding":
            options["max_retries"] = 0
        else:
            options["retry_policy"] = RetryPolicy(max_retries=0)
        return self.config.provider_registry.create(
            selected, credential_store=self.config.credential_store, **options
        )

    def _check_paid(self, view: LearningOperationView) -> None:
        if isinstance(self.transport, httpx.MockTransport):
            return
        if self.paid_authorization is None or not self.paid_authorization(view):
            raise OperationError("PAID_SEND_NOT_AUTHORIZED")
        if self.token_counter is None or any(
            s.get("input_token_limit") is None
            or s.get("output_token_limit") is None
            or s.get("estimated_cost") is None
            for s in view.steps
        ):
            raise OperationError("MODEL_BUDGET_REQUIRED")

    async def execute(
        self,
        operation_id: UUID,
        *,
        invoke: Callable[[Any, StepPlan], Awaitable[BaseModel]],
    ) -> LearningOperationView:
        current = self.view(operation_id)
        if current.state == "prepared":
            raise OperationError("CONFIRMATION_REQUIRED")
        if current.state != "confirmed":
            return current
        self._check_paid(current)
        snapshot = self.snapshots.load(current.runtime_snapshot_id)
        # Missing snapshot/dependencies never resolve against the current active revision.
        with self.sessions.begin() as session:
            claimed = session.execute(
                update(OperationRun)
                .where(
                    OperationRun.id == operation_id,
                    OperationRun.environment_id == self.config.environment_id,
                    OperationRun.state == "confirmed",
                )
                .values(state="running")
                .returning(OperationRun.id)
            ).scalar_one_or_none()
        if not claimed:
            return self.view(operation_id)
        runtime = self.snapshots.context(snapshot)
        final = "succeeded"
        for saved in current.steps:
            step = StepPlan.model_validate(
                {k: v for k, v in saved.items() if k in StepPlan.model_fields}
            )
            call = Invocation(operation_id, step, runtime)
            token = self._current.set(call)
            result = None
            failure = None
            state = "succeeded"
            try:
                value = await invoke(runtime, step)
                if not isinstance(value, BaseModel):
                    raise OperationError("SCHEMA_FAILED")
                result = value.model_dump(mode="json")
                self.limits.check("response_bytes", len(encoded(result)))
                observed = next(
                    s
                    for s in self.view(operation_id).steps
                    if s["step_id"] == step.step_id
                )
                if observed["actual_requests"] == 0:
                    raise OperationError("NO_REQUEST_SENT")
            except asyncio.CancelledError:
                state = "outcome_unknown"
                failure = "OUTCOME_UNKNOWN"
            except Exception:  # noqa: BLE001 - persist failure and release leases without exposing provider text.
                failure = call.failure or "PROVIDER_FAILED"
                state = "outcome_unknown" if failure == "OUTCOME_UNKNOWN" else "failed"
            finally:
                try:
                    await runtime.aclose()
                finally:
                    for client in reversed(call.clients):
                        close = getattr(client, "aclose", None) or getattr(
                            client, "close", None
                        )
                        if callable(close):
                            pending = close()
                            if inspect.isawaitable(pending):
                                await pending
                    self._current.reset(token)
            with self.sessions.begin() as session:
                row = self._row(session, operation_id, lock=True)
                values = copy.deepcopy(row.steps)
                target = next(s for s in values if s["step_id"] == step.step_id)
                target.update(
                    state=state,
                    result=result if state == "succeeded" else None,
                    error_code=failure,
                    **call.response_facts,
                )
                row.steps = values
            if state != "succeeded":
                final = state
                break
        with self.sessions.begin() as session:
            row = self._row(session, operation_id, lock=True)
            row.state = final
        return self.view(operation_id)

    def retry(
        self,
        operation_id: UUID,
        *,
        idempotency_key: str,
        acknowledge_unknown: bool = False,
    ) -> LearningOperationView:
        old = self.view(operation_id)
        if old.state not in {"failed", "partial", "outcome_unknown"}:
            raise OperationError("RETRY_NOT_AVAILABLE")
        if old.state == "outcome_unknown" and not acknowledge_unknown:
            raise OperationError("UNKNOWN_OUTCOME_ACK_REQUIRED")
        remaining = [
            StepPlan.model_validate(
                {k: v for k, v in s.items() if k in StepPlan.model_fields}
            )
            for s in old.steps
            if s["state"] != "succeeded"
        ]
        return self.prepare(
            purpose=old.purpose,
            idempotency_key=idempotency_key,
            steps=remaining,
            retry_of_id=old.id,
        )

    async def aclose(self) -> None:
        await self.snapshots.aclose()
        if self.transport is not None:
            await self.transport.aclose()
        if self.owned_engine is not None:
            self.owned_engine.dispose()
