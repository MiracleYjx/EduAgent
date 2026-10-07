import httpx
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError
from backend.app.core.retry_policy import classify_provider_exception


@pytest.mark.parametrize(
    "kind,status,code,retryable",
    [
        (APITimeoutError, None, "ProviderTimeout", True),
        (APIConnectionError, None, "ProviderFailed", True),
        (RateLimitError, 429, "ProviderRateLimited", True),
        (APIStatusError, 400, "ProviderFailed", False),
        (APIStatusError, 503, "ProviderFailed", True),
    ],
)
def test_real_sdk_errors_keep_retry_classification(kind, status, code, retryable):
    request = httpx.Request("POST", "https://example.invalid/v1")
    if status is None:
        error = kind(request=request)
    else:
        response = httpx.Response(status, request=request, headers={"Retry-After": "7"})
        error = kind("sensitive response", response=response, body={"key": "secret"})
    result = classify_provider_exception(error)
    assert result.code == code
    assert result.retryable is retryable
    assert (
        "sensitive" not in result.safe_message and "secret" not in result.safe_message
    )
    if status == 429:
        assert result.retry_after == 7
