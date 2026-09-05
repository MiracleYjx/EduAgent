"""统一的 Provider 重试、退避和备用 Provider 执行策略。"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar

from openai import APIConnectionError, APIError, APITimeoutError, RateLimitError

T = TypeVar("T")
type AsyncOperation[T] = Callable[[], Awaitable[T]]
type AsyncSleep = Callable[[float], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class ProviderErrorInfo:
    """对外暴露的脱敏 Provider 失败状态。"""

    code: str
    message: str
    attempt_count: int
    retryable: bool = False
    status: str = "failed"
    fallback_attempted: bool = False

    @property
    def error_code(self) -> str:
        """提供语义更明确的错误码别名。"""

        return self.code

    @property
    def final_status(self) -> str:
        """返回最终业务失败状态。"""

        return self.status


class ProviderCallError(RuntimeError):
    """Provider 调用过程中的可分类、可重试内部错误。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        retry_after: float | None = None,
        retry_limit: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.retryable = retryable
        self.retry_after = retry_after
        self.retry_limit = retry_limit


class ProviderExecutionError(RuntimeError):
    """重试和备用 Provider 均无法完成时返回的脱敏失败异常。"""

    def __init__(self, info: ProviderErrorInfo) -> None:
        super().__init__(info.message)
        self.info = info
        self.code = info.code
        self.error_code = info.code
        self.status = info.status
        self.attempt_count = info.attempt_count


def _status_code(error: BaseException) -> int | None:
    value = getattr(error, "status_code", None)
    if value is None:
        response = getattr(error, "response", None)
        value = getattr(response, "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _retry_after(error: BaseException) -> float | None:
    value = getattr(error, "retry_after", None)
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if isinstance(headers, Mapping):
        for name in ("retry-after", "Retry-After"):
            header_value = headers.get(name)
            if header_value is not None:
                value = header_value
                break

    try:
        parsed = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return parsed if parsed is not None and parsed >= 0 else None


def classify_provider_exception(error: Exception) -> ProviderCallError:
    """将 SDK 或运行时异常转换为不含敏感详情的内部错误。"""

    if isinstance(error, ProviderCallError):
        return error
    if isinstance(error, ProviderExecutionError):
        return ProviderCallError(
            error.info.code,
            error.info.message,
            retryable=False,
        )
    if isinstance(error, (APITimeoutError, TimeoutError)):
        return ProviderCallError(
            "ProviderTimeout",
            "LLM Provider 请求超时。",
            retryable=True,
        )

    status_code = _status_code(error)
    if isinstance(error, RateLimitError) or status_code == 429:
        return ProviderCallError(
            "ProviderRateLimited",
            "LLM Provider 请求受到频率限制。",
            retryable=True,
            retry_after=_retry_after(error),
        )
    if isinstance(error, APIConnectionError):
        return ProviderCallError(
            "ProviderFailed",
            "LLM Provider 网络连接失败。",
            retryable=True,
        )
    if isinstance(error, APIError):
        return ProviderCallError(
            "ProviderFailed",
            "LLM Provider 服务调用失败。",
            retryable=status_code is None or status_code >= 500,
        )
    if isinstance(error, OSError):
        return ProviderCallError(
            "ProviderFailed",
            "LLM Provider 网络连接失败。",
            retryable=True,
        )
    return ProviderCallError(
        "ProviderFailed",
        "LLM Provider 调用失败。",
        retryable=False,
    )


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """限制 Provider 重试次数并统一计算退避等待时间。"""

    max_retries: int = 2
    backoff_seconds: tuple[float, ...] = (1.0, 2.0)
    max_retry_after: float = 30.0
    jitter_seconds: float = 0.1

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("重试次数不能为负数。")
        if not self.backoff_seconds or any(delay < 0 for delay in self.backoff_seconds):
            raise ValueError("退避时间必须包含非负数。")
        if self.max_retry_after <= 0:
            raise ValueError("Retry-After 上限必须大于零。")
        if self.jitter_seconds < 0:
            raise ValueError("抖动时间不能为负数。")

    async def execute(
        self,
        operation: AsyncOperation[T],
        *,
        fallback: AsyncOperation[T] | None = None,
        sleep: AsyncSleep = asyncio.sleep,
        random_uniform: Callable[[float, float], float] = random.uniform,
    ) -> T:
        """执行主 Provider，耗尽后最多调用一次备用 Provider。"""

        attempt_count = 0
        retry_count = 0

        while True:
            attempt_count += 1
            try:
                return await operation()
            except Exception as raw_error:  # noqa: BLE001
                # Provider 可能抛出 SDK 未覆盖的异常，统一转换为脱敏状态。
                error = classify_provider_exception(raw_error)
                retry_limit = self.max_retries
                if error.retry_limit is not None:
                    retry_limit = min(retry_limit, error.retry_limit)

                if error.retryable and retry_count < retry_limit:
                    delay = self._retry_delay(error, retry_count, random_uniform)
                    if delay > 0:
                        await sleep(delay)
                    retry_count += 1
                    continue

                if fallback is not None:
                    try:
                        return await fallback()
                    except ProviderExecutionError as fallback_error:
                        info = ProviderErrorInfo(
                            code=fallback_error.info.code,
                            message=fallback_error.info.message,
                            attempt_count=attempt_count + 1,
                            retryable=False,
                            status=fallback_error.info.status,
                            fallback_attempted=True,
                        )
                        raise ProviderExecutionError(info) from None
                    except Exception as fallback_raw_error:  # noqa: BLE001
                        # 备用 Provider 的异常同样不得把原始详情返回给业务层。
                        classified_error = classify_provider_exception(
                            fallback_raw_error
                        )
                        info = self._final_info(
                            classified_error,
                            attempt_count=attempt_count + 1,
                            fallback_attempted=True,
                        )
                        raise ProviderExecutionError(info) from None

                info = self._final_info(error, attempt_count=attempt_count)
                raise ProviderExecutionError(info) from None

    def _retry_delay(
        self,
        error: ProviderCallError,
        retry_index: int,
        random_uniform: Callable[[float, float], float],
    ) -> float:
        if error.retry_after is not None:
            return min(error.retry_after, self.max_retry_after)

        backoff_index = min(retry_index, len(self.backoff_seconds) - 1)
        delay = self.backoff_seconds[backoff_index]
        if self.jitter_seconds:
            delay += random_uniform(0, self.jitter_seconds)
        return min(delay, self.max_retry_after)

    @staticmethod
    def _final_info(
        error: ProviderCallError,
        *,
        attempt_count: int,
        fallback_attempted: bool = False,
    ) -> ProviderErrorInfo:
        return ProviderErrorInfo(
            code=error.code,
            message=error.safe_message,
            attempt_count=attempt_count,
            retryable=error.retryable,
            status=error.code,
            fallback_attempted=fallback_attempted,
        )


__all__ = [
    "AsyncOperation",
    "ProviderCallError",
    "ProviderErrorInfo",
    "ProviderExecutionError",
    "RetryPolicy",
    "classify_provider_exception",
]
