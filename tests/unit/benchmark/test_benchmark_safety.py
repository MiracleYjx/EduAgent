"""T236 default invocation, safe HTTP observation and owned-client lifecycle."""
import asyncio
import json

import httpx
import pytest

from scripts import run_grading_benchmark as grading
from scripts import run_retrieval_benchmark as retrieval
from scripts import setup_benchmark_corpus as setup
from tests.unit.settings_helpers import build_test_settings


def test_retrieval_default_is_fake_and_real_is_explicit(monkeypatch):
    monkeypatch.setattr(retrieval, "create_embedding_provider", lambda: pytest.fail("implicit real call"))
    assert retrieval.parse_args([]).self_test is True
    assert retrieval.parse_args(["--real"]).self_test is False
    assert retrieval._make_embedding_provider(retrieval.parse_args([]).self_test).provider_name == "stub"
    with pytest.raises(SystemExit):
        retrieval.parse_args(["--real", "--self-test"])


def test_setup_default_is_fake_and_closes_provider(tmp_path, monkeypatch):
    observed = []
    monkeypatch.setattr(setup, "setup_corpus", lambda *a, **kw: observed.append(kw) or tmp_path / "manifest.json")
    assert setup.main(["--output-dir", str(tmp_path / "new")]) == 0
    assert observed[0]["provider"].provider_name == "stub"
    assert observed[0]["close_provider"] is True


@pytest.mark.parametrize("failure", [None, RuntimeError, asyncio.CancelledError])
@pytest.mark.parametrize("injected", [False, True])
def test_grading_closes_only_owned_clients_even_on_failure(monkeypatch, failure, injected):
    closed = []
    class Provider(grading.SelfTestScoringProvider):
        async def aclose(self):
            closed.append("llm")
    class Embedding(grading.StubEmbeddingProvider):
        async def aclose(self):
            closed.append("embedding")
    provider = Provider()
    monkeypatch.setattr(grading, "build_provider", lambda *a: provider)
    monkeypatch.setattr(grading, "build_embedding", lambda *a: Embedding())
    async def run(*a, **kw):
        if failure:
            raise failure("synthetic private failure")
        return grading.StrategyRun(strategy="zero_shot")
    monkeypatch.setattr(grading, "run_strategy", run)
    async def invoke():
        await grading._run_strategies({"runs": []}, [], mode="selftest", settings=build_test_settings(),
            strategies=["zero_shot"], corpus_path=None, provider=provider if injected else None)
    if failure:
        with pytest.raises(failure):
            asyncio.run(invoke())
    else:
        asyncio.run(invoke())
    assert closed.count("embedding") == 1
    assert closed.count("llm") == (0 if injected else 1)


def test_http_observer_never_persists_prompt_response_or_secret(tmp_path):
    from benchmark.t160.http_observation import HTTPObservation
    observer = HTTPObservation(tmp_path)
    sentinel = "synthetic-private-request-response"
    async def invoke():
        request = httpx.Request("POST", "https://model.invalid/chat/completions", json={
            "model": "actual-model-v1", "messages": [{"content": sentinel}],
            "response_format": {"type": "json_object", "extra": sentinel}})
        await observer.request(request)
        response = httpx.Response(200, request=request, json={"model": "actual-model-v2", "choices": [sentinel],
            "usage": {"total_tokens": 7, "extra": sentinel}, "secret": sentinel})
        await observer.response(response)
    asyncio.run(invoke())
    raw = (tmp_path / "http-001.json").read_text()
    assert sentinel not in raw
    row = json.loads(raw)
    assert row["actual_response_model"] == "actual-model-v2"
    assert row["usage"] == {"prompt_tokens": None, "completion_tokens": None, "total_tokens": 7}
    assert "response" not in row and "messages" not in row["request"]


def test_unknown_http_provenance_is_not_filled(tmp_path):
    from benchmark.t160.http_observation import HTTPObservation
    observer = HTTPObservation(tmp_path)
    async def invoke():
        request = httpx.Request("POST", "https://model.invalid/chat/completions", json={"model": "requested-only"})
        await observer.request(request)
        await observer.response(httpx.Response(200, request=request, json={"choices": []}))
    asyncio.run(invoke())
    assert observer.attempts[0]["actual_response_model"] is None
    assert all(value is None for value in observer.attempts[0]["usage"].values())
