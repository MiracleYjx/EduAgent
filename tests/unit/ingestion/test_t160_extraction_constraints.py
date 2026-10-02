"""T160 real failed output shapes and unchanged source contracts; TCR §19."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.app.ai.llm.deepseek import DeepSeekProvider
from backend.app.ai.paper_extraction import PaperExtractor, TextPage
from backend.app.ai.paper_extraction.schemas import ExtractionBatch
from backend.app.core.retry_policy import RetryPolicy
from tests.unit.settings_helpers import build_test_settings


def candidate(**values):
    return {"source_pages": [1], "question_number": "8", "content": "原题", **values}


def evidence(text="原稿答案：甲；解析：原文说明。", page=1):
    return {"page_number": page, "text": text}


class Provider:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.inputs = []

    async def generate_structured(self, messages, schema, model=None):
        self.inputs.append(json.loads(messages[-1]["content"]))
        return schema.model_validate(next(self.responses))


def extract(pages, provider):
    async def run():
        return [
            batch
            async for batch in PaperExtractor(provider, batch_size=1).extract(pages)
        ]

    return asyncio.run(run())


def test_unknown_evidence_keys_are_rejected_at_the_provider_schema_boundary():
    # The prior real 50-page failures supplied knowledge_points evidence.
    with pytest.raises(ValidationError):
        ExtractionBatch.model_validate(
            {
                "questions": [candidate(evidence={"knowledge_points": evidence()})],
            }
        )


def test_published_schema_restricts_evidence_keys_to_the_accepted_contract():
    schema = ExtractionBatch.model_json_schema()["$defs"]["Candidate"]["properties"]
    assert set(schema["evidence"]["propertyNames"]["enum"]) == {
        "reference_answer",
        "scoring_rubric",
        "analysis",
    }


@pytest.mark.parametrize("field", ["reference_answer", "scoring_rubric", "analysis"])
@pytest.mark.parametrize("value", ["原文说明。", "   "])
def test_each_populated_answer_field_requires_its_own_nonempty_evidence(field, value):
    with pytest.raises(ValidationError):
        ExtractionBatch.model_validate({"questions": [candidate(**{field: value})]})


def test_nonliteral_answer_is_rejected_before_business_persistence():
    with pytest.raises(ValidationError):
        ExtractionBatch.model_validate(
            {
                "questions": [
                    candidate(
                        reference_answer="计算产生的新答案",
                        evidence={"reference_answer": evidence()},
                    )
                ]
            }
        )


def test_missing_fields_stay_unknown_and_literal_fields_keep_their_own_evidence():
    parsed = ExtractionBatch.model_validate(
        {
            "questions": [
                candidate(),
                candidate(
                    reference_answer="甲",
                    analysis="原文说明。",
                    evidence={"reference_answer": evidence(), "analysis": evidence()},
                ),
            ]
        }
    )
    assert parsed.questions[0].reference_answer is None
    assert parsed.questions[0].analysis is None
    assert parsed.questions[0].evidence == {}
    assert parsed.questions[1].reference_answer == "甲"
    assert parsed.questions[1].analysis == "原文说明。"


def test_valid_schema_does_not_allow_a_fabricated_original_excerpt():
    provider = Provider(
        [
            {
                "questions": [
                    candidate(
                        reference_answer="甲",
                        evidence={"reference_answer": evidence()},
                    )
                ]
            }
        ]
    )
    with pytest.raises(ValueError, match="absent from its actual source page"):
        extract([TextPage(uuid4(), 1, "原页未给答案", "TEXT")], provider)


def test_completed_indices_are_explicit_and_do_not_reuse_original_numbers():
    pages = [
        TextPage(uuid4(), 1, "8. 原题", "TEXT"),
        TextPage(uuid4(), 2, "8. 原稿答案：甲", "TEXT"),
    ]
    provider = Provider(
        [
            {"questions": [candidate()]},
            {
                "updates": [
                    {
                        "question_index": 1,
                        "reference_answer": "甲",
                        "evidence": {
                            "reference_answer": evidence("8. 原稿答案：甲", 2)
                        },
                    }
                ]
            },
        ]
    )
    batches = extract(pages, provider)
    assert provider.inputs[0]["allowed_update_indices"] == []
    assert provider.inputs[1]["allowed_update_indices"] == [1]
    assert provider.inputs[1]["completed_questions"][0]["question_number"] == "8"
    assert batches[1].updates[0].question_index == 1
    assert batches[1].updates[0].reference_answer == "甲"


def test_same_batch_question_cannot_be_smuggled_into_completed_updates():
    provider = Provider(
        [
            {
                "questions": [candidate()],
                "updates": [
                    {
                        "question_index": 1,
                        "reference_answer": "甲",
                        "evidence": {"reference_answer": evidence()},
                    }
                ],
            }
        ]
    )
    with pytest.raises(ValueError, match="previously completed question"):
        extract([TextPage(uuid4(), 1, evidence()["text"], "TEXT")], provider)


def test_actual_provider_uses_existing_bounded_retry_for_schema_invalid_evidence():
    invalid = {"questions": [candidate(evidence={"knowledge_points": evidence()})]}
    valid = {
        "questions": [
            candidate(reference_answer="甲", evidence={"reference_answer": evidence()})
        ]
    }
    responses = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(output, ensure_ascii=False),
                    )
                )
            ]
        )
        for output in (invalid, valid)
    ]
    create = AsyncMock(side_effect=responses)
    provider = DeepSeekProvider(
        build_test_settings(deepseek_api_key="test-only-placeholder"),
        client=SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        ),
        retry_policy=RetryPolicy(backoff_seconds=(0,), jitter_seconds=0),
    )
    parsed = asyncio.run(
        provider.generate_structured(
            [{"role": "user", "content": "Extract JSON"}],
            ExtractionBatch,
        )
    )
    assert create.await_count == 2
    assert parsed.questions[0].reference_answer == "甲"
