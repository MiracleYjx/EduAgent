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
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import ExitStack
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Annotated, Any
from uuid import UUID, uuid4

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.core.retry_policy import (
    ProviderCallError,
    ProviderExecutionError,
    RetryPolicy,
)
from backend.app.models.operation_run import OperationRun
from backend.app.schemas.personal_learning import LearningOperationView
from backend.app.schemas.settings import CredentialReference
from backend.app.services.settings.runtime_snapshot import RuntimeSnapshotService

MiB = 1024 * 1024

# 图片输入按固定预留计量：base64 字节不得折算成文本 token（T083 TCR）。
IMAGE_INPUT_TOKEN_RESERVATION = 1_500
_IMAGE_PART_TYPES = frozenset({"image_url", "image"})


def _text_scoped_body(body: Any) -> tuple[Any, int]:
    """剥离图片部件，返回（文本计数范围, 图片数量）；无图时原样返回。"""

    if not isinstance(body, dict):
        return body, 0
    messages = body.get("messages")
    if not isinstance(messages, list):
        return body, 0
    images = 0
    scoped: list[Any] = []
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            scoped.append(message)
            continue
        kept: list[Any] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in _IMAGE_PART_TYPES:
                images += 1
                continue
            kept.append(part)
        scoped.append({**message, "content": kept})
    if not images:
        return body, 0
    return {**body, "messages": scoped}, images


def budget_input_tokens(counter: Callable[[str, Any], Any], model: str, body: Any) -> Any:
    """输入预算 = 文本 token + 每图固定预留；不把图片字节当作文本计。"""

    scoped, images = _text_scoped_body(body)
    count = counter(model, scoped)
    if images and isinstance(count, int) and not isinstance(count, bool):
        return count + IMAGE_INPUT_TOKEN_RESERVATION * images
    return count
logger = logging.getLogger(__name__)


class OperationError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _failure_code(error: BaseException) -> str:
    """Preserve a provider's explicit failure code instead of collapsing it."""

    if isinstance(error, (OperationError, ProviderCallError, ProviderExecutionError)):
        return error.code
    return "PROVIDER_FAILED"


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


