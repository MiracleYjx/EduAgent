"""Controlled semantic Provider for technical tests; no teacher-quality claim."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from backend.app.ai.llm.base import BaseLLMProvider, LLMMessages


class StubSemanticProvider(BaseLLMProvider):
    provider_name = "controlled-semantic-fixture"
    model_name = "fixture-four-checks"
    model_version = "fixture-v1"

    def __init__(
        self, *, verdict: str = "pass", error: Exception | None = None
    ) -> None:
        self.verdict = verdict
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    async def generate_structured(
        self, messages: LLMMessages, schema: type[BaseModel], model: str | None = None
    ) -> BaseModel:
        value = json.loads(str(messages[1]["content"]))
        self.calls.append(value)
        if self.error is not None:
            raise self.error
        refs = [item["evidence_id"] for item in value["evidence"]]
        return schema.model_validate(
            {
                "checks": [
                    {
                        "kind": kind,
                        "verdict": self.verdict,
                        "reason": "Controlled protocol conclusion; not measured model quality.",
                        "evidence_refs": refs,
                    }
                    for kind in (
                        "answer_correctness",
                        "condition_sufficiency",
                        "option_ambiguity",
                        "rubric_clarity",
                    )
                ],
                "issues": [],
            }
        )

    async def aclose(self) -> None:
        self.closed = True
