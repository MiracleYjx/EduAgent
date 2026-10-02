"""T157 structured batching, real source excerpts and cross-page carry. TCR §13."""

import asyncio
import json
from uuid import uuid4

import pytest

from backend.app.ai.paper_extraction import PaperExtractor, TextPage
from backend.app.ai.paper_extraction.schemas import ExtractionBatch


class Provider:
    def __init__(self, results):
        self.results = iter(results)
        self.inputs = []

    async def generate_structured(self, messages, schema, model=None):
        self.inputs.append(json.loads(messages[-1]["content"]))
        return schema.model_validate(next(self.results))


def question(pages, **extra):
    return {
        "source_pages": pages,
        "question_type": "SHORT_ANSWER",
        "content": "解释跨页问题",
        "reference_answer": None,
        "scoring_rubric": None,
        "analysis": None,
        "evidence": {},
        **extra,
    }


def run_extract(pages, provider):
    async def run():
        results = []
        async for batch in PaperExtractor(provider, batch_size=1).extract(pages):
            results.append(batch)
        return results

    return asyncio.run(run())


def test_cross_page_pending_and_later_answer_update_keep_real_source():
    pages = [
        TextPage(uuid4(), 1, "1. 解释跨页", "TEXT"),
        TextPage(uuid4(), 2, "问题。", "TEXT"),
        TextPage(uuid4(), 3, "答案：保持来源。", "TEXT"),
    ]
    provider = Provider(
        [
            {"questions": [], "pending": [{"page_number": 1, "text": "1. 解释跨页"}]},
            {"questions": [question([1, 2])], "pending": []},
            {
                "questions": [],
                "updates": [
                    {
                        "question_index": 1,
                        "reference_answer": "保持来源。",
                        "evidence": {
                            "reference_answer": {
                                "page_number": 3,
                                "text": "答案：保持来源。",
                            }
                        },
                    }
                ],
                "pending": [],
            },
        ]
    )
    results = run_extract(pages, provider)
    assert len(provider.inputs) == 3
    assert provider.inputs[1]["pending"][0]["page_number"] == 1
    assert results[1].questions[0].source_page_ids == [pages[0].id, pages[1].id]
    assert results[2].updates[0].reference_answer == "保持来源。"


@pytest.mark.parametrize(
    "result",
    [
        {"questions": [question([9])]},
        {"questions": [question([1], reference_answer="编造答案")]},
        {"questions": [], "pending": [{"page_number": 1, "text": "不在原文"}]},
        {"questions": [], "pending": [{"page_number": 1, "text": "原文"}]},
    ],
)
def test_unknown_source_invented_answer_and_unfinished_final_are_errors(result):
    provider = Provider([result])
    with pytest.raises(ValueError):
        run_extract([TextPage(uuid4(), 1, "原文", "TEXT")], provider)


def test_invalid_structured_response_is_not_repaired_with_regex():
    with pytest.raises(ValueError):
        ExtractionBatch.model_validate({"questions": "bad"})


def test_missing_answers_are_not_generated():
    provider = Provider([{"questions": [question([1])]}])
    result = run_extract([TextPage(uuid4(), 1, "1. 解释跨页问题", "OCR")], provider)[0]
    extracted = result.questions[0]
    assert extracted.reference_answer is None and extracted.analysis is None
    assert (
        extracted.extracted_by.value == "LLM"
        and extracted.extraction_confidence is None
    )
    assert extracted.assets is None and extracted.source_regions is None
