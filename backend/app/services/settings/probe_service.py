"""Single-capability, explicit-cost probes; never configuration activation.

Registry probe factories execute the real adapters. A probe owns its SDK/HTTP
clients, has a transport-level request budget and never uses business fallback.
Evidence assessment consumes trusted service/configuration facts, not arbitrary
HTTP request bodies. T210 owns durable acceptance and credential transactions.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import math
import socket
import ssl
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Any, ClassVar, Literal
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, Field, SecretStr, ValidationError

from backend.app.core.retry_policy import ProviderCallError, ProviderExecutionError
from backend.app.schemas.settings import ProviderProfile, SafeModel
from backend.app.services.settings.provider_registry import (
    ProviderRegistry,
    RegistryValidationError,
)

_MESSAGES = {
    "SUCCESS": "本次目标能力检测成功；不代表持续在线或业务质量合格。",
    "KEY_MISSING": "未提供可用密钥，请录入或绑定目标凭据后重新检测。",
    "KEY_INVALID": "服务明确拒绝了认证凭据，请核对或轮换目标密钥。",
    "CREDENTIAL_UNAVAILABLE": "目标凭据不可读取，请重新录入或核对外部 Secret 挂载。",
    "SERVICE_UNREACHABLE": "连接失败或检测超时，请检查服务地址、网络和证书后手动重试。",
    "MODEL_NOT_FOUND": "服务明确报告目标模型不存在，请核对模型标识。",
    "RATE_LIMITED": "服务限制了请求频率，请稍后手动重试；不会自动重试。",
    "CAPABILITY_UNSUPPORTED": "目标适配器或模型不支持所选能力，请调整绑定或能力。",
    "STRUCTURED_OUTPUT_INVALID": "返回内容未通过目标 JSON/结构校验，请核对模型能力。",
    "RESOURCE_UNAVAILABLE": "本地模型或运行资源不可用，请预置资源和依赖；不会自动下载。",
    "EMBEDDING_INCOMPATIBLE": "返回向量不符合目标维度合同，请核对模型与索引配置。",
    "UNKNOWN_PROVIDER_ERROR": "无法可靠确定服务故障原因，请按关联编号排查后手动重试。",
    "CANCELLED": "检测已取消；已发出的请求可能产生费用，取消不代表退款。",
}
_BINDING_FIELDS = ("profile_id", "profile_revision", "credential_version", "model_id", "capability")
_KIND = {"text": "llm", "json_object": "llm", "json_schema": "llm", "vision": "vision",
         "embedding": "embedding", "llm_rerank": "rerank", "local_rerank": "rerank", "ocr": "ocr"}
_PRIVATE_CALL: ContextVar[bool] = ContextVar("private_settings_probe", default=False)
# Package-owned 1x1 PNG for the OCR probe; never a business file or remote URL.
_SAMPLE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aB9sAAAAASUVORK5CYII="
)
# Package-owned 128x128 RGB PNG for the vision probe. Some providers reject a
# 1x1 image even though the documented resize path accepts small inputs.
_VISION_SAMPLE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAIAAAACACAIAAABMXPacAAADG0lEQVR42u3dW3LjMAxEUQn73zPnd8qR9TLA7gZbK0jd4ySWRJD7GGPzhbvCCQxgAF8GMIAvAxjg49r33YFSrpOQcV7fBln1v4WMy8++DbI++4chjwE+bo9tkPKX5/CZw9c/QTaYUP/in7ANqutffw21QWn9W/cBNqirf/dGzAZF9R/cCdugov6zRxE2SK//+FmQDXLrv3kYZ4PE+i+fhtogq/77x9E2SKn/0/uAxQ1S6v/6QmZZg6z6CW/EFjRIrJ/zSnIpg9z6ae+EFzFIr5/5Ur69QUX95FURjQ2K6ucvS2lpUFe/ZF1QM4PS+lULs9oYVNcvXBnXwGBC/dqlidIGc+qXrw0VNZhWf8biXDmDmfUnrY4WMphcf97ydAmD+fWnzgeQG0Dqzx7QoDVA1QdMyBAaAOtjRpSoDLD1YTNiJAbw+sghPbgBQ33wlCTQgKQ+fkwVYsBTn2JOeLIBVX2WQe1pBmz1iSblJxgQ1ufaqqDUgLM+3V4RRQa09Rk360g3YK5PultKogF5fd7talIM+OtT7xf0o4FEffYNm14bqNQX2DHrhYFQfY0tyx4ZaNWX2TPupoFcfaVN+y4NFOuL7Zp4YiBaX2/bykMD3fqS+4b+MRCur7px638GQ7q+8M65Y4wG9YUBDr4Eac7hRI/6unM40WxySM4gGtSXnoWKHp99XYNoMzUnahCdpuYUDaLZzKKcQfSbWdQyiJYTo0IG0XViVMUgGs/rShhE73ldfoNoPy1NbhArTEszG+yPzxOu/+n3bVQ83//oTnKQMu3a0G2ROf1YpD6twXKHebIZrHiaKpXBosfZ8hise54wicHSBzozGKx+ojbcwEeagw0MADYwANjAAGADA4ANDAA2MADYwABgAwOADQwANjAA2MAAYAMDgA0MADYwANjAAGADA4ANDAA2MADYwABgAwOADQwANgiypenLGYTrg/eYvTmnoL4z2DZpwufxDEi4Pvb3IFwfaxCujzUI18cahOtjDcL1sQZxOafo+lkGh99K43xW1PWzDL7dE+zDjTePqRrAlwEM4MsAC17/ALf3ARRXILvxAAAAAElFTkSuQmCC"
)
_SAMPLE_TEXT = "A short synthetic connection probe."


class _PrivateProbeLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _PRIVATE_CALL.get()


_private_logs = _PrivateProbeLogs()
for _logger in ("openai._base_client", "openai._response", "openai._legacy_response", "httpx",
                "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy", "httpcore.socks"):
    logging.getLogger(_logger).addFilter(_private_logs)


class ProbeRequestError(ValueError):
    """Local request rejection; no upstream text, credential or validation input."""

    def __init__(self, code: str, status_code: int = 422) -> None:
        super().__init__("检测请求无效或当前环境已有检测，请核对参数后手动重试。")
        self.code = code
        self.status_code = status_code


class ProbeError(SafeModel):
    safe_error_code: str
    message: str
    correlation_id: str
    retryable: bool = False
    retry_after: float | None = None


class ProbeResult(ProbeError):
    probe_id: UUID
    environment_id: UUID
    candidate_revision: UUID
    profile_id: UUID
    profile_revision: UUID
    credential_id: UUID | None = None
    credential_version: UUID | None = None
    model_id: str
    capability: str
    status: Literal["running", "succeeded", "failed", "cancelled"]
    tested_at: datetime
    expires_at: datetime | None = None
    elapsed_ms: int = 0
    external_requests: int = Field(default=0, ge=0, le=1)
    usage: dict[str, int] | None = None
    cancelled: bool = False
    sample_id: str = "eduagent-probe-v1"
    may_have_been_billed: bool = False
    cleanup_error_code: str | None = None


class _ProbeFailure(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("目标能力检测未完成。")


def _retry_after(error: BaseException) -> float | None:
    response = getattr(error, "response", None)
    value = getattr(response, "headers", {}).get("retry-after")
    try:
        number = float(value) if value is not None else float("nan")
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and 0 <= number <= 86400 else None


def _structured_code(error: BaseException) -> str | None:
    body = getattr(error, "body", None)
    response = getattr(error, "response", None)
    if not isinstance(body, Mapping) and isinstance(response, httpx.Response):
        try:
            if len(response.content) <= 65536:
                body = response.json()
        except (ValueError, httpx.ResponseNotRead):
            return None
    if not isinstance(body, Mapping):
        return None
    details = body.get("error", body)
    if not isinstance(details, Mapping):
        return None
    code = details.get("code") or details.get("type")
    return code if isinstance(code, str) else None


def classify_probe_error(error: BaseException, *, correlation_id: str) -> ProbeError:
    """Classify positive facts only. Never stringify or serialize an SDK error.

    Trusted adapter wrappers may hide SDK facts behind ``raise ... from None``.
    Inspect their bounded in-memory cause/context chain, not arbitrary exception
    text or untrusted class names; no raw exception survives in the result.
    """
    import openai

    from backend.app.ai.embedding.base import (
        EmbeddingDimensionError,
        EmbeddingProviderError,
        EmbeddingProviderNotReadyError,
    )
    from backend.app.ai.ingestion.ocr.base import OCRProviderError
    from backend.app.ai.retrieval.reranker import (
        RerankFailedError,
        RerankProviderNotReadyError,
    )

    current = error
    visited: set[int] = set()
    code = "UNKNOWN_PROVIDER_ERROR"
    retry_after = None
    for _ in range(8):
        if id(current) in visited:
            break
        visited.add(id(current))
        if isinstance(current, asyncio.CancelledError):
            code = "CANCELLED"
            break
        if isinstance(current, (TimeoutError, httpx.TimeoutException, openai.APITimeoutError,
                                httpx.NetworkError, openai.APIConnectionError, socket.gaierror,
                                ssl.SSLError, ConnectionError)):
            code = "SERVICE_UNREACHABLE"
            break
        if isinstance(current, (json.JSONDecodeError, ValidationError)):
            code = "STRUCTURED_OUTPUT_INVALID"
            break
        if isinstance(current, EmbeddingDimensionError):
            code = "EMBEDDING_INCOMPATIBLE"
            break
        if isinstance(current, (FileNotFoundError, ImportError, EmbeddingProviderNotReadyError,
                                RerankProviderNotReadyError)):
            code = "RESOURCE_UNAVAILABLE"
            break
        if isinstance(current, (_ProbeFailure, RegistryValidationError, ProviderCallError, ProviderExecutionError,
                                OCRProviderError)):
            owned_code = current.code
            mapped = {"StructuredOutputFailed": "STRUCTURED_OUTPUT_INVALID",
                      "ProviderEmptyResponse": "STRUCTURED_OUTPUT_INVALID",
                      "VISION_NOT_SUPPORTED": "CAPABILITY_UNSUPPORTED",
                      "OCR_PROVIDER_NOT_READY": "RESOURCE_UNAVAILABLE",
                      "OCR_OUTPUT_INVALID": "STRUCTURED_OUTPUT_INVALID"}.get(owned_code, owned_code)
            if mapped in _MESSAGES:
                code = mapped
                break
        if isinstance(current, (openai.APIStatusError, httpx.HTTPStatusError)):
            status = current.response.status_code
            upstream = _structured_code(current)
            if status == 429:
                code, retry_after = "RATE_LIMITED", _retry_after(current)
            elif (isinstance(current, openai.AuthenticationError) and status == 401) or (
                status in {401, 403} and upstream in {"invalid_api_key", "invalid_token", "authentication_error"}
            ):
                code = "KEY_INVALID"
            elif status in {400, 404} and upstream == "model_not_found":
                code = "MODEL_NOT_FOUND"
            elif status in {400, 422} and upstream in {"unsupported_capability", "unsupported_response_format"}:
                code = "CAPABILITY_UNSUPPORTED"
            break
        if not isinstance(current, (ProviderCallError, ProviderExecutionError, EmbeddingProviderError,
                                    OCRProviderError, RerankFailedError)):
            break
        nested = current.__cause__ or current.__context__
        if nested is None:
            break
        current = nested
    return ProbeError(safe_error_code=code, message=_MESSAGES[code], correlation_id=correlation_id,
                      retryable=code in {"RATE_LIMITED", "SERVICE_UNREACHABLE"}, retry_after=retry_after)


class _MinimalAnswer(SafeModel):
    ok: bool


@dataclass
class _Execution:
    resources: list[Any] = field(default_factory=list)
    requests: int = 0
    stopped: bool = False
    usage: dict[str, int] | None = None
    pending_load: asyncio.Task[Any] | None = None
    pending_credentials: asyncio.Task[Any] | None = None


class _LocalResource:
    """Own native work until it really ends, even if the caller stops waiting."""

    def __init__(self, provider: Any, capability: str) -> None:
        self.provider = provider
        self.capability = capability
        self.operation: asyncio.Task[Any] | None = None

    async def prepare(self) -> None:
        from backend.app.ai.embedding.base import EmbeddingDimensionError
        from backend.app.ai.embedding.providers.local import LocalEmbeddingProvider

        # Built-in Embedding loading is lazy. Give that known phase an explicit
        # boundary without guessing that arbitrary inference failures are loads.
        if isinstance(self.provider, LocalEmbeddingProvider):
            try:
                await self.provider._ensure_model()
            except EmbeddingDimensionError:
                raise
            except Exception:  # noqa: BLE001 - 已明确处于本地加载阶段，只返回资源不可用。
                raise _ProbeFailure("RESOURCE_UNAVAILABLE") from None

    async def run(self, sample: Any) -> Any:
        self.operation = asyncio.create_task(self._invoke(sample))
        return await asyncio.shield(self.operation)

    async def _invoke(self, sample: Any) -> Any:
        if self.capability == "embedding":
            return await self.provider.embed_documents([sample])
        if self.capability == "local_rerank":
            return await self.provider.rerank_async("synthetic probe", _rerank_candidates(), top_k=2)
        directory = await asyncio.to_thread(TemporaryDirectory, prefix="eduagent-probe-")
        try:
            image = Path(directory.name) / "sample.png"
            await asyncio.to_thread(image.write_bytes, sample)
            return await self.provider.extract_text(image)
        finally:
            await asyncio.to_thread(directory.cleanup)

    async def aclose(self) -> None:
        try:
            if self.operation is not None:
                await asyncio.gather(self.operation, return_exceptions=True)
        finally:
            close = getattr(self.provider, "aclose", None)
            if callable(close):
                await close()
            self.provider = None


def _rerank_candidates() -> list[Any]:
    from backend.app.ai.retrieval.base import RetrievedChunk

    return [RetrievedChunk(chunk_id=identifier, course_id="probe", document_id="probe",
                           content=text, metadata={}) for identifier, text in (
        ("probe-a", "A synthetic relevant sentence."), ("probe-b", "A synthetic different sentence."),
    )]


class ProbeService:
    # The application is single-process per environment; this mutex also covers
    # separate service instances/threads/event loops, without an asyncio loop lock.
    _paid: ClassVar[set[UUID]] = set()
    _local: ClassVar[set[UUID]] = set()
    _paid_mutex = threading.Lock()

    def __init__(
        self, *, environment_id: UUID, registry: ProviderRegistry, credential_store: Any,
        http_client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
        clock: Callable[[], datetime] | None = None,
        wait_for: Callable[..., Awaitable[Any]] = asyncio.wait_for,
        local_resource_loader: Callable[..., Any] | None = None,
        max_results: int = 128,
        validation_policy: Any = None,
    ) -> None:
        if not isinstance(environment_id, UUID) or not environment_id.int or max_results < 1:
            raise ProbeRequestError("INVALID_PROBE_SERVICE")
        self.environment_id = environment_id
        self.registry = registry
        self.credential_store = credential_store
        self._http_factory = http_client_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic: Callable[[], float] = getattr(self._clock, "monotonic", monotonic)
        self._wait_for = wait_for
        self._local_loader = local_resource_loader
        self._max_results = max_results
        self._results: OrderedDict[UUID, ProbeResult] = OrderedDict()
        self._running: dict[UUID, asyncio.Task[Any]] = {}
        self._cleanup: set[asyncio.Task[Any]] = set()
        self._closed = False
        self.validation_policy = validation_policy

    def get(self, probe_id: UUID) -> ProbeResult:
        try:
            return self._results[probe_id].model_copy(deep=True)
        except KeyError:
            raise ProbeRequestError("PROBE_NOT_FOUND", 404) from None

    def for_candidate(self, candidate_revision: UUID) -> tuple[ProbeResult, ...]:
        """只投影内存中已有检测，不创建任务或读取凭据。"""
        return tuple(result.model_copy(deep=True) for result in self._results.values()
                     if result.candidate_revision == candidate_revision)

    def cancel(self, probe_id: UUID) -> ProbeResult:
        result = self.get(probe_id)
        task = self._running.get(probe_id)
        if task is not None:
            task.cancel()
        return result

    def _remember(self, result: ProbeResult) -> None:
        self._results[result.probe_id] = result.model_copy(deep=True)
        while len(self._results) > self._max_results:
            removable = next((key for key, item in self._results.items() if item.status != "running"), None)
            if removable is None:
                break
            del self._results[removable]

    async def run(
        self, *, candidate_revision: UUID, profile: ProviderProfile | Mapping[str, Any], capability: str,
        allow_billable: bool = False, timeout_seconds: float = 15, probe_id: UUID | None = None,
    ) -> ProbeResult:
        if self._closed:
            raise ProbeRequestError("PROBE_SERVICE_CLOSED", 409)
        if len(self._running) + len(self._cleanup) >= self._max_results:
            raise ProbeRequestError("PROBE_LIMIT_REACHED", 429)
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or not 5 <= timeout_seconds <= 30
            or not isinstance(candidate_revision, UUID) or type(allow_billable) is not bool
            or (probe_id is not None and (not isinstance(probe_id, UUID) or not probe_id.int
                or probe_id in self._running or probe_id in self._results))):
            raise ProbeRequestError("INVALID_PROBE_REQUEST")
        try:
            raw = profile.model_dump(mode="python") if isinstance(profile, ProviderProfile) else dict(profile)
            parsed = ProviderProfile.model_validate(raw, context={"provider_registry": self.registry})
        except (ValidationError, TypeError, ValueError):
            raise ProbeRequestError("INVALID_PROVIDER_PROFILE") from None
        ref = parsed.credential_ref
        if ref is not None and (ref.environment_id != self.environment_id or ref.purpose != "provider"):
            raise ProbeRequestError("CREDENTIAL_REFERENCE_MISMATCH")
        kind = _KIND.get(capability)
        code = None
        try:
            if kind is None or capability not in parsed.declared_capabilities:
                raise RegistryValidationError("CAPABILITY_UNSUPPORTED")
            parsed = self.registry.validate_profile(parsed, kind=kind)
            if not parsed.enabled:
                raise ProbeRequestError("PROFILE_DISABLED")
        except RegistryValidationError as error:
            if error.code not in {"CAPABILITY_UNSUPPORTED", "ADAPTER_UNSUPPORTED"}:
                raise ProbeRequestError(error.code) from None
            code = "CAPABILITY_UNSUPPORTED"
        local = parsed.resource_directory is not None and (
            kind in {"ocr", "rerank"} and capability != "llm_rerank"
            or kind == "embedding" and parsed.adapter_id != "openai_compatible"
        )
        # A local preset with missing directory is still local: never ask for a
        # cloud key or fee confirmation before reporting missing local resources.
        local = local or kind == "ocr" or capability == "local_rerank" or (
            kind == "embedding" and parsed.adapter_id != "openai_compatible"
        )
        if not local and code is None and not allow_billable:
            raise ProbeRequestError("BILLABLE_CONFIRMATION_REQUIRED")
        validation = None
        if not local and code is None and self.validation_policy is not None:
            validation = self.validation_policy.probe_guard(
                environment_id=self.environment_id, candidate_revision=candidate_revision,
                profile=parsed, capability=capability)
        reserved = False
        lease_pool = self._local if local else self._paid
        if code is None:
            with self._paid_mutex:
                if self.environment_id in lease_pool:
                    raise ProbeRequestError("PROBE_LIMIT_REACHED", 429)
                lease_pool.add(self.environment_id)
                reserved = True
        state = _Execution()
        started = self._monotonic()
        stamp = self._clock()
        result = ProbeResult(
            safe_error_code="SUCCESS", message="正在检测目标能力。", correlation_id=str(uuid4()),
            probe_id=probe_id or uuid4(), environment_id=self.environment_id, candidate_revision=candidate_revision,
            profile_id=parsed.profile_id, profile_revision=parsed.profile_revision,
            credential_id=ref.credential_id if ref else None, credential_version=ref.version if ref else None,
            model_id=parsed.model_id, capability=capability, status="running", tested_at=stamp,
        )
        worker: asyncio.Task[Any] | None = None
        current = asyncio.current_task()
        if current is not None:
            self._running[result.probe_id] = current
        self._remember(result)
        private = _PRIVATE_CALL.set(True)
        cleanup_code = None
        failure = self._error(code or "SUCCESS", result.correlation_id)
        try:
            if code is None:
                worker = asyncio.create_task(self._execute(parsed, capability, kind or "llm", local,
                                                            float(timeout_seconds), state, validation))
                try:
                    # Shield avoids wait_for's unbounded wait for a cancellation-
                    # resistant operation. Teardown has its own two-second budget.
                    await self._wait_for(asyncio.shield(worker), timeout_seconds)
                except (asyncio.CancelledError, Exception) as error:  # noqa: BLE001 - Probe边界须收敛任意SDK异常且不泄露正文。
                    failure = classify_probe_error(error, correlation_id=result.correlation_id)
        finally:
            state.stopped = True
            if worker is not None and not worker.done():
                worker.cancel()
            release = asyncio.create_task(self._release(worker, state))
            try:
                cleanup_code = await self._wait_for(asyncio.shield(release), 2.0)
            except TimeoutError:
                cleanup_code = "RESOURCE_RELEASE_TIMEOUT"
            except asyncio.CancelledError:
                failure = self._error("CANCELLED", result.correlation_id)
                cleanup_code = "RESOURCE_RELEASE_PENDING"
            finally:
                self._cleanup.add(release)
                def finished(task: asyncio.Task[Any]) -> None:
                    self._cleanup.discard(task)
                    # Consume any terminal error; never log raw third-party text.
                    if not task.cancelled():
                        task.exception()
                    if reserved:
                        with self._paid_mutex:
                            lease_pool.discard(self.environment_id)
                if release.done():
                    finished(release)
                else:
                    release.add_done_callback(finished)
                self._running.pop(result.probe_id, None)
                _PRIVATE_CALL.reset(private)
        if cleanup_code is not None and failure.safe_error_code == "SUCCESS":
            failure = self._error("UNKNOWN_PROVIDER_ERROR", result.correlation_id)
        succeeded = failure.safe_error_code == "SUCCESS"
        cancelled = failure.safe_error_code == "CANCELLED"
        result = result.model_copy(update={
            **failure.model_dump(), "status": "succeeded" if succeeded else ("cancelled" if cancelled else "failed"),
            "tested_at": self._clock(), "expires_at": self._clock() + timedelta(minutes=10) if succeeded else None,
            "external_requests": state.requests, "usage": state.usage,
            "elapsed_ms": max(0, int((self._monotonic() - started) * 1000)),
            "cancelled": cancelled, "may_have_been_billed": state.requests > 0,
            "cleanup_error_code": cleanup_code,
        })
        self._remember(result)
        return result.model_copy(deep=True)

    @staticmethod
    def _error(code: str, correlation: str) -> ProbeError:
        return ProbeError(safe_error_code=code, message=_MESSAGES[code], correlation_id=correlation)

    async def _execute(self, profile: ProviderProfile, capability: str, kind: str, local: bool,
                       timeout: float, state: _Execution, validation: Any = None) -> None:
        if local:
            directory = profile.resource_directory
            def load_sync() -> Any:
                if directory is None or not directory.is_dir():
                    raise _ProbeFailure("RESOURCE_UNAVAILABLE")
                if self._local_loader is not None:
                    return self._local_loader(profile=profile, capability=capability)
                return _LocalResource(self.registry.create(profile, kind=kind, probe=True), capability)
            async def load() -> Any:
                try:
                    resource = await asyncio.to_thread(load_sync)
                    if inspect.isawaitable(resource):
                        resource = await resource
                except Exception:  # noqa: BLE001 - 此阶段只加载本地资源，第三方失败统一安全报告且不下载。
                    raise _ProbeFailure("RESOURCE_UNAVAILABLE") from None
                state.resources.append(resource)
                if isinstance(resource, _LocalResource):
                    await resource.prepare()
                return resource
            state.pending_load = asyncio.create_task(load())
            resource = await asyncio.shield(state.pending_load)
            sample = _SAMPLE_PNG if capability == "ocr" else _SAMPLE_TEXT
            try:
                answer = await resource.run(sample)
            except (FileNotFoundError, ImportError):
                raise _ProbeFailure("RESOURCE_UNAVAILABLE") from None
            if capability == "embedding":
                self._validate_vectors(answer)
            return
        key = None
        if profile.auth_mode == "api_key":
            ref = profile.credential_ref
            if ref is None:
                raise _ProbeFailure("KEY_MISSING")
            try:
                state.pending_credentials = asyncio.create_task(
                    asyncio.to_thread(self.credential_store.get, ref, ref.version)
                )
                key = await asyncio.shield(state.pending_credentials)
            except KeyError:
                raise _ProbeFailure("KEY_MISSING") from None
            except Exception:  # noqa: BLE001 - 凭据后端失败不得携秘密逃出Probe边界。
                # No inference that a locally unreadable key was rejected upstream.
                raise _ProbeFailure("CREDENTIAL_UNAVAILABLE") from None
            if not isinstance(key, SecretStr) or not key.get_secret_value().strip():
                raise _ProbeFailure("KEY_MISSING")
        if profile.base_url is None:
            raise ProbeRequestError("ENDPOINT_MISSING")
        async def request_hook(request: httpx.Request) -> None:
            if state.stopped or state.requests:
                raise _ProbeFailure("UNKNOWN_PROVIDER_ERROR")
            if validation is not None:
                validation.reserve(json.loads(request.content))
            state.requests += 1
        async def response_hook(response: httpx.Response) -> None:
            if validation is None:
                await response.aread()
            else:
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > validation.grant.model_budget.response_bytes:
                        raise _ProbeFailure("UNKNOWN_PROVIDER_ERROR")
                    content.extend(chunk)
                response._content = bytes(content)
                validation.received(response.status_code)
            try:
                payload = response.json()
            except ValueError:
                return
            raw_usage = payload.get("usage") if isinstance(payload, dict) else None
            if isinstance(raw_usage, dict):
                counts = {target: raw_usage.get(source) for source, target in (
                    ("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens"), ("total_tokens", "total_tokens"),
                )}
                safe = {name: value for name, value in counts.items()
                        if type(value) is int and 0 <= value <= 10**12}
                state.usage = safe or None
        http = self._http_factory(timeout=timeout, follow_redirects=False, trust_env=False,
                                  event_hooks={"request": [request_hook], "response": [response_hook]})
        state.resources.append(http)
        from openai import AsyncOpenAI

        options: dict[str, Any] = {"api_key": key.get_secret_value() if key else "", "base_url": profile.base_url,
                                   "max_retries": 0, "timeout": timeout, "http_client": http,
                                   "organization": "", "project": ""}
        parameters = inspect.signature(AsyncOpenAI).parameters
        for name in ("admin_api_key", "webhook_secret"):
            if name in parameters:
                options[name] = ""
        if "_enforce_credentials" in parameters:
            options["_enforce_credentials"] = profile.auth_mode != "none"
        sdk = AsyncOpenAI(**options)
        sdk._custom_headers = {}
        state.resources.append(sdk)
        budget_options = {"max_tokens": validation.max_tokens} if validation is not None and capability != "embedding" else {}
        provider = self.registry.create(profile, kind=kind, probe=True, api_key=key, client=sdk, **budget_options)
        state.resources.append(provider)
        if capability == "embedding":
            self._validate_vectors(await provider.embed_documents([_SAMPLE_TEXT]))
        elif capability == "llm_rerank":
            await provider.rerank_async("synthetic probe", _rerank_candidates(), top_k=2)
        else:
            messages: list[Any] = [{"role": "user", "content": 'Return JSON {"ok": true} for this synthetic probe.'}]
            if capability == "vision":
                messages[0]["content"] = [
                    {"type": "text", "text": 'Return JSON {"ok": true} for this synthetic image.'},
                    {"type": "image", "image": {"encoding": "base64", "mime_type": "image/png",
                                                  "value": base64.b64encode(_VISION_SAMPLE_PNG).decode("ascii")}},
                ]
            await provider.generate_structured(messages, _MinimalAnswer, model=profile.model_id)

    @staticmethod
    def _validate_vectors(vectors: Any) -> None:
        if (not isinstance(vectors, list) or len(vectors) != 1 or len(vectors[0]) != 1024
            or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                   for value in vectors[0])):
            raise _ProbeFailure("EMBEDDING_INCOMPATIBLE")

    @staticmethod
    async def _release(worker: asyncio.Task[Any] | None, state: _Execution) -> str | None:
        error_code = None
        if worker is not None:
            await asyncio.gather(worker, return_exceptions=True)
        if state.pending_load is not None:
            await asyncio.gather(state.pending_load, return_exceptions=True)
        if state.pending_credentials is not None:
            await asyncio.gather(state.pending_credentials, return_exceptions=True)
            state.pending_credentials = None
        for resource in reversed(state.resources):
            close = getattr(resource, "aclose", None) or getattr(resource, "close", None)
            if callable(close):
                try:
                    answer = close()
                    if inspect.isawaitable(answer):
                        await answer
                except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 单个关闭失败/取消不能跳过其余自有资源。
                    error_code = "RESOURCE_RELEASE_FAILED"
        state.resources.clear()
        return error_code

    def assess_evidence(self, *, target: Mapping[str, Any], probe: Mapping[str, Any] | BaseModel,
                        activation_validation: Mapping[str, Any] | BaseModel | None = None) -> dict[str, Any]:
        evidence = probe.model_dump() if isinstance(probe, BaseModel) else dict(probe)
        accepted = activation_validation.model_dump() if isinstance(activation_validation, BaseModel) else activation_validation
        fields = _BINDING_FIELDS + tuple(name for name in ("environment_id", "credential_id") if name in target)
        def matches(facts: Mapping[str, Any]) -> bool:
            if target.get("environment_id", self.environment_id) != self.environment_id:
                return False
            if facts.get("environment_id", self.environment_id) != self.environment_id:
                return False
            return all(name in target and name in facts and facts[name] == target[name] for name in fields)
        def stamp(value: Any) -> datetime | None:
            if isinstance(value, str):
                try:
                    value = datetime.fromisoformat(value)
                except ValueError:
                    return None
            return value if isinstance(value, datetime) and value.tzinfo is not None else None
        if accepted and accepted.get("status") in {"active", "pending_restart"} and matches(accepted):
            tested, at = stamp(accepted.get("tested_at")), stamp(accepted.get("accepted_at"))
            if (isinstance(accepted.get("probe_id"), UUID) and tested is not None and at is not None
                and tested <= at <= tested + timedelta(minutes=10) and at <= self._clock()):
                return {"valid": True, "code": None, "source": "activation_validation"}
        required = {"valid": False, "code": "PROBE_REQUIRED"}
        if (not matches(evidence) or "candidate_revision" not in target
            or evidence.get("candidate_revision") != target["candidate_revision"]
            or evidence.get("status") != "succeeded" or evidence.get("safe_error_code") != "SUCCESS"
            or not isinstance(evidence.get("probe_id"), UUID)):
            return required
        tested, expires = stamp(evidence.get("tested_at")), stamp(evidence.get("expires_at"))
        now = self._clock()
        if tested is None or expires is None or tested > now or expires < tested:
            return required
        if now > min(expires, tested + timedelta(minutes=10)):
            return {"valid": False, "code": "PROBE_EXPIRED"}
        return {"valid": True, "code": None, "source": "probe"}

    async def aclose(self) -> None:
        self._closed = True
        tasks = set(self._running.values())
        for task in tasks:
            task.cancel()
        tasks.update(self._cleanup)
        if tasks:
            # Shutdown is bounded too; ownership remains with unfinished tasks.
            await asyncio.wait(tasks, timeout=2.0)


__all__ = ["ProbeError", "ProbeRequestError", "ProbeResult", "ProbeService", "classify_probe_error"]