class ModelBudget(BaseModel):
    """Approved per-request bounds for one actual provider capability."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    input_token_limit: int = Field(gt=0, strict=True)
    output_token_limit: int | None = Field(default=None, gt=0, le=32768, strict=True)
    embedding_dimension: int | None = Field(default=None, gt=0, le=65536, strict=True)
    response_bytes: int = Field(default=65536, gt=0, le=65536, strict=True)
    estimated_cost: str = Field(min_length=1)


class StepPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    step_id: str = Field(min_length=1, max_length=80)
    task_kind: str = Field(min_length=1, max_length=80)
    messages: list[dict[str, Any]]
    request_budget: int = Field(ge=0, strict=True)
    local_only: bool = False
    material_input: dict[str, Any] | None = None
    input_token_limit: int | None = Field(default=None, gt=0, strict=True)
    output_token_limit: int | None = Field(default=None, gt=0, strict=True)
    estimated_cost: str | None = None
    embedding_inputs: list[str] | None = None
    scope_summary: str | None = None
    # A grading workflow derives retrieval/rerank prompts from this immutable
    # domain input. Each possible remote task has its own finite send budget.
    grading_input: dict[str, Any] | None = None
    task_budgets: dict[str, Annotated[int, Field(ge=0, strict=True)]] = Field(default_factory=dict)
    task_model_budgets: dict[str, ModelBudget] = Field(default_factory=dict)

    def model_budget(self, task_kind: str) -> ModelBudget | None:
        if task_kind in self.task_model_budgets:
            return self.task_model_budgets[task_kind]
        # Previously persisted single-task plans keep their original meaning.
        # A derived task can never borrow the primary model's token budget.
        if (task_kind == self.task_kind and self.input_token_limit is not None
                and self.estimated_cost is not None):
            return ModelBudget(input_token_limit=self.input_token_limit,
                output_token_limit=self.output_token_limit, estimated_cost=self.estimated_cost)
        return None

    @model_validator(mode="after")
    def finite_grading_tasks(self):
        if self.local_only:
            if self.task_kind != "embedding" or self.request_budget != 0 or self.task_budgets:
                raise ValueError("Only local embedding has a zero HTTP budget")
        elif self.request_budget <= 0:
            raise ValueError("Remote tasks require a positive request budget")
        if self.grading_input is None:
            if self.task_budgets:
                raise ValueError("Derived tasks require frozen grading input")
        elif (
            self.task_kind not in self.task_budgets
            or any(isinstance(n, bool) or n < 0 for n in self.task_budgets.values())
            or sum(self.task_budgets.values()) != self.request_budget
            or not set(self.task_budgets) <= {"subjective_grading", "vision", "embedding", "rerank"}
        ):
            raise ValueError("A finite budget is required for each grading dependency")
        return self


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
    cleanup_error_code: str | None = None

    def record_cleanup_failure(self, error: BaseException) -> None:
        self.cleanup_error_code = "RESOURCE_RELEASE_FAILED"
        logger.error("Learning operation %s step %s resource close failed",
            self.operation_id, self.step.step_id,
            exc_info=(type(error), error, error.__traceback__))


class _ResponseStream(httpx.AsyncByteStream):
    """Keep stream teardown errors separate from HTTP body completeness."""

    def __init__(self, stream: httpx.AsyncByteStream, invocation: Invocation):
        self.stream, self.invocation = stream, invocation

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self.stream:
            yield chunk

    async def aclose(self) -> None:
        try:
            await self.stream.aclose()
        except (asyncio.CancelledError, Exception) as error:  # noqa: BLE001 - teardown cannot rewrite received/unknown request facts.
            self.invocation.record_cleanup_failure(error)


class GuardedTransport(httpx.AsyncBaseTransport):
    def __init__(self, service: LearningOperations, invocation: Invocation,
                 task_kind: str, profile: Mapping[str, Any]):
        self.service = service
        self.invocation = invocation
        self.task_kind = task_kind
        self.profile = profile

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        service = self.service
        call = self.invocation
        try:
            if call.step.local_only:
                raise OperationError("UNPLANNED_REMOTE_REQUEST")
            body = json.loads(request.content)
            model = call.runtime.requested_model(self.task_kind)
            if body.get("model") != model:
                raise OperationError("MODEL_CHANGED")
            scoped_input = body.get("messages", body.get("input", body))
            if self.task_kind == "vision":
                from backend.app.ai.vision.base import MAX_REQUEST_BYTES
                if len(request.content) > MAX_REQUEST_BYTES:
                    raise OperationError("LIMIT_EXCEEDED")
                # Image bytes have their own approved limit. The text limit
                # must not accidentally turn image data into a text budget.
                scoped_input = [{**m, "content": [part for part in m["content"]
                    if part.get("type") != "image_url"]}
                    if isinstance(m.get("content"), list) else m for m in scoped_input]
            service.limits.check("text_request_bytes", len(encoded(scoped_input)))
            budget = call.step.model_budget(self.task_kind)
            if (budget is not None and self.task_kind != "embedding"
                    and body.get("max_tokens") != budget.output_token_limit):
                raise OperationError("MODEL_BUDGET_CHANGED")
            profile = self.profile
            base = httpx.URL(profile["base_url"])
            if (
                request.url.scheme != base.scheme
                or request.url.host != base.host
                or request.url.port != base.port
                or not request.url.path.startswith(base.path.rstrip("/") + "/")
            ):
                raise OperationError("ENDPOINT_CHANGED")
            if (call.step.grading_input is None and "messages" in body
                    and body["messages"] != call.expected_messages):
                raise OperationError("INPUT_SCOPE_CHANGED")
            if "input" in body and call.step.grading_input is None:
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
                    or budget is None
                    or not isinstance(count := budget_input_tokens(service.token_counter, model, body), int)
                    or isinstance(count, bool)
                    or count < 0
                    or count > budget.input_token_limit
                ):
                    raise OperationError("MODEL_BUDGET_REQUIRED")
            request_index = service._reserve(call.operation_id, call.step.step_id,
                task_kind=self.task_kind, request_digest=digest(body), requested_model=model)
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
                if isinstance(response.stream, httpx.AsyncByteStream):
                    response.stream = _ResponseStream(response.stream, call)
                content = bytearray()
                try:
                    async for chunk in response.aiter_bytes():
                        response_limit = min(service.limits.response_bytes,
                            budget.response_bytes if budget is not None else service.limits.response_bytes)
                        if len(content) + len(chunk) > response_limit:
                            raise OperationError("LIMIT_EXCEEDED")
                        content.extend(chunk)
                finally:
                    await response.aclose()
            received = None
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
            service._received(call.operation_id, call.step.step_id, request_index,
                              {"status_code": response.status_code, **call.response_facts})
            if (self.task_kind == "embedding" and budget is not None and response.is_success
                    and (not isinstance(received, dict) or not isinstance(received.get("data"), list)
                        or any(not isinstance(item, dict)
                            or not isinstance(item.get("embedding"), list)
                            or len(item["embedding"]) != budget.embedding_dimension
                            for item in received["data"]))):
                raise OperationError("MODEL_BUDGET_CHANGED")
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
        except BaseException as error:
            # The reservation is durable before the transport is entered. A
            # complete response, rather than an exception class, establishes
            # whether the attempted request has a known outcome.
            call.failure = ("OUTCOME_UNKNOWN" if service._has_unknown_request(
                call.operation_id, call.step.step_id)
                else _failure_code(error))
            raise
        finally:
            try:
                service._release_slot(call.operation_id)
            finally:
                if owned:
                    try:
                        await inner.aclose()
                    except (asyncio.CancelledError, Exception) as error:  # noqa: BLE001 - preserve response facts and record teardown separately.
                        # Closing a fully read HTTP stream is separate from its
                        # already durable response and cannot turn it into a
                        # failed/unknown request or trigger another send.
                        call.record_cleanup_failure(error)

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
        task_model_budgets: Mapping[str, ModelBudget] | None = None,
    ):
        self.owned_engine: Engine | None = None
        self.sessions = sessions
        self.config = config
        self.transport = transport
        self.limits = limits or LearningLimits()
        self.paid_authorization = paid_authorization
        self.token_counter = token_counter
        self.task_model_budgets = dict(task_model_budgets or {})
        self._paid_validation_installed = False
        self.planners: dict[str, Callable[[dict[str, Any]], list[StepPlan]]] = {}
        self._current: ContextVar[Invocation] = ContextVar("learning_invocation")
        self._execution: ContextVar[tuple[UUID, UUID, dict] | None] = ContextVar("learning_execution", default=None)
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
        execution = self._execution.get()
        if lock and execution is not None and execution[0] == operation_id and row.execution_id != execution[1]:
            raise OperationError("EXECUTION_FENCED")
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
        storage_root: Any = None,
        expected_settings_revision: UUID | None = None,
    ) -> LearningOperationView:
        steps = [step.model_copy(update={"task_model_budgets": {
            **{task: self.task_model_budgets[task]
                for task in (step.task_budgets or {step.task_kind: step.request_budget})
                if task in self.task_model_budgets},
            **step.task_model_budgets,
        }}) for step in steps]
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
            if step.grading_input is not None:
                self.limits.check("text_request_bytes", len(encoded(step.grading_input)))
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
            task_kinds=tuple(dict.fromkeys(k for s in steps for k in (s.task_budgets or {s.task_kind: s.request_budget}))),
            storage_root=storage_root,
        )
        if expected_settings_revision is not None and snapshot.settings_revision != expected_settings_revision:
            raise OperationError("PREVIEW_CHANGED")
        saved = [
            {
                **s.model_dump(mode="json"),
                "model": snapshot.requested_model[s.task_kind],
                "task_models": {k: snapshot.requested_model.get(k) for k in s.task_budgets},
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

    def _reserve(self, operation_id: UUID, step_id: str, *,
                 task_kind: str | None = None, request_digest: str | None = None,
                 requested_model: str | None = None) -> int:
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
                budgets = step.get("task_budgets") or {step["task_kind"]: step["request_budget"]}
                kind = task_kind or step["task_kind"]
                used = dict(step.get("task_actual_requests") or {})
                if used.get(kind, 0) >= budgets.get(kind, 0):
                    raise OperationError("REQUEST_BUDGET_EXCEEDED")
                row.active_slot = "model"
                row.actual_requests += 1
                step["actual_requests"] += 1
                step["state"] = "outcome_unknown"
                used[kind] = used.get(kind, 0) + 1
                step["task_actual_requests"] = used
                step["requests"] = [*(step.get("requests") or []), {
                    "task_kind": kind, "request_digest": request_digest,
                    "requested_model": requested_model,
                    "state": "outcome_unknown",
                }]
                row.steps = values
                request_index = len(step["requests"]) - 1
            return request_index
        except IntegrityError:
            raise OperationError("MODEL_BUSY") from None

    def _received(self, operation_id: UUID, step_id: str, index: int, facts: dict) -> None:
        with self.sessions.begin() as session:
            row = self._row(session, operation_id, lock=True)
            values = copy.deepcopy(row.steps)
            step = next(s for s in values if s["step_id"] == step_id)
            step["requests"][index].update(state="response_received", **facts)
            row.steps = values

    def _has_unknown_request(self, operation_id: UUID, step_id: str) -> bool:
        step = next(s for s in self.view(operation_id).steps if s["step_id"] == step_id)
        return any(request["state"] == "outcome_unknown" for request in step.get("requests", []))

    def _release_slot(self, operation_id: UUID) -> None:
        with self.sessions.begin() as session:
            row = self._row(session, operation_id, lock=True)
            row.active_slot = None

    def _client_factory(self, task_kind: str, profile: Mapping[str, Any]) -> Any:
        call = self._current.get()
        if call.step.local_only and (
            task_kind != "embedding" or profile["adapter_id"] not in {"local", "bge", "huggingface"}
        ):
            raise OperationError("UNPLANNED_REMOTE_REQUEST")
        if task_kind not in (call.step.task_budgets or {call.step.task_kind: call.step.request_budget}):
            raise OperationError("UNPLANNED_TASK")
        if profile["adapter_id"] in {"local", "bge", "huggingface", "cross_encoder"}:
            return self.config.provider_registry.create(profile, credential_store=self.config.credential_store)
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
            transport=GuardedTransport(self, call, task_kind, dict(profile)),
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
        budget = call.step.model_budget(task_kind)
        options = {"client": client, "timeout": self.limits.http_timeout_seconds}
        if task_kind == "embedding":
            options["max_retries"] = 0
            if budget is not None and selected.get("parameters", {}).get("dimension") != budget.embedding_dimension:
                raise OperationError("MODEL_BUDGET_CHANGED")
        else:
            options["retry_policy"] = RetryPolicy(max_retries=0)
            if budget is not None:
                options["max_tokens"] = budget.output_token_limit
        return self.config.provider_registry.create(
            selected, credential_store=self.config.credential_store, **options
        )

    def _check_paid(self, view: LearningOperationView) -> None:
        if isinstance(self.transport, httpx.MockTransport):
            return
        if self.paid_authorization is None or not self.paid_authorization(view):
            raise OperationError("PAID_SEND_NOT_AUTHORIZED")
        if self.token_counter is None:
            raise OperationError("MODEL_BUDGET_REQUIRED")
        snapshot = self.snapshots.load(view.runtime_snapshot_id)
        for saved in view.steps:
            step = StepPlan.model_validate({k: v for k, v in saved.items() if k in StepPlan.model_fields})
            for task_kind, count in (step.task_budgets or {step.task_kind: step.request_budget}).items():
                if not count:
                    continue
                binding = snapshot.resolved_task_bindings.get(task_kind)
                if binding is not None and binding.adapter_id in {"local", "bge", "huggingface", "cross_encoder"}:
                    continue
                budget = step.model_budget(task_kind)
                if (budget is None
                        or (task_kind == "embedding" and (budget.embedding_dimension is None or budget.output_token_limit is not None))
                        or (task_kind != "embedding" and (budget.output_token_limit is None or budget.embedding_dimension is not None))):
                    raise OperationError("MODEL_BUDGET_REQUIRED")

    def preflight(self, operation_id: UUID) -> LearningOperationView:
        """No sends: reject missing authorization/dependencies before freezing answers."""
        current = self.view(operation_id)
        if current.state == "prepared":
            raise OperationError("CONFIRMATION_REQUIRED")
        if current.state == "confirmed":
            snapshot = self.snapshots.load(current.runtime_snapshot_id)
            if all(s.get("local_only") for s in current.steps):
                for step in current.steps:
                    binding = snapshot.resolved_task_bindings.get(step["task_kind"])
                    if (step["task_kind"] != "embedding" or step["request_budget"] != 0
                            or binding is None or binding.adapter_id not in {"local", "bge", "huggingface"}):
                        raise OperationError("UNPLANNED_REMOTE_REQUEST")
            else:
                self._check_paid(current)
        return current

    async def execute(
        self,
        operation_id: UUID,
        *,
        invoke: Callable[[Any, StepPlan], Awaitable[BaseModel]],
        before_claim: Callable[[Session], None] | None = None,
    ) -> LearningOperationView:
        from backend.app.services.operation_executor import ExecutorError, ExecutorGuard

        current = self.preflight(operation_id)
        if current.state != "confirmed":
            return current
        execution_id = uuid4()
        try:
            guard = ExecutorGuard.create(self.config.root, execution_id)
        except (ExecutorError, OSError) as error:
            raise OperationError("EXECUTOR_PROOF_UNAVAILABLE") from error
        with guard:
            token = self._execution.set((operation_id, execution_id, guard.identity))
            try:
                return await self._execute_owned(operation_id, invoke=invoke, before_claim=before_claim)
            finally:
                self._execution.reset(token)

    def recover(self, operation_id: UUID, *, expected_execution_id: UUID,
                executor_proof_root: Any = None) -> LearningOperationView:
        """Explicit offline management coordination; never invoked by startup or GET."""
        from backend.app.services.operation_executor import ExecutorError, ExecutorGuard

        proof_root = self.config.root
        if executor_proof_root is not None:
            from pathlib import Path
            proof_root = Path(executor_proof_root)
            if not proof_root.is_absolute() or proof_root != proof_root.resolve() or not proof_root.is_dir():
                raise OperationError("EXECUTOR_PROOF_UNAVAILABLE")
        with ExitStack() as proofs, self.sessions.begin() as session:
            row = self._row(session, operation_id, lock=True)
            owner = copy.deepcopy(row.executor_identity or {})
            if owner.get("recovered_from") == str(expected_execution_id):
                return LearningOperationView.model_validate(row)
            if row.execution_id != expected_execution_id or row.state != "running":
                raise OperationError("EXECUTION_CHANGED")
            try:
                proofs.enter_context(ExecutorGuard.stopped(proof_root, owner))
            except ExecutorError as error:
                raise OperationError(str(error)) from error
            values = copy.deepcopy(row.steps)
            unknown = False
            for step in values:
                step_unknown = step.get("state") == "outcome_unknown" or any(
                    request.get("state") == "outcome_unknown" for request in step.get("requests", []))
                unknown = unknown or step_unknown
                if step.get("state") != "succeeded":
                    step.update(state="outcome_unknown" if step_unknown else "failed",
                        error_code="OUTCOME_UNKNOWN" if step_unknown else "EXECUTOR_STOPPED")
            row.steps = values
            row.state = "outcome_unknown" if unknown else "partial" if any(
                s.get("state") == "succeeded" for s in values) else "failed"
            row.active_slot = None
            row.execution_id = uuid4()
            row.executor_identity = {**owner, "recovered_from": str(expected_execution_id)}
            session.flush()
            return LearningOperationView.model_validate(row)

    async def _execute_owned(
        self,
        operation_id: UUID,
        *,
        invoke: Callable[[Any, StepPlan], Awaitable[BaseModel]],
        before_claim: Callable[[Session], None] | None = None,
    ) -> LearningOperationView:
        current = self.preflight(operation_id)
        if current.state != "confirmed":
            return current
        snapshot = self.snapshots.load(current.runtime_snapshot_id)
        # Missing snapshot/dependencies never resolve against the current active revision.
        local_operation = all(s.get("local_only") for s in current.steps)
        execution = self._execution.get()
        if execution is None:
            raise OperationError("EXECUTOR_PROOF_UNAVAILABLE")
        try:
            with self.sessions.begin() as session:
                if before_claim is not None:
                    before_claim(session)
                claimed = session.execute(
                    update(OperationRun)
                    .where(
                        OperationRun.id == operation_id,
                        OperationRun.environment_id == self.config.environment_id,
                        OperationRun.state == "confirmed",
                    )
                    .values(state="running", active_slot="model" if local_operation else None,
                        execution_id=execution[1], executor_identity=execution[2])
                    .returning(OperationRun.id)
                ).scalar_one_or_none()
        except IntegrityError:
            raise OperationError("MODEL_BUSY") from None
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
                if observed["actual_requests"] == 0 and not step.local_only:
                    raise OperationError("NO_REQUEST_SENT")
            except asyncio.CancelledError:
                state = "outcome_unknown"
                failure = "OUTCOME_UNKNOWN"
            except Exception as error:  # noqa: BLE001 - persist failure and release leases without exposing provider text.
                failure = ("OUTCOME_UNKNOWN" if self._has_unknown_request(operation_id, step.step_id)
                    else call.failure or _failure_code(error))
                state = "outcome_unknown" if failure == "OUTCOME_UNKNOWN" else "failed"
            finally:
                try:
                    try:
                        await runtime.aclose()
                    except (asyncio.CancelledError, Exception) as error:  # noqa: BLE001 - close the remaining clients and retain the terminal result.
                        call.record_cleanup_failure(error)
                    for client in reversed(call.clients):
                        try:
                            close = getattr(client, "aclose", None) or getattr(
                                client, "close", None
                            )
                            if callable(close):
                                pending = close()
                                if inspect.isawaitable(pending):
                                    await pending
                        except (asyncio.CancelledError, Exception) as error:  # noqa: BLE001 - one close failure cannot skip other owned clients.
                            call.record_cleanup_failure(error)
                finally:
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
                if call.cleanup_error_code is not None:
                    target["cleanup_error_code"] = call.cleanup_error_code
                row.steps = values
            if state != "succeeded":
                final = state
                break
        if local_operation:
            self._release_slot(operation_id)
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
        storage_root: Any = None,
        expected_settings_revision: UUID | None = None,
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
            storage_root=storage_root,
            expected_settings_revision=expected_settings_revision,
        )

    async def aclose(self) -> None:
        first_error: BaseException | None = None
        try:
            await self.snapshots.aclose()
        except BaseException as error:  # noqa: BLE001 - finish all owned releases before re-raising the original error.
            first_error = error
        try:
            if self.transport is not None:
                await self.transport.aclose()
        except BaseException as error:  # noqa: BLE001 - finish all owned releases before re-raising the original error.
            if first_error is None:
                first_error = error
        try:
            if self.owned_engine is not None:
                self.owned_engine.dispose()
        except BaseException as error:  # noqa: BLE001 - finish all owned releases before re-raising the original error.
            if first_error is None:
                first_error = error
        if first_error is not None:
            raise first_error
