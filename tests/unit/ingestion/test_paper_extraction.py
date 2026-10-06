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


def test_literal_anchors_produce_reading_order_not_question_number_order():
    page = TextPage(uuid4(), 1, "9. First stem.\n2. Second stem.", "TEXT")
    provider = Provider(
        [
            {
                "questions": [
                    question(
                        [1],
                        question_number="2",
                        content="Second stem.",
                        source_anchor={"page_number": 1, "text": "2. Second stem."},
                    ),
                    question(
                        [1],
                        question_number="9",
                        content="First stem.",
                        source_anchor={"page_number": 1, "text": "9. First stem."},
                    ),
                ]
            }
        ]
    )
    questions = run_extract([page], provider)[0].questions
    assert [q.question_number for q in questions] == ["9", "2"]
    assert questions[0].content == "First stem."
    assert questions[0].assets is None and questions[0].source_regions is None
    assert "source_anchor" not in questions[0].model_dump()


def test_option_literal_positions_preserve_keys_and_real_reading_order():
    page = TextPage(
        uuid4(), 1, "9. Stem.\nC. third\nA. first\nB. second\nD. fourth", "OCR"
    )
    sources = {
        key: {"page_number": 1, "text": f"{key}. {value}"}
        for key, value in {
            "A": "first",
            "B": "second",
            "C": "third",
            "D": "fourth",
        }.items()
    }
    provider = Provider(
        [
            {
                "questions": [
                    question(
                        [1],
                        options={
                            "A": "first",
                            "B": "second",
                            "C": "third",
                            "D": "fourth",
                        },
                        source_anchor={"page_number": 1, "text": "9. Stem."},
                        option_sources=sources,
                    )
                ]
            }
        ]
    )
    result = run_extract([page], provider)[0].questions[0]
    assert list(result.options) == ["C", "A", "B", "D"]
    assert result.options["A"] == "first"
    assert "option_sources" not in result.model_dump()


@pytest.mark.parametrize(
    "anchor,text",
    [
        ({"page_number": 2, "text": "9. Stem."}, "9. Stem."),
        ({"page_number": 1, "text": "invented"}, "9. Stem."),
        ({"page_number": 1, "text": "Stem."}, "9. Stem. 2. Stem."),
    ],
)
def test_unproved_or_ambiguous_question_anchor_is_not_guessed(anchor, text):
    provider = Provider([{"questions": [question([1], source_anchor=anchor)]}])
    with pytest.raises(ValueError):
        run_extract([TextPage(uuid4(), 1, text, "TEXT")], provider)


@pytest.mark.parametrize(
    "option_sources",
    [
        {"A": {"page_number": 1, "text": "A. first"}},
        {
            "A": {"page_number": 1, "text": "A. first"},
            "B": {"page_number": 1, "text": "B. invented"},
        },
        {
            "A": {"page_number": 2, "text": "A. first"},
            "B": {"page_number": 1, "text": "B. second"},
        },
    ],
)
def test_option_proof_requires_all_keys_and_real_source(option_sources):
    provider = Provider(
        [
            {
                "questions": [
                    question(
                        [1],
                        options={"A": "first", "B": "second"},
                        option_sources=option_sources,
                    )
                ]
            }
        ]
    )
    with pytest.raises(ValueError):
        run_extract(
            [TextPage(uuid4(), 1, "9. Stem. A. first B. second", "TEXT")], provider
        )


def test_legacy_response_keeps_order_without_inventing_positions():
    provider = Provider(
        [
            {
                "questions": [
                    question([1], question_number="9"),
                    question([1], question_number="2"),
                ]
            }
        ]
    )
    results = run_extract([TextPage(uuid4(), 1, "old text", "TEXT")], provider)[
        0
    ].questions
    assert [q.question_number for q in results] == ["9", "2"]
    assert all(q.assets is None for q in results)


def test_shared_whole_option_line_does_not_prove_distinct_reading_positions():
    text = "9. Stem. A. first B. second"
    provider = Provider(
        [
            {
                "questions": [
                    question(
                        [1],
                        options={"A": "first", "B": "second"},
                        option_sources={
                            key: {"page_number": 1, "text": "A. first B. second"}
                            for key in ("A", "B")
                        },
                    )
                ]
            }
        ]
    )
    with pytest.raises(ValueError, match="distinct positions"):
        run_extract([TextPage(uuid4(), 1, text, "TEXT")], provider)
